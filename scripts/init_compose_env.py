"""Create the private Compose configuration once, without logging credentials."""

import os
import secrets
from pathlib import Path

from generate_secrets import generate_secrets


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    settings = generate_secrets()
    password = secrets.token_urlsafe(32)
    settings["POSTGRES_PASSWORD"] = password
    settings["DATABASE_URL"] = (
        f"postgresql+psycopg://applyflow:{password}@postgres:5432/applyflow"
    )
    lines = (root / ".env.example").read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        key = line.partition("=")[0]
        if key in settings:
            lines[index] = f"{key}={settings[key]}"
    try:
        fd = os.open(root / ".env", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise SystemExit("A local .env already exists. Use the existing configuration.")
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write("\n".join(lines) + "\n")
    print("Created private Compose configuration in .env")


if __name__ == "__main__":
    main()
