"""Optional Google Sheets synchronization using minimal drive.file scope and stable application IDs."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.encryption import decrypt_json, encrypt_json, generate_opaque_token, generate_pkce_pair, hash_token
from app.db.base import utcnow
from app.db.models import (
    Application,
    AuditEvent,
    AuthToken,
    Batch,
    JobMatch,
    JobPosting,
    SheetsConnection,
    SheetsRowMapping,
    User,
)
from app.services.auth import consume_google_state
from app.services.effective_config import resolve_effective_google_oauth
from app.services.exports import TRACKER_HEADERS, build_tracker_rows

SHEETS_MINIMAL_SCOPE = "https://www.googleapis.com/auth/drive.file"


def build_sheets_oauth_url(db: Session, *, user_id: str) -> str:
    gcfg = resolve_effective_google_oauth(db)
    if not gcfg.client_id:
        raise ValueError("Google OAuth client credentials are not configured on this installation")

    state = generate_opaque_token(24)
    verifier, challenge = generate_pkce_pair()
    db.add(
        AuthToken(
            user_id=user_id,
            purpose="oauth_state",
            token_hash=hash_token(state),
            payload_json={"flow": "google_sheets", "code_verifier": verifier},
            expires_at=utcnow() + timedelta(minutes=15),
        )
    )
    db.commit()

    params = {
        "client_id": gcfg.client_id,
        "redirect_uri": gcfg.redirect_uri_sheets,
        "response_type": "code",
        "scope": SHEETS_MINIMAL_SCOPE,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "access_type": "offline",
        "prompt": "consent",
    }
    return f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"


def complete_sheets_oauth_callback(
    db: Session,
    *,
    user_id: str,
    code: str | None,
    state: str,
) -> SheetsConnection:
    if not user_id or not state:
        raise ValueError("An authenticated Google Sheets connection request is required")
    flow = consume_google_state(db, state=state, flow="google_sheets", user_id=user_id)
    if not code:
        raise ValueError("Google Sheets authorization was not completed")
    gcfg = resolve_effective_google_oauth(db)

    with httpx.Client(timeout=20.0) as client:
        resp = client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": gcfg.client_id,
                "client_secret": gcfg.client_secret,
                "redirect_uri": gcfg.redirect_uri_sheets,
                "grant_type": "authorization_code",
                "code_verifier": flow["code_verifier"],
            },
        )
        if resp.status_code != 200:
            raise ValueError(f"Google Sheets OAuth exchange failed (HTTP {resp.status_code})")
        token_data = resp.json()

    creds = {
        "access_token": token_data["access_token"],
        "refresh_token": token_data.get("refresh_token", ""),
        "expires_at": (utcnow() + timedelta(seconds=int(token_data.get("expires_in", 3600)))).isoformat(),
    }

    conn = db.scalar(select(SheetsConnection).where(SheetsConnection.user_id == user_id))
    if not conn:
        conn = SheetsConnection(user_id=user_id)
        db.add(conn)
    conn.encrypted_credentials = encrypt_json(creds)
    conn.granted_scopes_json = str(token_data.get("scope", "")).split()
    conn.status = "connected"
    conn.last_error = ""
    db.add(AuditEvent(user_id=user_id, actor_user_id=user_id, action="sheets_oauth_connected", target_type="user", target_id=user_id))
    db.commit()
    db.refresh(conn)
    return conn


def _refresh_sheets_token(db: Session, conn: SheetsConnection, http_transport: httpx.BaseTransport | None = None) -> str:
    creds = decrypt_json(conn.encrypted_credentials)
    exp_raw = creds.get("expires_at", "")
    if exp_raw:
        exp = datetime.fromisoformat(exp_raw)
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp > utcnow() + timedelta(seconds=90) and creds.get("access_token"):
            return creds["access_token"]
    gcfg = resolve_effective_google_oauth(db)
    with httpx.Client(timeout=15.0, transport=http_transport) as client:
        resp = client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": gcfg.client_id,
                "client_secret": gcfg.client_secret,
                "refresh_token": creds.get("refresh_token", ""),
                "grant_type": "refresh_token",
            },
        )
    if resp.status_code != 200:
        conn.status = "error"
        conn.last_error = f"Google Sheets token refresh failed (HTTP {resp.status_code})"
        db.commit()
        raise PermissionError(conn.last_error)
    data = resp.json()
    creds["access_token"] = data["access_token"]
    creds["expires_at"] = (utcnow() + timedelta(seconds=int(data.get("expires_in", 3600)))).isoformat()
    conn.encrypted_credentials = encrypt_json(creds)
    db.commit()
    return creds["access_token"]


def sync_applications_to_google_sheet(
    db: Session,
    *,
    user_id: str,
    http_transport: httpx.BaseTransport | None = None,
) -> dict[str, object]:
    """
    Idempotently sync user's PostgreSQL-authoritative applications to Google Sheets.
    Uses `SheetsRowMapping` keyed by stable `application_id` so retries NEVER duplicate rows.
    """
    conn = db.scalar(select(SheetsConnection).where(SheetsConnection.user_id == user_id))
    if not conn or conn.status != "connected":
        raise ValueError("Google Sheets is not connected")

    token = _refresh_sheets_token(db, conn, http_transport=http_transport)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    with httpx.Client(timeout=20.0, transport=http_transport) as client:
        if not conn.spreadsheet_id:
            create_resp = client.post(
                "https://sheets.googleapis.com/v4/spreadsheets",
                headers=headers,
                json={
                    "properties": {"title": "ApplyFlow Global — Applications Tracker"},
                    "sheets": [{"properties": {"title": conn.sheet_name or "Applications"}}],
                },
            )
            if create_resp.status_code != 200:
                raise RuntimeError(f"Failed creating Google Sheet (HTTP {create_resp.status_code})")
            sdata = create_resp.json()
            conn.spreadsheet_id = sdata["spreadsheetId"]
            conn.spreadsheet_url = sdata.get("spreadsheetUrl", "")
            # Write header row 1
            client.put(
                f"https://sheets.googleapis.com/v4/spreadsheets/{conn.spreadsheet_id}/values/{conn.sheet_name}!A1",
                headers=headers,
                params={"valueInputOption": "RAW"},
                json={"values": [TRACKER_HEADERS]},
            )
            db.commit()

        apps = db.scalars(
            select(Application)
            .where(Application.user_id == user_id, Application.is_demo.is_(False))
            .order_by(Application.created_at.asc())
        ).all()
        user_obj = db.scalar(select(User).where(User.id == user_id))
        tz_name = user_obj.timezone if user_obj else "UTC"
        batch_ids = {a.batch_id for a in apps if a.batch_id}
        post_ids = {a.job_posting_id for a in apps if a.job_posting_id}
        match_ids = {a.job_match_id for a in apps if a.job_match_id}
        batches_by_id = {b.id: b for b in db.scalars(select(Batch).where(Batch.id.in_(batch_ids))).all()} if batch_ids else {}
        postings_by_id = {p.id: p for p in db.scalars(select(JobPosting).where(JobPosting.id.in_(post_ids))).all()} if post_ids else {}
        matches_by_id = {m.id: m for m in db.scalars(select(JobMatch).where(JobMatch.id.in_(match_ids))).all()} if match_ids else {}
        built_rows = build_tracker_rows(
            apps,
            batches_by_id=batches_by_id,
            postings_by_id=postings_by_id,
            matches_by_id=matches_by_id,
            cv_labels_by_version_id={},
            tz_name=tz_name,
        )
        existing_mappings = {
            m.application_id: m
            for m in db.scalars(select(SheetsRowMapping).where(SheetsRowMapping.user_id == user_id)).all()
        }
        next_row = max((m.row_number for m in existing_mappings.values()), default=1) + 1

        synced_count = 0
        for app, row_values in zip(apps, built_rows, strict=True):
            row_hash = hashlib.sha256(json.dumps(row_values, default=str).encode("utf-8")).hexdigest()
            mapping = existing_mappings.get(app.id)
            if mapping and mapping.spreadsheet_id == conn.spreadsheet_id and mapping.last_synced_hash == row_hash:
                continue

            if mapping and mapping.spreadsheet_id == conn.spreadsheet_id:
                row_num = mapping.row_number
            else:
                row_num = next_row
                next_row += 1
                mapping = SheetsRowMapping(
                    user_id=user_id,
                    application_id=app.id,
                    spreadsheet_id=conn.spreadsheet_id,
                    row_number=row_num,
                    last_synced_hash="",
                )
                db.add(mapping)

            put_resp = client.put(
                f"https://sheets.googleapis.com/v4/spreadsheets/{conn.spreadsheet_id}/values/{conn.sheet_name}!A{row_num}",
                headers=headers,
                params={"valueInputOption": "RAW"},
                json={"values": [row_values]},
            )
            if put_resp.status_code == 200:
                mapping.last_synced_hash = row_hash
                synced_count += 1

        conn.last_synced_at = utcnow()
        conn.last_error = ""
        db.commit()
        return {
            "spreadsheet_id": conn.spreadsheet_id,
            "spreadsheet_url": conn.spreadsheet_url,
            "rows_synced": synced_count,
            "total_applications": len(apps),
        }
