"""Telegram Bot API client with dry-run support, persistent offset tracking, and approval processing.

Formats standardized approval cards, handles outgoing notifications to Telegram,
processes incoming APPROVE/REJECT/RETRY commands from the allowlisted chat ID,
and persists polling update offsets to SQLite for crash resilience.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Optional

from .db import ActionRecord, ActionStatus, ActionType, ApprovalDB

logger = logging.getLogger("automation.telegram")


def format_approval_card(action: ActionRecord) -> str:
    """Format the mandatory standardized approval card for Telegram."""
    target_display = action.target if action.target else "Feed / New Post"
    return (
        f"LinkedIn Approval #{action.id}\n\n"
        f"Action: {action.action_type}\n"
        f"Target: {target_display}\n\n"
        f"Content:\n{action.content}\n\n"
        f"Reason:\n{action.reason}\n\n"
        f"Reply:\n"
        f"APPROVE {action.id}\n"
        f"or\n"
        f"REJECT {action.id}"
    )


class TelegramClient:
    """Adapter for sending and receiving messages via Telegram Bot API or Dry-Run simulator."""

    def __init__(
        self,
        bot_token: Optional[str] = None,
        chat_id: Optional[str | int] = None,
        dry_run: Optional[bool] = None,
    ) -> None:
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        raw_chat_id = chat_id if chat_id is not None else os.getenv("TELEGRAM_CHAT_ID", "").strip()
        self.chat_id = str(raw_chat_id).strip()

        env_dry = os.getenv("TELEGRAM_DRY_RUN", "true").lower() in ("true", "1", "yes")
        if dry_run is not None:
            self.dry_run = dry_run
        else:
            self.dry_run = env_dry or not (self.bot_token and self.chat_id)

        self.log_file = Path(__file__).resolve().parents[1] / "testing" / "telegram_dryrun.log"
        self._consecutive_poll_errors = 0

    def is_configured_for_live(self) -> bool:
        """Check if all necessary live credentials are present."""
        return bool(self.bot_token and self.chat_id)

    def send_text(self, text: str, chat_id: Optional[str | int] = None) -> dict[str, Any]:
        """Send a plain text message to the Telegram chat."""
        target_chat = str(chat_id).strip() if chat_id is not None else self.chat_id

        if self.dry_run or not self.is_configured_for_live():
            # Dry run mode: log message to disk/console
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            log_entry = (
                f"\n{'='*50}\n[TELEGRAM DRY-RUN MESSAGE TO {target_chat or 'UNCONFIGURED'}]\n"
                f"{text}\n{'='*50}\n"
            )
            try:
                with open(self.log_file, "a", encoding="utf-8") as f:
                    f.write(log_entry)
            except Exception as e:
                logger.warning(f"Could not write to dry-run log: {e}")

            logger.info(f"[TELEGRAM DRY-RUN] Message generated for {target_chat}:\n{text}")
            return {
                "status": "dry_run",
                "success": True,
                "message_id": f"dry_run_{os.urandom(4).hex()}",
                "chat_id": target_chat,
                "text": text,
            }

        # Live mode: call Telegram Bot API
        import requests

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": target_chat,
            "text": text,
        }

        try:
            resp = requests.post(url, json=payload, timeout=15.0)
            data = resp.json()
            if not data.get("ok", False):
                error_msg = data.get("description", "Unknown Telegram API error")
                logger.error(f"Telegram API error: {error_msg}")
                return {"status": "error", "success": False, "error": error_msg}

            msg_id = str(data.get("result", {}).get("message_id", "unknown"))
            return {"status": "sent", "success": True, "message_id": msg_id, "data": data}
        except Exception as e:
            logger.exception(f"Telegram network exception: {e}")
            return {"status": "error", "success": False, "error": str(e)}

    def send_approval_request(self, action: ActionRecord) -> dict[str, Any]:
        """Send the formatted approval card for a pending action."""
        card_text = format_approval_card(action)
        return self.send_text(card_text)

    def send_execution_success(self, action: ActionRecord, result_info: str = "") -> dict[str, Any]:
        """Send a notification that an action was executed/scheduled."""
        target_display = action.target if action.target else "Feed"

        if action.action_type.upper() == "POST":
            sched = None
            if action.extra_params and action.extra_params.get("scheduled_time"):
                sched = action.extra_params.get("scheduled_time")
            elif "scheduledTime" in result_info:
                try:
                    res_dict = json.loads(result_info) if isinstance(result_info, str) else result_info
                    sched = res_dict.get("scheduledTime")
                except Exception:
                    pass

            time_str = f"{sched} UTC (Publora will publish to your feed in ~2 minutes)" if sched else "Published"
            msg = (
                f"✅ LinkedIn Post #{action.id} SCHEDULED FOR PUBLICATION.\n\n"
                f"Action: POST\n"
                f"Target: {target_display}\n"
                f"Scheduled Time: {time_str}\n"
            )
            if result_info:
                msg += f"\nDetails: {result_info}\n"
            return self.send_text(msg)

        msg = (
            f"✅ LinkedIn Action #{action.id} EXECUTED successfully.\n\n"
            f"Action: {action.action_type}\n"
            f"Target: {target_display}\n"
        )
        if result_info:
            msg += f"\nDetails: {result_info}\n"
        return self.send_text(msg)

    def send_execution_failure(self, action: ActionRecord, error_message: str) -> dict[str, Any]:
        """Send a notification that an action execution failed."""
        msg = (
            f"❌ LinkedIn Action #{action.id} execution FAILED.\n\n"
            f"Action: {action.action_type}\n"
            f"Error: {error_message}\n\n"
            f"The action has been marked as FAILED in the database.\n"
            f"To retry this action, reply:\nRETRY {action.id}"
        )
        return self.send_text(msg)

    def send_rejection_notice(self, action: ActionRecord, reason: str = "") -> dict[str, Any]:
        """Send a notification that an action was rejected."""
        msg = (
            f"🛑 LinkedIn Action #{action.id} REJECTED.\n\n"
            f"Action: {action.action_type}\n"
            f"Status: REJECTED (will not be published)\n"
        )
        if reason:
            msg += f"Reason: {reason}\n"
        return self.send_text(msg)

    def process_incoming_message(
        self,
        text: str,
        sender_chat_id: str | int,
        db: ApprovalDB,
        mock_publisher: Optional[Callable[[ActionRecord], Any]] = None,
        execute_fn: Optional[Callable[[ActionRecord, bool, Optional[Callable]], Any]] = None,
    ) -> dict[str, Any]:
        """Safely process an incoming command from Telegram with strict allowlist and validation."""
        sender_str = str(sender_chat_id).strip()
        allowed_chat = str(self.chat_id).strip()

        # 1. Chat ID allowlist check
        if allowed_chat and sender_str != allowed_chat:
            logger.warning(
                f"Unauthorized Telegram chat ID {sender_str} attempted command (expected {allowed_chat}). Ignoring."
            )
            return {"status": "unauthorized", "sender": sender_str}

        trimmed = text.strip()
        approve_match = re.match(r"^APPROVE\s+(\d+)$", trimmed, re.IGNORECASE)
        reject_match = re.match(r"^REJECT\s+(\d+)(?:\s+(.*))?$", trimmed, re.IGNORECASE)
        retry_match = re.match(r"^RETRY\s+(\d+)$", trimmed, re.IGNORECASE)

        if approve_match:
            action_id = int(approve_match.group(1))
            return self._handle_approve(action_id, sender_str, db, mock_publisher, execute_fn)
        elif reject_match:
            action_id = int(reject_match.group(1))
            reason = reject_match.group(2) or "Rejected by user via Telegram"
            return self._handle_reject(action_id, reason, sender_str, db)
        elif retry_match:
            action_id = int(retry_match.group(1))
            return self._handle_retry(action_id, sender_str, db)
        elif trimmed.lower() in ("approve", "yes", "reject", "no", "post", "retry"):
            self.send_text(
                "⚠️ Command must reference the exact action ID.\n\n"
                "To approve: APPROVE <ID>\n"
                "To reject:  REJECT <ID>\n"
                "To retry:   RETRY <ID>",
                chat_id=sender_str,
            )
            return {"status": "ambiguous_command"}
        else:
            self.send_text(
                "⚠️ Unrecognized command.\n\n"
                "Commands supported:\n"
                "• APPROVE <ID>\n"
                "• REJECT <ID>\n"
                "• RETRY <ID>",
                chat_id=sender_str,
            )
            return {"status": "unrecognized_command"}

    def _handle_approve(
        self,
        action_id: int,
        sender_chat_id: str,
        db: ApprovalDB,
        mock_publisher: Optional[Callable[[ActionRecord], Any]] = None,
        execute_fn: Optional[Callable[[ActionRecord, bool, Optional[Callable]], Any]] = None,
    ) -> dict[str, Any]:
        """Execute atomic approval lock, hash verification, and publication."""
        ok, record, msg = db.acquire_approval_lock(action_id)
        if not ok or record is None:
            self.send_text(f"❌ Cannot approve Action #{action_id}: {msg}", chat_id=sender_chat_id)
            return {"status": "approval_failed", "reason": msg}

        from .webhook import execute_approved_action

        exec_func = execute_fn or execute_approved_action
        try:
            result = exec_func(record, self.dry_run, mock_publisher)
            db.mark_executed(action_id, result)
            self.send_execution_success(record, str(result))
            logger.info(f"Action #{action_id} executed successfully via Telegram approval.")
            return {"status": "executed", "action_id": action_id, "result": result}
        except Exception as e:
            logger.exception(f"Execution error on action #{action_id}: {e}")
            db.mark_failed(action_id, str(e))
            self.send_execution_failure(record, str(e))
            return {"status": "failed", "action_id": action_id, "error": str(e)}

    def _handle_reject(
        self,
        action_id: int,
        reason: str,
        sender_chat_id: str,
        db: ApprovalDB,
    ) -> dict[str, Any]:
        """Execute atomic rejection."""
        ok, record, msg = db.reject_action(action_id, reason)
        if not ok or record is None:
            self.send_text(f"❌ Cannot reject Action #{action_id}: {msg}", chat_id=sender_chat_id)
            return {"status": "rejection_failed", "reason": msg}

        self.send_rejection_notice(record, reason)
        logger.info(f"Action #{action_id} rejected successfully via Telegram: {reason}")
        return {"status": "rejected", "action_id": action_id}

    def _handle_retry(
        self,
        action_id: int,
        sender_chat_id: str,
        db: ApprovalDB,
    ) -> dict[str, Any]:
        """Reset a FAILED action to PENDING and request fresh approval. Never publishes directly."""
        ok, record, msg = db.reset_failed_action_to_pending(action_id)
        if not ok or record is None:
            self.send_text(f"❌ Cannot retry Action #{action_id}: {msg}", chat_id=sender_chat_id)
            return {"status": "retry_failed", "reason": msg}

        card = format_approval_card(record)
        notice = f"🔄 Action #{action_id} has been reset to PENDING.\n\n{card}"
        self.send_text(notice, chat_id=sender_chat_id)
        logger.info(f"Action #{action_id} reset to PENDING via RETRY. Fresh approval required.")
        return {"status": "retried_to_pending", "action_id": action_id}

    def poll_updates(
        self,
        db: ApprovalDB,
        offset: Optional[int] = None,
        timeout: int = 10,
        mock_publisher: Optional[Callable[[ActionRecord], Any]] = None,
    ) -> Optional[int]:
        """Fetch and process pending updates via Telegram Bot API with persistent offset and backoff."""
        if self.dry_run or not self.is_configured_for_live():
            return offset

        # Resume from persisted update_id if offset not in memory
        if offset is None:
            persisted = db.get_state("telegram_last_update_id")
            if persisted and persisted.isdigit():
                offset = int(persisted) + 1
                logger.info(f"Resumed Telegram polling offset from database: {offset}")

        import requests

        url = f"https://api.telegram.org/bot{self.bot_token}/getUpdates"
        params: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset

        try:
            resp = requests.get(url, params=params, timeout=timeout + 5)
            data = resp.json()
            if not data.get("ok", False):
                self._consecutive_poll_errors += 1
                return offset

            # Reset error count on successful communication
            self._consecutive_poll_errors = 0
            updates = data.get("result", [])
            latest_offset = offset

            for upd in updates:
                upd_id = upd.get("update_id")
                if upd_id is not None:
                    latest_offset = upd_id + 1
                    # Persist latest update_id immediately to SQLite
                    db.set_state("telegram_last_update_id", str(upd_id))

                msg = upd.get("message") or upd.get("channel_post")
                if msg:
                    text = msg.get("text", "")
                    sender = msg.get("chat", {}).get("id")
                    if text and sender is not None:
                        self.process_incoming_message(
                            text=text,
                            sender_chat_id=sender,
                            db=db,
                            mock_publisher=mock_publisher,
                        )
            return latest_offset
        except Exception as e:
            self._consecutive_poll_errors += 1
            backoff_secs = min(30, 2 ** min(self._consecutive_poll_errors, 5))
            logger.warning(
                f"Telegram polling network error: {e}. Backing off for {backoff_secs}s "
                f"(error #{self._consecutive_poll_errors})"
            )
            time.sleep(backoff_secs)
            return offset
