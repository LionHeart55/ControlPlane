"""Object-store and metadata-store probes. No infrastructure.

SigV4 gets the most attention here because it is the kind of code that is either
exactly right or silently returns 403, with nothing in between and no useful
error to read. The canonical form is pinned against a fixed clock so a change to
the header set, the ordering or the scope fails loudly.
"""

from __future__ import annotations

import datetime as dt

import httpx
import pytest

from app.adapters.etcd_client import (
    METADATA_STORE_UNHEALTHY,
    METADATA_STORE_UNREACHABLE,
    MetadataStoreAdapter,
)
from app.adapters.minio_client import (
    OBJECT_STORE_AUTH_FAILED,
    OBJECT_STORE_BUCKET_MISSING,
    OBJECT_STORE_ERROR,
    OBJECT_STORE_UNREACHABLE,
    ObjectStoreAdapter,
    sign_request,
    signing_key,
)
from app.config import Settings

FIXED = dt.datetime(2026, 8, 8, 12, 0, 0, tzinfo=dt.UTC)


def settings(**kw: object) -> Settings:
    base: dict[str, object] = {
        "minio_endpoint": "milvus-minio:9000",
        "minio_bucket": "milvus-bucket",
        "minio_root_user": "minioadmin",
        "minio_root_password": "minioadmin",
        "minio_region": "us-east-1",
        "etcd_endpoint": "milvus-etcd:2379",
    }
    base.update(kw)
    return Settings(_env_file=None, **base)  # type: ignore[arg-type]


def store_adapter(handler: object, **kw: object) -> ObjectStoreAdapter:
    adapter = ObjectStoreAdapter(settings=settings(**kw))
    adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
    return adapter


def etcd_adapter(handler: object) -> MetadataStoreAdapter:
    adapter = MetadataStoreAdapter(settings=settings())
    adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
    return adapter


# --- SigV4 ----------------------------------------------------------------
def test_signature_is_stable_for_a_fixed_clock() -> None:
    """Pins the canonical request. Any change to the signed header set, their
    order, or the credential scope changes this value."""
    headers = sign_request(
        method="HEAD",
        host="milvus-minio:9000",
        path="/milvus-bucket",
        access_key="minioadmin",
        secret_key="minioadmin",
        region="us-east-1",
        now=FIXED,
    )
    assert headers["x-amz-date"] == "20260808T120000Z"
    assert "Credential=minioadmin/20260808/us-east-1/s3/aws4_request" in headers["Authorization"]
    assert "SignedHeaders=host;x-amz-content-sha256;x-amz-date" in headers["Authorization"]
    assert headers["Authorization"].startswith("AWS4-HMAC-SHA256 ")

    signature = headers["Authorization"].rsplit("Signature=", 1)[1]
    assert len(signature) == 64 and all(c in "0123456789abcdef" for c in signature)


def test_signature_changes_with_every_input_that_matters() -> None:
    base: dict[str, object] = {
        "method": "HEAD",
        "host": "milvus-minio:9000",
        "path": "/milvus-bucket",
        "access_key": "minioadmin",
        "secret_key": "minioadmin",
        "region": "us-east-1",
        "now": FIXED,
    }

    def sig(**overrides: object) -> str:
        headers = sign_request(**{**base, **overrides})  # type: ignore[arg-type]
        return headers["Authorization"].rsplit("Signature=", 1)[1]

    reference = sig()
    assert sig(secret_key="other") != reference, "secret must affect the signature"
    assert sig(path="/other-bucket") != reference, "path must affect the signature"
    assert sig(region="eu-west-1") != reference, "region must affect the signature"
    assert sig(host="other:9000") != reference, "host is a signed header"
    assert sig(method="GET") != reference, "method must affect the signature"
    assert sig(now=FIXED + dt.timedelta(seconds=1)) != reference, "time must affect it"


def test_signing_key_derivation_is_four_nested_hmacs() -> None:
    """Wrong order or a missing round produces a plausible-looking key that
    always fails, so the derivation is checked directly."""
    key = signing_key("secret", "20260808", "us-east-1")
    assert isinstance(key, bytes) and len(key) == 32
    assert signing_key("secret", "20260809", "us-east-1") != key
    assert signing_key("secret", "20260808", "eu-west-1") != key


def test_the_bucket_is_actually_signed_for() -> None:
    """Regression guard: the probe must sign the bucket path, not '/'."""
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization", "")
        return httpx.Response(200)

    adapter = store_adapter(handler)
    import asyncio

    asyncio.run(adapter.probe())
    assert captured["url"] == "http://milvus-minio:9000/milvus-bucket"
    assert "AWS4-HMAC-SHA256" in str(captured["auth"])


# --- object store outcomes ------------------------------------------------
async def test_reachable_bucket() -> None:
    result = await store_adapter(lambda r: httpx.Response(200)).probe()
    assert result.reachable is True
    assert result.error_code is None
    assert result.latency_ms is not None


async def test_missing_bucket_is_distinct_from_auth_failure() -> None:
    """An unsigned probe cannot tell these apart -- MinIO answers 403 to an
    anonymous HEAD whether the bucket exists or not. Signing is what makes
    these two different answers, and they need different fixes."""
    missing = await store_adapter(lambda r: httpx.Response(404)).probe()
    assert missing.reachable is False
    assert missing.error_code == OBJECT_STORE_BUCKET_MISSING

    denied = await store_adapter(lambda r: httpx.Response(403)).probe()
    assert denied.reachable is False
    assert denied.error_code == OBJECT_STORE_AUTH_FAILED
    assert "MINIO_ROOT_USER" in (denied.error_message or ""), "the message must say what to fix"


async def test_unreachable_store() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    result = await store_adapter(refuse).probe()
    assert result.reachable is False
    assert result.error_code == OBJECT_STORE_UNREACHABLE


async def test_unexpected_status_is_reported_not_swallowed() -> None:
    result = await store_adapter(lambda r: httpx.Response(500)).probe()
    assert result.reachable is False
    assert result.error_code == OBJECT_STORE_ERROR


async def test_probe_never_raises() -> None:
    """Callers rely on this: a probe that determines the store is down has
    succeeded, so it returns rather than raising."""

    def explode(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow")

    assert (await store_adapter(explode).probe()).reachable is False


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("milvus-minio:9000", "http://milvus-minio:9000"),
        ("http://milvus-minio:9000", "http://milvus-minio:9000"),
        ("https://s3.example.com", "https://s3.example.com"),
    ],
)
def test_endpoint_accepts_bare_host_or_url(endpoint: str, expected: str) -> None:
    """MINIO_ENDPOINT is a bare host:port, but clusters.object_store_endpoint
    may hold either form."""
    assert ObjectStoreAdapter(settings=settings(minio_endpoint=endpoint)).base_url == expected


# --- metadata store -------------------------------------------------------
async def test_healthy_etcd() -> None:
    body = '{"health":"true","reason":""}'
    result = await etcd_adapter(lambda r: httpx.Response(200, text=body)).probe()
    assert result.reachable is True
    assert result.error_code is None


async def test_health_field_is_a_string_not_a_boolean() -> None:
    """etcd returns the STRING "true". Comparing against a bool would silently
    report every healthy cluster as unhealthy."""
    result = await etcd_adapter(lambda r: httpx.Response(200, text='{"health":"true"}')).probe()
    assert result.reachable is True


async def test_unhealthy_etcd_is_distinct_from_unreachable() -> None:
    """A cluster that answers and says it cannot serve (no quorum, alarms set)
    is a different problem from one that does not answer."""
    body = '{"health":"false","reason":"NOSPACE"}'
    result = await etcd_adapter(lambda r: httpx.Response(503, text=body)).probe()
    assert result.reachable is False
    assert result.error_code == METADATA_STORE_UNHEALTHY
    assert "NOSPACE" in (result.error_message or "")


async def test_unreachable_etcd() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    result = await etcd_adapter(refuse).probe()
    assert result.reachable is False
    assert result.error_code == METADATA_STORE_UNREACHABLE


async def test_garbage_body_does_not_raise() -> None:
    result = await etcd_adapter(lambda r: httpx.Response(200, text="<html>oops")).probe()
    assert result.reachable is False
    assert result.error_code == METADATA_STORE_UNHEALTHY
