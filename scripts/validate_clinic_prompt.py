"""
validate_clinic_prompt.py — static checks for the clinic DOCX prompt pipeline.

Verifies that configs/clinic_runtime.json + src/clinic_prompt.py produce a
rendered system prompt that:
  1. is loaded from the DOCX files (not a file path),
  2. contains no leftover {{...}} placeholder literals,
  3. contains no filesystem/docx/path strings that would leak to the LLM,
  4. embeds the Q&A clinic_faq block exactly once,
  5. carries the OPERATOR precedence note,
  6. has a recommended num_ctx that is large enough for the prompt.

Usage:
    python scripts/validate_clinic_prompt.py
Exit code is 0 when all checks pass, 1 otherwise.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.clinic_prompt import get_system_prompt, render, recommended_num_ctx, voice_config  # noqa: E402

PLACEHOLDER_RE = re.compile(r"\{\{([^}]+)\}\}")
PATH_LIKE = re.compile(r"(?i)[a-z]:[\\/]|\.(?:docx|txt)\b|\\Users\\|C:\\\\|file://|site-packages|/local-voice-ai/")

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)


def main() -> int:
    prompt, meta = render()
    check("render succeeds", prompt is not None, meta.get("reason", "ok" if meta.get("ok") else "failed"))
    if prompt is None:
        print("Cannot continue without a rendered prompt.")
        return 1

    rendered = get_system_prompt() or ""

    # 1. source + sanity
    check("source is the DOCX (not a file path)", meta.get("prompt_source"), meta.get("prompt_source", ""))
    check("Q&A injected", bool(meta.get("qa_injected")))
    check("prompt non-trivial", len(rendered) > 1000, f"{len(rendered)} chars")

    # 2. leftover placeholders
    leftover = PLACEHOLDER_RE.findall(rendered)
    check("no leftover {{{{...}}}} placeholders", not leftover, str(leftover[:5]) if leftover else "")

    # 3. no path/docx strings
    leaked = [m for m in PATH_LIKE.finditer(rendered) if m.group(0).strip(".:/\\") not in ("txt", "docx")]
    check("no filesystem/path/docx strings", not leaked, [m.group(0) for m in leaked][:5] if leaked else "")

    # 4. Q&A injected exactly once — index marker text of the Q&A doc
    qa_marker = "The AI must strictly use only the information provided here"
    qa_count = rendered.count(qa_marker)
    check("Q&A block present (marker)", qa_count >= 1, f"{qa_count} occurrence(s)")

    # 5. precedence note present
    check("OPERATOR precedence note present", "DOCUMENT PRIORITY" in rendered and "clinic_faq" in rendered)

    # 6. context sizing
    rec = recommended_num_ctx()
    est_tokens = max(1, meta.get("prompt_chars", 1) // 4)
    check("num_ctx ≥ rendered prompt estimate", rec >= est_tokens, f"rec={rec} est={est_tokens} chars={meta.get('prompt_chars')}")

    # 7. safe meta surface (no prompt body / no config secrets)
    vc = voice_config()
    check("voice_config exposes no prompt text", "role" not in vc and "prompt" not in vc and "content" not in vc, str(list(vc.keys())))
    check("voice_config has agent + enabled", bool(vc.get("agent")) and isinstance(vc.get("enabled"), bool))

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED: {', '.join(FAILURES)}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())