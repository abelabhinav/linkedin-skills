"""Autonomous background automation engine for LinkedIn research, drafting, auditing, and enqueuing.

Generates authentic, high-value posts and engagement suggestions aligned with the user's
genuine profile (student learning & building in full-stack, AI, cloud, software engineering).
Audits drafts against 2026 voice heuristics, uses persistent topic rotation, and enqueues to SQLite for Telegram approval.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .auditor import audit_draft
from .db import ActionRecord, ActionStatus, ActionType, ApprovalDB, compute_content_hash
from .telegram import TelegramClient
from .whatsapp import WhatsAppClient

logger = logging.getLogger("automation.engine")


# Authentic topics reflecting the user's confirmed background with unique slugs
AUTHENTIC_TOPICS = [
    {
        "slug": "fullstack-state-boundaries",
        "title": "Full-Stack State Management & Architecture",
        "content": (
            "Building full-stack apps quickly teaches you where state belongs.\n\n"
            "Early on, it's tempting to keep everything in the frontend state or trigger re-renders on every update. "
            "Once data flows get slightly more complex, pushing state to the server boundary or database simplifies the entire system.\n\n"
            "The hardest part of full-stack engineering isn't writing components—it's deciding who owns the source of truth.\n\n"
            "How do you usually structure data flow between client and backend in your projects?"
        ),
        "reason": "Technical reflection on full-stack architecture lessons and state boundaries.",
    },
    {
        "slug": "cloud-infrastructure-fundamentals",
        "title": "Cloud Deployments & Infrastructure Basics",
        "content": (
            "Learning cloud services is 20% reading documentation and 80% troubleshooting permissions and networking.\n\n"
            "When starting out, configuring IAM roles, environment variables, and webhook security feels tedious compared to writing code. "
            "But understanding how applications communicate securely in the cloud is what turns a local script into reliable software.\n\n"
            "Getting the infrastructure and secrets management right from day one saves hours of debugging later.\n\n"
            "What was the most unintuitive cloud concept when you first started deploying?"
        ),
        "reason": "Practical engineering insight on cloud permissions, networking, and deployment fundamentals.",
    },
    {
        "slug": "ai-deterministic-guardrails",
        "title": "AI Integration vs Traditional Software Logic",
        "content": (
            "Integrating AI models into an application requires a mindset shift.\n\n"
            "Traditional software is deterministic—given input X, you expect output Y every time. "
            "With LLMs and generative APIs, you have to design around probabilistic outputs, schema validation, and fallback mechanisms.\n\n"
            "The real engineering effort isn't prompting the model; it's building guardrails, structured parsing, and error recovery around it.\n\n"
            "If you're building with AI APIs, what has been your best pattern for handling schema validation?"
        ),
        "reason": "Insight on engineering deterministic guardrails around non-deterministic AI APIs.",
    },
    {
        "slug": "student-build-log-public",
        "title": "Student Build-Log: The Value of Documenting in Public",
        "content": (
            "Documenting what you learn while building does two things:\n\n"
            "1. It forces you to understand a concept well enough to explain it clearly.\n"
            "2. It creates a searchable record of problems you've already solved.\n\n"
            "You don't need years of industry experience to share useful takeaways. "
            "Often, the explanation from someone who just struggled through a concept is clearer than the textbook definition.\n\n"
            "I'm keeping this page focused on practical lessons from building software from scratch.\n\n"
            "What concept did you recently learn that clicked after building it yourself?"
        ),
        "reason": "Foundational post on building in public and learning by doing as a student engineer.",
    },
]


class AutomationEngine:
    """Orchestrates research, drafting, auditing, and Telegram / WhatsApp approval requests."""

    def __init__(
        self,
        db: Optional[ApprovalDB] = None,
        telegram: Optional[TelegramClient] = None,
        whatsapp: Optional[WhatsAppClient] = None,
    ) -> None:
        self.db = db or ApprovalDB()
        self.telegram = telegram or TelegramClient()
        self.whatsapp = whatsapp or WhatsAppClient()

    def is_paused(self) -> bool:
        """Check if background automation is currently paused."""
        val = os.getenv("AUTOMATION_PAUSED", "false").strip().lower()
        return val in ("true", "1", "yes")

    def _select_next_topic(self) -> Optional[dict[str, str]]:
        """Select the next topic using persistent rotation history to avoid near-duplicate posts."""
        recent_slugs = self.db.get_recent_topic_slugs(limit=len(AUTHENTIC_TOPICS))
        
        # Find topics not in recent history
        for topic in AUTHENTIC_TOPICS:
            if topic["slug"] not in recent_slugs:
                return topic

        # If all topics were used, pick the least recently used (the one at the tail of recent_slugs)
        if recent_slugs:
            lru_slug = recent_slugs[-1]
            for topic in AUTHENTIC_TOPICS:
                if topic["slug"] == lru_slug:
                    return topic

        return AUTHENTIC_TOPICS[0] if AUTHENTIC_TOPICS else None

    def run_cycle(self) -> list[ActionRecord]:
        """Run a single autonomous research and drafting cycle.

        Checks rate limits, selects the next rotated topic, audits draft,
        enqueues to SQLite, records topic usage, and sends approval card to Telegram.
        """
        if self.is_paused():
            logger.info("Automation engine is PAUSED. Skipping cycle.")
            return []

        # Rate limiting check: max 3 pending actions in queue
        pending_actions = self.db.list_actions(status=ActionStatus.PENDING, limit=10)
        if len(pending_actions) >= 3:
            logger.info(
                f"Approval queue has {len(pending_actions)} pending actions. "
                "Waiting for user to approve/reject existing items before generating new ones."
            )
            return []

        # Topic rotation & duplicate prevention
        topic = self._select_next_topic()
        if not topic:
            logger.warning("No topic available for cycle.")
            return []

        content = topic["content"]
        reason = topic["reason"]
        target = "https://www.linkedin.com/feed/"
        topic_slug = topic["slug"]

        # Prevent duplicate content enqueuing
        target_hash = compute_content_hash(ActionType.POST.value, target, content)
        for act in pending_actions:
            if act.content_hash == target_hash:
                logger.info(f"Topic '{topic_slug}' already pending as Action #{act.id}. Skipping duplicate.")
                return []

        # Audit draft
        audit = audit_draft(content, kind="POST")
        if not audit.passed:
            logger.warning(f"Draft failed audit: {audit.issues}. Skipping.")
            return []

        # Enqueue as PENDING
        record = self.db.enqueue_action(
            action_type=ActionType.POST,
            target=target,
            content=content,
            reason=reason,
            extra_params={"scheduled_time": None, "topic_slug": topic_slug},
        )
        self.db.record_topic_usage(topic_slug, record.content_hash)
        logger.info(f"Enqueued new pending action #{record.id} ({record.action_type}) [slug: {topic_slug}]")

        # Dispatch approval card to Telegram (primary approval channel)
        res_tg = self.telegram.send_approval_request(record)
        logger.info(f"Dispatched approval card for action #{record.id} to Telegram (status: {res_tg.get('status')})")

        # If WhatsApp is explicitly configured and not dry-run, also send notice
        if self.whatsapp.is_configured_for_live() and not self.whatsapp.dry_run:
            res_wa = self.whatsapp.send_approval_request(record)
            if res_wa.get("message_id"):
                self.db.update_whatsapp_message_id(record.id, res_wa["message_id"])

        return [record]
