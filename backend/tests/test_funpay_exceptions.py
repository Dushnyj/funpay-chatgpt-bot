import pytest

from app.integrations.funpay.exceptions import (
    FunPayError,
    FunPayApiError,
    GoldenKeyError,
)


def test_funpay_error_is_exception():
    assert issubclass(FunPayError, Exception)


def test_funpay_api_error_carries_status_and_body():
    err = FunPayApiError(status=403, body="forbidden")
    assert err.status == 403
    assert err.body == "forbidden"
    assert isinstance(err, FunPayError)
    assert "403" in str(err)


def test_golden_key_error_is_funpay_error():
    err = GoldenKeyError("session expired")
    assert isinstance(err, FunPayError)
    assert "session expired" in str(err)
