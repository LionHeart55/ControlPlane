"""ORM models for the five control-plane tables.

Conventions:
  * Every timestamp is TIMESTAMPTZ and stored in UTC.
  * `clusters` is keyed by UUID; the four append-only tables use BIGSERIAL.
  * Every index and foreign key is named explicitly so the hand-written
    migration can create and drop exactly the same objects.

Some columns hold a fixed vocabulary but are typed TEXT rather than a native
enum (component state, event type, severity). That follows the schema spec:
these lists are expected to grow, and widening a TEXT column costs nothing
whereas ALTER TYPE ... ADD VALUE cannot run inside a transaction. The allowed
values are documented on each column and enforced by the Pydantic schemas at
the API edge.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import (
    DEPLOYMENT_STATUS_ENUM,
    DEPLOYMENT_TYPE_ENUM,
    HEALTH_STATUS_ENUM,
    Base,
    DeploymentStatus,
    DeploymentType,
    HealthStatus,
)

TZ_TIMESTAMP = sa.TIMESTAMP(timezone=True)


class Cluster(Base):
    """A registered Milvus deployment.

    The schema is multi-cluster; this build registers one in practice.
    Deletion is soft (deployment_status -> 'deleted') so the audit trail in
    `events` keeps referring to something real.
    """

    __tablename__ = "clusters"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    )
    name: Mapped[str] = mapped_column(sa.Text, nullable=False, unique=True)
    deployment_type: Mapped[DeploymentType] = mapped_column(DEPLOYMENT_TYPE_ENUM, nullable=False)
    deployment_status: Mapped[DeploymentStatus] = mapped_column(
        DEPLOYMENT_STATUS_ENUM,
        nullable=False,
        server_default=sa.text("'pending'::deployment_status"),
    )

    milvus_version: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    endpoint_uri: Mapped[str] = mapped_column(sa.Text, nullable=False)
    metrics_uri: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    object_store_endpoint: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    compose_project: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    namespace: Mapped[str | None] = mapped_column(sa.Text, nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(
        TZ_TIMESTAMP, nullable=False, server_default=sa.text("now()")
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        TZ_TIMESTAMP,
        nullable=False,
        server_default=sa.text("now()"),
        onupdate=sa.func.now(),
    )

    # Explicitly required by the assignment: the last time a health check ran,
    # distinct from the last time the row changed.
    last_health_check_at: Mapped[dt.datetime | None] = mapped_column(TZ_TIMESTAMP, nullable=True)
    last_health_status: Mapped[HealthStatus] = mapped_column(
        HEALTH_STATUS_ENUM,
        nullable=False,
        server_default=sa.text("'unknown'::health_status"),
    )

    labels: Mapped[dict[str, Any]] = mapped_column(
        postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )

    __table_args__ = (
        sa.Index("ix_clusters_deployment_status", "deployment_status"),
        # Partial index. Soft-deleted clusters accumulate forever but are
        # excluded from every list query, so keeping them out of the index
        # keeps it small and lets the planner use an index-only scan for the
        # common "show me the live clusters" path.
        sa.Index(
            "ix_clusters_active",
            "deployment_status",
            postgresql_where=sa.text("deployment_status <> 'deleted'"),
        ),
    )


class HealthCheck(Base):
    """Append-only health time series, one row per probe."""

    __tablename__ = "health_checks"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=True)
    cluster_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey(
            "clusters.id",
            ondelete="CASCADE",
            name="fk_health_checks_cluster_id_clusters",
        ),
        nullable=False,
    )
    checked_at: Mapped[dt.datetime] = mapped_column(
        TZ_TIMESTAMP, nullable=False, server_default=sa.text("now()")
    )
    status: Mapped[HealthStatus] = mapped_column(HEALTH_STATUS_ENUM, nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)

    milvus_reachable: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    # Nullable on purpose: null means "not probed this cycle", which is a
    # different fact from false ("probed and unreachable"). Collapsing the two
    # would make an unprobed dependency look healthy.
    object_store_reachable: Mapped[bool | None] = mapped_column(sa.Boolean, nullable=True)
    metadata_store_reachable: Mapped[bool | None] = mapped_column(sa.Boolean, nullable=True)

    server_version: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    raw: Mapped[dict[str, Any]] = mapped_column(
        postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )

    __table_args__ = (
        # DESC matches the access pattern (latest first). With cluster_id as an
        # equality predicate PostgreSQL could scan an ASC index backwards just
        # as cheaply, so this is about intent, not a performance requirement.
        sa.Index(
            "ix_health_checks_cluster_id_checked_at",
            "cluster_id",
            sa.text("checked_at DESC"),
        ),
    )


class ComponentStatus(Base):
    """Append-only observations of container/pod state.

    Append-only rather than upsert: the retention job prunes by age, and a
    history of state changes is what lets the events table be written on
    transition only.
    """

    __tablename__ = "component_status"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=True)
    cluster_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey(
            "clusters.id",
            ondelete="CASCADE",
            name="fk_component_status_cluster_id_clusters",
        ),
        nullable=False,
    )
    # One of: milvus-standalone, milvus-etcd, milvus-minio, cp-postgres.
    component_name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    # container | pod
    kind: Mapped[str] = mapped_column(sa.Text, nullable=False)

    # Nullable: a component reconciled as state='missing' has no container to
    # identify. Reporting it with a null runtime_id is the whole point -- a
    # vanished component must appear as missing, never be silently omitted.
    runtime_id: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    image: Mapped[str | None] = mapped_column(sa.Text, nullable=True)

    # running | exited | paused | restarting | missing
    state: Mapped[str] = mapped_column(sa.Text, nullable=False)
    # Docker healthcheck verdict, absent when the image defines none.
    health: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    restart_count: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default=sa.text("0")
    )
    started_at: Mapped[dt.datetime | None] = mapped_column(TZ_TIMESTAMP, nullable=True)
    observed_at: Mapped[dt.datetime] = mapped_column(
        TZ_TIMESTAMP, nullable=False, server_default=sa.text("now()")
    )
    raw: Mapped[dict[str, Any]] = mapped_column(
        postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )

    __table_args__ = (
        sa.Index(
            "ix_component_status_cluster_id_component_name_observed_at",
            "cluster_id",
            "component_name",
            sa.text("observed_at DESC"),
        ),
    )


class CollectionSnapshot(Base):
    """Append-only per-collection statistics."""

    __tablename__ = "collection_snapshots"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=True)
    cluster_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey(
            "clusters.id",
            ondelete="CASCADE",
            name="fk_collection_snapshots_cluster_id_clusters",
        ),
        nullable=False,
    )
    collection_name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    row_count: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    num_partitions: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    dimension: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    index_type: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    metric_type: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    is_loaded: Mapped[bool | None] = mapped_column(sa.Boolean, nullable=True)
    observed_at: Mapped[dt.datetime] = mapped_column(
        TZ_TIMESTAMP, nullable=False, server_default=sa.text("now()")
    )
    raw: Mapped[dict[str, Any]] = mapped_column(
        postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )

    __table_args__ = (
        sa.Index(
            "ix_collection_snapshots_cluster_id_collection_name_observed_at",
            "cluster_id",
            "collection_name",
            sa.text("observed_at DESC"),
        ),
    )


class Event(Base):
    """Audit and incident trail.

    Rows are written ONLY on transition, never on every poll. A health job
    that appends here every 15 seconds is a bug: it would bury the handful of
    rows that actually describe an incident.
    """

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=True)
    # SET NULL, not CASCADE: if a cluster is ever hard-deleted the incident
    # history must survive it. That history is the point of this table.
    cluster_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey(
            "clusters.id",
            ondelete="SET NULL",
            name="fk_events_cluster_id_clusters",
        ),
        nullable=True,
    )
    # cluster_registered | health_transition | component_state_change |
    # dependency_failure | dependency_recovered | breaker_opened | breaker_closed
    event_type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    # info | warning | error
    severity: Mapped[str] = mapped_column(sa.Text, nullable=False)
    message: Mapped[str] = mapped_column(sa.Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        TZ_TIMESTAMP, nullable=False, server_default=sa.text("now()")
    )

    __table_args__ = (
        sa.Index("ix_events_created_at", sa.text("created_at DESC")),
        sa.Index(
            "ix_events_cluster_id_created_at",
            "cluster_id",
            sa.text("created_at DESC"),
        ),
    )
