"""Secure Webhook server supporting Telegram and WhatsApp approval commands.

Verifies webhook tokens/signatures, authenticates senders against allowlists,
strictly parses APPROVE/REJECT commands, and safely gates LinkedIn/Publora execution.
"""

from __future__ import annotations

import hmac
import hashlib
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlparse

from .db import ActionRecord, ActionStatus, ActionType, ApprovalDB
from .telegram import TelegramClient
from .whatsapp import WhatsAppClient, clean_phone_number

logger = logging.getLogger("automation.webhook")


def verify_meta_signature(payload: bytes, signature_header: str, app_secret: str) -> bool:
    """Verify X-Hub-Signature-256 header from Meta using the app secret."""
    if not app_secret:
        return True
    if not signature_header:
        return False
    parts = signature_header.split("sha256=")
    if len(parts) != 2:
        return False
    expected_hash = hmac.new(
        app_secret.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(parts[1].strip(), expected_hash)


def verify_telegram_secret(secret_header: str, expected_secret: str) -> bool:
    """Verify X-Telegram-Bot-Api-Secret-Token header."""
    if not expected_secret:
        return True
    return hmac.compare_digest(secret_header.strip(), expected_secret.strip())


def execute_approved_action(
    action: ActionRecord,
    dry_run: bool = False,
    mock_publisher: Optional[Callable[[ActionRecord], Any]] = None,
) -> dict[str, Any]:
    """Execute the approved action using the library publishing layer.

    For POST actions, assigns a near-future scheduled_time (now + 2 minutes UTC)
    so Publora actually publishes to the LinkedIn feed rather than saving a draft.
    """
    kind_map = {
        ActionType.POST.value: "post",
        ActionType.COMMENT.value: "comment",
        ActionType.REPLY.value: "reply",
        ActionType.REACT.value: "reaction",
        ActionType.RESHARE.value: "reshare",
    }
    kind = kind_map.get(action.action_type.upper(), "post")
    target_url = action.target or "https://www.linkedin.com/feed/"

    extra = dict(action.extra_params or {})

    # For POST actions, ensure a valid near-future scheduled_time in ISO-8601 UTC
    if kind == "post":
        sched = extra.get("scheduled_time")
        if not sched:
            # Default to now + 2 minutes in ISO-8601 UTC format
            now_utc = datetime.now(timezone.utc)
            slot = now_utc + timedelta(minutes=2)
            sched = slot.strftime("%Y-%m-%dT%H:%M:%SZ")
            extra["scheduled_time"] = sched
            logger.info(f"Action #{action.id}: Set live publication slot to {sched} UTC")

    if mock_publisher is not None:
        # Pass extra params context if mock publisher supports it
        action_copy = ActionRecord(
            id=action.id,
            action_type=action.action_type,
            target=action.target,
            content=action.content,
            reason=action.reason,
            content_hash=action.content_hash,
            extra_params=extra,
            status=action.status,
            created_at=action.created_at,
            approved_at=action.approved_at,
            executed_at=action.executed_at,
            rejection_reason=action.rejection_reason,
            execution_result=action.execution_result,
            whatsapp_message_id=action.whatsapp_message_id,
        )
        return mock_publisher(action_copy)

    # Check both Telegram and WhatsApp dry run flags
    is_dry = dry_run or (
        os.getenv("TELEGRAM_DRY_RUN", "true").lower() in ("true", "1", "yes")
        and os.getenv("WHATSAPP_DRY_RUN", "true").lower() in ("true", "1", "yes")
    )

    if is_dry:
        logger.info(
            f"[DRY-RUN] Executing action #{action.id} ({action.action_type}) on {target_url} "
            f"with scheduled_time={extra.get('scheduled_time')}"
        )
        return {
            "mode": "dry_run",
            "action_id": action.id,
            "status": "simulated_success",
            "scheduledTime": extra.get("scheduled_time"),
            "target": target_url,
        }

    # Live execution via lib.publish
    from lib import publish

    res = publish(
        kind=kind,
        draft_text=action.content,
        target_url=target_url,
        **extra,
    ) or {"status": "published"}

    if kind == "post" and isinstance(res, dict):
        if not res.get("scheduledTime") and extra.get("scheduled_time"):
            res["scheduledTime"] = extra.get("scheduled_time")

    return res


class WebhookHandler(BaseHTTPRequestHandler):
    """HTTP Request Handler for Telegram & WhatsApp Webhooks."""

    db: ApprovalDB = None  # type: ignore
    telegram: TelegramClient = None  # type: ignore
    whatsapp: WhatsAppClient = None  # type: ignore
    mock_publisher: Optional[Callable[[ActionRecord], Any]] = None

    def _send_response_json(self, status_code: int, data: dict[str, Any]) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_plain(self, status_code: int, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        """Handle GET requests (e.g. Health checks or Meta Webhook challenge verification)."""
        parsed_url = urlparse(self.path)
        if parsed_url.path in ("/health", "/healthz"):
            db_healthy = False
            stats = {}
            try:
                stats = self.db.get_stats()
                db_healthy = True
            except Exception as e:
                logger.error(f"Healthcheck DB query failed: {e}")

            health_info = {
                "status": "healthy" if db_healthy else "unhealthy",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "database": "connected" if db_healthy else "error",
                "queue_counts": stats,
                "primary_channel": "telegram",
            }
            self._send_response_json(200 if db_healthy else 503, health_info)
            return

        if parsed_url.path == "/":
            self._send_plain(200, "OK: Autonomous LinkedIn Approval Server")
            return

        if parsed_url.path not in ("/webhook", "/webhook/whatsapp", "/webhook/telegram"):
            self._send_plain(404, "Not Found")
            return

        query = parse_qs(parsed_url.query)
        mode = query.get("hub.mode", [""])[0]
        token = query.get("hub.verify_token", [""])[0]
        challenge = query.get("hub.challenge", [""])[0]

        expected_token = os.getenv("WHATSAPP_VERIFY_TOKEN", "").strip()

        if mode == "subscribe" and (not expected_token or token == expected_token):
            logger.info("Webhook verification challenge passed.")
            self._send_plain(200, challenge)
        else:
            self._send_plain(200, "Webhook endpoint active")

    def do_POST(self) -> None:
        """Handle incoming webhook POST requests from Telegram or WhatsApp."""
        parsed_url = urlparse(self.path)
        if parsed_url.path not in ("/webhook", "/webhook/telegram", "/webhook/whatsapp"):
            self._send_plain(404, "Not Found")
            return

        content_length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(content_length)

        # Parse JSON payload
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except Exception as e:
            logger.error(f"Failed to parse webhook JSON: {e}")
            self._send_plain(400, "Bad Request")
            return

        # 1. Check if Telegram payload
        if parsed_url.path == "/webhook/telegram" or "update_id" in payload:
            secret_header = self.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
            expected_secret = os.getenv("TELEGRAM_SECRET_TOKEN", "").strip()
            if expected_secret and not verify_telegram_secret(secret_header, expected_secret):
                logger.error("Invalid Telegram secret token header.")
                self._send_plain(403, "Invalid secret token")
                return

            self._send_response_json(200, {"ok": True})
            self.process_telegram_payload(payload)
            return

        # 2. Check if WhatsApp / Meta payload
        if parsed_url.path == "/webhook/whatsapp" or "entry" in payload:
            app_secret = os.getenv("WHATSAPP_APP_SECRET", "").strip()
            sig_header = self.headers.get("X-Hub-Signature-256", "")
            if app_secret and not verify_meta_signature(raw_body, sig_header, app_secret):
                logger.error("Invalid HMAC signature on incoming WhatsApp request.")
                self._send_plain(403, "Invalid signature")
                return

            self._send_response_json(200, {"status": "received"})
            self.process_whatsapp_payload(payload)
            return

        self._send_response_json(200, {"status": "ignored"})

    def process_telegram_payload(self, payload: dict[str, Any]) -> None:
        """Extract and process Telegram message from update payload."""
        msg = payload.get("message") or payload.get("channel_post") or payload.get("edited_message")
        if not msg:
            return
        text = msg.get("text", "")
        sender_chat = msg.get("chat", {}).get("id")
        if not text or sender_chat is None:
            return

        self.telegram.process_incoming_message(
            text=text,
            sender_chat_id=sender_chat,
            db=self.db,
            mock_publisher=self.mock_publisher,
            execute_fn=execute_approved_action,
        )

    def process_whatsapp_payload(self, payload: dict[str, Any]) -> None:
        """Process incoming WhatsApp payload."""
        entries = payload.get("entry", [])
        for entry in entries:
            changes = entry.get("changes", [])
            for change in changes:
                value = change.get("value", {})
                messages = value.get("messages", [])
                for msg in messages:
                    self.process_single_whatsapp_message(msg)

    def process_single_whatsapp_message(self, msg: dict[str, Any]) -> None:
        """Process a single incoming WhatsApp message safely."""
        sender = msg.get("from", "")
        if msg.get("type") != "text":
            return
        text = msg.get("text", {}).get("body", "").strip()

        allowed_sender = clean_phone_number(os.getenv("WHATSAPP_RECIPIENT_PHONE", ""))
        clean_sender = clean_phone_number(sender)

        if allowed_sender and clean_sender != allowed_sender:
            logger.warning(f"Unauthorized WhatsApp sender {clean_sender}. Ignoring.")
            return

        approve_match = re.match(r"^APPROVE\s+(\d+)$", text, re.IGNORECASE)
        reject_match = re.match(r"^REJECT\s+(\d+)(?:\s+(.*))?$", text, re.IGNORECASE)

        if approve_match:
            action_id = int(approve_match.group(1))
            ok, record, err = self.db.acquire_approval_lock(action_id)
            if not ok or record is None:
                self.whatsapp.send_text(f"❌ Cannot approve Action #{action_id}: {err}", recipient=sender)
                return
            try:
                res = execute_approved_action(record, self.whatsapp.dry_run, self.mock_publisher)
                self.db.mark_executed(action_id, res)
                self.whatsapp.send_execution_success(record, str(res))
            except Exception as e:
                self.db.mark_failed(action_id, str(e))
                self.whatsapp.send_execution_failure(record, str(e))
        elif reject_match:
            action_id = int(reject_match.group(1))
            reason = reject_match.group(2) or "Rejected by user via WhatsApp"
            ok, record, err = self.db.reject_action(action_id, reason)
            if not ok or record is None:
                self.whatsapp.send_text(f"❌ Cannot reject Action #{action_id}: {err}", recipient=sender)
                return
            self.whatsapp.send_rejection_notice(record, reason)


def make_webhook_server(
    host: str = "0.0.0.0",
    port: int = 8080,
    db: Optional[ApprovalDB] = None,
    telegram: Optional[TelegramClient] = None,
    whatsapp: Optional[WhatsAppClient] = None,
    mock_publisher: Optional[Callable[[ActionRecord], Any]] = None,
) -> HTTPServer:
    """Create and configure the Webhook HTTP Server."""
    handler_class = type(
        "ConfiguredWebhookHandler",
        (WebhookHandler,),
        {
            "db": db or ApprovalDB(),
            "telegram": telegram or TelegramClient(),
            "whatsapp": whatsapp or WhatsAppClient(),
            "mock_publisher": mock_publisher,
        },
    )
    return HTTPServer((host, port), handler_class)
