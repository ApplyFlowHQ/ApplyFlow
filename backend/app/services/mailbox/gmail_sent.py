"""Gmail Sent history synchronization, conservative job-email classification, coverage reporting, and optional Draft creation."""

from __future__ import annotations

import base64
import email
import re
import ssl
from datetime import datetime, timezone
from email import policy
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core import mail_sockets
from app.core.config import get_settings
from app.core.encryption import decrypt_json, encrypt_json
from app.core.security import normalize_email_dedupe_key, normalize_email_display
from app.core.ssrf import validate_mail_host
from app.db.base import as_utc, utcnow
from app.db.models import GmailSentRecord, MailboxConnection, RecipientHistory
from app.services.mailbox.base import OutboundEmailMessage, build_mime_message
from app.services.verification import NOREPLY_LOCAL_PARTS

GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_COMPOSE_SCOPE = "https://www.googleapis.com/auth/gmail.compose"

_RECRUITMENT_LOCAL_RE = re.compile(
    r"^(careers?|recruitment|recruiting|talent|jobs?|hr|hiring|apply|people|cv|resume|staffing)([.+_-]|$)",
    re.IGNORECASE,
)
_JOB_SUBJECT_RE = re.compile(
    r"\b(cv|resume|application\s+for|applying\s+for|job\s+application|candidate|vacancy|"
    r"position|hiring|ref\s*[:#]|cafm|analyst|engineer|coordinator|manager|consultant|طلب\s+توظيف|سيرة\s+ذاتية)\b",
    re.IGNORECASE,
)
_NON_JOB_SUBJECT_RE = re.compile(
    r"\b(unsubscribe|newsletter|invoice|receipt|order\s+confirm|verification\s+code|otp|"
    r"bank\s+statement|shipping\s+notification|calendar\s+invite|happy\s+birthday)\b",
    re.IGNORECASE,
)
_NOREPLY_OR_SYSTEM_RE = re.compile(
    r"^(no[-_]?reply|donotreply|notifications?|mailer-daemon|postmaster|support|billing|receipts?)@",
    re.IGNORECASE,
)


def is_recruitment_address(address: str) -> bool:
    return bool(_RECRUITMENT_LOCAL_RE.match(address.partition("@")[0]))


def classify_sent_message_for_dedup(
    *,
    sender_email: str,
    recipient_email: str,
    subject: str,
) -> tuple[str, bool, str]:
    """
    Return the category, presence of an application signal and a readable reason.
    Exact prior contact is tracked separately for all external addresses.
    """
    rec_low = (recipient_email or "").strip().lower()
    snd_low = (sender_email or "").strip().lower()
    subj = (subject or "").strip()

    if not rec_low or "@" not in rec_low:
        return "non_job", False, "Missing or invalid recipient email"
    if rec_low == snd_low:
        return "non_job", False, "Self-sent message"
    if _NOREPLY_OR_SYSTEM_RE.search(rec_low) or is_nonreply_address(rec_low):
        return "non_job", False, "System or noreply address"
    if _NON_JOB_SUBJECT_RE.search(subj):
        return "non_job", False, "Subject matches automated billing/newsletter/calendar traffic"

    local_part = rec_low.split("@", 1)[0]
    has_rec_handle = bool(_RECRUITMENT_LOCAL_RE.match(local_part))
    has_job_subj = bool(_JOB_SUBJECT_RE.search(subj))

    if has_rec_handle and has_job_subj:
        return "job_application", True, "Matched recruiting mailbox prefix and job/CV subject keywords"
    if has_job_subj and re.search(r"\b(cv|resume|applying|application|طلب|سيرة)\b", subj, re.I):
        return "job_application", True, "Matched explicit CV/application subject signal"
    if has_rec_handle:
        return "job_application", True, f"Matched recruiting recipient prefix '{local_part}@'"
    return "ambiguous", False, "External sent message lacks explicit job-application or HR address signals"


def _ensure_valid_gmail_access_token(
    db: Session,
    conn: MailboxConnection,
    http_transport: httpx.BaseTransport | None = None,
) -> str:
    creds = decrypt_json(conn.encrypted_credentials) if conn.encrypted_credentials else {}
    access_token = str(creds.get("access_token", ""))
    refresh_token = str(creds.get("refresh_token", ""))
    expires_at_raw = creds.get("expires_at")
    needs_refresh = not access_token
    if expires_at_raw:
        try:
            exp = datetime.fromisoformat(str(expires_at_raw).replace("Z", "+00:00"))
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if exp <= utcnow():
                needs_refresh = True
        except Exception:
            pass

    if not needs_refresh:
        return access_token

    if not refresh_token:
        raise ValueError("Gmail access token expired and no refresh token is stored")

    from app.services.effective_config import resolve_effective_google_oauth

    google_cfg = resolve_effective_google_oauth(db)
    with httpx.Client(timeout=20.0, transport=http_transport) as client:
        resp = client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": google_cfg.client_id or get_settings().google_oauth_client_id,
                "client_secret": google_cfg.client_secret or get_settings().google_oauth_client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
    if resp.status_code != 200:
        raise ValueError(f"Gmail token refresh failed (HTTP {resp.status_code})")
    data = resp.json()
    new_tok = data["access_token"]
    creds["access_token"] = new_tok
    creds["expires_at"] = datetime.fromtimestamp(
        utcnow().timestamp() + int(data.get("expires_in", 3600)) - 60, tz=timezone.utc
    ).isoformat()
    conn.encrypted_credentials = encrypt_json(creds)
    db.flush()
    return new_tok


def _parse_sent_headers(msg_json: dict[str, Any]) -> tuple[list[str], str, str, datetime | None]:
    payload = msg_json.get("payload") or {}
    headers = payload.get("headers") or []
    hmap = {str(h.get("name", "")).lower(): str(h.get("value", "")) for h in headers}

    recipients: list[str] = []
    for key in ("to", "cc", "bcc"):
        raw = ", ".join(str(h.get("value", "")) for h in headers if str(h.get("name", "")).lower() == key)
        if raw:
            for _name, addr in getaddresses([raw]):
                if addr and "@" in addr:
                    recipients.append(addr.strip())

    subject = hmap.get("subject", "")
    rfc_id = hmap.get("message-id", "").strip()
    sent_dt: datetime | None = None
    if hmap.get("date"):
        try:
            dt = parsedate_to_datetime(hmap["date"])
            sent_dt = dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            sent_dt = None
    if not sent_dt and msg_json.get("internalDate"):
        try:
            ms = int(msg_json["internalDate"])
            sent_dt = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
        except Exception:
            sent_dt = None
    return recipients, subject, rfc_id, sent_dt


def is_nonreply_address(addr: str) -> bool:
    local = addr.lower().partition("@")[0]
    return local in NOREPLY_LOCAL_PARTS or bool(re.match(r"^(?:no[._-]?reply|do[._-]?not[._-]?reply)(?:[+._-]|$)", local))


def external_contact_key(addr: str, sender_email: str) -> str:
    try:
        key = normalize_email_dedupe_key(normalize_email_display(addr))
    except ValueError:
        return ""
    if key == normalize_email_dedupe_key(sender_email) or is_nonreply_address(key):
        return ""
    return key


def _record_sent_message(db: Session, conn: MailboxConnection, mid: str, message: dict[str, Any],
                         legacy_id: str = "") -> tuple[int, int]:
    recipients, subject, rfc_id, sent_at = _parse_sent_headers(message)
    from_headers = [h["value"] for h in (message.get("payload") or {}).get("headers", [])
                    if h.get("name", "").lower() == "from"]
    own_addresses = {normalize_email_dedupe_key(addr) for _, addr in getaddresses(from_headers)}
    contacts_by_key = {}
    for addr in recipients:
        key = external_contact_key(addr, conn.sender_email)
        if key and key not in own_addresses:
            contacts_by_key[key] = addr
    contacts = [addr for addr in contacts_by_key.values() if classify_sent_message_for_dedup(
        sender_email=conn.sender_email, recipient_email=addr, subject=subject)[1]]
    primary = next(iter(contacts or list(contacts_by_key.values()) or recipients), "")
    cls, _, reason = classify_sent_message_for_dedup(
        sender_email=conn.sender_email, recipient_email=primary, subject=subject)
    scope = select(GmailSentRecord).where(GmailSentRecord.mailbox_connection_id == conn.id,
                                          GmailSentRecord.user_id == conn.user_id)
    record = db.scalar(scope.where(GmailSentRecord.provider_message_id == mid))
    if not record and legacy_id and rfc_id:
        # Older IMAP records omitted UIDVALIDITY. Match only with additional RFC evidence.
        record = db.scalar(scope.where(GmailSentRecord.provider_message_id == legacy_id,
                                       GmailSentRecord.rfc_message_id == rfc_id))
    inserted = record is None
    if inserted:
        record = GmailSentRecord(user_id=conn.user_id, mailbox_connection_id=conn.id)
        db.add(record)
    record.provider_message_id = mid
    record.rfc_message_id = rfc_id[:255]
    record.thread_id = str(message.get("threadId") or "")[:255]
    record.recipient_email = primary[:254]
    record.recipient_normalized = normalize_email_dedupe_key(primary)[:254]
    record.recipient_domain = record.recipient_normalized.partition("@")[2][:200]
    record.subject = subject[:300]
    record.sent_at = sent_at
    record.classification = cls
    record.is_suppressed_recipient = bool(contacts_by_key)
    if not contacts_by_key and normalize_email_dedupe_key(primary) in own_addresses:
        cls, reason = "non_job", "Self-sent message"
        record.classification = cls
    record.confidence_reason = reason[:240]
    record.contact_keys_json = sorted(contacts_by_key)
    if sent_at and (not conn.sent_earliest_covered_at or sent_at < as_utc(conn.sent_earliest_covered_at)):
        conn.sent_earliest_covered_at = sent_at
    if contacts_by_key:
        existing = set(db.scalars(select(RecipientHistory.normalized_email).where(
            RecipientHistory.user_id == conn.user_id, RecipientHistory.normalized_email.in_(contacts_by_key))))
        for key in contacts_by_key.keys() - existing:
            db.add(RecipientHistory(user_id=conn.user_id, normalized_email=key, display_email=contacts_by_key[key],
                                    source="gmail_sent_sync", notes="Previously contacted in Sent history"))
    db.flush()
    return int(inserted), int(inserted and bool(contacts))


def _sync_imap_sent_history(
    db: Session,
    *,
    conn: MailboxConnection,
    max_messages: int = 200,
    force_full: bool = False,
) -> dict[str, Any]:
    """Synchronize Sent folder headers over IMAP (`[Gmail]/Sent Mail` / `Sent`) for SMTP/IMAP Gmail connections."""
    if not conn.imap_host:
        conn.sent_sync_status = "disabled"
        conn.sent_sync_error = "IMAP host not configured; dedup uses ApplyFlow ledger & imports only"
        db.commit()
        return {
            "coverage_mode": "applyflow_ledger_only",
            "synced_messages": 0,
            "job_applications_detected": 0,
            "status": "disabled",
            "message": conn.sent_sync_error,
        }

    try:
        validate_mail_host(conn.imap_host, conn.imap_port)
        creds = decrypt_json(conn.encrypted_credentials) if conn.encrypted_credentials else {}
        username = str(creds.get("username") or conn.sender_email)
        password = str(creds.get("password") or "")
        if not password:
            raise ValueError("IMAP credentials missing for Sent Mail sync")

        last_uid = 0
        if not force_full and conn.sent_sync_checkpoint_json and conn.sent_sync_cursor.startswith("imap:"):
            try:
                last_uid = int(conn.sent_sync_cursor.split(":", 1)[1])
            except ValueError:
                last_uid = 0

        highest_uid = last_uid
        synced_count = 0
        job_detected_count = 0
        ctx = ssl.create_default_context()
        with mail_sockets.PublicIMAPSSL(conn.imap_host, conn.imap_port, ssl_context=ctx, timeout=20) as imap:
            imap.login(username, password)
            selected_folder = ""
            for folder in ('"[Gmail]/Sent Mail"', "Sent", '"Sent Items"', '"[Gmail]/Sent"'):
                typ, _ = imap.select(folder, readonly=True)
                if typ == "OK":
                    selected_folder = folder.strip('"')
                    break
            if not selected_folder:
                raise ValueError("Unable to locate IMAP Sent folder")

            _, values = imap.response("UIDVALIDITY")
            if not values or not values[0]:
                raise ValueError("IMAP Sent folder did not report UIDVALIDITY")
            validity = values[0].decode("ascii")
            state = conn.sent_sync_checkpoint_json or {}
            if state.get("validity") != validity or state.get("folder") != selected_folder:
                last_uid = highest_uid = 0
            partial = False
            scan_started = utcnow().isoformat()
            search_crit = f"UID {last_uid + 1}:*" if last_uid > 0 else "ALL"
            typ, data = imap.uid("search", None, search_crit)
            if typ != "OK" or not data or not isinstance(data[0], bytes):
                raise ValueError("IMAP Sent search failed")
            if data[0]:
                uids = sorted(int(u) for u in data[0].split() if int(u) > last_uid)
                partial = len(uids) > max_messages
                for uid in uids[:max_messages]:
                    highest_uid = max(highest_uid, uid)
                    ftyp, fdata = imap.uid("fetch", str(uid), "(BODY.PEEK[HEADER.FIELDS (FROM TO CC BCC SUBJECT DATE MESSAGE-ID)])")
                    if ftyp != "OK" or not fdata:
                        raise ValueError("IMAP Sent header fetch failed")
                    raw_hdr = b"".join(part[1] for part in fdata if isinstance(part, tuple) and len(part) > 1)
                    if not raw_hdr:
                        raise ValueError("IMAP Sent header fetch returned no headers")
                    parsed = email.message_from_bytes(raw_hdr, policy=policy.default)
                    mid = f"imap:{selected_folder}:{validity}:{uid}"
                    message = {"payload": {"headers": [{"name": k, "value": str(v)} for k, v in parsed.items()]}}
                    added, detected = _record_sent_message(db, conn, mid, message, f"imap:{selected_folder}:{uid}")
                    synced_count += added
                    job_detected_count += detected
    except Exception as exc:
        db.rollback()
        conn.sent_sync_status = "error"
        conn.sent_sync_error = f"Sent history sync failed ({type(exc).__name__})"
        db.commit()
        return {
            "coverage_mode": "sync_error",
            "synced_messages": 0,
            "job_applications_detected": 0,
            "status": "error",
            "message": conn.sent_sync_error,
        }

    db.flush()
    total_records = db.scalar(
        select(func.count(GmailSentRecord.id)).where(
            GmailSentRecord.mailbox_connection_id == conn.id,
            GmailSentRecord.user_id == conn.user_id,
        )
    ) or 0
    conn.sent_sync_checkpoint_json = {"validity": validity, "folder": selected_folder,
                                     "checked_through_at": state.get("checked_through_at") if partial else scan_started}
    if not partial:
        conn.sent_last_synced_at = utcnow()
    conn.sent_sync_cursor = f"imap:{highest_uid}"
    conn.sent_records_count = int(total_records)
    conn.sent_sync_status = "partial" if partial else "ok"
    conn.sent_sync_error = ""
    db.commit()
    return {
        "coverage_mode": "partial" if partial else "applyflow_ledger_plus_gmail_sent",
        "synced_messages": synced_count,
        "total_sent_records": int(total_records),
        "job_applications_detected": job_detected_count,
        "earliest_covered_at": as_utc(conn.sent_earliest_covered_at).isoformat() if conn.sent_earliest_covered_at else None,
        "last_synced_at": as_utc(conn.sent_last_synced_at).isoformat() if conn.sent_last_synced_at else None,
        "status": conn.sent_sync_status,
    }


def _sync_gmail_sent_history(
    db: Session,
    *,
    conn: MailboxConnection,
    max_pages: int = 10,
    page_size: int = 50,
    force_full: bool = False,
    http_transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """
    Sync Gmail Sent messages (`in:sent` paginated + incremental `history.list` cursor) or IMAP Sent Mail
    for duplicate prevention. Respects incremental OAuth scope boundaries when using Gmail OAuth.
    """
    conn = db.scalars(select(MailboxConnection).where(MailboxConnection.id == conn.id,
        MailboxConnection.user_id == conn.user_id).with_for_update().execution_options(populate_existing=True)).one()
    if conn.status != "connected" or conn.revoked_at:
        return {"status": "disabled", "coverage_mode": "applyflow_ledger_only", "synced_messages": 0,
                "message": "Mailbox disconnected. Duplicate checking currently covers ApplyFlow records only."}
    if conn.provider == "smtp_imap":
        return _sync_imap_sent_history(
            db,
            conn=conn,
            max_messages=max(1, max_pages * page_size),
            force_full=force_full,
        )

    scopes = set(conn.granted_scopes_json or [])
    if GMAIL_READONLY_SCOPE not in scopes:
        conn.sent_sync_status = "disabled"
        conn.sent_sync_error = "gmail.readonly scope was not granted; dedup relies on ApplyFlow ledger & imports only"
        db.commit()
        return {
            "coverage_mode": "applyflow_ledger_only",
            "synced_messages": 0,
            "job_applications_detected": 0,
            "status": "disabled",
            "message": conn.sent_sync_error,
        }

    try:
        token = _ensure_valid_gmail_access_token(db, conn, http_transport=http_transport)
    except Exception as exc:
        conn.sent_sync_status = "error"
        conn.sent_sync_error = f"Unable to obtain Gmail access ({type(exc).__name__})"
        db.commit()
        return {
            "coverage_mode": "sync_error",
            "synced_messages": 0,
            "job_applications_detected": 0,
            "status": "error",
            "message": conn.sent_sync_error,
        }

    headers = {"Authorization": f"Bearer {token}"}
    state = {} if force_full else conn.sent_sync_checkpoint_json or {}
    mode = state.get("mode")
    page = state.get("page")
    anchor = state.get("anchor")
    scan_started = state.get("started_at") or utcnow().isoformat()
    root = "https://gmail.googleapis.com/gmail/v1/users/me/"
    msg_ids = {}
    with httpx.Client(base_url=root, timeout=25.0, transport=http_transport, headers=headers) as client:
        for _ in range(max(1, max_pages)):
            if not mode:
                scan_started = utcnow().isoformat()
                profile = client.get("profile")
                profile.raise_for_status()
                info = profile.json()
                if normalize_email_dedupe_key(info["emailAddress"]) != normalize_email_dedupe_key(conn.sender_email):
                    raise ValueError("Gmail profile does not match the connected sender")
                # Capture a high-water mark before enumeration (also works for empty Sent folders).
                anchor = str(info["historyId"])
                mode = "full"
            params = {"maxResults": min(max(1, page_size), 100)}
            if page:
                params["pageToken"] = page
            if mode == "incremental":
                params.update(startHistoryId=anchor, labelId="SENT")
                response = client.get("history", params=params)
                if response.status_code == 404:
                    mode = anchor = page = None
                    continue
            else:
                response = client.get("messages", params={**params, "q": "in:sent"})
            if page and response.status_code == 400:
                mode = anchor = page = None
                continue
            response.raise_for_status()
            data = response.json()
            messages = list(data.get("messages", []))
            for change in data.get("history", []):
                messages.extend(item["message"] for item in change.get("messagesAdded", []))
                messages.extend(item["message"] for item in change.get("labelsAdded", []) if "SENT" in item.get("labelIds", []))
            msg_ids.update(dict.fromkeys(m["id"] for m in messages))
            page = data.get("nextPageToken")
            if not page:
                if mode == "incremental":
                    anchor = str(data["historyId"])
                break
        partial = bool(page or not mode)
        synced_count = 0
        job_detected_count = 0
        for mid in msg_ids:
            m_resp = client.get(
                f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{mid}",
                headers=headers,
                params={"format": "metadata", "metadataHeaders": ["From", "To", "Cc", "Bcc", "Subject", "Date", "Message-ID"]},
            )
            if m_resp.status_code == 404:
                continue  # Deleted between listing and metadata fetch.
            m_resp.raise_for_status()
            m_json = m_resp.json()
            if "labelIds" in m_json and "SENT" not in m_json["labelIds"]:
                continue

            added, detected = _record_sent_message(db, conn, mid, m_json)
            synced_count += added
            job_detected_count += detected

    db.flush()
    total_records = db.scalar(
        select(func.count(GmailSentRecord.id)).where(
            GmailSentRecord.mailbox_connection_id == conn.id,
            GmailSentRecord.user_id == conn.user_id,
        )
    ) or 0

    if not partial:
        conn.sent_last_synced_at = utcnow()
        conn.sent_sync_cursor = anchor
    conn.sent_sync_checkpoint_json = ({
        "mode": mode, "anchor": anchor, "page": page, "started_at": scan_started,
        "checked_through_at": state.get("checked_through_at"),
    } if partial else {"mode": "incremental", "anchor": anchor, "checked_through_at": scan_started})
    conn.sent_records_count = int(total_records)
    conn.sent_sync_status = "partial" if partial else "ok"
    conn.sent_sync_error = ""
    db.commit()
    return {
        "coverage_mode": "partial" if partial else "applyflow_ledger_plus_gmail_sent",
        "synced_messages": synced_count,
        "total_sent_records": int(total_records),
        "job_applications_detected": job_detected_count,
        "earliest_covered_at": as_utc(conn.sent_earliest_covered_at).isoformat() if conn.sent_earliest_covered_at else None,
        "last_synced_at": as_utc(conn.sent_last_synced_at).isoformat() if conn.sent_last_synced_at else None,
        "status": conn.sent_sync_status,
    }


def sync_gmail_sent_history(db: Session, *, conn: MailboxConnection, max_pages: int = 10, page_size: int = 50,
                            force_full: bool = False, http_transport: httpx.BaseTransport | None = None) -> dict[str, Any]:
    try:
        return _sync_gmail_sent_history(db, conn=conn, max_pages=max_pages, page_size=page_size,
                                        force_full=force_full, http_transport=http_transport)
    except Exception as exc:
        db.rollback()
        code = f"HTTP {exc.response.status_code}" if isinstance(exc, httpx.HTTPStatusError) else type(exc).__name__
        conn.sent_sync_status = "error"
        conn.sent_sync_error = f"Sent history sync failed ({code}). Retry to resume."
        db.commit()
        return {"coverage_mode": "sync_error", "synced_messages": 0, "status": "error", "message": conn.sent_sync_error}


def get_mailbox_dedup_coverage(db: Session, *, conn: MailboxConnection) -> dict[str, Any]:
    """Return honest duplicate-prevention coverage state for UI/API visibility."""
    scopes = set(conn.granted_scopes_json or [])
    has_readonly = GMAIL_READONLY_SCOPE in scopes or (conn.provider == "smtp_imap" and bool(conn.imap_host))
    has_compose = GMAIL_COMPOSE_SCOPE in scopes

    status = conn.sent_sync_status
    if not has_readonly or conn.status != "connected" or conn.revoked_at:
        status = "disabled"
    elif status == "ok" and not conn.sent_sync_checkpoint_json:
        status = "partial"  # Older cursors may have skipped pages.
    if status == "ok":
        coverage_mode, warning = "applyflow_ledger_plus_gmail_sent", ""
    elif status == "partial":
        coverage_mode, warning = "partial", "Sent history is partially checked. Sync again to continue."
    elif status == "error":
        coverage_mode, warning = "sync_error", conn.sent_sync_error
    else:
        coverage_mode, warning = "applyflow_ledger_only", "Duplicate checking currently covers ApplyFlow records only."
    newest = db.scalar(select(func.max(GmailSentRecord.sent_at)).where(
        GmailSentRecord.user_id == conn.user_id, GmailSentRecord.mailbox_connection_id == conn.id))
    prior_contacts = set()
    job_contacts = set()
    records = db.execute(select(GmailSentRecord.recipient_normalized, GmailSentRecord.contact_keys_json,
                                GmailSentRecord.subject, GmailSentRecord.confidence_reason).where(
        GmailSentRecord.mailbox_connection_id == conn.id, GmailSentRecord.user_id == conn.user_id))
    for primary, contacts, subject, reason in records:
        keys = contacts or ([primary] if reason != "Self-sent message" else [])
        for key in keys:
            if external_contact_key(key, conn.sender_email):
                prior_contacts.add(key)
                if classify_sent_message_for_dedup(sender_email=conn.sender_email, recipient_email=key, subject=subject)[1]:
                    job_contacts.add(key)

    return {
        "mailbox_id": conn.id,
        "provider": conn.provider,
        "sender_email": conn.sender_email,
        "has_gmail_readonly_scope": has_readonly,
        "has_gmail_compose_scope": has_compose,
        "save_drafts_in_gmail": bool(conn.save_drafts_in_gmail),
        "coverage_mode": coverage_mode,
        "warning_banner": warning,
        "sent_sync_status": status,
        "checked_through_at": (conn.sent_sync_checkpoint_json or {}).get("checked_through_at"),
        "sent_latest_covered_at": as_utc(newest).isoformat() if newest else None,
        "sent_last_synced_at": as_utc(conn.sent_last_synced_at).isoformat() if conn.sent_last_synced_at else None,
        "sent_earliest_covered_at": as_utc(conn.sent_earliest_covered_at).isoformat() if conn.sent_earliest_covered_at else None,
        "sent_records_count": int(conn.sent_records_count or 0),
        "prior_contact_count": len(prior_contacts),
        "suppressed_job_recipients_count": len(job_contacts),
        "sent_sync_error": conn.sent_sync_error or "",
    }


def create_gmail_draft(
    db: Session,
    *,
    conn: MailboxConnection,
    outbound: OutboundEmailMessage,
    http_transport: httpx.BaseTransport | None = None,
) -> str:
    """Create a real draft inside user's Gmail Drafts ONLY when gmail.compose scope is explicitly granted."""
    scopes = set(conn.granted_scopes_json or [])
    if GMAIL_COMPOSE_SCOPE not in scopes:
        raise ValueError("gmail.compose scope not granted; cannot save draft to Gmail")
    if not conn.save_drafts_in_gmail:
        raise ValueError("save_drafts_in_gmail is disabled on this mailbox connection")

    token = _ensure_valid_gmail_access_token(db, conn, http_transport=http_transport)
    msg = build_mime_message(outbound)
    raw_b64 = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii").rstrip("=")
    with httpx.Client(timeout=20.0, transport=http_transport) as client:
        resp = client.post(
            "https://gmail.googleapis.com/gmail/v1/users/me/drafts",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"message": {"raw": raw_b64}},
        )
    if resp.status_code not in (200, 201):
        raise ValueError(f"Gmail Draft creation failed (HTTP {resp.status_code})")
    return str((resp.json() or {}).get("id") or "")
