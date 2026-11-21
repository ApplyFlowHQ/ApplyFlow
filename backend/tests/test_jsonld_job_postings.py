import json
from datetime import datetime, timezone

import pytest

from app.services.discovery import career_page
from app.services.research_planner import restore_candidate, serialize_candidate

URL = "https://publisher.test/dubai/jobs"
COMMON = "<p>Send your CV to general@publisher.test</p><p>Subject: COMMON</p>"


async def _fetch(monkeypatch, scripts, *, body=COMMON):
    html = (
        "<title>Dubai jobs roundup</title>"
        '<meta property="article:published_time" content="2026-01-01">'
        + "".join(f'<script type="application/ld+json">{json.dumps(s)}</script>' for s in scripts)
        + f"<main>{body}</main>"
    )

    async def fetch(url, **kwargs):
        if url.endswith("/robots.txt"):
            return url, 404, "text/plain", b""
        assert url == URL
        return url, 200, "text/html", html.encode()

    monkeypatch.setattr(career_page, "safe_fetch_public_url", fetch)
    candidate, status = await career_page.fetch_and_parse_career_page(
        URL, seed_location="Dubai", seed_title="Common page title",
        seed_posted_at=datetime(2026, 2, 1, tzinfo=timezone.utc), seed_date_source="provider",
    )
    assert status == "fetched"
    return candidate


@pytest.mark.asyncio
async def test_multiple_postings_survive_checkpoint_and_split_with_only_their_own_evidence(monkeypatch):
    first = {
        "@type": "JobPosting", "title": "Data Analyst", "hiringOrganization": {"name": "Acme Labs"},
        "description": "<p>Send your CV to data@acme.test</p><p>CC: lead@acme.test</p><p>Subject: DATA-01</p>",
        "qualifications": "Bachelor degree in statistics", "identifier": {"value": "DATA-" + "1" * 80},
    }
    second = {
        "@type": ["Thing", "JobPosting"], "title": "Facilities Planner", "hiringOrganization": {"name": "Delta Works"},
        "description": "<p>Send your CV to <a href='mailto:fm@delta.test'>the team</a></p><p>Subject: FM-02</p>",
        "qualifications": "Mechanical engineering diploma", "identifier": "FM-02",
        "jobLocation": {"address": {"addressLocality": "Doha", "addressCountry": "QA"}},
        "employmentType": ["PART_TIME"], "datePosted": "2026-03-01", "validThrough": "2026-04-01",
    }
    third = {"@type": "JobPosting", "title": "Quality Inspector", "description": "<p>Apply online.</p>"}
    carrier = await _fetch(monkeypatch, [{"@graph": [first, {"@type": "Organization"}]}, [second, third]])
    # Discovery enriches the carrier before checkpointing. These hints belong to the search hit.
    carrier.location_raw, carrier.posted_at = "Dubai", datetime(2026, 2, 1, tzinfo=timezone.utc)
    carrier.is_standing_invitation = True
    carrier.canonical_url = URL
    restored = restore_candidate(json.loads(json.dumps(serialize_candidate(carrier))))
    children = career_page.split_clear_multi_role_post(restored)
    assert [c.title for c in children] == ["Data Analyst", "Facilities Planner", "Quality Inspector"]
    a, b, c = children
    assert [(n.application_email, n.cc_emails, n.subject_instruction) for n in children] == [
        ("data@acme.test", ["lead@acme.test"], "DATA-01"), ("fm@delta.test", [], "FM-02"), ("", [], ""),
    ]
    assert (a.company_name, a.job_code, a.qualifications) == ("Acme Labs", "DATA-" + "1" * 59, [first["qualifications"]])
    assert (b.company_name, b.job_code, b.qualifications) == ("Delta Works", "FM-02", [second["qualifications"]])
    assert b.posted_at.date().isoformat() == "2026-03-01" and b.closing_at.date().isoformat() == "2026-04-01"
    assert (b.date_source, b.location_raw, b.employment_type) == ("jsonld", "Doha, QA", "part_time")
    for child in (a, c):
        assert (child.location_raw, child.posted_at, child.closing_at, child.date_source) == ("", None, None, "unknown")
    assert "delta.test" not in a.jd_text and "acme.test" not in b.jd_text
    assert all("general@publisher.test" not in n.jd_text and not n.is_standing_invitation for n in children)
    assert len({n.raw_metadata["split_role_index"] for n in children}) == 3
    for child in children:
        assert career_page.split_clear_multi_role_post(restore_candidate(serialize_candidate(child))) == [child]


@pytest.mark.asyncio
async def test_single_job_keeps_visible_page_application_and_metadata(monkeypatch):
    candidate = await _fetch(monkeypatch, [{"@type": "JobPosting", "title": "Data Analyst",
                                            "description": "<p>Analyze production metrics.</p>"}])
    assert career_page.split_clear_multi_role_post(candidate) == [candidate]
    assert (candidate.application_email, candidate.subject_instruction) == ("general@publisher.test", "COMMON")
    assert "Analyze production metrics." in candidate.jd_text
    assert (candidate.location_raw, candidate.date_source) == ("Dubai", "meta_tag")


@pytest.mark.asyncio
async def test_item_list_and_repeated_script_preserve_distinct_jobs(monkeypatch):
    job = {"@type": "JobPosting", "title": "Payroll Officer", "description": "Send CV to payroll@acme.test"}
    other = {"@type": "https://schema.org/JobPosting", "title": "Finance Director"}
    listing = {"@type": "ItemList", "itemListElement": [
        {"@type": "ListItem", "item": job}, {"@type": "ListItem", "item": other},
    ]}
    candidate = await _fetch(monkeypatch, [[{"@graph": [listing]}], job])
    first, second = career_page.split_clear_multi_role_post(candidate)
    assert (first.title, first.application_email) == ("Payroll Officer", "payroll@acme.test")
    assert (second.title, second.jd_text, second.application_email) == ("Finance Director", "", "")
