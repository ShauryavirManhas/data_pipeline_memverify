"""One-shot verifier. A reply that is not the required JSON is a no."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from memverify.agent import Complete, call_model, litellm_complete
from memverify.config import RunConfig, usd
from memverify.types import MemoryEntry

WINDOW_LINES = 60
_PARSE_REASON = "output could not be parsed"
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")


@dataclass(frozen=True)
class VerifierVerdict:
    decision: str
    reason: str
    tokens_in: int
    tokens_out: int
    cost_usd: float


def verify_write(
    config: RunConfig,
    *,
    issue_text: str,
    diagnosis: str,
    patch: str,
    complete: Complete | None = None,
) -> VerifierVerdict:
    prompt = fill(
        config.verifier.write_prompt,
        {"issue_text": issue_text, "diagnosis": diagnosis, "patch": patch},
    )
    return _call(config, prompt, complete)


def verify_read(
    config: RunConfig,
    *,
    issue_text: str,
    hint: MemoryEntry,
    file_contents: str,
    complete: Complete | None = None,
) -> VerifierVerdict:
    prompt = fill(
        config.verifier.read_prompt,
        {
            "issue_text": issue_text,
            "hint_issue_text": hint.issue_text,
            "diagnosis": hint.diagnosis,
            "patch": hint.patch,
            "file_contents": file_contents,
        },
    )
    return _call(config, prompt, complete)


def parse_verifier_output(text: str) -> tuple[str, str]:
    """Return ``(decision, reason)``. Invalid output is ``("no", "output could not be parsed")``."""
    stripped = _strip_one_fence(text.strip())
    try:
        loaded = json.loads(stripped)
    except json.JSONDecodeError:
        return "no", _PARSE_REASON
    if not isinstance(loaded, dict) or set(loaded) != {"decision", "reason"}:
        return "no", _PARSE_REASON
    decision = loaded["decision"]
    reason = loaded["reason"]
    if decision not in {"yes", "no"} or not isinstance(reason, str):
        return "no", _PARSE_REASON
    return decision, reason


def build_file_contents(checkout: Path, hint: MemoryEntry, config: RunConfig) -> str:
    """Windows of about 60 lines around remembered hunks, with a prefix fallback.

    Line numbers come from the remembered patch and may have shifted in
    ``checkout``. Each file is capped, then the joined text is capped.
    """
    hunks = hunks_by_file(hint.patch)
    sections = []
    per_file = config.verifier.max_file_chars
    for rel in hint.files_touched:
        text = _file_text(checkout, rel, hunks)
        sections.append(f"----- {rel} -----\n{truncate(text, per_file)}\n")
    return truncate("".join(sections), config.verifier.max_file_contents_chars)


def hunks_by_file(patch: str) -> dict[str, list[tuple[int, int]]] | None:
    """Map a repo path to ``(new_start, new_count)`` hunks.

    ``None`` means the patch has no unified-diff file headers, so every
    file falls back to a prefix.
    """
    if "diff --git " not in patch:
        return None
    found: dict[str, list[tuple[int, int]]] = {}
    current: str | None = None
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            current = _path_from_diff_header(line)
            found.setdefault(current, [])
            continue
        if current is None:
            continue
        match = _HUNK.match(line)
        if not match:
            continue
        start = int(match.group(1))
        count = 1 if match.group(2) is None else int(match.group(2))
        found[current].append((start, count))
    return found


def truncate(text: str, limit: int) -> str:
    """Keep the prefix and append a visible marker when ``text`` is longer than ``limit``."""
    if limit < 0:
        raise ValueError("limit must be >= 0")
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    return f"{text[:limit]}\n<truncated, {omitted} characters omitted>\n"


def fill(template: str, fields: dict[str, str]) -> str:
    """Replace ``{name}`` placeholders in the template once."""

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in fields:
            return fields[name]
        return match.group(0)

    return _PLACEHOLDER.sub(replace, template)


def _call(config: RunConfig, prompt: str, complete: Complete | None) -> VerifierVerdict:
    caller = complete or litellm_complete
    text, tokens_in, tokens_out = call_model(
        caller,
        model=config.verifier.model_name,
        messages=[{"role": "user", "content": prompt}],
        temperature=config.verifier.temperature,
        seed=config.seed,
        num_retries=config.num_retries,
    )
    decision, reason = parse_verifier_output(text)
    cost = usd(
        tokens_in,
        tokens_out,
        config.verifier.price_in_per_1m,
        config.verifier.price_out_per_1m,
    )
    return VerifierVerdict(decision, reason, tokens_in, tokens_out, cost)


def _strip_one_fence(text: str) -> str:
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    body = lines[1:]
    if body and body[-1].strip().startswith("```"):
        body = body[:-1]
    return "\n".join(body).strip()


def _file_text(
    checkout: Path,
    rel: str,
    hunks: dict[str, list[tuple[int, int]]] | None,
) -> str:
    raw = _read_repo_file(checkout, rel)
    if raw is None:
        return "<file not present>"
    file_hunks = None if hunks is None else hunks.get(rel)
    if not file_hunks:
        return raw
    lines = raw.splitlines()
    if not lines:
        return (
            "<approximate window, remembered hunks could not be placed "
            "in an empty file; line numbers may have shifted>"
        )
    parts = []
    for start, end, anchor in _merge_windows(lines, file_hunks):
        body = "\n".join(lines[start - 1 : end])
        parts.append(
            f"<approximate window around remembered hunk near line {anchor}; "
            f"showing current lines {start}-{end}, which may have shifted>\n{body}"
        )
    return "\n\n".join(parts)


def _window_for_hunk(n_lines: int, start: int, count: int) -> tuple[int, int]:
    """1-based inclusive window of about ``WINDOW_LINES`` around a hunk."""
    if n_lines <= 0:
        return 1, 0
    if count <= 0:
        anchor = min(max(start, 1), n_lines)
        half = WINDOW_LINES // 2
        win_start = max(1, anchor - half)
        win_end = min(n_lines, win_start + WINDOW_LINES - 1)
        win_start = max(1, win_end - WINDOW_LINES + 1)
        return win_start, win_end
    hunk_start = min(max(start, 1), n_lines)
    hunk_end = min(n_lines, max(hunk_start, start + count - 1))
    span = hunk_end - hunk_start + 1
    if span >= WINDOW_LINES:
        return hunk_start, hunk_end
    pad = WINDOW_LINES - span
    before = pad // 2
    win_start = max(1, hunk_start - before)
    win_end = min(n_lines, hunk_end + (pad - before))
    win_start = max(1, win_end - WINDOW_LINES + 1)
    return win_start, win_end


def _merge_windows(
    lines: list[str],
    hunks: list[tuple[int, int]],
) -> list[tuple[int, int, int]]:
    """Merge overlapping windows. Each item is ``(start, end, hunk_anchor)``."""
    windows = []
    for start, count in hunks:
        win_start, win_end = _window_for_hunk(len(lines), start, count)
        if win_end < win_start:
            continue
        windows.append((win_start, win_end, start))
    windows.sort()
    merged: list[tuple[int, int, int]] = []
    for start, end, anchor in windows:
        if not merged or start > merged[-1][1] + 1:
            merged.append((start, end, anchor))
        else:
            prev_start, prev_end, prev_anchor = merged[-1]
            merged[-1] = (prev_start, max(prev_end, end), prev_anchor)
    return merged


def _read_repo_file(checkout: Path, rel: str) -> str | None:
    root = checkout.resolve()
    path = (checkout / rel).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        return None
    return path.read_text(encoding="utf-8", errors="replace")


def _path_from_diff_header(line: str) -> str:
    rest = line[len("diff --git ") :]
    marker = " b/"
    if marker in rest:
        _left, right = rest.split(marker, 1)
        return right.replace("\\", "/")
    parts = rest.split()
    name = parts[-1] if parts else ""
    return name[2:] if name.startswith("b/") else name
