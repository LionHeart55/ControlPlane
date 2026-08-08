"""Shared setup for tests that need the real stack.

These skip -- loudly, with a reason -- rather than fail when nothing is
running, so `make test` is green on a laptop with Docker stopped while still
running for real in CI or before a demo. A hard failure here would train people
to ignore a red suite, which is worse than a skip they can see.

Run them against a stack with:

    ./infra/deploy.sh up --profile all
    make test-integration
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

API_BASE = os.environ.get("CP_BASE_URL", "http://localhost:8000")
REPO_ROOT = Path(__file__).resolve().parents[4]

# Generous: a forced health check probes Milvus with a 5s RPC timeout.
HTTP_TIMEOUT = 30.0


def stack_is_up() -> tuple[bool, str]:
    try:
        response = httpx.get(f"{API_BASE}/readyz", timeout=5.0)
    except Exception as exc:
        return False, f"control-plane API unreachable at {API_BASE}: {type(exc).__name__}"
    if response.status_code != 200:
        return False, f"{API_BASE}/readyz returned {response.status_code} (PostgreSQL down?)"
    return True, ""


@pytest.fixture(scope="session", autouse=True)
def require_stack() -> None:
    up, reason = stack_is_up()
    if not up:
        pytest.skip(
            f"{reason}\nStart it with: ./infra/deploy.sh up --profile all",
            allow_module_level=True,
        )


@pytest.fixture(scope="session")
def client() -> Iterator[httpx.Client]:
    with httpx.Client(base_url=API_BASE, timeout=HTTP_TIMEOUT) as http:
        yield http


@pytest.fixture(scope="session")
def cluster_id(client: httpx.Client) -> str:
    """The registered cluster. Registers one if the table is somehow empty."""
    page = client.get("/api/v1/clusters", params={"limit": 1}).json()
    if page["items"]:
        return str(page["items"][0]["id"])

    created = client.post(
        "/api/v1/clusters",
        json={
            "name": "integration-test",
            "endpoint_uri": os.environ.get("MILVUS_URI", "http://milvus-standalone:19530"),
            "deployment_type": "docker_standalone",
        },
    )
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


def docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=120, check=False
    )


def wait_until(predicate: Any, timeout_s: float, interval_s: float = 1.0, what: str = "") -> Any:
    """Poll until `predicate` returns something truthy, else fail with context."""
    deadline = time.monotonic() + timeout_s
    last: Any = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval_s)
    pytest.fail(f"timed out after {timeout_s}s waiting for {what or 'condition'} (last: {last!r})")
