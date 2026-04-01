"""
Intern Executor — validates and applies bounded edit plans.

Enforces:
- Single-file only (v1 restriction)
- File must be in ticket's allowed_files list
- "replace" and "insert_after" actions permitted
- old string must appear exactly once in target file
- Max 10 edits per plan
- No path traversal or out-of-repo edits
"""

from __future__ import annotations

import ast
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from intern.planner import EditPlan

logger = logging.getLogger("intern.executor")

MAX_EDITS = 10
MIN_ANCHOR_LENGTH = 50          # old string must be at least this many chars
MAX_EXPANSION_RATIO = 50.0      # new string can be at most N× the length of old
VALID_ACTIONS = {"replace", "replace_line", "insert_after"}


class ExecutorError(Exception):
    """Raised when an edit plan fails validation or application."""


@dataclass
class EditResult:
    """Result of applying an edit plan."""
    applied: bool
    file_path: str
    edits_applied: int
    message: str
    original_content: str = ""
    modified_content: str = ""


def validate_plan(
    plan: EditPlan,
    allowed_files: list[str],
    repo_root: Path,
    placeholder_content: str | None = None,
) -> list[str]:
    """Validate an edit plan against guardrails. Returns list of violation messages (empty = valid)."""
    violations: list[str] = []

    if not plan.edits:
        violations.append("Plan contains no edits")
        return violations

    if len(plan.edits) > MAX_EDITS:
        violations.append(f"Plan has {len(plan.edits)} edits, max is {MAX_EDITS}")

    # Collect unique target files
    target_files = set()
    for edit in plan.edits:
        target_files.add(edit.file)

    # v1: single-file only
    if len(target_files) > 1:
        violations.append(
            f"v1 restriction: plan targets {len(target_files)} files "
            f"({', '.join(sorted(target_files))}), only 1 allowed"
        )

    for edit in plan.edits:
        # Action must be in VALID_ACTIONS
        if edit.action not in VALID_ACTIONS:
            violations.append(
                f"Edit action must be one of {sorted(VALID_ACTIONS)}, got '{edit.action}'"
            )

        # File must be in allowed list
        if edit.file not in allowed_files:
            violations.append(
                f"File '{edit.file}' not in allowed_files: {allowed_files}"
            )

        # Resolve and check path is within repo
        try:
            resolved = (repo_root / edit.file).resolve()
            if not str(resolved).startswith(str(repo_root.resolve())):
                violations.append(f"Path traversal detected: '{edit.file}' resolves outside repo")
        except (OSError, ValueError) as e:
            violations.append(f"Invalid path '{edit.file}': {e}")

        # File must exist
        target = repo_root / edit.file
        if not target.is_file():
            violations.append(f"Target file does not exist: {edit.file}")

        # old string must not be empty
        if not edit.old:
            violations.append(f"Edit has empty 'old' string for {edit.file}")

        # old must differ from new (skip for insert_after — not applicable)
        if edit.action != "insert_after" and edit.old == edit.new:
            violations.append(f"Edit 'old' and 'new' are identical for {edit.file}")

        # Anchor strength checks — bypassed for placeholder-based new-file creation
        is_placeholder = (
            placeholder_content is not None
            and edit.old == placeholder_content
        )

        if not is_placeholder:
            # Anchor strength: old string must be long enough to be unambiguous
            # (skip for replace_line — intentionally narrow, validated against anchor in planner)
            if edit.action != "replace_line" and edit.old and len(edit.old) < MIN_ANCHOR_LENGTH:
                violations.append(
                    f"Anchor too short for {edit.file}: {len(edit.old)} chars "
                    f"(minimum {MIN_ANCHOR_LENGTH}). Use a larger context window "
                    f"around the edit point."
                )

            # Expansion ratio: prevent tiny anchor → huge replacement
            # (skip for insert_after and replace_line — both use narrow old text)
            if edit.action not in ("insert_after", "replace_line") and edit.old and edit.new:
                ratio = len(edit.new) / max(len(edit.old), 1)
                if ratio > MAX_EXPANSION_RATIO:
                    violations.append(
                        f"Expansion ratio too high for {edit.file}: "
                        f"{len(edit.old)} → {len(edit.new)} chars "
                        f"(ratio {ratio:.1f}×, max {MAX_EXPANSION_RATIO}×). "
                        f"Include more surrounding context in the anchor."
                    )

    return violations


def validate_and_check_uniqueness(
    plan: EditPlan,
    allowed_files: list[str],
    repo_root: Path,
    placeholder_content: str | None = None,
) -> list[str]:
    """Full validation including uniqueness check of old strings in file content."""
    violations = validate_plan(plan, allowed_files, repo_root, placeholder_content)
    if violations:
        return violations

    # Check each old string appears exactly once
    for edit in plan.edits:
        target = repo_root / edit.file
        try:
            content = target.read_text(encoding="utf-8")
        except OSError as e:
            violations.append(f"Cannot read {edit.file}: {e}")
            continue

        count = content.count(edit.old)
        if count == 0:
            violations.append(
                f"old string not found in {edit.file}: {edit.old[:80]!r}"
            )
        elif count > 1:
            violations.append(
                f"old string appears {count} times in {edit.file} (must be exactly 1): {edit.old[:80]!r}"
            )

    return violations


def _dump_syntax_fail(
    repo_root: Path, target_file: str, content: str, err: SyntaxError
) -> None:
    """Write the proposed file content and error metadata to sidecar artifacts."""
    artifacts = repo_root / "artifacts" / "intern"
    artifacts.mkdir(parents=True, exist_ok=True)
    stem = Path(target_file).stem

    # Full proposed file content (the text that failed ast.parse)
    (artifacts / f"{stem}.syntax_fail.py").write_text(content, encoding="utf-8")

    # Structured error metadata
    (artifacts / f"{stem}.syntax_fail.json").write_text(
        json.dumps({
            "target_file": target_file,
            "error_line": err.lineno,
            "error_offset": err.offset,
            "error_msg": err.msg,
            "error_text": err.text,
            "content_lines": len(content.splitlines()),
        }, indent=2),
        encoding="utf-8",
    )
    logger.info("Syntax-fail sidecar written to %s/%s.syntax_fail.*", artifacts, stem)


def apply_plan(
    plan: EditPlan,
    allowed_files: list[str],
    repo_root: Path,
    placeholder_content: str | None = None,
) -> EditResult:
    """Validate and apply an edit plan. Returns EditResult.

    Raises ExecutorError if validation fails.
    """
    violations = validate_and_check_uniqueness(plan, allowed_files, repo_root, placeholder_content)
    if violations:
        raise ExecutorError(
            f"Edit plan rejected ({len(violations)} violation(s)):\n"
            + "\n".join(f"  - {v}" for v in violations)
        )

    # All edits target the same file (enforced by validation)
    target_file = plan.edits[0].file
    target_path = repo_root / target_file

    original = target_path.read_text(encoding="utf-8")
    content = original

    for i, edit in enumerate(plan.edits):
        # Double-check uniqueness (defensive — already validated)
        if content.count(edit.old) != 1:
            raise ExecutorError(
                f"Edit {i}: old string no longer unique after prior edits "
                f"(found {content.count(edit.old)} occurrences)"
            )

        if edit.action == "insert_after":
            # insert_after: keep anchor intact, append new text after it
            new_text = edit.new.lstrip("\n")
            if new_text and not new_text.endswith("\n"):
                new_text += "\n"
            replacement = edit.old + "\n\n" + new_text
            logger.debug("Edit %d: insert_after anchor (%d chars new)", i, len(new_text))
        else:
            # replace: substitute anchor with new text
            new_text = edit.new
            if edit.old.endswith("\n") and new_text and not new_text.endswith("\n"):
                new_text += "\n"
                logger.debug("Edit %d: appended trailing newline to new text", i)
            replacement = new_text

        content = content.replace(edit.old, replacement, 1)
        logger.info("Applied edit %d/%d to %s", i + 1, len(plan.edits), target_file)

    target_path.write_text(content, encoding="utf-8")

    # Syntax sanity check for Python files — catches concatenation, missing
    # newlines, broken indentation before the slower pytest verification step.
    if target_file.endswith(".py"):
        try:
            ast.parse(content, filename=target_file)
        except SyntaxError as e:
            # Write sidecar artifacts before rollback so we can inspect the bad output
            _dump_syntax_fail(repo_root, target_file, content, e)
            target_path.write_text(original, encoding="utf-8")
            raise ExecutorError(
                f"Syntax error after edits ({e.lineno}:{e.offset}): {e.msg}"
            )

    return EditResult(
        applied=True,
        file_path=target_file,
        edits_applied=len(plan.edits),
        message=f"Applied {len(plan.edits)} edit(s) to {target_file}",
        original_content=original,
        modified_content=content,
    )
