#!/usr/bin/env python3
"""Intern Ticket Generator — scans codebase and seeds backlog automatically.

Generates tickets for:
  - Python modules without test files
  - Source directories without documentation
  - Scripts without error handling (set -e)

Respects Intern ticket format: single file, writable dir, proper verify block.
Skips tickets that already exist in backlog/, active/, escalated/, or done/.

Usage:
    python -m tools.generate_tickets --dry-run    # preview what would be created
    python -m tools.generate_tickets              # create tickets
    python -m tools.generate_tickets --limit 10   # cap at 10 new tickets
"""

from __future__ import annotations

import argparse
import os
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKLOG = REPO_ROOT / "tickets" / "backlog"
ACTIVE = REPO_ROOT / "tickets" / "active"
ESCALATED = REPO_ROOT / "tickets" / "escalated"
DONE = REPO_ROOT / "tickets" / "done"
SRC = REPO_ROOT / "src"
TESTS = REPO_ROOT / "tests"
DOCS = REPO_ROOT / "docs"
SCRIPTS = REPO_ROOT / "scripts"

# Modules to skip (too complex for Intern, or not worth testing)
SKIP_MODULES = {
    "bot.py",           # Discord bot wiring — needs live Discord
    "agent_server.py",  # LiveKit agent — needs live services
    "app.py",           # Gateway app — needs full stack
    "__main__.py",      # Entry points
    "voice_recv_patch.py",
    "content_extract.py",
}

SKIP_DIRS = {"__pycache__"}


def _existing_ticket_ids() -> set[str]:
    """Collect all ticket IDs across all directories."""
    ids = set()
    for d in (BACKLOG, ACTIVE, ESCALATED, DONE):
        if d.is_dir():
            for f in d.iterdir():
                if f.suffix == ".md":
                    ids.add(f.stem)
    return ids


def _module_to_ticket_id(src_path: Path, prefix: str = "NC-TEST") -> str:
    """Convert src/foo/bar.py → NC-TEST-FOO-BAR-001."""
    rel = src_path.relative_to(SRC)
    parts = list(rel.parts)
    parts[-1] = parts[-1].replace(".py", "")
    slug = "-".join(p.upper().replace("_", "-") for p in parts)
    return f"{prefix}-{slug}-001"


def _dir_to_ticket_id(dir_name: str) -> str:
    """Convert 'gateway' → NC-DOC-GATEWAY-001."""
    return f"NC-DOC-{dir_name.upper().replace('_', '-')}-001"


def generate_test_tickets(existing: set[str]) -> list[tuple[str, str]]:
    """Generate tickets for Python modules without tests."""
    tickets = []
    for py_file in sorted(SRC.rglob("*.py")):
        if py_file.name.startswith("__") and py_file.name != "__init__.py":
            continue
        if py_file.name == "__init__.py":
            continue
        if py_file.name in SKIP_MODULES:
            continue

        # Check if any test file covers this module
        base = py_file.stem
        has_test = any(
            t.name.startswith(f"test_") and base in t.name
            for t in TESTS.glob("test_*.py")
        ) if TESTS.is_dir() else False

        if has_test:
            continue

        tid = _module_to_ticket_id(py_file)
        if tid in existing:
            continue

        rel_path = py_file.relative_to(REPO_ROOT)
        test_file = f"tests/test_{base}.py"

        content = f"""# {tid}: Add unit tests for {rel_path}

**Priority:** P3
**Status:** backlog

## Description

Create unit tests for `{rel_path}`. The test file should:
- Import the module and verify key classes/functions exist
- Test at least 2-3 core functions with basic inputs
- Use pytest fixtures where appropriate
- Mock external dependencies (HTTP calls, file I/O, databases)

Read the source file first to understand what to test.

**Allowed files:**
- `{test_file}`

## Verify:
`pytest {test_file} -v`
"""
        tickets.append((tid, content))

    return tickets


def generate_doc_tickets(existing: set[str]) -> list[tuple[str, str]]:
    """Generate tickets for undocumented source directories."""
    tickets = []
    if not SRC.is_dir():
        return tickets

    for child in sorted(SRC.iterdir()):
        if not child.is_dir() or child.name in SKIP_DIRS:
            continue

        # Check if any doc covers this directory
        has_doc = any(
            child.name.lower() in d.name.lower()
            for d in DOCS.glob("*.md")
        ) if DOCS.is_dir() else False

        if has_doc:
            continue

        tid = _dir_to_ticket_id(child.name)
        if tid in existing:
            continue

        # List the Python files in the directory
        py_files = sorted(child.glob("*.py"))
        file_list = ", ".join(f.name for f in py_files if f.name != "__init__.py")
        doc_file = f"docs/{child.name.upper()}_ARCHITECTURE.md"

        content = f"""# {tid}: Document {child.name}/ module architecture

**Priority:** P3
**Status:** backlog

## Description

Create a markdown document describing the `src/{child.name}/` module.
Key files: {file_list or 'see directory'}

The document should cover:
- What this module does and its role in the KAI system
- Key classes and functions (brief descriptions)
- How it connects to other modules
- Configuration or environment variables it uses

Keep it factual and under 150 lines.

**Allowed files:**
- `{doc_file}`

## Verify:
`test -f {doc_file}`
"""
        tickets.append((tid, content))

    return tickets


def main():
    parser = argparse.ArgumentParser(description="Generate Intern tickets from codebase analysis")
    parser.add_argument("--dry-run", action="store_true", help="Preview without creating files")
    parser.add_argument("--limit", type=int, default=20, help="Max tickets to generate (default: 20)")
    parser.add_argument("--type", choices=["test", "doc", "all"], default="all", help="Ticket type to generate")
    args = parser.parse_args()

    existing = _existing_ticket_ids()
    print(f"Existing tickets: {len(existing)}")

    all_tickets: list[tuple[str, str]] = []

    if args.type in ("test", "all"):
        test_tickets = generate_test_tickets(existing)
        print(f"Test tickets to generate: {len(test_tickets)}")
        all_tickets.extend(test_tickets)

    if args.type in ("doc", "all"):
        doc_tickets = generate_doc_tickets(existing)
        print(f"Doc tickets to generate: {len(doc_tickets)}")
        all_tickets.extend(doc_tickets)

    # Apply limit
    if len(all_tickets) > args.limit:
        print(f"Capping at {args.limit} (from {len(all_tickets)} available)")
        all_tickets = all_tickets[:args.limit]

    if args.dry_run:
        print(f"\n=== DRY RUN — would create {len(all_tickets)} tickets ===")
        for tid, _ in all_tickets:
            print(f"  {tid}")
        return

    BACKLOG.mkdir(parents=True, exist_ok=True)
    created = 0
    for tid, content in all_tickets:
        path = BACKLOG / f"{tid}.md"
        if not path.exists():
            path.write_text(content)
            created += 1
            print(f"  Created: {tid}")

    print(f"\nGenerated {created} new tickets in tickets/backlog/")


if __name__ == "__main__":
    main()
