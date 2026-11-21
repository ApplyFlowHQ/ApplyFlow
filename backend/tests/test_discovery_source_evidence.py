import time
from datetime import datetime, timezone

import httpx
import pytest
from bs4 import BeautifulSoup

from app.services.discovery.base import SearchProviderOutcome
from app.services.discovery.brave import BraveSearchProvider
from app.services.discovery.career_page import (
    _extract_dates_from_html_or_text,
    clean_and_extract_role_and_employer,
    extract_structured_application_evidence,
    parse_user_pasted_posting,
    split_clear_multi_role_post,
)
from app.services.discovery.query_builder import build_email_application_discovery_plan, format_query_for_provider
from app.services.discovery.tavily import TavilySearchProvider
from app.services.research_planner import (
    StagedBudgetConfig,
    StagedBudgetUsage,
    classify_source_verification_tier,
    run_employer_followup_and_corroboration,
)
from app.services.verification import verify_discovered_candidate


def test_updated_cv_instruction_keeps_role_subject_and_ignores_fraud_address():
    source = ('How do I apply for the Sales & Projects Coordinator position?\n'
              'Send your updated CV to Recruitment@employer.test with the subject line "Sales & Projects Coordinator".\n'
              'Report fraud to help@publisher.test.')
    evidence = extract_structured_application_evidence(source)
    assert evidence.primary_email.casefold() == "recruitment@employer.test"
    assert evidence.subject_instruction == "Sales & Projects Coordinator"
    assert not evidence.cc_emails
    assert not extract_structured_application_evidence("Do not send your updated CV to help@publisher.test.").primary_email


def test_republished_roundup_does_not_seed_publisher_as_hiring_employer():
    _, employer, purported, _, publisher = clean_and_extract_role_and_employer(
        "FPA Recruitment 2026 | Top 8 Engineering & Construction Jobs in Dubai & Abu Dhabi",
        "Eight roles advertised across the UAE.", "https://dmc-education.com/jobs/fpa", og_site_name="DMC EDUCATION",
    )
    assert publisher == "DMC Education"
    assert not employer and not purported


def test_subject_after_scoped_mailbox_preserves_source_spelling():
    first = "Send your CV to data@sample.test, with Senior Data Analyst in the email subject."
    other = "Send your CV to hr@second.test, with Operations in the email subject."
    evidence = extract_structured_application_evidence(first + "\n" + other)
    assert (evidence.primary_email, evidence.subject_instruction) == ("data@sample.test", "Senior Data Analyst")


@pytest.mark.parametrize("text", [
    "Send your CV using the portal. For help email help@corp.test",
    "Apply by email\nFor press enquiries\npress@corp.test",
    "Do not send your CV to careers@corp.test",
    "Submit your application online.\nContact jobs@corp.test",
    "Send your CV to jobs@corp. test",
])
def test_no_recipient_without_direct_application_instruction(text):
    assert not extract_structured_application_evidence(text).primary_email


def test_mailto_is_scoped_to_its_instruction():
    html = ('<p>Send your CV to <a href="mailto:jobs@corp.test">our team</a>.</p>'
            '<footer>Careers <a href="mailto:press@corp.test">Press</a></footer>')
    evidence = extract_structured_application_evidence("", html_text=html)
    assert evidence.primary_email == "jobs@corp.test"
    assert not evidence.is_multi_role_digest
    assert "jobs@corp.test" in evidence.evidence_quote
    assert not extract_structured_application_evidence("", html_text=html[html.index("<footer>"):]).primary_email


def test_verifier_requires_source_support_and_role_scoped_cc():
    source = ("1. Data Analyst\nSend CV to data@corp.test\nCC: lead@corp.test\nSubject: DATA\n"
              "2. HR Officer\nSend CV to hr@corp.test\nCC: other@corp.test\nSubject: HR")
    candidate = parse_user_pasted_posting(raw_text=source, title="Data Analyst", company_name="Corp", location_raw="", source_url="https://corp.test/jobs")
    first, second = split_clear_multi_role_post(candidate)
    assert (first.application_email, first.cc_emails, first.subject_instruction) == ("data@corp.test", ["lead@corp.test"], "DATA")
    assert (second.application_email, second.cc_emails, second.subject_instruction) == ("hr@corp.test", ["other@corp.test"], "HR")
    first.cc_emails.append("other@corp.test")
    result = verify_discovered_candidate(first, include_standing_invitations=True, skip_network_dns=True)
    assert result.status == "verified"
    assert result.normalized_ccs == ["lead@corp.test"]
    first.application_email = "ta@corp.test"
    first.evidence_quote = "Send CV to ta@corp.test"
    assert verify_discovered_candidate(first, include_standing_invitations=True, skip_network_dns=True).primary_skip_code == "RECIPIENT_UNVERIFIED"


def test_modified_and_deadline_dates_do_not_become_publication_dates():
    html = '<meta property="og:updated_time" content="2026-11-10"><time datetime="2026-12-01">Deadline</time>'
    posted, closes, source = _extract_dates_from_html_or_text(BeautifulSoup(html, "html.parser"), "Deadline: 2026-12-01", datetime.now(timezone.utc))
    assert (posted, source) == (None, "unknown")
    assert closes.date().isoformat() == "2026-12-01"
    candidate = parse_user_pasted_posting(raw_text="Posted on 2026-11-03. Closing date: 2026-12-01.", title="", company_name="", location_raw="", source_url="")
    assert candidate.posted_at.date().isoformat() == "2026-11-03"
    assert candidate.date_source == "inline_text"


def test_query_plan_respects_selected_roles_cities_and_languages():
    params = dict(target_titles=["Welder", "Chef"], cv_titles=["Data Analyst"], excluded_keywords=["chef"],
                  role_families=[], target_locations=[{"country_code": "AE", "city": "Al Ain"}],
                  include_standing_invitations=False, search_languages=["ar", "en"], max_queries=10)
    plan = build_email_application_discovery_plan(**params)
    assert plan and {q.title_term for q in plan} == {"Welder"}
    for q in plan:
        for provider in ("brave", "tavily"):
            rendered = format_query_for_provider(q, provider)
            assert "Al Ain" in rendered
            assert all(term not in rendered for term in ("Chef", "Coordinator", "Analyst", "Abu Dhabi"))
        if q.language == "ar":
            assert format_query_for_provider(q, "brave") == q.query
    assert build_email_application_discovery_plan(**{**params, "excluded_keywords": ["chef", "welder"]}) == []
    assert build_email_application_discovery_plan(**{**params, "max_queries": 0}) == []


def test_paste_or_inaccessible_snippet_alone_does_not_authorize_corporate_recipient():
    candidate = parse_user_pasted_posting(raw_text="Send your CV to jobs@corp.test", title="Data Analyst", company_name="Corp", location_raw="", source_url="https://corp.test/job")
    for provider in ("user_pasted", "search_excerpt"):
        candidate.source_provider = provider
        tier, _, _ = classify_source_verification_tier(candidate, discovery_category="verified_email_vacancy", verification_status="verified")
        assert tier == "tier_3_third_party_claim"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_cls", [BraveSearchProvider, TavilySearchProvider])
async def test_rejected_provider_constraints_do_not_retry_unscoped(provider_cls):
    requests = []

    def reject(req):
        requests.append(req)
        return httpx.Response(422)

    provider = provider_cls(api_key="unit-test", transport=httpx.MockTransport(reject))
    outcome = await provider.search("Welder Al Ain", country="AE")
    assert len(requests) == 1
    assert outcome.error and not outcome.candidates


@pytest.mark.asyncio
@pytest.mark.parametrize(("page_body", "confirmed"), [
    ("Data Analyst. General enquiries: jobs@corp.test.", False),
    ("Office Assistant. Send CV to jobs@corp.test.", False),
    ("Data Analyst. Send CV to other@corp.test.", False),
    ("Data Analyst. Send CV to jobs@corp.test.", True),
])
async def test_corroboration_needs_official_vacancy_and_same_recipient(monkeypatch, page_body, confirmed):
    target = parse_user_pasted_posting(title="Data Analyst", company_name="Corp",
                                       source_url="https://board.test/listing", location_raw="Dubai",
                                       raw_text="Data Analyst. Send CV to jobs@corp.test.")
    page = parse_user_pasted_posting(title=page_body.split(".")[0], company_name="Corp",
                                     source_url="https://corp.test/careers/current", location_raw="Dubai",
                                     raw_text=page_body)

    class Provider:
        name = "fixture"

        async def search(self, query, **kwargs):
            return SearchProviderOutcome(self.name, query, candidates=[page])

    async def fetch(url, **kwargs):
        assert url == page.source_url
        return page, "fetched"

    monkeypatch.setattr("app.services.research_planner.fetch_and_parse_career_page", fetch)
    _, updates, sightings, errors = await run_employer_followup_and_corroboration(
        candidates=[target], providers=[Provider()], budget=StagedBudgetConfig(), usage=StagedBudgetUsage(),
        target_country_codes={"AE"}, role_terms=["Data Analyst"], freshness_days=14, started_monotonic=time.monotonic(),
    )
    assert not errors
    assert bool(updates) == sightings[-1]["corroborated"] == confirmed
