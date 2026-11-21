"""Secure Gemini ZIP bundle ingestion, verification, deduplication, and revalidation service.

Treats uploaded ZIP archives strictly as untrusted data:
- Never executes or imports Python/script files bundled in the archive (e.g. send_fresh_batch.py).
- Enforces archive size, member count, uncompressed size, compression ratio, and path traversal limits.
- Validates PDF attachments by magic bytes, pypdf readability, and exact SHA-256 checksums.
- Links byte-identical existing CVs or stores immutable private CvVersions without mutating existing active CVs.
- Independently verifies source URLs, recipient MX records, mandatory eligibility, and Gmail Sent deduplication.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

import httpx
from pypdf import PdfReader
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import normalize_email_dedupe_key, normalize_email_display, sanitize_header_value
from app.db.base import new_uuid, utcnow
from app.db.models import (
    Application,
    AuditEvent,
    Batch,
    CandidateProfile,
    CvDocument,
    CvExtractedFact,
    CvVersion,
    DiscoverySourceSighting,
    JobMatch,
    JobPosting,
    MailboxConnection,
    OutboxEntry,
    SearchPreference,
    User,
)
from app.services.approval import application_draft_hash, record_application_transition, sync_application_hash
from app.services.cv_parser import parse_cv_bytes
from app.services.dedupe import find_existing_recipients, release_reserved_recipients_if_pre_handoff_failed
from app.services.discovery.career_page import (
    EMAIL_RE,
    PORTAL_DOMAINS,
    classify_publisher_and_platform,
    fetch_and_parse_career_page,
)
from app.services.discovery.source_directory import is_non_vacancy_seo_or_index_url
from app.services.eligibility import (
    assess_posting_mandatory_eligibility,
    extract_mandatory_eligibility_requirements,
)
from app.services.geo import parse_job_location, resolve_user_search_locations
from app.services.mailbox.gmail import get_mailbox_dedup_coverage, sync_gmail_sent_history
from app.services.matching import evaluate_job_against_cv
from app.services.pre_handoff import (
    PENDING,
    cancel_initial_for_locked_draft,
    initial_handoff_started,
    lock_mutable_draft,
)
from app.services.storage import StorageAdapter, compute_sha256, get_storage_adapter
from app.services.verification import (
    FREE_MAIL_DOMAINS,
    NOREPLY_LOCAL_PARTS,
    PLACEHOLDER_EMAILS,
    check_domain_mail_routing,
    is_demo_email_or_domain,
)

MAX_ARCHIVE_BYTES = 25 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 100
MAX_UNCOMPRESSED_TOTAL_BYTES = 60 * 1024 * 1024
MAX_SINGLE_PDF_BYTES = 15 * 1024 * 1024
MAX_COMPRESSION_RATIO = 100.0
MAX_MANIFEST_RECORDS = 100

BLOCKED_EXECUTABLE_EXTS = {
    ".py",
    ".pyw",
    ".pyc",
    ".exe",
    ".dll",
    ".bat",
    ".cmd",
    ".ps1",
    ".sh",
    ".bash",
    ".js",
    ".mjs",
    ".vbs",
    ".scr",
    ".msi",
    ".com",
    ".jar",
    ".php",
    ".rb",
    ".pl",
}

GENERIC_NON_RECRUITING_INBOXES = {
    "info",
    "contact",
    "hello",
    "support",
    "enquiries",
    "enquiry",
    "general",
    "office",
    "sales",
    "marketing",
    "admin",
}

THIRD_PARTY_JOB_BOARD_HOSTS = (
    "uaehrs.com",
    "gocareer.club",
    "gccrecruitments.com",
    "freejobsindubai.com",
    "dubaivacancy.ae",
    "uaehelper.com",
    "realjobsindubai.com",
    "expatriates.com",
    "jobsora.com",
    "jooble.org",
    "indeed.com",
    "bayt.com",
    "gulftalent.com",
    "naukrigulf.com",
    "dubizzle.com",
    "jpgulf.com",
    "avarajobs.com",
)


class BundleImportError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass
class ParsedManifestRecord:
    index: int
    company: str
    role: str
    location: str
    to: str
    cc: list[str]
    subject: str
    source_url: str
    attachments: list[str]
    body: str
    expected_sha256: dict[str, str] = field(default_factory=dict)


@dataclass
class ExtractedArchiveBundle:
    archive_sha256: str
    manifest_name: str
    schema_version: str
    records: list[ParsedManifestRecord]
    pdf_files: dict[str, bytes]  # basename -> raw validated PDF bytes
    pdf_sha256: dict[str, str]  # basename -> sha256
    ignored_files: list[dict[str, str]]


def _validate_archive_entry_path(raw_name: str) -> str:
    """Reject path traversal, absolute paths, drive letters, and null bytes."""
    if not raw_name or "\x00" in raw_name:
        raise BundleImportError("UNSAFE_ARCHIVE_PATH", "Archive contains empty or null-byte entry path")
    normalized = raw_name.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise BundleImportError("UNSAFE_ARCHIVE_PATH", f"Archive contains absolute path entry: {raw_name}")
    parts = PurePosixPath(normalized).parts
    if any(part in ("..", "") for part in parts):
        raise BundleImportError("UNSAFE_ARCHIVE_PATH", f"Archive contains path traversal entry: {raw_name}")
    return normalized


def _validate_pdf_bytes(filename: str, data: bytes) -> int:
    """Validate PDF magic bytes, size limit, and pypdf structural integrity. Returns page count."""
    if len(data) == 0:
        raise BundleImportError("INVALID_PDF_ATTACHMENT", f"Attachment '{filename}' is empty")
    if len(data) > MAX_SINGLE_PDF_BYTES:
        raise BundleImportError(
            "ATTACHMENT_TOO_LARGE",
            f"Attachment '{filename}' exceeds {MAX_SINGLE_PDF_BYTES // (1024 * 1024)} MB limit",
        )
    if not data.startswith(b"%PDF-"):
        raise BundleImportError(
            "INVALID_PDF_ATTACHMENT",
            f"Attachment '{filename}' is not a valid PDF document (missing %PDF- signature)",
        )
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = len(reader.pages)
    except Exception as exc:
        raise BundleImportError(
            "INVALID_PDF_ATTACHMENT",
            f"Attachment '{filename}' failed PDF structural validation: {exc.__class__.__name__}",
        ) from exc
    if pages < 1:
        raise BundleImportError("INVALID_PDF_ATTACHMENT", f"Attachment '{filename}' contains zero pages")
    return pages


def _extract_manifest_payload(raw_json: Any) -> tuple[str, list[dict[str, Any]]]:
    """Support Gemini top-level array manifest and future versioned wrapper dicts."""
    if isinstance(raw_json, list):
        if not raw_json:
            raise BundleImportError("EMPTY_MANIFEST", "Manifest JSON contains zero application records")
        if not all(isinstance(item, dict) for item in raw_json):
            raise BundleImportError("MALFORMED_MANIFEST", "Every manifest entry must be a JSON object")
        return "gemini_v1_array", raw_json
    if isinstance(raw_json, dict):
        items = raw_json.get("applications")
        if items is None:
            items = raw_json.get("items")
        if not isinstance(items, list) or not items:
            raise BundleImportError(
                "MALFORMED_MANIFEST",
                "Versioned manifest must include a non-empty 'applications' or 'items' array",
            )
        if not all(isinstance(item, dict) for item in items):
            raise BundleImportError("MALFORMED_MANIFEST", "Every manifest entry must be a JSON object")
        ver = str(raw_json.get("schema_version") or "versioned_v1")[:32]
        return ver, items
    raise BundleImportError("MALFORMED_MANIFEST", "Manifest JSON must be an array or versioned object")


def parse_untrusted_gemini_zip(zip_bytes: bytes) -> ExtractedArchiveBundle:
    """Safely inspect, validate, and parse an uploaded Gemini batch ZIP archive in memory."""
    if not zip_bytes:
        raise BundleImportError("EMPTY_UPLOAD", "Uploaded ZIP file is empty")
    if len(zip_bytes) > MAX_ARCHIVE_BYTES:
        raise BundleImportError(
            "ARCHIVE_TOO_LARGE",
            f"Uploaded ZIP exceeds {MAX_ARCHIVE_BYTES // (1024 * 1024)} MB limit",
        )
    archive_sha256 = compute_sha256(zip_bytes)

    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes), mode="r")
    except zipfile.BadZipFile as exc:
        raise BundleImportError("INVALID_ZIP", "Uploaded file is not a valid ZIP archive") from exc

    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_ARCHIVE_ENTRIES:
            raise BundleImportError(
                "ARCHIVE_TOO_MANY_FILES",
                f"Archive contains too many entries ({len(infos)} > {MAX_ARCHIVE_ENTRIES})",
            )

        total_uncompressed = 0
        json_candidates: dict[str, bytes] = {}
        pdf_by_name: dict[str, bytes] = {}
        pdf_by_lower: dict[str, str] = {}
        all_non_dir_basenames: set[str] = set()
        ignored_files: list[dict[str, str]] = []

        for info in infos:
            norm_path = _validate_archive_entry_path(info.filename)
            if info.is_dir():
                continue
            # Reject symlinks inside ZIP (Unix external_attr mode 0o120000)
            if ((info.external_attr >> 16) & 0o170000) == 0o120000:
                raise BundleImportError("UNSAFE_ARCHIVE_PATH", f"Archive contains symbolic link: {norm_path}")

            total_uncompressed += max(0, info.file_size)
            if total_uncompressed > MAX_UNCOMPRESSED_TOTAL_BYTES:
                raise BundleImportError("ZIP_BOMB_DETECTED", "Archive uncompressed size exceeds safety limit")
            if info.compress_size > 0 and (info.file_size / info.compress_size) > MAX_COMPRESSION_RATIO:
                raise BundleImportError("ZIP_BOMB_DETECTED", f"Suspicious compression ratio on '{norm_path}'")

            basename = PurePosixPath(norm_path).name
            if not basename or basename.startswith("._") or norm_path.startswith("__MACOSX/"):
                continue
            ext = Path(basename).suffix.lower()
            all_non_dir_basenames.add(basename)

            if ext in BLOCKED_EXECUTABLE_EXTS:
                ignored_files.append(
                    {
                        "filename": basename,
                        "reason": "Executable/script file ignored and never executed or imported",
                    }
                )
                continue

            if ext == ".json":
                with zf.open(info) as fp:
                    raw_data = fp.read(5 * 1024 * 1024 + 1)
                if len(raw_data) > 5 * 1024 * 1024:
                    raise BundleImportError("MANIFEST_TOO_LARGE", f"JSON manifest '{basename}' exceeds 5 MB limit")
                json_candidates[basename] = raw_data
            elif ext == ".pdf":
                with zf.open(info) as fp:
                    pdf_data = fp.read(MAX_SINGLE_PDF_BYTES + 1)
                _validate_pdf_bytes(basename, pdf_data)
                if basename in pdf_by_name and compute_sha256(pdf_by_name[basename]) != compute_sha256(pdf_data):
                    raise BundleImportError("DUPLICATE_PDF_NAME", f"Conflicting PDF entries named '{basename}'")
                pdf_by_name[basename] = pdf_data
                pdf_by_lower[basename.lower()] = basename
            else:
                ignored_files.append(
                    {
                        "filename": basename,
                        "reason": f"Companion non-PDF file ({ext or 'unknown'}) ignored during import",
                    }
                )

    if not json_candidates:
        raise BundleImportError(
            "MISSING_MANIFEST",
            "Archive does not contain a JSON queue manifest (expected email_queue_fresh_10.json)",
        )

    manifest_name = next(
        (name for name in json_candidates if name.lower() == "email_queue_fresh_10.json"),
        sorted(json_candidates.keys())[0],
    )
    try:
        parsed_json = json.loads(json_candidates[manifest_name].decode("utf-8-sig"))
    except Exception as exc:
        raise BundleImportError("MALFORMED_MANIFEST", f"Manifest '{manifest_name}' is not valid UTF-8 JSON: {exc}") from exc

    schema_version, raw_items = _extract_manifest_payload(parsed_json)
    if len(raw_items) > MAX_MANIFEST_RECORDS:
        raise BundleImportError(
            "MANIFEST_TOO_LARGE",
            f"Manifest contains {len(raw_items)} records (maximum allowed is {MAX_MANIFEST_RECORDS})",
        )

    pdf_sha256 = {name: compute_sha256(data) for name, data in pdf_by_name.items()}
    parsed_records: list[ParsedManifestRecord] = []

    for idx, item in enumerate(raw_items, start=1):
        company = sanitize_header_value(str(item.get("company") or ""), 200)
        role = sanitize_header_value(str(item.get("role") or ""), 240)
        location = sanitize_header_value(str(item.get("location") or ""), 240)
        to_raw = str(item.get("to") or "").strip()
        subject = sanitize_header_value(str(item.get("subject") or ""), 300)
        source_url = str(item.get("source_url") or "").strip()
        body = str(item.get("body") or "").strip()
        raw_atts = item.get("attachments")
        raw_ccs = item.get("cc") or item.get("cc_emails") or []
        raw_hashes = item.get("attachment_sha256") or {}

        if not company or not role:
            raise BundleImportError("INVALID_RECORD", f"Record #{idx} is missing required 'company' or 'role'")
        if not to_raw or not EMAIL_RE.fullmatch(to_raw):
            raise BundleImportError("INVALID_RECIPIENT", f"Record #{idx} ({company}) has invalid recipient email: {to_raw!r}")
        if len(subject) < 2:
            raise BundleImportError("INVALID_RECORD", f"Record #{idx} ({company}) has empty or invalid subject")
        if len(body) < 10:
            raise BundleImportError("INVALID_RECORD", f"Record #{idx} ({company}) has empty or too-short body")
        if source_url:
            parsed_u = urlparse(source_url)
            if parsed_u.scheme not in ("http", "https") or not parsed_u.netloc:
                raise BundleImportError("INVALID_SOURCE_URL", f"Record #{idx} ({company}) has invalid source_url")

        if isinstance(raw_atts, str):
            raw_atts = [raw_atts]
        if not isinstance(raw_atts, list) or not raw_atts:
            raise BundleImportError("MISSING_ATTACHMENT", f"Record #{idx} ({company}) must list at least one PDF attachment")

        resolved_atts: list[str] = []
        expected_hashes: dict[str, str] = {}
        for att_entry in raw_atts:
            att_str = str(att_entry or "").strip()
            norm_att = _validate_archive_entry_path(att_str)
            att_base = PurePosixPath(norm_att).name
            att_ext = Path(att_base).suffix.lower()
            if att_ext != ".pdf":
                raise BundleImportError(
                    "UNSUPPORTED_ATTACHMENT",
                    f"Record #{idx} ({company}) references unsupported non-PDF attachment '{att_base}'",
                )
            matched_name = pdf_by_name.get(att_base) and att_base or pdf_by_lower.get(att_base.lower())
            if not matched_name:
                raise BundleImportError(
                    "MISSING_ATTACHMENT",
                    f"Record #{idx} ({company}) references PDF '{att_base}' which is missing from the ZIP archive",
                )
            if isinstance(raw_hashes, dict):
                exp_h = str(raw_hashes.get(att_base) or raw_hashes.get(matched_name) or "").strip().lower()
                if exp_h:
                    if exp_h != pdf_sha256[matched_name].lower():
                        raise BundleImportError(
                            "ATTACHMENT_HASH_MISMATCH",
                            f"Record #{idx} ({company}) attachment '{matched_name}' SHA-256 mismatch",
                        )
                    expected_hashes[matched_name] = exp_h
            if matched_name not in resolved_atts:
                resolved_atts.append(matched_name)

        ccs: list[str] = []
        if isinstance(raw_ccs, str) and raw_ccs.strip():
            raw_ccs = [raw_ccs]
        if isinstance(raw_ccs, list):
            for cc_val in raw_ccs:
                cc_s = str(cc_val or "").strip()
                if cc_s:
                    if not EMAIL_RE.fullmatch(cc_s):
                        raise BundleImportError("INVALID_RECIPIENT", f"Record #{idx} ({company}) has invalid CC email: {cc_s!r}")
                    ccs.append(normalize_email_display(cc_s))

        parsed_records.append(
            ParsedManifestRecord(
                index=idx,
                company=company,
                role=role,
                location=location,
                to=normalize_email_display(to_raw),
                cc=ccs,
                subject=subject,
                source_url=source_url,
                attachments=resolved_atts,
                body=body,
                expected_sha256=expected_hashes,
            )
        )

    return ExtractedArchiveBundle(
        archive_sha256=archive_sha256,
        manifest_name=manifest_name,
        schema_version=schema_version,
        records=parsed_records,
        pdf_files=pdf_by_name,
        pdf_sha256=pdf_sha256,
        ignored_files=ignored_files,
    )


def _resolve_or_import_pdf_cv(
    db: Session,
    *,
    user: User,
    filename: str,
    pdf_bytes: bytes,
    pdf_sha256: str,
    storage: StorageAdapter,
) -> tuple[CvVersion, str]:
    """
    Link to a byte-identical existing CvVersion for this user, or ingest as a new private immutable CvVersion.
    Never silently replaces a different-hash CV that happens to share the same filename.
    """
    existing_ver = db.scalar(
        select(CvVersion)
        .join(CvDocument, CvVersion.cv_document_id == CvDocument.id)
        .where(
            CvVersion.user_id == user.id,
            CvVersion.sha256 == pdf_sha256,
            CvDocument.is_archived.is_(False),
        )
        .order_by(CvVersion.created_at.asc())
    )
    if existing_ver:
        if existing_ver.mime_type == "application/pdf":
            if not existing_ver.derived_pdf_storage_key:
                existing_ver.derived_pdf_storage_key = existing_ver.storage_key
            if not existing_ver.derived_pdf_sha256:
                existing_ver.derived_pdf_sha256 = existing_ver.sha256
            if existing_ver.pdf_conversion_status != "ready":
                existing_ver.pdf_conversion_status = "ready"
        return existing_ver, "linked_existing_exact_sha256"

    safe_name = re.sub(r"[^A-Za-z0-9._\-\s]", "_", Path(filename).name).strip() or "imported_cv.pdf"
    stem = Path(safe_name).stem[:90]
    doc = CvDocument(
        user_id=user.id,
        variant_label=f"Imported: {stem}",
        is_archived=False,
    )
    db.add(doc)
    db.flush()

    storage_key = f"users/{user.id}/cvs/{doc.id}/v1/{safe_name}"
    storage.put_bytes(storage_key, pdf_bytes)
    parsed = parse_cv_bytes(pdf_bytes, "application/pdf")

    ver = CvVersion(
        cv_document_id=doc.id,
        user_id=user.id,
        version_number=1,
        original_filename=safe_name,
        mime_type="application/pdf",
        size_bytes=len(pdf_bytes),
        page_count=parsed.page_count,
        sha256=pdf_sha256,
        storage_key=storage_key,
        parse_status=parsed.parse_status,
        ocr_status=parsed.ocr_status,
        parse_error=parsed.parse_error,
        detected_language=parsed.detected_language,
        extracted_text=parsed.extracted_text,
        total_experience_months=parsed.total_experience_months,
        raw_summary_json={
            **(parsed.summary or {}),
            "import_origin": "gemini_zip",
        },
        derived_pdf_storage_key=storage_key,
        derived_pdf_sha256=pdf_sha256,
        pdf_conversion_status="ready",
        pdf_page_count=parsed.page_count,
    )
    db.add(ver)
    db.flush()
    doc.active_version_id = ver.id

    for fact in parsed.facts:
        db.add(
            CvExtractedFact(
                cv_version_id=ver.id,
                user_id=user.id,
                category=fact.category,
                title=fact.title,
                organization=fact.organization,
                value=fact.value,
                evidence_quote=fact.evidence_quote,
                start_date=fact.start_date,
                end_date=fact.end_date,
                is_current=fact.is_current,
                proficiency_level=fact.proficiency_level,
                confidence=fact.confidence,
                source_page=fact.source_page,
            )
        )
    db.flush()
    return ver, "imported_new_immutable_version"


def _root_domain(host: str) -> str:
    h = (host or "").strip().lower().removeprefix("www.")
    parts = [p for p in h.split(".") if p]
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net", "gov", "ac") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return h


def _domains_align(source_host: str, recipient_domain: str, company_name: str) -> bool:
    s_root = _root_domain(source_host)
    r_root = _root_domain(recipient_domain)
    if not r_root:
        return False
    if s_root and s_root == r_root:
        return True
    r_stem = r_root.split(".", 1)[0]
    s_stem = s_root.split(".", 1)[0] if s_root else ""
    if s_stem and len(s_stem) >= 4 and (s_stem in r_stem or r_stem in s_stem):
        return True
    comp_tokens = [
        tok for tok in re.findall(r"[a-z0-9]+", (company_name or "").lower())
        if len(tok) >= 4 and tok not in ("group", "dubai", "global", "facilities", "management", "services", "express", "worldwide", "holding", "company", "real", "estate")
    ]
    return any(tok in r_stem for tok in comp_tokens)


def _compute_import_item_hash(*, to_email: str, source_url: str, company: str, role: str, pdf_sha256: str) -> str:
    raw = "|".join(
        [
            normalize_email_dedupe_key(to_email),
            (source_url or "").strip().lower(),
            company.strip().lower(),
            role.strip().lower(),
            pdf_sha256.strip().lower(),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def compute_batch_workflow_counts(db: Session, *, batch_id: str, user_id: str) -> dict[str, int]:
    """Compute dashboard summary buckets separating import count from review, approval, and send outcomes."""
    from app.services.outbox import get_latest_outbox_map, reconcile_application_outbox_states

    reconcile_application_outbox_states(db, user_id=user_id)
    apps = list(
        db.scalars(
            select(Application).where(Application.batch_id == batch_id, Application.user_id == user_id)
        ).all()
    )
    outbox_by_app = get_latest_outbox_map(db, [a.id for a in apps])
    counts = {
        "imported": sum(bool(app.import_origin) for app in apps),
        "total": len(apps),
        "verified": 0,
        "needs_review": 0,
        "ready": 0,
        "duplicate": 0,
        "blocked": 0,
        "approved": 0,
        "queued": 0,
        "provider_accepted": 0,
        "failed": 0,
        "uncertain": 0,
    }
    for app_rec in apps:
        ob = outbox_by_app.get(app_rec.id)
        ob_status = (ob.status or "") if ob else ""
        ts = app_rec.transport_status or "draft"
        ap = app_rec.approval_state or "draft"
        tier = app_rec.source_verification_tier or ""
        if tier in ("tier_1_official", "tier_2_authorized"):
            counts["verified"] += 1

        if ts == "sent" or ob_status == "sent":
            counts["provider_accepted"] += 1
        elif ts == "send_uncertain" or ob_status == "send_uncertain":
            counts["uncertain"] += 1
        elif ts in ("queued", "sending") or ob_status in ("queued", "leased", "sending"):
            counts["queued"] += 1
        elif ts == "failed" or ob_status == "failed":
            counts["failed"] += 1
        elif ts == "skipped" or ap == "rejected":
            if app_rec.primary_skip_code in ("PREVIOUSLY_CONTACTED", "DUPLICATE_IMPORT"):
                counts["duplicate"] += 1
            else:
                counts["blocked"] += 1
        elif ap == "approved":
            counts["approved"] += 1
        elif ap == "needs_human_review":
            counts["needs_review"] += 1
        else:
            counts["ready"] += 1
    return counts


async def _fetch_sources_concurrently(
    records: list[tuple[int, str, str]],
    *,
    skip_network: bool,
    transport_override: httpx.AsyncBaseTransport | None = None,
) -> dict[int, tuple[Any, str]]:
    if skip_network and transport_override is None:
        return {}
    sem = asyncio.Semaphore(5)

    async def _one(idx: int, url: str, role: str) -> tuple[int, tuple[Any, str]]:
        if not url:
            return idx, (None, "no_source_url")
        async with sem:
            try:
                cand, status = await fetch_and_parse_career_page(
                    url,
                    seed_title=role,
                    target_role_terms=[role],
                    transport=transport_override,
                )
                return idx, (cand, status)
            except Exception as exc:
                return idx, (None, f"fetch_error:{exc.__class__.__name__}")

    results = await asyncio.gather(*[_one(i, u, r) for i, u, r in records])
    return dict(results)


def evaluate_imported_record(
    db: Session,
    *,
    user: User,
    profile: CandidateProfile,
    pref: SearchPreference,
    company: str,
    role: str,
    location: str,
    to_email: str,
    cc_emails: list[str],
    source_url: str,
    fetched_cand: Any | None,
    fetch_status: str,
    cv_version: CvVersion,
    cv_facts: list[CvExtractedFact],
    dedup_coverage: dict[str, Any],
    skip_network_dns: bool = False,
    exclude_application_id: str | None = None,
    duplicate_item_hash_hit: bool = False,
) -> dict[str, Any]:
    """
    Independently evaluate an imported application record without blindly trusting the manifest.
    Returns structured verification tier, approval state, skip codes, review reasons, eligibility, and evidence.
    """
    norm_to = normalize_email_display(to_email)
    norm_to_key = normalize_email_dedupe_key(norm_to)
    local_part, _, recip_domain = norm_to_key.partition("@")
    source_host = (urlparse(source_url).hostname or "").lower().removeprefix("www.") if source_url else ""
    publisher_name, _ = classify_publisher_and_platform(source_url) if source_url else ("Imported Manifest", "manual")

    review_reasons: list[str] = []
    block_codes: list[str] = []
    block_reasons: list[str] = []

    # 1. Demo / placeholder / noreply checks
    all_emails = [norm_to, *cc_emails]
    if any(is_demo_email_or_domain(em) for em in all_emails if em) or norm_to_key in PLACEHOLDER_EMAILS:
        block_codes.append("DEMO_FIXTURE")
        block_reasons.append("Demo or placeholder recipient address cannot be contacted")
    if local_part in NOREPLY_LOCAL_PARTS:
        block_codes.append("NO_REPLY_EMAIL")
        block_reasons.append(f"Recipient '{norm_to}' is a no-reply/automated mailbox")

    # 2. DNS mail-routing validation (never sufficient by itself to verify a source)
    dns_status, dns_detail = check_domain_mail_routing(recip_domain, skip_network_dns=skip_network_dns)
    if dns_status in ("no_mx", "invalid_domain"):
        block_codes.append("DOMAIN_INVALID")
        block_reasons.append(f"Recipient domain '{recip_domain}' has invalid mail routing ({dns_detail})")
    elif dns_status == "dns_error":
        review_reasons.append(f"Temporary DNS verification uncertainty for '{recip_domain}' ({dns_detail}); verify before sending")

    # 3. Duplicate check (ApplyFlow ledger + synced Gmail Sent + prior import item hash)
    existing_hits = find_existing_recipients(
        db,
        user_id=user.id,
        emails=all_emails,
        exclude_application_id=exclude_application_id,
    )
    is_duplicate = False
    if existing_hits:
        is_duplicate = True
        hit_str = ", ".join(sorted(existing_hits.keys()))
        block_codes.insert(0, "PREVIOUSLY_CONTACTED")
        block_reasons.insert(0, f"Recipient already contacted in ledger or Gmail Sent history ({hit_str})")
    elif duplicate_item_hash_hit:
        is_duplicate = True
        block_codes.insert(0, "DUPLICATE_IMPORT")
        block_reasons.insert(0, "Identical application record was already imported previously")

    if dedup_coverage.get("coverage_mode") != "applyflow_ledger_plus_gmail_sent":
        review_reasons.append(
            "Gmail Sent history sync is unavailable; duplicate certainty is limited to ApplyFlow's local ledger"
        )

    # 4. Source URL & recipient corroboration analysis
    fetched_jd = (getattr(fetched_cand, "jd_text", "") or "").strip() if fetched_cand else ""
    fetched_email = (getattr(fetched_cand, "application_email", "") or "").strip().lower() if fetched_cand else ""
    fetched_quote = (getattr(fetched_cand, "evidence_quote", "") or "").strip() if fetched_cand else ""
    fetched_standing = bool(getattr(fetched_cand, "is_standing_invitation", False)) if fetched_cand else False
    fetched_inaccessible = (getattr(fetched_cand, "page_inaccessible_reason", "") or "").strip() if fetched_cand else ""

    source_fetched_ok = bool(fetched_cand is not None and fetch_status == "fetched" and not fetched_inaccessible)
    recipient_on_page = bool(source_fetched_ok and fetched_email == norm_to_key)
    is_third_party_board = any(bd in source_host for bd in THIRD_PARTY_JOB_BOARD_HOSTS) if source_host else False
    is_portal_or_index = bool(
        source_url
        and (
            any(pd in source_url.lower() for pd in PORTAL_DOMAINS)
            or is_non_vacancy_seo_or_index_url(source_url, role)
        )
    )
    is_free_mail = recip_domain in FREE_MAIL_DOMAINS
    is_generic_inbox = local_part in GENERIC_NON_RECRUITING_INBOXES
    domain_aligned = _domains_align(source_host, recip_domain, company)

    if not source_url:
        review_reasons.append("No source URL provided in imported record; employer source could not be corroborated")
    elif is_portal_or_index:
        review_reasons.append(f"Source URL ({source_host}) appears to be a job-board search index or ATS listing rather than a direct email vacancy")
    elif is_third_party_board:
        review_reasons.append(f"Source ({source_host}) is a third-party job board; recipient claim is not directly hosted on the employer's website")

    if source_url and not source_fetched_ok:
        detail = fetched_inaccessible or fetch_status or "inaccessible"
        if detail == "skipped_network":
            review_reasons.append("Live source verification was skipped; revalidate source URL before sending")
        else:
            review_reasons.append(f"Source URL could not be fetched directly ({detail}); recipient was not corroborated on the live page")
    elif source_fetched_ok and not recipient_on_page:
        review_reasons.append(f"Fetched source page ({source_host}) did not explicitly display '{norm_to}' as an email application instruction")

    if is_free_mail:
        review_reasons.append(f"Recipient uses free webmail domain '{recip_domain}'; confirm recruiter identity before sending")
    if is_generic_inbox:
        review_reasons.append(f"Generic '{local_part}@' address is not automatically safe as a recruitment inbox; confirm it accepts CVs")
    if source_host and not is_third_party_board and not is_portal_or_index and not domain_aligned:
        review_reasons.append(
            f"Recipient domain '{recip_domain}' does not align with source domain '{source_host}' or employer '{company}'"
        )

    # 5. Location & Mandatory Eligibility Validation (NEVER overridable)
    combined_loc_text = f"{location}\n{getattr(fetched_cand, 'location_raw', '') if fetched_cand else ''}".strip()
    parsed_loc = parse_job_location(combined_loc_text or location, source_url=source_url)
    _, target_country_list = resolve_user_search_locations(
        target_locations=pref.target_locations_json or [],
        residence_country_code=profile.residence_country_code or "",
        residence_city=profile.residence_city or "",
        work_authorizations=profile.work_authorizations_json or [],
        relocation_targets=profile.relocation_targets_json or [],
        willing_to_relocate=profile.willing_to_relocate,
        allow_relocation=pref.allow_relocation,
        search_languages=pref.search_languages_json or ["en"],
    )
    allowed_countries = {c.upper() for c in target_country_list if c}
    if profile.residence_country_code:
        allowed_countries.add(profile.residence_country_code.upper())
    for wa in profile.work_authorizations_json or []:
        if wa:
            allowed_countries.add(wa.upper())
    if pref.allow_relocation and profile.willing_to_relocate:
        for rt in profile.relocation_targets_json or []:
            if rt:
                allowed_countries.add(rt.upper())

    if parsed_loc.country_code and allowed_countries and parsed_loc.country_code.upper() not in allowed_countries:
        if parsed_loc.remote_scope != "remote_worldwide":
            block_codes.append("OUTSIDE_TARGET_COUNTRY")
            block_reasons.append(
                f"Vacancy location ({parsed_loc.country_code}: {location}) is outside candidate's authorized/target countries ({', '.join(sorted(allowed_countries))})"
            )

    # Extract mandatory eligibility requirements from fetched JD + imported role/location metadata
    jd_for_elig = fetched_jd or f"Role: {role}\nCompany: {company}\nLocation: {location}"
    elig_reqs = extract_mandatory_eligibility_requirements(
        title=role,
        jd_text=jd_for_elig,
        subject_instruction="",
        evidence_quote=fetched_quote,
        location_raw=location,
        country_code=parsed_loc.country_code,
    )
    elig_reqs_dict = elig_reqs.to_dict()

    # Build a temporary unsaved JobPosting-like object for eligibility + advisory CV scoring
    temp_posting = JobPosting(
        user_id=user.id,
        batch_id="preview",
        canonical_url_hash="",
        content_hash="",
        company_name=company,
        company_domain=recip_domain,
        title=role,
        role_extracted=role,
        jd_text=jd_for_elig,
        location_raw=location,
        country_code=parsed_loc.country_code,
        city=parsed_loc.city,
        remote_scope=parsed_loc.remote_scope,
        application_email=norm_to,
        evidence_quote=fetched_quote,
        is_standing_invitation=fetched_standing or ("expression of interest" in role.lower()),
        eligibility_requirements_json=elig_reqs_dict,
    )
    _, elig_res = assess_posting_mandatory_eligibility(
        posting=temp_posting,
        profile=profile,
        cv_version=cv_version,
        facts=cv_facts,
        check_cv_requirements=True,
    )
    if not elig_res.passed:
        for fc in elig_res.failure_codes:
            if fc not in block_codes:
                block_codes.append(fc)
        for fm in elig_res.failure_messages():
            block_reasons.append(fm)

    # Advisory CV match explanation (never replaces imported CV or draft)
    cv_eval = evaluate_job_against_cv(
        posting=temp_posting,
        cv_version=cv_version,
        facts=cv_facts,
        pref=pref,
        profile=profile,
        now_utc=utcnow(),
    )

    # 6. Final Honest Classification
    if block_codes:
        verif_tier = "tier_5_unrelated_or_prohibited"
        corrob_status = "blocked"
        approval_state = "rejected"
        transport_status = "skipped"
        discovery_category = "rejected"
        verif_status = "skipped"
        outcome_bucket = "duplicate" if is_duplicate else "blocked"
    elif source_fetched_ok and recipient_on_page and domain_aligned and not is_third_party_board and not is_portal_or_index and not is_free_mail and not is_generic_inbox and not review_reasons:
        verif_tier = "tier_1_official"
        corrob_status = "corroborated_official"
        approval_state = "ready_for_approval"
        transport_status = "draft"
        discovery_category = "verified_general_cv_invitation" if temp_posting.is_standing_invitation else "verified_email_vacancy"
        verif_status = "verified"
        outcome_bucket = "ready"
    else:
        # Legitimate recruiter claim with cautions: requires explicit human review & audit notes
        verif_tier = "tier_3_third_party_claim"
        corrob_status = "corroborated_third_party" if recipient_on_page else "uncorroborated"
        approval_state = "needs_human_review"
        transport_status = "draft"
        if recipient_on_page:
            discovery_category = "verified_general_cv_invitation" if temp_posting.is_standing_invitation else "verified_email_vacancy"
            verif_status = "verified"
        else:
            discovery_category = "incomplete_evidence"
            verif_status = "unverified"
        outcome_bucket = "needs_review"

    evidence_quote = fetched_quote
    if not evidence_quote and recipient_on_page and fetched_jd:
        pos = fetched_jd.lower().find(norm_to_key)
        if pos >= 0:
            evidence_quote = " ".join(fetched_jd[max(0, pos - 120) : min(len(fetched_jd), pos + 200)].split())[:400]
    if not evidence_quote:
        evidence_quote = (
            f"Imported from Gemini manifest ({source_url or 'no source URL'}). "
            + (f"Source status: {fetch_status}." if fetch_status else "")
        ).strip()

    return {
        "normalized_to": norm_to,
        "recipient_domain": recip_domain,
        "publisher": publisher_name,
        "parsed_loc": parsed_loc,
        "dns_status": dns_status,
        "dns_detail": dns_detail,
        "source_fetched_ok": source_fetched_ok,
        "recipient_on_page": recipient_on_page,
        "fetch_status": fetch_status,
        "fetched_jd": jd_for_elig,
        "evidence_quote": evidence_quote,
        "is_standing": temp_posting.is_standing_invitation,
        "elig_reqs_dict": elig_reqs_dict,
        "elig_res": elig_res,
        "cv_eval": cv_eval,
        "source_verification_tier": verif_tier,
        "corroboration_status": corrob_status,
        "approval_state": approval_state,
        "transport_status": transport_status,
        "discovery_category": discovery_category,
        "verification_status": verif_status,
        "outcome_bucket": outcome_bucket,
        "review_reasons": review_reasons,
        "block_codes": block_codes,
        "block_reasons": block_reasons,
        "primary_skip_code": block_codes[0] if block_codes else "",
        "skip_reason": "; ".join(block_reasons),
    }


def import_gemini_bundle_zip(
    db: Session,
    *,
    user: User,
    zip_bytes: bytes,
    original_filename: str = "send_fresh_batch_bundle.zip",
    skip_network: bool = False,
    transport_override: httpx.AsyncBaseTransport | None = None,
    storage_override: StorageAdapter | None = None,
) -> dict[str, Any]:
    """
    Import a Gemini ZIP bundle transactionally into ApplyFlow's Batch + Review Queue + Tracker pipeline.
    Idempotent on identical ZIP SHA-256: re-importing the same ZIP creates zero duplicate sendable records.
    """
    bundle = parse_untrusted_gemini_zip(zip_bytes)
    storage = storage_override or get_storage_adapter()

    # Idempotent ZIP re-import check
    existing_batch = db.scalar(
        select(Batch).where(
            Batch.user_id == user.id,
            Batch.source_archive_sha256 == bundle.archive_sha256,
        )
    )
    if existing_batch:
        counts = compute_batch_workflow_counts(db, batch_id=existing_batch.id, user_id=user.id)
        return {
            "batch": existing_batch,
            "duplicate_import": True,
            "created_applications": 0,
            "counts": counts,
            "ignored_files": bundle.ignored_files,
            "message": f"Identical ZIP archive ({bundle.archive_sha256[:12]}) was already imported; returning existing batch without creating duplicates.",
        }

    profile = db.scalar(select(CandidateProfile).where(CandidateProfile.user_id == user.id))
    pref = db.scalar(select(SearchPreference).where(SearchPreference.user_id == user.id))
    if not profile:
        profile = CandidateProfile(user_id=user.id)
        db.add(profile)
        db.flush()
    if not pref:
        pref = SearchPreference(user_id=user.id)
        db.add(pref)
        db.flush()

    # Pre-sync connected Gmail Sent history for accurate duplicate detection
    settings = get_settings()
    primary_conn = db.scalar(
        select(MailboxConnection)
        .where(
            MailboxConnection.user_id == user.id,
            MailboxConnection.status == "connected",
            MailboxConnection.revoked_at.is_(None),
        )
        .order_by(MailboxConnection.is_primary.desc())
    )
    if primary_conn and not skip_network and settings.environment != "test":
        try:
            sync_gmail_sent_history(db, conn=primary_conn, max_pages=3, page_size=50)
        except Exception:
            pass

    dedup_coverage = (
        get_mailbox_dedup_coverage(db, conn=primary_conn)
        if primary_conn
        else {
            "coverage_mode": "applyflow_ledger_only",
            "sent_records_count": 0,
            "suppressed_job_recipients_count": 0,
            "warning_banner": "No connected mailbox found; deduplication checked against ApplyFlow ledger only.",
        }
    )

    # Store raw archive privately as non-executable audit evidence
    archive_key = f"users/{user.id}/imports/{bundle.archive_sha256}.zip"
    storage.put_bytes(archive_key, zip_bytes)

    now = utcnow()
    batch = Batch(
        user_id=user.id,
        idempotency_key=f"gemini_zip:{bundle.archive_sha256}",
        mode="dry_run",
        run_mode="approval_required",
        pipeline_version="vNext",
        strategy_used="gemini_zip_import",
        import_origin="gemini_zip",
        source_archive_sha256=bundle.archive_sha256,
        source_archive_storage_key=archive_key,
        target_goal_count=len(bundle.records),
        status="running",
        started_at=now,
        coverage_note=f"Imported {len(bundle.records)} prepared applications from '{Path(original_filename).name}' ({bundle.manifest_name}).",
        budget_config_json={
            "import_origin": "gemini_zip",
            "manifest_name": bundle.manifest_name,
            "schema_version": bundle.schema_version,
            "archive_sha256": bundle.archive_sha256,
            "ignored_files": bundle.ignored_files,
            "dedup_coverage_mode": dedup_coverage.get("coverage_mode", "applyflow_ledger_only"),
        },
    )
    db.add(batch)
    db.flush()

    # Ingest or link all referenced PDF attachments once per unique PDF
    cv_by_filename: dict[str, tuple[CvVersion, str, list[CvExtractedFact]]] = {}
    referenced_pdfs = sorted({att for rec in bundle.records for att in rec.attachments})
    for pdf_name in referenced_pdfs:
        ver, link_mode = _resolve_or_import_pdf_cv(
            db,
            user=user,
            filename=pdf_name,
            pdf_bytes=bundle.pdf_files[pdf_name],
            pdf_sha256=bundle.pdf_sha256[pdf_name],
            storage=storage,
        )
        facts = list(db.scalars(select(CvExtractedFact).where(CvExtractedFact.cv_version_id == ver.id)).all())
        cv_by_filename[pdf_name] = (ver, link_mode, facts)

    # Fetch source pages concurrently (bounded & SSRF-protected)
    fetch_inputs = [(rec.index, rec.source_url, rec.role) for rec in bundle.records]
    fetched_map = asyncio.run(
        _fetch_sources_concurrently(
            fetch_inputs,
            skip_network=skip_network,
            transport_override=transport_override,
        )
    )

    existing_item_hashes = set(
        db.scalars(
            select(Application.import_item_hash).where(
                Application.user_id == user.id,
                Application.import_item_hash != "",
                Application.transport_status != "skipped",
            )
        ).all()
    )

    category_counts: dict[str, int] = {}
    skip_reason_counts: dict[str, int] = {}
    created_apps = 0
    seen_item_hashes_in_bundle: set[str] = set()

    for rec in bundle.records:
        primary_pdf_name = rec.attachments[0]
        cv_ver, cv_link_mode, cv_facts = cv_by_filename[primary_pdf_name]
        pdf_sha = bundle.pdf_sha256[primary_pdf_name]
        item_hash = _compute_import_item_hash(
            to_email=rec.to,
            source_url=rec.source_url,
            company=rec.company,
            role=rec.role,
            pdf_sha256=pdf_sha,
        )
        dup_item_hit = item_hash in existing_item_hashes or item_hash in seen_item_hashes_in_bundle
        seen_item_hashes_in_bundle.add(item_hash)

        fetched_cand, f_status = fetched_map.get(rec.index, (None, "skipped_network" if skip_network else "no_source_url"))
        eval_res = evaluate_imported_record(
            db,
            user=user,
            profile=profile,
            pref=pref,
            company=rec.company,
            role=rec.role,
            location=rec.location,
            to_email=rec.to,
            cc_emails=rec.cc,
            source_url=rec.source_url,
            fetched_cand=fetched_cand,
            fetch_status=f_status,
            cv_version=cv_ver,
            cv_facts=cv_facts,
            dedup_coverage=dedup_coverage,
            skip_network_dns=skip_network,
            duplicate_item_hash_hit=dup_item_hit,
        )

        canon_url = rec.source_url.strip()
        url_hash = hashlib.sha256(canon_url.lower().encode("utf-8")).hexdigest() if canon_url else new_uuid()
        content_hash = hashlib.sha256(f"{rec.company}|{rec.role}|{rec.to}|{rec.subject}".lower().encode("utf-8")).hexdigest()

        posting = JobPosting(
            user_id=user.id,
            batch_id=batch.id,
            canonical_url_hash=url_hash,
            content_hash=content_hash,
            is_demo=False,
            canonical_url=canon_url,
            search_query="gemini_zip_import",
            source_provider="gemini_zip",
            source_url=canon_url,
            source_type="standing_invitation" if eval_res["is_standing"] else "job_post",
            publisher=eval_res["publisher"],
            company_name=rec.company,
            purported_employer=rec.company,
            validated_employer=rec.company if eval_res["source_verification_tier"] == "tier_1_official" else "",
            employer_confidence="verified" if eval_res["source_verification_tier"] == "tier_1_official" else "unverified",
            company_domain=eval_res["recipient_domain"],
            title=rec.role,
            role_extracted=rec.role,
            jd_text=eval_res["fetched_jd"],
            detected_language="en",
            location_raw=rec.location,
            country_code=eval_res["parsed_loc"].country_code,
            city=eval_res["parsed_loc"].city,
            location_confidence="explicit" if eval_res["parsed_loc"].country_code else "unknown",
            remote_scope=eval_res["parsed_loc"].remote_scope,
            retrieved_at=now,
            first_seen_at=now,
            application_email=eval_res["normalized_to"],
            cc_emails_json=rec.cc,
            evidence_quote=eval_res["evidence_quote"],
            is_standing_invitation=eval_res["is_standing"],
            discovery_category=eval_res["discovery_category"],
            source_priority_tier=1 if eval_res["source_verification_tier"] == "tier_1_official" else 3,
            access_status="fetched_200" if eval_res["source_fetched_ok"] else (f_status[:32] or "unfetched"),
            parsing_version="gemini_zip_v1",
            verification_status=eval_res["verification_status"],
            dns_check_status=eval_res["dns_status"],
            primary_skip_code=eval_res["primary_skip_code"],
            skip_codes_json=eval_res["block_codes"],
            skip_reason=eval_res["skip_reason"],
            extraction_confidence=0.95 if eval_res["source_verification_tier"] == "tier_1_official" else 0.5,
            eligibility_requirements_json=eval_res["elig_reqs_dict"],
            pipeline_version="vNext",
            source_verification_tier=eval_res["source_verification_tier"],
            corroboration_status=eval_res["corroboration_status"],
            research_status="imported_verified" if eval_res["outcome_bucket"] == "ready" else f"imported_{eval_res['outcome_bucket']}",
            raw_metadata_json={
                "import_origin": "gemini_zip",
                "manifest_index": rec.index,
                "cv_link_mode": cv_link_mode,
                "dns_detail": eval_res["dns_detail"],
                "fetch_status": f_status,
                "recipient_on_page": eval_res["recipient_on_page"],
            },
        )
        db.add(posting)
        db.flush()

        db.add(
            DiscoverySourceSighting(
                user_id=user.id,
                batch_id=batch.id,
                job_posting_id=posting.id,
                source_provider="gemini_zip",
                search_query=bundle.manifest_name,
                source_url=canon_url,
                canonical_url=canon_url,
                canonical_url_hash=url_hash,
                content_hash=content_hash,
                source_domain=eval_res["recipient_domain"],
                publisher=eval_res["publisher"],
                title_raw=rec.role[:300],
                access_status=posting.access_status,
                disposition=eval_res["outcome_bucket"],
                is_duplicate=(eval_res["outcome_bucket"] == "duplicate"),
                is_demo=False,
                primary_skip_code=eval_res["primary_skip_code"],
                secondary_skip_codes_json=eval_res["block_codes"][1:],
                research_stage="gemini_zip_import",
                corroborated=(eval_res["source_verification_tier"] == "tier_1_official"),
            )
        )

        cv_eval = eval_res["cv_eval"]
        match_row = JobMatch(
            user_id=user.id,
            batch_id=batch.id,
            job_posting_id=posting.id,
            cv_version_id=cv_ver.id,
            hard_filter_passed=bool(not eval_res["block_codes"]),
            hard_filter_failures_json=eval_res["block_reasons"],
            failure_codes_json=eval_res["block_codes"],
            eligibility_check_json={
                "status": eval_res["elig_res"].status,
                "passed": eval_res["elig_res"].passed,
                "failure_codes": eval_res["elig_res"].failure_codes,
                "failures": eval_res["elig_res"].failure_messages(),
            },
            fit_score=cv_eval.fit_score,
            score_components_json=cv_eval.score_components,
            supporting_facts_json=cv_eval.supporting_facts,
            missing_requirements_json=cv_eval.missing_requirements,
            decision_reason=eval_res["skip_reason"] or cv_eval.decision_reason,
            why_this_cv=f"Selected in Gemini ZIP manifest ({primary_pdf_name}, {cv_link_mode}); advisory fit {cv_eval.fit_score}/100",
            verified_skills_json=cv_eval.verified_skills,
            required_gaps_json=cv_eval.required_gaps,
            attachment_ready=True,
            is_selected_cv=True,
        )
        db.add(match_row)
        db.flush()

        app_rec = Application(
            user_id=user.id,
            batch_id=batch.id,
            job_posting_id=posting.id,
            job_match_id=match_row.id,
            selected_cv_version_id=cv_ver.id,
            frozen_cv_sha256=pdf_sha,
            frozen_cv_filename=primary_pdf_name,
            attachment_pdf_sha256=pdf_sha,
            supporting_file_ids_json=[],
            is_demo=False,
            pipeline_version="vNext",
            import_origin="gemini_zip",
            import_item_hash=item_hash,
            source_verification_tier=eval_res["source_verification_tier"],
            approval_state=eval_res["approval_state"],
            review_reasons_json=eval_res["review_reasons"],
            source_domain=eval_res["recipient_domain"],
            company_name=rec.company,
            role_title=rec.role,
            country_code=eval_res["parsed_loc"].country_code,
            city=eval_res["parsed_loc"].city,
            recipient_email=eval_res["normalized_to"],
            recipient_normalized=normalize_email_dedupe_key(eval_res["normalized_to"]),
            cc_emails_json=rec.cc,
            subject=rec.subject,
            body_text=rec.body,
            language="en",
            evidence_fact_ids_json=[],
            llm_provider_used="gemini_zip_import",
            llm_model_used=bundle.schema_version,
            transport_status=eval_res["transport_status"],
            discovery_category=eval_res["discovery_category"],
            primary_skip_code=eval_res["primary_skip_code"],
            skip_codes_json=eval_res["block_codes"],
            secondary_skip_codes_json=eval_res["block_codes"][1:],
            skip_or_error_reason=eval_res["skip_reason"],
            discovered_at=now,
        )
        sync_application_hash(app_rec)
        db.add(app_rec)
        db.flush()
        created_apps += 1

        record_application_transition(
            db,
            user_id=user.id,
            application_id=app_rec.id,
            field_name="approval_state",
            from_state="",
            to_state=app_rec.approval_state,
            actor=f"user:{user.id}",
            reason=f"Imported from Gemini ZIP '{Path(original_filename).name}' ({eval_res['outcome_bucket']})",
            metadata={
                "archive_sha256": bundle.archive_sha256,
                "cv_sha256": pdf_sha,
                "cv_link_mode": cv_link_mode,
                "source_verification_tier": eval_res["source_verification_tier"],
            },
        )

        cat = eval_res["discovery_category"]
        category_counts[cat] = category_counts.get(cat, 0) + 1
        for code in eval_res["block_codes"]:
            skip_reason_counts[code] = skip_reason_counts.get(code, 0) + 1

    counts = compute_batch_workflow_counts(db, batch_id=batch.id, user_id=user.id)
    batch.status = "completed"
    batch.finished_at = utcnow()
    batch.discovered_count = len(bundle.records)
    batch.eligible_count = counts["ready"] + counts["needs_review"]
    batch.drafted_count = counts["ready"] + counts["needs_review"]
    batch.skipped_count = counts["duplicate"] + counts["blocked"]
    batch.duplicates_removed_count = counts["duplicate"]
    batch.category_counts_json = category_counts
    batch.skip_reason_counts_json = skip_reason_counts
    batch.metrics_json = {
        **counts,
        "search_hits": len(bundle.records),
        "unique_source_urls": len({r.source_url for r in bundle.records if r.source_url}),
        "verified_role_email_invitations": counts["verified"],
        "eligible_for_draft": counts["ready"] + counts["needs_review"],
        "drafted": counts["ready"] + counts["needs_review"],
        "skipped": counts["duplicate"] + counts["blocked"],
        "duplicates": counts["duplicate"],
        "sent_accepted": 0,
        "goal_target": len(bundle.records),
        "goal_met": counts["ready"] >= len(bundle.records),
        "deadline_hit": False,
        "spend_cap_hit": False,
    }
    batch.stage_status_json = [
        {"stage": "archive_security_validation", "status": "completed", "pdfs_validated": len(referenced_pdfs)},
        {"stage": "cv_linking_and_ingestion", "status": "completed", "pdfs_linked": len(cv_by_filename)},
        {"stage": "independent_verification_and_dedup", "status": "completed", "counts": counts},
    ]

    db.add(
        AuditEvent(
            user_id=user.id,
            actor_user_id=user.id,
            action="gemini_bundle_imported",
            target_type="batch",
            target_id=batch.id,
            details_json={
                "archive_sha256": bundle.archive_sha256,
                "manifest_name": bundle.manifest_name,
                "records_count": len(bundle.records),
                "pdfs_count": len(referenced_pdfs),
                "ignored_files": bundle.ignored_files,
                "counts": counts,
            },
        )
    )
    db.commit()
    db.refresh(batch)

    return {
        "batch": batch,
        "duplicate_import": False,
        "created_applications": created_apps,
        "counts": counts,
        "ignored_files": bundle.ignored_files,
        "dedup_coverage": {k: v for k, v in dedup_coverage.items() if k != "sender_email"},
        "message": (
            f"Imported {len(bundle.records)} applications ({counts['ready']} ready, {counts['needs_review']} needs review, "
            f"{counts['duplicate']} duplicate, {counts['blocked']} blocked)."
        ),
    }


def revalidate_application_draft(
    db: Session,
    *,
    user: User,
    app_rec: Application,
    skip_network: bool = False,
    transport_override: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Re-run live source verification, MX check, Gmail Sent dedup, and mandatory eligibility on an application draft."""
    db.refresh(app_rec)
    if app_rec.user_id != user.id or initial_handoff_started(db, app_rec):
        raise BundleImportError("ALREADY_SENT", "Cannot revalidate application after handoff", status_code=409)
    reviewed_hash = application_draft_hash(app_rec)
    posting = db.scalar(select(JobPosting).where(JobPosting.id == app_rec.job_posting_id, JobPosting.user_id == user.id))
    profile = db.scalar(select(CandidateProfile).where(CandidateProfile.user_id == user.id))
    pref = db.scalar(select(SearchPreference).where(SearchPreference.user_id == user.id))
    cv_ver = (
        db.scalar(select(CvVersion).where(CvVersion.id == app_rec.selected_cv_version_id, CvVersion.user_id == user.id))
        if app_rec.selected_cv_version_id
        else None
    )
    if not posting or not profile or not pref or not cv_ver:
        raise BundleImportError("REVALIDATE_INCOMPLETE", "Application is missing posting, profile, or CV context", status_code=400)

    primary_conn = db.scalar(
        select(MailboxConnection)
        .where(
            MailboxConnection.user_id == user.id,
            MailboxConnection.status == "connected",
            MailboxConnection.revoked_at.is_(None),
        )
        .order_by(MailboxConnection.is_primary.desc())
    )
    dedup_coverage = (
        get_mailbox_dedup_coverage(db, conn=primary_conn)
        if primary_conn
        else {"coverage_mode": "applyflow_ledger_only"}
    )

    fetched_map = asyncio.run(
        _fetch_sources_concurrently(
            [(1, posting.source_url or "", app_rec.role_title or posting.title)],
            skip_network=skip_network,
            transport_override=transport_override,
        )
    )
    fetched_cand, f_status = fetched_map.get(1, (None, "skipped_network" if skip_network else "no_source_url"))
    cv_facts = list(db.scalars(select(CvExtractedFact).where(CvExtractedFact.cv_version_id == cv_ver.id)).all())

    eval_res = evaluate_imported_record(
        db,
        user=user,
        profile=profile,
        pref=pref,
        company=app_rec.company_name or posting.company_name,
        role=app_rec.role_title or posting.title,
        location=posting.location_raw or app_rec.city or app_rec.country_code,
        to_email=app_rec.recipient_email,
        cc_emails=app_rec.cc_emails_json or [],
        source_url=posting.source_url or "",
        fetched_cand=fetched_cand,
        fetch_status=f_status,
        cv_version=cv_ver,
        cv_facts=cv_facts,
        dedup_coverage=dedup_coverage,
        skip_network_dns=skip_network,
        exclude_application_id=app_rec.id,
    )

    # Verification may take time. Fence its result before changing either the posting or the draft.
    try:
        app_rec = lock_mutable_draft(db, user.id, app_rec.id)
    except ValueError as exc:
        raise BundleImportError("ALREADY_SENT", str(exc), status_code=409) from exc
    if application_draft_hash(app_rec) != reviewed_hash:
        raise BundleImportError("DRAFT_CHANGED", "Draft changed during verification. Revalidate again.", status_code=409)

    prev_state = app_rec.approval_state
    posting.dns_check_status = eval_res["dns_status"]
    posting.source_verification_tier = eval_res["source_verification_tier"]
    posting.corroboration_status = eval_res["corroboration_status"]
    posting.evidence_quote = eval_res["evidence_quote"]
    posting.verification_status = eval_res["verification_status"]
    posting.discovery_category = eval_res["discovery_category"]
    posting.primary_skip_code = eval_res["primary_skip_code"]
    posting.skip_codes_json = eval_res["block_codes"]
    posting.skip_reason = eval_res["skip_reason"]
    posting.eligibility_requirements_json = eval_res["elig_reqs_dict"]

    app_rec.source_verification_tier = eval_res["source_verification_tier"]
    app_rec.review_reasons_json = eval_res["review_reasons"]
    app_rec.discovery_category = eval_res["discovery_category"]
    app_rec.primary_skip_code = eval_res["primary_skip_code"]
    app_rec.skip_codes_json = eval_res["block_codes"]
    app_rec.skip_or_error_reason = eval_res["skip_reason"]

    pending_error = db.scalar(select(OutboxEntry.last_error).where(
        OutboxEntry.application_id == app_rec.id, OutboxEntry.user_id == user.id,
        OutboxEntry.kind == "initial", OutboxEntry.status.in_(PENDING),
    ).order_by(OutboxEntry.created_at.desc(), OutboxEntry.id.desc()).limit(1))

    if eval_res["block_codes"]:
        app_rec.transport_status = "skipped"
        app_rec.approval_state = "rejected"
        app_rec.approved_content_hash = ""
        app_rec.approved_at = None
        if cancel_initial_for_locked_draft(db, app_rec, f"Cancelled due to revalidation block: {eval_res['skip_reason']}"):
            release_reserved_recipients_if_pre_handoff_failed(db, user_id=user.id, application_id=app_rec.id)
    elif app_rec.transport_status == "skipped" and app_rec.import_origin == "gemini_zip":
        # Previously blocked item became valid after recipient/location update
        app_rec.transport_status = "draft"
        app_rec.approval_state = eval_res["approval_state"]
        app_rec.approved_content_hash = ""
        app_rec.approved_at = None
    elif eval_res["approval_state"] == "needs_human_review" and app_rec.approval_state == "approved":
        app_rec.approval_state = "needs_human_review"
        app_rec.approved_content_hash = ""
        app_rec.approved_at = None
        if cancel_initial_for_locked_draft(db, app_rec, "Cancelled because revalidation requires human review before sending"):
            app_rec.transport_status = "draft"
            release_reserved_recipients_if_pre_handoff_failed(db, user_id=user.id, application_id=app_rec.id)
    elif app_rec.approval_state != "approved":
        app_rec.approval_state = eval_res["approval_state"]
    elif pending_error is not None:
        app_rec.transport_status = "queued"
        if pending_error and not app_rec.skip_or_error_reason:
            app_rec.skip_or_error_reason = pending_error

    sync_application_hash(app_rec)
    record_application_transition(
        db,
        user_id=user.id,
        application_id=app_rec.id,
        field_name="approval_state",
        from_state=prev_state,
        to_state=app_rec.approval_state,
        actor=f"user:{user.id}",
        reason=f"Revalidated source & eligibility ({eval_res['outcome_bucket']})",
        metadata={
            "dns_status": eval_res["dns_status"],
            "source_verification_tier": eval_res["source_verification_tier"],
            "review_reasons": eval_res["review_reasons"],
            "block_codes": eval_res["block_codes"],
        },
    )
    db.commit()
    return {
        "application_id": app_rec.id,
        "outcome_bucket": eval_res["outcome_bucket"],
        "approval_state": app_rec.approval_state,
        "transport_status": app_rec.transport_status,
        "source_verification_tier": app_rec.source_verification_tier,
        "dns_status": eval_res["dns_status"],
        "review_reasons": eval_res["review_reasons"],
        "block_codes": eval_res["block_codes"],
        "skip_reason": eval_res["skip_reason"],
        "evidence_quote": eval_res["evidence_quote"],
    }
