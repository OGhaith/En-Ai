"""
Clinic system-prompt loader (the "Shared Agent Prompt").

Merges the two DOCX knowledge files into ONE rendered system prompt:
  - Prompt_tower_R_Restructured.docx  -> the agent instruction template
  - Clinic ai q&a - National.docx     -> approved Q&A knowledge ({{clinic_faq}})
and populates every dynamic variable from configs/clinic_runtime.json.

Numbers:
  - render() caches by source file mtimes + config size; rebuilt only on change.
  - The LLM is never handed file paths, docx metadata, or the loader itself —
    only the rendered text.
  - Dynamic variables come ONLY from the config file. Values that are empty stay
    empty (never invented) and are reported so the operator can fill them.
  - {{clinic_faq}} is injected exactly once, verbatim.
  - A short precedence note is appended so the stricter prompt rules
    (GUARDRAILS / PRIVACY / SYSTEM ACCESS / ROUTING) always win over the Q&A
    file's "use only this info" wording and forbidden-phrase leaks.
  - get_system_prompt() falls back to configs/agents/role.txt when no docx is usable.

Used by ws_voice_server.py, voice_server.py (LiveKit), auto_test/auto_test.py and
the /voice_config HTTP endpoint. Stdlib only.
"""

from __future__ import annotations

import json
import os
import re
import threading
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
CONFIG_FILE = ROOT / "configs" / "clinic_runtime.json"
ROLE_FILE = ROOT / "configs" / "agents" / "role.txt"

W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
W_NS_MAP = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}

# Every placeholder the template can contain.
PLACEHOLDER_KEYS = [
    "Agent_name",
    "language",
    "working_hours",
    "clinic_faq",
    "AI_Trigger_Words",
    "screened_transfers",
]
EX_KEYS = ["department", "number", "message"]
SCREEN_KEYS = ["department", "sip_uri", "message", "data_to_collect"]

# PRECEDENCE_NOTE = (
#     "\n\n##OPERATOR NOTE — DOCUMENT PRIORITY (auto-appended)\n"
#     "The clinic_faq Q&A block above is approved knowledge extracted verbatim from "
#     "the clinic's Q&A file and is used TOGETHER WITH the rest of this prompt. If "
#     "any wording in that block conflicts with the rules in this prompt "
#     "(GUARDRAILS, PRIVACY, SYSTEM ACCESS, ROUTING & TRANSFERS, TONE, Language "
#     "policy, conversation rules), the rules in THIS prompt always win. In "
#     "particular, you NEVER have system access: phrases such as \"let me check\", "
#     "\"let me look that up\" or any offer to verify a report/record are forbidden "
#     "even if the Q&A text suggests them — always follow the Medical Records / "
#     "Supervisor routing rules instead. You are still only allowed to answer from "
#     "this prompt and the clinic_faq Q&A block; anything else is transferred."
# )

PRECEDENCE_NOTE = """

## OPERATOR NOTE — DOCUMENT PRIORITY

The approved clinic FAQ is the primary source for answering
general patient questions.

If a patient's question is covered by the approved FAQ,
answer it directly using the approved information.

Do not transfer a caller solely because their question
mentions reports, results, images, email, or the patient portal.

General questions about report turnaround times, delivery
options, and image access should receive the approved FAQ
answer without requiring access to patient records.

Never claim to access, check, retrieve, send, or modify
a patient's personal medical records.

If a caller requests an actual patient-specific status check,
record lookup, or an action requiring system access, follow
the appropriate Medical Records transfer procedure.

If a caller asks whether an electronic delivery option exists,
explain the approved delivery options.

If the caller wants staff to actually send their personal
report or images, follow the appropriate transfer procedure.

All privacy, security, medical advice, and provider
identification rules remain in effect.

"""
_lock = threading.Lock()
_cache: Dict[str, object] = {
    "key": None,
    "prompt": None,
    "meta": None,
}


# ---------------------------------------------------------------------------
# DOCX extraction (stdlib only, mirrors the proven readdocx.py helper)
# ---------------------------------------------------------------------------
def _para_text(p) -> str:
    parts: List[str] = []
    for node in p.iter():
        if node.tag == W_NS + "t":
            parts.append(node.text or "")
        elif node.tag == W_NS + "tab":
            parts.append("\t")
        elif node.tag == W_NS + "br":
            parts.append("\n")
    return "".join(parts).strip()


def _walk(el, lines: List[str]) -> None:
    tag = el.tag.split("}")[-1]
    if tag == "p":
        txt = _para_text(el)
        if txt:
            lines.append(txt)
    elif tag == "tbl":
        lines.append("[TABLE]")
        for tr in el.findall(W_NS + "tr"):
            cells: List[str] = []
            for tc in tr.findall(W_NS + "tc"):
                cell_texts = []
                for p in tc.iter(W_NS + "p"):
                    cell_texts.append(_para_text(p))
                cells.append(" ".join(x for x in cell_texts if x))
            lines.append(" | ".join(cells))
        lines.append("[/TABLE]")
    else:
        for ch in el:
            _walk(ch, lines)


def extract_docx_text(path: Path) -> str:
    """Read a .docx and return its plain text (paragraphs + [TABLE] rows)."""
    with zipfile.ZipFile(str(path)) as z:
        xml_bytes = z.read("word/document.xml")
    root = ET.fromstring(xml_bytes)
    body = root.find("w:body", W_NS_MAP)
    if body is None:
        return ""
    lines: List[str] = []
    for el in body:
        _walk(el, lines)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------
def _load_config() -> Dict:
    if not CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _cfg_str(cfg: Dict, key: str) -> str:
    v = cfg.get(key, "")
    return v if isinstance(v, str) else str(v)


def _cfg_list(cfg: Dict, key: str) -> List[Dict]:
    v = cfg.get(key, [])
    return v if isinstance(v, list) else []


def build_placeholder_map(cfg: Dict, qa_text: str, prompt_template: str) -> Dict[str, str]:
    """Map every {{key}} the template may contain to its value."""
    values = {
        "Agent_name": _cfg_str(cfg, "agent_name"),
        "language": _cfg_str(cfg, "language"),
        "working_hours": _cfg_str(cfg, "working_hours"),
        "clinic_faq": qa_text,
        "AI_Trigger_Words": _cfg_str(cfg, "ai_trigger_words"),
    }
    # screened_transfers: computed from configured screens — never invented.
    screens = _cfg_list(cfg, "screens")
    any_screen = any(
        str(s.get(c, "")).strip() for s in screens for c in ("department", "sip_uri")
    )
    values["screened_transfers"] = "ENABLED" if any_screen else "DISABLED"

    ex_list = _cfg_list(cfg, "ex")
    for i in range(1, 8):
        src = ex_list[i - 1] if i - 1 < len(ex_list) else {}
        for k in EX_KEYS:
            values[f"ex{i}_{k}"] = str(src.get(k, ""))
    for i in range(1, 5):
        src = screens[i - 1] if i - 1 < len(screens) else {}
        for k in SCREEN_KEYS:
            values[f"screen{i}_{k}"] = str(src.get(k, ""))
    return values


# ---------------------------------------------------------------------------
# Cached render
# ---------------------------------------------------------------------------
def _cache_key(cfg: Dict) -> str:
    key_parts: List[str] = ["config", str(CONFIG_FILE), str(cfg)]
    for fname_key, attr in (("prompt_docx", "prompt_docx"), ("qa_docx", "qa_docx")):
        rel = _cfg_str(cfg, fname_key)
        p = ROOT / rel if rel else None
        if p is not None and p.exists():
            try:
                key_parts.append(f"{rel}={os.path.getmtime(p)}")
            except Exception:
                key_parts.append(f"{rel}=?")
        else:
            key_parts.append(f"{rel}=missing")
    return "|".join(key_parts)


def _render(cfg: Dict) -> Tuple[Optional[str], Dict]:
    """Render the combined system prompt; returns (prompt | None, meta)."""
    prompt_rel = _cfg_str(cfg, "prompt_docx")
    qa_rel = _cfg_str(cfg, "qa_docx")
    prompt_path = ROOT / prompt_rel if prompt_rel else None
    qa_path = ROOT / qa_rel if qa_rel else None

    meta: Dict = {
        "ok": False,
        "reason": "",
        "prompt_source": None,
        "qa_injected": False,
        "unset_vars": [],
        "leftover_placeholders": [],
        "prompt_chars": 0,
        "qa_chars": 0,
    }

    if prompt_path is None or not prompt_path.exists():
        meta["reason"] = f"prompt docx missing: {prompt_rel or '(unset)'}"
        return None, meta
    template = extract_docx_text(prompt_path)
    if not template.strip():
        meta["reason"] = f"prompt docx empty: {prompt_rel}"
        return None, meta

    qa_text = ""
    if qa_path is not None and qa_path.exists():
        qa_text = extract_docx_text(qa_path).strip()

    values = build_placeholder_map(cfg, qa_text, template)

    def _fill(m: re.Match) -> str:
        key = m.group(1)
        return values.get(key, "")

    rendered = re.sub(r"\{\{([A-Za-z0-9_]+)\}\}", _fill, template)

    # Track unset dynamic variables that actually appear in the template.
    template_keys = set(re.findall(r"\{\{([A-Za-z0-9_]+)\}\}", template))
    unset = []
    for key in sorted(template_keys):
        v = values.get(key, "")
        if isinstance(v, str) and v.strip() == "":
            unset.append(key)
    # Any remaining {{...}} that were not dynamic vars at all.
    leftover = list(set(re.findall(r"\{\{[A-Za-z0-9_]+\}\}", rendered)))

    full = rendered + PRECEDENCE_NOTE
    meta.update(
        {
            "ok": True,
            "reason": "",
            "prompt_source": prompt_rel,
            "qa_injected": bool(qa_text),
            "unset_vars": unset,
            "leftover_placeholders": leftover,
            "prompt_chars": len(full),
            "qa_chars": len(qa_text),
        }
    )
    return full, meta


def render(force: bool = False) -> Tuple[Optional[str], Dict]:
    """Thread-safe cached render of the clinic system prompt."""
    global _cache
    cfg = _load_config()
    key = _cache_key(cfg)
    with _lock:
        if not force and _cache["key"] == key and _cache["prompt"] is not None:
            return _cache["prompt"], _cache["meta"]
        prompt, meta = _render(cfg)
        if prompt is None:
            return None, meta
        _cache = {"key": key, "prompt": prompt, "meta": meta}
        return prompt, meta


def get_system_prompt(force: bool = False) -> str:
    """Return the clinic system prompt, or the role.txt fallback if unusable."""
    prompt, meta = render(force)
    if prompt is not None:
        return prompt
    if ROLE_FILE.exists():
        return ROLE_FILE.read_text(encoding="utf-8").strip()
    return ""


def clinic_enabled() -> bool:
    prompt, _ = render()
    return prompt is not None


# ---------------------------------------------------------------------------
# Voice-config meta (safe for caller-facing endpoints)
# ---------------------------------------------------------------------------
def voice_config() -> Dict:
    """Safe meta for the front-end; never contains prompt text or file paths."""
    _, meta = render()
    cfg = _load_config()
    return {
        "agent": _cfg_str(cfg, "agent_name") or "Tower Radiology at National Harbor",
        "enabled": meta.get("ok", False),
        "prompt_active": meta.get("ok", False),
        "qa_injected": meta.get("qa_injected", False),
        "unset_vars": meta.get("unset_vars", []),
        "leftover_placeholders": meta.get("leftover_placeholders", []),
        "prompt_chars": meta.get("prompt_chars", 0),
        "model_env": os.getenv("OLLAMA_MODEL", ""),
    }


# ---------------------------------------------------------------------------
# num_ctx
# ---------------------------------------------------------------------------
def recommended_num_ctx(base_default: int = 2048) -> int:
    """Estimate a safe context window for the rendered clinic prompt.

    Honours an explicit CLINIC_NUM_CTX override (safety valve for the RTX 5060
    8 GB). Otherwise returns max(base_default, est_prompt_tokens + 1024 slack).
    """
    override = os.getenv("CLINIC_NUM_CTX")
    if override:
        try:
            return max(1024, int(override))
        except Exception:
            pass
    prompt, meta = render()
    if prompt is None:
        return base_default
    est_tokens = max(1, meta.get("prompt_chars", 1) // 4)
    return max(base_default, est_tokens + 1024)