"""Local operator bootstrap CLI for encryption integrity checks and one-time setup claim tokens."""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from datetime import timedelta

from sqlalchemy import func, inspect, select

from app.core.config import get_settings
from app.core.encryption import decrypt_json, hash_token
from app.db.base import as_utc, utcnow
from app.db.models import (
    InstallationConfig,
    MailboxConnection,
    SetupClaimToken,
    SheetsConnection,
    User,
    UserIntegrationConfig,
)
from app.db.session import get_engine, session_scope


def check_encryption_integrity() -> int:
    """
    Verify that any existing encrypted credentials in the database can still be decrypted.
    Refuses to silently mask key loss on routine startups.
    """
    engine = get_engine()
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    if not existing_tables:
        print(json.dumps({"status": "clean_db", "ok": True}))
        return 0

    settings = get_settings()
    encrypted_samples: list[tuple[str, str]] = []

    with session_scope() as db:
        if "installation_configs" in existing_tables:
            for row in db.scalars(select(InstallationConfig).where(InstallationConfig.encrypted_secrets_json != "")).all():
                encrypted_samples.append(("installation_configs", row.encrypted_secrets_json))
        if "user_integration_configs" in existing_tables:
            for row in db.scalars(
                select(UserIntegrationConfig).where(UserIntegrationConfig.encrypted_secrets_json != "")
            ).all():
                encrypted_samples.append(("user_integration_configs", row.encrypted_secrets_json))
        if "mailbox_connections" in existing_tables:
            for row in db.scalars(select(MailboxConnection).where(MailboxConnection.encrypted_credentials != "")).all():
                encrypted_samples.append(("mailbox_connections", row.encrypted_credentials))
        if "sheets_connections" in existing_tables:
            for row in db.scalars(select(SheetsConnection).where(SheetsConnection.encrypted_credentials != "")).all():
                encrypted_samples.append(("sheets_connections", row.encrypted_credentials))

    if not encrypted_samples:
        print(json.dumps({"status": "no_encrypted_records", "ok": True}))
        return 0

    if settings.environment == "production" and not settings.mailbox_encryption_key.strip():
        print(
            json.dumps(
                {
                    "status": "recovery_problem",
                    "ok": False,
                    "message": (
                        "RECOVERY_PROBLEM: Encrypted credentials exist in the database, but MAILBOX_ENCRYPTION_KEY "
                        "is missing. Restore your original encryption key instead of regenerating a replacement."
                    ),
                }
            ),
            file=sys.stderr,
        )
        return 2

    for table_name, ciphertext in encrypted_samples:
        try:
            decrypt_json(ciphertext)
        except Exception:
            print(
                json.dumps(
                    {
                        "status": "recovery_problem",
                        "ok": False,
                        "table": table_name,
                        "message": (
                            f"RECOVERY_PROBLEM: Stored encrypted data in '{table_name}' cannot be decrypted with "
                            "the current MAILBOX_ENCRYPTION_KEY/APP_SECRET_KEY. Restore the original key rather than generating a new one."
                        ),
                    }
                ),
                file=sys.stderr,
            )
            return 2

    print(json.dumps({"status": "verified", "ok": True, "checked_records": len(encrypted_samples)}))
    return 0


def ensure_setup_claim_token(*, ttl_hours: int = 24, recover: bool = False, reissue: bool = False) -> int:
    """
    Create a high-entropy, expiring one-time setup claim code when needed, storing only its SHA-256 hash.
    Outputs JSON to stdout strictly for the local operator launcher (not written to ordinary server logs).
    """
    now = utcnow()
    with session_scope() as db:
        inst = db.scalar(select(InstallationConfig).where(InstallationConfig.id == "default"))
        if inst is None:
            settings = get_settings()
            inst = InstallationConfig(
                id="default",
                setup_state="unclaimed",
                current_step=1,
                deployment_mode="hosted" if settings.environment == "production" else "local",
                live_sending_enabled=False,
            )
            db.add(inst)
            db.flush()

        if recover:
            # Explicit local host operator recovery resets ownership claim pointer safely
            inst.owner_user_id = None
            inst.setup_state = "unclaimed"
            db.flush()

        if inst.owner_user_id is not None:
            print(json.dumps({"status": "already_claimed", "requires_claim": False}))
            return 0

        existing_ops = db.scalar(
            select(func.count(User.id)).where(User.is_operator.is_(True), User.deleted_at.is_(None))
        ) or 0
        if existing_ops > 0 and not recover and not reissue:
            print(
                json.dumps(
                    {
                        "status": "existing_operator",
                        "requires_claim": False,
                        "message": "Existing authorized administrator found. Sign in or rerun with --recover to generate an owner-recovery code.",
                    }
                )
            )
            return 0

        active_tokens = db.scalars(
            select(SetupClaimToken).where(SetupClaimToken.consumed_at.is_(None)).order_by(SetupClaimToken.created_at.desc())
        ).all()
        valid_active = [t for t in active_tokens if (exp := as_utc(t.expires_at)) is not None and exp > now]

        if valid_active and not reissue and not recover:
            exp_iso = as_utc(valid_active[0].expires_at)
            print(
                json.dumps(
                    {
                        "status": "active_token_exists",
                        "requires_claim": True,
                        "expires_at": exp_iso.isoformat() if exp_iso else "",
                        "message": "An active one-time setup code is already awaiting claim. Pass --reissue to rotate it.",
                    }
                )
            )
            return 0

        # Expire any prior unconsumed claim tokens cleanly before issuing the replacement
        for tok in active_tokens:
            tok.expires_at = now - timedelta(seconds=1)

        raw_code = f"af_setup_{secrets.token_urlsafe(24)}"
        expires_at = now + timedelta(hours=max(1, ttl_hours))
        db.add(
            SetupClaimToken(
                token_hash=hash_token(raw_code),
                purpose="owner_recovery" if recover else "initial_claim",
                expires_at=expires_at,
            )
        )
        db.commit()

        print(
            json.dumps(
                {
                    "status": "token_issued",
                    "requires_claim": True,
                    "purpose": "owner_recovery" if recover else "initial_claim",
                    "expires_at": expires_at.isoformat(),
                    "setup_code": raw_code,
                }
            )
        )
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="ApplyFlow Global local bootstrap utilities")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check-encryption-integrity", help="Verify existing encrypted DB records match the configured key")

    claim_p = sub.add_parser("ensure-setup-claim-token", help="Generate or inspect one-time setup claim token")
    claim_p.add_argument("--ttl-hours", type=int, default=24)
    claim_p.add_argument("--recover", action="store_true", help="Issue owner recovery code on an existing installation")
    claim_p.add_argument("--reissue", action="store_true", help="Rotate an active unclaimed setup code")

    args = parser.parse_args()
    if args.command == "check-encryption-integrity":
        return check_encryption_integrity()
    if args.command == "ensure-setup-claim-token":
        return ensure_setup_claim_token(ttl_hours=args.ttl_hours, recover=args.recover, reissue=args.reissue)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
