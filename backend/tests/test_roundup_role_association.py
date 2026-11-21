from dataclasses import replace
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.db.models import Batch, JobPosting
from app.services.batch_runner import execute_batch_pipeline
from app.services.discovery.career_page import (
    clean_and_extract_role_and_employer,
    parse_user_pasted_posting,
    split_clear_multi_role_post,
)
from app.services.eligibility import extract_mandatory_eligibility_requirements
from app.services.verification import verify_discovered_candidate
from tests.test_vnext_acceptance_suite import _seed_vnext_user


def roundup(text, title):
    page = parse_user_pasted_posting(raw_text=text, title=title, company_name="Sample",
                                     source_url="https://board.test/roundup", location_raw="Dubai, UAE")
    return replace(page, source_provider="career_page", posted_at=datetime.now(timezone.utc),
                   date_source="provider_metadata", job_code="PAGE-99",
                   raw_metadata={"corroborated_official": True, "corroboration_url": "https://sample.test/"})


CARD = (
    "Submit your CV to the recruitment email provided in the corresponding vacancy announcement.\n"
    "Verified Application Contact Available in the Supplied Information:\nContact Person: Pat Lee\n"
    "Position: Assistant Project Manager\nEmail: pm@sample.test"
)
LIST = (
    "All applicants must hold PMP certification.\n"
    "1. Technical and Construction Jobs\nThe following 2 positions are available:\nQA/QC Engineer\nHSE Officer\n"
    "2. Corporate Jobs\nThe company is also accepting applications for two corporate positions:\n"
    "HR Manager\nLogistics Coordinator\nHow to Apply\n"
    "Interested candidates can apply by sending an updated CV to the recruitment email address below.\n"
    "Application email: jobs@sample.test\nEmail subject: Use the exact designation you are applying for.\n"
    "For example:\nSubject: Application for QA/QC Engineer\nSubject: Application for Logistics Coordinator\n"
    "The announcement also lists the following HR email addresses:\nother@sample.test"
)


def verify(page):
    return verify_discovered_candidate(page, include_standing_invitations=False, skip_network_dns=True)


def test_numbered_contact_and_repeated_footer_resolve_only_the_named_role():
    text = "\n".join(f"{i}. Project Engineer {i}\nLocation: Abu Dhabi" for i in range(1, 7))
    text += ("\n7. Assistant Project Manager\nLocation: Dubai, UAE\nMinimum 10 years of experience.\n"
             "Application Contact:\nPat Lee\nEmail: pm@sample.test\n8. Commercial Manager\n"
             "Location: Wider GCC Region\nHow to Apply\n" + CARD +
             "\nFor the remaining seven vacancies use the corresponding advertisement.\nAll applicants must speak Arabic.")
    [child] = split_clear_multi_role_post(roundup(text, "8 job vacancies"))
    assert (child.role_extracted, child.location_raw, child.application_email) == ("Assistant Project Manager", "Dubai, UAE", "pm@sample.test")
    assert child.raw_metadata == {"split_role_index": 7}
    assert "Minimum 10 years" in child.jd_text and "must speak Arabic" in child.jd_text
    assert "Wider GCC Region" not in child.jd_text and "Project Engineer 1" not in child.jd_text
    assert child.posted_at is None and child.date_source == "unknown" and not child.job_code
    assert verify(child).status == "verified"

    [conflict] = split_clear_multi_role_post(roundup(text.replace("Email: pm@sample.test\n8.", "Email: other@sample.test\n8."), "8 job vacancies"))
    assert not conflict.application_email and not conflict.role_extracted


def test_designated_list_children_keep_requirements_and_literal_source_titles():
    children = split_clear_multi_role_post(roundup(LIST, "4 job vacancies"))
    assert [c.title for c in children] == ["QA/QC Engineer", "HSE Officer", "HR Manager", "Logistics Coordinator"]
    for c in children:
        assert c.title == c.role_extracted == c.subject_instruction
        assert c.application_email == "jobs@sample.test" and not c.cc_emails
        assert "other@sample.test" not in c.jd_text and "Subject: Application for" not in c.jd_text
        reqs = extract_mandatory_eligibility_requirements(title=c.title, jd_text=c.jd_text)
        assert any(r["level"] == "mandatory" and "PMP" in r["name"] for r in reqs.certification_requirements)
        assert verify(c).status == "verified"


def test_list_context_keeps_conditions_and_separates_engineering_and_administration():
    text = (
        "The company operates across accounting, procurement, construction, oil and gas.\n"
        + LIST.replace("QA/QC Engineer\nHSE Officer", "Project Engineer\nHSE Officer")
        .replace("2. Corporate Jobs", "These positions cover construction project delivery.\nRequirements for all roles:\nValid UAE driving licence required.\n2. Corporate Jobs")
        .replace("How to Apply", "These roles support accounting, purchasing, procurement and logistics.\nHow to Apply")
    )
    children = {p.title: p for p in split_clear_multi_role_post(roundup(text, "Current Jobs"))}
    engineer, admin = children["Project Engineer"], children["Logistics Coordinator"]
    assert "construction project delivery" in engineer.jd_text
    assert "purchasing, procurement" not in engineer.jd_text
    assert "construction project delivery" not in admin.jd_text
    assert "purchasing, procurement" in admin.jd_text
    for c in children.values():
        assert "All applicants must hold PMP certification" in c.jd_text
        assert "Valid UAE driving licence required." in c.jd_text
        assert "oil and gas" not in c.jd_text


@pytest.mark.parametrize("text", [
    "List Of Vacant Positions (New Updates)\nMixologist\nCommis II\nHow to Apply\nEmail CV: jobs@sample.test",
    LIST.replace("following 2", "following 5"),
    LIST.replace("Use the exact designation you are applying for.", "See the example subjects below."),
    LIST + "\nA driving licence is mandatory for some positions.",
    LIST + "\nApply to other@sample.test instead.",
    LIST + "\nSubject: USE THIS FOR EVERYONE",
])
def test_unresolved_roundup_stays_non_actionable_even_with_legacy_headline_recipient(text):
    parent = replace(roundup(text, "Current Openings"), application_email="jobs@sample.test",
                     evidence_quote="Email CV: jobs@sample.test", role_extracted="Current Openings")
    [result] = split_clear_multi_role_post(parent)
    assert result.is_multi_role_digest and not result.application_email and not result.role_extracted
    assert verify(result).status == "skipped"
    assert not clean_and_extract_role_and_employer(result.title, text, result.source_url)[0]


def test_pipeline_keeps_role_identity_before_recipient_dedup(db_session):
    user, _, pref, *_ = _seed_vnext_user(db_session)
    pref.unknown_dates_policy = "skip"
    batch = Batch(user_id=user.id, mode="dry_run", budget_config_json={"max_ai_calls": 0})
    db_session.add(batch)
    db_session.commit()
    page = roundup(LIST, "4 job vacancies")
    execute_batch_pipeline(db_session, batch=batch, candidate_overrides=[page, page], skip_network_dns=True)
    rows = db_session.scalars(select(JobPosting).where(JobPosting.batch_id == batch.id)).all()
    assert {row.role_extracted for row in rows} == {"QA/QC Engineer", "HSE Officer", "HR Manager", "Logistics Coordinator"}
    assert len(rows) == len({row.canonical_url_hash for row in rows}) == len({row.normalized_vacancy_key for row in rows}) == 4
    assert batch.duplicates_removed_count == 4
    for row in rows:
        assert row.posted_at is None and row.subject_instruction == row.role_extracted
        assert row.corroboration_status != "corroborated_authorized" and not row.corroboration_url
