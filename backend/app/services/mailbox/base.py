"""Shared MIME builder, handoff outcomes, and mailbox adapter interface."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from enum import Enum
from typing import Protocol

from app.core.security import assert_no_header_injection, sanitize_filename, sanitize_header_value


class HandoffOutcome(str, Enum):
    PRE_HANDOFF_FAILURE = "pre_handoff_failure"
    PROVIDER_REJECTED = "provider_rejected"
    PROVIDER_ACCEPTED = "provider_accepted"
    AMBIGUOUS_HANDOFF = "ambiguous_handoff"


@dataclass
class SendAttachment:
    filename: str
    mime_type: str
    data: bytes
    expected_sha256: str = ""


@dataclass
class OutboundEmailMessage:
    from_email: str
    from_name: str
    to_email: str
    cc_emails: list[str]
    subject: str
    body_text: str
    rfc_message_id: str
    attachments: list[SendAttachment] = field(default_factory=list)
    thread_id: str = ""
    in_reply_to: str = ""
    references: str = ""
    custom_action_id: str = ""


@dataclass
class SendResult:
    outcome: HandoffOutcome
    provider_message_id: str = ""
    provider_thread_id: str = ""
    rfc_message_id: str = ""
    error_code: str = ""
    error_message: str = ""
    retry_after_seconds: int | None = None
    is_rate_limit: bool = False


@dataclass
class InboundMessageRecord:
    provider_message_id: str
    provider_thread_id: str
    rfc_message_id: str
    in_reply_to: str
    references: str
    sender_email: str
    recipient_email: str
    subject: str
    body_snippet: str
    received_at: datetime
    headers: dict[str, str] = field(default_factory=dict)


def build_rfc_message_id(sender_email: str, action_id: str) -> str:
    domain = sender_email.split("@", 1)[-1] if "@" in sender_email else "applyflow.local"
    return make_msgid(idstring=f"af.{action_id[:16]}", domain=domain)


def build_mime_message(msg: OutboundEmailMessage) -> EmailMessage:
    """Build RFC 2822 / MIME message with header-injection guards and validated attachments."""
    assert_no_header_injection(msg.to_email, "To")
    assert_no_header_injection(msg.subject, "Subject")
    for cc in msg.cc_emails:
        assert_no_header_injection(cc, "CC")

    mime = EmailMessage()
    sender_name = sanitize_header_value(msg.from_name, 100)
    mime["From"] = formataddr((sender_name, msg.from_email)) if sender_name else msg.from_email
    mime["To"] = msg.to_email
    if msg.cc_emails:
        mime["Cc"] = ", ".join(msg.cc_emails)
    mime["Subject"] = sanitize_header_value(msg.subject, 240)
    mime["Message-ID"] = msg.rfc_message_id
    if msg.in_reply_to:
        mime["In-Reply-To"] = sanitize_header_value(msg.in_reply_to, 250)
    if msg.references:
        mime["References"] = sanitize_header_value(msg.references, 500)
    if msg.custom_action_id:
        mime["X-ApplyFlow-Action-ID"] = sanitize_header_value(msg.custom_action_id, 80)

    mime.set_content(msg.body_text, subtype="plain", charset="utf-8")

    for att in msg.attachments:
        maintype, _, subtype = att.mime_type.partition("/")
        if not maintype or not subtype:
            maintype, subtype = "application", "octet-stream"
        mime.add_attachment(
            att.data,
            maintype=maintype,
            subtype=subtype,
            filename=sanitize_filename(att.filename, "cv.pdf"),
        )
    return mime


class MailboxTransport(Protocol):
    def send_message(self, msg: OutboundEmailMessage) -> SendResult:
        ...

    def reconcile_sent_message(self, rfc_message_id: str, action_id: str) -> SendResult | None:
        ...

    def poll_inbox(self, cursor: str) -> tuple[list[InboundMessageRecord], str]:
        ...
