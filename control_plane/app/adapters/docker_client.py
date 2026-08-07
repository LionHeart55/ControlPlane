"""Container runtime adapter: component state and log tails over the Docker socket.

Behind an interface on purpose
------------------------------
``ComponentRuntime`` is the abstraction the rest of the control plane depends
on; ``DockerComponentAdapter`` is one implementation. A Kubernetes deployment
would add a ``KubernetesAdapter`` (pod status + ``read_namespaced_pod_log``)
selected by ``clusters.deployment_type`` and change nothing else. That
indirection costs one class today and is expensive to retrofit later, so it is
here even though only the Docker path is built.

Reconciliation, not enumeration
-------------------------------
Listing containers reports what exists. A control plane has to report what is
*supposed* to exist: the configured expected-components list is reconciled
against what Docker returns, and anything absent comes back as
``state="missing"``. A component that vanished entirely is the most important
thing on the page, and a plain listing would silently omit it.

Degrading without Docker
------------------------
The socket may be absent (not mounted), refused (permission denied on Linux, or
Docker Desktop's "Allow the default Docker socket to be used" turned off on
macOS) or simply dead. All of those raise
``DependencyUnavailableError(code="DOCKER_UNAVAILABLE")``, which the API renders
as a degradation envelope. Losing container visibility must not take the
control plane down -- health, collections and metrics all still work.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import re
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any

import docker
from docker.errors import APIError, DockerException, NotFound

from app.api.errors import DependencyUnavailableError, NotFoundError, ValidationError
from app.config import Settings, get_settings
from app.logging_conf import get_logger

log = get_logger("docker")

COMPONENT_LABEL = "com.milvus-cp.component"
COMPOSE_PROJECT_LABEL = "com.docker.compose.project"

# Every call is bounded at 3s. Applied twice, for the same reason as the Milvus
# adapter: the SDK's own timeout is what actually unblocks the worker thread,
# while asyncio.wait_for only bounds the coroutine.
DOCKER_CALL_TIMEOUT_S = 3.0
_OUTER_MARGIN_S = 1.0

# Server-side cap. A caller asking for a million lines gets 1000.
MAX_LOG_LINES = 1000
DEFAULT_LOG_LINES = 200

# Names this adapter will ever look up. The allowlist lives HERE, not only in
# the router: a component name reaches a container lookup, and user input must
# never be interpolated into one. Configured expected components are unioned in
# so extending the stack does not require editing this constant.
KNOWN_COMPONENTS = frozenset(
    {
        "milvus-etcd",
        "milvus-minio",
        "milvus-standalone",
        "cp-postgres",
        "cp-api",
        "cp-dashboard",
    }
)

# Docker's zero value for "never started".
_NEVER = "0001-01-01T00:00:00Z"
_SINCE_RE = re.compile(r"^(\d+)([smhd])$")
_SINCE_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


@dataclass(frozen=True)
class ComponentStatus:
    """Observed (or absent) state of one component."""

    component_name: str
    kind: str = "container"
    runtime_id: str | None = None
    image: str | None = None
    state: str = "missing"
    health: str | None = None
    restart_count: int = 0
    started_at: dt.datetime | None = None
    exit_code: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_running(self) -> bool:
        return self.state == "running"

    @property
    def is_healthy(self) -> bool:
        """Running, and passing its healthcheck if it defines one."""
        return self.is_running and self.health in (None, "healthy")

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["started_at"] = self.started_at.isoformat() if self.started_at else None
        return d


@dataclass(frozen=True)
class LogLine:
    timestamp: dt.datetime | None
    stream: str  # stdout | stderr
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "stream": self.stream,
            "message": self.message,
        }


class ComponentRuntime(ABC):
    """What the services layer depends on. Docker and K8s both satisfy it."""

    @abstractmethod
    async def ping(self) -> bool:
        """Whether the runtime is reachable. Never raises."""

    @abstractmethod
    async def list_components(self, compose_project: str | None = None) -> list[ComponentStatus]:
        """Every expected component, present or missing."""

    @abstractmethod
    async def tail_logs(
        self,
        component: str,
        lines: int = DEFAULT_LOG_LINES,
        since: str | dt.datetime | int | None = None,
    ) -> list[LogLine]:
        """Recent log lines for one allowlisted component."""


class DockerComponentAdapter(ComponentRuntime):
    """Docker SDK implementation over the mounted socket."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._socket = self._settings.docker_socket
        self._client: docker.DockerClient | None = None
        self._lock = asyncio.Lock()

    # --- allowlist --------------------------------------------------------
    @property
    def expected_components(self) -> list[str]:
        return list(self._settings.cp_expected_components)

    @property
    def allowed_components(self) -> frozenset[str]:
        return KNOWN_COMPONENTS | set(self._settings.cp_expected_components)

    def _check_allowed(self, component: str) -> str:
        if component not in self.allowed_components:
            raise ValidationError(
                f"unknown component '{component}'",
                detail={"allowed": sorted(self.allowed_components)},
            )
        return component

    # --- client -----------------------------------------------------------
    async def _get_client(self) -> docker.DockerClient:
        async with self._lock:
            if self._client is not None:
                return self._client
            try:
                self._client = await asyncio.wait_for(
                    asyncio.to_thread(self._build_client),
                    timeout=DOCKER_CALL_TIMEOUT_S + _OUTER_MARGIN_S,
                )
            except BaseException as exc:
                raise self._unavailable(exc, phase="connect") from exc
            return self._client

    def _build_client(self) -> docker.DockerClient:
        base_url = self._socket
        if not base_url.startswith(("unix://", "tcp://", "npipe://", "http://", "https://")):
            base_url = f"unix://{base_url}"
        # timeout= bounds the SDK's own HTTP calls to the daemon. Without it a
        # wedged daemon parks the worker thread indefinitely and asyncio.wait_for
        # cannot get it back.
        return docker.DockerClient(base_url=base_url, timeout=int(DOCKER_CALL_TIMEOUT_S))

    def _unavailable(self, exc: BaseException, *, phase: str) -> DependencyUnavailableError:
        return DependencyUnavailableError(
            f"docker socket unavailable ({phase}): {exc}",
            dependency="docker",
            code="DOCKER_UNAVAILABLE",
            detail={"socket": self._socket, "phase": phase},
        )

    async def _run(self, phase: str, fn: Any) -> Any:
        """Execute one SDK call under the 3s budget, classifying failures."""
        client = await self._get_client()
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(fn, client),
                timeout=DOCKER_CALL_TIMEOUT_S + _OUTER_MARGIN_S,
            )
        except NotFound:
            raise
        except (DockerException, APIError, OSError, TimeoutError) as exc:
            # The cached client may be bound to a dead socket; drop it so the
            # next call reconnects rather than failing identically forever.
            async with self._lock:
                self._client = None
            log.warning("docker_call_failed", phase=phase, error=str(exc)[:200])
            raise self._unavailable(exc, phase=phase) from exc

    async def close(self) -> None:
        async with self._lock:
            client, self._client = self._client, None
        if client is not None:
            try:
                await asyncio.to_thread(client.close)
            except Exception:
                log.debug("docker_client_close_failed")

    # --- surface ----------------------------------------------------------
    async def ping(self) -> bool:
        try:
            await self._run("ping", lambda c: c.ping())
        except DependencyUnavailableError:
            return False
        return True

    async def list_components(self, compose_project: str | None = None) -> list[ComponentStatus]:
        """Reconcile expected components against what Docker reports.

        Filters on the component LABEL rather than container names: names are a
        deployment detail, the label is the contract. all=True keeps stopped
        containers in the result, so a stopped Milvus reports `exited` with its
        exit code instead of disappearing.
        """
        project = compose_project or self._settings.compose_project_name
        filters: dict[str, Any] = {"label": [COMPONENT_LABEL]}
        if project:
            filters["label"] = [COMPONENT_LABEL, f"{COMPOSE_PROJECT_LABEL}={project}"]

        containers = await self._run(
            "list_components", lambda c: c.containers.list(all=True, filters=filters)
        )

        observed: dict[str, ComponentStatus] = {}
        for container in containers:
            status = _to_component_status(container)
            if status is not None:
                observed[status.component_name] = status

        results: list[ComponentStatus] = []
        for name in self.expected_components:
            found = observed.pop(name, None)
            # Expected but absent -> the default ComponentStatus, whose state is
            # "missing". Reported, never omitted.
            results.append(found if found is not None else ComponentStatus(component_name=name))
        # Anything labelled but not expected is still reported: an unexpected
        # container in the stack is information, not noise.
        results.extend(observed.values())
        return results

    async def tail_logs(
        self,
        component: str,
        lines: int = DEFAULT_LOG_LINES,
        since: str | dt.datetime | int | None = None,
    ) -> list[LogLine]:
        name = self._check_allowed(component)
        capped = max(1, min(int(lines or DEFAULT_LOG_LINES), MAX_LOG_LINES))
        since_arg = _parse_since(since)

        def _fetch(client: docker.DockerClient) -> Any:
            container = client.containers.get(name)
            # container.logs() has no demux option (that belongs to exec/attach),
            # and a combined stream cannot say which line came from where. Two
            # calls give each line a stream tag; timestamps=True then lets them
            # be merged back into true chronological order below.
            common = {"tail": capped, "since": since_arg, "timestamps": True}
            out = container.logs(stdout=True, stderr=False, **common)
            err = container.logs(stdout=False, stderr=True, **common)
            return out, err

        try:
            raw = await self._run("tail_logs", _fetch)
        except NotFound as exc:
            raise NotFoundError(
                f"component '{name}' has no container",
                detail={"component": name},
            ) from exc

        if isinstance(raw, tuple):
            stdout_bytes, stderr_bytes = raw
        else:  # tty-enabled containers return a single combined stream
            stdout_bytes, stderr_bytes = raw, None

        entries = _parse_log_bytes(stdout_bytes, "stdout") + _parse_log_bytes(
            stderr_bytes, "stderr"
        )
        # Fetching the two streams separately loses interleaving, but
        # timestamps=True stamps every line, so sorting restores chronological
        # order. The sort is stable and continuation lines carry the preceding
        # line's key, so a wrapped stack trace stays attached to its header
        # instead of being flung to the top of the view.
        entries.sort(key=lambda item: item[0])
        return [line for _, line in entries][-capped:]


# --- helpers -------------------------------------------------------------
def _to_component_status(container: Any) -> ComponentStatus | None:
    attrs: dict[str, Any] = getattr(container, "attrs", {}) or {}
    labels = attrs.get("Config", {}).get("Labels") or {}
    name = labels.get(COMPONENT_LABEL)
    if not name:
        return None

    state = attrs.get("State") or {}
    health = (state.get("Health") or {}).get("Status")
    exit_code = state.get("ExitCode")
    status = state.get("Status") or "unknown"

    return ComponentStatus(
        component_name=name,
        kind="container",
        runtime_id=(attrs.get("Id") or "")[:12] or None,
        # Read the image from attrs rather than container.image, which would
        # issue another API call per container.
        image=attrs.get("Config", {}).get("Image"),
        state=status,
        health=health,
        restart_count=int(attrs.get("RestartCount") or 0),
        started_at=_parse_docker_time(state.get("StartedAt")),
        # Only meaningful once stopped; a running container reports 0.
        exit_code=int(exit_code) if status != "running" and exit_code is not None else None,
        raw={
            "name": attrs.get("Name", "").lstrip("/"),
            "status": status,
            "health": health,
            "finished_at": state.get("FinishedAt"),
            "oom_killed": state.get("OOMKilled"),
            "error": state.get("Error") or None,
        },
    )


def _parse_docker_time(value: str | None) -> dt.datetime | None:
    if not value or value.startswith(_NEVER[:10]):
        return None
    text = value.replace("Z", "+00:00")
    # Docker stamps are RFC3339 with NANOsecond precision; fromisoformat accepts
    # at most microseconds, so truncate the fractional part to 6 digits and
    # keep whatever timezone offset follows it.
    if "." in text:
        head, _, tail = text.partition(".")
        match = re.match(r"(\d+)(.*)$", tail)
        if match:
            text = f"{head}.{match.group(1)[:6]}{match.group(2)}"
    try:
        return dt.datetime.fromisoformat(text)
    except ValueError:
        return None


def _parse_since(since: str | dt.datetime | int | None) -> Any:
    """Accept a relative duration ('10m'), a datetime, or a unix timestamp."""
    if since is None or isinstance(since, dt.datetime | int):
        return since
    text = str(since).strip()
    if not text:
        return None
    match = _SINCE_RE.match(text)
    if match:
        seconds = int(match.group(1)) * _SINCE_UNITS[match.group(2)]
        return dt.datetime.now(dt.UTC) - dt.timedelta(seconds=seconds)
    if text.isdigit():
        return int(text)
    raise ValidationError(
        f"invalid 'since' value: {since!r}",
        detail={"expected": "a duration like 10m/2h/1d, a unix timestamp, or an ISO datetime"},
    )


def _parse_log_bytes(payload: bytes | None, stream: str) -> list[tuple[dt.datetime, LogLine]]:
    """Split one stream into (sort_key, LogLine) pairs.

    The sort key is separate from ``LogLine.timestamp`` deliberately. With
    ``timestamps=True`` Docker stamps every line it emits, so an unstamped line
    is a continuation of the one before it (a wrapped stack trace, say). It
    inherits the previous line's key so it sorts into place, while its own
    timestamp stays None -- reporting a timestamp it never had would be a lie.
    """
    if not payload:
        return []
    # errors="replace": container logs are arbitrary bytes and one invalid
    # sequence must not blow up the whole log view.
    text = payload.decode("utf-8", errors="replace")
    epoch = dt.datetime.min.replace(tzinfo=dt.UTC)
    out: list[tuple[dt.datetime, LogLine]] = []
    last_key = epoch

    for raw_line in text.splitlines():
        if not raw_line.strip():
            continue
        stamp, _, message = raw_line.partition(" ")
        parsed = _parse_docker_time(stamp)
        if parsed is None:
            out.append((last_key, LogLine(timestamp=None, stream=stream, message=raw_line)))
        else:
            last_key = parsed
            out.append((parsed, LogLine(timestamp=parsed, stream=stream, message=message)))
    return out
