"""Declarative base, shared metadata and the three native PostgreSQL enums.

The ENUM instances defined here are module-level singletons reused across
tables (health_status appears on both `clusters` and `health_checks`), and all
are declared with ``create_type=False``.

Why create_type=False: SQLAlchemy would otherwise try to emit CREATE TYPE
implicitly whenever a table referencing the enum is created, which produces
"type already exists" errors on the second table and makes creation order
depend on table order. The migration creates all three types explicitly up
front and drops them last instead.

Why values_callable: by default SQLAlchemy persists a Python enum's *member
name*, so `DeploymentType.DOCKER_STANDALONE` would be stored as
"DOCKER_STANDALONE". The schema calls for the lowercase values, so every enum
below is mapped by value.
"""

from __future__ import annotations

import enum

from sqlalchemy import MetaData
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase

# Deterministic constraint names, so a downgrade can drop precisely what an
# upgrade created rather than relying on whatever PostgreSQL happened to name
# things. %(column_0_N_name)s keeps composite-index names stable.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata_obj = MetaData(naming_convention=NAMING_CONVENTION)


class Base(DeclarativeBase):
    """Declarative base for every control-plane table."""

    metadata = metadata_obj


# --- Python-side enums ---------------------------------------------------
class DeploymentType(enum.StrEnum):
    """How a registered cluster is deployed."""

    DOCKER_STANDALONE = "docker_standalone"
    DOCKER_DISTRIBUTED = "docker_distributed"
    K8S_OPERATOR = "k8s_operator"


class DeploymentStatus(enum.StrEnum):
    """Lifecycle state of a cluster, maintained by the health job."""

    PENDING = "pending"
    PROVISIONING = "provisioning"
    RUNNING = "running"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    STOPPED = "stopped"
    DELETED = "deleted"


class HealthStatus(enum.StrEnum):
    """Outcome of a health evaluation.

    UNKNOWN is not a synonym for unavailable: it means a dependency needed to
    *evaluate* health was itself unreachable. Reporting UNKNOWN rather than
    guessing is what stops the control plane from ever showing a false healthy.
    """

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


def _pg_enum(python_enum: type[enum.Enum], name: str) -> postgresql.ENUM:
    return postgresql.ENUM(
        python_enum,
        name=name,
        create_type=False,
        values_callable=lambda e: [member.value for member in e],
    )


DEPLOYMENT_TYPE_ENUM = _pg_enum(DeploymentType, "deployment_type")
DEPLOYMENT_STATUS_ENUM = _pg_enum(DeploymentStatus, "deployment_status")
HEALTH_STATUS_ENUM = _pg_enum(HealthStatus, "health_status")

# Ordered exactly as the migration creates (and reverse-drops) them.
ALL_ENUM_NAMES = ("deployment_type", "deployment_status", "health_status")
