"""TLS SMTP/STARTTLS and optional TLS IMAP mailbox adapter with certificate verification."""

from __future__ import annotations

import email
import smtplib
import ssl
from datetime import timezone
from email.utils import parsedate_to_datetime

from app.core import mail_sockets
from app.core.encryption import decrypt_json
from app.core.ssrf import validate_mail_host
from app.db.base import utcnow
from app.db.models import MailboxConnection
from app.services.mailbox.base import (
    HandoffOutcome,
    InboundMessageRecord,
    OutboundEmailMessage,
    SendResult,
    build_mime_message,
)


class SmtpImapMailboxTransport:
    """Standard authenticated TLS SMTP + optional TLS IMAP transport."""

    def __init__(self, conn: MailboxConnection) -> None:
        self.conn = conn

    def _get_creds(self) -> dict[str, str]:
        return decrypt_json(self.conn.encrypted_credentials)

    def send_message(self, msg: OutboundEmailMessage) -> SendResult:
        try:
            validate_mail_host(self.conn.smtp_host, self.conn.smtp_port)
            creds = self._get_creds()
            username = creds.get("username", self.conn.sender_email)
            password = creds.get("password", "")
            # Bind sender identity to authenticated mailbox or explicitly verified alias
            allowed_senders = {self.conn.sender_email.lower(), *(a.lower() for a in (self.conn.authorized_aliases_json or []))}
            if msg.from_email.lower() not in allowed_senders:
                raise ValueError(f"Sender '{msg.from_email}' is not authorized for this SMTP connection")
            mime = build_mime_message(msg)
        except Exception as exc:
            return SendResult(
                outcome=HandoffOutcome.PRE_HANDOFF_FAILURE,
                rfc_message_id=msg.rfc_message_id,
                error_code="PRE_HANDOFF_ERROR",
                error_message=str(exc),
            )

        ctx = ssl.create_default_context()
        handoff_started = False
        try:
            if self.conn.smtp_port == 465:
                with mail_sockets.PublicSMTPSSL(self.conn.smtp_host, self.conn.smtp_port, context=ctx, timeout=20) as server:
                    server.login(username, password)
                    handoff_started = True
                    refused = server.send_message(mime)
            else:
                with mail_sockets.PublicSMTP(self.conn.smtp_host, self.conn.smtp_port, timeout=20) as server:
                    server.ehlo()
                    # This mailbox transport authenticates on every send.
                    server.starttls(context=ctx)
                    server.ehlo()
                    server.login(username, password)
                    handoff_started = True
                    refused = server.send_message(mime)

            if refused:
                # smtplib returns a mapping only if at least one recipient was accepted.
                return SendResult(
                    outcome=HandoffOutcome.AMBIGUOUS_HANDOFF,
                    rfc_message_id=msg.rfc_message_id,
                    error_code="SMTP_RECIPIENT_REFUSED",
                    error_message="SMTP accepted only some recipients. Review the handoff before any further action.",
                )
            return SendResult(
                outcome=HandoffOutcome.PROVIDER_ACCEPTED,
                provider_message_id=msg.rfc_message_id,
                rfc_message_id=msg.rfc_message_id,
            )
        except smtplib.SMTPAuthenticationError as exc:
            return SendResult(
                outcome=HandoffOutcome.PROVIDER_REJECTED,
                rfc_message_id=msg.rfc_message_id,
                error_code="SMTP_AUTH_FAILED",
                error_message=f"SMTP authentication failed: {exc}",
            )
        except smtplib.SMTPResponseException as exc:
            is_rate = exc.smtp_code in (421, 450, 451, 452)
            return SendResult(
                outcome=HandoffOutcome.AMBIGUOUS_HANDOFF if handoff_started and exc.smtp_code < 500 else HandoffOutcome.PROVIDER_REJECTED,
                rfc_message_id=msg.rfc_message_id,
                error_code=f"SMTP_{exc.smtp_code}",
                error_message=str(exc),
                is_rate_limit=is_rate,
                retry_after_seconds=120 if is_rate else None,
            )
        except Exception as exc:
            return SendResult(
                outcome=HandoffOutcome.AMBIGUOUS_HANDOFF if handoff_started else HandoffOutcome.PRE_HANDOFF_FAILURE,
                rfc_message_id=msg.rfc_message_id,
                error_code="SMTP_TRANSPORT_ERROR",
                error_message=str(exc),
            )

    def reconcile_sent_message(self, rfc_message_id: str, action_id: str) -> SendResult | None:
        if not self.conn.imap_host:
            return None
        try:
            validate_mail_host(self.conn.imap_host, self.conn.imap_port)
            creds = self._get_creds()
            ctx = ssl.create_default_context()
            with mail_sockets.PublicIMAPSSL(self.conn.imap_host, self.conn.imap_port, ssl_context=ctx, timeout=15) as imap:
                imap.login(creds.get("username", self.conn.sender_email), creds.get("password", ""))
                for folder in ("Sent", '"Sent Items"', '"[Gmail]/Sent Mail"'):
                    typ, _ = imap.select(folder, readonly=True)
                    if typ != "OK":
                        continue
                    typ, data = imap.uid("search", None, "HEADER", "Message-ID", rfc_message_id)
                    if typ == "OK" and data and data[0]:
                        return SendResult(
                            outcome=HandoffOutcome.PROVIDER_ACCEPTED,
                            provider_message_id=rfc_message_id,
                            rfc_message_id=rfc_message_id,
                        )
        except Exception:
            return None
        return None

    def poll_inbox(self, cursor: str) -> tuple[list[InboundMessageRecord], str]:
        if not self.conn.inbox_poll_enabled or not self.conn.imap_host:
            raise PermissionError("IMAP inbox reading is unavailable")
        validate_mail_host(self.conn.imap_host, self.conn.imap_port)
        creds = self._get_creds()
        ctx = ssl.create_default_context()
        records: list[InboundMessageRecord] = []
        with mail_sockets.PublicIMAPSSL(self.conn.imap_host, self.conn.imap_port, ssl_context=ctx, timeout=20) as imap:
            imap.login(creds.get("username", self.conn.sender_email), creds.get("password", ""))
            typ, _ = imap.select("INBOX", readonly=True)
            if typ != "OK":
                raise ValueError("IMAP inbox selection failed")
            _, vals = imap.response("UIDVALIDITY")
            if not vals or not vals[0]:
                raise ValueError("IMAP inbox UIDVALIDITY unavailable")
            validity = vals[0].decode("ascii")
            old_validity, _, value = (cursor or "").removeprefix("pending:").partition(":")
            last_uid = int(value) if old_validity == validity and value.isdigit() else 0
            highest_uid = last_uid
            typ, data = imap.uid("search", None, f"UID {last_uid + 1}:*" if last_uid else "ALL")
            if typ != "OK" or not data or not isinstance(data[0], bytes):
                raise ValueError("IMAP inbox search failed")
            all_uids = sorted(u for u in map(int, data[0].split()) if u > last_uid)
            partial = len(all_uids) > 50
            uids = all_uids[:50]
            for uid in uids:
                highest_uid = max(highest_uid, uid)
                ftyp, fdata = imap.uid("fetch", str(uid), "(BODY.PEEK[HEADER] BODY.PEEK[TEXT]<0.1200>)")
                if ftyp != "OK" or not fdata:
                    raise ValueError("IMAP inbox metadata fetch failed")
                raw_bytes = b"".join(part[1] for part in fdata if isinstance(part, tuple) and len(part) > 1)
                if not raw_bytes:
                    raise ValueError("IMAP inbox fetch returned no message")
                parsed = email.message_from_bytes(raw_bytes)
                dt = utcnow()
                if parsed.get("Date"):
                    try:
                        dt = parsedate_to_datetime(parsed["Date"]).astimezone(timezone.utc)
                    except Exception:
                        pass
                snippet = ""
                if not parsed.is_multipart():
                    payload = parsed.get_payload(decode=True)
                    if isinstance(payload, bytes):
                        snippet = payload.decode("utf-8", errors="ignore")[:500]
                records.append(
                    InboundMessageRecord(
                        provider_message_id=f"{validity}:{uid}",
                        provider_thread_id="",
                        rfc_message_id=str(parsed.get("Message-ID", "")),
                        in_reply_to=str(parsed.get("In-Reply-To", "")),
                        references=str(parsed.get("References", "")),
                        sender_email=str(parsed.get("From", "")),
                        recipient_email=str(parsed.get("To", "")),
                        subject=str(parsed.get("Subject", "")),
                        body_snippet=snippet,
                        received_at=dt,
                        headers={k.lower(): str(v) for k, v in parsed.items()},
                    )
                )
        return records, ("pending:" if partial else "") + f"{validity}:{highest_uid}"
