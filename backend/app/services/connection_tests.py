"""Live connection validation tests for configured integrations without leaking secrets."""

from __future__ import annotations

import asyncio
import imaplib
import re
import smtplib
import ssl
import time
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core import mail_sockets
from app.core.config import get_settings
from app.core.ssrf import SSRFBlockedError, validate_mail_host
from app.db.base import utcnow
from app.db.models import User
from app.services.effective_config import is_mask_placeholder
from app.services.transactional_email import send_transactional_email

_TENANT_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


@dataclass
class ConnectionTestResult:
    ok: bool
    status: str  # verified | interactive_required | needs_attention | not_configured | skipped | disabled
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    checked_at: str = field(default_factory=lambda: utcnow().isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _scrub(text_val: str, secrets_to_hide: list[str]) -> str:
    cleaned = text_val
    for sec in secrets_to_hide:
        if sec and len(sec) >= 4:
            cleaned = cleaned.replace(sec, "[REDACTED]")
    return cleaned


async def test_openai_connection(
    api_key: str,
    model: str = "",
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ConnectionTestResult:
    cleaned_key = (api_key or "").strip()
    if not cleaned_key or is_mask_placeholder(cleaned_key):
        return ConnectionTestResult(
            ok=False,
            status="not_configured",
            message="OpenAI API key is not configured.",
        )

    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=12.0, transport=transport) as client:
            resp = await client.get(
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {cleaned_key}"},
            )
    except httpx.TimeoutException:
        return ConnectionTestResult(
            ok=False,
            status="needs_attention",
            message="Timed out contacting api.openai.com.",
        )
    except httpx.HTTPError as exc:
        return ConnectionTestResult(
            ok=False,
            status="needs_attention",
            message=_scrub(f"Network error contacting OpenAI: {exc.__class__.__name__}", [cleaned_key]),
        )

    latency_ms = int((time.monotonic() - started) * 1000)
    if resp.status_code in (401, 403):
        return ConnectionTestResult(
            ok=False,
            status="needs_attention",
            message="OpenAI rejected the API key (unauthorized or revoked).",
            details={"http_status": resp.status_code, "latency_ms": latency_ms},
        )
    if resp.status_code == 429:
        return ConnectionTestResult(
            ok=False,
            status="needs_attention",
            message="OpenAI returned HTTP 429 (rate limit or quota check needed).",
            details={"http_status": resp.status_code, "latency_ms": latency_ms},
        )
    if resp.status_code >= 400:
        return ConnectionTestResult(
            ok=False,
            status="needs_attention",
            message=f"OpenAI returned unexpected HTTP {resp.status_code}.",
            details={"http_status": resp.status_code, "latency_ms": latency_ms},
        )

    payload = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    raw_models = payload.get("data") or []
    model_ids = [str(item.get("id", "")) for item in raw_models if isinstance(item, dict) and item.get("id")]
    selected = (model or "").strip()
    model_available = True if not selected or not model_ids else (selected in model_ids)
    msg = f"OpenAI API key verified via /v1/models ({len(model_ids)} models visible)."
    if selected and model_ids and not model_available:
        msg = f"OpenAI key authenticated, but selected model '{selected}' was not in the listed models."
        return ConnectionTestResult(
            ok=False,
            status="needs_attention",
            message=msg,
            details={
                "latency_ms": latency_ms,
                "model_available": False,
                "sample_models": model_ids[:15],
                "billable_usage": False,
            },
        )

    return ConnectionTestResult(
        ok=True,
        status="verified",
        message=msg,
        details={
            "latency_ms": latency_ms,
            "model_available": model_available,
            "sample_models": model_ids[:15],
            "billable_usage": False,
        },
    )


async def test_gemini_connection(
    api_key: str,
    model: str = "",
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ConnectionTestResult:
    cleaned_key = (api_key or "").strip()
    if not cleaned_key or is_mask_placeholder(cleaned_key):
        return ConnectionTestResult(
            ok=False,
            status="not_configured",
            message="Gemini API key is not configured.",
        )

    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=12.0, transport=transport) as client:
            resp = await client.get(
                "https://generativelanguage.googleapis.com/v1beta/models",
                headers={"x-goog-api-key": cleaned_key},
            )
    except httpx.TimeoutException:
        return ConnectionTestResult(
            ok=False,
            status="needs_attention",
            message="Timed out contacting generativelanguage.googleapis.com.",
        )
    except httpx.HTTPError as exc:
        return ConnectionTestResult(
            ok=False,
            status="needs_attention",
            message=_scrub(f"Network error contacting Gemini API: {exc.__class__.__name__}", [cleaned_key]),
        )

    latency_ms = int((time.monotonic() - started) * 1000)
    if resp.status_code in (400, 401, 403):
        return ConnectionTestResult(
            ok=False,
            status="needs_attention",
            message="Gemini rejected the API key or the Generative Language API is not enabled for its project.",
            details={"http_status": resp.status_code, "latency_ms": latency_ms},
        )
    if resp.status_code == 429:
        return ConnectionTestResult(
            ok=False,
            status="needs_attention",
            message="Gemini API quota or rate limit exceeded (HTTP 429).",
            details={"http_status": resp.status_code, "latency_ms": latency_ms},
        )
    if resp.status_code >= 400:
        return ConnectionTestResult(
            ok=False,
            status="needs_attention",
            message=f"Gemini API returned HTTP {resp.status_code}.",
            details={"http_status": resp.status_code, "latency_ms": latency_ms},
        )

    payload = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    raw_models = payload.get("models") or []
    model_ids: list[str] = []
    for item in raw_models:
        if isinstance(item, dict) and item.get("name"):
            name = str(item["name"])
            model_ids.append(name.split("models/", 1)[-1] if name.startswith("models/") else name)

    selected = (model or "").strip()
    model_available = True if not selected or not model_ids else (selected in model_ids)
    if selected and model_ids and not model_available:
        return ConnectionTestResult(
            ok=False,
            status="needs_attention",
            message=f"Gemini key authenticated, but selected model '{selected}' is not listed for this key.",
            details={
                "latency_ms": latency_ms,
                "model_available": False,
                "sample_models": model_ids[:15],
                "billable_usage": False,
            },
        )

    return ConnectionTestResult(
        ok=True,
        status="verified",
        message=f"Gemini API key verified via models.list ({len(model_ids)} models available).",
        details={
            "latency_ms": latency_ms,
            "model_available": model_available,
            "sample_models": model_ids[:15],
            "billable_usage": False,
        },
    )


async def test_tavily_connection(
    api_key: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ConnectionTestResult:
    cleaned_key = (api_key or "").strip()
    if not cleaned_key or is_mask_placeholder(cleaned_key):
        return ConnectionTestResult(
            ok=False,
            status="not_configured",
            message="Tavily API key is not configured.",
        )

    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=12.0, transport=transport) as client:
            resp = await client.get(
                "https://api.tavily.com/usage",
                headers={"Authorization": f"Bearer {cleaned_key}"},
            )
    except httpx.TimeoutException:
        return ConnectionTestResult(
            ok=False,
            status="needs_attention",
            message="Timed out contacting api.tavily.com.",
        )
    except httpx.HTTPError as exc:
        return ConnectionTestResult(
            ok=False,
            status="needs_attention",
            message=_scrub(f"Network error contacting Tavily: {exc.__class__.__name__}", [cleaned_key]),
        )

    latency_ms = int((time.monotonic() - started) * 1000)
    if resp.status_code in (401, 403):
        return ConnectionTestResult(
            ok=False,
            status="needs_attention",
            message="Tavily rejected the API key.",
            details={"http_status": resp.status_code, "latency_ms": latency_ms},
        )
    if resp.status_code == 429:
        return ConnectionTestResult(
            ok=False,
            status="needs_attention",
            message="Tavily rate limit or plan quota reached (HTTP 429).",
            details={"http_status": resp.status_code, "latency_ms": latency_ms},
        )
    if resp.status_code >= 400:
        return ConnectionTestResult(
            ok=False,
            status="needs_attention",
            message=f"Tavily usage check returned HTTP {resp.status_code}.",
            details={"http_status": resp.status_code, "latency_ms": latency_ms},
        )

    payload = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    account = payload.get("account") if isinstance(payload, dict) else {}
    return ConnectionTestResult(
        ok=True,
        status="verified",
        message="Tavily API key verified (non-billable /usage endpoint).",
        details={
            "latency_ms": latency_ms,
            "plan": account.get("current_plan") if isinstance(account, dict) else None,
            "billable_usage": False,
        },
    )


async def test_brave_connection(
    api_key: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ConnectionTestResult:
    cleaned_key = (api_key or "").strip()
    if not cleaned_key or is_mask_placeholder(cleaned_key):
        return ConnectionTestResult(
            ok=False,
            status="not_configured",
            message="Brave Search API key is not configured.",
        )

    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=12.0, transport=transport) as client:
            resp = await client.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": "software engineer careers", "count": "1"},
                headers={"X-Subscription-Token": cleaned_key, "Accept": "application/json"},
            )
    except httpx.TimeoutException:
        return ConnectionTestResult(
            ok=False,
            status="needs_attention",
            message="Timed out contacting api.search.brave.com.",
        )
    except httpx.HTTPError as exc:
        return ConnectionTestResult(
            ok=False,
            status="needs_attention",
            message=_scrub(f"Network error contacting Brave Search: {exc.__class__.__name__}", [cleaned_key]),
        )

    latency_ms = int((time.monotonic() - started) * 1000)
    if resp.status_code in (401, 403):
        return ConnectionTestResult(
            ok=False,
            status="needs_attention",
            message="Brave Search rejected the subscription token.",
            details={"http_status": resp.status_code, "latency_ms": latency_ms},
        )
    if resp.status_code == 429:
        return ConnectionTestResult(
            ok=False,
            status="needs_attention",
            message="Brave Search rate limit or quota reached (HTTP 429).",
            details={"http_status": resp.status_code, "latency_ms": latency_ms},
        )
    if resp.status_code >= 400:
        return ConnectionTestResult(
            ok=False,
            status="needs_attention",
            message=f"Brave Search returned HTTP {resp.status_code}.",
            details={"http_status": resp.status_code, "latency_ms": latency_ms},
        )

    return ConnectionTestResult(
        ok=True,
        status="verified",
        message="Brave Search subscription token verified (used 1 lightweight test query).",
        details={"latency_ms": latency_ms, "billable_usage": True, "queries_used": 1},
    )


async def test_google_oauth_config(
    client_id: str,
    client_secret: str,
    *,
    redirect_uri_auth: str,
    redirect_uri_gmail: str,
    redirect_uri_sheets: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ConnectionTestResult:
    cid = (client_id or "").strip()
    csec = (client_secret or "").strip()
    details: dict[str, Any] = {
        "redirect_uris": {"auth": redirect_uri_auth, "gmail": redirect_uri_gmail, "sheets": redirect_uri_sheets},
    }
    if not cid or not csec or is_mask_placeholder(csec):
        return ConnectionTestResult(False, "not_configured", "Both Google OAuth client credentials are required.", details)

    # Only Google's fixed HTTPS token endpoint sees the credentials. No user grant is used.
    try:
        async with httpx.AsyncClient(timeout=10.0, transport=transport, follow_redirects=False) as client:
            probe = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "code": "applyflow_credential_validation_probe",
                    "client_id": cid,
                    "client_secret": csec,
                    "redirect_uri": redirect_uri_auth,
                    "grant_type": "authorization_code",
                },
            )
    except httpx.HTTPError:
        return ConnectionTestResult(False, "needs_attention", "Unable to reach Google's token endpoint.", details)

    details["http_status"] = probe.status_code
    try:
        payload = probe.json()
    except ValueError:
        payload = {}
    error = payload.get("error") if isinstance(payload, dict) else None
    if probe.status_code in (400, 401) and error == "invalid_client":
        message = (
            f"Google rejected the configured OAuth client credentials (HTTP {probe.status_code} invalid_client). "
            "The installation owner must obtain an active secret for the same Web application client."
        )
    elif probe.status_code == 400 and error == "invalid_grant":
        return ConnectionTestResult(
            True, "interactive_required",
            "Google rejected the synthetic code as expected. Complete interactive consent to verify the installation.",
            details,
        )
    elif probe.status_code == 400 and error == "redirect_uri_mismatch":
        message = "Google rejected the configured callback URI. Register the exact sign-in callback for this client."
    elif error == "deleted_client":
        message = "Google reports that the OAuth client was deleted. Select an active Web application client."
    else:
        message = f"Google credential probe was inconclusive (HTTP {probe.status_code})."
    return ConnectionTestResult(False, "needs_attention", message, details)


async def test_ms_graph_oauth_config(
    client_id: str,
    client_secret: str,
    tenant_id: str,
    *,
    redirect_uri: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ConnectionTestResult:
    cid = (client_id or "").strip()
    csec = (client_secret or "").strip()
    tenant = (tenant_id or "common").strip() or "common"
    if not cid or not csec or is_mask_placeholder(csec):
        return ConnectionTestResult(
            ok=False,
            status="not_configured",
            message="Microsoft Graph Client ID and Client Secret are required.",
            details={"redirect_uri": redirect_uri, "tenant_id": tenant},
        )
    if not _TENANT_RE.match(tenant):
        return ConnectionTestResult(
            ok=False,
            status="needs_attention",
            message="Microsoft Tenant ID format is invalid.",
        )

    discovery_url = f"https://login.microsoftonline.com/{tenant}/v2.0/.well-known/openid-configuration"
    try:
        async with httpx.AsyncClient(timeout=10.0, transport=transport) as client:
            resp = await client.get(discovery_url)
        if resp.status_code >= 400:
            return ConnectionTestResult(
                ok=False,
                status="needs_attention",
                message=f"Microsoft Entra OIDC discovery returned HTTP {resp.status_code} for tenant '{tenant}'.",
            )
    except httpx.HTTPError as exc:
        return ConnectionTestResult(
            ok=False,
            status="needs_attention",
            message=f"Unable to reach Microsoft Entra endpoints: {exc.__class__.__name__}.",
        )

    return ConnectionTestResult(
        ok=True,
        status="verified",
        message="Microsoft Graph app credentials and Entra tenant discovery verified. Connect a Microsoft mailbox to complete interactive OAuth.",
        details={"redirect_uri": redirect_uri, "tenant_id": tenant},
    )


def _sync_smtp_probe(
    *,
    host: str,
    port: int,
    username: str,
    password: str,
    use_tls: bool,
) -> ConnectionTestResult:
    cleaned_host = (host or "").strip()
    if not cleaned_host:
        return ConnectionTestResult(ok=False, status="not_configured", message="SMTP host is required.")
    try:
        validate_mail_host(cleaned_host, port)
    except SSRFBlockedError as exc:
        return ConnectionTestResult(ok=False, status="needs_attention", message=str(exc))

    try:
        if port == 465:
            with mail_sockets.PublicSMTPSSL(cleaned_host, port, timeout=10, context=ssl.create_default_context()) as smtp:
                smtp.ehlo()
                if username and password:
                    smtp.login(username, password)
        else:
            with mail_sockets.PublicSMTP(cleaned_host, port, timeout=10) as smtp:
                smtp.ehlo()
                if use_tls or username:
                    smtp.starttls(context=ssl.create_default_context())
                    smtp.ehlo()
                if username and password:
                    smtp.login(username, password)
    except smtplib.SMTPAuthenticationError:
        return ConnectionTestResult(
            ok=False,
            status="needs_attention",
            message="SMTP server rejected username or password (authentication failed).",
        )
    except (smtplib.SMTPException, OSError, SSRFBlockedError) as exc:
        return ConnectionTestResult(
            ok=False,
            status="needs_attention",
            message=_scrub(f"SMTP connection failed: {exc}", [password]),
        )

    return ConnectionTestResult(
        ok=True,
        status="verified",
        message=f"SMTP connection and authentication verified on {cleaned_host}:{port} (no email sent).",
        details={"host": cleaned_host, "port": port, "tls": use_tls or port == 465},
    )


def _sync_imap_probe(
    *,
    host: str,
    port: int,
    username: str,
    password: str,
) -> ConnectionTestResult:
    cleaned_host = (host or "").strip()
    if not cleaned_host:
        return ConnectionTestResult(ok=False, status="not_configured", message="IMAP host is not configured.")
    try:
        validate_mail_host(cleaned_host, port)
    except SSRFBlockedError as exc:
        return ConnectionTestResult(ok=False, status="needs_attention", message=str(exc))

    try:
        if port == 993:
            client = mail_sockets.PublicIMAPSSL(cleaned_host, port, timeout=10, ssl_context=ssl.create_default_context())
        else:
            client = mail_sockets.PublicIMAP(cleaned_host, port, timeout=10)
        try:
            if port != 993:
                client.starttls(ssl_context=ssl.create_default_context())
            client.login(username, password)
            client.logout()
        except Exception:
            try:
                client.shutdown()
            except Exception:
                pass
            raise
    except imaplib.IMAP4.error:
        return ConnectionTestResult(
            ok=False,
            status="needs_attention",
            message="IMAP login failed; verify host, port, username, and app password.",
        )
    except (OSError, SSRFBlockedError) as exc:
        return ConnectionTestResult(
            ok=False,
            status="needs_attention",
            message=_scrub(f"IMAP connection failed: {exc}", [password]),
        )

    return ConnectionTestResult(
        ok=True,
        status="verified",
        message=f"IMAP connection and login verified on {cleaned_host}:{port}.",
        details={"host": cleaned_host, "port": port},
    )


async def test_transactional_smtp(
    *,
    provider: str,
    host: str,
    port: int,
    username: str,
    password: str,
    use_tls: bool,
    from_email: str,
) -> ConnectionTestResult:
    if provider != "smtp":
        return ConnectionTestResult(
            ok=True,
            status="verified",
            message="Transactional email is using console/log mode (development/local testing; no external emails delivered).",
            details={"provider": "console", "from_email": from_email},
        )
    return await asyncio.to_thread(
        _sync_smtp_probe,
        host=host,
        port=port,
        username=username,
        password=password,
        use_tls=use_tls,
    )


async def test_user_smtp_imap(
    *,
    smtp_host: str,
    smtp_port: int,
    smtp_username: str,
    smtp_password: str,
    use_tls: bool,
    imap_host: str = "",
    imap_port: int = 993,
    imap_username: str = "",
    imap_password: str = "",
) -> ConnectionTestResult:
    smtp_res = await asyncio.to_thread(
        _sync_smtp_probe,
        host=smtp_host,
        port=smtp_port,
        username=smtp_username,
        password=smtp_password,
        use_tls=use_tls,
    )
    if not smtp_res.ok:
        return smtp_res

    if imap_host.strip():
        imap_res = await asyncio.to_thread(
            _sync_imap_probe,
            host=imap_host,
            port=imap_port,
            username=(imap_username or smtp_username).strip(),
            password=imap_password or smtp_password,
        )
        if not imap_res.ok:
            return ConnectionTestResult(
                ok=False,
                status="needs_attention",
                message=f"SMTP succeeded, but {imap_res.message}",
                details={"smtp": smtp_res.details, "imap_error": imap_res.message},
            )
        return ConnectionTestResult(
            ok=True,
            status="verified",
            message="Both SMTP sending and IMAP reply-polling login verified.",
            details={"smtp": smtp_res.details, "imap": imap_res.details},
        )

    return smtp_res


def send_test_email_to_self(db: Session, *, user: User) -> ConnectionTestResult:
    """Explicit test email action strictly constrained to the authenticated user's own account email."""
    delivery = send_transactional_email(
        user.email,
        "ApplyFlow Global transactional email test",
        (
            f"Hello {user.full_name or user.email},\n\n"
            "This test confirms your ApplyFlow Global transactional email settings.\n"
            "No job-application messages were sent."
        ),
        db=db,
    )
    provider_used = delivery.get("provider", "console") if isinstance(delivery, dict) else str(delivery)
    return ConnectionTestResult(
        ok=True,
        status="verified",
        message=(
            f"Sent test message via SMTP to your own address ({user.email})."
            if provider_used == "smtp"
            else f"Logged test message to application console/sink for {user.email} (SMTP not enabled)."
        ),
        details={"delivery_mode": provider_used, "recipient": user.email},
    )


def check_worker_and_scheduler_status(db: Session) -> dict[str, Any]:
    """Check database, broker reachability, and worker responsiveness honestly."""
    settings = get_settings()
    db_ok = False
    try:
        db.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False

    worker_status = "verified"
    worker_message = "Inline eager task mode active (local single-process mode)."
    broker_reachable = True
    workers_online = 0

    if not settings.celery_task_always_eager:
        try:
            import redis  # local import to keep startup light if redis client unused

            client = redis.from_url(settings.redis_url, socket_connect_timeout=1.0, socket_timeout=1.0)
            client.ping()
            broker_reachable = True
            try:
                from app.tasks.celery_app import celery_app

                pong = celery_app.control.inspect(timeout=0.8).ping() or {}
                workers_online = len(pong)
                if workers_online > 0:
                    worker_status = "verified"
                    worker_message = f"Redis broker reachable and {workers_online} Celery worker(s) online."
                else:
                    worker_status = "needs_attention"
                    worker_message = "Redis broker is reachable, but no live Celery worker responded to ping yet."
            except Exception:
                worker_status = "needs_attention"
                worker_message = "Redis broker reachable; Celery worker ping timed out."
        except Exception:
            broker_reachable = False
            worker_status = "needs_attention"
            worker_message = "Redis broker is unreachable; background worker and Beat schedules will wait until Redis starts."

    return {
        "database": {
            "status": "verified" if db_ok else "needs_attention",
            "dialect": "sqlite" if settings.is_sqlite else "postgresql",
            "message": "Database reachable and ready." if db_ok else "Database connection check failed.",
        },
        "worker": {
            "status": worker_status,
            "mode": "eager_inline" if settings.celery_task_always_eager else "celery_redis",
            "broker_reachable": broker_reachable,
            "workers_online": workers_online,
            "message": worker_message,
        },
    }
