from app.db.base import utcnow
from app.db.models import JobPosting
from app.services.matching import _extract_jd_keywords, evaluate_job_against_cv
from tests.test_csv_audit_remediation import _seed_cafm_user


def test_department_description_does_not_inherit_an_adjacent_skills_requirement():
    text = (
        "These roles support procurement, logistics and office administration operations. "
        "Candidates with strong communication skills may consider applying."
    )
    mandatory, _ = _extract_jd_keywords(text)
    assert "Procurement" not in mandatory
    assert "Logistics" not in mandatory


def test_a_later_explicit_requirement_survives_an_earlier_department_description():
    text = "The company has a logistics department. Experience in logistics is required."
    mandatory, preferred = _extract_jd_keywords(text)
    assert "Logistics" in mandatory
    assert "Logistics" not in preferred


def test_application_mailbox_is_not_a_recruitment_skill():
    assert _extract_jd_keywords("Send an updated CV to the recruitment email address below.") == ([], [])


def test_unmatched_preference_cannot_raise_a_cv_score(db_session):
    _, profile, pref, cv, *_ = _seed_cafm_user(db_session)
    posting = JobPosting(title="Project Engineer", role_extracted="Project Engineer",
                         jd_text="Coordinate site work.", location_raw="Dubai, UAE",
                         source_url="https://acme.test/job", country_code="AE")
    def evaluate():
        return evaluate_job_against_cv(posting=posting, cv_version=cv, facts=[], pref=pref,
                                       profile=profile, now_utc=utcnow())

    baseline = evaluate()
    posting.jd_text += " Logistics preferred."
    optional = evaluate()
    assert not optional.verified_skills
    assert optional.score_components["tools_and_skills"] == baseline.score_components["tools_and_skills"]


def test_standing_invitation_scores_specialization_and_ignores_candidate_name(db_session):
    _, profile, pref, cv, *_ = _seed_cafm_user(db_session)
    profile.full_name = "Lumen Quill"
    cv.original_filename = "Lumen_Quill_cartography.pdf"
    cv.extracted_text = "Lumen Quill. Cartography."
    pref.target_titles_json = []
    pref.role_families_json = []
    posting = JobPosting(title="Career opportunities", is_standing_invitation=True,
                         location_raw="Dubai, UAE", country_code="AE",
                         source_url="https://example.test/careers")

    def duties(text):
        posting.jd_text = text
        result = evaluate_job_against_cv(posting=posting, cv_version=cv, facts=[], pref=pref,
                                         profile=profile, now_utc=utcnow())
        return result.score_components["duties_and_title"]

    baseline = duties("Submit an application.")
    assert duties("Submit an application to Lumen Quill.") == baseline
    assert duties("Submit a cartography application.") > baseline
