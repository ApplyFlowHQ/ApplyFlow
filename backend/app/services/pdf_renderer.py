"""Deterministic original-preserving DOCX-to-PDF conversion and verified attachment resolution."""

from __future__ import annotations

import io
import shutil
import subprocess
import tempfile
import textwrap
import zipfile
from pathlib import Path

import docx
from pypdf import PdfReader

from app.core.config import get_settings
from app.services.storage import StorageAdapter, compute_sha256, get_storage_adapter

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PDF_MIME = "application/pdf"


def requires_docx_only(requirements: list[str] | None) -> bool:
    formats = {item.strip().casefold() for item in (requirements or [])}
    return "docx" in formats and "pdf" not in formats


DOCX_ONLY_REASON = "Employer requires DOCX only. ApplyFlow sends PDF CVs."

_PUNCT_MAP = str.maketrans(
    {
        "\u2013": "-",
        "\u2014": "-",
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2022": "*",
        "\u2026": "...",
        "\u00a0": " ",
    }
)


def _pdf_escape_winansi(text: str) -> bytes:
    cleaned = (text or "").translate(_PUNCT_MAP)
    cleaned = cleaned.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return cleaned.encode("latin-1", errors="replace")


def _extract_structured_lines_from_docx(docx_bytes: bytes) -> list[tuple[str, bool]]:
    """Return list of (line_text, is_heading) extracted deterministically from DOCX paragraphs and tables."""
    settings = get_settings()
    # Reuse zip-bomb size safety check from cv_parser
    from app.services.cv_parser import _check_docx_decompression_bounds

    _check_docx_decompression_bounds(docx_bytes, settings.cv_max_decompressed_bytes)
    doc = docx.Document(io.BytesIO(docx_bytes))
    lines: list[tuple[str, bool]] = []

    for para in doc.paragraphs:
        raw = " ".join((para.text or "").split())
        if not raw:
            continue
        style_name = (para.style.name or "").lower() if para.style else ""
        is_heading = "heading" in style_name or "title" in style_name
        lines.append((raw, is_heading))

    for table in doc.tables:
        for row in table.rows:
            cells = [" ".join((c.text or "").split()) for c in row.cells]
            row_txt = " | ".join(c for c in cells if c)
            if row_txt:
                lines.append((row_txt, False))

    return lines


def _assemble_multipage_pdf(pages_ops: list[bytes]) -> bytes:
    """Assemble a standards-compliant multi-page PDF 1.4 document with Helvetica & Helvetica-Bold Type1 fonts."""
    if not pages_ops:
        pages_ops = [b"BT /F1 11 Tf 50 740 Td (Document) Tj ET"]

    # Object numbering:
    # 1: Catalog
    # 2: Pages
    # 3: Font F1 (Helvetica)
    # 4: Font F2 (Helvetica-Bold)
    # Then for each page i: (5 + 2*i) = Page, (6 + 2*i) = Contents
    objects: list[bytes] = []
    page_obj_nums = [5 + 2 * i for i in range(len(pages_ops))]
    kids_str = " ".join(f"{n} 0 R" for n in page_obj_nums)

    objects.append(b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n")
    objects.append(f"2 0 obj\n<< /Type /Pages /Kids [{kids_str}] /Count {len(pages_ops)} >>\nendobj\n".encode("ascii"))
    objects.append(
        b"3 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>\nendobj\n"
    )
    objects.append(
        b"4 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>\nendobj\n"
    )

    for i, stream_bytes in enumerate(pages_ops):
        p_num = 5 + 2 * i
        c_num = p_num + 1
        objects.append(
            (
                f"{p_num} 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents {c_num} 0 R >>\nendobj\n"
            ).encode("ascii")
        )
        objects.append(
            f"{c_num} 0 obj\n<< /Length {len(stream_bytes)} >>\nstream\n".encode("ascii")
            + stream_bytes
            + b"\nendstream\nendobj\n"
        )

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
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


def _render_lines_to_pdf_bytes(structured_lines: list[tuple[str, bool]]) -> bytes:
    """Wrap lines cleanly across Letter pages and render readable PDF 1.4 bytes."""
    wrapped_rows: list[tuple[str, bool]] = []
    for text, is_heading in structured_lines:
        width = 78 if is_heading else 90
        segments = textwrap.wrap(text, width=width, break_long_words=True, break_on_hyphens=True) or [text]
        for seg in segments:
            wrapped_rows.append((seg, is_heading))
        if is_heading:
            wrapped_rows.append(("", False))

    lines_per_page = 46
    page_chunks = [
        wrapped_rows[i : i + lines_per_page] for i in range(0, max(1, len(wrapped_rows)), lines_per_page)
    ]

    pages_ops: list[bytes] = []
    for chunk in page_chunks:
        ops: list[bytes] = [b"BT", b"50 748 Td", b"14 TL"]
        for idx, (row_txt, is_heading) in enumerate(chunk):
            font_cmd = b"/F2 11 Tf" if is_heading else b"/F1 10 Tf"
            escaped = _pdf_escape_winansi(row_txt)
            line_op = font_cmd + (b" (" if idx == 0 else b" T* (") + escaped + b") Tj"
            ops.append(line_op)
        ops.append(b"ET")
        pages_ops.append(b"\n".join(ops))

    return _assemble_multipage_pdf(pages_ops)


def _try_libreoffice_docx_to_pdf(docx_bytes: bytes) -> bytes | None:
    """Use headless LibreOffice when present on host/container; return None if the converter is unavailable."""
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        return None
    try:
        with tempfile.TemporaryDirectory(prefix="applyflow_pdf_") as tmpdir:
            in_path = Path(tmpdir) / "source.docx"
            out_path = Path(tmpdir) / "source.pdf"
            in_path.write_bytes(docx_bytes)
            proc = subprocess.run(
                [soffice, f"-env:UserInstallation={(Path(tmpdir) / 'profile').as_uri()}", "--headless", "--norestore", "--convert-to", "pdf", "--outdir", tmpdir, str(in_path)],
                capture_output=True,
                timeout=30,
                check=False,
            )
            if proc.returncode == 0 and out_path.exists():
                pdf_bytes = out_path.read_bytes()
                if pdf_bytes.startswith(b"%PDF"):
                    return pdf_bytes
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("LibreOffice failed to convert this DOCX. Retry or upload a PDF.") from exc
    raise ValueError("LibreOffice did not produce a PDF. Retry or upload a PDF.")


def verify_pdf_bytes_readable(pdf_bytes: bytes, *, min_chars: int = 10) -> int:
    """Validate that PDF bytes parse cleanly with pypdf and yield extractable pages/text."""
    if not pdf_bytes or not pdf_bytes.lstrip().startswith(b"%PDF"):
        raise ValueError("Generated output is not a valid %PDF document")
    reader = PdfReader(io.BytesIO(pdf_bytes))
    page_count = len(reader.pages)
    if page_count > get_settings().cv_max_pages:
        raise ValueError("Converted PDF exceeds the page limit")
    if page_count < 1:
        raise ValueError("Generated PDF contains zero pages")
    extracted = "\n".join((p.extract_text() or "") for p in reader.pages[: min(page_count, 5)])
    if len(extracted.strip()) < min_chars:
        raise ValueError("Generated PDF yielded insufficient readable text upon verification")
    return page_count


def convert_docx_bytes_to_verified_pdf(docx_bytes: bytes, *, prefer_libreoffice: bool = True) -> tuple[bytes, int]:
    """
    Convert a DOCX file into readable, verified PDF bytes without altering the original DOCX bytes.
    Uses LibreOffice headless when installed and valid, with deterministic pure-Python PDF fallback.
    """
    from app.services.cv_parser import _check_docx_decompression_bounds

    _check_docx_decompression_bounds(docx_bytes, get_settings().cv_max_decompressed_bytes)
    if prefer_libreoffice:
        lo_bytes = _try_libreoffice_docx_to_pdf(docx_bytes)
        if lo_bytes is not None:
            return lo_bytes, verify_pdf_bytes_readable(lo_bytes)

    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as archive:
        if any(name.startswith(("word/media/", "word/header", "word/footer", "word/embeddings/"))
               for name in archive.namelist()):
            raise ValueError("DOCX contains embedded content that needs LibreOffice. Upload a PDF or enable conversion.")
    structured = _extract_structured_lines_from_docx(docx_bytes)
    if not structured:
        raise ValueError("DOCX contains no extractable text paragraphs or table rows to render as PDF")

    # The plain-text renderer cannot preserve glyphs outside its font encoding.
    for line, _ in structured:
        try:
            line.translate(_PUNCT_MAP).encode("latin-1")
        except UnicodeEncodeError as exc:
            raise ValueError("DOCX needs a Unicode-capable converter. Upload a PDF or enable LibreOffice.") from exc
    pdf_bytes = _render_lines_to_pdf_bytes(structured)
    pages = verify_pdf_bytes_readable(pdf_bytes)
    return pdf_bytes, pages


def derived_pdf_filename(original_filename: str) -> str:
    stem = Path(original_filename or "cv").stem
    return f"{stem}.pdf"


def prepare_new_cv_pdf(cv_version, raw: bytes) -> bytes | None:
    """Prepare attachment metadata and derivative bytes before publishing the version."""
    try:
        if cv_version.parse_status != "parsed":
            raise ValueError(f"CV version parse status is '{cv_version.parse_status}'")
        if cv_version.mime_type == PDF_MIME:
            pages = verify_pdf_bytes_readable(raw, min_chars=0)
            key, digest, derivative = cv_version.storage_key, cv_version.sha256, None
        elif cv_version.mime_type == DOCX_MIME:
            derivative, pages = convert_docx_bytes_to_verified_pdf(raw)
            digest = compute_sha256(derivative)
            key = f"{cv_version.storage_key.rsplit('/', 1)[0]}/{digest}.pdf"
        else:
            raise ValueError("Unsupported attachment type")
        cv_version.derived_pdf_storage_key = key
        cv_version.derived_pdf_sha256 = digest
        cv_version.pdf_page_count = pages
        cv_version.pdf_conversion_status = "ready"
        cv_version.pdf_conversion_error = ""
        return derivative
    except Exception as exc:
        cv_version.pdf_conversion_status = "failed"
        cv_version.pdf_conversion_error = str(exc)[:500]
        return None


def store_prepared_derivative(cv_version, derivative: bytes | None, storage: StorageAdapter) -> None:
    if derivative is None:
        return
    try:
        storage.put_bytes(cv_version.derived_pdf_storage_key, derivative, content_type=PDF_MIME)
    except Exception as exc:
        cv_version.derived_pdf_storage_key = ""
        cv_version.derived_pdf_sha256 = ""
        cv_version.pdf_page_count = 0
        cv_version.pdf_conversion_status = "failed"
        cv_version.pdf_conversion_error = str(exc)[:500]


def ensure_cv_version_pdf_ready(
    cv_version,
    *,
    storage: StorageAdapter | None = None,
    source_bytes: bytes | None = None,
) -> tuple[bool, str]:
    """
    Populate or verify derived PDF fields on a CvVersion without modifying original DOCX bytes.
    Returns (is_ready, error_or_status_message).
    """
    adapter = storage or get_storage_adapter()
    if cv_version.parse_status != "parsed":
        cv_version.pdf_conversion_status = "failed"
        cv_version.pdf_conversion_error = f"CV version parse status is '{cv_version.parse_status}'"
        return False, cv_version.pdf_conversion_error

    try:
        raw = source_bytes if source_bytes is not None else adapter.get_bytes(cv_version.storage_key)
        if compute_sha256(raw) != cv_version.sha256:
            raise ValueError("Original CV checksum mismatch")

        if cv_version.mime_type == DOCX_MIME and (cv_version.derived_pdf_sha256 or cv_version.derived_pdf_storage_key):
            existing = adapter.get_bytes(cv_version.derived_pdf_storage_key)
            if compute_sha256(existing) != cv_version.derived_pdf_sha256:
                raise ValueError("Frozen PDF checksum mismatch")
            verify_pdf_bytes_readable(existing, min_chars=0)
            cv_version.pdf_conversion_status = "ready"
            cv_version.pdf_conversion_error = ""
            return True, "Frozen PDF checksum verified"

        derivative = prepare_new_cv_pdf(cv_version, raw)
        store_prepared_derivative(cv_version, derivative, adapter)
        if cv_version.pdf_conversion_status != "ready":
            return False, cv_version.pdf_conversion_error
        return True, "PDF attachment verified"
    except Exception as exc:
        cv_version.pdf_conversion_status = "failed"
        cv_version.pdf_conversion_error = str(exc)[:500]
        return False, cv_version.pdf_conversion_error
