"""Cluster metadata schemas."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.db.base import DeploymentStatus, DeploymentType, HealthStatus

_CLUSTER_EXAMPLE: dict[str, Any] = {
    "id": "202b9ea6-a927-44ec-98d4-46f7ceff4a08",
    "name": "local-standalone",
    "deployment_type": "docker_standalone",
    "deployment_status": "running",
    "milvus_version": "v2.6.20",
    "endpoint_uri": "http://milvus-standalone:19530",
    "metrics_uri": "http://milvus-standalone:9091",
    "object_store_endpoint": "milvus-minio:9000",
    "compose_project": "milvus-cp",
    "namespace": None,
    "last_health_status": "healthy",
    "last_health_check_at": "2026-08-07T08:22:55Z",
    "labels": {"source": "bootstrap"},
    "created_at": "2026-08-07T08:17:06Z",
    "updated_at": "2026-08-07T08:22:55Z",
}


class ClusterRead(BaseModel):
    """A registered cluster as stored."""

    model_config = ConfigDict(
        from_attributes=True, json_schema_extra={"examples": [_CLUSTER_EXAMPLE]}
    )

    id: uuid.UUID
    name: str
    deployment_type: DeploymentType
    deployment_status: DeploymentStatus
    milvus_version: str | None = None
    endpoint_uri: str
    metrics_uri: str | None = None
    object_store_endpoint: str | None = None
    compose_project: str | None = None
    namespace: str | None = None
    last_health_status: HealthStatus
    last_health_check_at: dt.datetime | None = None
    labels: dict[str, Any] = Field(default_factory=dict)
    created_at: dt.datetime
    updated_at: dt.datetime


def _validate_uri(value: str | None) -> str | None:
    """Reject a URI that would only fail later as a fake outage.

    A typo here surfaces as MILVUS_UNREACHABLE on every subsequent probe, which
    during a reliability drill is indistinguishable from a real one.
    """
    if value is None:
        return None
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"scheme must be http or https, got {value!r}")
    if not parsed.hostname:
        raise ValueError(f"missing host in {value!r}")
    return value.rstrip("/")


class ClusterCreate(BaseModel):
    """Registration payload."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "name": "local-standalone",
                    "endpoint_uri": "http://milvus-standalone:19530",
                    "deployment_type": "docker_standalone",
                    "metrics_uri": "http://milvus-standalone:9091",
                    "object_store_endpoint": "milvus-minio:9000",
                    "compose_project": "milvus-cp",
                    "labels": {"env": "local"},
                }
            ]
        }
    )

    name: str = Field(min_length=1, max_length=128)
    endpoint_uri: str = Field(description="Milvus gRPC endpoint, e.g. http://host:19530")
    deployment_type: DeploymentType = DeploymentType.DOCKER_STANDALONE
    metrics_uri: str | None = None
    object_store_endpoint: str | None = None
    compose_project: str | None = None
    namespace: str | None = None
    labels: dict[str, Any] = Field(default_factory=dict)

    @field_validator("endpoint_uri", "metrics_uri")
    @classmethod
    def _check_uri(cls, v: str | None) -> str | None:
        return _validate_uri(v)


class ClusterUpdate(BaseModel):
    """Mutable fields. Every field optional; omitted fields are left alone.

    `name`, `id` and `deployment_type` are deliberately absent: the first two
    identify the cluster in the event trail, and changing the third would
    invalidate which runtime adapter is in use.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"metrics_uri": "http://milvus-standalone:9091", "labels": {"tier": "dev"}}
            ]
        }
    )

    endpoint_uri: str | None = None
    metrics_uri: str | None = None
    object_store_endpoint: str | None = None
    compose_project: str | None = None
    namespace: str | None = None
    labels: dict[str, Any] | None = None

    @field_validator("endpoint_uri", "metrics_uri")
    @classmethod
    def _check_uri(cls, v: str | None) -> str | None:
        return _validate_uri(v)
