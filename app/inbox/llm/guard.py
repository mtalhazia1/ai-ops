"""Plain-code safety checks around the LLM (Sections 8.2, 8.3 and 12).

- `injection_keywords`: a simple keyword check on the incoming email, logged next to the
  classifier's own `contains_instructions_to_ai` flag. Either one routes to review.
- `enforce_evidence`: drops extracted values that have no supporting quote in the email.
- `check_reply`: blocks drafts with new addresses or links, money amounts, or excess length.
"""

from __future__ import annotations

import re
from typing import Any

from .. import containers

_INJECTION_PATTERNS = [
    r"ignore (all |any |the )?(previous|prior|above|earlier) (instructions|prompts?|rules)",
    r"disregard (all |any |the )?(previous|prior|above|earlier|your) (instructions|prompts?|rules)",
    r"forget (all |your |the )?(previous |prior )?(instructions|rules)",
    r"\byou are now\b",
    r"\bact as (an? )?(ai|assistant|system|admin)",
    r"^\s*(system|assistant)\s*:",
    r"\[\s*(system|inst)\s*\]",
    r"<\s*/?\s*(system|instructions?)\s*>",
    r"\b(new|updated|override) (system )?instructions\b",
    r"\b(ai|assistant|bot|model|llm|gpt|claude)\b[^.\n]{0,40}\b(must|should|will|shall)\b[^.\n]{0,40}\b(classify|reply|forward|send|ignore)",
    r"\bclassify (this|the) (email|message) as\b",
    r"\bprompt injection\b",
    r"\bjailbreak\b",
    r"\b(reveal|print|output|show) (your|the) (system prompt|instructions)",
    r"\bforward (all|every) (invoices?|emails?|documents?|shipment data)\b",
    r"\breply with (all|every|the full)\b",
]
_INJECTION_RE = [re.compile(p, re.IGNORECASE | re.MULTILINE) for p in _INJECTION_PATTERNS]


def injection_keywords(*texts: str) -> list[str]:
    """Return the matched snippets (empty list = nothing suspicious)."""
    hits: list[str] = []
    for text in texts:
        for pattern in _INJECTION_RE:
            match = pattern.search(text or "")
            if match:
                hits.append(match.group(0).strip()[:80])
    return hits


# --- Evidence -----------------------------------------------------------------------

def _norm(text: str) -> str:
    text = (text or "").lower()
    text = re.sub(r"[‘’“”\"'`]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _compact(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def evidence_found(evidence: str | None, haystack: str) -> bool:
    """True when the quote appears in the email text, tolerating whitespace, case,
    quote marks and small paraphrases (most of its words present)."""
    if not evidence or not evidence.strip():
        return False
    ev, hay = _norm(evidence).strip(" .,:;-"), _norm(haystack)
    if ev and ev in hay:
        return True
    if _compact(evidence) and _compact(evidence) in _compact(haystack):
        return True
    words = [w for w in re.findall(r"[a-z0-9]+", ev) if len(w) > 1]
    if not words:
        return False
    hay_words = set(re.findall(r"[a-z0-9]+", hay))
    return sum(w in hay_words for w in words) / len(words) >= 0.8


# List fields whose items are identifiers that must literally appear in the email.
_IDENTIFIER_LISTS = {"container_numbers", "bl_numbers", "po_numbers", "reference_numbers"}


def enforce_evidence(fields: dict[str, Any], evidence: dict[str, Any], haystack: str) -> tuple[dict[str, Any], list[str]]:
    """Null out values without evidence found in the email. Returns (fields, flags)."""
    cleaned: dict[str, Any] = {}
    flags: list[str] = []
    hay_compact = _compact(haystack)
    for name, value in fields.items():
        if isinstance(value, list):
            if name in _IDENTIFIER_LISTS:
                kept = [v for v in value if _compact(str(v)) and _compact(str(v)) in hay_compact]
                if len(kept) != len(value):
                    flags.append(f"dropped_unquoted:{name}")
                cleaned[name] = kept
            elif value and not evidence_found(evidence.get(name), haystack):
                flags.append(f"no_evidence:{name}")
                cleaned[name] = []
            else:
                cleaned[name] = value
            continue
        if value is None:
            cleaned[name] = None
        elif evidence_found(evidence.get(name), haystack):
            cleaned[name] = value
        else:
            flags.append(f"no_evidence:{name}")
            cleaned[name] = None
    return cleaned, flags


def container_flags(fields: dict[str, Any]) -> list[str]:
    return [f"invalid_container:{c}" for c in fields.get("container_numbers") or [] if not containers.is_valid(c)]


# --- Reply output checks --------------------------------------------------------------

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>()\"']+|\b[\w-]+(?:\.[\w-]+)*\.(?:com|net|org|io|pk|co|info|biz|example)(?:/[^\s<>()\"']*)?\b", re.IGNORECASE)
CURRENCY_RE = re.compile(
    r"(?:[$€£¥₨]\s?\d)|(?:\b(?:USD|PKR|EUR|GBP|AED|Rs\.?|Rupees?)\s?\d)|(?:\d[\d,.]*\s?(?:USD|PKR|EUR|GBP|AED|dollars|rupees|Rs)\b)",
    re.IGNORECASE,
)
MAX_REPLY_WORDS = 180  # brief: "about 150 words"; a little slack for the signature


def check_reply(body: str, *, allowed_sources: list[str]) -> list[str]:
    """Return a list of problems; empty means the draft may go to approval."""
    problems: list[str] = []
    allowed = " ".join(allowed_sources).lower()
    for address in set(EMAIL_RE.findall(body)):
        if address.lower() not in allowed:
            problems.append(f"new email address: {address}")
    without_emails = EMAIL_RE.sub(" ", body)
    for url in set(URL_RE.findall(without_emails)):
        if url.lower().rstrip("/.") not in allowed:
            problems.append(f"new link: {url}")
    if CURRENCY_RE.search(body):
        problems.append("contains a money amount")
    words = len(body.split())
    if words > MAX_REPLY_WORDS:
        problems.append(f"too long: {words} words")
    return problems
