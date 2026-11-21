"""Candidate profile, CV Container, supporting attachments, search preferences, and global geo endpoints."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile, status
from pydantic import BaseModel, StringConstraints
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.config import get_settings
from app.core.security import normalize_email_dedupe_key, sanitize_filename, validate_magic_bytes
from app.db.base import new_uuid, utcnow
from app.db.models import (
    Application,
    CandidateProfile,
    CvDocument,
    CvExtractedFact,
    CvVersion,
    JobPosting,
    SearchPreference,
    SupportingFile,
    User,
)
from app.db.session import get_db
from app.schemas import CandidateProfileUpdate, SearchPreferenceUpdate, SupportingFilePatchRequest
from app.services.cv_parser import apply_parsed_cv, parse_cv_bytes
from app.services.dates import ensure_valid_timezone
from app.services.discovery.career_page import EMAIL_RE
from app.services.geo import list_countries, search_geo_locations
from app.services.pdf_renderer import (
    DOCX_MIME,
    DOCX_ONLY_REASON,
    PDF_MIME,
    derived_pdf_filename,
    ensure_cv_version_pdf_ready,
    prepare_new_cv_pdf,
    requires_docx_only,
    store_prepared_derivative,
)
from app.services.storage import compute_sha256, get_storage_adapter

router = APIRouter(tags=["workspace"])

PREFERENCE_DEFAULT_EXPLANATIONS = {
    "daily_recipient_limit": "Conservative default (20 recipients/24h, including CCs and follow-ups) to protect sender reputation; actual provider ceilings vary by account tier.",
    "unknown_salary_policy": "Allow neutral by default so unstated salaries do not hide valid roles unless you strictly require disclosed pay.",
    "unknown_location_policy": "Skip ambiguous or country-unspecified postings by default so you do not apply to ineligible locations.",
    "unknown_eligibility_policy": "Skip when work authorization eligibility cannot be verified against your profile.",
    "followup_interval_days": "Wait 7 days before sending at most 1 polite follow-up in the original thread (stopped automatically on any reply or bounce).",
    "include_standing_career_invitations": "Disabled by default so batches target concrete open positions unless you opt into general 'send your CV' career invitations.",
}


def _recompute_profile_conflicts(db: Session, profile: CandidateProfile) -> list[dict[str, str]]:
    warnings: list[dict[str, str]] = []
    versions = db.scalars(
        select(CvVersion).where(CvVersion.user_id == profile.user_id, CvVersion.parse_status == "parsed")
    ).all()
    for ver in versions:
        summary = ver.raw_summary_json or {}
        cv_names = summary.get("detected_names", [])
        if profile.full_name and cv_names:
            first_cv_name = str(cv_names[0]).strip().lower()
            if first_cv_name and profile.full_name.strip().lower() not in first_cv_name and first_cv_name not in profile.full_name.strip().lower():
                warnings.append(
                    {
                        "type": "name_mismatch",
                        "cv_version_id": ver.id,
                        "message": f"Profile name '{profile.full_name}' differs from top-line CV text '{cv_names[0]}' in {ver.original_filename}",
                    }
                )
        cv_emails = summary.get("detected_emails", [])
        if profile.contact_email and cv_emails and profile.contact_email.lower() not in [e.lower() for e in cv_emails]:
            warnings.append(
                {
                    "type": "email_mismatch",
                    "cv_version_id": ver.id,
                    "message": f"Profile email '{profile.contact_email}' differs from email(s) {cv_emails} in {ver.original_filename}",
                }
            )
    profile.consistency_warnings_json = warnings
    return warnings


@router.get("/geo/countries")
def api_geo_countries() -> dict:
    return {"countries": list_countries()}


@router.get("/geo/search")
def api_geo_search(q: str = "", limit: int = 15) -> dict:
    return {"locations": search_geo_locations(q, limit=min(max(1, limit), 50))}


@router.get("/profile")
def api_get_profile(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    profile = db.scalar(select(CandidateProfile).where(CandidateProfile.user_id == user.id))
    if not profile:
        profile = CandidateProfile(user_id=user.id, full_name=user.full_name, contact_email=user.email)
        db.add(profile)
        db.commit()
        db.refresh(profile)
    return {
        "profile": {
            "full_name": profile.full_name,
            "headline": profile.headline,
            "contact_email": profile.contact_email,
            "phone": profile.phone,
            "residence_country_code": profile.residence_country_code,
            "residence_city": profile.residence_city,
            "residence_geo_id": profile.residence_geo_id,
            "notice_period_days": profile.notice_period_days,
            "availability_note": profile.availability_note,
            "work_authorizations": profile.work_authorizations_json,
            "nationalities": profile.nationalities_json,
            "residency_permits": profile.residency_permits_json,
            "confirmed_languages": profile.confirmed_languages_json,
            "confirmed_certifications": profile.confirmed_certifications_json,
            "requires_sponsorship": profile.requires_sponsorship,
            "willing_to_relocate": profile.willing_to_relocate,
            "relocation_targets": profile.relocation_targets_json,
            "preferred_roles": profile.preferred_roles_json,
            "salary_expectation_min": profile.salary_expectation_min,
            "salary_expectation_max": profile.salary_expectation_max,
            "salary_currency": profile.salary_currency,
            "salary_period": profile.salary_period,
            "salary_basis": profile.salary_basis,
            "signature_text": profile.signature_text,
            "portfolio_links": profile.portfolio_links_json,
            "consistency_warnings": profile.consistency_warnings_json,
            "confirmed_at": profile.confirmed_at.isoformat() if profile.confirmed_at else None,
        }
    }


@router.put("/profile")
def api_update_profile(
    payload: CandidateProfileUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    profile = db.scalar(select(CandidateProfile).where(CandidateProfile.user_id == user.id))
    if not profile:
        profile = CandidateProfile(user_id=user.id)
        db.add(profile)

    profile.full_name = payload.full_name.strip()
    profile.headline = payload.headline.strip()
    profile.contact_email = payload.contact_email.strip()
    profile.phone = payload.phone.strip()
    profile.residence_country_code = payload.residence_country_code.upper().strip()
    profile.residence_city = payload.residence_city.strip()
    profile.residence_geo_id = payload.residence_geo_id.strip()
    profile.notice_period_days = payload.notice_period_days
    profile.availability_note = payload.availability_note.strip()
    profile.work_authorizations_json = [c.upper().strip() for c in payload.work_authorizations if c.strip()]
    profile.nationalities_json = [c.upper().strip() for c in payload.nationalities if c.strip()]
    profile.residency_permits_json = [p.upper().strip() for p in payload.residency_permits if p.strip()]
    profile.confirmed_languages_json = [item.model_dump() for item in payload.confirmed_languages if item.language.strip()]
    profile.confirmed_certifications_json = [c.strip() for c in payload.confirmed_certifications if c.strip()]
    profile.requires_sponsorship = payload.requires_sponsorship
    profile.willing_to_relocate = payload.willing_to_relocate
    profile.relocation_targets_json = [c.upper().strip() for c in payload.relocation_targets if c.strip()]
    profile.preferred_roles_json = [r.strip() for r in payload.preferred_roles if r.strip()]
    profile.salary_expectation_min = payload.salary_expectation_min
    profile.salary_expectation_max = payload.salary_expectation_max
    profile.salary_currency = payload.salary_currency.upper().strip()
    profile.salary_period = payload.salary_period
    profile.salary_basis = payload.salary_basis
    profile.signature_text = payload.signature_text.strip()
    profile.portfolio_links_json = [item.model_dump() for item in payload.portfolio_links]
    profile.confirmed_at = utcnow() if payload.confirm_profile else None
    if payload.full_name.strip():
        user.full_name = payload.full_name.strip()

    _recompute_profile_conflicts(db, profile)
    db.commit()
    return api_get_profile(user=user, db=db)


@router.get("/cvs")
def api_list_cvs(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    docs = db.scalars(
        select(CvDocument).where(CvDocument.user_id == user.id).order_by(CvDocument.created_at.desc())
    ).all()
    items = []
    for doc in docs:
        versions = db.scalars(
            select(CvVersion)
            .where(CvVersion.cv_document_id == doc.id, CvVersion.user_id == user.id)
            .order_by(CvVersion.version_number.desc())
        ).all()
        items.append(
            {
                "id": doc.id,
                "variant_label": doc.variant_label,
                "is_archived": doc.is_archived,
                "active_version_id": doc.active_version_id,
                "created_at": doc.created_at.isoformat(),
                "versions": [
                    {
                        "id": v.id,
                        "version_number": v.version_number,
                        "original_filename": v.original_filename,
                        "mime_type": v.mime_type,
                        "size_bytes": v.size_bytes,
                        "page_count": v.page_count,
                        "sha256": v.sha256,
                        "parse_status": v.parse_status,
                        "ocr_status": v.ocr_status,
                        "parse_error": v.parse_error,
                        "detected_language": v.detected_language,
                        "total_experience_months": v.total_experience_months,
                        "pdf_conversion_status": v.pdf_conversion_status,
                        "pdf_conversion_error": v.pdf_conversion_error,
                        "pdf_page_count": v.pdf_page_count,
                        "derived_pdf_sha256": v.derived_pdf_sha256,
                        "created_at": v.created_at.isoformat(),
                    }
                    for v in versions
                ],
            }
        )
    return {"cvs": items}


@router.post("/cvs/upload", status_code=status.HTTP_201_CREATED)
async def api_upload_cv(
    variant_label: str = Form("General"),
    document_id: str | None = Form(None),
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    settings = get_settings()
    raw_bytes = await file.read(settings.cv_max_upload_bytes + 1)
    if not raw_bytes:
        raise HTTPException(status_code=400, detail={"code": "EMPTY_FILE", "message": "Uploaded file is empty"})
    if len(raw_bytes) > settings.cv_max_upload_bytes:
        raise HTTPException(
            status_code=413,
            detail={"code": "FILE_TOO_LARGE", "message": f"File exceeds {settings.cv_max_upload_bytes} byte limit"},
        )

    safe_name = sanitize_filename(file.filename or "cv.pdf", default="cv.pdf")
    ext = Path(safe_name).suffix.lower()
    try:
        verified_mime = validate_magic_bytes(raw_bytes, ext)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "INVALID_FILE_TYPE", "message": str(exc)}) from exc

    doc_id = document_id or new_uuid()
    owned_document = (CvDocument.id == doc_id, CvDocument.user_id == user.id)
    if document_id:
        if not db.scalar(select(CvDocument.id).where(*owned_document)):
            raise HTTPException(status_code=404, detail={"code": "CV_NOT_FOUND", "message": "CV document not found"})

    version_id = new_uuid()
    sha256 = compute_sha256(raw_bytes)
    storage_key = f"users/{user.id}/cvs/{doc_id}/{version_id}_{safe_name}"
    parsed = parse_cv_bytes(raw_bytes, verified_mime)
    cv_ver = CvVersion(
        id=version_id,
        cv_document_id=doc_id,
        user_id=user.id,
        original_filename=safe_name,
        mime_type=verified_mime,
        size_bytes=len(raw_bytes),
        page_count=parsed.page_count,
        sha256=sha256,
        storage_key=storage_key,
        parse_status=parsed.parse_status,
        ocr_status=parsed.ocr_status,
        parse_error=parsed.parse_error,
        detected_language=parsed.detected_language,
        extracted_text=parsed.extracted_text,
        total_experience_months=parsed.total_experience_months,
        raw_summary_json=parsed.summary,
    )
    derivative = prepare_new_cv_pdf(cv_ver, raw_bytes)
    if document_id:
        # This write serializes numbering on both PostgreSQL and SQLite, which ignores FOR UPDATE.
        claimed = db.scalar(update(CvDocument).where(*owned_document)
                            .values(active_version_id=version_id, is_archived=False).returning(CvDocument.id))
        if not claimed:
            raise HTTPException(status_code=404, detail={"code": "CV_NOT_FOUND", "message": "CV document not found"})
        last = db.scalar(select(func.max(CvVersion.version_number)).where(CvVersion.cv_document_id == doc_id))
        cv_ver.version_number = (last or 0) + 1
    else:
        db.add(CvDocument(id=doc_id, user_id=user.id, variant_label=variant_label.strip()[:80] or "General",
                          active_version_id=version_id))
        db.flush()
        cv_ver.version_number = 1
    db.add(cv_ver)
    db.flush()

    apply_parsed_cv(db, cv_ver, parsed)

    profile = db.scalar(select(CandidateProfile).where(CandidateProfile.user_id == user.id))
    if profile:
        _recompute_profile_conflicts(db, profile)

    db.flush()
    storage = get_storage_adapter()
    storage.put_bytes(storage_key, raw_bytes, content_type=verified_mime)
    store_prepared_derivative(cv_ver, derivative, storage)
    db.commit()
    return {
        "document_id": doc_id,
        "version_id": cv_ver.id,
        "parse_status": cv_ver.parse_status,
        "ocr_status": cv_ver.ocr_status,
        "parse_error": cv_ver.parse_error,
        "sha256": sha256,
        "pdf_conversion_status": cv_ver.pdf_conversion_status,
        "derived_pdf_sha256": cv_ver.derived_pdf_sha256,
        "pdf_conversion_error": cv_ver.pdf_conversion_error,
        "facts_count": len(parsed.facts),
        "total_experience_months": cv_ver.total_experience_months,
    }


@router.get("/cvs/{doc_id}/versions/{version_id}")
def api_get_cv_version(
    doc_id: str,
    version_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    ver = db.scalar(
        select(CvVersion).where(
            CvVersion.id == version_id,
            CvVersion.cv_document_id == doc_id,
            CvVersion.user_id == user.id,
        )
    )
    if not ver:
        raise HTTPException(status_code=404, detail={"code": "CV_VERSION_NOT_FOUND", "message": "CV version not found"})
    facts = db.scalars(select(CvExtractedFact).where(CvExtractedFact.cv_version_id == ver.id, CvExtractedFact.user_id == user.id)).all()
    return {
        "version": {
            "id": ver.id,
            "document_id": ver.cv_document_id,
            "version_number": ver.version_number,
            "original_filename": ver.original_filename,
            "mime_type": ver.mime_type,
            "size_bytes": ver.size_bytes,
            "page_count": ver.page_count,
            "sha256": ver.sha256,
            "parse_status": ver.parse_status,
            "ocr_status": ver.ocr_status,
            "parse_error": ver.parse_error,
            "detected_language": ver.detected_language,
            "total_experience_months": ver.total_experience_months,
            "pdf_conversion_status": ver.pdf_conversion_status,
            "pdf_conversion_error": ver.pdf_conversion_error,
            "pdf_page_count": ver.pdf_page_count,
            "derived_pdf_sha256": ver.derived_pdf_sha256,
            "extracted_text_preview": ver.extracted_text[:6000],
            "corrections": (ver.raw_summary_json or {}).get("corrections", []),
            "facts": [
                {
                    "id": f.id,
                    "category": f.category,
                    "title": f.title,
                    "value": f.value,
                    "evidence_quote": f.evidence_quote,
                    "start_date": f.start_date,
                    "end_date": f.end_date,
                    "confidence": f.confidence,
                    "source_page": f.source_page,
                }
                for f in facts
            ],
        }
    }


class FactCorrection(BaseModel):
    statement: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=240)]
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]


@router.post("/cvs/{doc_id}/versions/{version_id}/facts/{fact_id}/corrections", status_code=201)
def api_correct_cv_fact(doc_id: str, version_id: str, fact_id: str, payload: FactCorrection,
                        user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    version = db.scalar(select(CvVersion).where(
        CvVersion.id == version_id, CvVersion.cv_document_id == doc_id, CvVersion.user_id == user.id
    ).with_for_update())
    fact = db.scalar(select(CvExtractedFact).where(
        CvExtractedFact.id == fact_id, CvExtractedFact.cv_version_id == version_id, CvExtractedFact.user_id == user.id
    ))
    if not version or not fact:
        raise HTTPException(404, detail="CV fact not found")
    if fact.confidence not in ("confirmed", "evidenced"):
        raise HTTPException(409, detail="This fact has changed. Refresh the version before correcting it.")
    confirmed = CvExtractedFact(
        id=new_uuid(), user_id=user.id, cv_version_id=version.id, category=fact.category,
        title=payload.statement, value=payload.statement, evidence_quote=payload.statement, confidence="confirmed",
    )
    change = {
        "source_fact_id": fact.id, "confirmed_fact_id": confirmed.id, "source_sha256": version.sha256,
        "confirmed_by": user.id, "confirmed_at": utcnow().isoformat(), "reason": payload.reason,
    }
    summary = version.raw_summary_json or {}
    version.raw_summary_json = {**summary, "corrections": [*summary.get("corrections", []), change]}
    fact.confidence = "superseded"
    db.add(confirmed)
    db.commit()
    return {"correction": change}


def _disposition(kind: str, name: str) -> str:
    if name.isascii():
        return f'{kind}; filename="{name}"'
    return f"{kind}; filename*=UTF-8''{quote(name, safe='')}"


def _verified_attachment(storage, key: str, expected_sha: str) -> bytes:
    try:
        data = storage.get_bytes(key)
        if compute_sha256(data) == expected_sha:
            return data
    except (OSError, ValueError, BotoCoreError, ClientError):
        pass
    raise HTTPException(status_code=409, detail={
        "code": "ATTACHMENT_INTEGRITY_ERROR", "message": "Stored attachment could not be verified."
    })


@router.get("/applications/{application_id}/attachment-preview", response_class=Response, responses={
    200: {"description": "Frozen CV attachment", "content": {
        mime: {"schema": {"type": "string", "format": "binary"}} for mime in (PDF_MIME, DOCX_MIME)
    }},
    409: {"description": "Attachment snapshot missing or integrity check failed"},
})
def api_preview_application_attachment(
    application_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> Response:
    application = db.scalar(select(Application).where(Application.id == application_id, Application.user_id == user.id))
    if not application:
        raise HTTPException(404, detail={"code": "APPLICATION_NOT_FOUND", "message": "Application not found"})
    historical = application.sent_at is not None or application.transport_status in (
        "sent", "provider_accepted", "sending", "send_uncertain", "skipped",
    )
    native_draft = application.import_origin != "gemini_zip" and not historical
    digest = application.attachment_pdf_sha256
    filename = application.frozen_cv_filename
    if not application.selected_cv_version_id or not application.frozen_cv_sha256 or (
        not native_draft and not (digest and filename)
    ):
        raise HTTPException(409, detail={"code": "ATTACHMENT_SNAPSHOT_MISSING", "message": "No exact attachment snapshot."})
    storage = get_storage_adapter()
    if native_draft:
        from app.services.outbox import _load_verified_attachments

        posting = db.scalar(select(JobPosting).where(JobPosting.id == application.job_posting_id, JobPosting.user_id == user.id))
        if not posting or requires_docx_only(posting.attachment_requirements_json):
            raise HTTPException(409, detail={
                "code": "ATTACHMENT_NOT_READY", "message": DOCX_ONLY_REASON if posting else "Posting unavailable.",
            })
        try:
            cv = _load_verified_attachments(db, app_rec=application, storage=storage)[0]
        except Exception as exc:
            raise HTTPException(409, detail={
                "code": "ATTACHMENT_INTEGRITY_ERROR", "message": "Frozen PDF attachment could not be verified.",
            }) from exc
        data, mime, filename, digest = cv.data, cv.mime_type, cv.filename, cv.expected_sha256
    else:
        version = db.scalar(select(CvVersion).where(
            CvVersion.id == application.selected_cv_version_id, CvVersion.user_id == user.id,
        ))
        if not version or application.frozen_cv_sha256 != version.sha256:
            raise HTTPException(409, detail={"code": "ATTACHMENT_INTEGRITY_ERROR", "message": "Frozen CV version mismatch."})
        data = _verified_attachment(storage, version.storage_key, version.sha256)
        mime = version.mime_type
        if digest != version.sha256:
            if not version.derived_pdf_storage_key or version.derived_pdf_sha256 != digest:
                raise HTTPException(409, detail={"code": "ATTACHMENT_INTEGRITY_ERROR", "message": "Frozen PDF mismatch."})
            data = _verified_attachment(storage, version.derived_pdf_storage_key, digest)
            mime = PDF_MIME
    filename = sanitize_filename(filename, "cv")
    return Response(data, media_type=mime, headers={
        "Content-Disposition": _disposition("inline", filename),
        "X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store",
        "X-Attachment-SHA256": digest,
    })


@router.get("/cvs/{doc_id}/versions/{version_id}/download")
def api_download_cv_version(
    doc_id: str,
    version_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    ver = db.scalar(
        select(CvVersion).where(
            CvVersion.id == version_id,
            CvVersion.cv_document_id == doc_id,
            CvVersion.user_id == user.id,
        )
    )
    if not ver:
        raise HTTPException(status_code=404, detail={"code": "CV_VERSION_NOT_FOUND", "message": "CV version not found"})
    data = _verified_attachment(get_storage_adapter(), ver.storage_key, ver.sha256)
    safe_dl = sanitize_filename(ver.original_filename, "cv.pdf")
    return Response(
        content=data,
        media_type=ver.mime_type,
        headers={
            "Content-Disposition": _disposition("attachment", safe_dl),
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/cvs/{doc_id}/versions/{version_id}/download-pdf")
def api_download_cv_pdf_version(
    doc_id: str,
    version_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    ver = db.scalar(
        select(CvVersion).where(
            CvVersion.id == version_id,
            CvVersion.cv_document_id == doc_id,
            CvVersion.user_id == user.id,
        )
    )
    if not ver:
        raise HTTPException(status_code=404, detail={"code": "CV_VERSION_NOT_FOUND", "message": "CV version not found"})
    adapter = get_storage_adapter()
    ready, err_msg = ensure_cv_version_pdf_ready(ver, storage=adapter)
    db.commit()
    if not ready or not ver.derived_pdf_storage_key:
        raise HTTPException(status_code=422, detail={"code": "ATTACHMENT_NOT_READY", "message": err_msg or "Derived PDF not available"})
    pdf_bytes = _verified_attachment(adapter, ver.derived_pdf_storage_key, ver.derived_pdf_sha256)
    safe_pdf = sanitize_filename(derived_pdf_filename(ver.original_filename), "cv.pdf")
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": _disposition("attachment", safe_pdf),
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/cvs/{doc_id}/versions/{version_id}/reparse")
def api_reparse_cv_version(
    doc_id: str,
    version_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    ver = db.scalar(
        select(CvVersion).where(
            CvVersion.id == version_id,
            CvVersion.cv_document_id == doc_id,
            CvVersion.user_id == user.id,
        ).with_for_update()
    )
    if not ver:
        raise HTTPException(status_code=404, detail={"code": "CV_VERSION_NOT_FOUND", "message": "CV version not found"})
    data = _verified_attachment(get_storage_adapter(), ver.storage_key, ver.sha256)
    parsed = parse_cv_bytes(data, ver.mime_type)
    apply_parsed_cv(db, ver, parsed)
    ensure_cv_version_pdf_ready(ver, storage=get_storage_adapter(), source_bytes=data)
    db.commit()
    return {"parse_status": ver.parse_status, "facts_count": len(parsed.facts), "pdf_conversion_status": ver.pdf_conversion_status}


@router.post("/cvs/{doc_id}/archive")
def api_archive_cv(doc_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    doc = db.scalar(select(CvDocument).where(CvDocument.id == doc_id, CvDocument.user_id == user.id))
    if not doc:
        raise HTTPException(status_code=404, detail={"code": "CV_NOT_FOUND", "message": "CV document not found"})
    doc.is_archived = not doc.is_archived
    db.commit()
    return {"id": doc.id, "is_archived": doc.is_archived}


@router.get("/supporting-files")
def api_list_supporting_files(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    files = db.scalars(select(SupportingFile).where(SupportingFile.user_id == user.id).order_by(SupportingFile.created_at.desc())).all()
    return {
        "supporting_files": [
            {
                "id": sf.id,
                "label": sf.label,
                "file_kind": sf.file_kind,
                "original_filename": sf.original_filename,
                "size_bytes": sf.size_bytes,
                "sha256": sf.sha256,
                "attach_by_default": sf.attach_by_default,
                "enabled_for_matching": sf.enabled_for_matching,
                "keywords": sf.keywords_json,
                "created_at": sf.created_at.isoformat(),
            }
            for sf in files
        ]
    }


@router.post("/supporting-files", status_code=status.HTTP_201_CREATED)
async def api_upload_supporting_file(
    label: str = Form(...),
    file_kind: str = Form("portfolio"),
    enabled_for_matching: bool = Form(False),
    attach_by_default: bool = Form(False),
    keywords: str = Form(""),
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    data = await file.read()
    if not data or len(data) > get_settings().cv_max_upload_bytes:
        raise HTTPException(status_code=400, detail={"code": "INVALID_FILE", "message": "File missing or exceeds size limit"})
    safe_name = sanitize_filename(file.filename or "portfolio.pdf", "portfolio.pdf")
    ext = Path(safe_name).suffix.lower()
    try:
        mime = validate_magic_bytes(data, ext)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "INVALID_FILE_TYPE", "message": str(exc)}) from exc

    sf_id = new_uuid()
    storage_key = f"users/{user.id}/supporting/{sf_id}_{safe_name}"
    get_storage_adapter().put_bytes(storage_key, data, content_type=mime)

    sf = SupportingFile(
        id=sf_id,
        user_id=user.id,
        label=label.strip()[:120],
        file_kind=file_kind[:32],
        original_filename=safe_name,
        mime_type=mime,
        size_bytes=len(data),
        sha256=compute_sha256(data),
        storage_key=storage_key,
        attach_by_default=attach_by_default,
        enabled_for_matching=enabled_for_matching,
        keywords_json=[k.strip() for k in keywords.split(",") if k.strip()],
    )
    db.add(sf)
    db.commit()
    return {"id": sf.id, "sha256": sf.sha256}


@router.patch("/supporting-files/{sf_id}")
def api_patch_supporting_file(
    sf_id: str,
    payload: SupportingFilePatchRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    sf = db.scalar(select(SupportingFile).where(SupportingFile.id == sf_id, SupportingFile.user_id == user.id))
    if not sf:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Supporting file not found"})
    if payload.label is not None:
        sf.label = payload.label.strip()
    if payload.attach_by_default is not None:
        sf.attach_by_default = payload.attach_by_default
    if payload.enabled_for_matching is not None:
        sf.enabled_for_matching = payload.enabled_for_matching
    if payload.keywords is not None:
        sf.keywords_json = [k.strip() for k in payload.keywords if k.strip()]
    db.commit()
    return {"ok": True}


@router.delete("/supporting-files/{sf_id}")
def api_delete_supporting_file(
    sf_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    sf = db.scalar(select(SupportingFile).where(SupportingFile.id == sf_id, SupportingFile.user_id == user.id))
    if not sf:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Supporting file not found"})
    try:
        get_storage_adapter().delete_bytes(sf.storage_key)
    except Exception:
        pass
    db.delete(sf)
    db.commit()
    return {"ok": True}


@router.get("/preferences")
def api_get_preferences(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    pref = db.scalar(select(SearchPreference).where(SearchPreference.user_id == user.id))
    if not pref:
        pref = SearchPreference(user_id=user.id, timezone=user.timezone or "UTC")
        db.add(pref)
        db.commit()
        db.refresh(pref)
    return {
        "preferences": {
            "target_titles": pref.target_titles_json,
            "role_families": pref.role_families_json,
            "industries": pref.industries_json,
            "seniority_levels": pref.seniority_levels_json,
            "excluded_keywords": pref.excluded_keywords_json,
            "excluded_companies": pref.excluded_companies_json,
            "excluded_recipients": pref.excluded_recipients_json or [],
            "target_locations": pref.target_locations_json,
            "allowed_remote_modes": pref.allowed_remote_modes_json,
            "allow_relocation": pref.allow_relocation,
            "require_sponsorship_friendly": pref.require_sponsorship_friendly,
            "employment_types": pref.employment_types_json,
            "freshness_days": pref.freshness_days,
            "min_match_score": pref.min_match_score,
            "max_experience_gap_years": pref.max_experience_gap_years,
            "min_salary_amount": pref.min_salary_amount,
            "min_salary_currency": pref.min_salary_currency,
            "min_salary_period": pref.min_salary_period,
            "max_weekly_hours": pref.max_weekly_hours,
            "unknown_salary_policy": pref.unknown_salary_policy,
            "unknown_dates_policy": pref.unknown_dates_policy,
            "unknown_location_policy": pref.unknown_location_policy,
            "unknown_eligibility_policy": pref.unknown_eligibility_policy,
            "search_languages": pref.search_languages_json,
            "application_language_mode": pref.application_language_mode,
            "explicit_application_language": pref.explicit_application_language,
            "email_tone": pref.email_tone,
            "email_length_words_min": pref.email_length_words_min,
            "email_length_words_max": pref.email_length_words_max,
            "batch_size": pref.batch_size,
            "daily_recipient_limit": pref.daily_recipient_limit,
            "send_window_start_hhmm": pref.send_window_start_hhmm,
            "send_window_end_hhmm": pref.send_window_end_hhmm,
            "quiet_hours_start_hhmm": pref.quiet_hours_start_hhmm,
            "quiet_hours_end_hhmm": pref.quiet_hours_end_hhmm,
            "timezone": pref.timezone,
            "max_batch_cost_usd": pref.max_batch_cost_usd,
            "max_monthly_cost_usd": pref.max_monthly_cost_usd,
            "max_search_queries_per_batch": pref.max_search_queries_per_batch,
            "followup_interval_days": pref.followup_interval_days,
            "followup_max_count": pref.followup_max_count,
            "include_standing_career_invitations": pref.include_standing_career_invitations,
            "campaign_mode": pref.campaign_mode,
            "live_consent_granted": pref.live_consent_granted,
            "live_consent_version": pref.live_consent_version,
            "live_consent_at": pref.live_consent_at.isoformat() if pref.live_consent_at else None,
            "save_drafts_in_gmail": pref.save_drafts_in_gmail,
            "target_goal_count": pref.target_goal_count,
            "budget_config": pref.budget_config_json,
            "require_official_or_authorized_send": pref.require_official_or_authorized_send,
        },
        "default_explanations": PREFERENCE_DEFAULT_EXPLANATIONS,
        "operator_daily_hard_cap": get_settings().max_daily_recipients_hard_cap,
    }


@router.put("/preferences")
def api_update_preferences(
    payload: SearchPreferenceUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    try:
        ensure_valid_timezone(payload.timezone)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "INVALID_TIMEZONE", "message": str(exc)}) from exc

    pref = db.scalar(select(SearchPreference).where(SearchPreference.user_id == user.id))
    if not pref:
        pref = SearchPreference(user_id=user.id)
        db.add(pref)

    hard_cap = get_settings().max_daily_recipients_hard_cap
    pref.target_titles_json = [x.strip() for x in payload.target_titles if x.strip()]
    pref.role_families_json = [x.strip() for x in payload.role_families if x.strip()]
    pref.industries_json = [x.strip() for x in payload.industries if x.strip()]
    pref.seniority_levels_json = [x.strip() for x in payload.seniority_levels if x.strip()]
    pref.excluded_keywords_json = [x.strip() for x in payload.excluded_keywords if x.strip()]
    pref.excluded_companies_json = [x.strip() for x in payload.excluded_companies if x.strip()]
    if payload.excluded_recipients is not None:
        excluded = {normalize_email_dedupe_key(addr) for addr in payload.excluded_recipients}
        if any(not EMAIL_RE.fullmatch(addr) or len(addr) > 254 for addr in excluded):
            raise HTTPException(422, detail={"code": "INVALID_RECIPIENT", "message": "Enter valid excluded email addresses"})
        pref.excluded_recipients_json = sorted(excluded)
    pref.target_locations_json = [item.model_dump() for item in payload.target_locations]
    pref.allowed_remote_modes_json = list(payload.allowed_remote_modes)
    pref.allow_relocation = payload.allow_relocation
    pref.require_sponsorship_friendly = payload.require_sponsorship_friendly
    pref.employment_types_json = list(payload.employment_types)
    pref.freshness_days = payload.freshness_days
    pref.min_match_score = payload.min_match_score
    pref.max_experience_gap_years = payload.max_experience_gap_years
    pref.min_salary_amount = payload.min_salary_amount
    pref.min_salary_currency = payload.min_salary_currency.upper().strip()
    pref.min_salary_period = payload.min_salary_period
    pref.max_weekly_hours = payload.max_weekly_hours
    pref.unknown_salary_policy = payload.unknown_salary_policy
    pref.unknown_dates_policy = payload.unknown_dates_policy
    pref.unknown_location_policy = payload.unknown_location_policy
    pref.unknown_eligibility_policy = payload.unknown_eligibility_policy
    pref.search_languages_json = [x.lower().strip() for x in payload.search_languages if x.strip()] or ["en"]
    pref.application_language_mode = payload.application_language_mode
    pref.explicit_application_language = payload.explicit_application_language.lower().strip() or "en"
    pref.email_tone = payload.email_tone
    pref.email_length_words_min = payload.email_length_words_min
    pref.email_length_words_max = max(payload.email_length_words_min + 20, payload.email_length_words_max)
    pref.batch_size = payload.batch_size
    pref.daily_recipient_limit = min(payload.daily_recipient_limit, hard_cap)
    pref.send_window_start_hhmm = payload.send_window_start_hhmm
    pref.send_window_end_hhmm = payload.send_window_end_hhmm
    pref.quiet_hours_start_hhmm = payload.quiet_hours_start_hhmm
    pref.quiet_hours_end_hhmm = payload.quiet_hours_end_hhmm
    pref.timezone = payload.timezone
    pref.max_batch_cost_usd = payload.max_batch_cost_usd
    pref.max_monthly_cost_usd = payload.max_monthly_cost_usd
    pref.max_search_queries_per_batch = payload.max_search_queries_per_batch
    pref.followup_interval_days = payload.followup_interval_days
    pref.followup_max_count = payload.followup_max_count
    pref.include_standing_career_invitations = payload.include_standing_career_invitations
    pref.campaign_mode = payload.campaign_mode
    if payload.live_consent_granted and not pref.live_consent_granted:
        pref.live_consent_granted = True
        pref.live_consent_version = payload.live_consent_version or "vNext-1"
        pref.live_consent_at = utcnow()
    elif not payload.live_consent_granted:
        pref.live_consent_granted = False
        pref.live_consent_at = None
    pref.save_drafts_in_gmail = payload.save_drafts_in_gmail
    pref.target_goal_count = payload.target_goal_count
    pref.budget_config_json = dict(payload.budget_config or {})
    pref.require_official_or_authorized_send = payload.require_official_or_authorized_send
    db.commit()
    return api_get_preferences(user=user, db=db)
