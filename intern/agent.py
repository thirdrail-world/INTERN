"""
Intern Agent v1 — single-ticket, single-file CLI executor.

Reads a ticket file, plans with LLM (Nemotron NIM or local Qwen),
applies bounded edits, runs verification, writes a result report,
posts to #nemo-ops.

Does NOT auto-commit. Human reviews the diff before committing.

Usage:
    python -m src.intern.agent --ticket tickets/backlog/EXAMPLE.md
    python -m src.intern.agent --ticket tickets/backlog/EXAMPLE.md --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from intern.planner import EditPlan, PlannerError, generate_plan
from intern.executor import ExecutorError, apply_plan, validate_and_check_uniqueness, EditResult
from intern.verifier import VerifierError, run_verification, VerifyResult

logger = logging.getLogger("intern.agent")

_REPO_ROOT = Path(__file__).parent.parent.parent
_PLACEHOLDER = "# placeholder — new file\n"
_ARTIFACTS_DIR = _REPO_ROOT / "artifacts" / "intern"
MAX_RETRIES = 2


# ---------------------------------------------------------------------------
# Ticket parsing
# ---------------------------------------------------------------------------

def parse_ticket(ticket_path: Path) -> dict:
    """Parse a ticket markdown file into structured data.

    Extracts:
      - ticket_id: from filename or TICKET-[ID] in first heading
      - body: full markdown content
      - allowed_files: from "Allowed files:" or "Files involved:" field
      - description, acceptance_criteria: from standard fields
    """
    if not ticket_path.is_file():
        raise FileNotFoundError(f"Ticket not found: {ticket_path}")

    text = ticket_path.read_text(encoding="utf-8")

    # Extract ticket ID from first heading
    id_match = re.search(r"#\s*TICKET-([A-Z0-9_-]+)", text)
    if id_match:
        ticket_id = id_match.group(1)
    else:
        # Fallback to filename
        ticket_id = ticket_path.stem.replace(" ", "-").upper()

    # Extract allowed_files — look for explicit "Allowed files:" field
    allowed_files: list[str] = []
    af_match = re.search(
        r"\*{0,2}Allowed files?:?\*{0,2}:?\s*\n((?:\s*-\s*`[^`]+`\s*\n)+)",
        text,
        re.IGNORECASE,
    )
    if af_match:
        for m in re.finditer(r"`([^`]+)`", af_match.group(1)):
            allowed_files.append(m.group(1))

    # Also check "Files involved:" as fallback
    if not allowed_files:
        fi_match = re.search(
            r"\*{0,2}Files involved:?\*{0,2}:?\s*\n((?:\s*-\s*`[^`]+`\s*\n)+)",
            text,
            re.IGNORECASE,
        )
        if fi_match:
            for m in re.finditer(r"`([^`]+)`", fi_match.group(1)):
                allowed_files.append(m.group(1))

    return {
        "ticket_id": ticket_id,
        "body": text,
        "allowed_files": allowed_files,
        "source_path": str(ticket_path),
    }


# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------

def preflight(ticket: dict) -> list[str]:
    """Run pre-flight checks on a parsed ticket. Returns list of blocking issues."""
    issues: list[str] = []

    if not ticket["allowed_files"]:
        issues.append(
            "Ticket does not declare allowed_files. "
            "Add an 'Allowed files:' section with backtick-quoted paths."
        )

    if len(ticket["allowed_files"]) > 1:
        issues.append(
            f"v1 restriction: ticket declares {len(ticket['allowed_files'])} allowed files "
            f"({', '.join(ticket['allowed_files'])}). Only 1 file allowed in v1."
        )

    # Check the single allowed file — if it doesn't exist, that's OK for
    # new-file creation tickets (we'll create a placeholder before planning).
    # Only reject paths outside the repo or in blocked directories.
    if len(ticket["allowed_files"]) == 1:
        fpath = _REPO_ROOT / ticket["allowed_files"][0]
        try:
            resolved = fpath.resolve()
            if not str(resolved).startswith(str(_REPO_ROOT.resolve())):
                issues.append(f"Allowed file resolves outside repo: {ticket['allowed_files'][0]}")
        except (OSError, ValueError) as e:
            issues.append(f"Invalid allowed file path: {e}")

    return issues


# ---------------------------------------------------------------------------
# Result reporting
# ---------------------------------------------------------------------------

def _write_verify_log(
    ticket_id: str,
    stdout: str | None,
    stderr: str | None,
) -> str:
    """Write full pytest output to a sidecar log file. Returns the path."""
    _ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = _ARTIFACTS_DIR / f"{ticket_id}.verify.log"
    parts = []
    if stdout:
        parts.append("=== STDOUT ===\n")
        parts.append(stdout)
        parts.append("\n")
    if stderr:
        parts.append("=== STDERR ===\n")
        parts.append(stderr)
        parts.append("\n")
    log_path.write_text("".join(parts) or "(empty output)\n", encoding="utf-8")
    logger.info("Verification log written to %s", log_path)
    return str(log_path)


def write_result(
    ticket_id: str,
    plan: EditPlan | None,
    edit_result: EditResult | None,
    verify_result: VerifyResult | None,
    error: str | None,
    duration_ms: float,
    dry_run: bool = False,
    verify_log: str | None = None,
) -> Path:
    """Write execution result to artifacts/intern/<ticket_id>.json."""
    _ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)

    if dry_run:
        success = plan is not None and error is None
    else:
        success = (
            edit_result is not None
            and edit_result.applied
            and verify_result is not None
            and verify_result.passed
        )

    result = {
        "ticket_id": ticket_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "duration_ms": round(duration_ms, 1),
        "mode": "dry_run" if dry_run else "live",
        "success": success,
        "error": error,
        "plan": {
            "summary": plan.summary if plan else None,
            "edit_count": len(plan.edits) if plan else 0,
            "verify_command": plan.verify_command if plan else None,
            "confidence": plan.confidence if plan else None,
        } if plan else None,
        "edit": {
            "applied": edit_result.applied if edit_result else False,
            "file": edit_result.file_path if edit_result else None,
            "edits_applied": edit_result.edits_applied if edit_result else 0,
        } if edit_result else None,
        "verification": {
            "passed": verify_result.passed if verify_result else None,
            "command": verify_result.command if verify_result else None,
            "return_code": verify_result.return_code if verify_result else None,
            "timed_out": verify_result.timed_out if verify_result else None,
            "stdout_tail": (verify_result.stdout[-500:] if verify_result and verify_result.stdout else ""),
            "stderr_tail": (verify_result.stderr[-500:] if verify_result and verify_result.stderr else ""),
            "full_log": verify_log,
        } if verify_result else None,
    }

    out_path = _ARTIFACTS_DIR / f"{ticket_id}.json"
    out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    logger.info("Result written to %s", out_path)
    return out_path


def report_to_nemo_ops(ticket_id: str, action: str, summary: str) -> None:
    """Post status update to #nemo-ops via nc_report.sh."""
    nc_report = _REPO_ROOT / "tools" / "nc_report.sh"
    if not nc_report.is_file():
        logger.warning("nc_report.sh not found, skipping #nemo-ops notification")
        return

    try:
        subprocess.run(
            ["bash", str(nc_report), "ticket", ticket_id, action, summary],
            cwd=str(_REPO_ROOT),
            timeout=15,
            capture_output=True,
        )
    except Exception as e:
        logger.warning("Failed to post to #nemo-ops: %s", e)


# ---------------------------------------------------------------------------
# Main execution flow
# ---------------------------------------------------------------------------

async def execute_ticket(
    ticket_path: Path,
    dry_run: bool = False,
    base_url: str = "",
    model: str = "",
    api_key: str = "",
) -> bool:
    """Execute a single ticket end-to-end. Returns True if successful."""
    t_start = time.monotonic()

    # Step 1: Parse ticket
    logger.info("Reading ticket: %s", ticket_path)
    ticket = parse_ticket(ticket_path)
    ticket_id = ticket["ticket_id"]
    logger.info("Ticket ID: %s, allowed_files: %s", ticket_id, ticket["allowed_files"])

    # Step 2: Pre-flight checks
    issues = preflight(ticket)
    if issues:
        error_msg = f"Pre-flight failed:\n" + "\n".join(f"  - {i}" for i in issues)
        logger.error(error_msg)
        elapsed = (time.monotonic() - t_start) * 1000
        write_result(ticket_id, None, None, None, error_msg, elapsed)
        report_to_nemo_ops(ticket_id, "blocked", f"Pre-flight: {issues[0]}")
        return False

    target_file = ticket["allowed_files"][0]
    target_path = _REPO_ROOT / target_file
    if target_path.is_file():
        file_content = target_path.read_text(encoding="utf-8")
        if file_content == _PLACEHOLDER:
            logger.info("Target file is a leftover placeholder: %s", target_file)
            ticket["placeholder_content"] = file_content
    else:
        # New file — create placeholder so anchor extraction and apply_plan work
        logger.info("Target file does not exist, creating placeholder: %s", target_file)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        file_content = _PLACEHOLDER
        target_path.write_text(file_content, encoding="utf-8")
        ticket["placeholder_content"] = file_content

    # Step 3: Report started
    report_to_nemo_ops(ticket_id, "started", "Intern v1 local agent executing")

    # Step 4-6: Plan → Apply → Verify (with retry loop for live runs)
    plan: EditPlan | None = None
    edit_result: EditResult | None = None
    verify_result: VerifyResult | None = None
    error: str | None = None
    verify_log_path: str | None = None
    original_content = file_content  # saved before any edits
    attempts_made = 0

    if dry_run:
        # --- Dry-run path: single attempt, no retry ---
        try:
            logger.info("Generating plan with %s via %s", model, base_url)
            plan = await generate_plan(
                ticket_id=ticket_id,
                ticket_body=ticket["body"],
                file_path=target_file,
                file_content=file_content,
                base_url=base_url,
                model=model,
                api_key=api_key,
            )
            logger.info(
                "Plan: %d edit(s), verify=%s, confidence=%s",
                len(plan.edits), plan.verify_command, plan.confidence,
            )
        except PlannerError as e:
            error = f"Planning failed: {e}"
            logger.error(error)

        if plan and not error:
            logger.info("DRY RUN — validating plan against guardrails")
            violations = validate_and_check_uniqueness(
                plan, ticket["allowed_files"], _REPO_ROOT,
                placeholder_content=ticket.get("placeholder_content"),
            )
            if violations:
                error = (
                    f"DRY RUN — plan failed validation "
                    f"({len(violations)} violation(s)):\n"
                    + "\n".join(f"  - {v}" for v in violations)
                )
                logger.error(error)
            else:
                logger.info("DRY RUN — plan passes all guardrails")
                logger.info("Plan summary: %s", plan.summary)
                for i, edit in enumerate(plan.edits):
                    logger.info(
                        "  Edit %d: %s — replace %d chars with %d chars",
                        i, edit.file, len(edit.old), len(edit.new),
                    )
    else:
        # --- Live path: plan → apply → verify with retry loop ---
        retry_body = ticket["body"]

        for attempt in range(1 + MAX_RETRIES):
            attempts_made = attempt + 1
            t_attempt = time.monotonic()
            error = None  # reset per attempt

            # Always restore file to original before each attempt
            (_REPO_ROOT / target_file).write_text(original_content, encoding="utf-8")

            # Generate plan (with fix context on retries)
            try:
                logger.info(
                    "Generating plan (attempt %d/%d) with %s via %s",
                    attempt + 1, 1 + MAX_RETRIES, model, base_url,
                )
                plan = await generate_plan(
                    ticket_id=ticket_id,
                    ticket_body=retry_body,
                    file_path=target_file,
                    file_content=original_content,
                    base_url=base_url,
                    model=model,
                    api_key=api_key,
                )
                logger.info(
                    "Plan: %d edit(s), verify=%s, confidence=%s",
                    len(plan.edits), plan.verify_command, plan.confidence,
                )
            except PlannerError as e:
                error = f"Planning failed (attempt {attempt + 1}): {e}"
                logger.error(error)
                break  # Planner errors are not retryable

            # Apply edits
            try:
                edit_result = apply_plan(
                    plan, ticket["allowed_files"], _REPO_ROOT,
                    placeholder_content=ticket.get("placeholder_content"),
                )
                logger.info("Edits applied: %s", edit_result.message)
            except ExecutorError as e:
                error = f"Execution failed (attempt {attempt + 1}): {e}"
                logger.error(error)
                break  # Executor errors are not retryable

            # Run verification
            try:
                verify_result = run_verification(plan.verify_command, _REPO_ROOT)
            except VerifierError as e:
                error = f"Verification rejected (attempt {attempt + 1}): {e}"
                logger.error(error)
                # Revert
                (_REPO_ROOT / target_file).write_text(original_content, encoding="utf-8")
                break  # Verifier rejection (bad command) is not retryable

            attempt_ms = (time.monotonic() - t_attempt) * 1000

            if verify_result.passed:
                logger.info(
                    "Verification PASSED (attempt %d, %.0fms)",
                    attempt + 1, attempt_ms,
                )
                break  # Success!

            # Verification failed — log and prepare retry
            logger.warning(
                "Verification FAILED (attempt %d/%d, exit=%d, %.0fms)",
                attempt + 1, 1 + MAX_RETRIES,
                verify_result.return_code, attempt_ms,
            )
            verify_log_path = _write_verify_log(
                ticket_id, verify_result.stdout, verify_result.stderr,
            )

            if attempt < MAX_RETRIES:
                # Prepare retry context with pytest error feedback
                fix_context = (
                    f"\n\n--- RETRY {attempt + 1} ---\n"
                    f"The previous attempt generated code that failed verification.\n"
                    f"Pytest output:\n"
                    f"```\n{verify_result.stdout[-1500:]}\n```\n"
                    f"Stderr:\n"
                    f"```\n{verify_result.stderr[-500:]}\n```\n"
                    f"Fix the code to make the tests pass. Do NOT repeat the same mistake."
                )
                retry_body = ticket["body"] + fix_context
                logger.info(
                    "RETRY %d/%d: re-planning with pytest error feedback (%s...)",
                    attempt + 1, MAX_RETRIES,
                    verify_result.stdout[:200].replace("\n", " "),
                )
            else:
                # Final failure — revert
                (_REPO_ROOT / target_file).write_text(original_content, encoding="utf-8")
                logger.info("Reverted %s to original content after %d attempts", target_file, 1 + MAX_RETRIES)
                error = f"Verification failed after {1 + MAX_RETRIES} attempts"

    # Step 7: Write result
    elapsed = (time.monotonic() - t_start) * 1000
    if dry_run:
        success = plan is not None and error is None
    else:
        success = (
            edit_result is not None
            and edit_result.applied
            and verify_result is not None
            and verify_result.passed
        )
    result_path = write_result(
        ticket_id, plan, edit_result, verify_result, error, elapsed,
        dry_run=dry_run,
        verify_log=verify_log_path,
    )

    # Step 8: Report to #nemo-ops
    if dry_run:
        if success:
            report_to_nemo_ops(ticket_id, "completed", f"DRY RUN — plan passes guardrails, {len(plan.edits) if plan else 0} edit(s)")
        else:
            report_to_nemo_ops(ticket_id, "blocked", f"DRY RUN — {error or 'unknown error'}")
    elif success:
        retry_note = f" on attempt {attempts_made}" if attempts_made > 1 else ""
        report_to_nemo_ops(
            ticket_id,
            "completed",
            f"{plan.summary if plan else 'done'} — verification passed{retry_note}",
        )
    else:
        retry_note = f" after {attempts_made} attempt(s)" if attempts_made > 1 else ""
        report_to_nemo_ops(ticket_id, "blocked", f"{error or 'Unknown error'}{retry_note}")

    mode_label = "DRY RUN" if dry_run else "LIVE"
    logger.info("Done: %s in %.0fms — %s %s", ticket_id, elapsed, mode_label, "SUCCESS" if success else "FAILED")
    return success


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        prog="intern-agent",
        description="Intern v1 — single-ticket, single-file local executor",
    )
    parser.add_argument(
        "--ticket",
        type=Path,
        required=True,
        help="Path to ticket markdown file",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate plan but do not apply edits",
    )
    parser.add_argument(
        "--profile",
        default=os.getenv("NEMOCLAW_PROFILE", "devstral"),
        choices=["nemotron", "qwen", "devstral"],
        help="Planner backend profile (default: devstral)",
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("NEMOCLAW_LLM_URL", ""),
        help="LLM base URL (overrides profile default)",
    )
    parser.add_argument(
        "--model",
        default=os.getenv("NEMOCLAW_LLM_MODEL", ""),
        help="LLM model name (overrides profile default)",
    )
    parser.add_argument(
        "--api-key",
        default=os.getenv("NEMOCLAW_LLM_API_KEY", os.getenv("NVIDIA_API_KEY", "")),
        help="LLM API key (default: NVIDIA_API_KEY env var)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    from intern.planner import PLANNER_PROFILES
    profile = PLANNER_PROFILES[args.profile]
    base_url = args.base_url or profile["base_url"]
    model = args.model or profile["model"]
    api_key = args.api_key

    if profile["needs_api_key"] and not api_key:
        parser.error(
            f"Profile '{args.profile}' requires an API key. "
            f"Set NVIDIA_API_KEY or NEMOCLAW_LLM_API_KEY, or pass --api-key."
        )

    success = asyncio.run(execute_ticket(
        ticket_path=args.ticket,
        dry_run=args.dry_run,
        base_url=base_url,
        model=model,
        api_key=api_key,
    ))

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
