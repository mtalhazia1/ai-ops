"""Turn a raw email body into the text the LLM should read.

Removes quoted reply history and signatures. Forwarded messages are kept, because
the forwarded content is usually the actual request.
"""

import html
import re

# A line that starts the quoted history of a reply.
_REPLY_HEADER_PATTERNS = [
    re.compile(r"^\s*On .{3,200}wrote:\s*$", re.IGNORECASE),
    re.compile(r"^\s*On .{3,200}$", re.IGNORECASE),  # Gmail sometimes wraps "wrote:" onto the next line
    re.compile(r"^\s*-{2,}\s*Original Message\s*-{2,}\s*$", re.IGNORECASE),
    re.compile(r"^\s*_{10,}\s*$"),  # Outlook separator line
    re.compile(r"^\s*From:\s.+$", re.IGNORECASE),  # Outlook header block ("From: ... Sent: ...")
]
_FORWARD_MARKER = re.compile(r"^\s*-{2,}\s*Forwarded message\s*-{2,}\s*$|^\s*Begin forwarded message:\s*$", re.IGNORECASE)
_SIGNATURE_PATTERNS = [
    re.compile(r"^--\s*$"),
    re.compile(r"^\s*Sent from my (iPhone|iPad|Android|Galaxy|mobile).*$", re.IGNORECASE),
    re.compile(r"^\s*Get Outlook for .*$", re.IGNORECASE),
]


def html_to_text(raw: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", "", raw)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text)


def _is_reply_header(lines: list[str], i: int) -> bool:
    line = lines[i]
    if _REPLY_HEADER_PATTERNS[0].match(line) or _REPLY_HEADER_PATTERNS[2].match(line) or _REPLY_HEADER_PATTERNS[3].match(line):
        return True
    # "On Tue, ... <x@y>" followed by a "wrote:" line.
    if _REPLY_HEADER_PATTERNS[1].match(line) and i + 1 < len(lines) and lines[i + 1].strip().lower().endswith("wrote:"):
        return True
    # Outlook style: "From: ..." followed within 4 lines by "Sent:" or "Date:".
    if _REPLY_HEADER_PATTERNS[4].match(line):
        window = " ".join(lines[i + 1 : i + 5]).lower()
        return "sent:" in window or "date:" in window
    return False


def clean_body(text: str | None, html_body: str | None = None) -> str:
    if not (text or "").strip() and html_body:
        text = html_to_text(html_body)
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")

    kept: list[str] = []
    in_forward = False
    for i, line in enumerate(lines):
        if _FORWARD_MARKER.match(line):
            in_forward = True
            kept.append(line)
            continue
        if not in_forward and _is_reply_header(lines, i):
            # Only cut if something meaningful came before, otherwise this is a
            # forward-like message with headers at the top.
            if any(k.strip() for k in kept):
                break
        if line.lstrip().startswith(">"):
            continue
        if any(p.match(line) for p in _SIGNATURE_PATTERNS):
            if any(k.strip() for k in kept):
                break
            continue
        kept.append(line)

    cleaned = "\n".join(kept)
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()
