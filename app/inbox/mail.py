"""Builds the outgoing reply as a raw RFC 5322 message for the Gmail API.

The recipient and threading headers come from the stored Gmail metadata, never from
LLM output (Section 12). Gmail fills in From with the authenticated mailbox.
"""

import base64
from email.message import EmailMessage
from email.utils import formataddr

from .models import Email


class NoRecipient(ValueError):
    """Intake could not parse a sender (or Reply-To) address, so there is no one to reply to."""


# What purge_old_bodies writes in place of deleted text (Section 12).
PURGED = "[deleted after retention period]"


class PurgedBody(ValueError):
    """The reply text was deleted by the retention purge; it must never be sent."""


def reply_recipient(email: Email) -> str:
    """The address the reply goes to, or "" when intake stored none."""
    return email.reply_to or email.from_email


def build_reply(email: Email, subject: str, body: str) -> EmailMessage:
    recipient = reply_recipient(email)
    if not recipient:
        raise NoRecipient(f"email #{email.pk} has no valid sender address to reply to")
    if body.strip() == PURGED:
        raise PurgedBody("the reply text was deleted after the retention period; write a new reply")
    msg = EmailMessage()
    msg["To"] = formataddr((email.from_name, recipient)) if not email.reply_to else recipient
    msg["Subject"] = subject
    if email.rfc_message_id:
        msg["In-Reply-To"] = email.rfc_message_id
        msg["References"] = " ".join(filter(None, [email.references.strip(), email.rfc_message_id]))
    msg.set_content(body)
    return msg


def gmail_raw(msg: EmailMessage) -> str:
    """base64url, as the Gmail API `users.messages.send` expects in `raw`."""
    return base64.urlsafe_b64encode(msg.as_bytes()).decode().rstrip("=")
