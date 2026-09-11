"""WhatsApp Business / Meta Cloud API client with dry-run support.

Formats standardized approval cards and handles outgoing notifications to WhatsApp.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

from .db import ActionRecord

logger = logging.getLogger("automation.whatsapp")


def format_approval_card(action: ActionRecord) -> str:
    """Format the mandatory standardized approval card for WhatsApp."""
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


def clean_phone_number(phone: str) -> str:
    """Strip leading +, spaces, hyphens, and parentheses from phone number."""
    return re.sub(r"[^\d]", "", phone)


class WhatsAppClient:
    """Adapter for sending messages via Meta WhatsApp Cloud API or Dry-Run simulator."""

    def __init__(
        self,
        access_token: Optional[str] = None,
        phone_number_id: Optional[str] = None,
        recipient_phone: Optional[str] = None,
        dry_run: Optional[bool] = None,
    ) -> None:
        self.access_token = access_token or os.getenv("WHATSAPP_ACCESS_TOKEN", "").strip()
        self.phone_number_id = phone_number_id or os.getenv("WHATSAPP_PHONE_NUMBER_ID", "").strip()
        raw_recipient = recipient_phone or os.getenv("WHATSAPP_RECIPIENT_PHONE", "").strip()
        self.recipient_phone = clean_phone_number(raw_recipient)

        # Dry run is True if explicitly enabled OR if credentials are missing
        env_dry = os.getenv("WHATSAPP_DRY_RUN", "true").lower() in ("true", "1", "yes")
        if dry_run is not None:
            self.dry_run = dry_run
        else:
            self.dry_run = env_dry or not (self.access_token and self.phone_number_id and self.recipient_phone)

        self.api_version = "v21.0"
        self.log_file = Path(__file__).resolve().parents[1] / "testing" / "whatsapp_dryrun.log"

    def is_configured_for_live(self) -> bool:
        """Check if all necessary live credentials are present."""
        return bool(self.access_token and self.phone_number_id and self.recipient_phone)

    def send_text(self, text: str, recipient: Optional[str] = None) -> dict[str, Any]:
        """Send a plain text message to the recipient."""
        to_phone = clean_phone_number(recipient) if recipient else self.recipient_phone

        if self.dry_run or not self.is_configured_for_live():
            # Dry run mode: log message to disk/console
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            log_entry = (
                f"\n{'='*50}\n[WHATSAPP DRY-RUN MESSAGE TO {to_phone or 'UNCONFIGURED'}]\n"
                f"{text}\n{'='*50}\n"
            )
            try:
                with open(self.log_file, "a", encoding="utf-8") as f:
                    f.write(log_entry)
            except Exception as e:
                logger.warning(f"Could not write to dry-run log: {e}")

            logger.info(f"[WHATSAPP DRY-RUN] Message generated for {to_phone}:\n{text}")
            return {
                "status": "dry_run",
                "success": True,
                "message_id": f"dry_run_{os.urandom(4).hex()}",
                "recipient": to_phone,
                "text": text,
            }

        # Live mode: call Meta WhatsApp Business Cloud API
        import requests

        url = f"https://graph.facebook.com/{self.api_version}/{self.phone_number_id}/messages"
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to_phone,
            "type": "text",
            "text": {"preview_url": False, "body": text},
        }

        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=15.0)
            data = resp.json()
            if resp.status_code >= 400:
                error_msg = data.get("error", {}).get("message", resp.text)
                logger.error(f"WhatsApp API error ({resp.status_code}): {error_msg}")
                return {"status": "error", "success": False, "error": error_msg, "code": resp.status_code}
            
            msg_id = data.get("messages", [{}])[0].get("id", "unknown")
            return {"status": "sent", "success": True, "message_id": msg_id, "data": data}
        except Exception as e:
            logger.exception(f"WhatsApp network exception: {e}")
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
                import json
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
            f"The action has been marked as FAILED in the database."
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
