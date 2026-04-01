"""Intern CLI — autonomous dev agent that ships code while you sleep.

Usage:
    intern run --once          Process all backlog tickets, then exit
    intern run --live          Daemon mode — scan every 60s
    intern init                Create ticket directory structure
    intern status              Show backlog/done/escalated counts
    intern generate-tickets    Scan codebase for untested/undocumented modules
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def cmd_init(args):
    """Create ticket directory structure."""
    base = Path(args.workdir)
    dirs = ["tickets/backlog", "tickets/done", "tickets/escalated",
            "tickets/retired", "artifacts/intern"]
    for d in dirs:
        (base / d).mkdir(parents=True, exist_ok=True)
        print(f"  Created: {d}/")

    config = base / "intern.yaml"
    if not config.exists():
        config.write_text(
            "# Intern Configuration\n"
            "# See https://github.com/thirdrail-world/intern for docs\n\n"
            "llm_profiles:\n"
            "  default:\n"
            '    base_url: "http://localhost:8200/v1"\n'
            '    model: "Qwen/Qwen3-8B-AWQ"\n'
            "    max_tokens: 8192\n"
            "    temperature: 0.2\n\n"
            "writable_dirs:\n"
            '  - "src/"\n  - "tests/"\n  - "docs/"\n\n'
            "protected_files: []\n\n"
            "escalation:\n"
            '  handler: "file"\n'
            "  max_retries: 5\n\n"
            "scanner:\n"
            "  interval_seconds: 60\n"
        )
        print(f"  Created: intern.yaml")

    print("\nIntern initialized. Drop tickets in tickets/backlog/ and run: intern run --once")


def cmd_status(args):
    """Show ticket counts."""
    base = Path(args.workdir)
    for name in ["backlog", "done", "escalated", "retired"]:
        d = base / "tickets" / name
        count = len(list(d.glob("*.md"))) if d.exists() else 0
        print(f"  {name}: {count}")


def cmd_run(args):
    """Run the queue runner."""
    os.chdir(args.workdir)
    sys.path.insert(0, args.workdir)

    from intern.queue_runner import main as runner_main
    sys.argv = ["intern"]
    if args.live:
        sys.argv.append("--live")
    if args.once:
        sys.argv.append("--once")
    runner_main()


def cmd_generate(args):
    """Generate tickets from codebase scan."""
    os.chdir(args.workdir)
    sys.path.insert(0, args.workdir)

    scan_dir = args.scan_dir or "src"
    ticket_type = args.type or "tests"
    print(f"Scanning {scan_dir}/ for {ticket_type} tickets...")

    try:
        from tools.generate_tickets import main as gen_main
        gen_main()
    except ImportError:
        print("tools/generate_tickets.py not found. Run 'intern init' first.")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        prog="intern",
        description="Autonomous dev agent. Ships code while you sleep.",
    )
    parser.add_argument("--workdir", default=".", help="Working directory (default: .)")
    sub = parser.add_subparsers(dest="command")

    # init
    sub.add_parser("init", help="Create ticket directory structure")

    # status
    sub.add_parser("status", help="Show ticket counts")

    # run
    run_p = sub.add_parser("run", help="Run the queue runner")
    run_p.add_argument("--live", action="store_true", help="Daemon mode")
    run_p.add_argument("--once", action="store_true", help="Process once and exit")

    # generate-tickets
    gen_p = sub.add_parser("generate-tickets", help="Generate tickets from codebase")
    gen_p.add_argument("--scan-dir", default="src", help="Directory to scan")
    gen_p.add_argument("--type", default="tests", choices=["tests", "docs"])

    args = parser.parse_args()

    if args.command == "init":
        cmd_init(args)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "run":
        cmd_run(args)
    elif args.command == "generate-tickets":
        cmd_generate(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
