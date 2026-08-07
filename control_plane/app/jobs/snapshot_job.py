"""Periodic component and collection snapshot.

Both tables are append-only, so a row lands every tick regardless of change.
`events`, by contrast, gets a `component_state_change` row only when a
component's state actually differs from its previous observation -- the same
transition contract the health job follows, for the same reason.

The previous state comes from `component_status.latest_per_component`, not from
memory, so a restarted API does not replay every component as a fresh change.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from app.adapters.registry import get_docker_adapter, get_milvus_adapter
from app.api.errors import DependencyUnavailableError
from app.config import Settings, get_settings
from app.jobs.base import guarded, job_session
from app.logging_conf import get_logger
from app.repositories import (
    ClusterRepository,
    CollectionSnapshotRepository,
    ComponentStatusRepository,
    EventRepository,
    EventType,
    Severity,
)
from app.services.observability_service import collect_collection_stats

log = get_logger("snapshot_job")

JOB_ID = "snapshot_job"

# States that read as trouble when a component moves into them.
_BAD_STATES = frozenset({"exited", "missing", "restarting", "paused", "dead"})


@guarded(JOB_ID)
async def run_snapshot_job(settings: Settings | None = None) -> int:
    """Snapshot every registered cluster. Returns how many were processed."""
    cfg = settings or get_settings()

    async with job_session() as session:
        clusters = await ClusterRepository(session).list(limit=100)
        targets = [(c.id, c.name, c.endpoint_uri, c.compose_project) for c in clusters]

    if not targets:
        log.debug("snapshot_job_no_clusters")
        return 0

    for cluster_id, name, endpoint_uri, compose_project in targets:
        await _snapshot_components(
            cluster_id=cluster_id, name=name, compose_project=compose_project, settings=cfg
        )
        await _snapshot_collections(cluster_id=cluster_id, endpoint_uri=endpoint_uri, settings=cfg)
    return len(targets)


async def _snapshot_components(
    *, cluster_id: Any, name: str, compose_project: str | None, settings: Settings
) -> None:
    docker = get_docker_adapter(settings)
    try:
        observed = await docker.list_components(compose_project=compose_project)
    except DependencyUnavailableError as exc:
        # Degrading, not failing: the health job already reports the socket
        # loss (rule 4). Writing nothing is right -- inventing rows for
        # components we could not see would be worse than a gap.
        log.warning("snapshot_components_unavailable", cluster=name, code=exc.code)
        return

    observed_at = dt.datetime.now(dt.UTC)
    async with job_session() as session:
        components = ComponentStatusRepository(session)
        events = EventRepository(session)
        stored = await components.latest_per_component(cluster_id)
        previous = {row.component_name: row.state for row in stored}

        for component in observed:
            await components.insert_component_status(
                cluster_id=cluster_id,
                component_name=component.component_name,
                kind=component.kind,
                runtime_id=component.runtime_id,
                image=component.image,
                state=component.state,
                health=component.health,
                restart_count=component.restart_count,
                started_at=component.started_at,
                observed_at=observed_at,
                raw=component.as_dict(),
            )

            before = previous.get(component.component_name)
            # A component observed for the first time is not a transition. It
            # has no "before", and emitting one would put a change event on
            # every row the very first time the job ever ran.
            if before is None or before == component.state:
                continue

            await events.insert_event(
                cluster_id=cluster_id,
                event_type=EventType.COMPONENT_STATE_CHANGE,
                severity=_severity_for(component.state),
                message=(f"component {component.component_name!r} {before} -> {component.state}"),
                payload={
                    "component": component.component_name,
                    "from": before,
                    "to": component.state,
                    "health": component.health,
                    "exit_code": component.exit_code,
                    "restart_count": component.restart_count,
                    "image": component.image,
                },
                created_at=observed_at,
            )
            log.info(
                "component_state_change",
                cluster=name,
                component=component.component_name,
                **{"from": before},
                to=component.state,
                exit_code=component.exit_code,
            )


async def _snapshot_collections(*, cluster_id: Any, endpoint_uri: str, settings: Settings) -> None:
    milvus = get_milvus_adapter(endpoint_uri, settings)
    try:
        # Honours the breaker here, unlike the health job: this job produces
        # nice-to-have inventory, not the liveness signal that drives recovery,
        # so there is no reason to hammer a Milvus already known to be down.
        stats = await collect_collection_stats(milvus)
    except DependencyUnavailableError as exc:
        log.warning("snapshot_collections_unavailable", code=exc.code, error=exc.message[:200])
        return

    if not stats:
        log.debug("snapshot_no_collections", cluster_id=str(cluster_id))
        return

    observed_at = dt.datetime.now(dt.UTC)
    async with job_session() as session:
        repo = CollectionSnapshotRepository(session)
        for stat in stats:
            await repo.insert_collection_snapshot(
                cluster_id=cluster_id,
                collection_name=stat.collection_name,
                row_count=stat.row_count,
                num_partitions=stat.num_partitions,
                dimension=stat.dimension,
                index_type=stat.index_type,
                metric_type=stat.metric_type,
                is_loaded=stat.is_loaded,
                observed_at=observed_at,
                raw=stat.as_dict(),
            )
    log.debug("snapshot_collections_written", count=len(stats))


def _severity_for(state: str) -> Severity:
    if state in _BAD_STATES:
        return Severity.ERROR
    if state == "running":
        return Severity.INFO
    return Severity.WARNING
