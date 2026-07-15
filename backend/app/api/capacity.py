from __future__ import annotations

import logging

from fastapi import Request


logger = logging.getLogger(__name__)


def _notify_lifecycle(
    request: Request,
    callback_name: str,
    failure_message: str,
) -> None:
    """Call one lifecycle edge after its database transition is durable."""

    lifecycle = getattr(request.app.state, "lifecycle", None)
    callback = getattr(lifecycle, callback_name, None)
    if not callable(callback):
        return
    try:
        callback()
    except Exception:
        # The database transition is already durable. Never return a false 5xx
        # that encourages the operator to repeat a successful mutation; the
        # periodic scheduler remains the eventual recovery path.
        logger.exception(failure_message)


def notify_capacity_changed(request: Request) -> None:
    """Best-effort post-commit trigger for FunPay stock reconciliation."""

    _notify_lifecycle(
        request,
        "request_capacity_reconcile",
        "Could not schedule capacity reconciliation",
    )


def notify_validation_queued(request: Request) -> None:
    """Best-effort wake-up after a durable validation-job enqueue."""

    _notify_lifecycle(
        request,
        "request_validation_check",
        "Could not wake validation queue",
    )
