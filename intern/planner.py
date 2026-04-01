"""
Intern Planner — sends ticket + file content to LLM, returns edit plan.

Supports multiple backends via PLANNER_PROFILES (Nemotron NIM cloud, Qwen local).
Produces a structured JSON edit plan that the executor validates and applies.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

logger = logging.getLogger("intern.planner")

# Strip Qwen3 <think>...</think> blocks from output
_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)
_THINK_UNCLOSED_RE = re.compile(r"<think>.*", re.DOTALL)

# Planner backend profiles
PLANNER_PROFILES = {
    "nemotron": {
        "base_url": "https://integrate.api.nvidia.com/v1",
        "model": "nvidia/llama-3.3-nemotron-super-49b-v1",
        "max_tokens": 4096,
        "temperature": 0,
        "timeout": 120.0,
        "needs_api_key": True,
    },
    "qwen": {
        "base_url": "http://localhost:8200/v1",
        "model": "Qwen/Qwen3-8B-AWQ",
        "max_tokens": 768,
        "temperature": 0,
        "timeout": 120.0,
        "needs_api_key": False,
        "json_mode": False,  # xgrammar needs triton (unavailable on aarch64)
    },
    "devstral": {
        "base_url": "http://localhost:8201/v1",
        "model": "cyankiwi/Devstral-Small-2-24B-Instruct-2512-AWQ-4bit",
        "max_tokens": 4096,
        "temperature": 0,
        "timeout": 60.0,
        "needs_api_key": False,
        "json_mode": False,  # xgrammar needs triton (unavailable on aarch64)
    },
}

# Extract JSON from markdown fenced code blocks
_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL)


@dataclass
class Edit:
    """Single string-replacement edit."""
    file: str
    action: str
    old: str
    new: str


@dataclass
class EditPlan:
    """Structured edit plan produced by the LLM."""
    ticket_id: str
    summary: str
    edits: list[Edit] = field(default_factory=list)
    verify_command: str = ""
    confidence: str = "unknown"
    raw_response: str = ""


class PlannerError(Exception):
    """Raised when the LLM fails to produce a valid plan."""


def _strip_thinking(text: str) -> str:
    """Remove Qwen3 reasoning blocks."""
    text = _THINK_RE.sub("", text)
    text = _THINK_UNCLOSED_RE.sub("", text)
    return text.strip()


_CTRL_ESCAPE = {
    "\n": "\\n",
    "\t": "\\t",
    "\r": "\\r",
}


def _fuzzy_match_line(needle: str, anchor_text: str) -> str | None:
    """Try to find needle in anchor_text, tolerating common LLM JSON escaping errors.

    Returns the actual matching line from anchor_text, or None if no match.
    Only corrects quote-style and escape differences, not content changes.
    """
    needle_stripped = needle.strip()
    if not needle_stripped:
        return None

    for line in anchor_text.splitlines():
        line_stripped = line.strip()
        if not line_stripped:
            continue

        # Exact match (already checked, but included for completeness)
        if needle_stripped == line_stripped:
            return line

        # Normalize quotes: replace single quotes with double quotes for comparison
        n_norm = needle_stripped.replace("'", '"')
        l_norm = line_stripped.replace("'", '"')
        if n_norm == l_norm:
            return line

        # Normalize escaped dollar signs: \\$ → $
        n_norm2 = needle_stripped.replace("\\$", "$")
        if n_norm2 == line_stripped:
            return line

        # Combined: quotes + escaped dollar
        n_norm3 = n_norm.replace("\\$", "$")
        l_norm3 = l_norm.replace("\\$", "$")
        if n_norm3 == l_norm3:
            return line

    return None


def _repair_json(text: str) -> str:
    """Apply common LLM JSON mistakes: trailing commas, unescaped control chars."""
    # Remove trailing commas before } or ]
    text = re.sub(r",\s*([}\]])", r"\1", text)
    # Fix literal control characters inside JSON strings
    result = []
    in_string = False
    escape = False
    for ch in text:
        if escape:
            # Fix invalid JSON escapes (e.g. \s, \e from bash scripts)
            if in_string and ch not in '"\\/bfnrtu':
                result.append("\\")  # double backslash to make literal
            result.append(ch)
            escape = False
            continue
        if ch == "\\":
            escape = True
            result.append(ch)
            continue
        if ch == '"':
            in_string = not in_string
            result.append(ch)
            continue
        if in_string and ord(ch) < 0x20:
            result.append(_CTRL_ESCAPE.get(ch, f"\\u{ord(ch):04x}"))
            continue
        result.append(ch)
    return "".join(result)


def _find_balanced_json(text: str) -> str | None:
    """Find the first balanced { ... } block in text."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _extract_json(text: str) -> str:
    """Extract JSON from LLM response with multiple fallback strategies."""
    stripped = text.strip()

    # Strategy 1: entire response is valid JSON
    try:
        json.loads(stripped)
        return stripped
    except (json.JSONDecodeError, ValueError):
        pass

    # Strategy 2: fenced code block
    m = _JSON_BLOCK_RE.search(text)
    if m:
        candidate = m.group(1).strip()
        try:
            json.loads(candidate)
            return candidate
        except (json.JSONDecodeError, ValueError):
            # Try with repairs
            repaired = _repair_json(candidate)
            try:
                json.loads(repaired)
                return repaired
            except (json.JSONDecodeError, ValueError):
                pass

    # Strategy 3: balanced-brace extraction
    balanced = _find_balanced_json(text)
    if balanced:
        try:
            json.loads(balanced)
            return balanced
        except (json.JSONDecodeError, ValueError):
            repaired = _repair_json(balanced)
            try:
                json.loads(repaired)
                return repaired
            except (json.JSONDecodeError, ValueError):
                pass

    # Strategy 4: repair entire stripped text
    repaired = _repair_json(stripped)
    try:
        json.loads(repaired)
        return repaired
    except (json.JSONDecodeError, ValueError):
        pass

    # Last resort: return best candidate for error reporting
    return balanced or stripped


_SYSTEM_PROMPT = """\
Output ONLY valid JSON. No prose, no markdown, no explanation — just the JSON object.

You are Intern, a code execution agent. You receive a ticket, the current \
contents of a single source file, and a numbered ANCHOR CATALOG extracted from \
that file. Your job is to produce a JSON edit plan.

Rules:
- Your ENTIRE response must be a single JSON object. No text before or after it. \
No markdown fences, no explanation, no comments.
- Each edit uses "anchor_id" to select a pre-extracted anchor from the catalog.
- "action" must be "replace" or "insert_after".
- "verify_command" must be a bare command with NO shell operators. No "cd", no "&&", no ";", no pipes. Examples: "pytest tests/test_foo.py -v" or "test -f docs/foo.md" or "bash tests/test_foo.sh". NEVER "cd /path && pytest".
- Keep edits minimal — only change what the ticket requires.
- Do not add comments, docstrings, or type annotations beyond what the ticket asks for.

Three edit actions:

1. "replace_line" — PREFERRED for small changes. Replace one or a few lines within an anchor.
   - "anchor_id" = the anchor containing the line(s) to change.
   - "old" = the exact line(s) to find within the anchor (must appear exactly once).
   - "new" = the replacement line(s).
   - Use this when changing a constant, a single statement, a return value, or a \
few adjacent lines. Do NOT reproduce the entire anchor.

2. "replace" — REPLACE an entire anchor with new text.
   - "new" = the COMPLETE replacement text for the selected anchor (a modified copy).
   - The "new" field must not exceed 50× the length of the selected anchor.
   - Only use this when the majority of the anchor needs to change.

3. "insert_after" — APPEND new code after an anchor WITHOUT reproducing the anchor.
   - "new" = ONLY the new block to insert. Do NOT include the anchor text.
   - The executor preserves the anchor exactly and inserts your new text after it \
with proper blank-line separation. Do NOT add leading blank lines to "new".
   - Use this when adding a new class, function, or block after existing code.

IMPORTANT: Prefer "replace_line" over "replace" when only 1-3 lines need to change. \
This avoids reproducing large anchor blocks and prevents corruption.

CRITICAL — Anchor selection rules:
- You MUST use anchor IDs from the catalog (e.g. A1, A2, A3...). Do NOT invent IDs.
- Anchor IDs are sequential labels, NOT line numbers. A file with 200 lines may \
only have 17 anchors (A1-A17). Using "A24" when only A1-A17 exist is INVALID.
- For "replace": choose the smallest anchor that contains ALL the code you \
need to change. Check the line range (e.g. A1: L1-34) to find the right anchor.
- For "insert_after": choose the anchor AFTER which the new code should appear \
(typically the last anchor when appending to end of file).
- "new" is always a single STRING, never a list or array.

JSON schema:
{
  "ticket_id": "string",
  "summary": "one-line description of the change",
  "edits": [
    {"file": "path/to/file.py", "action": "replace_line", "anchor_id": "A1", "old": "exact line to find", "new": "replacement line"},
    {"file": "path/to/file.py", "action": "replace", "anchor_id": "A2", "new": "full modified anchor text"},
    {"file": "path/to/file.py", "action": "insert_after", "anchor_id": "A3", "new": "new code block only"}
  ],
  "verify_command": "python -m pytest tests/test_relevant.py -v",
  "confidence": "high|medium|low"
}
"""


@dataclass
class Anchor:
    """A numbered region of the target file for LLM selection."""
    anchor_id: str
    start_line: int
    end_line: int
    text: str


def _looks_like_python(content: str) -> bool:
    """Heuristic: return True if content looks like Python source."""
    if not content.strip():
        return False
    first_line = content.lstrip().split("\n", 1)[0]
    if first_line.startswith(("import ", "from ", "#!/usr/bin/env python")):
        return True
    return "def " in content or "class " in content


def _extract_anchors_treesitter(file_content: str) -> list[Anchor] | None:
    """Extract anchors using tree-sitter Python parser.

    Returns None if tree-sitter is unavailable or parsing fails,
    signaling caller to fall back to regex extraction.
    """
    try:
        import tree_sitter
        import tree_sitter_python
    except ImportError:
        return None

    try:
        lang = tree_sitter.Language(tree_sitter_python.language())
        parser = tree_sitter.Parser(lang)
        tree = parser.parse(file_content.encode("utf-8"))
    except Exception:
        return None

    root = tree.root_node
    if root.type != "module" or root.child_count == 0:
        return None

    # Group top-level children into anchor regions
    #   - function_definition / class_definition / decorated_definition → one anchor each
    #   - consecutive other nodes (imports, assignments, docstrings) → one preamble anchor
    _DEFINITION_TYPES = {"function_definition", "class_definition", "decorated_definition"}

    groups: list[list] = []  # list of lists of child nodes
    current_other: list = []

    for child in root.children:
        if child.type in _DEFINITION_TYPES:
            if current_other:
                groups.append(current_other)
                current_other = []
            groups.append([child])
        else:
            current_other.append(child)

    if current_other:
        groups.append(current_other)

    if not groups:
        return None

    anchors: list[Anchor] = []
    for idx, group in enumerate(groups):
        first = group[0]
        last = group[-1]
        start_byte = first.start_byte
        end_byte = last.end_byte
        text = file_content[start_byte:end_byte]

        # Include any trailing whitespace/newlines up to the next group (or EOF)
        if idx + 1 < len(groups):
            next_start = groups[idx + 1][0].start_byte
        else:
            next_start = len(file_content)
        text = file_content[start_byte:next_start]

        start_line = first.start_point.row + 1  # 1-indexed
        # Count actual lines in the text
        end_line = start_line + text.count("\n") - (1 if text.endswith("\n") else 0)
        if text.count("\n") == 0 and text:
            end_line = start_line

        anchors.append(Anchor(
            anchor_id=f"A{idx + 1}",
            start_line=start_line,
            end_line=end_line,
            text=text,
        ))

    # Coverage check: joined anchor text must exactly equal file content
    if "".join(a.text for a in anchors) != file_content:
        return None

    return anchors


def extract_anchors(
    file_content: str,
    min_lines: int = 5,
    max_lines: int = 15,
) -> list[Anchor]:
    """Split file content into numbered anchor regions.

    Tries tree-sitter structural parsing for Python files first,
    then falls back to regex-based boundary detection and fixed-size chunks.
    """
    if not file_content:
        return []

    # Try tree-sitter for Python files
    if _looks_like_python(file_content):
        ts_result = _extract_anchors_treesitter(file_content)
        if ts_result is not None:
            logger.info("Anchor method: tree-sitter (%d anchors)", len(ts_result))
            return ts_result
        else:
            logger.info("Anchor method: tree-sitter failed, falling back to regex")

    # Original regex-based extraction (fallback)
    lines = file_content.splitlines(keepends=True)
    if not lines:
        return []

    # Find top-level boundary lines (class/def at column 0, or decorators)
    boundaries: list[int] = [0]
    for i, line in enumerate(lines):
        stripped = line.rstrip()
        if i == 0:
            continue
        if re.match(r"^(class |def |@)", stripped):
            # Also include a preceding blank line in the previous chunk
            boundaries.append(i)

    # Remove duplicate or too-close boundaries
    filtered: list[int] = [boundaries[0]]
    for b in boundaries[1:]:
        if b - filtered[-1] >= min_lines:
            filtered.append(b)
    boundaries = filtered

    # Build chunks from boundaries
    chunks: list[tuple[int, int]] = []
    for i, start in enumerate(boundaries):
        end = boundaries[i + 1] if i + 1 < len(boundaries) else len(lines)
        # Split oversized chunks
        while end - start > max_lines:
            chunks.append((start, start + max_lines))
            start += max_lines
        chunks.append((start, end))

    anchors = []
    for idx, (start, end) in enumerate(chunks):
        text = "".join(lines[start:end])
        anchors.append(Anchor(
            anchor_id=f"A{idx + 1}",
            start_line=start + 1,  # 1-indexed
            end_line=end,
            text=text,
        ))

    logger.info("Anchor method: regex (%d anchors)", len(anchors))
    return anchors


def format_anchor_catalog(anchors: list[Anchor]) -> str:
    """Format anchors as a compact index (no full text — file content has line numbers)."""
    if not anchors:
        return "(empty)"
    parts = [f"  [{len(anchors)} anchors — valid IDs: A1 through A{len(anchors)}]"]
    for a in anchors:
        preview = a.text.split("\n", 1)[0].strip()[:60]
        parts.append(f"  {a.anchor_id}: L{a.start_line}-{a.end_line} | {preview}")
    return "\n".join(parts)


def _number_lines(content: str) -> str:
    """Add line numbers to file content for anchor correlation."""
    lines = content.splitlines(keepends=True)
    numbered = []
    for i, line in enumerate(lines, 1):
        numbered.append(f"{i:4d}| {line}")
    return "".join(numbered)


def _build_user_prompt(
    ticket_id: str,
    ticket_body: str,
    file_path: str,
    file_content: str,
    anchor_catalog: str,
) -> str:
    numbered = _number_lines(file_content)
    return (
        f"## Ticket: {ticket_id}\n\n"
        f"{ticket_body}\n\n"
        f"## File: {file_path} (line-numbered)\n\n"
        f"```\n{numbered}```\n\n"
        f"## Anchor catalog (use ONLY these IDs — anchor_id is NOT a line number)\n\n"
        f"{anchor_catalog}\n\n"
        f"Produce the JSON edit plan now. Use anchor IDs from the catalog above (A1, A2, etc.), NOT line numbers."
    )


def parse_edit_plan(
    raw: str,
    ticket_id: str,
    anchors: list[Anchor] | None = None,
) -> EditPlan:
    """Parse LLM response into an EditPlan. Raises PlannerError on failure.

    If anchors are provided, resolves anchor_id references to exact file text.
    Also accepts legacy "old" field for backwards compatibility.
    """
    cleaned = _strip_thinking(raw)
    json_str = _extract_json(cleaned)

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        raise PlannerError(f"LLM response is not valid JSON: {e}\nRaw: {cleaned[:500]}") from e

    if not isinstance(data, dict):
        raise PlannerError(f"Expected JSON object, got {type(data).__name__}")

    # Build anchor lookup
    anchor_map: dict[str, str] = {}
    if anchors:
        for a in anchors:
            anchor_map[a.anchor_id] = a.text

    edits = []
    for i, edit_data in enumerate(data.get("edits", [])):
        if not isinstance(edit_data, dict):
            raise PlannerError(f"Edit {i} is not a JSON object")

        # Require file, action, new
        for key in ("file", "action", "new"):
            if key not in edit_data:
                raise PlannerError(f"Edit {i} missing required key: {key}")

        action = str(edit_data["action"])

        # Resolve old text based on action type
        if action == "replace_line":
            # replace_line: planner provides anchor_id (validation) + old (exact text)
            if "anchor_id" not in edit_data:
                raise PlannerError(f"Edit {i}: replace_line requires anchor_id")
            aid = str(edit_data["anchor_id"]).strip().upper()
            if aid not in anchor_map:
                raise PlannerError(
                    f"Edit {i}: anchor_id '{aid}' not found in catalog. "
                    f"Valid IDs: {', '.join(sorted(anchor_map.keys()))}"
                )
            if "old" not in edit_data:
                raise PlannerError(f"Edit {i}: replace_line requires 'old' field")
            old_text = str(edit_data["old"])
            # Verify the old text actually exists within the anchor.
            # LLMs often mangle quotes/escaping in JSON, so try line-level
            # fuzzy matching as a fallback before rejecting.
            if old_text not in anchor_map[aid]:
                corrected = _fuzzy_match_line(old_text, anchor_map[aid])
                if corrected is not None:
                    logger.info(
                        "Edit %d: fuzzy-matched old text in anchor %s "
                        "(LLM quote/escape mismatch corrected)", i, aid,
                    )
                    old_text = corrected
                else:
                    raise PlannerError(
                        f"Edit {i}: 'old' text not found within anchor {aid}. "
                        f"The 'old' field must be an exact substring of the anchor."
                    )
        elif "anchor_id" in edit_data:
            aid = str(edit_data["anchor_id"]).strip().upper()
            if aid not in anchor_map:
                raise PlannerError(
                    f"Edit {i}: anchor_id '{aid}' not found in catalog. "
                    f"Valid IDs: {', '.join(sorted(anchor_map.keys()))}"
                )
            old_text = anchor_map[aid]
        elif "old" in edit_data:
            old_text = str(edit_data["old"])
        else:
            raise PlannerError(
                f"Edit {i} missing required key: 'anchor_id' (or legacy 'old')"
            )

        # Coerce "new" to string — LLMs occasionally return a list of lines
        new_val = edit_data["new"]
        if isinstance(new_val, list):
            new_val = "\n".join(str(line) for line in new_val)
        else:
            new_val = str(new_val)

        edits.append(Edit(
            file=str(edit_data["file"]),
            action=str(edit_data["action"]),
            old=old_text,
            new=new_val,
        ))

    return EditPlan(
        ticket_id=data.get("ticket_id", ticket_id),
        summary=data.get("summary", ""),
        edits=edits,
        verify_command=str(data.get("verify_command", "")),
        confidence=str(data.get("confidence", "unknown")),
        raw_response=raw,
    )


async def generate_plan(
    ticket_id: str,
    ticket_body: str,
    file_path: str,
    file_content: str,
    base_url: str,
    model: str,
    max_tokens: int = 4096,
    temperature: float = 0,
    timeout: float = 120.0,
    api_key: str = "",
) -> EditPlan:
    """Call LLM to generate an edit plan for a ticket.

    Returns an EditPlan. Raises PlannerError on LLM or parse failure.
    """
    # Resolve profile name from base_url for telemetry
    profile_name = "unknown"
    for pname, pconf in PLANNER_PROFILES.items():
        if pconf["base_url"].rstrip("/") == base_url.rstrip("/"):
            profile_name = pname
            break

    anchors = extract_anchors(file_content)
    if not anchors:
        raise PlannerError(f"Target file is empty, cannot extract anchors: {file_path}")
    anchor_catalog = format_anchor_catalog(anchors)
    logger.info("Extracted %d anchors from %s", len(anchors), file_path)

    user_prompt = _build_user_prompt(
        ticket_id, ticket_body, file_path, file_content, anchor_catalog,
    )

    prompt_chars = len(_SYSTEM_PROMPT) + len(user_prompt)
    logger.info(
        "Planner request: profile=%s, ~%d chars (~%d tokens est), "
        "max_tokens=%d, timeout=%.0fs, model=%s",
        profile_name, prompt_chars, prompt_chars // 4,
        max_tokens, timeout, model,
    )

    # Check profile for json_mode support (xgrammar needs triton, unavailable on aarch64)
    use_json_mode = True
    if profile_name != "unknown":
        use_json_mode = PLANNER_PROFILES[profile_name].get("json_mode", True)

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if use_json_mode:
        payload["response_format"] = {"type": "json_object"}
    # Qwen3 via vLLM: suppress <think> blocks
    if "qwen" in model.lower():
        payload["chat_template_kwargs"] = {"enable_thinking": False}

    t0 = time.monotonic()
    try:
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
            resp = await client.post(f"{base_url}/chat/completions", json=payload)
            resp.raise_for_status()
    except httpx.ConnectError as e:
        elapsed = time.monotonic() - t0
        logger.error(
            "Planner failed: profile=%s, elapsed=%.1fs (budget %.0fs), "
            "~%d tokens est, model=%s: %s",
            profile_name, elapsed, timeout, prompt_chars // 4, model, e,
        )
        raise PlannerError(f"Cannot reach LLM at {base_url}: {e}") from e
    except httpx.TimeoutException as e:
        elapsed = time.monotonic() - t0
        logger.error(
            "Planner failed: profile=%s, elapsed=%.1fs (budget %.0fs), "
            "~%d tokens est, model=%s: %s",
            profile_name, elapsed, timeout, prompt_chars // 4, model, e,
        )
        raise PlannerError(
            f"LLM request timed out after {elapsed:.1f}s (budget {timeout:.0f}s)"
        ) from e
    except httpx.HTTPStatusError as e:
        elapsed = time.monotonic() - t0
        logger.error(
            "Planner failed: profile=%s, elapsed=%.1fs (budget %.0fs), "
            "~%d tokens est, model=%s: HTTP %d",
            profile_name, elapsed, timeout, prompt_chars // 4, model,
            e.response.status_code,
        )
        raise PlannerError(f"LLM error {e.response.status_code}: {e.response.text}") from e
    elapsed = time.monotonic() - t0

    data = resp.json()
    raw_content = data["choices"][0]["message"]["content"]
    usage = data.get("usage", {})

    logger.info(
        "LLM response: %d tokens in %.1fs (budget %.0fs), model=%s, profile=%s",
        usage.get("completion_tokens", 0), elapsed, timeout,
        data.get("model", model), profile_name,
    )

    try:
        plan = parse_edit_plan(raw_content, ticket_id, anchors=anchors)
        plan.raw_response = raw_content
        return plan
    except PlannerError as first_err:
        if "anchor_id" not in str(first_err) or "not found in catalog" not in str(first_err):
            raise  # Not an anchor mismatch — don't retry
        anchor_err_msg = str(first_err)

    # ------------------------------------------------------------------
    # One automatic retry for invalid anchor_id
    # ------------------------------------------------------------------
    valid_ids = ", ".join(a.anchor_id for a in anchors)
    retry_prompt = (
        f"Your previous response referenced an invalid anchor_id.\n"
        f"Error: {anchor_err_msg}\n\n"
        f"VALID anchor IDs for {file_path}:\n{anchor_catalog}\n\n"
        f"Rewrite the JSON edit plan using ONLY these anchor IDs: {valid_ids}\n"
        f"Output ONLY the corrected JSON object."
    )
    logger.warning(
        "Planner used invalid anchor_id, retrying once (valid: %s)", valid_ids
    )

    payload["messages"].append({"role": "assistant", "content": raw_content})
    payload["messages"].append({"role": "user", "content": retry_prompt})

    t1 = time.monotonic()
    try:
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
            resp2 = await client.post(f"{base_url}/chat/completions", json=payload)
            resp2.raise_for_status()
    except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError) as e:
        raise PlannerError(f"Anchor-retry LLM call failed: {e}") from e

    retry_elapsed = time.monotonic() - t1
    data2 = resp2.json()
    raw_retry = data2["choices"][0]["message"]["content"]
    usage2 = data2.get("usage", {})
    logger.info(
        "Anchor-retry response: %d tokens in %.1fs, profile=%s",
        usage2.get("completion_tokens", 0), retry_elapsed, profile_name,
    )

    plan = parse_edit_plan(raw_retry, ticket_id, anchors=anchors)
    plan.raw_response = raw_retry
    return plan
