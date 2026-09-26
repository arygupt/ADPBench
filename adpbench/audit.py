"""Static checks on a submitted Verilog file, run before anything is scored.

The audit rejects constructs that could make a design behave differently in
simulation than after synthesis, or let it read the expected answers. It also
records (but allows) constructs that usually just mean the design is confused.

Only executable code is checked: comments and string literals are blanked
out first, so a word like `initial` inside a comment is not a violation.
"""

from __future__ import annotations

import re

MAX_REPORTED = 20

# (regex, reason) pairs. Any match rejects the submission.
VIOLATIONS = (
    (r"\$readmem", "file-based memory load: not synthesizable, reads external data"),
    (r"\$f(?:open|close|scanf|gets|read|eof)", "file I/O: not synthesizable"),
    (r"\$(?:display|write|monitor)", "simulator output: not synthesizable"),
    (r"\$(?:finish|stop)", "halts the simulator"),
    (r"\$(?:dumpfile|dumpvars|dumpflush)", "waveform dumping: not synthesizable"),
    (r"\binitial\b", "initial block: simulates but does not synthesize"),
    (r"#\s*\d", "delay control: not synthesizable"),
    (r"\b(?:force|release)\b", "force/release: not synthesizable"),
    (r"`include", "must be a single self-contained file"),
    (r"\btb\s*\.", "references the testbench hierarchy"),
    (r"\bu_dut\s*\.", "references its own instance path"),
)

# (regex, reason) pairs. Matches are recorded but do not reject.
WARNINGS = (
    (r"\b(?:assert|assume|cover)\b", "SystemVerilog assertion: ignored by synthesis"),
    (r"\b(?:real|shortreal|time)\b", "non-synthesisable data type"),
    (r"\bwait\b", "wait statement: check it is synthesisable"),
)


def audit_submission(text: str) -> dict:
    """Check a submission. Returns {"ok", "violations", "warnings"}.

    Each violation or warning is {"line", "match", "reason"}; `line` is 1-based,
    or 0 for a whole-file problem such as a missing `module dut`.
    """
    code = mask_noncode(text)
    violations = _find_matches(VIOLATIONS, code)
    warnings = _find_matches(WARNINGS, code)

    if not re.search(r"\bmodule\s+dut\b", code):
        violations.append(
            {"line": 0, "match": "module dut", "reason": "no module named `dut` found"}
        )

    return {
        "ok": not violations,
        "violations": violations[:MAX_REPORTED],
        "warnings": warnings[:MAX_REPORTED],
    }


def _find_matches(patterns: tuple, code: str) -> list[dict]:
    hits = []
    for pattern, reason in patterns:
        for match in re.finditer(pattern, code):
            line = code.count("\n", 0, match.start()) + 1
            hits.append({"line": line, "match": match.group(0), "reason": reason})
    hits.sort(key=lambda hit: hit["line"])
    return hits


def mask_noncode(text: str) -> str:
    """Replace comments and string literals with spaces.

    Newlines are kept, so line numbers in the masked text match the original.
    """
    chars = list(text)
    i = 0
    while i < len(text):
        if text.startswith("//", i):
            end = _end_of_line_comment(text, i)
        elif text.startswith("/*", i):
            end = _end_of_block_comment(text, i)
        elif text[i] == '"':
            end = _end_of_string(text, i)
        else:
            i += 1
            continue

        for j in range(i, end):
            if chars[j] != "\n":
                chars[j] = " "
        i = end
    return "".join(chars)


def _end_of_line_comment(text: str, start: int) -> int:
    """Index of the newline that ends a `//` comment (or end of text)."""
    newline = text.find("\n", start)
    return len(text) if newline == -1 else newline


def _end_of_block_comment(text: str, start: int) -> int:
    """Index just past the `*/` that closes a `/*` comment (or end of text)."""
    close = text.find("*/", start + 2)
    return len(text) if close == -1 else close + 2


def _end_of_string(text: str, start: int) -> int:
    """Index just past the closing quote of a string literal (or end of text)."""
    i = start + 1
    while i < len(text):
        if text[i] == "\\":
            i += 2  # skip the escaped character, which may be a quote
        elif text[i] == '"':
            return i + 1
        else:
            i += 1
    return len(text)
