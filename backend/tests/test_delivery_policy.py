from datetime import timedelta

from app.services.delivery_policy import (
    DELIVERY_ALLOCATION_HEADROOM,
    credential_delivery_retry_delay_seconds,
)


def test_delivery_headroom_covers_full_retry_and_transport_horizon():
    assert [
        credential_delivery_retry_delay_seconds(attempt)
        for attempt in range(1, 6)
    ] == [60, 120, 240, 480, 960]
    assert DELIVERY_ALLOCATION_HEADROOM == timedelta(minutes=40)
