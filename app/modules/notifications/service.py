"""Notification service — port + in-memory/log stub.

Production adapters: WhatsApp Cloud API templates, email, SMS. The
notification layer never changes business state; it only informs.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("fde.notifications")


def notify(recipients: list[str], template: str, context: dict) -> None:
    """Stub notification. Replace with a real adapter (WhatsApp/email)."""
    logger.info("notify template=%s recipients=%s context=%s",
                template, recipients, context)
