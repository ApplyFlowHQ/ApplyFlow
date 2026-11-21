"""Tests for international mandatory eligibility extraction, pre-CV/pre-send gating, and Batch 3 regression."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.db.base import utcnow
from app.db.models import (
    Application,
    Batch,
    CandidateProfile,
    CvDocument,
    CvExtractedFact,
    CvVersion,
    InstallationConfig,
    JobMatch,
    JobPosting,
    MailboxConnection,
    OutboxEntry,
    SearchPreference,
    User,
)
from app.services.batch_runner import (
    audit_existing_eligible_applications,
    execute_batch_pipeline,
    reevaluate_batch_eligibility,
)
from app.services.discovery.base import DiscoveredCandidate
from app.services.eligibility import (
    assess_posting_mandatory_eligibility,
    extract_mandatory_eligibility_requirements,
    validate_candidate_mandatory_eligibility,
)
from app.services.mailbox.base import HandoffOutcome, OutboundEmailMessage, SendResult
from app.services.matching import evaluate_job_against_cv
from app.services.outbox import claim_next_outbox_entry, enqueue_application_for_send, process_outbox_entry
from app.services.storage import compute_sha256, get_storage_adapter
from tests.conftest import build_pdf_bytes

ELIGIBILITY_POSTING = """Paper Lantern Works seeks a Collection Officer / Accounts Receivable Executive.
Coordinate accounts receivable, AMC invoicing and ERP records in Abu Dhabi.
Nationality: UAE Nationals only.
Excellent Arabic and English communication skills.
UAE driving licence preferred.
Send your updated CV to info@fixture-b9e140.test.
"""


def _seed_candidate(
    db,
    *,
    email: str = "cand@example.org",
    full_name: str = "Ravi Kumar",
    res_country: str = "AE",
    res_city: str = "Dubai",
    work_auth: list[str] | None = None,
    nationalities: list[str] | None = None,
    permits: list[str] | None = None,
    certs: list[str] | None = None,
    langs: list[dict[str, str]] | None = None,
    requires_sponsorship: bool | None = True,
    willing_to_relocate: bool = True,
    notice_days: int | None = 30,
    exp_months: int = 84,
    cv_lang: str = "en",
    cv_text: str = "Ravi Kumar\nCAFM Administrator IBM Maximo Power BI\nLanguages: Tamil (Native), English (Fluent), Arabic (Beginner)",
):
    user = User(email=email, email_normalized=email, full_name=full_name, timezone="Asia/Dubai", email_verified_at=utcnow())
    db.add(user)
    db.flush()

    profile = CandidateProfile(
        user_id=user.id,
        full_name=full_name,
        contact_email=email,
        residence_country_code=res_country,
        residence_city=res_city,
        work_authorizations_json=work_auth if work_auth is not None else ["AE"],
        nationalities_json=nationalities if nationalities is not None else [],
        residency_permits_json=permits if permits is not None else [],
        confirmed_certifications_json=certs if certs is not None else [],
        confirmed_languages_json=langs if langs is not None else [],
        requires_sponsorship=requires_sponsorship,
        willing_to_relocate=willing_to_relocate,
        notice_period_days=notice_days,
        confirmed_at=utcnow(),
        signature_text=f"Regards,\n{full_name}",
    )
    pref = SearchPreference(
        user_id=user.id,
        target_titles_json=["CAFM Administrator", "Facilities Coordinator", "Collection Officer"],
        target_locations_json=[{"country_code": res_country, "city": res_city, "label": f"{res_city}, {res_country}"}],
        allow_relocation=willing_to_relocate,
        min_match_score=40,
        max_experience_gap_years=2,
        unknown_dates_policy="allow_neutral",
    )
    doc = CvDocument(user_id=user.id, variant_label="Primary")
    db.add_all([profile, pref, doc])
    db.flush()

    pdf_bytes = build_pdf_bytes(cv_text.splitlines())
    sha = compute_sha256(pdf_bytes)
    storage_key = f"users/{user.id}/cvs/{doc.id}/v1.pdf"
    get_storage_adapter().put_bytes(storage_key, pdf_bytes, "application/pdf")

    ver = CvVersion(
        cv_document_id=doc.id,
        user_id=user.id,
        version_number=1,
        original_filename="cv.pdf",
        mime_type="application/pdf",
        size_bytes=len(pdf_bytes),
        sha256=sha,
        storage_key=storage_key,
        parse_status="parsed",
        detected_language=cv_lang,
        extracted_text=cv_text,
        total_experience_months=exp_months,
    )
    db.add(ver)
    db.flush()
    doc.active_version_id = ver.id

    lang_title = ", ".join(f"{x['language']} ({x['level']})" for x in (langs or [])) or "English (Fluent), Arabic (Beginner)"
    facts = [
        CvExtractedFact(
            cv_version_id=ver.id,
            user_id=user.id,
            category="tool",
            title="IBM Maximo",
            value="IBM Maximo",
            evidence_quote="CAFM Administrator IBM Maximo Power BI",
            confidence="evidenced",
        ),
        CvExtractedFact(
            cv_version_id=ver.id,
            user_id=user.id,
            category="language",
            title=lang_title,
            value=lang_title,
            evidence_quote=lang_title,
            confidence="evidenced",
        ),
    ]
    db.add_all(facts)
    db.commit()
    return user, profile, pref, ver, facts


def test_batch3_uae_nationals_only_rejected_before_cv_selection_and_positive_case(db_session):
    now = datetime.now(timezone.utc)
    # 1. Non-national candidate (unconfirmed nationalities=[])
    user, profile, pref, _, _ = _seed_candidate(db_session, nationalities=[])

    cand = DiscoveredCandidate(
        source_provider="tavily",
        source_url="https://fictional-studio.test/jobs/sample-04ef17c8",
        source_type="job_post",
        title="#wearehiring #uaejobs #emiratitalent #emiratisation #facilitiesmanagement | Paper Lantern Facility Management",
        company_name="Paper Lantern Facility Management",
        company_domain="www.linkedin.com",
        jd_text=ELIGIBILITY_POSTING,
        location_raw="Abu Dhabi, United Arab Emirates",
        retrieved_at=now,
        posted_at=now - timedelta(days=2),
        application_email="info@fixture-b9e140.test",
        subject_instruction="UAE National – [Position Applied For] [...]",
        evidence_quote="Send your updated CV to info@fixture-b9e140.test.",
    )

    b = Batch(user_id=user.id, mode="dry_run")
    db_session.add(b)
    db_session.commit()

    execute_batch_pipeline(db_session, batch=b, candidate_overrides=[cand], skip_network_dns=True)

    assert b.discovered_count == 1
    assert b.eligible_count == 0
    assert b.skipped_count == 1
    assert b.category_counts_json["verified_email_vacancy"] == 0
    assert b.category_counts_json["rejected"] == 1
    assert b.skip_reason_counts_json["NATIONALITY_RESTRICTION"] == 1

    posting = db_session.query(JobPosting).filter(JobPosting.batch_id == b.id).one()
    app_rec = db_session.query(Application).filter(Application.batch_id == b.id).one()
    assert app_rec.transport_status == "skipped"
    assert app_rec.selected_cv_version_id is None  # Rejected BEFORE CV selection & drafting!
    assert posting.primary_skip_code == "NATIONALITY_RESTRICTION"
    assert db_session.query(JobMatch).filter(JobMatch.batch_id == b.id).count() == 0

    # Preferred UAE driving licence MUST NOT be classified as mandatory!
    certs = posting.eligibility_requirements_json["certification_requirements"]
    uae_dl = [c for c in certs if "Driving Licence" in c["name"]]
    assert uae_dl and all(c["level"] == "preferred" for c in uae_dl)

    # 2. Explicitly confirmed incompatible nationality (IN) also fails NATIONALITY_RESTRICTION + MANDATORY_LANGUAGE_MISMATCH on full check
    user2, profile2, pref2, cv2, facts2 = _seed_candidate(
        db_session,
        email="in_cand@example.org",
        nationalities=["IN"],
    )
    ev2 = evaluate_job_against_cv(posting=posting, cv_version=cv2, facts=facts2, pref=pref2, profile=profile2, now_utc=now)
    assert ev2.hard_filter_passed is False
    assert "NATIONALITY_RESTRICTION" in ev2.failure_codes
    assert "MANDATORY_LANGUAGE_MISMATCH" in ev2.failure_codes
    assert "REQUIRED_CERTIFICATION_MISSING" not in ev2.failure_codes

    # 3. Positive case: confirmed UAE National fluent in Arabic & English passes (UAE driving licence was only preferred!)
    user3, profile3, pref3, cv3, facts3 = _seed_candidate(
        db_session,
        email="uae_nat@example.org",
        res_city="Abu Dhabi",
        full_name="Sample Candidate Three",
        nationalities=["AE"],
        langs=[{"language": "ar", "level": "native"}, {"language": "en", "level": "fluent"}],
        requires_sponsorship=False,
        cv_text="Sample Candidate Three\nCollection Officer / Accounts Receivable Executive\n6 years in AMC invoicing and ERP collections.\nLanguages: Arabic (Native), English (Fluent)",
    )
    pref3.min_match_score = 30
    from app.db.models import CvExtractedFact
    skill_fact = CvExtractedFact(
        cv_version_id=cv3.id,
        user_id=user3.id,
        category="skill",
        title="Accounts Receivable",
        value="AMC invoicing",
        evidence_quote="6 years in AMC invoicing and ERP collections.",
        confidence="evidenced",
    )
    db_session.add(skill_fact)
    db_session.commit()
    facts3.append(skill_fact)
    ev3 = evaluate_job_against_cv(posting=posting, cv_version=cv3, facts=facts3, pref=pref3, profile=profile3, now_utc=now)
    assert ev3.eligibility_status == "eligible"
    assert ev3.hard_filter_passed is True
    assert ev3.failure_codes == []


def test_never_infer_citizenship_and_distinguish_preferred_vs_mandatory(db_session):
    # Arabic name, AE residence, AE work auth, Arabic CV, yet nationalities=[] MUST NEVER infer AE citizenship!
    user, profile, pref, cv, facts = _seed_candidate(
        db_session,
        email="ahmed@example.org",
        full_name="Ahmed Al Mansouri",
        res_country="AE",
        work_auth=["AE"],
        nationalities=[],
        cv_lang="ar",
        cv_text="أحمد المنصوري\nمنسق عمليات\nاللغات: العربية (لغة أم), الإنجليزية (ممتاز)",
    )

    mandatory_emirati_posting = JobPosting(
        user_id=user.id,
        batch_id="b-nat",
        canonical_url_hash="h-nat-1",
        content_hash="c-nat-1",
        source_provider="tavily",
        source_url="https://example.org/emirati-only",
        title="Operations Coordinator (Emiratisation)",
        company_name="Gulf Entity",
        jd_text="Restricted to Emirati nationals only. Apply by email to hr@gulf-entity.example.",
        location_raw="Dubai, UAE",
        country_code="AE",
        remote_scope="onsite",
        application_email="hr@gulf-entity.example",
    )
    _, res = assess_posting_mandatory_eligibility(posting=mandatory_emirati_posting, profile=profile, cv_version=cv, facts=facts)
    assert res.passed is False
    assert res.status == "unknown_eligibility"
    assert res.failure_codes == ["NATIONALITY_RESTRICTION"]

    # Contrast: "UAE Nationals preferred" MUST NOT trigger mandatory NATIONALITY_RESTRICTION rejection!
    preferred_only_posting = JobPosting(
        user_id=user.id,
        batch_id="b-pref",
        canonical_url_hash="h-pref-1",
        content_hash="c-pref-1",
        source_provider="tavily",
        source_url="https://example.org/emirati-pref",
        title="Operations Coordinator",
        company_name="Open Gulf Co",
        jd_text="Operations Coordinator in Dubai, UAE. UAE Nationals preferred, expats welcome. UAE driving licence preferred. Send your CV to hr@open-gulf.example.",
        location_raw="Dubai, UAE",
        country_code="AE",
        remote_scope="onsite",
        application_email="hr@open-gulf.example",
    )
    reqs_pref, res_pref = assess_posting_mandatory_eligibility(posting=preferred_only_posting, profile=profile, cv_version=cv, facts=facts)
    assert reqs_pref.nationality_restrictions == []
    assert len(reqs_pref.preferred_nationalities) == 1
    assert res_pref.passed is True
    assert any("Preference only" in note for note in res_pref.preference_notes)


def test_international_eligibility_restrictions_matrix(db_session):
    user, profile, pref, cv, facts = _seed_candidate(
        db_session,
        email="global@example.org",
        res_country="AE",
        res_city="Dubai",
        work_auth=["AE", "SA", "QA", "GB", "US", "DE"],
        nationalities=["IN"],
        permits=[],
        certs=[],
        langs=[{"language": "en", "level": "fluent"}, {"language": "ar", "level": "beginner"}],
        requires_sponsorship=True,
        willing_to_relocate=True,
        notice_days=30,
        exp_months=48,
    )

    # 1. Saudi Saudisation + Transferable Iqama
    reqs_sa = extract_mandatory_eligibility_requirements(
        title="Admin Coordinator",
        jd_text="Saudisation Nitaqat program vacancy for Saudi Nationals only. Must hold transferable Iqama.",
        country_code="SA",
    )
    res_sa = validate_candidate_mandatory_eligibility(requirements=reqs_sa, profile=profile, cv_version=cv, facts=facts)
    assert res_sa.passed is False
    assert {"NATIONALITY_RESTRICTION", "WORK_AUTHORIZATION_MISMATCH"}.issubset(set(res_sa.failure_codes))

    # 2. Qatar Transferable QID with NOC
    reqs_qa = extract_mandatory_eligibility_requirements(
        title="Facilities Planner",
        jd_text="Must have Transferable QID with NOC in Doha, Qatar.",
        country_code="QA",
    )
    res_qa = validate_candidate_mandatory_eligibility(requirements=reqs_qa, profile=profile, cv_version=cv, facts=facts)
    assert res_qa.failure_codes == ["WORK_AUTHORIZATION_MISMATCH"]

    profile.residency_permits_json = ["QA_TRANSFERABLE_QID"]
    res_qa_ok = validate_candidate_mandatory_eligibility(requirements=reqs_qa, profile=profile, cv_version=cv, facts=facts)
    assert res_qa_ok.passed is True

    # 3. UK Local Residency Only (relocation NOT allowed!)
    reqs_uk = extract_mandatory_eligibility_requirements(
        title="Data Analyst",
        jd_text="Candidates currently residing in the UK only. No overseas applicants.",
        location_raw="London, United Kingdom",
        country_code="GB",
    )
    res_uk = validate_candidate_mandatory_eligibility(requirements=reqs_uk, profile=profile, cv_version=cv, facts=facts)
    assert res_uk.failure_codes == ["RESIDENCY_REQUIREMENT"]

    # 4. US Citizens or Green Card + No Visa Sponsorship
    reqs_us = extract_mandatory_eligibility_requirements(
        title="Operations Analyst",
        jd_text="US Citizens or Green Card holders only. No visa sponsorship available.",
        country_code="US",
    )
    res_us = validate_candidate_mandatory_eligibility(requirements=reqs_us, profile=profile, cv_version=cv, facts=facts)
    assert {"NATIONALITY_RESTRICTION", "WORK_AUTHORIZATION_MISMATCH"}.issubset(set(res_us.failure_codes))

    profile_us = CandidateProfile(
        user_id=user.id,
        residence_country_code="US",
        work_authorizations_json=["US"],
        residency_permits_json=["US_GREEN_CARD"],
        requires_sponsorship=False,
    )
    res_us_gc = validate_candidate_mandatory_eligibility(requirements=reqs_us, profile=profile_us, cv_version=cv, facts=facts)
    assert res_us_gc.passed is True

    # 5. Mandatory Certification + Mandatory Experience + Mandatory German + Immediate Joiner
    reqs_multi = extract_mandatory_eligibility_requirements(
        title="Project Manager",
        jd_text=(
            "Requirements:\n"
            "Minimum 7 years experience required.\n"
            "Valid UAE driving licence is mandatory.\n"
            "NEBOSH certification required.\n"
            "Fluent German (C1) required.\n"
            "Immediate joiners only."
        ),
        country_code="AE",
    )
    res_multi = validate_candidate_mandatory_eligibility(requirements=reqs_multi, profile=profile, cv_version=cv, facts=facts)
    assert res_multi.passed is False
    assert {
        "MANDATORY_AVAILABILITY_MISMATCH",
        "REQUIRED_CERTIFICATION_MISSING",
        "MANDATORY_EXPERIENCE_GAP",
        "MANDATORY_LANGUAGE_MISMATCH",
    }.issubset(set(res_multi.failure_codes))


def test_presend_outbox_blocks_mandatory_restriction_even_with_high_fit_score(db_session):
    cfg = InstallationConfig(id="default", live_sending_enabled=True, setup_state="completed")
    db_session.add(cfg)

    user, profile, pref, cv, facts = _seed_candidate(
        db_session,
        email="sendgate@example.org",
        nationalities=["IN"],
    )
    cfg.owner_user_id = user.id
    conn = MailboxConnection(
        user_id=user.id,
        provider="gmail_oauth",
        label="Gmail",
        sender_email="sendgate@example.org",
        granted_scopes_json=["https://www.googleapis.com/auth/gmail.send"],
        status="connected",
    )
    db_session.add(conn)
    batch = Batch(user_id=user.id, mode="live", status="completed")
    db_session.add(batch)
    db_session.flush()

    posting = JobPosting(
        user_id=user.id,
        batch_id=batch.id,
        canonical_url_hash="h-send-1",
        content_hash="c-send-1",
        source_provider="tavily",
        source_url="https://example.org/saudi-nat",
        title="CAFM Administrator",
        company_name="Riyadh Co",
        jd_text="Nationality: Saudi Nationals only. Send CV to hr@riyadh.example.",
        location_raw="Riyadh, Saudi Arabia",
        country_code="SA",
        remote_scope="onsite",
        application_email="hr@riyadh-co.test",
        subject_instruction="Saudi National – CAFM Administrator",
        evidence_quote="Send CV to hr@riyadh-co.test.",
        verification_status="verified",
        discovery_category="verified_email_vacancy",
    )
    db_session.add(posting)
    db_session.flush()

    # Even if an outdated/high AI fit_score=96.0 exists, pre-queue & pre-send MUST reject!
    jm = JobMatch(
        user_id=user.id,
        batch_id=batch.id,
        job_posting_id=posting.id,
        cv_version_id=cv.id,
        hard_filter_passed=True,
        fit_score=96.0,
        decision_reason="Stale AI score",
        is_selected_cv=True,
    )
    db_session.add(jm)
    db_session.flush()

    app_rec = Application(
        user_id=user.id,
        batch_id=batch.id,
        job_posting_id=posting.id,
        job_match_id=jm.id,
        selected_cv_version_id=cv.id,
        frozen_cv_sha256=cv.sha256,
        frozen_cv_filename=cv.original_filename,
        company_name="Riyadh Co",
        role_title="CAFM Administrator",
        recipient_email="hr@riyadh-co.test",
        recipient_normalized="hr@riyadh-co.test",
        subject="Saudi National – CAFM Administrator",
        body_text="Applying.",
        transport_status="draft",
    )
    db_session.add(app_rec)
    db_session.commit()

    # 1. Enqueue gate refuses to queue!
    outbox_entry, err = enqueue_application_for_send(db_session, user=user, batch=batch, app_record=app_rec, conn=conn)
    assert outbox_entry is None
    assert "nationality" in err.lower() or "citizens" in err.lower()
    assert app_rec.transport_status == "skipped"
    assert app_rec.primary_skip_code == "NATIONALITY_RESTRICTION"

    # 2. Simulate pre-existing queued OutboxEntry bypassing enqueue -> process_outbox_entry MUST STILL block send!
    class _SpyTransport:
        def __init__(self) -> None:
            self.sent: list[OutboundEmailMessage] = []

        def send_message(self, msg: OutboundEmailMessage) -> SendResult:
            self.sent.append(msg)
            return SendResult(outcome=HandoffOutcome.PROVIDER_ACCEPTED, rfc_message_id=msg.rfc_message_id)

        def reconcile_sent_message(self, *_a):
            return None

        def poll_inbox(self, cursor: str):
            return [], cursor

    forced_entry = OutboxEntry(
        user_id=user.id,
        application_id=app_rec.id,
        mailbox_connection_id=conn.id,
        kind="initial",
        action_id="forced-act-1",
        rfc_message_id="<forced@example.org>",
        status="queued",
    )
    app_rec.transport_status = "ready"
    from app.services.approval import application_draft_hash

    app_rec.approval_state = "approved"
    app_rec.approved_content_hash = application_draft_hash(app_rec)
    db_session.add(forced_entry)
    db_session.commit()

    leased = claim_next_outbox_entry(db_session, worker_id="w-test", outbox_id=forced_entry.id)
    assert leased is not None
    spy = _SpyTransport()
    status_out = process_outbox_entry(db_session, entry=leased, transport_override=spy, ignore_window_for_test=True)
    assert status_out == "skipped"
    assert spy.sent == []
    assert app_rec.primary_skip_code == "NATIONALITY_RESTRICTION"


def test_reevaluate_batch3_remediation_and_audit(db_session):
    user, _, _, cv, _ = _seed_candidate(db_session, nationalities=[])
    b = Batch(user_id=user.id, mode="dry_run", status="completed", discovered_count=1, eligible_count=1, skipped_count=0, drafted_count=1)
    db_session.add(b)
    db_session.flush()

    posting = JobPosting(
        user_id=user.id,
        batch_id=b.id,
        canonical_url_hash="h-b3",
        content_hash="c-b3",
        source_provider="search_excerpt",
        source_url="https://www.linkedin.com/posts/alghurairfm_wearehiring",
        title="#wearehiring #emiratisation | Paper Lantern Facility Management",
        company_name="Paper Lantern Facility Management",
        jd_text=ELIGIBILITY_POSTING,
        location_raw="Abu Dhabi, United Arab Emirates",
        country_code="AE",
        remote_scope="onsite",
        application_email="job@fixture-8d6e26.test",
        subject_instruction="UAE National – [Position Applied For]",
        evidence_quote="Interested? Send your CV to job@fixture-8d6e26.test.",
        verification_status="verified",
        discovery_category="verified_email_vacancy",
    )
    db_session.add(posting)
    db_session.flush()

    app_rec = Application(
        user_id=user.id,
        batch_id=b.id,
        job_posting_id=posting.id,
        selected_cv_version_id=cv.id,
        company_name="Paper Lantern Facility Management",
        role_title=posting.title,
        recipient_email="job@fixture-8d6e26.test",
        recipient_normalized="job@fixture-8d6e26.test",
        subject="UAE National – [Position Applied For]",
        body_text="Draft.",
        transport_status="draft",
        discovery_category="verified_email_vacancy",
    )
    db_session.add(app_rec)
    db_session.commit()

    # Audit detects violation BEFORE remediation
    pre_audit = audit_existing_eligible_applications(db_session)
    assert len(pre_audit) == 1
    assert pre_audit[0]["eligible"] is False
    assert "NATIONALITY_RESTRICTION" in pre_audit[0]["failure_codes"]

    # Rerun Batch 3 eligibility without sending emails
    summary = reevaluate_batch_eligibility(db_session, batch_id=b.id)
    assert summary["eligible_count"] == 0
    assert summary["skipped_count"] == 1
    assert len(summary["newly_rejected"]) == 1
    assert summary["newly_rejected"][0]["primary_skip_code"] == "NATIONALITY_RESTRICTION"

    db_session.refresh(app_rec)
    assert app_rec.transport_status == "skipped"
    assert app_rec.selected_cv_version_id is None
    assert app_rec.primary_skip_code == "NATIONALITY_RESTRICTION"
    assert audit_existing_eligible_applications(db_session) == []
