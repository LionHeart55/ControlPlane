#!/usr/bin/env python3
"""End-to-end Milvus 2.6 operations demo: schema, insert, index, load, search.

Standalone by design. It imports nothing from `control_plane` and talks only to
Milvus, so it can prove a freshly deployed cluster actually works before any of
the control plane exists.

Eleven labelled, timed stages. Every Milvus call runs inside a stage context
manager that names the failing stage in the error message -- "insert failed" is
actionable, a bare gRPC traceback is not.

Host vs container: the control plane runs inside Compose and reaches Milvus at
`milvus-standalone:19530`, but this script normally runs on your host, where
that name does not resolve. Hence the default of `http://localhost:19530`.

Exit codes:
    0  success
    2  bad arguments
    3  could not connect to Milvus
    4  a Milvus operation failed

Usage:
    python ops/milvus_demo.py --rows 5000
    python ops/milvus_demo.py --embedder minilm --rows 200 \\
        --query "how do vector indexes work?" --filter 'category == "tech"'
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

import numpy as np

# Allow `python ops/milvus_demo.py` from the repo root as well as `python -m`.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from embeddings import (
    CATEGORIES,
    CORPUS,
    MINILM_DIM,
    Embedder,
    build_documents,
    build_embedder,
    l2_normalize,
)

EXIT_OK = 0
EXIT_BAD_ARGS = 2
EXIT_CONNECT = 3
EXIT_MILVUS = 4

TOTAL_STAGES = 11

INDEX_PARAMS: dict[str, dict[str, Any]] = {
    "HNSW": {"M": 16, "efConstruction": 200},
    "IVF_FLAT": {"nlist": 128},
}
# Search parameters must match the index: `ef` means nothing to IVF and
# `nprobe` means nothing to HNSW. Milvus ignores the wrong one silently, which
# would leave you tuning a parameter that has no effect.
SEARCH_PARAMS: dict[str, dict[str, Any]] = {
    "HNSW": {"ef": 64},
    "IVF_FLAT": {"nprobe": 16},
}
METRICS = ("COSINE", "L2", "IP")

TEXT_MAX_LEN = 512
CATEGORY_MAX_LEN = 64


class StageError(RuntimeError):
    """A failure inside a named stage, carrying the exit code to use."""

    def __init__(self, stage: str, cause: BaseException, exit_code: int = EXIT_MILVUS) -> None:
        super().__init__(f"stage {stage!r} failed: {type(cause).__name__}: {cause}")
        self.stage = stage
        self.cause = cause
        self.exit_code = exit_code


@dataclass
class StageRecord:
    name: str
    seconds: float
    detail: str = ""


@dataclass
class Note:
    """Mutable handle a stage body uses to report what it did."""

    detail: str = ""


@dataclass
class Reporter:
    """Prints labelled, timed stages and records them for --json-out."""

    verbose: bool = False
    stream: TextIO = field(default_factory=lambda: sys.stdout)
    records: list[StageRecord] = field(default_factory=list)
    _index: int = 0

    def line(self, text: str = "") -> None:
        print(text, file=self.stream, flush=True)

    def detail_line(self, text: str) -> None:
        """Indented sub-output, e.g. per-batch insert progress."""
        print(f"          {text}", file=self.stream, flush=True)

    def debug(self, text: str) -> None:
        if self.verbose:
            self.detail_line(text)

    @contextlib.contextmanager
    def stage(self, name: str, *, exit_code: int = EXIT_MILVUS) -> Iterator[Note]:
        self._index += 1
        note = Note()
        self.line(f"[{self._index:2d}/{TOTAL_STAGES}] {name}")
        started = time.perf_counter()
        try:
            yield note
        except StageError:
            raise
        except Exception as exc:
            elapsed = time.perf_counter() - started
            self.records.append(StageRecord(name, round(elapsed, 3), f"FAILED: {exc}"))
            print(f"        !! {elapsed:6.2f}s  {exc}", file=self.stream, flush=True)
            raise StageError(name, exc, exit_code) from exc
        elapsed = time.perf_counter() - started
        self.records.append(StageRecord(name, round(elapsed, 3), note.detail))
        print(f"        ok {elapsed:6.2f}s  {note.detail}", file=self.stream, flush=True)


# --- arguments ------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="milvus_demo.py",
        description="End-to-end Milvus 2.6 demo: schema, insert, index, load, search.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        epilog="Exit codes: 0 success, 2 bad arguments, 3 connect failure, 4 Milvus error.",
    )
    parser.add_argument("--uri", default="http://localhost:19530", help="Milvus endpoint.")
    parser.add_argument("--token", default=None, help="Auth token, if the server requires one.")
    parser.add_argument("--collection", default="demo_docs", help="Collection name.")
    parser.add_argument("--dim", type=int, default=384, help="Vector dimension.")
    parser.add_argument("--rows", type=int, default=5000, help="Rows to insert.")
    parser.add_argument("--batch", type=int, default=1000, help="Insert batch size.")
    parser.add_argument("--index", default="HNSW", choices=sorted(INDEX_PARAMS), help="Index type.")
    parser.add_argument("--metric", default="COSINE", choices=METRICS, help="Distance metric.")
    parser.add_argument(
        "--embedder",
        default="random",
        choices=("random", "minilm"),
        help="random needs no downloads; minilm gives semantically meaningful neighbours.",
    )
    parser.add_argument("--topk", type=int, default=5, help="Results to return.")
    parser.add_argument(
        "--filter", default=None, help="Boolean expression, e.g. 'category == \"tech\"'."
    )
    parser.add_argument(
        "--query",
        default=None,
        help="Query text. Only meaningful with --embedder minilm; random uses a random vector.",
    )
    parser.add_argument(
        "--drop-existing", action="store_true", help="Drop the collection first if it exists."
    )
    parser.add_argument(
        "--keep", action="store_true", help="Do not drop the collection at the end."
    )
    parser.add_argument("--json-out", default=None, help="Write a machine-readable summary here.")
    parser.add_argument("--timeout", type=float, default=30.0, help="Per-call timeout in seconds.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Extra detail per stage.")
    return parser


def validate(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    """Reject impossible combinations before touching the network.

    argparse already exits 2 on a parse error; these keep that same code for
    values that parse fine but cannot work.
    """
    if args.rows <= 0:
        parser.error("--rows must be positive")
    if args.batch <= 0:
        parser.error("--batch must be positive")
    if args.topk <= 0:
        parser.error("--topk must be positive")
    if args.dim <= 0:
        parser.error("--dim must be positive")
    if args.topk > args.rows:
        parser.error(f"--topk ({args.topk}) cannot exceed --rows ({args.rows})")
    if args.embedder == "minilm" and args.dim != MINILM_DIM:
        # An override rather than an error: the model's output dimension is
        # fixed, so any other value could only ever fail at insert time.
        print(
            f"note: --embedder minilm fixes the dimension at {MINILM_DIM}; "
            f"ignoring --dim {args.dim}",
            file=sys.stderr,
        )
        args.dim = MINILM_DIM
    if args.query and args.embedder == "random":
        print(
            "note: --query is ignored with --embedder random, which searches with a "
            "random vector; use --embedder minilm for a text query",
            file=sys.stderr,
        )


# --- stages ---------------------------------------------------------------
def stage_connect(
    reporter: Reporter, uri: str, token: str | None, timeout: float
) -> tuple[Any, str]:
    """1. Connect and report the server version."""
    from pymilvus import MilvusClient

    with reporter.stage("connect", exit_code=EXIT_CONNECT) as note:
        client = MilvusClient(uri=uri, token=token, timeout=timeout)
        version = str(client.get_server_version(timeout=timeout))
        note.detail = f"Milvus {version} at {uri}"
    return client, version


def stage_schema(reporter: Reporter, client: Any, dim: int) -> Any:
    """2. Build the schema."""
    from pymilvus import DataType

    with reporter.stage("schema") as note:
        schema = client.create_schema(auto_id=True, enable_dynamic_field=True)
        # DataType enums, not strings: add_field validates the type and rejects
        # a string with "Field dtype must be of DataType".
        schema.add_field("id", DataType.INT64, is_primary=True)
        schema.add_field("vector", DataType.FLOAT_VECTOR, dim=dim)
        schema.add_field("text", DataType.VARCHAR, max_length=TEXT_MAX_LEN)
        schema.add_field("category", DataType.VARCHAR, max_length=CATEGORY_MAX_LEN)
        schema.add_field("created_at", DataType.INT64)
        note.detail = f"5 fields, vector dim={dim}, auto_id, dynamic field enabled"
    return schema


def stage_create_collection(
    reporter: Reporter, client: Any, name: str, schema: Any, drop_existing: bool, timeout: float
) -> bool:
    """3. Create the collection, without an index.

    Deliberately no index here: creating it as its own stage makes the build
    time visible, which is the number that actually matters when tuning.
    """
    with reporter.stage("create collection") as note:
        existed = client.has_collection(name, timeout=timeout)
        if existed and drop_existing:
            client.drop_collection(name, timeout=timeout)
            reporter.detail_line(f"dropped pre-existing collection {name!r}")
        elif existed:
            raise RuntimeError(
                f"collection {name!r} already exists; pass --drop-existing to replace it"
            )
        client.create_collection(name, schema=schema, timeout=timeout)
        note.detail = f"{name!r} created (no index yet)"
    return existed


def stage_generate(
    reporter: Reporter, embedder: Embedder, rows: int
) -> tuple[list[Any], np.ndarray]:
    """4. Generate embeddings."""
    with reporter.stage("generate embeddings") as note:
        documents, vectors, tiling = build_documents(rows, embedder)
        norms = np.linalg.norm(vectors, axis=1)
        detail = (
            f"{len(vectors)} x {vectors.shape[1]}d via {embedder.name}, "
            f"L2 norm {norms.min():.4f}-{norms.max():.4f}"
        )
        if tiling > 1:
            detail += f", corpus of {len(CORPUS)} tiled {tiling}x"
            reporter.detail_line(
                f"note: {embedder.name} embeds {len(CORPUS)} unique sentences; "
                f"top-k may contain duplicates"
            )
        note.detail = detail
    return documents, vectors


def stage_insert(
    reporter: Reporter,
    client: Any,
    name: str,
    documents: list[Any],
    vectors: np.ndarray,
    batch: int,
    timeout: float,
) -> dict[str, Any]:
    """5. Insert in batches, then flush."""
    with reporter.stage("insert") as note:
        now = int(time.time())
        started = time.perf_counter()
        inserted = 0
        total_batches = -(-len(documents) // batch)

        for batch_no, start in enumerate(range(0, len(documents), batch), start=1):
            chunk = documents[start : start + batch]
            payload = [
                {
                    "vector": vectors[start + offset].tolist(),
                    "text": doc.text,
                    "category": doc.category,
                    "created_at": now + start + offset,
                }
                for offset, doc in enumerate(chunk)
            ]
            batch_started = time.perf_counter()
            result = client.insert(name, payload, timeout=timeout)
            inserted += int(result.get("insert_count", len(payload)))
            reporter.detail_line(
                f"batch {batch_no}/{total_batches}  {len(payload):>5} rows  "
                f"{time.perf_counter() - batch_started:5.2f}s"
            )

        # flush() seals the growing segment. Without it the row count read back
        # in the stats stage can lag behind what was actually inserted.
        client.flush(name, timeout=timeout)
        elapsed = time.perf_counter() - started
        rate = inserted / elapsed if elapsed > 0 else 0.0
        note.detail = f"{inserted} rows in {total_batches} batches, {rate:,.0f} rows/s"
    return {"rows": inserted, "seconds": round(elapsed, 3), "rows_per_second": round(rate, 1)}


def stage_build_index(
    reporter: Reporter, client: Any, name: str, index_type: str, metric: str, timeout: float
) -> dict[str, Any]:
    """6. Build the vector index plus an INVERTED scalar index on category.

    The scalar index is what makes `--filter 'category == "tech"'` meaningful:
    without it Milvus brute-force scans every row to evaluate the predicate,
    so the filtered search measures scan speed rather than index behaviour.
    """
    with reporter.stage("build index") as note:
        index_params = client.prepare_index_params()
        index_params.add_index(
            field_name="vector",
            index_type=index_type,
            metric_type=metric,
            params=INDEX_PARAMS[index_type],
        )
        index_params.add_index(field_name="category", index_type="INVERTED")
        client.create_index(name, index_params, timeout=timeout, sync=True)

        described = dict(client.describe_index(name, "vector", timeout=timeout) or {})
        reporter.debug(f"vector index: {described}")
        note.detail = (
            f"{index_type}/{metric} {INDEX_PARAMS[index_type]} on vector, INVERTED on category"
        )
    return described


def stage_load(reporter: Reporter, client: Any, name: str, timeout: float) -> str:
    """7. Load into memory, polling until the state is Loaded."""
    with reporter.stage("load collection") as note:
        client.load_collection(name, timeout=timeout)
        deadline = time.perf_counter() + timeout
        state = "Unknown"
        while time.perf_counter() < deadline:
            raw = client.get_load_state(name, timeout=timeout)
            value = raw.get("state") if isinstance(raw, dict) else raw
            state = str(getattr(value, "name", value)).split(".")[-1]
            if state == "Loaded":
                break
            time.sleep(0.25)
        else:
            raise TimeoutError(f"collection did not reach Loaded within {timeout}s (last: {state})")
        note.detail = f"state={state}"
    return state


def build_query_vector(
    embedder: Embedder, query: str | None, vectors: np.ndarray
) -> tuple[list[float], str]:
    """Return (vector, description) for the search."""
    if embedder.name == "minilm":
        text = query or "how does an approximate nearest neighbour index work?"
        return embedder.encode([text])[0].tolist(), f"text {text!r}"
    # A random query vector, normalised so the COSINE scores are comparable
    # with the indexed data.
    rng = np.random.default_rng(7)
    vector = l2_normalize(rng.normal(size=(1, vectors.shape[1])))[0]
    return vector.tolist(), "a random unit vector"


def stage_search(
    reporter: Reporter,
    client: Any,
    name: str,
    query_vector: list[float],
    query_desc: str,
    index_type: str,
    topk: int,
    expr: str | None,
    timeout: float,
) -> tuple[list[dict[str, Any]], float]:
    """8. Search."""
    with reporter.stage("search") as note:
        params = SEARCH_PARAMS[index_type]
        started = time.perf_counter()
        raw = client.search(
            name,
            data=[query_vector],
            anns_field="vector",
            search_params=params,
            limit=topk,
            output_fields=["text", "category"],
            filter=expr or "",
            timeout=timeout,
        )
        latency_ms = (time.perf_counter() - started) * 1000
        hits = list(raw[0]) if raw else []
        note.detail = (
            f"{len(hits)} hits in {latency_ms:.1f}ms, params={params}, query={query_desc}"
            + (f", filter={expr!r}" if expr else "")
        )
    return [dict(h) for h in hits], latency_ms


def stage_display(
    reporter: Reporter, hits: list[dict[str, Any]], latency_ms: float, metric: str
) -> list[dict[str, Any]]:
    """9. Print an aligned table of results."""
    rows: list[dict[str, Any]] = []
    with reporter.stage("display results") as note:
        # COSINE and IP are similarities (higher is better); L2 is a distance
        # (lower is better). Labelling the column correctly matters -- reading
        # a COSINE score as a distance inverts the ranking.
        label = "distance" if metric == "L2" else "score"
        header = f"{'rank':>4}  {'id':>19}  {label:>10}  {'category':<10}  text"
        reporter.line()
        reporter.detail_line(header)
        reporter.detail_line(f"{'-' * 4}  {'-' * 19}  {'-' * 10}  {'-' * 10}  {'-' * 60}")
        for rank, hit in enumerate(hits, start=1):
            entity = hit.get("entity") or {}
            text = str(entity.get("text", ""))
            truncated = text if len(text) <= 60 else text[:57] + "..."
            category = str(entity.get("category", ""))
            score = float(hit.get("distance", 0.0))
            reporter.detail_line(
                f"{rank:>4}  {hit.get('id', ''):>19}  {score:>10.4f}  {category:<10}  {truncated}"
            )
            rows.append(
                {
                    "rank": rank,
                    "id": hit.get("id"),
                    "score": round(score, 6),
                    "category": category,
                    "text": text,
                }
            )
        reporter.line()
        note.detail = f"{len(rows)} ranked results, query latency {latency_ms:.1f}ms"
    return rows


def stage_stats(reporter: Reporter, client: Any, name: str, timeout: float) -> dict[str, Any]:
    """10. Row count and a schema summary."""
    with reporter.stage("stats") as note:
        stats = dict(client.get_collection_stats(name, timeout=timeout) or {})
        described = dict(client.describe_collection(name, timeout=timeout) or {})
        fields = described.get("fields") or []
        row_count = int(stats.get("row_count", 0) or 0)
        reporter.debug(f"fields: {[f.get('name') for f in fields]}")
        note.detail = (
            f"row_count={row_count:,}, {len(fields)} fields, auto_id={described.get('auto_id')}"
        )
    return {
        "row_count": row_count,
        "field_count": len(fields),
        "fields": [f.get("name") for f in fields],
        "auto_id": described.get("auto_id"),
    }


def stage_cleanup(reporter: Reporter, client: Any, name: str, keep: bool, timeout: float) -> bool:
    """11. Drop unless --keep."""
    with reporter.stage("cleanup") as note:
        if keep:
            note.detail = f"kept {name!r} (--keep)"
            return False
        client.drop_collection(name, timeout=timeout)
        note.detail = f"dropped {name!r}"
    return True


# --- orchestration --------------------------------------------------------
def run(args: argparse.Namespace, reporter: Reporter) -> dict[str, Any]:
    """Run all eleven stages. Raises StageError on the first failure."""
    client, version = stage_connect(reporter, args.uri, args.token, args.timeout)
    try:
        embedder = build_embedder(args.embedder, args.dim)
        schema = stage_schema(reporter, client, args.dim)
        stage_create_collection(
            reporter, client, args.collection, schema, args.drop_existing, args.timeout
        )
        documents, vectors = stage_generate(reporter, embedder, args.rows)
        insert_stats = stage_insert(
            reporter, client, args.collection, documents, vectors, args.batch, args.timeout
        )
        index_info = stage_build_index(
            reporter, client, args.collection, args.index, args.metric, args.timeout
        )
        load_state = stage_load(reporter, client, args.collection, args.timeout)
        query_vector, query_desc = build_query_vector(embedder, args.query, vectors)
        hits, latency_ms = stage_search(
            reporter,
            client,
            args.collection,
            query_vector,
            query_desc,
            args.index,
            args.topk,
            args.filter,
            args.timeout,
        )
        results = stage_display(reporter, hits, latency_ms, args.metric)
        stats = stage_stats(reporter, client, args.collection, args.timeout)
        dropped = stage_cleanup(reporter, client, args.collection, args.keep, args.timeout)
    finally:
        with contextlib.suppress(Exception):
            client.close()

    return {
        "ok": True,
        "uri": args.uri,
        "collection": args.collection,
        "server_version": version,
        "embedder": args.embedder,
        "dim": args.dim,
        "rows_requested": args.rows,
        "index_type": args.index,
        "metric_type": args.metric,
        "index": {k: v for k, v in index_info.items() if isinstance(v, str | int | float | bool)},
        "load_state": load_state,
        "insert": insert_stats,
        "search": {
            "topk": args.topk,
            "filter": args.filter,
            "query": query_desc,
            "latency_ms": round(latency_ms, 2),
            "results": results,
        },
        "stats": stats,
        "dropped": dropped,
        "corpus_size": len(CORPUS),
        "categories": list(CATEGORIES),
    }


def write_json(path: str, payload: dict[str, Any], reporter: Reporter) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    reporter.line(f"wrote {target}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    validate(args, parser)

    reporter = Reporter(verbose=args.verbose)
    reporter.line("=" * 74)
    reporter.line(
        f" Milvus operations demo — {args.rows:,} rows, {args.index}/{args.metric}, "
        f"embedder={args.embedder}"
    )
    reporter.line("=" * 74)

    started = time.perf_counter()
    try:
        summary = run(args, reporter)
    except StageError as exc:
        total = time.perf_counter() - started
        reporter.line()
        reporter.line(f"FAILED after {total:.2f}s in stage {exc.stage!r}")
        reporter.line(f"  {type(exc.cause).__name__}: {exc.cause}")
        if exc.exit_code == EXIT_CONNECT:
            reporter.line(f"  Is Milvus running and reachable at {args.uri}?")
            reporter.line("  Try: make up   (or: docker ps --filter name=milvus-standalone)")
        if args.json_out:
            write_json(
                args.json_out,
                {
                    "ok": False,
                    "failed_stage": exc.stage,
                    "error": str(exc.cause),
                    "error_type": type(exc.cause).__name__,
                    "exit_code": exc.exit_code,
                    "stages": [vars(r) for r in reporter.records],
                    "total_seconds": round(total, 3),
                },
                reporter,
            )
        return exc.exit_code
    except RuntimeError as exc:
        # build_embedder / missing optional dependency: a usage problem.
        reporter.line()
        reporter.line(f"error: {exc}")
        return EXIT_BAD_ARGS

    total = time.perf_counter() - started
    summary["stages"] = [vars(r) for r in reporter.records]
    summary["total_seconds"] = round(total, 3)

    reporter.line("=" * 74)
    reporter.line(
        f" done in {total:.2f}s — {summary['insert']['rows']:,} rows, "
        f"{len(summary['search']['results'])} ranked results"
    )
    reporter.line("=" * 74)

    if args.json_out:
        write_json(args.json_out, summary, reporter)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
