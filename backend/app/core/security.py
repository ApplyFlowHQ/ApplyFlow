"""Password hashing, sanitization, header injection defense, and CSRF helpers."""

from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path
from typing import Any

from pwdlib import PasswordHash

_PASSWORD_HASHER = PasswordHash.recommended()

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
_HEADER_CONTROL_RE = re.compile(r"[\r\n\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._\-\u0600-\u06FF ]+")


def hash_password(password: str) -> str:
    """Hash password with Argon2id via pwdlib."""
    return _PASSWORD_HASHER.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    """Verify plaintext password against stored Argon2id hash."""
    if not password or not hashed:
        return False
    try:
        return _PASSWORD_HASHER.verify(password, hashed)
    except Exception:
        return False


def sanitize_header_value(value: str, max_len: int = 300) -> str:
    """Strip CR/LF and control characters to block email header injection."""
    cleaned = _HEADER_CONTROL_RE.sub(" ", value or "").strip()
    return cleaned[:max_len]


def assert_no_header_injection(value: str, field_name: str = "header") -> str:
    """Reject strings containing CR/LF/NUL characters."""
    if "\r" in value or "\n" in value or "\x00" in value:
        raise ValueError(f"Invalid control character in {field_name}")
    return value


def sanitize_spreadsheet_cell(value: Any) -> Any:
    """Neutralize spreadsheet formula injection (=, +, -, @, tab, CR) on CSV/XLSX export."""
    if value is None or isinstance(value, (int, float, bool)):
        return value
    text = str(value)
    if not text:
        return text
    stripped = text.lstrip(" ")
    if text.startswith(_FORMULA_PREFIXES) or stripped.startswith(_FORMULA_PREFIXES):
        return f"'{text}"
    return text


def sanitize_filename(filename: str, default: str = "document") -> str:
    """Produce a safe basename without path traversal or control bytes."""
    base = Path(filename or "").name
    base = unicodedata.normalize("NFKC", base)
    base = _HEADER_CONTROL_RE.sub("", base)
    base = base.replace("..", ".").strip(" .")
    base = _SAFE_FILENAME_RE.sub("_", base).strip("_ ")
    if not base:
        return default
    return base[:160]


def normalize_email_display(email: str) -> str:
    """Validate basic formatting and normalize domain casing while preserving local part."""
    cleaned = sanitize_header_value(email, max_len=254).strip()
    if "@" not in cleaned or cleaned.count("@") != 1:
        raise ValueError("Invalid email syntax")
    local, domain = cleaned.split("@", 1)
    if not local or not domain or "." not in domain:
        raise ValueError("Invalid email domain")
    return f"{local}@{domain.lower()}"


def normalize_email_dedupe_key(email: str) -> str:
    """
    Canonical case-insensitive per-recipient dedupe key.

    Preserves plus-tags and dots in the local part because many mail servers treat
    them as distinct mailboxes; only normalizes Unicode NFKC and lowercases.
    """
    cleaned = unicodedata.normalize("NFKC", email.strip())
    return cleaned.lower()


def validate_magic_bytes(data: bytes, extension: str) -> str:
    """Verify actual uploaded file signature matches PDF or DOCX, ignoring client MIME claims."""
    ext = extension.lower().lstrip(".")
    if ext == "pdf":
        if not data[:1024].lstrip().startswith(b"%PDF-"):
            raise ValueError("File bytes do not match PDF header signature")
        return "application/pdf"
    if ext == "docx":
        if not data.startswith((b"PK\x03\x04", b"PK\x05\x06")):
            raise ValueError("File bytes do not match DOCX ZIP archive signature")
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    raise ValueError("Unsupported file format; only PDF and DOCX are accepted")


def is_safe_relative_storage_key(key: str) -> bool:
    """Reject absolute paths, drive letters, and parent directory segments."""
    if not key or os.path.isabs(key) or "\\" in key or ":" in key:
        return False
    parts = key.split("/")
    return all(part not in ("", ".", "..") for part in parts)
