#!/usr/bin/env python3
"""Generate local cryptographic secrets for ApplyFlow Global without inventing external credentials."""

from __future__ import annotations

import base64
import secrets
from pathlib import Path


def generate_secrets() -> dict[str, str]:
    app_secret = secrets.token_hex(32)
    fernet_key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
    return {
        "APP_SECRET_KEY": app_secret,
        "MAILBOX_ENCRYPTION_KEY": fernet_key,
    }


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Generate local cryptographic keys for ApplyFlow Global."
    )
    parser.add_argument(
        "--write-local-env",
        action="store_true",
        help="Create a local development .env from .env.example (SQLite + eager tasks for dry-runs only) if .env does not exist.",
    )
    args = parser.parse_args()

    generated = generate_secrets()
    if args.write_local_env:
        import os

        env_path = Path(__file__).resolve().parents[1] / ".env"
        values = {
            **generated,
            "DATABASE_URL": "sqlite:///./data/applyflow_dev.db",
            "CELERY_TASK_ALWAYS_EAGER": "true",
            "LIVE_SENDING_ENABLED": "false",
        }
        try:
            fd = os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise SystemExit("A local .env already exists.")
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            for key, value in values.items():
                stream.write(f"{key}={value}\n")
        print("Initialized private SQLite development configuration.")
    else:
        for key, value in generated.items():
            print(f"{key}={value}")


if __name__ == "__main__":
    main()
