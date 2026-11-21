"""Pytest fixtures for isolated SQLite test DB, FastAPI TestClient, and synthetic PDF/DOCX builders."""

from __future__ import annotations

import io
from pathlib import Path

import docx
import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.db.base import Base
from app.db.session import get_sessionmaker, reset_engine
from app.main import create_app


@pytest.fixture(autouse=True)
def isolated_test_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "test_applyflow.db"
    storage_dir = tmp_path / "private_storage"
    storage_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("LOCAL_STORAGE_PATH", str(storage_dir))
    monkeypatch.setenv("CELERY_TASK_ALWAYS_EAGER", "true")
    monkeypatch.setenv("LIVE_SENDING_ENABLED", "false")
    monkeypatch.setenv("APP_SECRET_KEY", "test-secret-key-for-applyflow-unit-tests-123456")
    monkeypatch.setenv("MAILBOX_ENCRYPTION_KEY", "")
    monkeypatch.setenv("OPERATOR_ADMIN_EMAILS", "operator@applyflow.example")

    get_settings.cache_clear()
    engine = reset_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    yield tmp_path
    engine.dispose()
    get_settings.cache_clear()


@pytest.fixture
def db_session():
    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client():
    app = create_app()
    with TestClient(app) as tc:
        yield tc


def _assemble_single_page_pdf(stream_content: bytes) -> bytes:
    objects: list[bytes] = []
    objects.append(b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n")
    objects.append(b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n")
    objects.append(
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>\nendobj\n"
    )
    objects.append(b"4 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n")
    objects.append(
        f"5 0 obj\n<< /Length {len(stream_content)} >>\nstream\n".encode("ascii")
        + stream_content
        + b"\nendstream\nendobj\n"
    )

    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for obj in objects:
        offsets.append(len(out))
        out.extend(obj)
    xref_start = len(out)
    out.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode("ascii"))
    for off in offsets[1:]:
        out.extend(f"{off:010d} 00000 n \n".encode("ascii"))
    out.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref_start}\n%%EOF\n".encode("ascii"))
    return bytes(out)


def build_pdf_bytes(lines: list[str]) -> bytes:
    ops = ["BT", "/F1 12 Tf", "50 750 Td", "16 TL"]
    for idx, line in enumerate(lines):
        escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        if idx == 0:
            ops.append(f"({escaped}) Tj")
        else:
            ops.append(f"T* ({escaped}) Tj")
    ops.append("ET")
    return _assemble_single_page_pdf("\n".join(ops).encode("latin-1", errors="replace"))


def build_blank_scanned_pdf_bytes() -> bytes:
    return _assemble_single_page_pdf(b"q 0.1 0.1 0.1 rg 50 50 200 200 re f Q")


def build_docx_bytes(lines: list[str]) -> bytes:
    doc = docx.Document()
    for ln in lines:
        doc.add_paragraph(ln)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
