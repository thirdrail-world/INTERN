"""Intern Queue Runner — Phase 3A+3B (Dry-Run + Live Execution).

Scans tickets/backlog/, classifies each as SAFE or GATED per
docs/INTERN_QUEUE_POLICY.md, and runs dry-runs or live edits
for SAFE tickets.

Default mode is dry-run. Must pass --live explicitly for live execution.

Usage:
    python -m src.intern.queue_runner --once --dry-run-only
    python -m src.intern.queue_runner --once --live
    python -m src.intern.queue_runner --once --live --verbose

INTERN-PHASE3A-001, INTERN-PHASE3B-001
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import logging
import os
import re
import subprocess
import time
from pathlib import Path

logger = logging.getLogger("kai.intern.queue_runner")

# Escalation bridge
from intern.escalate import escalate_to_claude_code

# Skip-list: persistent failure tracker
import json as _json

MAX_TICKET_FAILURES = 5
_SKIP_FILE = Path(__file__).parent.parent.parent / "var" / "intern_skip.json"

def _load_skip_list():
    try:
        if _SKIP_FILE.exists():
            return _json.loads(_SKIP_FILE.read_text())
    except Exception:
        pass
    return {}

def _save_skip_list(skip):
    _SKIP_FILE.parent.mkdir(parents=True, exist_ok=True)
    _SKIP_FILE.write_text(_json.dumps(skip, indent=2))

def record_failure(ticket_id):
    skip = _load_skip_list()
    skip[ticket_id] = skip.get(ticket_id, 0) + 1
    _save_skip_list(skip)
    return skip[ticket_id]

def should_skip(ticket_id):
    skip = _load_skip_list()
    return skip.get(ticket_id, 0) >= MAX_TICKET_FAILURES

def clear_skip(ticket_id):
    skip = _load_skip_list()
    skip.pop(ticket_id, None)
    _save_skip_list(skip)


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# ---------------------------------------------------------------------------
# Constants from INTERN_QUEUE_POLICY.md
# ---------------------------------------------------------------------------

WRITABLE_DIRS = ("tools/", "tickets/", "tests/", "docs/", "configs/", "scripts/", "data/", "src/", "config/")

PROTECTED_DIRS = (  # Minimal — only env/secrets
    # src/ directories opened for autonomous dev
    "soul/", ".env",
)

# Critical files Intern must NOT modify — these are hand-wired integrations
PROTECTED_FILES = (
    "src/gateway/app.py",       # Core gateway — RLM wiring, lane routing, persona compile
    "src/gateway/voice.py",     # Voice pipeline — RLM reflex retrieval, streaming
    "src/gateway/llm_client.py",  # LLM interface
    "src/memory/rlm.py",        # RLM recursive memory navigator
    "src/intern/queue_runner.py",  # Self — Intern must not modify itself
    "src/intern/escalate.py",  # Escalation bridge
    "src/persona/compiler_v2.py",  # Persona compiler
    "CLAUDE.md",                # Project context for Claude Code
)

BLOCKED_KEYWORDS = (
    "EnvironmentFile", "dotenv",            # env file references
    "api_key=", "api-key=",                  # key assignment (not mention)
    "api_secret", "client_secret",           # specific secret patterns
    "api_token", "auth_token", "access_token",  # specific token patterns
    "credential", "password",
    "pip install", "requirements.txt", "requirements.lock",
)

# Priority order for SAFE tickets (lower index = higher priority)
_PRIORITY_ORDER = ("tests/", "docs/", "tools/", "configs/", "scripts/", "data/")


# ---------------------------------------------------------------------------
# 1. Preflight
# ---------------------------------------------------------------------------

def preflight() -> str | None:
    """Return error string if preflight fails, None if OK."""

    # 1. Dirty-repo guard — only check tracked files (ignore untracked)
    result = subprocess.run(
        ["git", "diff", "--name-only", "HEAD"],
        capture_output=True, text=True, cwd=_REPO_ROOT,
    )
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        capture_output=True, text=True, cwd=_REPO_ROOT,
    )
    dirty = (result.stdout.strip() + "\n" + staged.stdout.strip()).strip()
    if dirty:
        return f"Dirty repo (tracked files modified): {dirty[:200]}"

    # 2. Gateway health
    try:
        import httpx
        resp = httpx.get("http://localhost:8000/health", timeout=5)
        health = resp.json()
        if health.get("status") not in ("ok", "degraded"):
            return f"Gateway unhealthy: {health.get('status')}"
    except Exception as exc:
        return f"Gateway unreachable: {exc}"

    return None


def acquire_lock() -> int | None:
    """Acquire queue runner lock file. Returns fd if acquired, None if locked."""
    lock_dir = _REPO_ROOT / "var"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / "queue_runner.lock"

    fd = os.open(str(lock_path), os.O_CREAT | os.O_WRONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.write(fd, str(os.getpid()).encode())
        return fd
    except BlockingIOError:
        os.close(fd)
        return None


def release_lock(fd: int) -> None:
    """Release queue runner lock file."""
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    except Exception:
        pass
    lock_path = _REPO_ROOT / "var" / "queue_runner.lock"
    lock_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 2. Ticket Classifier
# ---------------------------------------------------------------------------

def classify_ticket(ticket: dict) -> tuple[str, str]:
    """Return (queue, reason) — queue is 'safe' or 'gated'.

    Pure function. No LLM. Implements INTERN_QUEUE_POLICY.md.
    """
    files = ticket.get("allowed_files", [])
    body_lower = ticket.get("body", "").lower()

    # Check for verify command in ticket body
    has_verify = bool(
        re.search(r"(?:^|\n)#{0,3}\s*\*{0,2}(?:Verify|Test)\s*:?\*{0,2}\s*\n", ticket.get("body", ""), re.IGNORECASE)
    )

    if len(files) != 1:
        return ("gated", f"multi-file: {len(files)} files")

    target = files[0]

    if any(target.startswith(p) for p in PROTECTED_DIRS):
        return ("gated", f"protected directory: {target}")

    if target in PROTECTED_FILES:
        return ("gated", f"protected file: {target}")

    if not any(target.startswith(w) for w in WRITABLE_DIRS):
        return ("gated", f"not in writable dir: {target}")

    if not has_verify:
        return ("gated", "no verification command")

    if any(kw in body_lower for kw in BLOCKED_KEYWORDS):
        return ("gated", "contains blocked keyword")

    return ("safe", "all criteria met")


def _ticket_priority(ticket: dict) -> tuple[int, str]:
    """Return sort key for priority ordering."""
    files = ticket.get("allowed_files", [])
    target = files[0] if files else ""
    for idx, prefix in enumerate(_PRIORITY_ORDER):
        if target.startswith(prefix):
            return (idx, ticket.get("ticket_id", ""))
    return (len(_PRIORITY_ORDER), ticket.get("ticket_id", ""))


# ---------------------------------------------------------------------------
# 3. Ticket Scanner
# ---------------------------------------------------------------------------

def scan_backlog() -> tuple[list[dict], list[dict]]:
    """Return (safe_tickets, gated_tickets) sorted by priority."""
    from intern.agent import parse_ticket

    backlog_dir = _REPO_ROOT / "tickets" / "backlog"
    if not backlog_dir.is_dir():
        logger.warning("Backlog directory not found: %s", backlog_dir)
        return [], []

    safe = []
    gated = []

    for path in sorted(backlog_dir.glob("*.md")):
        try:
            ticket = parse_ticket(path)
        except Exception as exc:
            logger.warning("Failed to parse %s: %s", path.name, exc)
            continue

        queue, reason = classify_ticket(ticket)
        ticket["_queue"] = queue
        ticket["_reason"] = reason

        if queue == "safe":
            safe.append(ticket)
        else:
            gated.append(ticket)

    safe.sort(key=_ticket_priority)
    return safe, gated


# ---------------------------------------------------------------------------
# 4. Reporting
# ---------------------------------------------------------------------------

def report(category: str, identifier: str, action: str, summary: str = "") -> None:
    """Report to #nemo-ops via nc_report.sh. Falls back to logging."""
    script = _REPO_ROOT / "tools" / "nc_report.sh"
    if script.is_file():
        cmd = ["bash", str(script), category, identifier, action]
        if summary:
            cmd.append(summary)
        try:
            subprocess.run(cmd, capture_output=True, timeout=10, cwd=_REPO_ROOT)
        except Exception as exc:
            logger.warning("nc_report.sh failed: %s", exc)
    logger.info("[REPORT] %s %s %s %s", category, identifier, action, summary)


# ---------------------------------------------------------------------------
# 5. Shared Helpers
# ---------------------------------------------------------------------------

def resolve_planner_profile() -> dict:
    """Resolve LLM planner profile from env (same as Discord dispatch)."""
    from intern.planner import PLANNER_PROFILES
    profile_name = os.environ.get("INTERN_PROFILE", "devstral")
    profile = PLANNER_PROFILES.get(profile_name, {})
    base_url = profile.get("base_url", "")
    model = profile.get("model", "")
    api_key = ""
    if profile.get("needs_api_key"):
        api_key = os.environ.get("NVIDIA_API_KEY", "")
    return {"base_url": base_url, "model": model, "api_key": api_key}


def get_current_sha() -> str:
    """Return the current HEAD commit SHA."""
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True, text=True, cwd=_REPO_ROOT,
    )
    return result.stdout.strip()


def rollback(target_sha: str) -> bool:
    """Hard reset to a known-good commit SHA."""
    logger.warning("ROLLBACK to %s", target_sha)
    result = subprocess.run(
        ["git", "reset", "--hard", target_sha],
        capture_output=True, text=True, cwd=_REPO_ROOT,
    )
    if result.returncode == 0:
        logger.info("Rollback successful to %s", target_sha[:10])
    else:
        logger.error("Rollback FAILED: %s", result.stderr)
    return result.returncode == 0


def auto_commit(ticket_id: str, summary: str) -> bool:
    """Stage all changes and commit with standard message format.

    Returns True if commit succeeded or nothing to commit.
    """
    subprocess.run(["git", "add", "-A"], cwd=_REPO_ROOT, check=True)

    # Check if there is anything to commit
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True, text=True, cwd=_REPO_ROOT,
    )
    if not status.stdout.strip():
        logger.info("Nothing to commit (edit was a no-op)")
        return True

    msg = f"intern: {ticket_id} — {summary}"
    result = subprocess.run(
        ["git", "commit", "-m", msg],
        capture_output=True, text=True, cwd=_REPO_ROOT,
    )
    if result.returncode == 0:
        logger.info("Committed: %s", msg)
    else:
        logger.error("Commit failed: %s", result.stderr)
    return result.returncode == 0


def move_ticket_to_done(ticket_path: Path) -> None:
    """Move completed ticket from backlog/ to done/."""
    done_dir = _REPO_ROOT / "tickets" / "done"
    done_dir.mkdir(parents=True, exist_ok=True)
    dest = done_dir / ticket_path.name
    if ticket_path.is_file():
        ticket_path.rename(dest)
        logger.info("Moved ticket to done/: %s", dest.name)
        # Stage the move for the next commit or amend
        subprocess.run(["git", "add", "-A"], cwd=_REPO_ROOT)


def check_gateway_health() -> bool:
    """Lightweight post-commit health check."""
    try:
        import httpx
        resp = httpx.get("http://localhost:8000/health", timeout=5)
        return resp.json().get("status") in ("ok", "degraded")
    except Exception as exc:
        logger.warning("Post-commit health check failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# 6. Dry-Run Execution
# ---------------------------------------------------------------------------

async def dry_run_ticket(ticket: dict) -> dict:
    """Execute dry-run for a single ticket. Returns result dict."""
    from intern.agent import execute_ticket

    ticket_id = ticket["ticket_id"]
    ticket_path = Path(ticket["source_path"])
    t_start = time.monotonic()

    report("ticket", ticket_id, "started", f"Dry-run: {ticket_id}")

    profile = resolve_planner_profile()

    try:
        success = await execute_ticket(
            ticket_path,
            dry_run=True,
            base_url=profile["base_url"],
            model=profile["model"],
            api_key=profile["api_key"],
        )
        duration_ms = (time.monotonic() - t_start) * 1000

        result = {
            "success": success,
            "ticket_id": ticket_id,
            "mode": "dry_run",
            "duration_ms": round(duration_ms, 1),
            "error": None if success else "dry-run returned False",
        }

        if success:
            report("ticket", ticket_id, "completed", f"Dry-run passed ({duration_ms:.0f}ms)")
        else:
            report("ticket", ticket_id, "blocked", f"Dry-run failed ({duration_ms:.0f}ms)")

        return result

    except Exception as exc:
        duration_ms = (time.monotonic() - t_start) * 1000
        logger.error("Dry-run crashed for %s: %s", ticket_id, exc, exc_info=True)
        report("ticket", ticket_id, "blocked", f"Dry-run crashed: {exc}")
        return {
            "success": False,
            "ticket_id": ticket_id,
            "mode": "dry_run",
            "duration_ms": round(duration_ms, 1),
            "error": str(exc),
        }


# ---------------------------------------------------------------------------
# 7. Live Execution (Phase 3B)
# ---------------------------------------------------------------------------

async def live_run_ticket(ticket: dict) -> dict:
    """Execute a ticket with live edits + atomic commit.

    Follows INTERN_QUEUE_POLICY.md execution protocol:
    ANNOUNCE → LIVE-RUN → VERIFY → COMMIT → HEALTH → REPORT
    Rolls back to pre-edit SHA on any failure.
    """
    from intern.agent import execute_ticket

    ticket_id = ticket["ticket_id"]
    ticket_path = Path(ticket["source_path"])
    t_start = time.monotonic()

    # 1. Record pre-edit SHA (rollback target)
    pre_edit_sha = get_current_sha()

    # 2. Announce
    report("ticket", ticket_id, "started", f"Live-run: {ticket_id}")

    profile = resolve_planner_profile()

    try:
        # 3. Execute ticket (dry_run=False — applies edits + runs verification)
        success = await execute_ticket(
            ticket_path,
            dry_run=False,
            base_url=profile["base_url"],
            model=profile["model"],
            api_key=profile["api_key"],
        )
        duration_ms = (time.monotonic() - t_start) * 1000

        if not success:
            rollback(pre_edit_sha)
            report("ticket", ticket_id, "blocked",
                   f"Live-run failed, rolled back ({duration_ms:.0f}ms)")
            return {
                "success": False,
                "ticket_id": ticket_id,
                "mode": "live",
                "duration_ms": round(duration_ms, 1),
                "error": "execute_ticket returned False — rolled back",
            }

        # 4. Move ticket to done/ (before commit so its included)
        move_ticket_to_done(ticket_path)

        # 4. Auto-commit
        summary = ticket.get("_summary", ticket_id)
        if not auto_commit(ticket_id, summary):
            rollback(pre_edit_sha)
            report("ticket", ticket_id, "blocked",
                   f"Commit failed, rolled back ({duration_ms:.0f}ms)")
            return {
                "success": False,
                "ticket_id": ticket_id,
                "mode": "live",
                "duration_ms": round(duration_ms, 1),
                "error": "auto_commit failed — rolled back",
            }

        # 5. Post-commit health check
        if not check_gateway_health():
            logger.warning("Post-commit health check failed — rolling back")
            rollback(pre_edit_sha)
            report("ticket", ticket_id, "blocked",
                   f"Health check failed post-commit, rolled back ({duration_ms:.0f}ms)")
            return {
                "success": False,
                "ticket_id": ticket_id,
                "mode": "live",
                "duration_ms": round(duration_ms, 1),
                "error": "post-commit health check failed — rolled back",
            }

        # 7. Report success
        duration_ms = (time.monotonic() - t_start) * 1000
        report("ticket", ticket_id, "completed",
               f"Live-run passed, committed ({duration_ms:.0f}ms)")

        return {
            "success": True,
            "ticket_id": ticket_id,
            "mode": "live",
            "duration_ms": round(duration_ms, 1),
            "error": None,
        }

    except Exception as exc:
        duration_ms = (time.monotonic() - t_start) * 1000
        logger.error("Live-run crashed for %s: %s", ticket_id, exc, exc_info=True)
        rollback(pre_edit_sha)
        report("ticket", ticket_id, "blocked",
               f"Live-run crashed + rolled back: {exc}")
        return {
            "success": False,
            "ticket_id": ticket_id,
            "mode": "live",
            "duration_ms": round(duration_ms, 1),
            "error": str(exc),
        }


# ---------------------------------------------------------------------------
# 8. Queue Loop
# ---------------------------------------------------------------------------

async def run_queue(dry_run_only: bool = True, once: bool = False) -> None:
    """Main queue loop."""

    while True:
        # 1. Preflight
        preflight_error = preflight()
        if preflight_error:
            logger.error("Preflight failed: %s", preflight_error)
            report("error", "queue-runner", preflight_error)
            if once:
                return
            logger.info("Retrying in 60s...")
            await asyncio.sleep(60)
            continue

        # 2. Scan backlog
        safe_tickets, gated_tickets = scan_backlog()
        logger.info(
            "Scan complete: %d safe, %d gated tickets",
            len(safe_tickets), len(gated_tickets),
        )

        # 3. Report gated tickets as deferred
        for ticket in gated_tickets:
            report(
                "ticket",
                ticket["ticket_id"],
                "deferred",
                f"Approval required: {ticket['_reason']}",
            )

        # 4. Process safe tickets
        consecutive_failures = 0
        mode_label = "dry-run" if dry_run_only else "live"
        for ticket in safe_tickets:
            if consecutive_failures >= 5:
                report("error", "queue-runner", "Stopped: 5 consecutive failures")
                logger.error("Stopped: 5 consecutive failures")
                break

            # Skip tickets that failed too many times
            tid = ticket["ticket_id"]
            if should_skip(tid):
                logger.info("SKIP %s (failed %d+ times across runs)", tid, MAX_TICKET_FAILURES)
                continue

            if dry_run_only:
                result = await dry_run_ticket(ticket)
            else:
                result = await live_run_ticket(ticket)

            if result["success"]:
                consecutive_failures = 0
                clear_skip(ticket["ticket_id"])
                logger.info(
                    "✓ %s %s passed (%.0fms)",
                    ticket["ticket_id"], mode_label, result["duration_ms"],
                )
            else:
                consecutive_failures += 1
                fail_count = record_failure(ticket["ticket_id"])
                logger.warning(
                    "✗ %s %s failed: %s (%.0fms)",
                    ticket["ticket_id"], mode_label,
                    result.get("error", "unknown"), result["duration_ms"],
                )
                # Escalate to Claude Code when Devstral gives up
                if fail_count >= MAX_TICKET_FAILURES:
                    escalate_to_claude_code(ticket)

        # 5. Exit or loop
        if once:
            logger.info("--once flag set, exiting after single pass")
            return

        logger.info("Queue pass complete. Sleeping 60s before next scan...")
        await asyncio.sleep(60)


# ---------------------------------------------------------------------------
# 9. CLI Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Intern Queue Runner — Autonomous ticket executor",
    )
    parser.add_argument(
        "--dry-run-only", action="store_true", default=False,
        help="Only run dry-runs, never apply live edits",
    )
    parser.add_argument(
        "--live", action="store_true", default=False,
        help="Enable live execution with auto-commit (requires explicit flag)",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Single pass through backlog, then exit",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    # Safety: default to dry-run unless --live is explicitly passed
    if not args.live:
        args.dry_run_only = True

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)-35s %(levelname)-5s %(message)s",
        datefmt="%H:%M:%S",
    )

    # Acquire lock
    lock_fd = acquire_lock()
    if lock_fd is None:
        logger.error("Another queue runner is already running (lock file held)")
        return

    try:
        logger.info(
            "Intern Queue Runner starting (mode=%s, once=%s)",
            "live" if args.live else "dry-run-only", args.once,
        )
        asyncio.run(run_queue(dry_run_only=args.dry_run_only, once=args.once))
    finally:
        release_lock(lock_fd)
        logger.info("Intern Queue Runner stopped")


if __name__ == "__main__":
    main()
