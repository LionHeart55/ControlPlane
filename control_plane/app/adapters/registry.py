"""Process-wide adapter instances, keyed by endpoint.

Adapters are stateful in ways that matter and are easy to lose by accident:

  * `MilvusAdapter` caches one pymilvus client (one gRPC channel) per endpoint.
    Constructing a fresh adapter per job run would open a new channel every
    15 seconds and, worse, throw away the circuit breaker's failure count so it
    could never reach `fail_max` and open.
  * `MetricsAdapter` holds a pooled `httpx.AsyncClient`.
  * `DockerComponentAdapter` holds a socket connection it rebuilds on failure.

So they live here, created once and reused. Everything is keyed by endpoint
because the schema is multi-cluster: two registered clusters must not share a
breaker, or one cluster's outage would blind the control plane to the other.
"""

from __future__ import annotations

from app.adapters.cache import LastKnownGoodCache
from app.adapters.circuit_breaker import CircuitBreaker, get_breaker
from app.adapters.docker_client import DockerComponentAdapter
from app.adapters.etcd_client import MetadataStoreAdapter
from app.adapters.metrics_client import MetricsAdapter
from app.adapters.milvus_client import MilvusAdapter
from app.adapters.minio_client import ObjectStoreAdapter
from app.config import Settings, get_settings

_milvus: dict[str, MilvusAdapter] = {}
_metrics: dict[str, MetricsAdapter] = {}
_object_store: dict[str, ObjectStoreAdapter] = {}
_metadata_store: dict[str, MetadataStoreAdapter] = {}
_docker: DockerComponentAdapter | None = None
_live_cache: LastKnownGoodCache | None = None
_cluster_cache: LastKnownGoodCache | None = None

# How long cluster metadata stays usable after PostgreSQL goes away. Far longer
# than the live-data window because it answers a different question: live data
# ages into uselessness in seconds, whereas an endpoint URI is effectively
# static. This is what lets /clusters/{id}/health keep probing Milvus through a
# Postgres outage instead of 503-ing -- the moment you most need to know whether
# Milvus is up is exactly when the control plane's own store has fallen over.
CLUSTER_CACHE_TTL_S = 300.0
CLUSTER_CACHE_STALE_AFTER_S = 86_400.0


def get_live_cache(settings: Settings | None = None) -> LastKnownGoodCache:
    """Last-known-good cache for live data (metrics, collections, components).

    The stale window comes from `Settings.stale_after_s`, which is held at two
    snapshot intervals so snapshot-derived values cannot oscillate in and out
    of staleness every cycle and make the dashboard flicker.
    """
    global _live_cache
    if _live_cache is None:
        cfg = settings or get_settings()
        _live_cache = LastKnownGoodCache(
            ttl_s=float(cfg.cp_cache_ttl_s), stale_after_s=float(cfg.stale_after_s)
        )
    return _live_cache


def get_cluster_cache() -> LastKnownGoodCache:
    """Cache of cluster metadata, used only when PostgreSQL is unreachable."""
    global _cluster_cache
    if _cluster_cache is None:
        _cluster_cache = LastKnownGoodCache(
            ttl_s=CLUSTER_CACHE_TTL_S, stale_after_s=CLUSTER_CACHE_STALE_AFTER_S
        )
    return _cluster_cache


def get_milvus_breaker(uri: str, settings: Settings | None = None) -> CircuitBreaker:
    """Breaker for one Milvus endpoint.

    Named by URI, not "milvus": a per-dependency-instance breaker is the only
    correct granularity once more than one cluster is registered.
    """
    cfg = settings or get_settings()
    return get_breaker(
        f"milvus:{uri}",
        fail_max=cfg.cp_breaker_fail_max,
        reset_timeout_s=float(cfg.cp_breaker_reset_s),
    )


def get_milvus_adapter(uri: str | None = None, settings: Settings | None = None) -> MilvusAdapter:
    cfg = settings or get_settings()
    endpoint = uri or cfg.milvus_uri
    if endpoint not in _milvus:
        _milvus[endpoint] = MilvusAdapter(
            uri=endpoint,
            settings=cfg,
            breaker=get_milvus_breaker(endpoint, cfg),
        )
    return _milvus[endpoint]


def get_metrics_adapter(uri: str | None = None, settings: Settings | None = None) -> MetricsAdapter:
    cfg = settings or get_settings()
    endpoint = uri or cfg.milvus_metrics_uri
    if endpoint not in _metrics:
        _metrics[endpoint] = MetricsAdapter(metrics_uri=endpoint, settings=cfg)
    return _metrics[endpoint]


def get_object_store_adapter(
    endpoint: str | None = None, settings: Settings | None = None
) -> ObjectStoreAdapter:
    cfg = settings or get_settings()
    key = endpoint or cfg.minio_endpoint
    if key not in _object_store:
        _object_store[key] = ObjectStoreAdapter(endpoint=key, settings=cfg)
    return _object_store[key]


def get_metadata_store_adapter(
    endpoint: str | None = None, settings: Settings | None = None
) -> MetadataStoreAdapter:
    cfg = settings or get_settings()
    key = endpoint or cfg.etcd_endpoint
    if key not in _metadata_store:
        _metadata_store[key] = MetadataStoreAdapter(endpoint=key, settings=cfg)
    return _metadata_store[key]


def get_docker_adapter(settings: Settings | None = None) -> DockerComponentAdapter:
    """Single Docker adapter: there is one socket regardless of cluster count."""
    global _docker
    if _docker is None:
        _docker = DockerComponentAdapter(settings=settings or get_settings())
    return _docker


def reset_caches() -> None:
    """Drop cached live data and cluster metadata. For tests."""
    if _live_cache is not None:
        _live_cache.clear()
    if _cluster_cache is not None:
        _cluster_cache.clear()


async def close_all() -> None:
    """Release every cached adapter. Called from the FastAPI lifespan shutdown."""
    global _docker
    for adapter in list(_milvus.values()):
        await adapter.close()
    _milvus.clear()
    for metrics in list(_metrics.values()):
        await metrics.close()
    _metrics.clear()
    for store in list(_object_store.values()):
        await store.close()
    _object_store.clear()
    for meta in list(_metadata_store.values()):
        await meta.close()
    _metadata_store.clear()
    if _docker is not None:
        await _docker.close()
    _docker = None
