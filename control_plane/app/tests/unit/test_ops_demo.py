"""The operations script's pure logic. No Milvus, no network.

Every stage lives in its own function precisely so this is possible; the parts
worth pinning are the ones that would fail silently rather than loudly --
un-normalised vectors, mismatched search parameters, a stage error that loses
the stage name.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import numpy as np
import pytest

OPS = Path(__file__).resolve().parents[4] / "ops"
sys.path.insert(0, str(OPS))

from embeddings import (  # noqa: E402
    CATEGORIES,
    CORPUS,
    MINILM_DIM,
    RandomEmbedder,
    build_documents,
    build_embedder,
    l2_normalize,
)
from milvus_demo import (  # noqa: E402
    EXIT_CONNECT,
    EXIT_MILVUS,
    INDEX_PARAMS,
    SEARCH_PARAMS,
    Reporter,
    StageError,
    build_parser,
    build_query_vector,
    validate,
)


# --- normalisation --------------------------------------------------------
def test_random_vectors_are_unit_length() -> None:
    """COSINE is the dot product of unit vectors.

    Without normalisation the ranking is dominated by magnitude and the
    "similarity" scores mean nothing -- a failure that produces plausible
    output rather than an error, which is why it is pinned here.
    """
    vectors = RandomEmbedder(dim=64).encode(["a"] * 100)
    norms = np.linalg.norm(vectors, axis=1)
    assert np.allclose(norms, 1.0, atol=1e-6)


def test_zero_vector_does_not_become_nan() -> None:
    """A NaN row would be rejected by Milvus at insert time."""
    out = l2_normalize(np.zeros((2, 8)))
    assert not np.isnan(out).any()
    assert (out == 0).all()


def test_vectors_are_float32() -> None:
    """Milvus FLOAT_VECTOR is float32; float64 is silently converted or rejected."""
    assert RandomEmbedder(dim=16).encode(["a"]).dtype == np.float32


def test_random_embedder_is_deterministic() -> None:
    a = RandomEmbedder(dim=32, seed=99).encode(["x", "y"])
    b = RandomEmbedder(dim=32, seed=99).encode(["x", "y"])
    assert np.array_equal(a, b), "two runs must produce comparable data"


# --- corpus and documents -------------------------------------------------
def test_corpus_covers_several_distinct_topics() -> None:
    """Near-synonyms would make any embedder look good."""
    assert len(CATEGORIES) >= 5
    assert len(CORPUS) >= 40


def test_corpus_text_fits_the_varchar_columns() -> None:
    from milvus_demo import CATEGORY_MAX_LEN, TEXT_MAX_LEN

    for doc in CORPUS:
        assert len(doc.text) <= TEXT_MAX_LEN
        assert len(doc.category) <= CATEGORY_MAX_LEN


def test_documents_are_generated_for_every_requested_row() -> None:
    embedder = RandomEmbedder(dim=8)
    docs, vectors, tiling = build_documents(5000, embedder)
    assert len(docs) == 5000
    assert vectors.shape == (5000, 8)
    assert tiling == 1, "random gives every row its own vector"


def test_more_rows_than_corpus_still_works() -> None:
    docs, vectors, _ = build_documents(len(CORPUS) * 3 + 7, RandomEmbedder(dim=4))
    assert len(docs) == len(vectors)


def test_zero_rows_is_rejected() -> None:
    with pytest.raises(ValueError, match="positive"):
        build_documents(0, RandomEmbedder(dim=4))


def test_unknown_embedder_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown embedder"):
        build_embedder("word2vec", 128)


# --- search parameters ----------------------------------------------------
def test_every_index_type_has_matching_search_params() -> None:
    """`ef` means nothing to IVF and `nprobe` nothing to HNSW.

    Milvus ignores the wrong key silently, so a mismatch leaves you tuning a
    parameter that has no effect.
    """
    assert set(INDEX_PARAMS) == set(SEARCH_PARAMS)
    assert "ef" in SEARCH_PARAMS["HNSW"]
    assert "nprobe" in SEARCH_PARAMS["IVF_FLAT"]
    assert "M" in INDEX_PARAMS["HNSW"] and "efConstruction" in INDEX_PARAMS["HNSW"]
    assert "nlist" in INDEX_PARAMS["IVF_FLAT"]


def test_query_vector_is_normalised_and_right_sized() -> None:
    embedder = RandomEmbedder(dim=32)
    _, vectors, _ = build_documents(10, embedder)
    vector, description = build_query_vector(embedder, None, vectors)
    assert len(vector) == 32
    assert np.isclose(np.linalg.norm(vector), 1.0, atol=1e-6)
    assert "random" in description


# --- argument validation --------------------------------------------------
def parse(argv: list[str]) -> Any:
    parser = build_parser()
    args = parser.parse_args(argv)
    validate(args, parser)
    return args


@pytest.mark.parametrize(
    "argv",
    [
        ["--rows", "0"],
        ["--rows", "-1"],
        ["--batch", "0"],
        ["--topk", "0"],
        ["--dim", "0"],
        ["--topk", "10", "--rows", "5"],
        ["--index", "NOPE"],
        ["--metric", "NOPE"],
        ["--embedder", "word2vec"],
    ],
)
def test_bad_arguments_exit_2(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as ei:
        parse(argv)
    assert ei.value.code == 2, "bad arguments must exit 2, not crash"


def test_defaults_match_the_documented_contract() -> None:
    args = parse([])
    assert args.rows == 5000
    assert args.topk == 5
    assert args.batch == 1000
    assert args.index == "HNSW"
    assert args.metric == "COSINE"
    assert args.embedder == "random"
    assert args.uri == "http://localhost:19530", "the script runs on the host, not in Compose"


def test_minilm_forces_the_model_dimension() -> None:
    """384 is fixed by the model; any other value could only fail at insert."""
    args = parse(["--embedder", "minilm", "--dim", "128"])
    assert args.dim == MINILM_DIM


# --- stage error handling -------------------------------------------------
def test_stage_error_names_the_failing_stage() -> None:
    """A bare gRPC traceback is not actionable; "insert failed" is."""
    reporter = Reporter()
    with pytest.raises(StageError) as ei, reporter.stage("insert"):
        raise ValueError("boom")
    assert ei.value.stage == "insert"
    assert "insert" in str(ei.value)
    assert ei.value.exit_code == EXIT_MILVUS
    assert isinstance(ei.value.cause, ValueError)


def test_connect_stage_carries_its_own_exit_code() -> None:
    reporter = Reporter()
    with pytest.raises(StageError) as ei, reporter.stage("connect", exit_code=EXIT_CONNECT):
        raise OSError("refused")
    assert ei.value.exit_code == EXIT_CONNECT


def test_stages_are_recorded_with_timings() -> None:
    reporter = Reporter()
    with reporter.stage("one") as note:
        note.detail = "did a thing"
    with reporter.stage("two"):
        pass
    assert [r.name for r in reporter.records] == ["one", "two"]
    assert reporter.records[0].detail == "did a thing"
    assert all(r.seconds >= 0 for r in reporter.records)


def test_failed_stage_is_still_recorded() -> None:
    """--json-out must show where it got to, not an empty list."""
    reporter = Reporter()
    with pytest.raises(StageError), reporter.stage("build index"):
        raise RuntimeError("nope")
    assert len(reporter.records) == 1
    assert reporter.records[0].detail.startswith("FAILED")
