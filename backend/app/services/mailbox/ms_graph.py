"""Microsoft Graph OAuth 2.0 connector (distinct from ordinary SMTP/IMAP compatibility)."""

from __future__ import annotations

import base64
import re
from datetime import datetime, timedelta, timezone
from email import policy
from urllib.parse import quote, urlencode, urlsplit

import httpx
from sqlalchemy.orm import Session

from app.core.encryption import decrypt_json, encrypt_json, generate_opaque_token, generate_pkce_pair, hash_token
from app.db.base import utcnow
from app.db.models import AuditEvent, AuthToken, MailboxConnection
from app.services.auth import consume_google_state as consume_oauth_state
from app.services.effective_config import resolve_effective_ms_graph_oauth
from app.services.mailbox.base import (
    HandoffOutcome,
    InboundMessageRecord,
    OutboundEmailMessage,
    SendResult,
    build_mime_message,
)

MS_GRAPH_SCOPES = "offline_access https://graph.microsoft.com/Mail.Send https://graph.microsoft.com/Mail.Read https://graph.microsoft.com/User.Read"


def build_ms_graph_oauth_url(db: Session, *, user_id: str) -> str:
    mcfg = resolve_effective_ms_graph_oauth(db)
    if not mcfg.client_id:
        raise ValueError("Microsoft Graph client credentials are not configured on this installation")

    state = generate_opaque_token(24)
    verifier, challenge = generate_pkce_pair()
    db.add(
        AuthToken(
            user_id=user_id,
            purpose="oauth_state",
            token_hash=hash_token(state),
            payload_json={"flow": "ms_graph", "code_verifier": verifier},
            expires_at=utcnow() + timedelta(minutes=15),
        )
    )
    db.commit()

    params = {
        "client_id": mcfg.client_id,
        "response_type": "code",
        "redirect_uri": mcfg.redirect_uri,
        "response_mode": "query",
        "scope": MS_GRAPH_SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"https://login.microsoftonline.com/{mcfg.tenant_id}/oauth2/v2.0/authorize?{urlencode(params)}"


def complete_ms_graph_callback(db: Session, *, user_id: str, code: str | None, state: str) -> MailboxConnection:
    if not user_id or not state:
        raise ValueError("An authenticated Microsoft Graph connection request is required")
    flow = consume_oauth_state(db, state=state, flow="ms_graph", user_id=user_id)
    if not code:
        raise ValueError("Microsoft Graph authorization was not completed")
    mcfg = resolve_effective_ms_graph_oauth(db)

    with httpx.Client(timeout=20.0) as client:
        resp = client.post(
            f"https://login.microsoftonline.com/{mcfg.tenant_id}/oauth2/v2.0/token",
            data={
                "client_id": mcfg.client_id,
                "client_secret": mcfg.client_secret,
                "scope": MS_GRAPH_SCOPES,
                "code": code,
                "redirect_uri": mcfg.redirect_uri,
                "grant_type": "authorization_code",
                "code_verifier": flow["code_verifier"],
            },
        )
        if resp.status_code != 200:
            raise ValueError(f"Microsoft Graph token exchange failed (HTTP {resp.status_code})")
        token_data = resp.json()
        access_token = token_data["access_token"]
        me_resp = client.get(
            "https://graph.microsoft.com/v1.0/me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        email_addr = ""
        if me_resp.status_code == 200:
            me = me_resp.json()
            email_addr = str(me.get("mail") or me.get("userPrincipalName") or "")

    if not email_addr:
        raise ValueError("Unable to read Microsoft mailbox email address")

    creds = {
        "access_token": access_token,
        "refresh_token": token_data.get("refresh_token", ""),
        "expires_at": (utcnow() + timedelta(seconds=int(token_data.get("expires_in", 3600)))).isoformat(),
    }
    conn = MailboxConnection(
        user_id=user_id,
        provider="ms_graph_oauth",
        label=f"Outlook / Microsoft 365 ({email_addr})",
        sender_email=email_addr,
        granted_scopes_json=str(token_data.get("scope", "")).split(),
        encrypted_credentials=encrypt_json(creds),
        is_primary=False,
        status="connected",
        inbox_poll_enabled=True,
        inbox_health_status="healthy",
    )
    db.add(conn)
    db.add(AuditEvent(user_id=user_id, actor_user_id=user_id, action="ms_graph_oauth_connected", target_type="user", target_id=user_id))
    db.commit()
    db.refresh(conn)
    return conn


GRAPH_ME = "https://graph.microsoft.com/v1.0/me"
DELTA_URL = GRAPH_ME + "/mailFolders/Inbox/messages/delta"
DELTA_FIELDS = "id,conversationId,internetMessageId,internetMessageHeaders,from,toRecipients,subject,bodyPreview,receivedDateTime,isDraft"
DELTA_COMPLETE = "graph-delta:"
DELTA_PENDING = "pending:graph:"
MAX_DELTA_PAGES = 8
IMMUTABLE_ID = 'IdType="ImmutableId"'


def graph_can_read(conn: MailboxConnection) -> bool:
    return any(isinstance(s, str) and s.casefold().removeprefix("https://graph.microsoft.com/") in {"mail.read", "mail.readwrite"}
               for s in conn.granted_scopes_json or [])


def _delta_link(link: str) -> str:
    parsed = urlsplit(link)
    if (parsed.scheme != "https" or parsed.netloc.lower() != "graph.microsoft.com" or parsed.fragment
            or not parsed.query or not re.fullmatch(
                r"/v1\.0/me/mailfolders(?:/[A-Za-z0-9_+=%-]+|\('[A-Za-z0-9_+=%-]+'\))/messages/delta",
                parsed.path, re.IGNORECASE)):
        raise ValueError("Invalid Graph inbox continuation")
    return link


def _headers(item: dict) -> dict[str, str]:
    headers = {}
    for header in item["internetMessageHeaders"]:
        name = header["name"].lower()
        headers[name] = (headers.get(name, "") + " " + header["value"]).strip()
    return headers


def _inbound_record(item: dict) -> InboundMessageRecord | None:
    if not item["id"]:
        raise ValueError("Missing Graph inbox message identity")
    if "@removed" in item and not isinstance(item["@removed"], dict):
        raise ValueError("Malformed Graph removal")
    if "@removed" in item or item.get("isDraft") is True:
        return None
    headers = _headers(item)
    sender = item["from"]["emailAddress"]["address"]
    received = datetime.fromisoformat(item["receivedDateTime"].replace("Z", "+00:00"))
    if not sender or received.tzinfo is None or not (item.get("conversationId") or "in-reply-to" in headers or "references" in headers):
        raise ValueError("Incomplete Graph inbox message")
    return InboundMessageRecord(
        provider_message_id=item["id"], provider_thread_id=item.get("conversationId") or "",
        rfc_message_id=item.get("internetMessageId") or "", in_reply_to=headers.get("in-reply-to", ""),
        references=headers.get("references", ""), sender_email=sender,
        recipient_email=", ".join(r["emailAddress"]["address"] for r in item["toRecipients"]),
        subject=item["subject"], body_snippet=item["bodyPreview"], received_at=received.astimezone(timezone.utc),
        headers=headers,
    )


class MsGraphMailboxTransport:
    def __init__(self, db: Session, conn: MailboxConnection, http_transport: httpx.BaseTransport | None = None) -> None:
        self.db = db
        self.conn = conn
        self.http_transport = http_transport

    def _get_token(self) -> str:
        creds = decrypt_json(self.conn.encrypted_credentials)
        exp_raw = creds.get("expires_at", "")
        if exp_raw:
            exp = datetime.fromisoformat(exp_raw)
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if exp > utcnow() + timedelta(seconds=90) and creds.get("access_token"):
                return creds["access_token"]
        mcfg = resolve_effective_ms_graph_oauth(self.db)
        with httpx.Client(timeout=15.0, transport=self.http_transport) as client:
            resp = client.post(
                f"https://login.microsoftonline.com/{mcfg.tenant_id}/oauth2/v2.0/token",
                data={
                    "client_id": mcfg.client_id,
                    "client_secret": mcfg.client_secret,
                    "scope": MS_GRAPH_SCOPES,
                    "refresh_token": creds.get("refresh_token", ""),
                    "grant_type": "refresh_token",
                },
            )
        if resp.status_code != 200:
            self.conn.status = "revoked"
            self.db.commit()
            raise PermissionError("Microsoft Graph OAuth refresh failed")
        data = resp.json()
        creds["access_token"] = data["access_token"]
        creds["expires_at"] = (utcnow() + timedelta(seconds=int(data.get("expires_in", 3600)))).isoformat()
        self.conn.encrypted_credentials = encrypt_json(creds)
        self.db.commit()
        return creds["access_token"]

    def send_message(self, msg: OutboundEmailMessage) -> SendResult:
        try:
            if msg.from_email.casefold() != self.conn.sender_email.casefold():
                raise ValueError("Sender differs from connected Graph mailbox")
            token = self._get_token()
            payload = base64.b64encode(build_mime_message(msg).as_bytes(policy=policy.SMTP))
        except Exception as exc:
            return SendResult(outcome=HandoffOutcome.PRE_HANDOFF_FAILURE, rfc_message_id=msg.rfc_message_id, error_message=str(exc))

        try:
            with httpx.Client(timeout=25.0, transport=self.http_transport) as client:
                resp = client.post(
                    "https://graph.microsoft.com/v1.0/me/sendMail",
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "text/plain"},
                    content=payload,
                )
        except httpx.RequestError as exc:
            return SendResult(outcome=HandoffOutcome.AMBIGUOUS_HANDOFF, rfc_message_id=msg.rfc_message_id, error_message=str(exc))

        if resp.status_code == 202:
            # sendMail returns an empty acceptance. Its Sent copy may appear later.
            return self.reconcile_sent_message(msg.rfc_message_id, msg.custom_action_id) or SendResult(
                outcome=HandoffOutcome.PROVIDER_ACCEPTED, rfc_message_id=msg.rfc_message_id,
            )
        if resp.status_code == 429:
            delay = resp.headers.get("Retry-After", "")
            retry_after = int(delay) if delay.isascii() and delay.isdecimal() and len(delay) < 10 else 60
            return SendResult(
                outcome=HandoffOutcome.PROVIDER_REJECTED,
                rfc_message_id=msg.rfc_message_id,
                error_code="RATE_LIMITED",
                error_message="Microsoft Graph rate limit exceeded",
                retry_after_seconds=retry_after,
                is_rate_limit=True,
            )
        return SendResult(
            outcome=(HandoffOutcome.PROVIDER_REJECTED if 400 <= resp.status_code < 500 and resp.status_code != 408
                     else HandoffOutcome.AMBIGUOUS_HANDOFF),
            rfc_message_id=msg.rfc_message_id,
            error_code=f"HTTP_{resp.status_code}",
            error_message=f"Microsoft Graph sendMail returned HTTP {resp.status_code}",
        )

    def reconcile_sent_message(self, rfc_message_id: str, action_id: str) -> SendResult | None:
        if not rfc_message_id or not graph_can_read(self.conn):
            return None
        escaped = rfc_message_id.replace("'", "''")
        try:
            with httpx.Client(timeout=10.0, transport=self.http_transport) as client:
                resp = client.get(
                    GRAPH_ME + "/mailFolders/sentitems/messages",
                    headers={"Authorization": f"Bearer {self._get_token()}", "Prefer": IMMUTABLE_ID},
                    params={"$filter": f"internetMessageId eq '{escaped}'", "$top": 2,
                            "$select": "id,conversationId,internetMessageId,isDraft,from,internetMessageHeaders"},
                )
                if resp.status_code != 200:
                    return None
                data = resp.json()
                if data.get("@odata.nextLink") or len(data["value"]) != 1:
                    return None
                item = data["value"][0]
                if (item["internetMessageId"] != rfc_message_id or not item["id"] or item["isDraft"] is not False
                        or item["from"]["emailAddress"]["address"].casefold() != self.conn.sender_email.casefold()
                        or (action_id and _headers(item).get("x-applyflow-action-id") != action_id)):
                    return None
                return SendResult(outcome=HandoffOutcome.PROVIDER_ACCEPTED, rfc_message_id=rfc_message_id,
                                  provider_message_id=item["id"], provider_thread_id=item.get("conversationId") or "")
        except Exception:
            # Read-back is optional evidence, including after an already accepted handoff.
            return None

    def poll_inbox(self, cursor: str) -> tuple[list[InboundMessageRecord], str]:
        if not self.conn.inbox_poll_enabled or not graph_can_read(self.conn):
            raise ValueError("Graph inbox read unavailable")
        url = DELTA_URL
        for prefix in (DELTA_COMPLETE, DELTA_PENDING):
            if (cursor or "").startswith(prefix):
                url = _delta_link(cursor[len(prefix):])
                break
        records = []
        visited = set()
        headers = {"Authorization": f"Bearer {self._get_token()}", "Prefer": IMMUTABLE_ID + ", odata.maxpagesize=100"}
        with httpx.Client(timeout=20.0, transport=self.http_transport, headers=headers) as client:
            for _ in range(MAX_DELTA_PAGES):
                if url in visited:
                    raise ValueError("Graph inbox paging loop")
                visited.add(url)
                resp = client.get(url, params={"$select": DELTA_FIELDS}) if url == DELTA_URL else client.get(url)
                if resp.status_code == 410 and url != DELTA_URL and DELTA_URL not in visited:
                    # Graph invalidated the checkpoint. Start a new full folder sync.
                    url = DELTA_URL
                    continue
                if resp.status_code != 200:
                    raise ValueError(f"Graph inbox page returned HTTP {resp.status_code}")
                data = resp.json()
                more, complete = data.get("@odata.nextLink"), data.get("@odata.deltaLink")
                if bool(more) == bool(complete) or not isinstance(data["value"], list):
                    raise ValueError("Incomplete Graph inbox page")
                url = _delta_link(more or complete)
                for item in data["value"]:
                    if ("@removed" not in item and item.get("isDraft") is not True
                            and "internetMessageHeaders" not in item):
                        message_id = item["id"]
                        detail = client.get(GRAPH_ME + "/messages/" + quote(message_id, safe=""),
                                            params={"$select": DELTA_FIELDS})
                        if detail.status_code != 200:
                            raise ValueError(f"Graph inbox message returned HTTP {detail.status_code}")
                        item = detail.json()
                        if item.get("id") != message_id:
                            raise ValueError("Graph inbox message identity changed")
                    record = _inbound_record(item)
                    if record is not None:
                        records.append(record)
                if complete:
                    return records, DELTA_COMPLETE + url
        if url == DELTA_URL or url in visited:
            raise ValueError("Graph inbox checkpoint unavailable")
        return records, DELTA_PENDING + url
