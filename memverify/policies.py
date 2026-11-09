"""The four memory policies. Retrieval is skipped entirely for ``none``."""

from __future__ import annotations

from memverify.types import MemoryEntry

POLICIES_THAT_READ = ("memory_no_check", "write_check", "read_check")
POLICIES_THAT_SAVE = ("memory_no_check", "write_check", "read_check")


def reads_memory(policy: str) -> bool:
    return policy in POLICIES_THAT_READ


def saves_entries(policy: str) -> bool:
    return policy in POLICIES_THAT_SAVE


def format_hint(entry: MemoryEntry, prefix: str) -> str:
    return (
        f"{prefix}\n"
        f"Issue: {entry.issue_text}\n"
        f"Diagnosis: {entry.diagnosis}\n"
        f"Patch:\n"
        f"{entry.patch}"
    )


def compose_prompt(issue_text: str, hint_block: str | None) -> str:
    """Issue text, then a blank line and the hint when one is injected."""
    if hint_block is None:
        return issue_text
    return f"{issue_text}\n\n{hint_block}"


def should_save(policy: str, patch: str, write_decision: str | None) -> bool:
    """True only when a row should be inserted.

    An empty patch never saves. ``none`` never saves. ``write_check``
    saves only when the verifier said yes. The other memory policies
    save a non-empty patch with no further check.
    """
    if not patch.strip() or not saves_entries(policy):
        return False
    if policy == "write_check":
        return write_decision == "yes"
    return True
