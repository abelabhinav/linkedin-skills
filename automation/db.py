"""Persistent SQLite approval queue for autonomous LinkedIn actions.

Provides ACID transactions, atomic single-use state transitions, content hash validation,
WAL mode concurrency, system state persistence, and restart recovery.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional


class ActionStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ActionType(str, Enum):
    POST = "POST"
    COMMENT = "COMMENT"
    REPLY = "REPLY"
    REACT = "REACT"
    RESHARE = "RESHARE"


def compute_content_hash(action_type: str, target: str, content: str) -> str:
    """Compute an immutable SHA-256 hash for the action payload."""
    payload = f"{action_type.upper().strip()}:{target.strip()}:{content.strip()}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class ActionRecord:
    id: int
    action_type: str
    target: str
    content: str
    reason: str
    content_hash: str
    extra_params: dict[str, Any]
    status: str
    created_at: str
    approved_at: Optional[str] = None
    executed_at: Optional[str] = None
    rejection_reason: Optional[str] = None
    execution_result: Optional[str] = None
    whatsapp_message_id: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ApprovalDB:
    """Thread-safe SQLite storage for LinkedIn action approvals with WAL concurrency."""

    def __init__(self, db_path: Optional[Path | str] = None) -> None:
        if db_path is None:
            env_path = os.getenv("SQLITE_DB_PATH", "").strip()
            if env_path:
                db_path = env_path
            else:
                root = Path(__file__).resolve().parents[1]
                data_dir = root / "testing" / "data"
                data_dir.mkdir(parents=True, exist_ok=True)
                db_path = data_dir / "approval_queue.sqlite3"
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        # Enable Write-Ahead Logging and 5000ms busy timeout for safe concurrency
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn

    def _init_schema(self) -> None:
        with self._get_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action_type TEXT NOT NULL,
                    target TEXT NOT NULL,
                    content TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    extra_params TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    created_at TEXT NOT NULL,
                    approved_at TEXT,
                    executed_at TEXT,
                    rejection_reason TEXT,
                    execution_result TEXT,
                    whatsapp_message_id TEXT
                );
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_actions_status ON actions(status);
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_actions_hash ON actions(content_hash);
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS system_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS topic_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    topic_slug TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    enqueued_at TEXT NOT NULL
                );
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_topic_history_slug ON topic_history(topic_slug);
                """
            )
            conn.commit()

    def _row_to_record(self, row: sqlite3.Row) -> ActionRecord:
        extra = {}
        if row["extra_params"]:
            try:
                extra = json.loads(row["extra_params"])
            except Exception:
                extra = {}
        return ActionRecord(
            id=row["id"],
            action_type=row["action_type"],
            target=row["target"],
            content=row["content"],
            reason=row["reason"],
            content_hash=row["content_hash"],
            extra_params=extra,
            status=row["status"],
            created_at=row["created_at"],
            approved_at=row["approved_at"],
            executed_at=row["executed_at"],
            rejection_reason=row["rejection_reason"],
            execution_result=row["execution_result"],
            whatsapp_message_id=row["whatsapp_message_id"],
        )

    def enqueue_action(
        self,
        action_type: str | ActionType,
        target: str,
        content: str,
        reason: str,
        extra_params: Optional[dict[str, Any]] = None,
    ) -> ActionRecord:
        """Create a new pending action in the database."""
        action_type_str = action_type.value if isinstance(action_type, ActionType) else str(action_type).upper()
        target = target.strip()
        content = content.strip()
        reason = reason.strip()
        content_hash = compute_content_hash(action_type_str, target, content)
        extra_json = json.dumps(extra_params or {})
        now_utc = datetime.now(timezone.utc).isoformat()

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO actions (
                    action_type, target, content, reason, content_hash,
                    extra_params, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    action_type_str,
                    target,
                    content,
                    reason,
                    content_hash,
                    extra_json,
                    ActionStatus.PENDING.value,
                    now_utc,
                ),
            )
            conn.commit()
            action_id = cursor.lastrowid
            assert action_id is not None
            record = self.get_action(action_id)
            assert record is not None
            return record

    def get_action(self, action_id: int) -> Optional[ActionRecord]:
        """Fetch an action by its unique ID."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM actions WHERE id = ?", (action_id,))
            row = cursor.fetchone()
            if row is None:
                return None
            return self._row_to_record(row)

    def list_actions(
        self,
        status: Optional[str | ActionStatus] = None,
        limit: int = 50,
    ) -> list[ActionRecord]:
        """List actions optionally filtered by status."""
        query = "SELECT * FROM actions"
        params: list[Any] = []
        if status:
            status_val = status.value if isinstance(status, ActionStatus) else str(status)
            query += " WHERE status = ?"
            params.append(status_val)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            rows = cursor.fetchall()
            return [self._row_to_record(r) for r in rows]

    def acquire_approval_lock(
        self,
        action_id: int,
        expected_hash: Optional[str] = None,
    ) -> tuple[bool, Optional[ActionRecord], str]:
        """Atomically transition a PENDING action to APPROVED."""
        now_utc = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM actions WHERE id = ?", (action_id,))
            row = cursor.fetchone()
            if row is None:
                return False, None, f"Action #{action_id} not found"

            record = self._row_to_record(row)
            if record.status != ActionStatus.PENDING.value:
                return (
                    False,
                    record,
                    f"Action #{action_id} is already in state '{record.status}', cannot approve",
                )

            live_hash = compute_content_hash(record.action_type, record.target, record.content)
            if live_hash != record.content_hash:
                return (
                    False,
                    record,
                    f"Content integrity failed: stored hash {record.content_hash} != live {live_hash}",
                )

            if expected_hash and expected_hash != record.content_hash:
                return (
                    False,
                    record,
                    f"Hash mismatch: expected {expected_hash}, got {record.content_hash}",
                )

            cursor.execute(
                """
                UPDATE actions
                SET status = ?, approved_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    ActionStatus.APPROVED.value,
                    now_utc,
                    action_id,
                    ActionStatus.PENDING.value,
                ),
            )
            if cursor.rowcount == 0:
                conn.rollback()
                return (
                    False,
                    record,
                    f"Concurrency conflict: Action #{action_id} status changed concurrently",
                )
            conn.commit()
            updated = self.get_action(action_id)
            return True, updated, "Approval acquired"

    def mark_executed(self, action_id: int, execution_result: Any) -> Optional[ActionRecord]:
        """Mark an approved action as EXECUTED with result details."""
        now_utc = datetime.now(timezone.utc).isoformat()
        res_str = json.dumps(execution_result) if not isinstance(execution_result, str) else execution_result
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE actions
                SET status = ?, executed_at = ?, execution_result = ?
                WHERE id = ?
                """,
                (ActionStatus.EXECUTED.value, now_utc, res_str, action_id),
            )
            conn.commit()
            return self.get_action(action_id)

    def mark_failed(self, action_id: int, error_message: str) -> Optional[ActionRecord]:
        """Mark an action as FAILED with error message."""
        now_utc = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE actions
                SET status = ?, executed_at = ?, execution_result = ?
                WHERE id = ?
                """,
                (ActionStatus.FAILED.value, now_utc, error_message, action_id),
            )
            conn.commit()
            return self.get_action(action_id)

    def reject_action(
        self,
        action_id: int,
        reason: Optional[str] = None,
    ) -> tuple[bool, Optional[ActionRecord], str]:
        """Atomically transition a PENDING action to REJECTED."""
        now_utc = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM actions WHERE id = ?", (action_id,))
            row = cursor.fetchone()
            if row is None:
                return False, None, f"Action #{action_id} not found"

            record = self._row_to_record(row)
            if record.status != ActionStatus.PENDING.value:
                return (
                    False,
                    record,
                    f"Action #{action_id} is already in state '{record.status}', cannot reject",
                )

            cursor.execute(
                """
                UPDATE actions
                SET status = ?, rejection_reason = ?, executed_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    ActionStatus.REJECTED.value,
                    reason or "Rejected by user",
                    now_utc,
                    action_id,
                    ActionStatus.PENDING.value,
                ),
            )
            if cursor.rowcount == 0:
                conn.rollback()
                return (
                    False,
                    record,
                    f"Concurrency conflict: Action #{action_id} status changed concurrently",
                )
            conn.commit()
            updated = self.get_action(action_id)
            return True, updated, "Action rejected"

    def reset_failed_action_to_pending(
        self,
        action_id: int,
    ) -> tuple[bool, Optional[ActionRecord], str]:
        """Reset a FAILED action back to PENDING for re-approval.

        Crucial rule: Only FAILED actions can be reset to PENDING.
        EXECUTED, REJECTED, and CANCELLED actions cannot be reset.
        Resetting requires a FRESH explicit approval before any execution.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM actions WHERE id = ?", (action_id,))
            row = cursor.fetchone()
            if row is None:
                return False, None, f"Action #{action_id} not found"

            record = self._row_to_record(row)
            if record.status != ActionStatus.FAILED.value:
                return (
                    False,
                    record,
                    f"Cannot retry Action #{action_id} because it is in state '{record.status}'. "
                    f"Only FAILED actions can be retried.",
                )

            cursor.execute(
                """
                UPDATE actions
                SET status = ?, approved_at = NULL, executed_at = NULL,
                    execution_result = NULL, rejection_reason = NULL
                WHERE id = ? AND status = ?
                """,
                (
                    ActionStatus.PENDING.value,
                    action_id,
                    ActionStatus.FAILED.value,
                ),
            )
            if cursor.rowcount == 0:
                conn.rollback()
                return (
                    False,
                    record,
                    f"Concurrency conflict: Action #{action_id} state changed",
                )
            conn.commit()
            updated = self.get_action(action_id)
            return True, updated, "Action reset to PENDING. Approval required."

    def update_whatsapp_message_id(self, action_id: int, message_id: str) -> None:
        """Store the external WhatsApp message ID for tracking."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE actions SET whatsapp_message_id = ? WHERE id = ?",
                (message_id, action_id),
            )
            conn.commit()

    def get_stats(self) -> dict[str, int]:
        """Return counts of actions grouped by status."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT status, count(*) as cnt FROM actions GROUP BY status")
            rows = cursor.fetchall()
            stats = {s.value: 0 for s in ActionStatus}
            for row in rows:
                stats[row["status"]] = row["cnt"]
            return stats

    # ---- System State & Telegram Offset Persistence -----------------------

    def get_state(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Get a persistent key-value configuration or state value."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM system_state WHERE key = ?", (key,))
            row = cursor.fetchone()
            if row is None:
                return default
            return row["value"]

    def set_state(self, key: str, value: str) -> None:
        """Set a persistent key-value configuration or state value."""
        now_utc = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO system_state (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, str(value), now_utc),
            )
            conn.commit()

    # ---- Topic History & Rotation -----------------------------------------

    def record_topic_usage(self, topic_slug: str, content_hash: str) -> None:
        """Record topic usage to prevent repeated generation."""
        now_utc = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO topic_history (topic_slug, content_hash, enqueued_at)
                VALUES (?, ?, ?)
                """,
                (topic_slug, content_hash, now_utc),
            )
            conn.commit()

    def get_recent_topic_slugs(self, limit: int = 20) -> list[str]:
        """Fetch list of recently used topic slugs ordered by most recent."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT topic_slug FROM topic_history ORDER BY id DESC LIMIT ?",
                (limit,),
            )
            rows = cursor.fetchall()
            return [r["topic_slug"] for r in rows]
