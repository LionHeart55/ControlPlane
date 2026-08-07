"""Application settings, mirroring the keys in .env.

Single source of truth for configuration. Everything is read from the
environment (or a .env file) via pydantic-settings, validated once at startup,
and reached through the cached `get_settings()` accessor.

Host-vs-container note: the defaults here are the container-network names
(cp-postgres, milvus-standalone), which are correct when the API runs inside
Compose. To run the API or Alembic from your host, override the endpoints:

    POSTGRES_HOST=localhost POSTGRES_PORT=5433 alembic upgrade head
    MILVUS_URI=http://localhost:19530 uvicorn app.main:app
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repository root: .../control_plane/app/config.py -> up three levels.
REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Typed view of the environment."""

    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        # Every key in .env is mapped below. extra="ignore" is a forward-
        # compatibility guard only: an operator adding a key to .env should not
        # crash the API on startup.
        extra="ignore",
    )

    # --- compose / deployment identity -----------------------------------
    compose_project_name: str = "milvus-cp"
    docker_volume_directory: str = "./volumes"

    # --- pinned image versions -------------------------------------------
    # Mirrored from .env so the control plane knows what it is *supposed* to be
    # running, not only what it observes. The components adapter compares the
    # expected image against the running container's image, which is how a
    # cluster silently started on the wrong tag becomes visible.
    milvus_version: str = "v2.6.20"
    etcd_version: str = "v3.5.25"
    minio_version: str = "RELEASE.2024-05-28T17-19-04Z"
    postgres_version: str = "16-alpine"

    # --- object store (MinIO) --------------------------------------------
    minio_root_user: str = "minioadmin"
    minio_root_password: str = "minioadmin"
    minio_bucket: str = "milvus-bucket"
    # Reachable from inside cp-net; the object-store probe (WP-06b) uses this.
    minio_endpoint: str = "milvus-minio:9000"

    # --- control-plane database ------------------------------------------
    postgres_user: str = "controlplane"
    postgres_password: str = "controlplane"
    postgres_db: str = "controlplane"
    postgres_host: str = "cp-postgres"
    # In-network port used to build the DSN. Not the host publish port.
    postgres_port: int = Field(default=5432, ge=1, le=65535)
    # Host publish port only, consumed by Compose and the tooling. Kept here so
    # Settings mirrors .env completely and nothing silently falls through.
    postgres_host_port: int = Field(default=5432, ge=1, le=65535)

    # --- Milvus -----------------------------------------------------------
    milvus_uri: str = "http://milvus-standalone:19530"
    milvus_metrics_uri: str = "http://milvus-standalone:9091"
    # Both must be > 0. A zero or negative timeout means "no deadline" in most
    # clients, which is precisely the hung-dependency failure mode the control
    # plane exists to avoid.
    milvus_connect_timeout_s: float = Field(default=3.0, gt=0)
    milvus_rpc_timeout_s: float = Field(default=5.0, gt=0)

    # --- runtime ----------------------------------------------------------
    docker_socket: str = "/var/run/docker.sock"
    # Components that MUST exist. Anything on this list with no container is
    # reported as state="missing" rather than omitted -- the difference between
    # a dashboard that shows an outage and one that shows nothing. Override with
    # CP_EXPECTED_COMPONENTS as a comma-separated list; add cp-api and
    # cp-dashboard once the app tier is deployed (WP-13).
    cp_expected_components: list[str] = Field(
        default_factory=lambda: [
            "milvus-etcd",
            "milvus-minio",
            "milvus-standalone",
            "cp-postgres",
        ]
    )
    cp_api_port: int = Field(default=8000, ge=1, le=65535)
    cp_log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    cp_health_interval_s: int = Field(default=15, gt=0)
    cp_snapshot_interval_s: int = Field(default=60, gt=0)
    cp_cache_ttl_s: int = Field(default=5, gt=0)
    cp_breaker_fail_max: int = Field(default=3, gt=0)
    cp_breaker_reset_s: int = Field(default=30, gt=0)
    cp_retention_days: int = Field(default=7, gt=0)
    dashboard_port: int = Field(default=8080, ge=1, le=65535)

    @field_validator("cp_log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, v: object) -> object:
        return v.upper() if isinstance(v, str) else v

    @field_validator("cp_expected_components", mode="before")
    @classmethod
    def _split_components(cls, v: object) -> object:
        """Accept a comma-separated string as well as a JSON list.

        pydantic-settings parses complex types as JSON, so a plain
        CP_EXPECTED_COMPONENTS=a,b in .env would otherwise fail to load.
        """
        if isinstance(v, str) and not v.strip().startswith("["):
            return [item.strip() for item in v.split(",") if item.strip()]
        return v

    @field_validator("milvus_uri", "milvus_metrics_uri")
    @classmethod
    def _validate_url(cls, v: str) -> str:
        """Reject anything that is not a usable http(s) URL with a host.

        Caught at startup rather than on the first probe: a typo here would
        otherwise surface as MILVUS_UNREACHABLE and be misread as a real
        outage during a reliability drill.
        """
        parsed = urlparse(v)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(f"scheme must be http or https, got {v!r}")
        if not parsed.hostname:
            raise ValueError(f"missing host in {v!r}")
        if parsed.port is not None and not (1 <= parsed.port <= 65535):
            raise ValueError(f"port out of range in {v!r}")
        return v.rstrip("/")

    # --- derived ----------------------------------------------------------
    @property
    def database_url(self) -> str:
        """Async SQLAlchemy DSN (asyncpg driver)."""
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def stale_after_s(self) -> int:
        """How long cached data may still be served, flagged `stale: true`.

        The base window is cp_cache_ttl_s * 12, but with the default values
        that is 5 * 12 = 60s -- exactly cp_snapshot_interval_s. Snapshot-derived
        values would then expire at the very moment the next snapshot is due,
        so any jitter would flip them in and out of the stale window every
        cycle and the dashboard would flicker between fresh and dimmed.

        Holding the floor at two snapshot intervals gives a snapshot a full
        extra cycle to land before its data is called stale.
        """
        return max(self.cp_cache_ttl_s * 12, self.cp_snapshot_interval_s * 2)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton."""
    return Settings()
