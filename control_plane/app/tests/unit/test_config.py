"""Settings parsing.

Focused on the one field that has actually broken a deployment:
`CP_EXPECTED_COMPONENTS`. pydantic-settings JSON-decodes complex types in the
source layer, *before* field validators run, so a comma-separated value raised
SettingsError at import time and the API could not start at all. It was invisible
until the compose app tier first set the variable, because nothing else ever did.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from app.config import Settings


def settings(**env: Any) -> Settings:
    return Settings(_env_file=None, **env)


# --- CP_EXPECTED_COMPONENTS ----------------------------------------------
def test_comma_separated_components_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    """The form people actually write in .env and in a compose environment block."""
    monkeypatch.setenv("CP_EXPECTED_COMPONENTS", "milvus-etcd,cp-api,cp-dashboard")
    assert Settings(_env_file=None).cp_expected_components == [
        "milvus-etcd",
        "cp-api",
        "cp-dashboard",
    ]


def test_json_list_components_still_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CP_EXPECTED_COMPONENTS", '["a", "b"]')
    assert Settings(_env_file=None).cp_expected_components == ["a", "b"]


def test_whitespace_and_empty_entries_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CP_EXPECTED_COMPONENTS", "  a , b ,, c  ")
    assert Settings(_env_file=None).cp_expected_components == ["a", "b", "c"]


def test_malformed_json_is_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CP_EXPECTED_COMPONENTS", '["unterminated"')
    with pytest.raises(ValidationError, match="not valid JSON"):
        Settings(_env_file=None)


def test_default_is_the_infra_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CP_EXPECTED_COMPONENTS", raising=False)
    assert Settings(_env_file=None).cp_expected_components == [
        "milvus-etcd",
        "milvus-minio",
        "milvus-standalone",
        "cp-postgres",
    ]


# --- other env-driven fields ---------------------------------------------
def test_database_url_uses_the_in_network_port() -> None:
    """POSTGRES_HOST_PORT is a host publish port and must never reach the DSN.

    Conflating the two means a host-side port clash silently rewrites the
    API's connection string.
    """
    cfg = settings(postgres_host="cp-postgres", postgres_port=5432, postgres_host_port=5433)
    assert "cp-postgres:5432" in cfg.database_url
    assert "5433" not in cfg.database_url


def test_stale_window_never_equals_the_snapshot_interval() -> None:
    """Equal windows make snapshot values flip in and out of staleness."""
    cfg = settings(cp_cache_ttl_s=5, cp_snapshot_interval_s=60)
    assert cfg.stale_after_s > cfg.cp_snapshot_interval_s


@pytest.mark.parametrize("bad", ["ftp://milvus:19530", "milvus-standalone:19530", "http://"])
def test_unusable_milvus_uri_is_rejected_at_startup(bad: str) -> None:
    """Caught here rather than surfacing later as a fake MILVUS_UNREACHABLE."""
    with pytest.raises(ValidationError):
        settings(milvus_uri=bad)


@pytest.mark.parametrize("field", ["milvus_connect_timeout_s", "milvus_rpc_timeout_s"])
def test_timeouts_must_be_positive(field: str) -> None:
    """Zero means 'no deadline' in most clients — the exact hung-dependency
    failure mode the control plane exists to avoid."""
    with pytest.raises(ValidationError):
        settings(**{field: 0})


def test_log_level_is_case_insensitive() -> None:
    assert settings(cp_log_level="debug").cp_log_level == "DEBUG"
