"""CLI management utility for the autonomous LinkedIn approval system.

Usage:
    python -m automation.manage status
    python -m automation.manage list [--status PENDING|APPROVED|REJECTED|EXECUTED]
    python -m automation.manage show <ID>
    python -m automation.manage approve <ID>
    python -m automation.manage reject <ID> [--reason REASON]
    python -m automation.manage run-once
    python -m automation.manage serve [--port 8080] [--interval-minutes 180] [--enable-polling]
    python -m automation.manage poll [--timeout 10]
    python -m automation.manage validate [--live]
    python -m automation.manage pause
    python -m automation.manage resume
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

# Ensure repo root is on sys.path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from lib._env import load_env

load_env()

from automation.db import ActionStatus, ActionType, ApprovalDB
from automation.engine import AutomationEngine
from automation.telegram import TelegramClient
from automation.webhook import execute_approved_action, make_webhook_server
from automation.whatsapp import WhatsAppClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("automation.manage")

shutdown_event = threading.Event()


def handle_shutdown_signal(signum: int, frame: Any) -> None:
    sig_name = signal.Signals(signum).name if hasattr(signal, "Signals") else str(signum)
    logger.info(f"Received termination signal ({sig_name}). Initiating graceful shutdown...")
    shutdown_event.set()


def validate_production_config(live_mode: bool = False, raise_on_error: bool = False) -> list[str]:
    """Validate environment variables for production live execution without exposing secrets."""
    errors: list[str] = []

    pub_key = os.getenv("PUBLORA_API_KEY", "").strip()
    if not pub_key:
        errors.append("PUBLORA_API_KEY is missing or empty.")

    plat_id = os.getenv("LINKEDIN_PLATFORM_ID", "").strip()
    if not plat_id:
        errors.append("LINKEDIN_PLATFORM_ID is missing or empty.")

    tg_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not tg_token:
        errors.append("TELEGRAM_BOT_TOKEN is missing or empty.")
    elif ":" not in tg_token:
        errors.append("TELEGRAM_BOT_TOKEN format is invalid (expected <bot_id>:<token>).")

    tg_chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not tg_chat:
        errors.append("TELEGRAM_CHAT_ID is missing or empty.")

    tg_dry = os.getenv("TELEGRAM_DRY_RUN", "true").lower() in ("true", "1", "yes")
    if live_mode and tg_dry:
        errors.append("TELEGRAM_DRY_RUN is set to true (must be false for live production mode).")

    if errors and raise_on_error:
        raise RuntimeError("Production configuration validation failed:\n" + "\n".join(f" - {e}" for e in errors))

    return errors


def cmd_validate(args: argparse.Namespace) -> int:
    print("\n" + "=" * 50)
    print("   LinkedIn Automation Production Config Check")
    print("=" * 50)
    errors = validate_production_config(live_mode=args.live)
    if not errors:
        print("✅ Production configuration is VALID and ready for live operation.")
        print(f"• Mode: {'LIVE (Real publication enabled)' if args.live else 'TEST / DRY-RUN'}")
        print("=" * 50 + "\n")
        return 0
    else:
        print("❌ Configuration issues detected:")
        for err in errors:
            print(f"  • {err}")
        print("=" * 50 + "\n")
        return 1


def cmd_status(args: argparse.Namespace) -> int:
    db = ApprovalDB()
    stats = db.get_stats()
    paused = os.getenv("AUTOMATION_PAUSED", "false").lower() in ("true", "1", "yes")

    telegram = TelegramClient()
    whatsapp = WhatsAppClient()

    print("\n" + "=" * 45)
    print("   LinkedIn Autonomous Automation Status")
    print("=" * 45)
    print(f"Automation Engine State: {'[PAUSED]' if paused else '[RUNNING]'}")
    print(f"Primary Channel:         Telegram ({'DRY-RUN' if telegram.dry_run else 'LIVE'})")
    print(f"Secondary Channel:       WhatsApp ({'DRY-RUN' if whatsapp.dry_run else 'LIVE'})")
    print(f"Database File:           {db.db_path}")
    print("-" * 45)
    for status, count in stats.items():
        icon = {
            "PENDING": "[PENDING] ",
            "APPROVED": "[APPROVED]",
            "EXECUTED": "[EXECUTED]",
            "REJECTED": "[REJECTED]",
            "FAILED": "[FAILED]  ",
            "CANCELLED": "[CANCELED]",
        }.get(status, "[INFO]    ")
        print(f"  {icon} {status:<10} : {count}")
    print("=" * 45 + "\n")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    db = ApprovalDB()
    status_filter = args.status.upper() if args.status else None
    actions = db.list_actions(status=status_filter, limit=args.limit)

    if not actions:
        print(f"No actions found{' with status ' + status_filter if status_filter else ''}.")
        return 0

    print(f"\nFound {len(actions)} action(s):\n")
    for act in actions:
        preview = act.content.replace("\n", " ")[:60]
        print(f"#{act.id:<4} [{act.status:<8}] {act.action_type:<7} | {preview}...")
    print()
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    db = ApprovalDB()
    act = db.get_action(args.id)
    if not act:
        print(f"Action #{args.id} not found.")
        return 1

    print("\n" + "=" * 50)
    print(f"Action #{act.id} Details")
    print("=" * 50)
    print(f"Type:        {act.action_type}")
    print(f"Status:      {act.status}")
    print(f"Target:      {act.target}")
    print(f"Created At:  {act.created_at}")
    print(f"Approved At: {act.approved_at or 'N/A'}")
    print(f"Executed At: {act.executed_at or 'N/A'}")
    print(f"Hash:        {act.content_hash}")
    print(f"Reason:      {act.reason}")
    print("-" * 50)
    print("Content:")
    print(act.content)
    if act.rejection_reason:
        print(f"\nRejection Reason: {act.rejection_reason}")
    if act.execution_result:
        print(f"\nExecution Result: {act.execution_result}")
    print("=" * 50 + "\n")
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    db = ApprovalDB()
    telegram = TelegramClient()
    ok, record, msg = db.acquire_approval_lock(args.id)
    if not ok or record is None:
        print(f"❌ Cannot approve Action #{args.id}: {msg}")
        return 1

    print(f"👍 Action #{args.id} locked in APPROVED state. Executing...")
    try:
        result = execute_approved_action(record, dry_run=telegram.dry_run)
        db.mark_executed(args.id, result)
        telegram.send_execution_success(record, str(result))
        print(f"✅ Action #{args.id} executed successfully: {result}")
        return 0
    except Exception as e:
        db.mark_failed(args.id, str(e))
        telegram.send_execution_failure(record, str(e))
        print(f"❌ Execution failed for Action #{args.id}: {e}")
        return 1


def cmd_reject(args: argparse.Namespace) -> int:
    db = ApprovalDB()
    telegram = TelegramClient()
    reason = args.reason or "Rejected via CLI"
    ok, record, msg = db.reject_action(args.id, reason)
    if not ok or record is None:
        print(f"❌ Cannot reject Action #{args.id}: {msg}")
        return 1

    telegram.send_rejection_notice(record, reason)
    print(f"🛑 Action #{args.id} marked as REJECTED.")
    return 0


def cmd_run_once(args: argparse.Namespace) -> int:
    engine = AutomationEngine()
    created = engine.run_cycle()
    if created:
        print(f"✅ Research & drafting cycle complete. Created pending action #{created[0].id}.")
    else:
        print("ℹ Cycle complete. No new action was enqueued (paused, queue full, or rate limited).")
    return 0


def cmd_poll(args: argparse.Namespace) -> int:
    """Run Telegram Bot API long-polling loop with supervision and graceful shutdown."""
    db = ApprovalDB()
    telegram = TelegramClient()

    if telegram.dry_run or not telegram.is_configured_for_live():
        print("ℹ Telegram credentials not configured or TELEGRAM_DRY_RUN=true.")
        print("To enable live Telegram polling, set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env.")
        return 0

    print("🤖 Starting Telegram polling for approvals... (Press Ctrl+C to stop)")
    offset = None

    signal.signal(signal.SIGINT, handle_shutdown_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handle_shutdown_signal)

    while not shutdown_event.is_set():
        try:
            offset = telegram.poll_updates(db=db, offset=offset, timeout=args.timeout)
            time.sleep(1)
        except Exception as e:
            logger.error(f"Supervised poll error: {e}")
            time.sleep(5)

    print("\n🛑 Polling loop terminated gracefully.")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    port = args.port or int(os.getenv("WHATSAPP_WEBHOOK_PORT", "8080"))
    host = "0.0.0.0"
    db = ApprovalDB()
    telegram = TelegramClient()
    whatsapp = WhatsAppClient()
    engine = AutomationEngine(db=db, telegram=telegram, whatsapp=whatsapp)

    # Register signals for graceful shutdown
    signal.signal(signal.SIGINT, handle_shutdown_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handle_shutdown_signal)

    server = make_webhook_server(host=host, port=port, db=db, telegram=telegram, whatsapp=whatsapp)
    print(f"🚀 Approval Server listening on http://{host}:{port} (Healthcheck: /health)")
    print(f"📱 Telegram Mode: {'DRY_RUN (Simulated)' if telegram.dry_run else 'LIVE (Bot API)'}")
    print(f"📱 WhatsApp Mode: {'DRY_RUN (Simulated)' if whatsapp.dry_run else 'LIVE (Meta API)'}")
    print("Press Ctrl+C to stop.\n")

    # Supervised background scheduler thread
    def supervised_scheduler_loop():
        interval_secs = (args.interval_minutes or 180) * 60
        while not shutdown_event.is_set():
            # Wait with 1s intervals to respond immediately to shutdown_event
            for _ in range(interval_secs):
                if shutdown_event.is_set():
                    return
                time.sleep(1)
            try:
                if not shutdown_event.is_set():
                    logger.info("Running supervised background opportunity discovery...")
                    engine.run_cycle()
            except Exception as e:
                logger.error(f"Supervised scheduler loop error: {e}")

    sched_thread = threading.Thread(target=supervised_scheduler_loop, daemon=True)
    sched_thread.start()

    # Supervised background telegram polling thread
    if args.enable_polling and telegram.is_configured_for_live():
        def supervised_telegram_poll_loop():
            offset = None
            while not shutdown_event.is_set():
                try:
                    offset = telegram.poll_updates(db=db, offset=offset, timeout=10)
                    time.sleep(1)
                except Exception as e:
                    logger.error(f"Supervised telegram polling loop error: {e}")
                    time.sleep(5)

        tg_thread = threading.Thread(target=supervised_telegram_poll_loop, daemon=True)
        tg_thread.start()

    # Serve loop with timeout to allow graceful shutdown
    server.timeout = 1.0
    while not shutdown_event.is_set():
        try:
            server.handle_request()
        except Exception as e:
            if not shutdown_event.is_set():
                logger.error(f"HTTP Server handle error: {e}")

    print("\n🛑 Shutting down HTTP server cleanly...")
    server.server_close()
    print("✅ Service terminated gracefully.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Autonomous LinkedIn Automation Manager")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # status
    subparsers.add_parser("status", help="Show system status and action counts")

    # validate
    p_val = subparsers.add_parser("validate", help="Validate production configuration")
    p_val.add_argument("--live", action="store_true", help="Validate for live mode (checks TELEGRAM_DRY_RUN=false)")

    # list
    p_list = subparsers.add_parser("list", help="List actions in queue")
    p_list.add_argument("--status", choices=["PENDING", "APPROVED", "REJECTED", "EXECUTED", "FAILED", "CANCELLED"])
    p_list.add_argument("--limit", type=int, default=50)

    # show
    p_show = subparsers.add_parser("show", help="Show details of a specific action")
    p_show.add_argument("id", type=int, help="Action ID")

    # approve
    p_app = subparsers.add_parser("approve", help="Approve and execute a pending action")
    p_app.add_argument("id", type=int, help="Action ID to approve")

    # reject
    p_rej = subparsers.add_parser("reject", help="Reject a pending action")
    p_rej.add_argument("id", type=int, help="Action ID to reject")
    p_rej.add_argument("--reason", default="Rejected via CLI", help="Rejection reason")

    # run-once
    subparsers.add_parser("run-once", help="Run a single research & drafting cycle")

    # poll
    p_poll = subparsers.add_parser("poll", help="Run Telegram long-polling loop")
    p_poll.add_argument("--timeout", type=int, default=10)

    # serve
    p_serve = subparsers.add_parser("serve", help="Run webhook server and background scheduler")
    p_serve.add_argument("--port", type=int, default=8080)
    p_serve.add_argument("--interval-minutes", type=int, default=180)
    p_serve.add_argument("--enable-polling", action="store_true", help="Enable concurrent Telegram polling in background")

    args = parser.parse_args()

    commands = {
        "status": cmd_status,
        "validate": cmd_validate,
        "list": cmd_list,
        "show": cmd_show,
        "approve": cmd_approve,
        "reject": cmd_reject,
        "run-once": cmd_run_once,
        "poll": cmd_poll,
        "serve": cmd_serve,
    }
    return commands[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
