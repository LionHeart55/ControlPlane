"""Docker adapter behaviour against a fake SDK client. No infrastructure."""

from __future__ import annotations

import datetime as dt
from typing import Any, ClassVar
from unittest.mock import patch

import pytest
from docker.errors import DockerException, NotFound

from app.adapters.docker_client import (
    COMPONENT_LABEL,
    MAX_LOG_LINES,
    ComponentStatus,
    DockerComponentAdapter,
    _parse_docker_time,
    _parse_since,
)
from app.api.errors import DependencyUnavailableError, NotFoundError, ValidationError
from app.config import Settings

EXPECTED = ["milvus-etcd", "milvus-minio", "milvus-standalone", "cp-postgres"]


def container(
    name: str,
    state: str = "running",
    health: str | None = "healthy",
    exit_code: int = 0,
    restart_count: int = 0,
    image: str = "img:1",
) -> Any:
    class C:
        attrs: ClassVar[dict[str, Any]] = {
            "Id": f"{name}-deadbeefcafe",
            "Name": f"/{name}",
            "RestartCount": restart_count,
            "Config": {"Image": image, "Labels": {COMPONENT_LABEL: name}},
            "State": {
                "Status": state,
                "ExitCode": exit_code,
                "StartedAt": "2026-08-07T05:27:19.116123456Z",
                "FinishedAt": "0001-01-01T00:00:00Z",
                "OOMKilled": False,
                **({"Health": {"Status": health}} if health else {}),
            },
        }

    return C()


class FakeContainers:
    def __init__(self, items: list[Any]) -> None:
        self._items = items

    def list(self, all: bool = False, filters: dict | None = None) -> list[Any]:
        assert all is True, "stopped containers must be included or an outage looks like absence"
        return self._items

    def get(self, name: str) -> Any:
        for c in self._items:
            if c.attrs["Config"]["Labels"].get(COMPONENT_LABEL) == name:
                return _LoggingContainer(name)
        raise NotFound(f"no such container: {name}")


class _LoggingContainer:
    def __init__(self, name: str) -> None:
        self.name = name

    def logs(self, stdout: bool = True, stderr: bool = False, **kw: Any) -> bytes:
        if stdout:
            return (
                b"2026-08-07T06:00:02.000000000Z out second\n"
                b"2026-08-07T06:00:00.000000000Z out first\n"
            )
        return b"2026-08-07T06:00:01.000000000Z err middle\n\xff\xfe bad bytes\n"


class FakeDockerClient:
    instances: ClassVar[list[FakeDockerClient]] = []
    items: ClassVar[list[Any]] = []
    fail: ClassVar[BaseException | None] = None

    def __init__(self, base_url: str = "", timeout: int = 0, **_: Any) -> None:
        if FakeDockerClient.fail is not None:
            raise FakeDockerClient.fail
        self.base_url = base_url
        self.timeout = timeout
        self.containers = FakeContainers(FakeDockerClient.items)
        FakeDockerClient.instances.append(self)

    def ping(self) -> bool:
        return True

    def close(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _reset() -> Any:
    FakeDockerClient.instances.clear()
    FakeDockerClient.items = [container(n) for n in EXPECTED]
    FakeDockerClient.fail = None
    with patch("app.adapters.docker_client.docker.DockerClient", FakeDockerClient):
        yield


def make_adapter(**kw: Any) -> DockerComponentAdapter:
    settings = Settings(_env_file=None, cp_expected_components=EXPECTED, **kw)
    return DockerComponentAdapter(settings=settings)


# --- reconciliation ------------------------------------------------------
async def test_all_expected_components_running() -> None:
    rows = await make_adapter().list_components()
    assert len(rows) == 4
    assert {r.component_name for r in rows} == set(EXPECTED)
    assert all(r.state == "running" and r.is_healthy for r in rows)


async def test_stopped_component_reports_exited_with_exit_code() -> None:
    FakeDockerClient.items = [
        container("milvus-standalone", state="exited", health="unhealthy", exit_code=137),
        *[container(n) for n in EXPECTED if n != "milvus-standalone"],
    ]
    rows = await make_adapter().list_components()
    m = next(r for r in rows if r.component_name == "milvus-standalone")
    assert m.state == "exited"
    assert m.exit_code == 137
    assert len(rows) == 4, "a stopped component must remain in the list"


async def test_vanished_component_reports_missing_not_omitted() -> None:
    """The whole point of reconciliation: absence must be visible."""
    FakeDockerClient.items = [container(n) for n in EXPECTED if n != "milvus-standalone"]
    rows = await make_adapter().list_components()
    assert len(rows) == 4, "expected component must still appear"
    m = next(r for r in rows if r.component_name == "milvus-standalone")
    assert m.state == "missing"
    assert m.runtime_id is None and m.image is None and m.health is None


async def test_running_container_has_no_exit_code() -> None:
    """Docker reports ExitCode 0 while running; that is not an exit."""
    rows = await make_adapter().list_components()
    assert all(r.exit_code is None for r in rows)


async def test_unexpected_labelled_container_is_still_reported() -> None:
    FakeDockerClient.items = [*[container(n) for n in EXPECTED], container("cp-api")]
    rows = await make_adapter().list_components()
    assert len(rows) == 5
    assert "cp-api" in {r.component_name for r in rows}


async def test_unlabelled_container_is_ignored() -> None:
    stray = container("milvus-etcd")
    stray.attrs["Config"]["Labels"] = {}
    FakeDockerClient.items = [stray]
    rows = await make_adapter().list_components()
    assert all(r.state == "missing" for r in rows)


async def test_component_without_healthcheck_is_healthy_when_running() -> None:
    FakeDockerClient.items = [container(n, health=None) for n in EXPECTED]
    rows = await make_adapter().list_components()
    assert all(r.health is None and r.is_healthy for r in rows)


# --- logs ----------------------------------------------------------------
async def test_logs_are_tagged_and_chronological() -> None:
    lines = await make_adapter().tail_logs("milvus-standalone", lines=10)
    assert [line.message for line in lines[:2]] == ["out first", "err middle"]
    assert lines[-1].message == "out second"
    assert [line.stream for line in lines[:2]] == ["stdout", "stderr"]


async def test_undecodable_bytes_do_not_break_the_view() -> None:
    lines = await make_adapter().tail_logs("milvus-standalone", lines=10)
    assert any("bad bytes" in line.message for line in lines)


async def test_continuation_line_stays_with_its_parent() -> None:
    """An unstamped line is a continuation, not the oldest line in the file."""
    lines = await make_adapter().tail_logs("milvus-standalone", lines=10)
    messages = [line.message for line in lines]
    bad = next(i for i, m in enumerate(messages) if "bad bytes" in m)
    assert messages[bad - 1] == "err middle", "must follow the line it continues"
    assert lines[bad].timestamp is None, "and must not claim a timestamp it never had"


async def test_line_count_is_capped_server_side() -> None:
    adapter = make_adapter()
    captured: dict[str, Any] = {}

    class Counting(_LoggingContainer):
        def logs(self, stdout: bool = True, stderr: bool = False, **kw: Any) -> bytes:
            captured["tail"] = kw.get("tail")
            return b""

    with patch.object(FakeContainers, "get", lambda self, n: Counting(n)):
        await adapter.tail_logs("milvus-standalone", lines=10_000_000)
    assert captured["tail"] == MAX_LOG_LINES


# --- allowlist -----------------------------------------------------------
@pytest.mark.parametrize(
    "bad",
    ["../../etc/passwd", "cp-postgres; rm -rf /", "milvus-standalone\n", "unknown", ""],
)
async def test_allowlist_rejects_arbitrary_names(bad: str) -> None:
    """Enforced in the ADAPTER: user input must never reach a container lookup."""
    with pytest.raises(ValidationError):
        await make_adapter().tail_logs(bad)


async def test_allowlisted_but_absent_container_is_404() -> None:
    FakeDockerClient.items = []
    with pytest.raises(NotFoundError):
        await make_adapter().tail_logs("cp-api")


# --- degradation ---------------------------------------------------------
async def test_socket_failure_raises_docker_unavailable() -> None:
    FakeDockerClient.fail = DockerException("permission denied while trying to connect")
    with pytest.raises(DependencyUnavailableError) as ei:
        await make_adapter().list_components()
    assert ei.value.code == "DOCKER_UNAVAILABLE"
    assert ei.value.dependency == "docker"


async def test_ping_never_raises() -> None:
    FakeDockerClient.fail = DockerException("socket missing")
    assert await make_adapter().ping() is False


async def test_client_is_dropped_after_failure_so_it_can_reconnect() -> None:
    adapter = make_adapter()
    await adapter.list_components()
    assert len(FakeDockerClient.instances) == 1

    with (
        patch.object(FakeContainers, "list", side_effect=DockerException("daemon went away")),
        pytest.raises(DependencyUnavailableError),
    ):
        await adapter.list_components()

    await adapter.list_components()
    assert len(FakeDockerClient.instances) == 2, "a stale client must not be reused"


# --- helpers -------------------------------------------------------------
def test_parse_docker_nanosecond_timestamps() -> None:
    """fromisoformat accepts microseconds; Docker emits nanoseconds."""
    parsed = _parse_docker_time("2026-08-07T05:27:19.116123456Z")
    assert parsed is not None
    assert parsed.microsecond == 116123
    assert parsed.tzinfo is not None


def test_never_started_is_none() -> None:
    assert _parse_docker_time("0001-01-01T00:00:00Z") is None
    assert _parse_docker_time(None) is None
    assert _parse_docker_time("garbage") is None


@pytest.mark.parametrize(
    ("value", "seconds"), [("30s", 30), ("10m", 600), ("2h", 7200), ("1d", 86400)]
)
def test_parse_relative_since(value: str, seconds: int) -> None:
    parsed = _parse_since(value)
    assert isinstance(parsed, dt.datetime)
    delta = (dt.datetime.now(dt.UTC) - parsed).total_seconds()
    assert abs(delta - seconds) < 5


def test_parse_since_rejects_nonsense() -> None:
    with pytest.raises(ValidationError):
        _parse_since("yesterday")


def test_missing_component_status_defaults() -> None:
    s = ComponentStatus(component_name="milvus-standalone")
    assert s.state == "missing"
    assert s.is_running is False and s.is_healthy is False
