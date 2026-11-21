"""Offline regressions for frozen attachments, corrections, geography and CV-specific drafts."""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastapi.testclient import TestClient

from app.api import routes_workspace
from app.db.models import Application, Batch, CandidateProfile, CvVersion, JobMatch, JobPosting, SearchPreference, User
from app.services.ai.base import LlmDraftResult, StructuredDraftPayload
from app.services.ai.grounding import generate_grounded_application_draft
from app.services.approval import approve_application_for_send
from app.services.geo import parse_job_location
from app.services.matching import _check_location_and_authorization
from app.services.outbox import _load_verified_attachments
from app.services.pdf_renderer import DOCX_MIME, PDF_MIME, ensure_cv_version_pdf_ready
from app.services.storage import compute_sha256, get_storage_adapter
from app.tasks.pipeline import task_parse_cv_version
from tests.conftest import build_docx_bytes, build_pdf_bytes
from tests.test_security_and_isolation import _register_and_auth


def upload(client, headers, name, raw, document_id=""):
    mime = DOCX_MIME if name.endswith(".docx") else PDF_MIME
    response = client.post("/api/v1/cvs/upload", files={"file": (name, raw, mime)},
                           data={"document_id": document_id}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def version_url(result):
    return f"/api/v1/cvs/{result['document_id']}/versions/{result['version_id']}"


@pytest.fixture
def prepared(client, db_session):
    registration, headers = _register_and_auth(client, "cvowner@example.org")
    uid = registration["user"]["id"]
    raw = build_docx_bytes(["Analyst", "Skills: Excel", "Prepared monthly financial reports in Dubai."])
    result = upload(client, headers, "analyst.docx", raw)
    batch = Batch(user_id=uid)
    db_session.add(batch)
    db_session.flush()
    posting = JobPosting(user_id=uid, batch_id=batch.id, canonical_url_hash="vacancy", content_hash="hash",
                         source_provider="fixture", source_url="https://example.test/analyst",
                         title="Data Analyst", company_name="Example", country_code="AE", location_raw="Dubai, UAE",
                         jd_text="Data Analyst in Dubai. Skills: SQL", detected_language="en")
    db_session.add(posting)
    db_session.flush()
    application = Application(user_id=uid, batch_id=batch.id, job_posting_id=posting.id,
                              selected_cv_version_id=result["version_id"], frozen_cv_filename="analyst.pdf",
                              frozen_cv_sha256=result["sha256"], attachment_pdf_sha256=result["derived_pdf_sha256"],
                              subject="Requested subject", body_text="Old Excel application.")
    db_session.add(application)
    db_session.commit()
    return headers, raw, result, application


def test_snapshot_preview_checks_both_blobs_and_is_tenant_scoped(client, db_session, prepared):
    headers, original, result, application = prepared
    url = f"/api/v1/applications/{application.id}/attachment-preview"
    version = db_session.get(CvVersion, result["version_id"])
    response = client.get(url)
    assert response.status_code == 200 and response.headers["content-type"] == PDF_MIME
    assert response.headers["content-disposition"] == 'inline; filename="analyst.pdf"'
    assert response.content.startswith(b"%PDF") and compute_sha256(response.content) == application.attachment_pdf_sha256
    application.attachment_pdf_sha256 = version.sha256
    application.frozen_cv_filename = version.original_filename
    db_session.commit()
    response = client.get(url)
    assert response.status_code == 409 and response.json()["detail"]["code"] == "ATTACHMENT_INTEGRITY_ERROR"
    application.transport_status = "sent"
    db_session.commit()
    replacement = build_pdf_bytes(["Analyst reporting experience with budgets and forecasts.", "Skills: Excel"])
    upload(client, headers, "analyst.pdf", replacement, result["document_id"])
    assert client.post(f"/api/v1/cvs/{result['document_id']}/archive", headers=headers).status_code == 200
    response = client.get(url)
    assert response.content == original and response.headers["content-type"] == DOCX_MIME
    application.attachment_pdf_sha256 = ""
    db_session.commit()
    assert client.get(url).json()["detail"]["code"] == "ATTACHMENT_SNAPSHOT_MISSING"
    application.attachment_pdf_sha256 = version.derived_pdf_sha256
    db_session.commit()
    storage = get_storage_adapter()
    storage.put_bytes(version.derived_pdf_storage_key, b"changed")
    assert client.get(url).json()["detail"]["code"] == "ATTACHMENT_INTEGRITY_ERROR"
    assert not ensure_cv_version_pdf_ready(version, storage=storage)[0]
    assert storage.get_bytes(version.derived_pdf_storage_key) == b"changed"
    storage.put_bytes(version.storage_key, b"changed")
    assert client.get(version_url(result) + "/download").status_code == 409
    _register_and_auth(client, "othercv@example.org")
    assert client.get(url).status_code == 404


@pytest.mark.parametrize("name,build", [("analyst.pdf", build_pdf_bytes), ("analyst.docx", build_docx_bytes)])
def test_overlapping_uploads_keep_both_originals(client, monkeypatch, name, build):
    _, headers = _register_and_auth(client, "parallel@example.org")
    first = build(["Data analyst portfolio and reporting experience.", "Skills: SQL"])
    initial = upload(client, headers, name, first)
    document_id = initial["document_id"]
    overlap = Barrier(2, timeout=10)
    parse = routes_workspace.parse_cv_bytes

    def simultaneous_parse(data, mime):
        overlap.wait()
        return parse(data, mime)

    monkeypatch.setattr(routes_workspace, "parse_cv_bytes", simultaneous_parse)

    def upload_version(raw):
        with TestClient(client.app) as independent_client:
            independent_client.cookies.update(client.cookies)
            return upload(independent_client, headers, name, raw, document_id)

    payloads = [build(["Data analyst with monthly reporting experience.", f"Skills: {tool}"])
                for tool in ("SQL", "Excel")]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(upload_version, payloads))
    listing = client.get("/api/v1/cvs").json()["cvs"][0]
    assert [v["version_number"] for v in listing["versions"]] == [3, 2, 1]
    assert listing["active_version_id"] == listing["versions"][0]["id"]
    assert len({r["version_id"] for r in [initial, *results]}) == 3
    for result, raw in zip([initial, *results], [first, *payloads], strict=True):
        assert result["sha256"] == compute_sha256(raw)
        assert client.get(version_url(result) + "/download").content == raw
        pdf = client.get(version_url(result) + "/download-pdf")
        assert pdf.content.startswith(b"%PDF") and compute_sha256(pdf.content) == result["derived_pdf_sha256"]


def test_docx_only_rejected_at_switch_approval_and_preview(client, db_session, prepared):
    headers, _, first, application = prepared
    posting = db_session.get(JobPosting, application.job_posting_id)
    posting.attachment_requirements_json = ["DOCX"]
    posting.application_email = application.recipient_email = application.recipient_normalized = "hiring@sample-8658ae.test"
    application.source_verification_tier = "tier_1_official"
    db_session.commit()
    raw = build_docx_bytes(["Skills: SQL", "Designed and maintained reporting databases."])
    second = upload(client, headers, "analyst.docx", raw, document_id=first["document_id"])
    revise = f"/api/v1/drafts/{application.id}/revise"
    preview = f"/api/v1/applications/{application.id}/attachment-preview"
    for version_id in (first["version_id"], second["version_id"]):
        refusal = client.post(revise, headers=headers, json={"selected_cv_version_id": version_id})
        assert refusal.status_code == 400 and "DOCX only" in refusal.json()["detail"]["message"]
    refusal = client.get(preview)
    assert refusal.status_code == 409 and refusal.json()["detail"]["code"] == "ATTACHMENT_NOT_READY"
    ok, reason = approve_application_for_send(db_session, app_rec=application, user=db_session.get(User, application.user_id))
    assert not ok and "DOCX only" in reason
    db_session.refresh(application)
    assert application.selected_cv_version_id == first["version_id"]
    assert application.attachment_pdf_sha256 == first["derived_pdf_sha256"]

    posting.attachment_requirements_json = ["docx", "pdf"]
    db_session.commit()
    assert client.post(revise, headers=headers, json={"selected_cv_version_id": second["version_id"]}).status_code == 200
    db_session.refresh(application)
    assert application.frozen_cv_sha256 == compute_sha256(raw)
    assert application.attachment_pdf_sha256 == second["derived_pdf_sha256"] != application.frozen_cv_sha256
    cv = _load_verified_attachments(db_session, app_rec=application, storage=get_storage_adapter())[0]
    response = client.get(preview)
    assert response.content == cv.data and response.headers["content-type"] == cv.mime_type == PDF_MIME
    assert response.headers["x-attachment-sha256"] == cv.expected_sha256 == application.attachment_pdf_sha256
    assert response.headers["content-disposition"] == f'inline; filename="{cv.filename}"'
    assert client.get(version_url(second) + "/download").content == raw


def test_correction_retains_original_and_ids_across_reparses(client, prepared):
    headers, original, result, _ = prepared
    url = version_url(result)
    fact = client.get(url).json()["version"]["facts"][0]
    endpoint = f"{url}/facts/{fact['id']}/corrections"
    correction = {"statement": "Basic spreadsheet training only", "reason": "Correcting the proficiency level"}
    response = client.post(endpoint, json=correction, headers=headers)
    assert response.status_code == 201
    change = response.json()["correction"]
    assert change["source_sha256"] == compute_sha256(original) and change["confirmed_by"] and change["confirmed_at"]
    assert client.post(endpoint, json=correction, headers=headers).status_code == 409
    assert client.post(url + "/reparse", headers=headers).status_code == 200
    assert task_parse_cv_version.run(result["version_id"]) == "parsed"
    version = client.get(url).json()["version"]
    by_id = {f["id"]: f for f in version["facts"]}
    assert version["corrections"] == [change]
    assert by_id[fact["id"]]["confidence"] == "superseded"
    assert by_id[fact["id"]]["evidence_quote"] == fact["evidence_quote"]
    assert by_id[change["confirmed_fact_id"]]["value"] == correction["statement"]
    assert client.get(url + "/download").content == original
    assert compute_sha256(client.get(url + "/download-pdf").content) == result["derived_pdf_sha256"]
    _, other = _register_and_auth(client, "othercorrector@example.org")
    assert client.post(endpoint, json=correction, headers=other).status_code == 404


def test_failed_docx_preserves_original(client, monkeypatch):
    _, headers = _register_and_auth(client, "unicode@example.org")
    monkeypatch.setattr("app.services.pdf_renderer._try_libreoffice_docx_to_pdf", lambda _: None)
    raw = build_docx_bytes(["السيرة الذاتية", "خبرة في تحليل البيانات وإعداد تقارير الأعمال الشهرية"])
    result = upload(client, headers, "original.docx", raw)
    assert result["parse_status"] == "parsed" and result["pdf_conversion_status"] == "failed"
    assert result["pdf_conversion_error"] and not result["derived_pdf_sha256"]
    assert client.get(version_url(result) + "/download").content == raw
    assert client.get(version_url(result) + "/download-pdf").json()["detail"]["code"] == "ATTACHMENT_NOT_READY"


def test_invalid_converter_output_is_reported(client, monkeypatch):
    _, headers = _register_and_auth(client, "converter@example.org")
    monkeypatch.setattr("app.services.pdf_renderer._try_libreoffice_docx_to_pdf", lambda _: b"broken")
    raw = build_docx_bytes(["Analyst", "Skills: Excel", "Prepared quarterly business reports."])
    result = upload(client, headers, "cv.docx", raw)
    assert result["parse_status"] == "parsed" and result["pdf_conversion_status"] == "failed"
    assert "PDF" in result["pdf_conversion_error"] and not result["derived_pdf_sha256"]
    assert client.get(version_url(result) + "/download").content == raw


@pytest.mark.parametrize("place,scope,policy,expected,review", [
    ("Dubai, UAE", "onsite", "skip", None, False),
    ("Abu Dhabi, UAE", "hybrid", "skip", None, False),
    ("Sharjah, UAE", "onsite", "skip", "OUTSIDE_TARGET_CITY", False),
    ("Sharjah, UAE", "hybrid", "require_review", "OUTSIDE_TARGET_CITY", False),
    ("UAE", "hybrid", "skip", "JOB_LOCATION_UNKNOWN", False),
    ("UAE", "onsite", "allow_if_remote_worldwide", "JOB_LOCATION_UNKNOWN", False),
    ("UAE", "onsite", "require_review", None, True),
    ("Sharjah, UAE", "remote_country", "skip", None, False),
    ("Jeddah, Saudi Arabia", "onsite", "skip", None, False),
])
def test_city_targets(place, scope, policy, expected, review):
    pref = SearchPreference(target_locations_json=[{"country_code": "AE", "city": city} for city in ["Dubai", "Abu Dhabi"]]
                            + [{"country_code": "SA", "city": ""}], unknown_location_policy=policy)
    profile = CandidateProfile(work_authorizations_json=["AE", "SA"])
    # An explicit source city takes precedence over a stale structured hint.
    posting = JobPosting(location_raw=place, remote_scope=scope, city="Dubai" if place.startswith("Sharjah") else "")
    reasons = []
    ok, _, codes = _check_location_and_authorization(posting=posting, parsed_loc=parse_job_location(place),
                                                     pref=pref, profile=profile, review_reasons=reasons)
    assert ok == (expected is None)
    assert codes == ([expected] if expected else []) and bool(reasons) == review


def test_switch_and_regenerate_use_selected_evidence_offline(client, db_session, prepared, monkeypatch):
    headers, _, first, application = prepared
    def forbid_provider(*args, **kwargs):
        pytest.fail("Unbudgeted provider lookup")
    monkeypatch.setattr("app.services.ai.grounding.get_configured_llm_provider", forbid_provider)
    raw = build_pdf_bytes(["Data Analyst", "Skills: SQL", "Built monthly customer reporting dashboards."])
    second = upload(client, headers, "data.pdf", raw, document_id=first["document_id"])
    old_preview = client.get(f"/api/v1/applications/{application.id}/attachment-preview")
    assert compute_sha256(old_preview.content) == first["derived_pdf_sha256"]
    url = f"/api/v1/drafts/{application.id}"
    response = client.post(url + "/revise", headers=headers,
                           json={"selected_cv_version_id": second["version_id"], "body_text": application.body_text})
    assert response.status_code == 200, response.text
    db_session.refresh(application)
    match = db_session.get(JobMatch, application.job_match_id)
    assert match.cv_version_id == second["version_id"] and "SQL" in match.verified_skills_json
    assert application.subject == "Requested subject" and match.why_this_cv
    facts = client.get(version_url(second)).json()["version"]["facts"]
    assert application.evidence_fact_ids_json and set(application.evidence_fact_ids_json) <= {f["id"] for f in facts}
    assert "SQL" in application.body_text and "Excel" not in application.body_text
    assert client.get(f"/api/v1/applications/{application.id}/attachment-preview").content == raw
    assert client.post(url + "/regenerate", headers=headers).json()["draft"]["subject"] == "Requested subject"
    posting = db_session.get(JobPosting, application.job_posting_id)
    posting.subject_instruction = "Employer ref 4711"
    db_session.commit()
    assert client.post(url + "/regenerate", headers=headers).json()["draft"]["subject"] == "Employer ref 4711"


@pytest.mark.parametrize("fabricate", [False, True])
def test_provider_citations_only_authorize_exact_quotes(prepared, db_session, fabricate):
    _, _, result, application = prepared
    facts = db_session.get(CvVersion, result["version_id"]).facts
    class Provider:
        def generate_structured_draft(self, system, prompt):
            data = json.loads(prompt)
            quote = data["facts"][0]
            return LlmDraftResult(provider="fixture", model="fixture", payload=StructuredDraftPayload(
                subject=data["subject"], cited_fact_ids=[quote["id"]],
                body_paragraphs=["I led NASA for 20 years." if fabricate else quote["quote"]],
            ))
    profile = db_session.query(CandidateProfile).filter_by(user_id=application.user_id).one()
    outputs = []
    for nationality in ("AE", "FR"):
        profile.nationalities_json = [nationality]
        outcome = generate_grounded_application_draft(
            posting=db_session.get(JobPosting, application.job_posting_id), profile=profile,
            pref=SearchPreference(), facts=facts, language="en", llm_provider=Provider(), supporting_fact_ids=[],
        )
        assert outcome.provider_used == ("template_fallback" if fabricate else "fixture")
        assert "NASA" not in outcome.body_text and outcome.evidence_fact_ids
        outputs.append(outcome.body_text)
    assert outputs[0] == outputs[1]

