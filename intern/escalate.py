"""Intern → Claude Code escalation bridge.

When Devstral fails a ticket MAX_TICKET_FAILURES times, this module
hands the ticket off to Claude Code via:
  1. A formatted instruction file written to Claude Code's Discord inbox
  2. A Discord webhook notification to #nemo-ops for audit trail

Ticket classification determines execution policy:
  - docs/, scripts/ targets → AUTO-EXECUTE (Claude Code runs immediately)
  - src/, tests/, config/, tools/ targets → NEEDS APPROVAL (posts for Kamil to approve)
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from pathlib import Path

logger = logging.getLogger("kai.intern.escalate")

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_CC_INBOX = Path.home() / ".claude" / "channels" / "discord" / "inbox"
_ESCALATED_DIR = _REPO_ROOT / "tickets" / "escalated"

AUTO_EXECUTE_DIRS = ("docs/", "scripts/")
_MAX_FAILURES = 5


def classify_risk(ticket: dict) -> str:
    files = ticket.get("allowed_files", [])
    if isinstance(files, str):
        try:
            files = json.loads(files)
        except (json.JSONDecodeError, TypeError):
            files = [files]
    for f in files:
        if any(f.startswith(d) for d in AUTO_EXECUTE_DIRS):
            continue
        return "approval"
    return "auto" if files else "approval"


def _format_instructions(ticket: dict, risk: str) -> str:
    tid = ticket.get("ticket_id", "UNKNOWN")
    body = ticket.get("body", "")
    files = ticket.get("allowed_files", [])
    if isinstance(files, str):
        try:
            files = json.loads(files)
        except (json.JSONDecodeError, TypeError):
            files = [files]

    verify_cmd = ""
    lines = body.split("\n")
    for i, line in enumerate(lines):
        if line.strip().lower().startswith("## verify"):
            for j in range(i + 1, min(i + 3, len(lines))):
                cmd_line = lines[j].strip().strip("`")
                if cmd_line:
                    verify_cmd = cmd_line
                    break
            break

    policy = "AUTO-EXECUTE" if risk == "auto" else "NEEDS APPROVAL — wait for Kamil to confirm before executing"

    return f"""[Intern Escalation] Ticket {tid} failed {_MAX_FAILURES}x with Devstral — escalating to you.

POLICY: {policy}
WORKING DIRECTORY: /opt/kai/stack
TARGET FILES: {', '.join(files) if files else 'see ticket body'}
VERIFY COMMAND: {verify_cmd or 'see ticket body'}

--- TICKET CONTENT ---
{body}
--- END TICKET ---

Instructions:
1. Read the ticket above carefully
2. cd /opt/kai/stack
3. Make the changes described in the ticket to the target file(s)
4. Run the verify command to confirm it works
5. If verify passes: git add -A && git commit -m "claude-code: {tid}"
6. Move the ticket: mv tickets/escalated/{tid}.md tickets/done/
"""


def _write_to_inbox(ticket: dict, risk: str) -> Path | None:
    if not _CC_INBOX.is_dir():
        logger.warning("Claude Code inbox not found at %s", _CC_INBOX)
        return None
    instructions = _format_instructions(ticket, risk)
    ts_ms = int(time.time() * 1000)
    tid = ticket.get("ticket_id", "UNKNOWN")
    filename = f"{ts_ms}-intern-{tid}.txt"
    path = _CC_INBOX / filename
    try:
        path.write_text(instructions)
        logger.info("Wrote escalation to Claude Code inbox: %s", filename)
        return path
    except Exception as exc:
        logger.error("Failed to write to CC inbox: %s", exc)
        return None


def _move_to_escalated(ticket: dict) -> Path | None:
    _ESCALATED_DIR.mkdir(parents=True, exist_ok=True)
    source = Path(ticket.get("source_path", ""))
    if not source.is_file():
        logger.warning("Ticket source not found: %s", source)
        return None
    dest = _ESCALATED_DIR / source.name
    try:
        source.rename(dest)
        logger.info("Moved ticket to escalated: %s", dest.name)
        return dest
    except Exception as exc:
        logger.error("Failed to move ticket: %s", exc)
        return None


def _post_discord_notification(ticket: dict, risk: str) -> None:
    webhook_url = os.environ.get("NEMO_OPS_WEBHOOK_URL", "")
    if not webhook_url:
        return
    tid = ticket.get("ticket_id", "UNKNOWN")
    files = ticket.get("allowed_files", [])
    if isinstance(files, str):
        try:
            files = json.loads(files)
        except (json.JSONDecodeError, TypeError):
            files = [files]
    policy_label = "AUTO-EXECUTE" if risk == "auto" else "NEEDS APPROVAL"
    color = 0x3498DB if risk == "auto" else 0xF39C12
    payload = json.dumps({
        "username": "Intern Ops",
        "embeds": [{"title": f"Escalated to Claude Code: {tid}",
                     "description": f"Devstral failed {_MAX_FAILURES}x. Escalating.\n\n**Policy:** {policy_label}\n**Target:** {', '.join(files) if files else 'see ticket'}",
                     "color": color}]})
    try:
        subprocess.run(["curl", "-sf", "-H", "Content-Type: application/json", "-d", payload, webhook_url],
                       capture_output=True, timeout=10)
        logger.info("Posted Discord escalation notification for %s", tid)
    except Exception as exc:
        logger.warning("Discord notification failed: %s", exc)



# ---------------------------------------------------------------------------
# Discord DM to Claude Code
# ---------------------------------------------------------------------------

CLAUDE_CODE_BOT_ID = "1484595274411151612"
ESCALATION_CHANNEL_ID = "1488125906127159358"
DISCORD_API_BASE = "https://discord.com/api/v10"


def _get_kai_bot_token() -> str:
    """Read KAI Discord bot token from environment or env file."""
    token = os.environ.get("DISCORD_TOKEN", "")
    if token:
        return token
    env_path = Path("/etc/kai/kai-discord.env")
    try:
        if env_path.is_file():
            for line in env_path.read_text().splitlines():
                if line.startswith("DISCORD_TOKEN="):
                    return line.split("=", 1)[1].strip()
    except PermissionError:
        pass
    # Fallback: read from Intern-accessible token file
    token_file = Path(__file__).resolve().parent.parent.parent / "var" / "kai_bot_token.env"
    try:
        if token_file.is_file():
            for line in token_file.read_text().splitlines():
                if line.startswith("DISCORD_TOKEN="):
                    return line.split("=", 1)[1].strip()
    except Exception:
        pass
    logger.warning("Cannot read bot token from %s, %s, or var/kai_bot_token.env", env_path, "env")
    return ""


def _dm_claude_code(ticket: dict, risk: str) -> bool:
    """Post escalation to #escalations channel where Claude Code monitors."""
    token = _get_kai_bot_token()
    if not token:
        logger.warning("No KAI bot token -- cannot post to escalation channel")
        return False

    tid = ticket.get("ticket_id", "UNKNOWN")
    instructions = _format_instructions(ticket, risk)

    # Discord message limit is 2000 chars
    if len(instructions) > 1900:
        instructions = instructions[:1900] + "\n\n[truncated -- see tickets/escalated/ for full ticket]"

    # Post to #escalations channel
    msg_payload = json.dumps({"content": instructions})
    try:
        result = subprocess.run(
            ["curl", "-s", "-X", "POST",
             f"{DISCORD_API_BASE}/channels/{ESCALATION_CHANNEL_ID}/messages",
             "-H", f"Authorization: Bot {token}",
             "-H", "Content-Type: application/json",
             "-d", msg_payload],
            capture_output=True, timeout=15, text=True,
        )
        if result.returncode != 0:
            logger.warning("Failed to post to escalation channel: %s", result.stderr[:200])
            return False
        resp = json.loads(result.stdout) if result.stdout else {}
        if "id" in resp:
            logger.info("Posted escalation to #escalations for %s (msg=%s)", tid, resp["id"])
            return True
        else:
            logger.warning("Unexpected response from Discord: %s", result.stdout[:300])
            return False
    except Exception as exc:
        logger.error("Escalation channel post failed: %s", exc)
        return False


def escalate_to_claude_code(ticket: dict) -> bool:
    tid = ticket.get("ticket_id", "UNKNOWN")
    risk = classify_risk(ticket)
    logger.info("ESCALATING %s to Claude Code (risk=%s, policy=%s)",
                tid, risk, "auto-execute" if risk == "auto" else "needs-approval")
    moved = _move_to_escalated(ticket)
    inbox_path = _write_to_inbox(ticket, risk)
    _post_discord_notification(ticket, risk)

    # DM Claude Code directly (triggers execution)
    dm_sent = _dm_claude_code(ticket, risk)
    if dm_sent:
        logger.info("Claude Code DM sent for %s", tid)
    else:
        logger.warning("Claude Code DM failed for %s -- manual forwarding needed", tid)

    success = bool(moved) and bool(inbox_path)
    if success:
        logger.info("Escalation complete for %s", tid)
    else:
        logger.warning("Partial escalation for %s: moved=%s inbox=%s", tid, bool(moved), bool(inbox_path))
    return success
