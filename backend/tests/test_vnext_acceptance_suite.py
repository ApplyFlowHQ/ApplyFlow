"""Comprehensive ApplyFlow Global vNext Acceptance Test Suite covering DISC-01..10, ELIG-01..04, CV-01..03, GMAIL-01..08, UI-01..03, DATA-01..03, SEC-01..02, and EXPORT-01."""

from __future__ import annotations

import io
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import docx
import httpx
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from pypdf import PdfReader

from app.core.config import get_settings
from app.core.encryption import encrypt_json
from app.db.base import utcnow
from app.db.models import (
    Application,
    Batch,
    CandidateProfile,
    CvDocument,
    CvExtractedFact,
    CvVersion,
    GmailSentRecord,
    InstallationConfig,
    JobMatch,
    MailboxConnection,
    RecipientHistory,
    SearchPreference,
    User,
)
from app.services.batch_runner import execute_batch_pipeline
from app.services.discovery.base import DiscoveredCandidate, SearchProviderOutcome
from app.services.discovery.query_builder import build_email_application_discovery_plan
from app.services.mailbox.base import HandoffOutcome, OutboundEmailMessage, SendResult
from app.services.mailbox.gmail import (
    GMAIL_COMPOSE_SCOPE,
    GMAIL_READONLY_SCOPE,
    GMAIL_SEND_SCOPE,
    build_gmail_oauth_url,
    create_gmail_draft,
    get_mailbox_dedup_coverage,
    sync_gmail_sent_history,
)
from app.services.storage import compute_sha256, get_storage_adapter
from tests.test_security_and_isolation import _register_and_auth


def _build_docx_bytes(lines: list[str]) -> bytes:
    d = docx.Document()
    for i, ln in enumerate(lines):
        if i == 0:
            d.add_heading(ln, level=1)
        else:
            d.add_paragraph(ln)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def _seed_vnext_user(db_session, *, email: str = "vnext@example.org", with_docx_cv: bool = False):
    user = User(email=email, email_normalized=email.lower(), timezone="Asia/Dubai", full_name="Tariq Al-Mansoor")
    db_session.add(user)
    db_session.flush()

    profile = CandidateProfile(
        user_id=user.id,
        full_name="Tariq Al-Mansoor",
        contact_email=email,
        residence_country_code="AE",
        residence_city="Dubai",
        work_authorizations_json=["AE", "SA"],
        nationalities_json=["JO"],
        willing_to_relocate=True,
        relocation_targets_json=["AE", "SA"],
        confirmed_languages_json=[{"language": "en", "level": "fluent"}, {"language": "ar", "level": "native"}],
        signature_text="Best regards,\nTariq Al-Mansoor",
        confirmed_at=utcnow(),
    )
    db_session.add(profile)

    pref = SearchPreference(
        user_id=user.id,
        target_titles_json=["CAFM Administrator", "Data Analyst"],
        role_families_json=["CAFM & CMMS", "Analytics"],
        industries_json=["Facilities Management"],
        target_locations_json=[{"country_code": "AE", "country_name": "United Arab Emirates", "city": "Dubai"}],
        search_languages_json=["en"],
        freshness_days=30,
        min_match_score=45,
        batch_size=10,
        target_goal_count=5,
        daily_recipient_limit=15,
        unknown_salary_policy="allow_neutral",
        unknown_dates_policy="allow_neutral",
        unknown_location_policy="skip",
        unknown_eligibility_policy="skip",
        campaign_mode="dry_run",
        live_consent_granted=False,
    )
    db_session.add(pref)

    doc = CvDocument(user_id=user.id, variant_label="CAFM Analytics")
    db_session.add(doc)
    db_session.flush()

    storage = get_storage_adapter()
    if with_docx_cv:
        raw_bytes = _build_docx_bytes(
            [
                "Tariq Al-Mansoor",
                "CAFM Administrator and Data Analyst - Dubai, UAE (2020-01 to 2025-10)",
                "5+ years of IBM Maximo, Archibus, Power BI, SQL, PPM planning, and SLA/KPI dashboards.",
                "Skills: CAFM, IBM Maximo, SQL, Power BI, Facilities Management, Excel",
                "Languages: English (Fluent), Arabic (Native)",
            ]
        )
        mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        fname = "Tariq_CAFM_CV.docx"
    else:
        from tests.conftest import build_pdf_bytes

        raw_bytes = build_pdf_bytes(
            [
                "Tariq Al-Mansoor - CAFM Administrator & Data Analyst",
                "2020-01 to 2025-10 CAFM Administrator managing IBM Maximo, SQL, Power BI in Dubai, UAE",
                "Skills: CAFM, IBM Maximo, SQL, Power BI, Facilities Management",
            ]
        )
        mime = "application/pdf"
        fname = "Tariq_CAFM_CV.pdf"

    sha = compute_sha256(raw_bytes)
    ver_id = f"ver-{user.id[:8]}"
    skey = f"users/{user.id}/cvs/{doc.id}/{ver_id}_{fname}"
    storage.put_bytes(skey, raw_bytes, content_type=mime)

    ver = CvVersion(
        id=ver_id,
        cv_document_id=doc.id,
        user_id=user.id,
        version_number=1,
        original_filename=fname,
        mime_type=mime,
        size_bytes=len(raw_bytes),
        page_count=1,
        sha256=sha,
        storage_key=skey,
        parse_status="parsed",
        ocr_status="not_needed",
        detected_language="en",
        extracted_text="Tariq Al-Mansoor CAFM Administrator Data Analyst IBM Maximo SQL Power BI Dubai UAE Facilities Management",
        total_experience_months=60,
    )
    db_session.add(ver)
    db_session.flush()
    doc.active_version_id = ver.id

    facts = [
        CvExtractedFact(
            cv_version_id=ver.id,
            user_id=user.id,
            category="experience",
            title="CAFM Administrator",
            value="5 years CAFM Administration IBM Maximo SQL Power BI",
            evidence_quote="2020-01 to 2025-10 CAFM Administrator managing IBM Maximo, SQL, Power BI",
            confidence="evidenced",
        ),
        CvExtractedFact(
            cv_version_id=ver.id,
            user_id=user.id,
            category="skill",
            title="IBM Maximo",
            value="IBM Maximo",
            evidence_quote="IBM Maximo CAFM and Power BI reporting",
            confidence="evidenced",
        ),
        CvExtractedFact(
            cv_version_id=ver.id,
            user_id=user.id,
            category="skill",
            title="Power BI",
            value="Power BI",
            evidence_quote="Power BI and SQL dashboards",
            confidence="evidenced",
        ),
        CvExtractedFact(
            cv_version_id=ver.id,
            user_id=user.id,
            category="skill",
            title="SQL",
            value="SQL",
            evidence_quote="SQL reporting and CAFM database administration",
            confidence="evidenced",
        ),
    ]
    db_session.add_all(facts)
    db_session.commit()
    return user, profile, pref, doc, ver, facts, raw_bytes, sha


# --- DISC-01..10: Staged Research, Independent Budgets, Multi-Region, Corroboration & Section 10.1 ---


class _MockStagedProvider:
    name = "mock_search"

    def __init__(self, handler):
        self.handler = handler
        self.queries_seen: list[str] = []

    async def search(self, query: str, *, freshness_days: int = 14, max_results: int = 10, country: str = "", page: int = 0):
        self.queries_seen.append(query)
        return self.handler(query)


def test_disc01_disc04_disc06_disc08_disc09_disc10_staged_budgets_and_corroboration(db_session):
    user, profile, pref, _, _, _, _, _ = _seed_vnext_user(db_session)
    now = utcnow()
    pref.max_search_queries_per_batch = 1
    pref.target_goal_count = 6
    pref.budget_config_json = {
        "max_search_queries": 1,
        "max_fetched_pages": 6,
        "per_domain_page_cap": 2,
        "max_employer_followup_queries": 1,
        "max_email_corroboration_queries": 1,
    }
    db_session.commit()

    def _search_handler(query: str) -> SearchProviderOutcome:
        if "site:targetco.test" in query:
            # Stage 4 corroboration hit confirms role & careers@targetco.test on official domain!
            return SearchProviderOutcome(
                provider_name="mock_search",
                query=query,
                estimated_cost_usd=0.005,
                candidates=[
                    DiscoveredCandidate(
                        source_provider="mock_search",
                        source_url="https://targetco.test/careers/cafm-admin",
                        source_type="job_post",
                        title="TargetCo Careers — CAFM Administrator",
                        company_name="TargetCo FM",
                        company_domain="targetco.test",
                        jd_text="Official careers confirmation: apply for CAFM Administrator in Dubai via careers@targetco.test.",
                        location_raw="Dubai, UAE",
                        retrieved_at=now,
                    )
                ],
            )
        if "Board Portal Co" in query and "careers" in query:
            # Stage 3 follow-up hit finds official page with email invite!
            return SearchProviderOutcome(
                provider_name="mock_search",
                query=query,
                estimated_cost_usd=0.005,
                candidates=[
                    DiscoveredCandidate(
                        source_provider="mock_search",
                        source_url="https://boardportalco.test/careers/data-analyst",
                        source_type="job_post",
                        title="Data Analyst — Board Portal Co",
                        company_name="Board Portal Co",
                        company_domain="boardportalco.test",
                        jd_text="Data Analyst in Dubai UAE with SQL and Power BI. Send your CV to talent@boardportalco.test",
                        location_raw="Dubai, UAE",
                        retrieved_at=now,
                        posted_at=now - timedelta(days=1),
                    )
                ],
            )
        # Stage 1 discovery returns: noisy repeated-domain items (>cap 2), 2 syndicated boards with REQ-901, 1 social claim without official link, 1 portal-only
        return SearchProviderOutcome(
            provider_name="mock_search",
            query=query,
            estimated_cost_usd=0.008,
            candidates=[
                DiscoveredCandidate(
                    source_provider="mock_search",
                    source_url="https://linkedin.com/posts/targetco-cafm-req901",
                    source_type="job_post",
                    title="CAFM Administrator [REQ-901] — TargetCo FM",
                    company_name="TargetCo FM",
                    company_domain="linkedin.com",
                    jd_text="TargetCo FM is hiring CAFM Administrator (IBM Maximo, SQL, Power BI) in Dubai UAE. Send your CV to careers@targetco.test.",
                    location_raw="Dubai, UAE",
                    location_confidence="explicit",
                    retrieved_at=now,
                    posted_at=now - timedelta(days=1),
                ),
                DiscoveredCandidate(
                    source_provider="mock_search",
                    source_url="https://board2.example.org/jobs/cafm-req901",
                    source_type="job_post",
                    title="CAFM Administrator (Ref REQ-901) - TargetCo FM",
                    company_name="TargetCo FM",
                    company_domain="board2.example.org",
                    jd_text="Syndicated copy of TargetCo FM CAFM Administrator (REQ-901) in Dubai UAE. Send your CV to careers@targetco.test.",
                    location_raw="Dubai, UAE",
                    location_confidence="explicit",
                    retrieved_at=now,
                    posted_at=now - timedelta(days=1),
                ),
                DiscoveredCandidate(
                    source_provider="mock_search",
                    source_url="https://boardportalco.test/apply-portal",
                    source_type="job_post",
                    title="Data Analyst",
                    company_name="Board Portal Co",
                    company_domain="boardportalco.test",
                    jd_text="Board Portal Co hiring Data Analyst in Dubai UAE. Complete the online application on company site.",
                    location_raw="Dubai, UAE",
                    location_confidence="explicit",
                    portal_apply_detected=True,
                    retrieved_at=now,
                    posted_at=now - timedelta(days=1),
                    validated_employer="Board Portal Co",
                    role_extracted="Data Analyst",
                ),
                DiscoveredCandidate(
                    source_provider="mock_search",
                    source_url="https://noiseboard.test/a",
                    source_type="job_post",
                    title="Noisy Listing A",
                    company_name="Noise",
                    company_domain="noiseboard.test",
                    jd_text="Apply online only via portal",
                    location_raw="Dubai, UAE",
                    retrieved_at=now,
                ),
                DiscoveredCandidate(
                    source_provider="mock_search",
                    source_url="https://noiseboard.test/b",
                    source_type="job_post",
                    title="Noisy Listing B",
                    company_name="Noise",
                    company_domain="noiseboard.test",
                    jd_text="Apply online only via portal",
                    location_raw="Dubai, UAE",
                    retrieved_at=now,
                ),
                DiscoveredCandidate(
                    source_provider="mock_search",
                    source_url="https://noiseboard.test/c",
                    source_type="job_post",
                    title="Noisy Listing C",
                    company_name="Noise",
                    company_domain="noiseboard.test",
                    jd_text="Apply online only via portal",
                    location_raw="Dubai, UAE",
                    retrieved_at=now,
                ),
            ],
        )

    async def _http_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/robots.txt"):
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if "boardportalco.test/careers/data-analyst" in url:
            return httpx.Response(
                200,
                text="<html><body><h1>Data Analyst - Board Portal Co</h1><p>Dubai, UAE. SQL and Power BI required. Send your CV to talent@boardportalco.test.</p></body></html>",
                headers={"content-type": "text/html"},
            )
        return httpx.Response(404, text="Not found")

    provider = _MockStagedProvider(_search_handler)
    batch = Batch(user_id=user.id, mode="dry_run", run_mode="dry_run", target_goal_count=6)
    db_session.add(batch)
    db_session.commit()

    execute_batch_pipeline(
        db_session,
        batch=batch,
        search_providers_override=[provider],
        transport_override=httpx.MockTransport(_http_handler),
        skip_network_dns=True,
    )

    m = batch.metrics_json
    # DISC-01: Stage 1 capped at 1 query + per-domain cap triggered, yet Stage 3 & Stage 4 still executed!
    assert m["search_queries_executed"] == 1
    assert m["followup_queries_executed"] >= 1
    assert m["corroboration_queries_executed"] >= 1
    assert m["pages_skipped_domain_cap"] >= 1
    # DISC-04: REQ-901 syndicated postings collapsed to 1 normalized vacancy + duplicate tracked
    assert m["duplicates"] >= 1
    # DISC-06: Stage 4 corroborated the LinkedIn claim -> tier_2_authorized! Stage 3 discovered official tier_1_official!
    assert (m["vacancies_tier_1_official"] + m["vacancies_tier_2_authorized"]) >= 1
    # DISC-09 / DISC-10: Goal of 6 not met -> explicit shortfall explanation populated cleanly
    assert m["goal_met"] is False
    assert "Target goal 6:" in batch.goal_shortfall_reason


def test_disc02_multi_region_multi_role_dynamic_query_plan():
    plan = build_email_application_discovery_plan(
        target_titles=["CAFM Administrator", "Data Analyst", "BI Developer"],
        role_families=["Facilities Management", "Analytics"],
        target_locations=[
            {"country_code": "AE", "country_name": "United Arab Emirates", "city": "Dubai"},
            {"country_code": "SA", "country_name": "Saudi Arabia", "city": "Riyadh"},
        ],
        search_languages=["en", "ar"],
        include_standing_invitations=False,
        rotation_offset=0,
        max_queries=8,
    )
    countries_covered = {p.country_code for p in plan}
    titles_covered = {p.title_term for p in plan}
    assert {"AE", "SA"}.issubset(countries_covered)
    assert len(titles_covered) >= 3


def test_disc06_third_party_claim_held_in_review_and_never_auto_sent(db_session, monkeypatch):
    monkeypatch.setenv("LIVE_SENDING_ENABLED", "true")
    get_settings.cache_clear()
    user, profile, pref, _, _, _, _, _ = _seed_vnext_user(db_session)
    pref.campaign_mode = "autopilot"
    pref.live_consent_granted = True
    pref.live_consent_at = utcnow()
    conn = MailboxConnection(
        user_id=user.id,
        provider="smtp_imap",
        label="Work Mail",
        sender_email="me@work.test",
        smtp_host="smtp.work.test",
        status="connected",
        is_primary=True,
    )
    db_session.add(conn)
    db_session.commit()

    now = utcnow()
    uncorroborated_social = DiscoveredCandidate(
        source_provider="tavily",
        source_url="https://facebook.com/groups/dubaijobs/posts/9911",
        source_type="job_post",
        title="CAFM Administrator — Mystery Facilities LLC",
        company_name="Mystery Facilities LLC",
        company_domain="facebook.com",
        jd_text="CAFM Administrator needed in Dubai UAE (IBM Maximo, SQL). Send your CV to fictional-49f95dd3@gmail.com.",
        location_raw="Dubai, UAE",
        location_confidence="explicit",
        retrieved_at=now,
        posted_at=now - timedelta(days=1),
        application_email="fictional-49f95dd3@gmail.com",
        evidence_quote="Send your CV to fictional-49f95dd3@gmail.com.",
        validated_employer="Mystery Facilities LLC",
        role_extracted="CAFM Administrator",
    )

    batch = Batch(user_id=user.id, mode="live", run_mode="autopilot")
    db_session.add(batch)
    db_session.commit()

    execute_batch_pipeline(
        db_session,
        batch=batch,
        candidate_overrides=[uncorroborated_social],
        skip_network_dns=True,
        auto_process_outbox_sync=True,
    )

    app_rec = db_session.query(Application).filter(Application.batch_id == batch.id).one()
    assert app_rec.source_verification_tier == "tier_3_third_party_claim"
    assert app_rec.approval_state == "needs_human_review"
    assert app_rec.transport_status == "draft"
    assert batch.metrics_json["sent_accepted"] == 0
    assert batch.metrics_json["needs_human_review"] == 1


# --- ELIG-01..04 & CV-01..03: DOCX->PDF Conversion, Unknown Policies, Missing Docs, Role Mismatch ---


def test_cv01_cv02_elig02_elig03_docx_pdf_and_review_policies(client: TestClient, db_session, monkeypatch):
    _, headers = _register_and_auth(client, "cvdocx@example.org")
    # Grant live auth/profile on this user
    user = db_session.query(User).filter(User.email_normalized == "cvdocx@example.org").one()
    user_id = user.id
    client.put(
        "/api/v1/profile",
        json={
            "full_name": "Tariq Al-Mansoor",
            "headline": "CAFM Administrator & Data Analyst",
            "contact_email": "cvdocx@example.org",
            "residence_country_code": "AE",
            "residence_city": "Dubai",
            "work_authorizations": ["AE"],
            "nationalities": ["JO"],
            "confirmed_languages": [{"language": "en", "level": "fluent"}],
            "confirm_profile": True,
        },
        headers=headers,
    )
    client.put(
        "/api/v1/preferences",
        json={
            "target_titles": ["CAFM Administrator", "Data Analyst"],
            "target_locations": [{"country_code": "AE", "country_name": "United Arab Emirates", "city": "Dubai"}],
            "min_salary_amount": 5000,
            "min_salary_currency": "USD",
            "unknown_salary_policy": "require_review",
            "min_match_score": 40,
            "campaign_mode": "approval_required",
        },
        headers=headers,
    )

    # CV-01: Upload genuine DOCX CV -> original SHA unchanged, derived PDF generated & readable by pypdf!
    docx_bytes = _build_docx_bytes(
        [
            "Tariq Al-Mansoor",
            "CAFM Administrator | Data Analyst — Dubai, UAE",
            "Experience: 2020-01 to 2025-10 CAFM Administrator managing IBM Maximo, Archibus, Power BI, SQL.",
            "Skills: CAFM, IBM Maximo, SQL, Power BI, Excel, Facilities Management",
        ]
    )
    orig_sha = compute_sha256(docx_bytes)
    up = client.post(
        "/api/v1/cvs/upload",
        data={"variant_label": "CAFM DOCX"},
        files={"file": ("Tariq_CV.docx", docx_bytes, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
        headers=headers,
    )
    assert up.status_code == 201
    up_data = up.json()
    assert up_data["sha256"] == orig_sha
    assert up_data["pdf_conversion_status"] == "ready"
    assert up_data["derived_pdf_sha256"] != orig_sha

    dl_orig = client.get(f"/api/v1/cvs/{up_data['document_id']}/versions/{up_data['version_id']}/download", headers=headers)
    assert dl_orig.status_code == 200 and dl_orig.content == docx_bytes

    dl_pdf = client.get(f"/api/v1/cvs/{up_data['document_id']}/versions/{up_data['version_id']}/download-pdf", headers=headers)
    assert dl_pdf.status_code == 200
    assert dl_pdf.content.startswith(b"%PDF")
    reader = PdfReader(io.BytesIO(dl_pdf.content))
    pdf_txt = "\n".join(p.extract_text() or "" for p in reader.pages)
    assert "IBM Maximo" in pdf_txt and "Power BI" in pdf_txt

    # Now evaluate: (a) PDF-required matching job with unstated salary & missing portfolio -> PASSES PDF gate, held for human review (ELIG-02 + ELIG-03)!
    # (b) Completely mismatched Nurse role -> ROLE_MISMATCH rejected (CV-02)!
    now = utcnow()
    pdf_req_cand = DiscoveredCandidate(
        source_provider="career_page",
        source_url="https://acme-fm.test/careers/cafm-pdf",
        source_type="job_post",
        title="CAFM Administrator",
        company_name="Acme FM",
        company_domain="acme-fm.test",
        jd_text="CAFM Administrator in Dubai UAE. Skills: IBM Maximo, SQL, Power BI. Attach your CV in PDF only and include your portfolio to hr@acme-fm.test.",
        location_raw="Dubai, UAE",
        location_confidence="explicit",
        retrieved_at=now,
        posted_at=now - timedelta(days=1),
        application_email="hr@acme-fm.test",
        attachment_requirements=["pdf", "portfolio"],
        evidence_quote="Attach your CV in PDF only and include your portfolio to hr@acme-fm.test.",
        validated_employer="Acme FM",
        role_extracted="CAFM Administrator",
    )
    nurse_cand = DiscoveredCandidate(
        source_provider="career_page",
        source_url="https://clinic.test/careers/nurse",
        source_type="job_post",
        title="Pediatric Registered Nurse",
        company_name="Care Clinic",
        company_domain="clinic.test",
        jd_text="Pediatric Registered Nurse in Dubai UAE with ICU triage certification. Send your CV to jobs@clinic.test.",
        location_raw="Dubai, UAE",
        location_confidence="explicit",
        retrieved_at=now,
        posted_at=now - timedelta(days=1),
        application_email="jobs@clinic.test",
        evidence_quote="Send your CV to jobs@clinic.test.",
        validated_employer="Care Clinic",
        role_extracted="Pediatric Registered Nurse",
    )

    batch = Batch(user_id=user_id, mode="live", run_mode="approval_required")
    db_session.add(batch)
    db_session.commit()
    execute_batch_pipeline(
        db_session,
        batch=batch,
        candidate_overrides=[pdf_req_cand, nurse_cand],
        skip_network_dns=True,
    )

    apps = db_session.query(Application).filter(Application.batch_id == batch.id).all()
    by_email = {a.recipient_email: a for a in apps}

    app_fm = by_email["hr@acme-fm.test"]
    assert app_fm.transport_status == "draft"
    assert app_fm.approval_state == "needs_human_review"
    assert app_fm.attachment_pdf_sha256 == up_data["derived_pdf_sha256"]
    assert app_fm.frozen_cv_filename.endswith(".pdf")
    review_blob = " ".join(app_fm.review_reasons_json or []).lower()
    assert "salary" in review_blob and "portfolio" in review_blob

    app_nurse = by_email["jobs@clinic.test"]
    assert app_nurse.transport_status == "skipped"
    assert app_nurse.primary_skip_code in ("ROLE_MISMATCH", "QUALIFICATION_MISMATCH", "FIT_THRESHOLD")


# --- UI-01..03, GMAIL-01..08, SEC-01..02, DATA-01..03, EXPORT-01 ---


def test_gmail01_to_gmail08_scopes_sent_sync_coverage_and_compose_draft(db_session):
    user, profile, pref, _, _, _, _, _ = _seed_vnext_user(db_session)
    db_session.add(
        InstallationConfig(
            id="default",
            google_oauth_client_id="client-id",
            encrypted_secrets_json=encrypt_json({"google_oauth_client_secret": "client-sec"}),
        )
    )
    db_session.commit()

    # GMAIL-01 & GMAIL-05: Send-only excludes gmail.readonly & gmail.compose; opt-in compose includes gmail.compose
    url_send_only = build_gmail_oauth_url(db_session, user_id=user.id, include_inbox_readonly=False, include_compose_drafts=False)
    qs_send = parse_qs(urlparse(url_send_only).query)["scope"][0].split()
    assert GMAIL_SEND_SCOPE in qs_send
    assert GMAIL_READONLY_SCOPE not in qs_send
    assert GMAIL_COMPOSE_SCOPE not in qs_send

    url_compose = build_gmail_oauth_url(db_session, user_id=user.id, include_inbox_readonly=True, include_compose_drafts=True)
    qs_comp = parse_qs(urlparse(url_compose).query)["scope"][0].split()
    assert {GMAIL_SEND_SCOPE, GMAIL_READONLY_SCOPE, GMAIL_COMPOSE_SCOPE}.issubset(set(qs_comp))

    # GMAIL-02: Connection without gmail.readonly clearly reports "applyflow_ledger_only" warning
    conn_limited = MailboxConnection(
        user_id=user.id,
        provider="gmail_oauth",
        sender_email="fictional-04f8996d@gmail.com",
        granted_scopes_json=[GMAIL_SEND_SCOPE],
        encrypted_credentials=encrypt_json({"access_token": "tok", "expires_at": (utcnow() + timedelta(hours=1)).isoformat()}),
        is_primary=False,
    )
    db_session.add(conn_limited)
    db_session.flush()
    cov_limited = get_mailbox_dedup_coverage(db_session, conn=conn_limited)
    assert cov_limited["coverage_mode"] == "applyflow_ledger_only"
    assert cov_limited["warning_banner"] == "Duplicate checking currently covers ApplyFlow records only."

    # GMAIL-03 & GMAIL-04 & GMAIL-06: Paginated `in:sent` sync + conservative classification + dedup enforcement + incremental cursor
    conn_full = MailboxConnection(
        user_id=user.id,
        provider="gmail_oauth",
        sender_email="fictional-04f8996d@gmail.com",
        granted_scopes_json=[GMAIL_SEND_SCOPE, GMAIL_READONLY_SCOPE, GMAIL_COMPOSE_SCOPE],
        encrypted_credentials=encrypt_json({"access_token": "tok", "expires_at": (utcnow() + timedelta(hours=1)).isoformat()}),
        save_drafts_in_gmail=True,
        is_primary=True,
    )
    db_session.add(conn_full)
    db_session.commit()

    def _gmail_mock_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json={"historyId": "8999", "emailAddress": conn_full.sender_email})
        if request.method == "POST" and "/drafts" in url:
            return httpx.Response(200, json={"id": "draft-xyz-789"})
        if "/messages?" in url:
            pt = parse_qs(urlparse(url).query).get("pageToken", [""])[0]
            if not pt:
                return httpx.Response(200, json={"messages": [{"id": "m-job-1"}], "nextPageToken": "page2"})
            return httpx.Response(200, json={"messages": [{"id": "m-personal-2"}]})
        if "/messages/m-job-1" in url:
            return httpx.Response(
                200,
                json={
                    "id": "m-job-1",
                    "threadId": "thr-1",
                    "historyId": "9001",
                    "payload": {
                        "headers": [
                            {"name": "To", "value": "careers@priorco.test"},
                            {"name": "Subject", "value": "Application for CAFM Administrator"},
                            {"name": "Date", "value": "Mon, 10 Nov 2026 10:00:00 +0000"},
                            {"name": "Message-ID", "value": "<sent1@sample-3d4b97.test>"},
                        ]
                    },
                },
            )
        if "/messages/m-personal-2" in url:
            return httpx.Response(
                200,
                json={
                    "id": "m-personal-2",
                    "threadId": "thr-2",
                    "historyId": "9002",
                    "payload": {
                        "headers": [
                            {"name": "To", "value": "fictional-cde48537@gmail.com"},
                            {"name": "Subject", "value": "Lunch tomorrow?"},
                            {"name": "Date", "value": "Tue, 11 Nov 2026 12:00:00 +0000"},
                        ]
                    },
                },
            )
        if "/history" in url:
            return httpx.Response(
                200,
                json={
                    "historyId": "9010",
                    "history": [{"messagesAdded": [{"message": {"id": "m-job-3"}}]}],
                },
            )
        if "/messages/m-job-3" in url:
            return httpx.Response(
                200,
                json={
                    "id": "m-job-3",
                    "threadId": "thr-3",
                    "historyId": "9010",
                    "payload": {
                        "headers": [
                            {"name": "To", "value": "recruitment@secondprior.test"},
                            {"name": "Subject", "value": "CV attached"},
                            {"name": "Date", "value": "Wed, 12 Nov 2026 09:00:00 +0000"},
                        ]
                    },
                },
            )
        return httpx.Response(404, json={})

    transport = httpx.MockTransport(_gmail_mock_handler)
    res1 = sync_gmail_sent_history(db_session, conn=conn_full, http_transport=transport)
    assert res1["coverage_mode"] == "applyflow_ledger_plus_gmail_sent", res1
    assert res1["total_sent_records"] == 2, res1
    assert conn_full.sent_sync_cursor == "8999"

    # Exact external addresses are prior contacts; classification separates job and personal correspondence.
    rec_job = db_session.query(GmailSentRecord).filter(GmailSentRecord.provider_message_id == "m-job-1").one()
    rec_pers = db_session.query(GmailSentRecord).filter(GmailSentRecord.provider_message_id == "m-personal-2").one()
    assert rec_job.classification == "job_application" and rec_job.is_suppressed_recipient is True
    assert rec_pers.classification == "ambiguous" and rec_pers.is_suppressed_recipient is True

    # Incremental history sync picks up m-job-3 cleanly
    res2 = sync_gmail_sent_history(db_session, conn=conn_full, http_transport=transport)
    assert res2["total_sent_records"] == 3
    assert conn_full.sent_sync_cursor == "9010"

    # Discovery batch skips careers@priorco.test due to synced Gmail Sent history!
    now = utcnow()
    dup_cand = DiscoveredCandidate(
        source_provider="career_page",
        source_url="https://priorco.test/careers/cafm",
        source_type="job_post",
        title="CAFM Administrator",
        company_name="PriorCo",
        company_domain="priorco.test",
        jd_text="CAFM Administrator in Dubai UAE (IBM Maximo, SQL, Power BI). Send CV to careers@priorco.test.",
        location_raw="Dubai, UAE",
        location_confidence="explicit",
        retrieved_at=now,
        posted_at=now - timedelta(days=1),
        application_email="careers@priorco.test",
        evidence_quote="Send CV to careers@priorco.test.",
        validated_employer="PriorCo",
        role_extracted="CAFM Administrator",
    )
    b = Batch(user_id=user.id, mode="dry_run", run_mode="dry_run")
    db_session.add(b)
    db_session.commit()
    execute_batch_pipeline(db_session, batch=b, candidate_overrides=[dup_cand], skip_network_dns=True)
    assert b.skipped_count == 1
    assert b.skip_reason_counts_json.get("PREVIOUSLY_CONTACTED") == 1

    # GMAIL-07: Optional save draft in Gmail uses users.drafts.create cleanly without sending
    did = create_gmail_draft(
        db_session,
        conn=conn_full,
        outbound=OutboundEmailMessage(
            from_email="fictional-04f8996d@gmail.com",
            from_name="Tariq",
            to_email="jobs@freshcompany.test",
            cc_emails=[],
            subject="Application for CAFM",
            body_text="Please find my attached CV.",
            rfc_message_id="<fictional-2d711642@gmail.com>",
        ),
        http_transport=transport,
    )
    assert did == "draft-xyz-789"


def test_ui01_ui02_ui03_sec01_export01_review_hash_invalidation_isolation_and_5sheet_xlsx(client: TestClient, db_session, monkeypatch):
    # Register two separate tenants using independent TestClients so cookies don't collide
    with TestClient(client.app) as client1, TestClient(client.app) as client2:
        u1_data, h1 = _register_and_auth(client1, "owner-u1@example.org")
        u1 = u1_data["user"]
        _, h2 = _register_and_auth(client2, "other-u2@example.org")
        # Verify Tenant 1 can run approval_required & export cleanly
        c1 = client1
        c2 = client2
        return _run_ui_sec_export_assertions(c1, c2, u1, h1, h2, db_session, monkeypatch)


def _run_ui_sec_export_assertions(client1: TestClient, client2: TestClient, u1: dict, h1: dict, h2: dict, db_session, monkeypatch):
    client = client1

    # Setup profile, DOCX CV, and official verified draft via approval_required batch
    client.put(
        "/api/v1/profile",
        json={
            "full_name": "Tariq Al-Mansoor",
            "contact_email": "owner-u1@example.org",
            "residence_country_code": "AE",
            "residence_city": "Dubai",
            "work_authorizations": ["AE"],
            "nationalities": ["JO"],
            "confirm_profile": True,
        },
        headers=h1,
    )
    client.put(
        "/api/v1/preferences",
        json={
            "target_titles": ["CAFM Administrator"],
            "target_locations": [{"country_code": "AE", "country_name": "United Arab Emirates", "city": "Dubai"}],
            "min_match_score": 40,
            "campaign_mode": "approval_required",
            "live_consent_granted": False,
        },
        headers=h1,
    )

    # UI-03: Autopilot API blocked with 409 LIVE_CONSENT_REQUIRED until consent granted!
    r_no_consent = client.post(
        "/api/v1/batches",
        json={"mode": "live", "run_mode": "autopilot", "run_immediately": False},
        headers=h1,
    )
    assert r_no_consent.status_code == 409
    assert r_no_consent.json()["detail"]["code"] == "LIVE_CONSENT_REQUIRED"

    from tests.conftest import build_pdf_bytes

    pdf_bytes = build_pdf_bytes(
        [
            "Tariq Al-Mansoor",
            "2020-01 to 2025-10 CAFM Administrator managing IBM Maximo, SQL, Power BI in Dubai, UAE",
            "Skills: CAFM, IBM Maximo, SQL, Power BI",
        ]
    )
    client.post(
        "/api/v1/cvs/upload",
        data={"variant_label": "CAFM PDF"},
        files={"file": ("Tariq.pdf", pdf_bytes, "application/pdf")},
        headers=h1,
    )

    # Paste an application instruction. Domain alignment alone cannot corroborate an official page.
    b_resp = client.post(
        "/api/v1/batches",
        json={
            "mode": "live",
            "run_mode": "approval_required",
            "run_immediately": True,
            "pasted_posting": {
                "title": "CAFM Administrator",
                "company_name": "Oasis FM",
                "source_url": "https://oasisfm.test/careers/cafm",
                "location_raw": "Dubai, UAE",
                "raw_text": (
                    "Oasis FM is hiring a CAFM Administrator in Dubai, UAE with IBM Maximo, SQL, and Power BI. "
                    "Interested candidates should send your CV to careers@oasisfm.test Subject: REF-OASIS-01."
                ),
            },
        },
        headers=h1,
    )
    assert b_resp.status_code == 201
    batch_id = b_resp.json()["batch_id"]
    b_detail = client.get(f"/api/v1/batches/{batch_id}", headers=h1).json()
    assert b_detail["batch"]["run_mode"] == "approval_required"
    assert b_detail["batch"]["sent_count"] == 0

    rq = client.get("/api/v1/review-queue", headers=h1).json()
    assert len(rq["items"]) == 1
    draft_item = rq["items"][0]
    app_id = draft_item["id"]
    assert draft_item["approval_state"] == "needs_human_review"
    assert draft_item["review_reasons"]

    # SEC-01: Tenant 2 CANNOT revise or approve Tenant 1's draft!
    assert client2.post(f"/api/v1/drafts/{app_id}/revise", json={"subject": "Hijack"}, headers=h2).status_code == 404
    assert client2.post("/api/v1/drafts/approve", json={"application_ids": [app_id]}, headers=h2).json()["approved_ids"] == []

    # UI-01: Approve -> hash locked
    ap = client.post("/api/v1/drafts/approve", json={"application_ids": [app_id], "expected_hashes": {app_id: draft_item["draft_content_hash"]}, "review_notes": "Verified Oasis FM"}, headers=h1)
    assert ap.status_code == 200 and ap.json()["approved_ids"] == [app_id]

    # UI-01b: Attempting send while server LIVE_SENDING_ENABLED=false returns 409 LIVE_SENDING_DISABLED!
    snd_disabled = client.post("/api/v1/drafts/send-approved", json={"application_ids": [app_id]}, headers=h1)
    assert snd_disabled.status_code == 409
    assert snd_disabled.json()["detail"]["code"] == "LIVE_SENDING_DISABLED"

    # UI-02: Revising an approved draft INVALIDATES approved_content_hash & blocks sending until re-approved!
    rev = client.post(
        f"/api/v1/drafts/{app_id}/revise",
        json={"subject": "REF-OASIS-01 Updated Subject", "body_text": draft_item["body_text"] + "\nLooking forward to your reply."},
        headers=h1,
    )
    assert rev.status_code == 200
    assert rev.json()["approval_invalidated"] is True
    assert rev.json()["draft"]["approved_content_hash"] == ""
    assert rev.json()["draft"]["approval_state"] == "needs_human_review"

    # Enable server live sending & add mailbox -> verify stale/unapproved draft is STILL BLOCKED until re-approved!
    monkeypatch.setenv("LIVE_SENDING_ENABLED", "true")
    get_settings.cache_clear()
    db_session.add(
        MailboxConnection(
            user_id=u1["id"],
            provider="smtp_imap",
            label="SMTP",
            sender_email="owner-u1@example.org",
            smtp_host="smtp.example.org",
            status="connected",
            is_primary=True,
        )
    )
    db_session.commit()
    sender_id = client.get("/api/v1/mailboxes", headers=h1).json()["sending_mailbox_id"]
    confirm = {"application_ids": [app_id], "expected_mailbox_id": sender_id}
    missing_ack = client.post("/api/v1/drafts/send-approved", json=confirm, headers=h1)
    assert missing_ack.status_code == 409 and missing_ack.json()["detail"]["code"] == "INCOMPLETE_SENT_HISTORY"
    confirm["acknowledge_incomplete_history"] = True
    wrong_sender = client.post("/api/v1/drafts/send-approved", json={**confirm, "expected_mailbox_id": "stale"}, headers=h1)
    assert wrong_sender.status_code == 409 and wrong_sender.json()["detail"]["code"] == "SENDER_CHANGED"
    snd_blocked = client.post("/api/v1/drafts/send-approved", json=confirm, headers=h1).json()
    assert snd_blocked["queued_ids"] == []
    assert any(b["id"] == app_id for b in snd_blocked["blocked"])

    # DATA-03: ApplicationTransition recorded draft -> approved -> invalidated transition trail!
    detail = client.get(f"/api/v1/applications/{app_id}", headers=h1).json()
    states_seen = [(tr["from_state"], tr["to_state"]) for tr in detail["transitions"]]
    assert ("needs_human_review", "approved") in states_seen
    assert ("approved", "needs_human_review") in states_seen

    # EXPORT-01: 5-sheet XLSX workbook with freeze panes, filters, safe formula escaping, typed dates
    db_session.add(RecipientHistory(user_id=u1["id"], normalized_email="=cmd|calc", display_email="test@sample-319590.test", company_name="=2+2", source="suppressed"))
    db_session.commit()
    x_resp = client.get("/api/v1/exports/xlsx?scope=real", headers=h1)
    assert x_resp.status_code == 200
    wb = load_workbook(io.BytesIO(x_resp.content))
    assert wb.sheetnames == [
        "Batch Summary",
        "Verified Opportunities",
        "Research & Skips",
        "Applications & Outcomes",
        "Dedup & Suppression",
    ]
    for sname in wb.sheetnames:
        ws = wb[sname]
        assert ws.freeze_panes == "A2"
        assert ws.auto_filter.ref is not None
    # Verify formula injection neutralization on Sheet 5
    ws_dedup = wb["Dedup & Suppression"]
    cells_str = [str(c.value or "") for row in ws_dedup.iter_rows(min_row=2) for c in row]
    assert any(val.startswith("'=cmd|calc") for val in cells_str)
    assert any(val.startswith("'=2+2") for val in cells_str)


def test_cv03_narrow_fit_tie_triggers_human_review_and_rationale(db_session):
    user, profile, pref, doc1, ver1, facts1, _, _ = _seed_vnext_user(db_session, email="cvtie@example.org")
    # Add a second CV variant with very close fit score so narrow tie margin (<= 3.0) triggers review
    doc2 = CvDocument(user_id=user.id, variant_label="Operations Generalist")
    db_session.add(doc2)
    db_session.flush()
    ver2 = CvVersion(
        cv_document_id=doc2.id,
        user_id=user.id,
        version_number=1,
        original_filename="Tariq_Ops_CV.pdf",
        mime_type="application/pdf",
        size_bytes=900,
        page_count=1,
        sha256="e" * 64,
        storage_key="users/test/ops.pdf",
        parse_status="parsed",
        ocr_status="not_needed",
        detected_language="en",
        extracted_text="Tariq Al-Mansoor CAFM Administrator IBM Maximo SQL Power BI Facilities Management",
        total_experience_months=58,
    )
    db_session.add(ver2)
    db_session.flush()
    doc2.active_version_id = ver2.id
    for cat, ttl, val, q in [
        ("experience", "CAFM Administrator", "4.8 years CAFM IBM Maximo SQL", "CAFM Administrator IBM Maximo SQL"),
        ("skill", "IBM Maximo", "IBM Maximo", "IBM Maximo"),
        ("skill", "Power BI", "Power BI", "Power BI"),
        ("skill", "SQL", "SQL", "SQL"),
    ]:
        db_session.add(
            CvExtractedFact(cv_version_id=ver2.id, user_id=user.id, category=cat, title=ttl, value=val, evidence_quote=q, confidence="evidenced")
        )
    db_session.commit()

    now = utcnow()
    cand = DiscoveredCandidate(
        source_provider="career_page",
        source_url="https://tieco.test/careers/cafm",
        source_type="job_post",
        title="CAFM Administrator",
        company_name="TieCo FM",
        company_domain="tieco.test",
        jd_text="CAFM Administrator in Dubai UAE (IBM Maximo, SQL, Power BI). Send your CV to jobs@tieco.test.",
        location_raw="Dubai, UAE",
        location_confidence="explicit",
        retrieved_at=now,
        posted_at=now - timedelta(days=1),
        application_email="jobs@tieco.test",
        evidence_quote="Send your CV to jobs@tieco.test.",
        validated_employer="TieCo FM",
        role_extracted="CAFM Administrator",
    )
    b = Batch(user_id=user.id, mode="dry_run", run_mode="dry_run")
    db_session.add(b)
    db_session.commit()
    execute_batch_pipeline(db_session, batch=b, candidate_overrides=[cand], skip_network_dns=True)

    app_rec = db_session.query(Application).filter(Application.batch_id == b.id).one()
    match_rec = db_session.query(JobMatch).filter(JobMatch.id == app_rec.job_match_id).one()
    assert app_rec.approval_state == "needs_human_review"
    assert any("Narrow CV variant tie" in r for r in (app_rec.review_reasons_json or []))
    assert "Outscored next CV variant" in match_rec.why_this_cv


def test_disc03_05_07_robots_standing_gmail_http_and_docx_send_pdf_checksum(client: TestClient, db_session, monkeypatch):
    # 1. DISC-03 & DISC-05 & DISC-07: robots block, 403 block, and standing invitation toggle + DOCX->PDF live send checksum
    user, profile, pref, doc, ver, facts, _, orig_docx_sha = _seed_vnext_user(db_session, email="docxsend@example.org", with_docx_cv=True)
    pref.include_standing_career_invitations = False
    db_session.commit()

    now = utcnow()
    robots_blocked = DiscoveredCandidate(
        source_provider="career_page",
        source_url="https://robots-co.test/careers/cafm",
        source_type="job_post",
        title="CAFM Administrator",
        company_name="RobotsCo",
        company_domain="robots-co.test",
        jd_text="CAFM Administrator Dubai UAE.",
        location_raw="Dubai, UAE",
        location_confidence="explicit",
        page_inaccessible_reason="Blocked by robots.txt policy",
        retrieved_at=now,
    )
    standing_inv = DiscoveredCandidate(
        source_provider="career_page",
        source_url="https://standco.test/careers/general",
        source_type="standing_invitation",
        title="General CAFM & Operations Career Invitation",
        company_name="StandCo",
        company_domain="standco.test",
        jd_text="We are always looking for CAFM talent in Dubai UAE. Send your CV for future roles to hr@standco.test.",
        location_raw="Dubai, UAE",
        location_confidence="explicit",
        is_standing_invitation=True,
        application_email="hr@standco.test",
        evidence_quote="Send your CV for future roles to hr@standco.test.",
        retrieved_at=now,
        posted_at=now - timedelta(days=1),
        validated_employer="StandCo",
        role_extracted="CAFM Talent",
    )
    official_cand = DiscoveredCandidate(
        source_provider="career_page",
        source_url="https://officialdocx.test/careers/cafm",
        source_type="job_post",
        title="CAFM Administrator",
        company_name="OfficialDocx Co",
        company_domain="officialdocx.test",
        jd_text="CAFM Administrator in Dubai UAE (IBM Maximo, SQL, Power BI). Send CV in PDF only to jobs@officialdocx.test.",
        location_raw="Dubai, UAE",
        location_confidence="explicit",
        retrieved_at=now,
        posted_at=now - timedelta(days=1),
        application_email="jobs@officialdocx.test",
        attachment_requirements=["pdf"],
        evidence_quote="Send CV in PDF only to jobs@officialdocx.test.",
        validated_employer="OfficialDocx Co",
        role_extracted="CAFM Administrator",
    )

    monkeypatch.setenv("LIVE_SENDING_ENABLED", "true")
    get_settings.cache_clear()
    conn = MailboxConnection(
        user_id=user.id,
        provider="smtp_imap",
        label="Work Mail",
        sender_email="docxsend@example.org",
        smtp_host="smtp.example.org",
        status="connected",
        is_primary=True,
    )
    db_session.add(conn)
    pref.campaign_mode = "autopilot"
    pref.live_consent_granted = True
    pref.live_consent_at = now
    db_session.commit()

    sent_payloads: list[OutboundEmailMessage] = []

    class _CapTransport:
        def send_message(self, payload: OutboundEmailMessage) -> SendResult:
            sent_payloads.append(payload)
            return SendResult(outcome=HandoffOutcome.PROVIDER_ACCEPTED, provider_message_id="smtp-pdf-1", rfc_message_id=payload.rfc_message_id)

        def reconcile_sent_message(self, rfc_message_id: str, action_id: str) -> SendResult | None:
            return None

    b = Batch(user_id=user.id, mode="live", run_mode="autopilot")
    db_session.add(b)
    db_session.commit()

    execute_batch_pipeline(
        db_session,
        batch=b,
        candidate_overrides=[robots_blocked, standing_inv, official_cand],
        skip_network_dns=True,
        auto_process_outbox_sync=True,
        transport_override=_CapTransport(),
    )

    from tests.test_batch_pipeline import approve_and_dispatch

    assert not sent_payloads
    approve_and_dispatch(db_session, user, b, conn, _CapTransport())

    # Standing invite skipped when toggle disabled; robots-blocked accounted for; official docx->pdf sent!
    assert b.skip_reason_counts_json.get("SOURCE_ROBOTS_BLOCKED", 0) >= 1
    assert len(sent_payloads) == 1
    att = sent_payloads[0].attachments[0]
    assert att.mime_type == "application/pdf"
    assert att.filename.endswith(".pdf")
    db_session.refresh(ver)
    assert ver.sha256 == orig_docx_sha  # Original DOCX immutable
    assert compute_sha256(att.data) == ver.derived_pdf_sha256
    assert compute_sha256(att.data) != orig_docx_sha

    # Verify GMAIL-06 HTTP coverage & SEC-01 isolation on HTTP routes
    with TestClient(client.app) as c1, TestClient(client.app) as c2:
        u1, h1 = _register_and_auth(c1, "cov1@example.org")
        _, h2 = _register_and_auth(c2, "cov2@example.org")
        m_u1 = MailboxConnection(
            user_id=u1["user"]["id"],
            provider="gmail_oauth",
            sender_email="fictional-b2070c15@gmail.com",
            granted_scopes_json=[GMAIL_SEND_SCOPE],
            status="connected",
        )
        db_session.add(m_u1)
        db_session.commit()
        r_cov = c1.get(f"/api/v1/mailboxes/{m_u1.id}/dedup-coverage", headers=h1)
        assert r_cov.status_code == 200
        assert r_cov.json()["coverage_mode"] == "applyflow_ledger_only"
        assert c2.get(f"/api/v1/mailboxes/{m_u1.id}/dedup-coverage", headers=h2).status_code == 404
        assert c2.post(f"/api/v1/mailboxes/{m_u1.id}/sync-sent", headers=h2).status_code == 404


def test_ref_bundle_query_diversity_cfemail_employer_and_driving_licence():
    # 1. Diverse multi-role clustering spreads CAFM synonyms + Data Analyst + Document Controller
    plan = build_email_application_discovery_plan(
        target_titles=[
            "CAFM Administrator", "CAFM Planner", "CAFM Coordinator", "CAFM Specialist",
            "CMMS Administrator", "Data Analyst", "Business Analyst", "Document Controller",
        ],
        role_families=["CAFM & CMMS", "Data Analytics", "Document Control"],
        target_locations=[{"country_code": "AE", "country_name": "United Arab Emirates", "city": "Dubai"}],
        search_languages=["en"],
        include_standing_invitations=True,
        max_queries=10,
    )
    titles_used = {q.title_term for q in plan}
    assert len(titles_used) >= 3
    assert any("Analyst" in t or "Document" in t for t in titles_used)

    # 2. Passive Cloudflare email decode + unbroken inline tag + subject instruction + employer extraction
    from app.services.discovery.career_page import (
        clean_and_extract_role_and_employer,
        extract_structured_application_evidence,
    )

    # Encode 'careers@rmjmd3.test' using XOR key 0x2A
    target_em = "careers@rmjmd3.test"
    cf_hex = "2a" + "".join(f"{ord(ch) ^ 0x2A:02x}" for ch in target_em)
    html = (
        f'<html><head><title>Document Controller - RMJM</title></head><body>'
        f'<p>We are seeking a Document Controller in Dubai, UAE.</p>'
        f'<p>To apply, please send your CV and portfolio to <a href="/cdn-cgi/l/email-protection#{cf_hex}" '
        f'data-cfemail="{cf_hex}">[email protected]</a>. Include the subject title “Document Controller”.</p>'
        f'</body></html>'
    )
    ev = extract_structured_application_evidence("", html_text=html, source_url="https://rmjm.test/careers/document-controller/")
    assert ev.primary_email == "careers@rmjmd3.test"
    assert ev.subject_instruction == "Document Controller"

    role, val_emp, _, _, _ = clean_and_extract_role_and_employer(
        "CAFM Coordinator Jobs UAE - Operon 2026",
        "CAFM Coordinator in UAE",
        "https://fictional-studio.test/jobs/sample-0ddcb922",
        og_site_name="UAEHRS.COM - UAE New Jobs and Walk In Interviews",
    )
    assert val_emp == "Operon"
    assert "UAEHRS" not in val_emp
    assert "CAFM Coordinator" in role

    # 3. CV parser extracts UAE Driving Licence into certification facts & satisfies mandatory DL gate
    from app.services.cv_parser import extract_facts_from_text

    parsed_facts, _, _, _ = extract_facts_from_text(
        "Tariq Al-Mansoor\nDubai, UAE | UAE Driving Licence | IBM Maximo MAS 8\n2020-01 to Present CAFM Administrator"
    )
    assert any(f.category == "certification" and "driving licen" in f.value.lower() for f in parsed_facts)


def test_ref_bundle_cv_specialization_pick_standing_career_and_batch_preview_api(client: TestClient, db_session):
    from app.db.models import JobPosting
    from app.services.matching import select_best_cv_for_posting

    user, profile, pref, doc1, ver1, facts1, _, _ = _seed_vnext_user(db_session, email="cvspec@example.org")
    # Update CV 1 explicitly to CAFM specialist identity
    ver1.original_filename = "Candidate_CAFM_Specialist_CV.pdf"
    ver1.extracted_text = "Tariq Al-Mansoor\nCAFM Specialist | IBM Maximo MAS 8 | Facilities Management | UAE Driving Licence | Dubai UAE"
    # Second CV specifically tailored to MIS / Data Analytics
    doc2 = CvDocument(user_id=user.id, variant_label="MIS Data Analyst")
    db_session.add(doc2)
    db_session.flush()
    ver2 = CvVersion(
        cv_document_id=doc2.id,
        user_id=user.id,
        version_number=1,
        original_filename="Candidate_MIS_Data_Analyst_CV.pdf",
        mime_type="application/pdf",
        size_bytes=950,
        page_count=1,
        sha256="d" * 64,
        storage_key="users/test/data_analyst.pdf",
        parse_status="parsed",
        ocr_status="not_needed",
        detected_language="en",
        extracted_text="Tariq Al-Mansoor\nMIS Data Analyst | SQL | Power BI | Excel | Python | Business Intelligence | Dubai UAE | UAE Driving Licence",
        total_experience_months=60,
    )
    db_session.add(ver2)
    db_session.flush()
    doc2.active_version_id = ver2.id
    facts2 = []
    for cat, ttl, val, q in [
        ("experience", "MIS Data Analyst", "5 years SQL Power BI reporting and dashboards", "MIS Data Analyst SQL Power BI"),
        ("tool", "SQL", "SQL", "SQL"),
        ("tool", "Power BI", "Power BI", "Power BI"),
        ("tool", "Python", "Python", "Python"),
        ("certification", "UAE Driving Licence", "UAE Driving Licence", "UAE Driving Licence"),
    ]:
        f = CvExtractedFact(cv_version_id=ver2.id, user_id=user.id, category=cat, title=ttl, value=val, evidence_quote=q, confidence="evidenced")
        db_session.add(f)
        facts2.append(f)

    pref.target_titles_json = ["CAFM Coordinator", "Data Analyst", "Facilities Management"]
    pref.include_standing_career_invitations = True
    db_session.commit()

    # CAFM Coordinator must decisively pick Candidate_CAFM_Specialist_CV.pdf without false tie
    p_cafm = JobPosting(
        user_id=user.id,
        batch_id="spec-1",
        source_url="https://operon.example/cafm",
        canonical_url="https://operon.example/cafm",
        company_name="Operon FM",
        validated_employer="Operon FM",
        title="CAFM Coordinator",
        role_extracted="CAFM Coordinator",
        jd_text="CAFM Coordinator in Dubai UAE managing IBM Maximo, PPM, KPI, SQL, and Power BI. UAE Driving Licence required. Email careers@operon.example.",
        country_code="AE",
        city="Dubai",
        location_raw="Dubai, UAE",
        application_email="careers@operon.example",
        evidence_quote="Email careers@operon.example",
        parsing_version="v3.0",
    )
    best_cafm, evals_cafm = select_best_cv_for_posting(posting=p_cafm, cv_candidates=[(ver1, facts1), (ver2, facts2)], pref=pref, profile=profile)
    assert best_cafm is not None
    assert best_cafm.hard_filter_passed is True
    assert best_cafm.cv_version_id == ver1.id
    assert not any("Narrow CV variant tie" in r for r in best_cafm.review_reasons)

    # Data Analyst must decisively pick Candidate_MIS_Data_Analyst_CV.pdf
    p_data = JobPosting(
        user_id=user.id,
        batch_id="spec-1",
        source_url="https://data.example/analyst",
        canonical_url="https://data.example/analyst",
        company_name="Insight AE",
        validated_employer="Insight AE",
        title="Data Analyst",
        role_extracted="Data Analyst",
        jd_text="MIS Data Analyst in Dubai UAE skilled in SQL, Power BI, Python, and Excel reporting. Email jobs@data.example.",
        country_code="AE",
        city="Dubai",
        location_raw="Dubai, UAE",
        application_email="jobs@data.example",
        evidence_quote="Email jobs@data.example",
        parsing_version="v3.0",
    )
    best_data, _ = select_best_cv_for_posting(posting=p_data, cv_candidates=[(ver1, facts1), (ver2, facts2)], pref=pref, profile=profile)
    assert best_data is not None
    assert best_data.hard_filter_passed is True
    assert best_data.cv_version_id == ver2.id

    # Standing company career page Expression of Interest prepares cleanly instead of ROLE_MISMATCH
    p_standing = JobPosting(
        user_id=user.id,
        batch_id="spec-1",
        source_url="https://bukhatir.example/careers/real-estate",
        canonical_url="https://bukhatir.example/careers/real-estate",
        company_name="Bukhatir",
        validated_employer="Bukhatir",
        title="Real Estate Careers | Bukhatir",
        role_extracted="Real Estate Careers",
        jd_text="Bukhatir Real Estate & Property Operations Sharjah UAE. If there is no position of interest, send your CV to careers@bukhatir.example.",
        country_code="AE",
        city="Sharjah",
        location_raw="Sharjah, UAE",
        application_email="careers@bukhatir.example",
        evidence_quote="send your CV to careers@bukhatir.example",
        is_standing_invitation=True,
        parsing_version="v3.0",
    )
    pref.target_locations_json = [*pref.target_locations_json, {"country_code": "AE", "city": "Sharjah"}]
    best_st, _ = select_best_cv_for_posting(posting=p_standing, cv_candidates=[(ver1, facts1), (ver2, facts2)], pref=pref, profile=profile)
    assert best_st is not None and best_st.hard_filter_passed is True

    # Off-domain Accountant & Financial Analyst must NEVER pass for CAFM/Data Analyst CV via static phantom points
    p_acc = JobPosting(
        user_id=user.id,
        batch_id="spec-1",
        source_url="https://acc.example/job",
        canonical_url="https://acc.example/job",
        company_name="Manufacturing Co",
        validated_employer="Manufacturing Co",
        title="Accountant & Financial Analyst",
        role_extracted="Accountant & Financial Analyst",
        jd_text="Seeking Accountant & Financial Analyst in Abu Dhabi with IFRS, Budgeting, and Excel experience. Send CV to hr@acc.test.",
        country_code="AE",
        city="Abu Dhabi",
        location_raw="Abu Dhabi, UAE",
        application_email="hr@acc.test",
        evidence_quote="Send CV to hr@acc.test",
        parsing_version="v3.0",
    )
    best_acc, _ = select_best_cv_for_posting(posting=p_acc, cv_candidates=[(ver1, facts1), (ver2, facts2)], pref=pref, profile=profile)
    assert best_acc is not None and best_acc.hard_filter_passed is False
    assert "ROLE_MISMATCH" in best_acc.failure_codes or "FIT_THRESHOLD" in best_acc.failure_codes

    # LLM drafting must strip leaked (fact IDs: ...) or UUID strings from email body
    from app.services.ai.base import LlmDraftResult, StructuredDraftPayload
    from app.services.ai.grounding import generate_grounded_application_draft

    class _LeakLlm:
        def generate_structured_draft(self, _sys: str, _usr: str) -> LlmDraftResult:
            f_id = facts1[0].id
            return LlmDraftResult(
                payload=StructuredDraftPayload(
                    subject="CAFM Coordinator – Tariq Al-Mansoor",
                    body_paragraphs=[
                        f"Dear Operon Team, I am applying for CAFM Coordinator aligning with Maximo (fact IDs: {f_id}).",
                        f"My background covers PPM and SLA reporting [{f_id}] in Dubai, UAE.",
                        "Please find my CV attached for your review and consideration.",
                    ],
                    cited_fact_ids=[f_id],
                ),
                provider="mock",
                model="mock-1",
            )

    drafted = generate_grounded_application_draft(
        posting=p_cafm,
        profile=profile,
        pref=pref,
        facts=facts1,
        supporting_fact_ids=[facts1[0].id],
        language="en",
        llm_provider=_LeakLlm(),
    )
    assert facts1[0].id not in drafted.body_text
    assert "fact id" not in drafted.body_text.lower()


def test_ref_bundle_imap_sent_history_dedup_sync_and_batch_preview_api(client: TestClient, db_session, monkeypatch):

    from app.services.dedupe import find_existing_recipients

    u_reg, headers = _register_and_auth(client, "imapsent@example.org")
    uid = u_reg["user"]["id"]
    client.put(
        "/api/v1/profile",
        json={
            "full_name": "Imap Tester",
            "contact_email": "imapsent@example.org",
            "residence_country_code": "AE",
            "residence_city": "Dubai",
            "work_authorizations": ["AE"],
            "notice_period_days": 30,
            "confirm_profile": True,
        },
        headers=headers,
    )
    doc = CvDocument(user_id=uid, variant_label="CAFM")
    db_session.add(doc)
    db_session.flush()
    ver = CvVersion(
        cv_document_id=doc.id,
        user_id=uid,
        version_number=1,
        original_filename="Tester_CAFM_CV.pdf",
        mime_type="application/pdf",
        size_bytes=512,
        page_count=1,
        sha256="f" * 64,
        derived_pdf_sha256="f" * 64,
        derived_pdf_storage_key="users/imap/cv.pdf",
        pdf_conversion_status="ready",
        storage_key="users/imap/cv.pdf",
        parse_status="parsed",
        detected_language="en",
        extracted_text="CAFM Coordinator IBM Maximo Dubai UAE",
        total_experience_months=48,
    )
    db_session.add(ver)
    db_session.flush()
    doc.active_version_id = ver.id
    db_session.add(
        CvExtractedFact(
            cv_version_id=ver.id,
            user_id=uid,
            category="experience",
            title="CAFM Coordinator",
            value="4 years IBM Maximo",
            evidence_quote="4 years IBM Maximo CAFM",
            confidence="evidenced",
        )
    )

    conn = MailboxConnection(
        user_id=uid,
        provider="smtp_imap",
        label="Gmail App Password",
        sender_email="fictional-afc2b548@gmail.com",
        smtp_host="smtp.gmail.com",
        smtp_port=587,
        imap_host="imap.gmail.com",
        imap_port=993,
        encrypted_credentials=encrypt_json({"username": "fictional-afc2b548@gmail.com", "password": "app-password"}),
        status="connected",
        is_primary=True,
    )
    db_session.add(conn)
    db_session.commit()

    class _FakeImap:
        def __init__(self, *_a, **_kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def login(self, *_a):
            return "OK", []

        def select(self, folder, readonly=True):
            if "Sent Mail" in folder:
                return "OK", [b"1"]
            return "NO", []

        def response(self, key):
            assert key == "UIDVALIDITY"
            return key, [b"7"]

        def uid(self, cmd, *_a):
            if cmd == "search":
                return "OK", [b"101"]
            if cmd == "fetch":
                hdr = (
                    b"To: hr@already-applied.test\r\n"
                    b"Subject: Application for CAFM Coordinator\r\n"
                    b"Date: Thu, 19 Nov 2026 10:00:00 +0000\r\n"
                    b"Message-ID: <fictional-78e2ba4b@gmail.com>\r\n\r\n"
                )
                return "OK", [(b"101 (BODY[HEADER]", hdr)]
            return "NO", []

    monkeypatch.setattr("app.core.mail_sockets.PublicIMAPSSL", _FakeImap)
    monkeypatch.setattr("app.services.mailbox.gmail_sent.validate_mail_host", lambda host, port: None)
    res_sync = client.post(f"/api/v1/mailboxes/{conn.id}/sync-sent", headers=headers)
    assert res_sync.status_code == 200
    body_sync = res_sync.json()
    assert body_sync["status"] == "ok"
    assert body_sync["coverage_mode"] == "applyflow_ledger_plus_gmail_sent"
    assert body_sync["dedup_coverage"]["suppressed_job_recipients_count"] == 1
    # Verify dedupe blocks hr@already-applied.test pulled from IMAP Sent Mail
    assert "hr@already-applied.test" in find_existing_recipients(db_session, user_id=uid, emails=["hr@already-applied.test"])

    # Batch detail returns inline draft_applications for preview right after batch execution
    resp_b = client.post(
        "/api/v1/batches",
        json={
            "mode": "dry_run",
            "run_mode": "dry_run",
            "include_standing_invitations": True,
            "run_immediately": True,
            "pasted_posting": {
                "title": "CAFM Coordinator",
                "company_name": "Fresh Co",
                "source_url": "https://freshco.test/careers/cafm",
                "location_raw": "Dubai, UAE",
                "raw_text": "Hiring CAFM Coordinator in Dubai UAE using IBM Maximo. Send your CV to careers@freshco.test.",
            },
        },
        headers=headers,
    )
    assert resp_b.status_code == 201
    b_id = resp_b.json()["batch_id"]
    b_det = client.get(f"/api/v1/batches/{b_id}", headers=headers).json()
    assert len(b_det["draft_applications"]) == 1
    assert b_det["draft_applications"][0]["recipient_email"] == "careers@freshco.test"
    assert b_det["draft_applications"][0]["frozen_cv_filename"] == "Tester_CAFM_CV.pdf"
    assert "CAFM Coordinator – Imap Tester" in b_det["draft_applications"][0]["subject"]


def test_international_email_first_source_directory_and_multiline_extraction(monkeypatch):
    from app.services import verification as ver_mod
    from app.services.discovery.career_page import (
        detect_informational_post,
        extract_structured_application_evidence,
    )
    from app.services.discovery.query_builder import format_query_for_provider
    from app.services.discovery.source_directory import (
        classify_industry_buckets,
        get_recommended_domain_cap,
        is_non_vacancy_seo_or_index_url,
        select_directory_sources,
    )

    # 1. Directory matches country & industry cleanly without mixing UK/Qatar boards into UAE-only queries
    buckets = classify_industry_buckets(["CAFM Coordinator", "Data Analyst"], ["Facilities Management", "Data Analytics"])
    assert "fm_engineering" in buckets and "data_analytics_tech" in buckets
    ae_sources = select_directory_sources(country_codes=["AE"], industry_buckets=buckets)
    ae_domains = {s.domain for s in ae_sources}
    assert {"gocareer.club", "uaehrs.com", "freejobsindubai.com", "expatriates.com"}.issubset(ae_domains)
    assert "charityjob.co.uk" not in ae_domains

    gb_sources = select_directory_sources(country_codes=["GB"], industry_buckets=buckets)
    assert any(s.domain == "charityjob.co.uk" for s in gb_sources)

    assert get_recommended_domain_cap("https://www.gocareer.club/2026/11/post.html", default_cap=3) >= 5
    assert get_recommended_domain_cap("https://random-portal.test/job/1", default_cap=3) == 3

    assert is_non_vacancy_seo_or_index_url("https://enhancv.com/ai-resume-builder/", "AI Resume Builder") is True
    assert is_non_vacancy_seo_or_index_url("https://qa.indeed.com/q-data-jobs.html", "Jobs") is True
    assert is_non_vacancy_seo_or_index_url("https://www.gocareer.club/2026/11/data-analyst.html", "Data Analyst") is False

    # 2. Query planner includes email_first_directory + never sends site:.ae to Tavily
    plan = build_email_application_discovery_plan(
        target_titles=["CAFM Administrator", "Data Analyst", "Business Analyst", "Operations Coordinator"],
        role_families=["CAFM & CMMS", "Data Analytics"],
        target_locations=[{"country_code": "AE", "country_name": "United Arab Emirates", "city": "Dubai"}],
        search_languages=["en"],
        include_standing_invitations=True,
        max_queries=10,
    )
    assert any(p.source_group == "email_first_directory" and p.preferred_domains for p in plan)
    for pq in plan:
        assert "site:.ae" not in format_query_for_provider(pq, "tavily")

    # 3. Multi-line "email your details" / 2-line header + HTML tag stripping + seeker classifieds detection
    multiline_txt = (
        "Company: Example Exchange LLC\n"
        "To apply for this job\n"
        "email your details to\n"
        "<strong>careers@exchange-co.example</strong>\n"
        "Use subject \"Data Analyst\""
    )
    ev1 = extract_structured_application_evidence(multiline_txt, source_url="https://gccrecruitments.com/job/da")
    assert ev1.primary_email == "careers@exchange-co.example"
    assert "<strong>" not in ev1.evidence_quote

    three_line_txt = (
        "Interested candidates can send their updated CV to the recruitment team:\n"
        "Email:\n"
        "recruitment@hospitality-co.example\n"
    )
    ev2 = extract_structured_application_evidence(three_line_txt, source_url="https://freejobsindubai.com/job/test")
    assert ev2.primary_email == "recruitment@hospitality-co.example"

    assert (
        detect_informational_post(
            url="https://www.expatriates.com/cls/63711847.html",
            title="Data Analyst | Open To Relocate - Dubai Job Seekers, 63711847",
            jd_text="Data Analyst seeking opportunity in Dubai",
            has_email_invite=False,
            has_portal_apply=False,
        )
        is True
    )

    # 4. DNS check retries over TCP cleanly when UDP times out (never mislabels valid MX as RECIPIENT_UNVERIFIED)
    calls: list[bool] = []

    class _FakeResolver:
        lifetime = 4.0
        timeout = 2.0

        def resolve(self, _domain: str, _qtype: str, tcp: bool = False):
            calls.append(tcp)
            if not tcp:
                raise ver_mod.dns.exception.Timeout()
            return ["mx1.mail.example"]

    ver_mod._DNS_ROUTING_CACHE.clear()
    monkeypatch.setattr(ver_mod.dns.resolver, "Resolver", _FakeResolver)
    d_stat, d_msg = ver_mod.check_domain_mail_routing("valid-corp-mx.net", skip_network_dns=False)
    assert calls == [False, True]
    assert d_stat == "valid_mx"
    assert d_msg == ""


def test_gemini_reference_bundle_replay_and_gmail_sent_dedup(db_session, monkeypatch):
    """Reference-bundle regression test distinguished from live discovery: verifies specialized CVs + Sent dedup."""
    from sqlalchemy import select

    from app.services.storage import compute_sha256
    from tests.conftest import build_pdf_bytes

    monkeypatch.setenv("LIVE_SENDING_ENABLED", "false")
    get_settings.cache_clear()
    user, profile, pref, doc_cafm, ver_cafm, _, _, _ = _seed_vnext_user(db_session)
    pref.include_standing_career_invitations = True
    pref.freshness_days = 30
    pref.target_titles_json = ["CAFM Coordinator", "CAFM Administrator", "MIS Data Analyst", "Data Analyst"]

    # Add a second specialized CV (MIS Data Analyst) alongside CAFM CV
    storage = get_storage_adapter()
    data_pdf = build_pdf_bytes("Tariq Al-Mansoor\nDubai, UAE | MIS Data Analyst\nSQL Power BI Python Excel Dashboard Reporting\n2021-01 to Present Data Analyst")
    data_key = f"cvs/{user.id}/mis_data_analyst.pdf"
    storage.put_bytes(data_key, data_pdf)
    data_sha = compute_sha256(data_pdf)
    doc_data = CvDocument(user_id=user.id, variant_label="MIS Data Analyst CV")
    db_session.add(doc_data)
    db_session.flush()
    ver_data = CvVersion(
        cv_document_id=doc_data.id,
        user_id=user.id,
        version_number=1,
        original_filename="Candidate_MIS_Data_Analyst_CV.pdf",
        mime_type="application/pdf",
        size_bytes=len(data_pdf),
        sha256=data_sha,
        storage_key=data_key,
        derived_pdf_storage_key=data_key,
        derived_pdf_sha256=data_sha,
        pdf_conversion_status="ready",
        pdf_page_count=1,
        parse_status="parsed",
        ocr_status="not_needed",
        detected_language="en",
        extracted_text="MIS Data Analyst SQL Power BI Python Excel Dashboard Reporting Dubai UAE",
        total_experience_months=48,
    )
    db_session.add(ver_data)
    db_session.flush()
    doc_data.active_version_id = ver_data.id
    for cat, ttl, val in (
        ("experience", "MIS Data Analyst", "4 years SQL Power BI Python Excel dashboards"),
        ("tool", "SQL", "SQL"),
        ("tool", "Power BI", "Power BI"),
        ("tool", "Python", "Python"),
        ("role", "MIS Data Analyst", "MIS Data Analyst"),
    ):
        db_session.add(
            CvExtractedFact(
                cv_version_id=ver_data.id,
                user_id=user.id,
                category=cat,
                title=ttl,
                value=val,
                evidence_quote=val,
                confidence="evidenced",
            )
        )

    # Record a previously sent Gmail recipient so replay dedup suppresses duplicate contact
    conn = MailboxConnection(
        user_id=user.id,
        provider="gmail_oauth",
        label="Gmail OAuth",
        sender_email="fictional-dda18a0e@gmail.com",
        status="connected",
        is_primary=True,
    )
    db_session.add(conn)
    db_session.flush()
    db_session.add(
        GmailSentRecord(
            user_id=user.id,
            mailbox_connection_id=conn.id,
            provider_message_id="gmail-msg-prior-1",
            thread_id="th-prior-1",
            recipient_email="hr@sample-251677.test",
            recipient_normalized="hr@sample-251677.test",
            recipient_domain="sample-251677.test",
            subject="Application for CAFM Administrator",
            sent_at=utcnow() - timedelta(days=3),
            classification="job_application",
            is_suppressed_recipient=True,
        )
    )
    db_session.commit()

    now = utcnow()
    replay_candidates = [
        # 1. Direct email role vacancy -> must select CAFM CV
        DiscoveredCandidate(
            source_provider="career_page",
            source_url="https://sample-9a0cfa.test/jobs/cafm-coordinator",
            source_type="job_post",
            title="CAFM Coordinator",
            role_extracted="CAFM Coordinator",
            company_name="Operon Ref",
            company_domain="sample-9a0cfa.test",
            validated_employer="Operon Ref",
            jd_text="Hiring CAFM Coordinator in Dubai UAE using IBM Maximo, PPM, KPI, SQL, and Power BI. Send your CV to careers@sample-9a0cfa.test.",
            location_raw="Dubai, UAE",
            location_confidence="explicit",
            retrieved_at=now,
            posted_at=now - timedelta(days=2),
            application_email="careers@sample-9a0cfa.test",
            evidence_quote="Send your CV to careers@sample-9a0cfa.test.",
            access_status="fetched_200",
        ),
        # 2. Data/MIS email vacancy -> must select MIS Data Analyst CV
        DiscoveredCandidate(
            source_provider="career_page",
            source_url="https://sample-e4a4c4.test/jobs/mis-analyst",
            source_type="job_post",
            title="MIS Data Analyst",
            role_extracted="MIS Data Analyst",
            company_name="Insight Ref",
            company_domain="sample-e4a4c4.test",
            validated_employer="Insight Ref",
            jd_text="Seeking MIS Data Analyst in Dubai UAE skilled in SQL, Power BI, Python, Excel dashboards, and reporting. Email your CV to jobs@sample-e4a4c4.test.",
            location_raw="Dubai, UAE",
            location_confidence="explicit",
            retrieved_at=now,
            posted_at=now - timedelta(days=1),
            application_email="jobs@sample-e4a4c4.test",
            evidence_quote="Email your CV to jobs@sample-e4a4c4.test.",
            access_status="fetched_200",
        ),
        # 3. Standing Expression of Interest -> prepares cleanly when standing invitations enabled
        DiscoveredCandidate(
            source_provider="career_page",
            source_url="https://sample-d2dc9c.test/careers",
            source_type="standing_invitation",
            title="Careers | Cloudlink Ref",
            role_extracted="General Career Invitation",
            company_name="Cloudlink Ref",
            company_domain="sample-d2dc9c.test",
            validated_employer="Cloudlink Ref",
            jd_text="Cloudlink Ref Dubai UAE operations, systems analytics, Power BI & Excel support. Send your CV for future openings to careers@sample-d2dc9c.test.",
            location_raw="Dubai, UAE",
            location_confidence="explicit",
            retrieved_at=now,
            application_email="careers@sample-d2dc9c.test",
            evidence_quote="Send your CV for future openings to careers@sample-d2dc9c.test.",
            is_standing_invitation=True,
            access_status="fetched_200",
        ),
        # 4. Already in Gmail Sent history -> MUST skip with PREVIOUSLY_CONTACTED
        DiscoveredCandidate(
            source_provider="career_page",
            source_url="https://sample-251677.test/jobs/cafm",
            source_type="job_post",
            title="CAFM Administrator",
            role_extracted="CAFM Administrator",
            company_name="Already Sent Ref",
            company_domain="sample-251677.test",
            validated_employer="Already Sent Ref",
            jd_text="Hiring CAFM Administrator in Dubai UAE using IBM Maximo and SQL. Send your CV to hr@sample-251677.test.",
            location_raw="Dubai, UAE",
            location_confidence="explicit",
            retrieved_at=now,
            posted_at=now - timedelta(days=2),
            application_email="hr@sample-251677.test",
            evidence_quote="Send your CV to hr@sample-251677.test.",
            access_status="fetched_200",
        ),
        # 5. Page with no email invite -> MUST stay out of drafts
        DiscoveredCandidate(
            source_provider="career_page",
            source_url="https://dead-ref.net/careers",
            source_type="job_post",
            title="404 Careers",
            company_name="Dead Ref",
            company_domain="dead-ref.net",
            jd_text="Careers portal page without email contact.",
            location_raw="Dubai, UAE",
            location_confidence="explicit",
            retrieved_at=now,
            access_status="http_404",
        ),
    ]

    batch = Batch(user_id=user.id, mode="dry_run", run_mode="approval_required", status="queued", target_goal_count=10)
    db_session.add(batch)
    db_session.commit()

    execute_batch_pipeline(
        db_session,
        batch=batch,
        candidate_overrides=replay_candidates,
        skip_network_dns=True,
    )
    apps = db_session.scalars(select(Application).where(Application.batch_id == batch.id)).all()
    drafted = {a.recipient_email: a for a in apps if a.transport_status == "draft"}
    skipped = {a.recipient_email: a for a in apps if a.transport_status == "skipped"}
    assert "careers@sample-9a0cfa.test" in drafted
    assert drafted["careers@sample-9a0cfa.test"].selected_cv_version_id == ver_cafm.id
    assert "jobs@sample-e4a4c4.test" in drafted
    assert drafted["jobs@sample-e4a4c4.test"].selected_cv_version_id == ver_data.id
    assert "careers@sample-d2dc9c.test" in drafted
    assert "hr@sample-251677.test" in skipped
    assert skipped["hr@sample-251677.test"].primary_skip_code == "PREVIOUSLY_CONTACTED"
    assert all(a.sent_at is None for a in apps)


def test_query_builder_yield_and_evidence_quote_centering(monkeypatch) -> None:
    """High-rotation queries stay within high-yield title clusters, cap Brave pages, and keep emails inside evidence quotes."""
    from app.services.discovery.career_page import extract_structured_application_evidence
    from app.services.discovery.query_builder import (
        build_email_application_discovery_plan,
        extract_title_variations_from_inputs,
        format_query_for_provider,
    )

    real_titles = [
        "CAFM Coordinator",
        "Data Analyst",
        "MIS Analyst",
        "Business Analyst",
        "IBM Maximo Administrator",
        "Document Controller",
        "QA/QC Coordinator",
    ]
    extracted = extract_title_variations_from_inputs(
        target_titles=real_titles,
        role_families=["Data Analytics", "Operations Analytics", "Project Coordination"],
        cv_titles=["Facilities Coordinator"],
    )
    assert "Project Coordination" not in extracted
    assert "Data Analytics" not in extracted

    sparse = extract_title_variations_from_inputs(
        target_titles=["Operations Coordinator"],
        role_families=["Data Analytics"],
    )
    assert "Data Analytics" in sparse

    plan = build_email_application_discovery_plan(
        target_titles=real_titles,
        role_families=["CAFM & CMMS Management", "Data Analytics"],
        target_locations=[{"country_code": "AE", "country_name": "United Arab Emirates", "cities": ["Dubai", "Abu Dhabi"]}],
        search_languages=["en"],
        include_standing_invitations=True,
        rotation_offset=16,
        max_queries=10,
    )
    assert all(q.page <= 1 for q in plan)

    dir_queries = [q for q in plan if q.source_group == "email_first_directory"]
    assert dir_queries
    assert any("UAE" in q.query and "Dubai" in q.query for q in dir_queries)

    for q in plan:
        if q.source_group == "social_hiring_posts" and "IBM" in q.query:
            assert '"IBM Maximo"' in q.query
            assert '"IBM" ' not in q.query
        if q.source_group == "company_careers" and q.provider_hint == "brave":
            assert q.page == 0
            rendered = format_query_for_provider(q, "brave")
            assert "/" not in rendered

    long_prefix = "We are hiring a skilled Operations and Data Specialist to manage dashboards, KPI reporting, PPM scheduling, preventive work orders, vendor coordination, compliance reviews, and executive monthly packs across commercial properties in Dubai and Sharjah. " * 2
    long_text = f"{long_prefix}Qualified candidates please send your CV to careers@sample-5b32a2.test before Sunday."
    assert len(long_text) > 500
    ev = extract_structured_application_evidence(long_text, source_url="https://sample-5b32a2.test/careers")
    assert ev.primary_email == "careers@sample-5b32a2.test"
    assert "careers@sample-5b32a2.test" in ev.evidence_quote
    assert len(ev.evidence_quote) <= 400

    adj_text = (
        "Document Controller vacancy. As part of our commitment to Inclusivity, we offer reasonable adjustments "
        "throughout the recruitment process for individuals who may require them. If you feel you need any adjustments, "
        "please contact SCE.GroupHR@sample-f10656.test."
    )
    ev_adj = extract_structured_application_evidence(adj_text, source_url="https://careers.sample-f10656.test/jobs/1")
    assert not ev_adj.primary_email
    assert any(r.get("email") == "SCE.GroupHR@sample-f10656.test" for r in ev_adj.rejected_emails)

    disclaimer_text = (
        "Disclaimer from the Paper Loom bulletin editor. "
        "Report fraud to the editorial desk at fraud@paper-loom.test."
    )
    assert not extract_structured_application_evidence(disclaimer_text, source_url="https://paper-loom.test/news").primary_email

    scam_text = (
        "Protect Yourself from Recruitment Scams: AmgenCareers@sample-ab4b27.test may appear in scam warnings. "
        "Official communications from Amgen will come from amgen.com."
    )
    assert not extract_structured_application_evidence(scam_text, source_url="https://careers.amgen.com/job").primary_email

    footer_text = "Submit your application online.\nHome News Jobs Events Sponsor About Standards Corrections RSS hello@sample-6e583c.test"
    assert not extract_structured_application_evidence(footer_text, source_url="https://sample-6e583c.test/jobs/1").primary_email

    # Also verify Schema.org JSON-LD list employmentType normalizes cleanly to full_time
    import asyncio as _asyncio

    import httpx as _httpx

    from app.services.discovery.career_page import fetch_and_parse_career_page

    monkeypatch.setattr(
        "app.core.ssrf.socket.getaddrinfo",
        lambda host, port, *a, **kw: [(2, 1, 6, "", ("93.184.216.34", 0))],
    )
    html_jsonld = (
        '<html><head><script type="application/ld+json">'
        '{"@type":"JobPosting","title":"Data Analyst","employmentType":["FULL_TIME"],'
        '"hiringOrganization":{"name":"Acme Gulf"},"jobLocation":{"address":{"addressLocality":"Dubai","addressCountry":"AE"}}}'
        "</script></head><body><p>Hiring Data Analyst in Dubai UAE. Send your CV to jobs@sample-ec069c.test.</p></body></html>"
    )
    mock_tx = _httpx.MockTransport(lambda req: _httpx.Response(200, text=html_jsonld, headers={"content-type": "text/html"}))
    cand, status = _asyncio.run(fetch_and_parse_career_page("https://sample-ec069c.test/jobs/1", transport=mock_tx))
    assert status == "fetched" and cand is not None
    assert cand.employment_type == "full_time"
    assert cand.application_email == "jobs@sample-ec069c.test"


