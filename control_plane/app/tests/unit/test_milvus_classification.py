"""Error-classification truth table for the Milvus adapter.

Every exception instance here mirrors one observed against a real Milvus
2.6.20 / pymilvus 2.6.17 -- the codes and messages were captured from live
failures, not invented, because three of them are counter-intuitive:

  * connect failures surface as MilvusException(code=2), not grpc.RpcError
  * DescribeCollectionException carries code=0, the SUCCESS value
  * grpc._InactiveRpcError.code is a bound method, not an int

No infrastructure required.
"""

from __future__ import annotations

import grpc
import pytest
from pymilvus.exceptions import (
    CollectionNotExistException,
    DescribeCollectionException,
    IndexNotExistException,
    MilvusException,
    MilvusUnavailableException,
)

from app.adapters.milvus_client import MilvusErrorCode, classify_error


class FakeRpcError(grpc.RpcError):
    """Stand-in for grpc._channel._InactiveRpcError.

    Deliberately exposes code() and details() as METHODS, reproducing the trap
    that makes attribute-style access return a bound method.
    """

    def __init__(self, status: grpc.StatusCode, details: str = "") -> None:
        self._status = status
        self._details = details

    def code(self) -> grpc.StatusCode:
        return self._status

    def details(self) -> str:
        return self._details


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        # --- connection class (captured: code=2, NOT a grpc.RpcError) ------
        (
            MilvusException(
                2,
                "Fail connecting to server on localhost:19999, illegal connection "
                "params or server unavailable",
            ),
            MilvusErrorCode.UNREACHABLE,
        ),
        (
            MilvusException(
                2, "Fail connecting to server on no-such-host:19530, server unavailable"
            ),
            MilvusErrorCode.UNREACHABLE,
        ),
        (MilvusUnavailableException(1, "milvus is unavailable"), MilvusErrorCode.UNREACHABLE),
        (
            FakeRpcError(grpc.StatusCode.UNAVAILABLE, "failed to connect"),
            MilvusErrorCode.UNREACHABLE,
        ),
        (ConnectionRefusedError("[Errno 61] Connection refused"), MilvusErrorCode.UNREACHABLE),
        (OSError("network is unreachable"), MilvusErrorCode.UNREACHABLE),
        # --- timeout -------------------------------------------------------
        (
            FakeRpcError(grpc.StatusCode.DEADLINE_EXCEEDED, "Stream removed (Deadline Exceeded)"),
            MilvusErrorCode.TIMEOUT,
        ),
        # asyncio.TimeoutError is an alias of TimeoutError on 3.11+.
        (TimeoutError("outer asyncio.wait_for deadline"), MilvusErrorCode.TIMEOUT),
        (FakeRpcError(grpc.StatusCode.DEADLINE_EXCEEDED, ""), MilvusErrorCode.TIMEOUT),
        # --- auth ----------------------------------------------------------
        (FakeRpcError(grpc.StatusCode.UNAUTHENTICATED, "bad token"), MilvusErrorCode.AUTH_FAILED),
        (
            FakeRpcError(grpc.StatusCode.PERMISSION_DENIED, "not allowed"),
            MilvusErrorCode.AUTH_FAILED,
        ),
        (
            MilvusException(5, "auth check failure, please check credential"),
            MilvusErrorCode.AUTH_FAILED,
        ),
        # --- collection not found -------------------------------------------
        # code=0 collides with SUCCESS: must be caught by type, not number.
        (
            DescribeCollectionException(
                0, "can't find collection[database=default][collection=missing_xyz]"
            ),
            MilvusErrorCode.COLLECTION_NOT_FOUND,
        ),
        (
            MilvusException(100, "collection not found[database=default][collection=missing_xyz]"),
            MilvusErrorCode.COLLECTION_NOT_FOUND,
        ),
        (
            CollectionNotExistException(100, "collection not exist"),
            MilvusErrorCode.COLLECTION_NOT_FOUND,
        ),
        (IndexNotExistException(700, "index not found"), MilvusErrorCode.COLLECTION_NOT_FOUND),
        (FakeRpcError(grpc.StatusCode.NOT_FOUND, "nope"), MilvusErrorCode.COLLECTION_NOT_FOUND),
        # --- generic RPC ----------------------------------------------------
        (MilvusException(1, "unexpected error"), MilvusErrorCode.RPC_ERROR),
        (FakeRpcError(grpc.StatusCode.INTERNAL, "boom"), MilvusErrorCode.RPC_ERROR),
        (ValueError("something else entirely"), MilvusErrorCode.RPC_ERROR),
    ],
)
def test_classification(exc: BaseException, expected: str) -> None:
    code, message = classify_error(exc)
    assert code == expected, f"{type(exc).__name__} -> {code}, expected {expected}"
    assert message, "a classified error must carry a message"


def test_every_stable_code_is_reachable() -> None:
    """Guard against a code that no input can ever produce."""
    produced = {
        classify_error(e)[0]
        for e in (
            MilvusException(2, "Fail connecting to server on x:1, server unavailable"),
            FakeRpcError(grpc.StatusCode.DEADLINE_EXCEEDED, ""),
            FakeRpcError(grpc.StatusCode.UNAUTHENTICATED, ""),
            MilvusException(100, "collection not found"),
            MilvusException(1, "unexpected error"),
        )
    }
    assert produced == {
        MilvusErrorCode.UNREACHABLE,
        MilvusErrorCode.TIMEOUT,
        MilvusErrorCode.AUTH_FAILED,
        MilvusErrorCode.COLLECTION_NOT_FOUND,
        MilvusErrorCode.RPC_ERROR,
    }


def test_grpc_code_is_a_method_not_an_attribute() -> None:
    """Pin the trap that motivates checking grpc.RpcError first."""
    exc = FakeRpcError(grpc.StatusCode.DEADLINE_EXCEEDED, "deadline")
    assert callable(exc.code), "grpc exposes code() as a method"
    assert not isinstance(getattr(exc, "code", None), int)
    assert classify_error(exc)[0] == MilvusErrorCode.TIMEOUT


def test_describe_collection_exception_uses_success_code() -> None:
    """code=0 means SUCCESS elsewhere; only the type disambiguates."""
    exc = DescribeCollectionException(0, "can't find collection[collection=x]")
    assert exc.code == 0
    assert classify_error(exc)[0] == MilvusErrorCode.COLLECTION_NOT_FOUND
