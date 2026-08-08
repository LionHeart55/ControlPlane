"""Direct object-store probe.

Why this exists
---------------
Neither of the two health signals the control plane already had can see an
object-store outage, which is the single most misleading failure Milvus has:

  * ``:9091/healthz`` is a shallow liveness check. During the WP-15 drill it
    returned **200 with MinIO completely stopped**.
  * ``deep_probe`` -- connect, ``list_collections``, ``describe_collection`` --
    **also passed completely**. Those calls are answered from etcd metadata by
    way of RootCoord and never touch object storage. That limitation was written
    into ``milvus_client.py``'s docstring when it was built; the drill confirmed
    it.

What actually caught it was component reconciliation noticing the container had
exited. That works here and only here: point the deployment at S3, or run on
Kubernetes with an external object store, and there is no container to
reconcile, so every probe reports healthy through a total outage.

This module closes that gap by asking the object store itself, which is the
only check that keeps working when MinIO is not a container we can see.

Why the request is signed
-------------------------
An unauthenticated ``HEAD`` cannot answer the question. Measured against the
running MinIO:

    anonymous HEAD /milvus-bucket      -> 403
    anonymous HEAD /no-such-bucket-xyz -> 403

Identical. So an unsigned probe can tell you a server is listening and nothing
else -- not whether the bucket exists, not whether the credentials work. Since
Milvus stores the Woodpecker WAL in that bucket, "the bucket is reachable with
these credentials" is the fact that matters, and getting it requires SigV4.

Signing is done here rather than by pulling in boto3 or the minio SDK: it is
about forty lines of hmac, the control plane makes exactly one kind of S3
request, and neither library is otherwise needed. httpx is already a dependency.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from app.config import Settings, get_settings
from app.logging_conf import get_logger

log = get_logger("object_store")

PROBE_TIMEOUT_S = 3.0

OBJECT_STORE_UNREACHABLE = "OBJECT_STORE_UNREACHABLE"
OBJECT_STORE_AUTH_FAILED = "OBJECT_STORE_AUTH_FAILED"
OBJECT_STORE_BUCKET_MISSING = "OBJECT_STORE_BUCKET_MISSING"
OBJECT_STORE_ERROR = "OBJECT_STORE_ERROR"

_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_ALGORITHM = "AWS4-HMAC-SHA256"


@dataclass(frozen=True)
class StoreProbeResult:
    """Outcome of a store probe. Never raised -- always returned.

    Same contract as `ProbeResult` in the Milvus adapter: a probe that
    determines the dependency is down has *succeeded*, so it returns rather
    than raising, and the caller decides what that means for overall status.
    """

    reachable: bool
    latency_ms: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "reachable": self.reachable,
            "latency_ms": self.latency_ms,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "detail": self.detail,
        }


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def signing_key(secret: str, date_stamp: str, region: str, service: str = "s3") -> bytes:
    """Derive the SigV4 signing key. Four nested HMACs, in this order."""
    key = _hmac(f"AWS4{secret}".encode(), date_stamp)
    key = _hmac(key, region)
    key = _hmac(key, service)
    return _hmac(key, "aws4_request")


def sign_request(
    *,
    method: str,
    host: str,
    path: str,
    access_key: str,
    secret_key: str,
    region: str,
    now: dt.datetime | None = None,
    payload_hash: str = _EMPTY_SHA256,
) -> dict[str, str]:
    """Build the SigV4 headers for one request.

    Pure and injectable (`now`) so the canonical form can be pinned in a unit
    test against a known signature -- signing is exactly the kind of code that
    is either perfectly right or silently 403s, with nothing in between.
    """
    stamp = now or dt.datetime.now(dt.UTC)
    amz_date = stamp.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = stamp.strftime("%Y%m%d")

    # The path must be URI-encoded but keep its slashes; a bucket name never
    # needs escaping, but an object key would.
    canonical_uri = quote(path, safe="/~")
    canonical_headers = f"host:{host}\nx-amz-content-sha256:{payload_hash}\nx-amz-date:{amz_date}\n"
    signed_headers = "host;x-amz-content-sha256;x-amz-date"

    canonical_request = "\n".join(
        [method, canonical_uri, "", canonical_headers, signed_headers, payload_hash]
    )
    scope = f"{date_stamp}/{region}/s3/aws4_request"
    string_to_sign = "\n".join(
        [
            _ALGORITHM,
            amz_date,
            scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )
    signature = hmac.new(
        signing_key(secret_key, date_stamp, region),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return {
        "Host": host,
        "x-amz-date": amz_date,
        "x-amz-content-sha256": payload_hash,
        "Authorization": (
            f"{_ALGORITHM} Credential={access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        ),
    }


class ObjectStoreAdapter:
    """Probes the MinIO/S3 endpoint Milvus is configured to use."""

    def __init__(self, endpoint: str | None = None, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        raw = endpoint or self._settings.minio_endpoint
        # MINIO_ENDPOINT is a bare host:port ("milvus-minio:9000"); tolerate a
        # full URL too, since `clusters.object_store_endpoint` may carry either.
        if "://" in raw:
            parsed = urlsplit(raw)
            self._scheme = parsed.scheme
            self._host = parsed.netloc
        else:
            self._scheme = "http"
            self._host = raw
        self._bucket = self._settings.minio_bucket
        self._region = self._settings.minio_region
        self._client: httpx.AsyncClient | None = None

    @property
    def base_url(self) -> str:
        return f"{self._scheme}://{self._host}"

    @property
    def bucket(self) -> str:
        return self._bucket

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=PROBE_TIMEOUT_S)
        return self._client

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    async def probe(self) -> StoreProbeResult:
        """Signed HEAD on the configured bucket. Never raises.

        Three distinguishable outcomes, because they need different responses:
        the store is down (restart it), the credentials are wrong (fix .env),
        or the bucket is gone (recreate it -- `deploy.sh` does this via `mc`).
        """
        started = time.perf_counter()
        path = f"/{self._bucket}"
        headers = sign_request(
            method="HEAD",
            host=self._host,
            path=path,
            access_key=self._settings.minio_root_user,
            secret_key=self._settings.minio_root_password,
            region=self._region,
        )

        try:
            response = await self._get_client().head(f"{self.base_url}{path}", headers=headers)
        except (httpx.HTTPError, OSError) as exc:
            elapsed = int((time.perf_counter() - started) * 1000)
            return StoreProbeResult(
                reachable=False,
                latency_ms=elapsed,
                error_code=OBJECT_STORE_UNREACHABLE,
                error_message=f"cannot reach {self.base_url}: {exc}"[:300],
                detail={"endpoint": self.base_url, "bucket": self._bucket},
            )

        elapsed = int((time.perf_counter() - started) * 1000)
        detail = {"endpoint": self.base_url, "bucket": self._bucket, "status": response.status_code}

        if response.status_code == 200:
            return StoreProbeResult(reachable=True, latency_ms=elapsed, detail=detail)
        if response.status_code == 404:
            return StoreProbeResult(
                reachable=False,
                latency_ms=elapsed,
                error_code=OBJECT_STORE_BUCKET_MISSING,
                error_message=f"bucket {self._bucket!r} does not exist",
                detail=detail,
            )
        if response.status_code in (401, 403):
            return StoreProbeResult(
                reachable=False,
                latency_ms=elapsed,
                error_code=OBJECT_STORE_AUTH_FAILED,
                error_message=(
                    f"credentials rejected by the object store (HTTP {response.status_code}); "
                    f"check MINIO_ROOT_USER / MINIO_ROOT_PASSWORD"
                ),
                detail=detail,
            )
        return StoreProbeResult(
            reachable=False,
            latency_ms=elapsed,
            error_code=OBJECT_STORE_ERROR,
            error_message=f"unexpected HTTP {response.status_code} from the object store",
            detail=detail,
        )

    async def ping(self) -> bool:
        """Cheap boolean for callers that only need reachability."""
        return (await self.probe()).reachable
