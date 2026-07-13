from __future__ import annotations

from datetime import timedelta


CREDENTIAL_DELIVERY_MAX_ATTEMPTS = 6
CREDENTIAL_SEND_TIMEOUT_SECONDS = 30
CREDENTIAL_DELIVERY_RETRY_BASE_SECONDS = 60
CREDENTIAL_DELIVERY_RETRY_MAX_SECONDS = 60 * 60
CREDENTIAL_DELIVERY_POLL_SECONDS = 60
_DELIVERY_POLICY_SAFETY_SECONDS = 60


def credential_delivery_retry_delay_seconds(attempt_number: int) -> float:
    """Backoff after a failed 1-based attempt that still has a retry."""

    exponent = max(0, attempt_number - 1)
    return min(
        CREDENTIAL_DELIVERY_RETRY_BASE_SECONDS * (2**exponent),
        CREDENTIAL_DELIVERY_RETRY_MAX_SECONDS,
    )


# Subscription eligibility must cover the complete automated delivery horizon:
# five retry delays (1+2+4+8+16m), six bounded sends, scheduler jitter before
# every retry, plus a small processing margin. Keeping this formula beside the
# policy constants prevents a retry-count/timeout change from silently making
# a paid order impossible to deliver after allocation.
DELIVERY_ALLOCATION_HEADROOM = timedelta(
    seconds=(
        sum(
            credential_delivery_retry_delay_seconds(attempt)
            for attempt in range(1, CREDENTIAL_DELIVERY_MAX_ATTEMPTS)
        )
        + CREDENTIAL_DELIVERY_MAX_ATTEMPTS
        * CREDENTIAL_SEND_TIMEOUT_SECONDS
        + (CREDENTIAL_DELIVERY_MAX_ATTEMPTS - 1)
        * CREDENTIAL_DELIVERY_POLL_SECONDS
        + _DELIVERY_POLICY_SAFETY_SECONDS
    )
)
