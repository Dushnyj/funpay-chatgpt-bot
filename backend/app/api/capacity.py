from __future__ import annotations

import logging

from fastapi import Request


logger = logging.getLogger(__name__)


def notify_capacity_changed(request: Request) -> None:
    """Best-effort post-commit trigger for FunPay stock reconciliation."""

    lifecycle = getattr(request.app.state, "lifecycle", None)
    callback = getattr(lifecycle, "request_capacity_reconcile", None)
    if not callable(callback):
        return
    try:
        callback()
    except Exception:
        # The database transition is already durable. Never return a false 5xx
        # that encourages the operator to repeat a successful mutation; the
        # periodic lot reconciler remains the eventual recovery path.
        logger.exception("Could not schedule capacity reconciliation")
