"""
Intern Verifier — runs pytest verification and validates verify_command safety.

Hard-fail strict: only pytest invocations are allowed.
No shell separators, chaining, pipes, redirects, or arbitrary commands.
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("intern.verifier")

# Strict pytest command pattern:
#   python -m pytest [path] [flags]
#   pytest [path] [flags]
# No shell metacharacters allowed anywhere in the command.
_PYTEST_RE = re.compile(
    r"^(?:python3?\s+-m\s+)?pytest"  # pytest or python -m pytest
    r"(?:\s+[A-Za-z0-9_./:=\-]+)*$"  # optional args: paths, flags (alphanumeric, dots, slashes, hyphens, equals)
)

# test -f <path> — file existence check (safe, read-only)
_TEST_F_RE = re.compile(
    r"^test\s+-[fedrwxs]\s+[A-Za-z0-9_./:=\-]+$"
)

# bash <script> — run a shell script (must be in safe directories)
_BASH_RE = re.compile(
    r"^bash\s+(?:tests|scripts|tools)/[A-Za-z0-9_./-]+\.sh"
    r"(?:\s+[A-Za-z0-9_./:=\-]+)*$"
)

# All safe command patterns
# grep -q <pattern> <file> — string existence check (safe, read-only)
_GREP_RE = re.compile(
    r'^grep\s+-[qciEl]+\s+["\'\w_./:=\s-]+\s+[\w_./:=-]+$'
)

_SAFE_PATTERNS = [_PYTEST_RE, _TEST_F_RE, _BASH_RE, _GREP_RE]

# Shell metacharacters that must never appear
_SHELL_DANGER = re.compile(r"[;&|`$(){}!<>\\\n\r]")

VERIFY_TIMEOUT = 120  # seconds


class VerifierError(Exception):
    """Raised when verification fails safety checks."""


@dataclass
class VerifyResult:
    """Result of running verification."""
    passed: bool
    command: str
    return_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


def validate_verify_command(command: str) -> list[str]:
    """Validate that verify_command matches a safe execution pattern.

    Returns list of violation messages (empty = valid).
    """
    violations: list[str] = []

    if not command or not command.strip():
        violations.append("verify_command is empty")
        return violations

    command = command.strip()

    # Check for shell metacharacters
    if _SHELL_DANGER.search(command):
        violations.append(
            f"verify_command contains shell metacharacters: {command!r}"
        )
        return violations  # Don't bother with further checks

    # Must match at least one safe command pattern
    if not any(pat.match(command) for pat in _SAFE_PATTERNS):
        violations.append(
            f"verify_command does not match any safe pattern: {command!r}"
        )

    return violations


def run_verification(
    command: str,
    repo_root: Path,
    timeout: int = VERIFY_TIMEOUT,
) -> VerifyResult:
    """Validate and run a verification command.

    Raises VerifierError if the command fails safety validation.
    Returns VerifyResult with test outcome.
    """
    violations = validate_verify_command(command)
    if violations:
        raise VerifierError(
            f"Verification command rejected:\n"
            + "\n".join(f"  - {v}" for v in violations)
        )

    # Split command into args (safe — no shell metacharacters)
    args = command.strip().split()

    # In systemd, bare "python" resolves to /usr/bin/python (system python).
    # Use the same interpreter running the bot so venv packages are available.
    if args[0] in ("python", "python3"):
        args[0] = sys.executable

    logger.info("Running verification: %s", command)

    try:
        result = subprocess.run(
            args,
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        logger.warning("Verification timed out after %ds: %s", timeout, command)
        return VerifyResult(
            passed=False,
            command=command,
            return_code=-1,
            stdout="",
            stderr=f"Timed out after {timeout}s",
            timed_out=True,
        )

    passed = result.returncode == 0
    logger.info(
        "Verification %s (exit=%d): %s",
        "PASSED" if passed else "FAILED",
        result.returncode,
        command,
    )

    return VerifyResult(
        passed=passed,
        command=command,
        return_code=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
    )
