"""Comprehensive tests for Email Application Discovery, evidence extraction, categories, skip codes, and cross-batch dedupe."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.db.models import (
    Application,
    Batch,
    CandidateProfile,
    CvDocument,
    CvExtractedFact,
    CvVersion,
    JobPosting,
    SearchPreference,
    User,
)
from app.services.batch_runner import execute_batch_pipeline
from app.services.discovery.base import DiscoveredCandidate, SearchProviderOutcome
from app.services.discovery.brave import BraveSearchProvider
from app.services.discovery.career_page import (
    extract_structured_application_evidence,
    fetch_and_parse_career_page,
)
from app.services.discovery.query_builder import (
    build_email_application_discovery_plan,
    format_query_for_provider,
)
from app.services.discovery.tavily import TavilySearchProvider
from app.services.geo import parse_job_location
from app.services.verification import verify_discovered_candidate


def _seed_cafm_dubai_user(
    db_session,
    *,
    target_locations: list[dict] | None = None,
    include_standing: bool = False,
    unknown_dates_policy: str = "allow_neutral",
    batch_size: int = 10,
) -> tuple[User, SearchPreference, CandidateProfile]:
    user = User(email="cafm.user@example.org", email_normalized="cafm.user@example.org", timezone="Asia/Dubai")
    db_session.add(user)
    db_session.flush()

    profile = CandidateProfile(
        user_id=user.id,
        full_name="Tariq Al-Mansoor",
        contact_email="cafm.user@example.org",
        residence_country_code="AE",
        residence_city="Dubai",
        work_authorizations_json=["AE"],
        willing_to_relocate=True,
        relocation_targets_json=["AE", "SA"],
        signature_text="Best regards,\nTariq Al-Mansoor",
        confirmed_at=datetime.now(timezone.utc),
    )
    db_session.add(profile)

    pref = SearchPreference(
        user_id=user.id,
        target_titles_json=["CAFM Administrator", "CAFM Planner", "Facilities Coordinator"],
        role_families_json=["CAFM & CMMS Management", "Facilities Management"],
        industries_json=["Facilities Management"],
        target_locations_json=target_locations if target_locations is not None else [],
        search_languages_json=["en", "ar"],
        include_standing_career_invitations=include_standing,
        unknown_dates_policy=unknown_dates_policy,
        unknown_location_policy="skip",
        freshness_days=14,
        min_match_score=55,
        batch_size=batch_size,
        max_search_queries_per_batch=6,
    )
    db_session.add(pref)

    doc = CvDocument(user_id=user.id, variant_label="CAFM Primary")
    db_session.add(doc)
    db_session.flush()

    ver = CvVersion(
        cv_document_id=doc.id,
        user_id=user.id,
        version_number=1,
        original_filename="Tariq_CAFM_CV.pdf",
        mime_type="application/pdf",
        size_bytes=1200,
        page_count=1,
        sha256="f" * 64,
        storage_key="users/test/tariq_cafm.pdf",
        parse_status="parsed",
        ocr_status="not_needed",
        detected_language="en",
        extracted_text=(
            "Tariq Al-Mansoor. CAFM Administrator and CAFM Planner with 5 years of experience in Dubai, UAE "
            "using IBM Maximo, Archibus, PPM scheduling, SLA/KPI reporting, Power BI, and SQL."
        ),
        total_experience_months=60,
    )
    db_session.add(ver)
    db_session.flush()
    doc.active_version_id = ver.id

    for cat, title, val, quote in [
        ("experience", "CAFM Administrator", "5 years CAFM Administration in Dubai", "CAFM Administrator (5 years) managing IBM Maximo & PPM"),
        ("skill", "CAFM / CMMS", "CAFM", "Proficient in CAFM & CMMS platforms"),
        ("skill", "IBM Maximo", "IBM Maximo", "IBM Maximo asset & maintenance planning"),
        ("skill", "Power BI", "Power BI", "Power BI KPI & SLA dashboards"),
        ("certification", "FM Cert", "ISO 41001 Facilities", "ISO 41001 Facilities Management certification"),
    ]:
        db_session.add(
            CvExtractedFact(
                cv_version_id=ver.id,
                user_id=user.id,
                category=cat,
                title=title,
                value=val,
                evidence_quote=quote,
            )
        )
    db_session.commit()
    return user, pref, profile


def test_email_application_query_builder_location_and_rotation():
    # UAE resident with empty target_locations must NOT silently fall back to bare "Remote"
    plan_b0 = build_email_application_discovery_plan(
        target_titles=["CAFM Administrator", "CAFM Planner"],
        role_families=["Facilities Management"],
        target_locations=[],
        search_languages=["en", "ar"],
        include_standing_invitations=True,
        residence_country_code="AE",
        residence_city="Dubai",
        work_authorizations=["AE"],
        rotation_offset=0,
        max_queries=6,
    )
    assert len(plan_b0) == 6
    assert all("Remote Worldwide" not in q.query for q in plan_b0)
    assert any(q.country_code == "AE" for q in plan_b0)
    assert any(q.source_group == "social_hiring_posts" for q in plan_b0)
    assert any(q.source_group == "localized" for q in plan_b0)

    # Saudi Arabia selection includes Saudi cities & local Arabic query variants
    plan_sa = build_email_application_discovery_plan(
        target_titles=["Data Analyst"],
        role_families=["Analytics"],
        target_locations=[{"country_code": "SA", "country_name": "Saudi Arabia", "city": ""}],
        search_languages=["en", "ar"],
        include_standing_invitations=False,
        rotation_offset=0,
        max_queries=6,
    )
    assert all(q.country_code == "SA" for q in plan_sa)
    assert any(loc in q.query for q in plan_sa for loc in ("Riyadh", "Jeddah", "Saudi Arabia", "السعودية", "الرياض"))

    # Next batch offset rotates queries and uses distinct combinations
    plan_b1 = build_email_application_discovery_plan(
        target_titles=["CAFM Administrator", "CAFM Planner"],
        role_families=["Facilities Management"],
        target_locations=[{"country_code": "AE", "country_name": "United Arab Emirates", "city": "Dubai"}],
        search_languages=["en"],
        include_standing_invitations=False,
        rotation_offset=3,
        max_queries=6,
    )
    assert [p.query for p in plan_b0] != [p.query for p in plan_b1]
    brave_q = format_query_for_provider(plan_b1[0], "brave")
    assert "OR" in brave_q


def test_real_world_regression_dataset_blocking_and_diagnosis():
    now = datetime.now(timezone.utc)
    uae_codes = {"AE"}

    # Accommodation contacts on an ATS posting are not hiring contacts.
    workday_text = (
        "The Paper Lantern team has an on-site facilities vacancy. Apply remote type On-site. "
        "For an accommodation, email adjustments@paper-lantern.test. "
        "This address is only for an accommodation. Submit applications using the online portal."
    )
    ev_wd = extract_structured_application_evidence(
        workday_text,
        source_url="https://fictional.wd1.myworkdayjobs.com/jobs/sample",
        target_role_terms=["CAFM Administrator", "CAD/CAFM Analyst"],
    )
    assert ev_wd.primary_email == ""
    assert any(r["email"] == "adjustments@paper-lantern.test" for r in ev_wd.rejected_emails)
    assert ev_wd.portal_apply_detected is True

    loc_wd = parse_job_location("", workday_text, title="CAD/CAFM Analyst - Myworkdayjobs.com")
    assert loc_wd.remote_scope != "remote_worldwide"  # Workday "Apply remote type On-site" trap avoided!

    ver_wd = verify_discovered_candidate(
        DiscoveredCandidate(
            source_provider="tavily",
            source_url="https://fictional-studio.test/jobs/sample-b2f1bd9d",
            source_type="job_post",
            title="CAD/CAFM Analyst - Myworkdayjobs.com",
            company_name="Paper Lantern Works",
            company_domain="fictional.wd1.myworkdayjobs.com",
            jd_text=workday_text,
            location_raw="",
            retrieved_at=now,
            portal_apply_detected=True,
            rejected_emails=ev_wd.rejected_emails,
        ),
        include_standing_invitations=False,
        now_utc=now,
        skip_network_dns=True,
        target_country_codes=uae_codes,
        parsed_country_code=loc_wd.country_code,
        parsed_remote_scope=loc_wd.remote_scope,
    )
    assert ver_wd.status == "skipped"
    assert ver_wd.discovery_category == "website_application_only"
    assert ver_wd.primary_skip_code == "ATS_ONLY"
    assert "NO_EMAIL_INVITATION" in ver_wd.skip_codes
    assert "NO_EMAIL_INVITATION" in ver_wd.secondary_skip_codes

    # 2. UK LinkedIn & UK Indeed portal listings -> OUTSIDE_TARGET_COUNTRY + NO_EMAIL_INVITATION
    uk_text = "Fully Remote Jobs in the UK (Within UK ONLY). £28,050 per annum. Apply Now to send your CV to Megan Gale on company site."
    loc_uk = parse_job_location("", uk_text, title="jobsRmine | Fully Remote Jobs in the UK", source_url="https://uk.indeed.com/viewjob?jk=123")
    assert loc_uk.country_code == "GB"
    assert loc_uk.remote_scope == "remote_country"
    ev_uk = extract_structured_application_evidence(uk_text, source_url="https://uk.indeed.com/viewjob?jk=123")
    assert ev_uk.primary_email == ""
    ver_uk = verify_discovered_candidate(
        DiscoveredCandidate(
            source_provider="tavily",
            source_url="https://uk.indeed.com/viewjob?jk=123",
            source_type="job_post",
            title="CAFM Administrator - Stoke-on-Trent",
            company_name="Equans",
            company_domain="uk.indeed.com",
            jd_text=uk_text,
            location_raw="Stoke-on-Trent, England, United Kingdom",
            retrieved_at=now,
            portal_apply_detected=ev_uk.portal_apply_detected,
        ),
        include_standing_invitations=False,
        now_utc=now,
        skip_network_dns=True,
        target_country_codes=uae_codes,
        parsed_country_code="GB",
        parsed_remote_scope="remote_country",
    )
    assert ver_uk.discovery_category == "rejected"
    assert ver_uk.primary_skip_code == "OUTSIDE_TARGET_COUNTRY"
    assert "NO_EMAIL_INVITATION" in ver_uk.skip_codes

    # 3. Malformed space-broken email domain ONLY -> rejected without guessing
    broken_text = "Sample role. Send Your CV To: careers@paper-lantern. net | +1 202 555 0142"
    ev_br = extract_structured_application_evidence(broken_text, source_url="https://www.facebook.com/posts/1")
    assert ev_br.primary_email == ""
    assert any("careers@paper-lantern. net" in r["email"] for r in ev_br.rejected_emails)


@pytest.mark.asyncio
async def test_positive_extraction_unicode_digest_mailto_and_inaccessible_fallback(monkeypatch):
    import socket

    # Simulate public DNS resolution for RFC 2606 .example domains when testing MockTransport offline
    orig_getaddrinfo = socket.getaddrinfo

    def _fake_getaddrinfo(host, port, *args, **kwargs):
        if str(host).endswith(".example"):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port or 443))]
        return orig_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr("app.core.ssrf.socket.getaddrinfo", _fake_getaddrinfo)
    # 1. Mathematical bold Unicode social post + broken email preserved safely without guessing
    fb_snippet = ("Sample facilities vacancy in Dubai. Unconfirmed address: jobs@broken . test. "
                  "Please send your resume at 𝐜𝐚𝐫𝐞𝐞𝐫@𝐬𝐭𝐮𝐝𝐢𝐨.𝐭𝐞𝐬𝐭")
    ev_fb = extract_structured_application_evidence(
        fb_snippet,
        source_url="https://www.facebook.com/groups/example/posts/1000000000000001",
        target_role_terms=["CAFM Administrator", "CAFM Specialist"],
    )
    assert ev_fb.primary_email == "career@studio.test"
    assert "career@studio.test" in ev_fb.evidence_quote.lower()

    # Verify normalized check passes verification cleanly!
    ver_fb = verify_discovered_candidate(
        DiscoveredCandidate(
            source_provider="search_excerpt",
            source_url="https://www.facebook.com/groups/example/posts/1000000000000001",
            source_type="job_post",
            title="ADMIN / CAFM SPECIALIST",
            company_name="Pendulum Motion",
            company_domain="www.facebook.com",
            jd_text=fb_snippet,
            location_raw="Dubai, UAE",
            retrieved_at=datetime.now(timezone.utc),
            application_email=ev_fb.primary_email,
            evidence_quote=ev_fb.evidence_quote,
        ),
        include_standing_invitations=False,
        skip_network_dns=True,
        target_country_codes={"AE"},
        parsed_country_code="AE",
        parsed_remote_scope="onsite",
    )
    assert ver_fb.status == "verified"
    assert ver_fb.discovery_category == "verified_email_vacancy"

    # 2. Multi-role Instagram digest selects CAFM segment email instead of unrelated Indonesian role email
    ig_snippet = (
        "Reception Assistant at Blue Tile Studio. Submit your CV to welcome@blue-tile.test ・ "
        "CAFM & PPM Specialist at Paper Loom. Send your CV to hiring@paper-loom.test ・ "
        "Subject line: Planning Desk PL-42"
    )
    ev_ig = extract_structured_application_evidence(
        ig_snippet,
        source_url="https://fictional-studio.test/jobs/sample-ec9e93a7",
        target_role_terms=["CAFM Administrator", "CAFM Planner"],
    )
    assert ev_ig.primary_email == "hiring@paper-loom.test"
    assert ev_ig.is_multi_role_digest is True
    assert ev_ig.subject_instruction == "Planning Desk PL-42"

    # 3. HTML mailto anchor + CC extraction
    html = """
    <html><head><title>CAFM Planner at Desert Facilities LLC - Dubai, UAE</title>
    <meta property="article:published_time" content="2026-11-17T08:00:00Z" /></head>
    <body>
      <p>We are hiring a CAFM Planner in Dubai, UAE with 3+ years of IBM Maximo experience.</p>
      <p>Please <a href="mailto:talent@desertfm.example">send your CV to talent@desertfm.example</a></p>
      <p>CC: fm-director@desertfm.example</p>
      <p>Subject: REF-CAFM-DXB-2026</p>
    </body></html>
    """
    transport = httpx.MockTransport(
        lambda req: httpx.Response(404) if req.url.path == "/robots.txt" else httpx.Response(200, text=html, headers={"content-type": "text/html"})
    )
    cand, status_str = await fetch_and_parse_career_page(
        "https://desertfm.example/careers/cafm-planner",
        target_role_terms=["CAFM Planner"],
        transport=transport,
    )
    assert status_str == "fetched"
    assert cand is not None
    assert cand.application_email == "talent@desertfm.example"
    assert cand.cc_emails == ["fm-director@desertfm.example"]
    assert cand.subject_instruction == "REF-CAFM-DXB-2026"
    assert cand.company_name == "Desert Facilities LLC"
    assert cand.posted_at is not None

    # 4. Inaccessible HTTP 403 page without usable snippet becomes SOURCE_INACCESSIBLE incomplete record
    blocked_transport = httpx.MockTransport(
        lambda req: httpx.Response(404) if req.url.path == "/robots.txt" else httpx.Response(403, text="Forbidden")
    )
    cand_403, note_403 = await fetch_and_parse_career_page(
        "https://blocked.example/job/99",
        existing_snippet="CAFM Coordinator in Dubai. View job online.",
        seed_title="CAFM Coordinator",
        transport=blocked_transport,
    )
    assert cand_403 is not None
    assert "403" in cand_403.page_inaccessible_reason
    assert note_403.startswith("Source returned HTTP 403")


@pytest.mark.asyncio
async def test_brave_and_tavily_country_and_pagination_params():
    seen_brave: list[httpx.Request] = []
    brave_transport = httpx.MockTransport(
        lambda req: (seen_brave.append(req), httpx.Response(200, json={"web": {"results": []}}))[1]
    )
    b_prov = BraveSearchProvider(api_key="BSA_test", transport=brave_transport)
    await b_prov.search('CAFM Administrator ("send your CV") Dubai', country="AE", page=2, freshness_days=14)
    assert seen_brave[0].url.params["country"] == "ALL"
    assert seen_brave[0].url.params["q"] == 'CAFM Administrator ("send your CV") Dubai UAE'
    assert seen_brave[0].url.params["offset"] == "2"

    seen_tav: list[dict] = []
    import json

    def _tav_handler(req: httpx.Request) -> httpx.Response:
        seen_tav.append(json.loads(req.content.decode("utf-8")))
        return httpx.Response(200, json={"results": []})

    t_prov = TavilySearchProvider(api_key="tvly_test", transport=httpx.MockTransport(_tav_handler))
    await t_prov.search(
        '"CAFM Administrator" "send your CV" Dubai',
        country="AE",
        country_name="United Arab Emirates",
        exclude_domains=["indeed.com"],
    )
    assert seen_tav[0]["country"] == "united arab emirates"
    assert seen_tav[0]["exclude_domains"] == ["indeed.com"]


def test_end_to_end_categories_repeat_batch_dedupe_and_no_fabricated_recipients(db_session):
    user, pref, _ = _seed_cafm_dubai_user(
        db_session,
        target_locations=[{"country_code": "AE", "country_name": "United Arab Emirates", "city": "Dubai"}],
        include_standing=False,
        unknown_dates_policy="skip",
    )
    now = datetime.now(timezone.utc)

    # Batch 1 candidates covering Categories A, B, C, D, E
    candidates_b1 = [
        # Cat A: Verified email vacancy (Dubai, UAE, subject req, CC, valid date) -> ELIGIBLE
        DiscoveredCandidate(
            source_provider="career_page",
            source_url="https://gulf-estates.example/careers/cafm-admin",
            source_type="job_post",
            title="CAFM Administrator",
            company_name="Gulf Estates FM",
            company_domain="gulf-estates.test",
            jd_text=(
                "We are hiring a CAFM Administrator in Dubai, UAE using IBM Maximo and Power BI.\n"
                "Send your CV to jobs@gulf-estates.test\n"
                "CC: fm-lead@gulf-estates.test\n"
                "Subject: REF-CAFM-2026"
            ),
            location_raw="Dubai, UAE",
            retrieved_at=now,
            posted_at=now - timedelta(days=2),
            application_email="jobs@gulf-estates.test",
            cc_emails=["fm-lead@gulf-estates.test"],
            subject_instruction="REF-CAFM-2026",
            evidence_quote="Send your CV to jobs@gulf-estates.test",
            source_priority_tier=2,
        ),
        # Cat B: General CV invitation (skipped when include_standing=False!)
        DiscoveredCandidate(
            source_provider="career_page",
            source_url="https://crescent-fm.test/careers",
            source_type="standing_invitation",
            title="Facilities Careers - General CV Invitation",
            company_name="Crescent FM",
            company_domain="crescent-fm.test",
            jd_text="Spontaneous application: always looking for CAFM & Maximo talent in Dubai, UAE. Send your CV to talent@crescent-fm.test",
            location_raw="Dubai, UAE",
            retrieved_at=now,
            posted_at=now - timedelta(days=1),
            application_email="talent@crescent-fm.test",
            evidence_quote="Send your CV to talent@crescent-fm.test",
            is_standing_invitation=True,
            source_priority_tier=2,
        ),
        # Cat C: Portal application only (same country AE, Workday/portal without email)
        DiscoveredCandidate(
            source_provider="career_page",
            source_url="https://emirates-corp.wd1.myworkdayjobs.com/job/cafm",
            source_type="job_post",
            title="CAFM Coordinator",
            company_name="Emirates Corp",
            company_domain="emirates-corp.wd1.myworkdayjobs.com",
            jd_text="Apply online at our portal. For accommodation only email access@emirates-corp.test.",
            location_raw="Abu Dhabi, UAE",
            retrieved_at=now,
            posted_at=now - timedelta(days=3),
            portal_apply_detected=True,
            rejected_emails=[{"email": "access@emirates-corp.test", "reason": "Accommodation request only"}],
            source_priority_tier=5,
        ),
        # Cat D: Inaccessible source lacking explicit email evidence -> SOURCE_HTTP_403
        DiscoveredCandidate(
            source_provider="search_excerpt",
            source_url="https://protected.test/jobs/55",
            source_type="job_post",
            title="CAFM Planner",
            company_name="Protected Co",
            company_domain="protected.test",
            jd_text="CAFM Planner in Dubai, UAE. View details on site.",
            location_raw="Dubai, UAE",
            retrieved_at=now,
            posted_at=now - timedelta(days=1),
            page_inaccessible_reason="Source returned HTTP 403",
            source_priority_tier=3,
        ),
        # Cat E: UK posting outside target country -> OUTSIDE_TARGET_COUNTRY
        DiscoveredCandidate(
            source_provider="career_page",
            source_url="https://uk.portal.test/cafm-uk",
            source_type="job_post",
            title="CAFM Administrator - London, United Kingdom",
            company_name="UK Facilities",
            company_domain="uk.portal.test",
            jd_text="Within UK ONLY. Send your CV to ukhr@uk.portal.test",
            location_raw="London, United Kingdom",
            retrieved_at=now,
            posted_at=now - timedelta(days=1),
            application_email="ukhr@uk.portal.test",
            evidence_quote="Send your CV to ukhr@uk.portal.test",
            source_priority_tier=5,
        ),
        # Cat E: Unknown publication date when unknown_dates_policy="skip" -> POSTING_DATE_UNVERIFIED
        DiscoveredCandidate(
            source_provider="career_page",
            source_url="https://nodate.test/cafm",
            source_type="job_post",
            title="CAFM Specialist",
            company_name="NoDate FM",
            company_domain="nodate.test",
            jd_text="CAFM Specialist in Dubai, UAE using IBM Maximo. Send your CV to hr@nodate.test",
            location_raw="Dubai, UAE",
            retrieved_at=now,
            posted_at=None,
            application_email="hr@nodate.test",
            evidence_quote="Send your CV to hr@nodate.test",
            source_priority_tier=2,
        ),
    ]

    b1 = Batch(user_id=user.id, mode="dry_run", query_rotation_offset=0)
    db_session.add(b1)
    db_session.commit()

    execute_batch_pipeline(
        db_session,
        batch=b1,
        candidate_overrides=candidates_b1,
        skip_network_dns=True,
    )

    assert b1.discovered_count == 6
    assert b1.eligible_count == 1
    assert b1.skipped_count == 5
    assert b1.strategy_used == "email_application_discovery"
    assert b1.category_counts_json["verified_email_vacancy"] == 1
    assert b1.category_counts_json["verified_general_cv_invitation"] == 1
    assert b1.category_counts_json["website_application_only"] == 1
    assert b1.category_counts_json["incomplete_evidence"] == 1
    assert b1.category_counts_json["rejected"] == 2

    # Check required subject & CC preservation in drafted Application
    eligible_app = db_session.query(Application).filter(Application.batch_id == b1.id, Application.transport_status == "draft").one()
    assert eligible_app.recipient_email == "jobs@gulf-estates.test"
    assert eligible_app.cc_emails_json == ["fm-lead@gulf-estates.test"]
    assert "REF-CAFM-2026" in eligible_app.subject
    assert eligible_app.discovery_category == "verified_email_vacancy"

    # Check skipped postings preserved exact diagnostic codes
    skip_codes_all = {p.primary_skip_code for p in db_session.query(JobPosting).filter(JobPosting.batch_id == b1.id).all() if p.primary_skip_code}
    assert {"ATS_ONLY", "SOURCE_HTTP_403", "OUTSIDE_TARGET_COUNTRY", "POSTING_DATE_UNVERIFIED"}.issubset(skip_codes_all)

    # Batch 2: Turn on general CV invitations & test cross-batch dedupe with a mock search provider
    pref.include_standing_career_invitations = True
    db_session.commit()

    class _MockRotateProvider:
        name = "tavily"

        def __init__(self) -> None:
            self.queries_seen: list[str] = []

        async def search(self, query: str, *, freshness_days: int = 14, max_results: int = 10, **_kw):
            self.queries_seen.append(query)
            return SearchProviderOutcome(
                provider_name=self.name,
                query=query,
                candidates=[
                    # Exact duplicate from Batch 1 -> must be deduplicated!
                    DiscoveredCandidate(
                        source_provider=self.name,
                        source_url="https://emirates-corp.wd1.myworkdayjobs.com/job/cafm",
                        source_type="job_post",
                        title="CAFM Coordinator",
                        company_name="Emirates Corp",
                        company_domain="emirates-corp.wd1.myworkdayjobs.com",
                        jd_text="Apply online",
                        location_raw="Abu Dhabi, UAE",
                        retrieved_at=now,
                    ),
                    # Fresh Category B standing invitation qualified from snippet if HTTP unavailable
                    DiscoveredCandidate(
                        source_provider=self.name,
                        source_url="https://new-saudi-fm.test/careers",
                        source_type="standing_invitation",
                        title="Careers - Saudi Facilities Management",
                        company_name="Saudi FM Co",
                        company_domain="new-saudi-fm.test",
                        jd_text="We are always looking for CAFM Administrator talent in Dubai, UAE. Send your CV to careers@new-saudi-fm.test",
                        location_raw="Dubai, UAE",
                        retrieved_at=now,
                        posted_at=now - timedelta(days=1),
                    ),
                ],
            )

    mock_prov = _MockRotateProvider()
    b2 = Batch(user_id=user.id, mode="dry_run", query_rotation_offset=1)
    db_session.add(b2)
    db_session.commit()

    execute_batch_pipeline(
        db_session,
        batch=b2,
        search_providers_override=[mock_prov],
        skip_network_dns=True,
        transport_override=httpx.MockTransport(lambda _r: httpx.Response(404)),
    )

    assert b2.metrics_json["search_queries_executed"] == 6
    assert len(mock_prov.queries_seen) == sum(
        b2.metrics_json[key] for key in ("search_queries_executed", "followup_queries_executed", "corroboration_queries_executed")
    )
    assert b2.duplicates_removed_count >= 1
    assert b2.eligible_count == 1
    assert b2.category_counts_json["verified_general_cv_invitation"] == 1
    b2_app = db_session.query(Application).filter(Application.batch_id == b2.id, Application.transport_status == "draft").one()
    assert b2_app.recipient_email == "careers@new-saudi-fm.test"



