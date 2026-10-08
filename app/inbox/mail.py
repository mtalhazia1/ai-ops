"""Builds the outgoing reply as a raw RFC 5322 message for the Gmail API.

The recipient and threading headers come from the stored Gmail metadata, never from
LLM output (Section 12). Gmail fills in From with the authenticated mailbox.
"""

import base64
from email.message import EmailMessage
from email.utils import formataddr

from .models import Email


def reply_recipient(email: Email) -> str:
    return email.reply_to or email.from_email


def build_reply(email: Email, subject: str, body: str) -> EmailMessage:
    msg = EmailMessage()
    msg["To"] = formataddr((email.from_name, reply_recipient(email))) if not email.reply_to else email.reply_to
    msg["Subject"] = subject
    if email.rfc_message_id:
        msg["In-Reply-To"] = email.rfc_message_id
        msg["References"] = " ".join(filter(None, [email.references.strip(), email.rfc_message_id]))
    msg.set_content(body)
    return msg


def gmail_raw(msg: EmailMessage) -> str:
    """base64url, as the Gmail API `users.messages.send` expects in `raw`."""
    return base64.urlsafe_b64encode(msg.as_bytes()).decode().rstrip("=")
