"""Embedding backends and the built-in corpus for the operations demo.

Two backends, for two different purposes:

  * ``random`` -- numpy normal vectors, **L2-normalised**. The default, because
    it needs no model download and makes the script runnable on a bare machine.
    Normalisation is not cosmetic: COSINE similarity is the dot product of unit
    vectors, so without it the "distances" Milvus returns are dominated by
    vector magnitude and the ranking is meaningless. IP has the same problem.
  * ``minilm`` -- sentence-transformers/all-MiniLM-L6-v2 over the corpus below,
    which produces neighbours you can actually eyeball for sense. Its output
    dimension is fixed at 384 by the model.

The corpus is deliberately built from six well-separated topics. A corpus of
near-synonyms would make any embedder look good; distinct topics mean a
nearest-neighbour result is either obviously right or obviously wrong.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np

MINILM_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
MINILM_DIM = 384


@dataclass(frozen=True)
class Document:
    text: str
    category: str


# Six topics, ~8 sentences each. Categories double as the values for the
# INVERTED scalar index, so filtered search has something meaningful to filter.
CORPUS: tuple[Document, ...] = (
    # --- tech ---
    Document("Vector databases index high-dimensional embeddings for similarity search.", "tech"),
    Document("Kubernetes schedules containers across a cluster of worker nodes.", "tech"),
    Document("A circuit breaker stops an application hammering a failing dependency.", "tech"),
    Document(
        "HNSW builds a navigable small-world graph for approximate nearest neighbours.", "tech"
    ),
    Document("Postgres uses write-ahead logging to guarantee durability after a crash.", "tech"),
    Document("Load balancers spread incoming requests over several backend servers.", "tech"),
    Document("Object storage systems keep data as immutable blobs addressed by key.", "tech"),
    Document("Continuous integration runs the test suite on every pushed commit.", "tech"),
    # --- science ---
    Document(
        "Photosynthesis converts sunlight into chemical energy inside plant cells.", "science"
    ),
    Document("The mitochondrion generates most of the cell's supply of ATP.", "science"),
    Document("Neutron stars are the collapsed cores of massive supergiant stars.", "science"),
    Document("DNA stores genetic information in sequences of four nucleotide bases.", "science"),
    Document("Plate tectonics explains how continents drift across the Earth's mantle.", "science"),
    Document("Vaccines train the immune system to recognise a specific pathogen.", "science"),
    Document(
        "Superconductors conduct electricity with no resistance below a critical temperature.",
        "science",
    ),
    Document("The speed of light in a vacuum is constant for every observer.", "science"),
    # --- food ---
    Document("Slowly caramelising onions brings out their natural sweetness.", "food"),
    Document("Sourdough bread rises using a starter of wild yeast and bacteria.", "food"),
    Document("Fresh basil, tomato and mozzarella make a classic caprese salad.", "food"),
    Document("Searing a steak over high heat develops a rich brown crust.", "food"),
    Document("Risotto needs constant stirring so the rice releases its starch.", "food"),
    Document("Miso paste adds a deep savoury umami flavour to soups.", "food"),
    Document("Tempering chocolate gives it a glossy finish and a clean snap.", "food"),
    Document("A good stock simmers for hours with bones and aromatic vegetables.", "food"),
    # --- sport ---
    Document("The marathon covers a distance of forty-two point two kilometres.", "sport"),
    Document("A cricket test match can last for five days of play.", "sport"),
    Document("Cyclists draft behind one another to save energy in the peloton.", "sport"),
    Document("The goalkeeper is the only player allowed to handle the ball.", "sport"),
    Document("Rowing crews must synchronise their stroke rate exactly to stay fast.", "sport"),
    Document("A tennis player wins a set by taking six games with a two-game margin.", "sport"),
    Document("Climbers grade routes by difficulty before attempting an ascent.", "sport"),
    Document("Sprinters explode from the blocks in the first few metres of a race.", "sport"),
    # --- travel ---
    Document("The night train from Vienna to Venice crosses the Alps while you sleep.", "travel"),
    Document("Iceland's ring road passes waterfalls, glaciers and black sand beaches.", "travel"),
    Document("Kyoto's temples are busiest during the cherry blossom season.", "travel"),
    Document("A layover long enough to leave the airport turns a stop into a visit.", "travel"),
    Document("Hiking the coastal path takes four days with overnight stops in villages.", "travel"),
    Document("Booking a window seat on the left gives the better view on that route.", "travel"),
    Document("The old town is best explored on foot early in the morning.", "travel"),
    Document("Ferries between the islands run less often outside the summer months.", "travel"),
    # --- finance ---
    Document("Compound interest grows a balance faster the longer it is left alone.", "finance"),
    Document("Diversifying a portfolio reduces exposure to any single asset.", "finance"),
    Document("Central banks raise interest rates to bring down inflation.", "finance"),
    Document("An index fund tracks a market benchmark at very low cost.", "finance"),
    Document("Bond prices fall when prevailing interest rates rise.", "finance"),
    Document("Liquidity measures how quickly an asset converts to cash.", "finance"),
    Document("Hedging offsets a potential loss with an opposing position.", "finance"),
    Document("Currency exchange rates float according to supply and demand.", "finance"),
)

CATEGORIES: tuple[str, ...] = tuple(dict.fromkeys(d.category for d in CORPUS))


def l2_normalize(matrix: np.ndarray) -> np.ndarray:
    """Scale each row to unit length.

    Required for COSINE and IP to mean anything. Zero-length rows would divide
    by zero, so their norm is clamped to 1 -- they stay zero vectors rather
    than becoming NaN, which Milvus would reject on insert.
    """
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (matrix / norms).astype(np.float32)


class Embedder(Protocol):
    """What the demo needs from an embedding backend."""

    name: str
    dim: int

    def encode(self, texts: list[str]) -> np.ndarray: ...


class RandomEmbedder:
    """Normally-distributed unit vectors.

    Deterministic given a seed, so two runs of the demo produce identical data
    and a search result can be compared between them.
    """

    def __init__(self, dim: int, seed: int = 1234) -> None:
        self.name = "random"
        self.dim = dim
        self._rng = np.random.default_rng(seed)

    def encode(self, texts: list[str]) -> np.ndarray:
        raw = self._rng.normal(size=(len(texts), self.dim))
        return l2_normalize(raw)


class MiniLMEmbedder:
    """all-MiniLM-L6-v2. Output dimension is fixed at 384 by the model."""

    def __init__(self, model_name: str = MINILM_MODEL) -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise RuntimeError(
                "--embedder minilm needs sentence-transformers, which is left out of "
                "ops/requirements.txt by default because it pulls in torch and ~90 MB "
                "of model weights.\n"
                "  Install it with:  pip install 'sentence-transformers>=3.0,<6'\n"
                "  Or use the default:  --embedder random"
            ) from exc

        self.name = "minilm"
        self.dim = MINILM_DIM
        self._model = SentenceTransformer(model_name)

    def encode(self, texts: list[str]) -> np.ndarray:
        vectors = self._model.encode(
            texts, batch_size=64, show_progress_bar=False, convert_to_numpy=True
        )
        # Normalised again rather than trusting normalize_embeddings=True: the
        # COSINE ranking is only meaningful on unit vectors, and this is cheap
        # insurance against a model or version that does not normalise.
        return l2_normalize(np.asarray(vectors, dtype=np.float32))


def build_embedder(kind: str, dim: int) -> Embedder:
    if kind == "random":
        return RandomEmbedder(dim=dim)
    if kind == "minilm":
        return MiniLMEmbedder()
    raise ValueError(f"unknown embedder {kind!r}; expected 'random' or 'minilm'")


def build_documents(rows: int, embedder: Embedder) -> tuple[list[Document], np.ndarray, int]:
    """Return (documents, vectors, tiling_factor) for `rows` records.

    The corpus is smaller than a realistic row count, so it is tiled. For
    `minilm` that means the vectors genuinely repeat, and the tiling factor is
    returned so the caller can say so out loud -- duplicate top-k hits would
    otherwise look like a bug in the search rather than a property of the data.

    For `random` every row gets its own vector regardless; only the text and
    category are recycled.
    """
    if rows <= 0:
        raise ValueError("rows must be positive")

    documents = [CORPUS[i % len(CORPUS)] for i in range(rows)]
    tiling = -(-rows // len(CORPUS))  # ceil

    if embedder.name == "random":
        vectors = embedder.encode([d.text for d in documents])
        return documents, vectors, 1

    # Encode each unique sentence once, then tile. Embedding 5 000 copies of 48
    # sentences would be 100x the work for identical output.
    unique = embedder.encode([d.text for d in CORPUS])
    index = np.arange(rows) % len(CORPUS)
    return documents, unique[index], tiling
