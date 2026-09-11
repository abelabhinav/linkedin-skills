"""Autonomous LinkedIn automation and approval package.

Handles opportunity research, drafting, auditing, persistent approval queuing (SQLite),
Telegram / WhatsApp notifications, and secure execution gates.
"""

from .db import ActionRecord, ActionStatus, ActionType, ApprovalDB
from .telegram import TelegramClient
from .whatsapp import WhatsAppClient
from .auditor import audit_draft
from .engine import AutomationEngine

__all__ = [
    "ApprovalDB",
    "ActionRecord",
    "ActionStatus",
    "ActionType",
    "TelegramClient",
    "WhatsAppClient",
    "audit_draft",
    "AutomationEngine",
]
