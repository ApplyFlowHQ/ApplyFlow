"""Transactional email adapter for account verification and password recovery."""

from __future__ import annotations

import logging
import ssl
from email.message import EmailMessage

from sqlalchemy.orm import Session

from app.core import mail_sockets
from app.core.security import sanitize_header_value
from app.core.ssrf import validate_mail_host
from app.services.effective_config import resolve_effective_transactional_email

logger = logging.getLogger(__name__)


def send_transactional_email(
    to_email: str,
    subject: str,
    body_text: str,
    *,
    db: Session | None = None,
) -> dict[str, str]:
    """
    Send a verification or password-recovery email through the effective transactional adapter.
    In console/dev mode, logs a safe delivery notice without exposing secrets.
    """
    safe_to = sanitize_header_value(to_email, 254)
    safe_subject = sanitize_header_value(subject, 200)

    if db is not None:
        cfg = resolve_effective_transactional_email(db)
    else:
        from app.core.config import get_settings

        s = get_settings()
        cfg = type(
            "_Cfg",
            (),
            {
                "provider": s.transactional_email_provider,
                "from_email": s.transactional_from_email,
                "smtp_host": s.transactional_smtp_host,
                "smtp_port": s.transactional_smtp_port,
                "smtp_username": s.transactional_smtp_username,
                "smtp_password": s.transactional_smtp_password,
                "smtp_use_tls": s.transactional_smtp_use_tls,
            },
        )()

    if cfg.provider == "smtp" and cfg.smtp_host:
        validate_mail_host(cfg.smtp_host, cfg.smtp_port)
        msg = EmailMessage()
        msg["From"] = cfg.from_email
        msg["To"] = safe_to
        msg["Subject"] = safe_subject
        msg.set_content(body_text)

        context = ssl.create_default_context()
        if cfg.smtp_port == 465:
            with mail_sockets.PublicSMTPSSL(cfg.smtp_host, cfg.smtp_port, context=context, timeout=15) as server:
                if cfg.smtp_username:
                    server.login(cfg.smtp_username, cfg.smtp_password)
                server.send_message(msg)
        else:
            with mail_sockets.PublicSMTP(cfg.smtp_host, cfg.smtp_port, timeout=15) as server:
                if cfg.smtp_use_tls or cfg.smtp_username:
                    server.starttls(context=context)
                if cfg.smtp_username:
                    server.login(cfg.smtp_username, cfg.smtp_password)
                server.send_message(msg)
        return {"provider": "smtp", "status": "sent"}

    logger.info(
        "Transactional email queued in console mode",
        extra={"extra_fields": {"to": safe_to, "subject": safe_subject}},
    )
    return {"provider": "console", "status": "logged_only"}
