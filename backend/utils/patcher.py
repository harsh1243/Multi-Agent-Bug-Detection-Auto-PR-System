"""Surgical code patching via anchored SEARCH/REPLACE edits.

This is the single source of truth for turning an LLM's proposed change into a
new file. Unlike whole-file regeneration (which rewrites and hallucinates large
files), each edit must match an exact existing snippet exactly once, so only the
intended regions change and diffs stay minimal.

LLM edit format (one or more blocks):

    <<<<<<< SEARCH
    <exact existing code>
    =======
    <replacement code>
    >>>>>>> REPLACE
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field


SEARCH_MARK = "<<<<<<< SEARCH"
DIVIDER = "======="
REPLACE_MARK = ">>>>>>> REPLACE"

# Tolerant block matcher: captures SEARCH body and REPLACE body. The divider and
# end marker are matched loosely (>=5 of the marker char) to survive minor LLM drift.
_BLOCK_RE = re.compile(
    r"<{5,}\s*SEARCH\s*\n(.*?)\n={5,}\s*\n(.*?)\n>{5,}\s*REPLACE",
    re.DOTALL,
)


@dataclass
class Edit:
    """A single anchored search/replace edit."""
    search: str
    replace: str


@dataclass
class ApplyResult:
    """Outcome of applying a set of edits to a file."""
    ok: bool = False
    new_content: str = ""
    applied: int = 0
    total: int = 0
    errors: list[str] = field(default_factory=list)


def parse_edits(text: str) -> list[Edit]:
    """Extract all SEARCH/REPLACE edit blocks from an LLM response."""
    edits: list[Edit] = []
    for m in _BLOCK_RE.finditer(text or ""):
        search = m.group(1)
        replace = m.group(2)
        # Strip a single trailing newline artifact but preserve internal structure.
        edits.append(Edit(search=search, replace=replace))
    return edits


def _normalize_eol(s: str) -> str:
    return s.replace("\r\n", "\n").replace("\r", "\n")


def apply_edits(original: str, edits: list[Edit]) -> ApplyResult:
    """Apply edits sequentially. Each SEARCH must match exactly once.

    An edit whose SEARCH is missing, or appears more than once, is rejected with
    a reason and skipped (never applied ambiguously). The result is ``ok`` only
    when at least one edit applied and the output passes ``sanity_check``.
    """
    content = _normalize_eol(original)
    result = ApplyResult(new_content=content, total=len(edits))

    if not edits:
        result.errors.append("No SEARCH/REPLACE edits found in the model output.")
        return result

    for i, edit in enumerate(edits, start=1):
        search = _normalize_eol(edit.search)
        replace = _normalize_eol(edit.replace)

        if search == "":
            result.errors.append(f"Edit {i}: empty SEARCH block — skipped.")
            continue

        count = content.count(search)
        if count == 0:
            # Retry with whitespace-flexible matching before giving up.
            flexible = _flexible_find(content, search)
            if flexible is None:
                snippet = search.strip().splitlines()[0][:80] if search.strip() else ""
                result.errors.append(
                    f"Edit {i}: SEARCH text not found (near: {snippet!r})."
                )
                continue
            start, end = flexible
            content = content[:start] + replace + content[end:]
            result.applied += 1
            continue
        if count > 1:
            snippet = search.strip().splitlines()[0][:80] if search.strip() else ""
            result.errors.append(
                f"Edit {i}: SEARCH text is ambiguous — matches {count} places (near: {snippet!r}). "
                f"Add more surrounding context to make it unique."
            )
            continue

        content = content.replace(search, replace, 1)
        result.applied += 1

    result.new_content = content
    sane, reason = sanity_check(original, content)
    if not sane:
        result.errors.append(reason)
        result.ok = False
    else:
        result.ok = result.applied > 0 and content != _normalize_eol(original)
        if result.applied == 0 and not result.errors:
            result.errors.append("No edits applied.")
    return result


def _flexible_find(haystack: str, needle: str) -> tuple[int, int] | None:
    """Find ``needle`` in ``haystack`` ignoring trailing-whitespace differences.

    Matches line-by-line with each line right-stripped, so the model getting a
    line's trailing spaces wrong doesn't break an otherwise-correct anchor.
    Returns (start, end) character offsets of the match in the original haystack,
    or None. Requires a unique match.
    """
    h_lines = haystack.split("\n")
    n_lines = [ln.rstrip() for ln in needle.split("\n")]
    if not n_lines:
        return None

    # Precompute character offsets for the start of each haystack line.
    offsets = []
    pos = 0
    for ln in h_lines:
        offsets.append(pos)
        pos += len(ln) + 1  # +1 for the '\n'

    matches: list[tuple[int, int]] = []
    last = len(h_lines) - len(n_lines)
    for i in range(0, max(0, last) + 1):
        if all(h_lines[i + j].rstrip() == n_lines[j] for j in range(len(n_lines))):
            start = offsets[i]
            end_line = i + len(n_lines) - 1
            end = offsets[end_line] + len(h_lines[end_line])
            matches.append((start, end))

    if len(matches) == 1:
        return matches[0]
    return None


def sanity_check(old: str, new: str) -> tuple[bool, str]:
    """Backstop against catastrophic edits (the whole-file-rewrite failure mode)."""
    if new is None or new.strip() == "":
        return False, "Patch produced an empty file — rejected."

    old_lines = _normalize_eol(old).split("\n")
    new_lines = _normalize_eol(new).split("\n")

    # Ratio of changed lines via difflib opcodes.
    sm = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    changed = sum(
        max(i2 - i1, j2 - j1)
        for tag, i1, i2, j1, j2 in sm.get_opcodes()
        if tag != "equal"
    )
    denom = max(len(old_lines), 1)
    ratio = changed / denom
    # A surgical fix should not rewrite most of the file.
    if ratio > 0.60 and len(old_lines) > 25:
        return False, (
            f"Patch changes {ratio:.0%} of the file ({changed}/{denom} lines) — "
            f"rejected as a likely full-file rewrite. Produce minimal targeted edits."
        )
    return True, ""


def unified_diff(old: str, new: str, path: str) -> str:
    """Render a unified diff for the PR body."""
    diff = difflib.unified_diff(
        _normalize_eol(old).splitlines(keepends=True),
        _normalize_eol(new).splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        n=3,
    )
    return "".join(diff)


def diff_stats(old: str, new: str) -> tuple[int, int]:
    """Return (additions, deletions) between old and new content."""
    adds = dels = 0
    for line in difflib.unified_diff(
        _normalize_eol(old).splitlines(), _normalize_eol(new).splitlines(), lineterm=""
    ):
        if line.startswith("+") and not line.startswith("+++"):
            adds += 1
        elif line.startswith("-") and not line.startswith("---"):
            dels += 1
    return adds, dels
