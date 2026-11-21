"""Hard eligibility filtering, explainable fit scoring, CV selection, and pre-send re-verification."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app.db.models import CandidateProfile, CvExtractedFact, CvVersion, JobPosting, SearchPreference
from app.services.cv_parser import effective_cv_evidence
from app.services.eligibility import assess_posting_mandatory_eligibility
from app.services.geo import ParsedLocationResult, country_in_region, parse_job_location
from app.services.i18n import choose_application_language
from app.services.money import compare_salary_to_minimum
from app.services.pdf_renderer import (
    DOCX_MIME,
    DOCX_ONLY_REASON,
    PDF_MIME,
    ensure_cv_version_pdf_ready,
    requires_docx_only,
)


@dataclass
class MatchEvaluation:
    cv_version_id: str
    hard_filter_passed: bool
    hard_filter_failures: list[str] = field(default_factory=list)
    failure_codes: list[str] = field(default_factory=list)
    eligibility_status: str = "eligible"
    eligibility_check: dict[str, object] = field(default_factory=dict)
    fit_score: float = 0.0
    score_components: dict[str, float] = field(default_factory=dict)
    supporting_facts: list[dict[str, str]] = field(default_factory=list)
    missing_requirements: list[str] = field(default_factory=list)
    decision_reason: str = ""
    application_language: str = "en"
    review_required: bool = False
    review_reasons: list[str] = field(default_factory=list)
    why_this_cv: str = ""
    verified_skills: list[str] = field(default_factory=list)
    required_gaps: list[str] = field(default_factory=list)
    attachment_ready: bool = True


def _add_code(codes: list[str], code: str) -> None:
    if code and code not in codes:
        codes.append(code)


def _extract_required_years_from_jd(jd_text: str) -> int | None:
    matches = re.findall(r"(\d{1,2})\s*\+?\s*(?:years?|yrs?|سنوات|سنة)\s+(?:of\s+)?experience", jd_text or "", re.IGNORECASE)
    if not matches:
        return None
    return max(int(m) for m in matches)


def _extract_jd_keywords(jd_text: str, extra_cv_terms: list[str] | None = None) -> tuple[list[str], list[str]]:
    """Extract mandatory vs preferred requirement tokens cleanly while ignoring nav/sidebar category links."""
    lines = [line.strip() for line in (jd_text or "").splitlines() if line.strip()][:120]
    filtered_lines = [
        ln
        for ln in lines
        if len(ln) <= 320
        and not re.search(
            r"\b(?:browse\s+(?:by\s+)?categor|trending\s+jobs|related\s+jobs|all\s+categories|privacy\s+policy|terms\s+of\s+service|cookie\s+policy|all\s+rights\s+reserved|recruitment\s+team)\b",
            ln,
            re.IGNORECASE,
        )
    ]
    known_terms = [
        "Python", "SQL", "PostgreSQL", "MySQL", "TypeScript", "JavaScript", "React", "FastAPI",
        "Django", "Docker", "Kubernetes", "AWS", "GCP", "Azure", "Excel", "Power BI", "Tableau",
        "Looker", "Pandas", "Spark", "Airflow", "dbt", "Terraform", "Git", "Linux",
        "Java", "C#", "Go", "SAP", "Salesforce", "IFRS", "GAAP", "Financial Modeling",
        "Budgeting", "Forecasting", "Auditing", "Supply Chain", "Procurement", "Logistics",
        "Agile", "Scrum", "HRIS", "Payroll", "Recruitment", "Machine Learning", "Redis", "Celery",
        "CAFM", "CMMS", "IBM Maximo", "Maximo", "Archibus", "Planon", "PPM", "Facilities Management",
        "Maintenance Planning", "SLA", "KPI", "MIS", "Document Control", "QA/QC", "AutoCAD", "ISO 41001",
    ]
    # Generic business nouns should never become hard blockers unless paired with explicit requirement context
    context_required_nouns = {"recruitment", "procurement", "supply chain", "logistics", "auditing", "budgeting", "forecasting", "payroll"}
    seen_terms = {t.lower() for t in known_terms}
    for et in extra_cv_terms or []:
        cleaned = (et or "").strip()
        if 2 <= len(cleaned) <= 40 and cleaned.lower() not in seen_terms:
            seen_terms.add(cleaned.lower())
            known_terms.append(cleaned)

    mandatory: list[str] = []
    preferred: list[str] = []
    req_context_re = re.compile(r"\b(must|mandatory|required|requirements?|skills?|experience\s+(?:in|with)|proficiency|proficient|knowledge\s+of|hands-on)\b", re.IGNORECASE)
    preference_markers = ("preferred", "nice to have", "plus", "bonus", "يفضل")
    sentences = [part.lower() for line in filtered_lines for part in re.split(r"(?<=[.!?;])\s+", line)]

    for term in known_terms:
        pat = re.compile(rf"(?<!\w){re.escape(term)}(?!\w)", re.IGNORECASE)
        mentioned = False
        for sentence in sentences:
            if not pat.search(sentence):
                continue
            if term.lower() == "recruitment" and re.search(r"\brecruitment\b\s*(?:team|agency|department|partner|e-?mail|inbox|@)|send\s+(?:your\s+)?cv\s+to", sentence):
                continue
            mentioned = True
            if any(marker in sentence for marker in preference_markers):
                continue
            if term.lower() not in context_required_nouns or req_context_re.search(sentence):
                mandatory.append(term)
                break
        else:
            if mentioned:
                preferred.append(term)
    return mandatory, preferred


def _check_location_and_authorization(
    *,
    posting: JobPosting,
    parsed_loc: ParsedLocationResult,
    pref: SearchPreference,
    profile: CandidateProfile,
    review_reasons: list[str],
) -> tuple[bool, list[str], list[str]]:
    failures: list[str] = []
    codes: list[str] = []
    allowed_modes = set(pref.allowed_remote_modes_json or [])
    scope = parsed_loc.remote_scope or posting.remote_scope or "unknown"
    if scope in ("unknown", "onsite") and posting.remote_scope not in (None, "", "unknown"):
        scope = posting.remote_scope
    loc_conf = (getattr(posting, "location_confidence", "") or parsed_loc.location_confidence or "unknown").lower()

    if parsed_loc.is_ambiguous:
        if pref.unknown_location_policy == "skip" or (
            pref.unknown_location_policy == "allow_if_remote_worldwide" and scope != "remote_worldwide"
        ):
            candidates_txt = ", ".join(parsed_loc.candidates) if parsed_loc.candidates else parsed_loc.city
            failures.append(f"Ambiguous job location '{posting.location_raw}' ({candidates_txt}); skipped per unknown-location policy")
            _add_code(codes, "JOB_LOCATION_UNKNOWN")
        return len(failures) == 0, failures, codes

    if scope != "unknown" and allowed_modes and scope not in allowed_modes:
        failures.append(f"Work arrangement '{scope}' is excluded by user remote/onsite preferences")
        _add_code(codes, "QUALIFICATION_MISMATCH")

    auth_countries = {c.upper() for c in (profile.work_authorizations_json or []) if c}
    res_country = (profile.residence_country_code or "").upper()
    # Country ccTLD/domain hints cannot pass strict country filters as confirmed location unless text confirms country.
    text_confirmed_country = (
        parsed_loc.country_code.upper()
        if parsed_loc.country_code and parsed_loc.location_confidence in ("explicit", "inferred_text")
        else ""
    )
    job_country = text_confirmed_country or ("" if loc_conf == "domain_hint" else (parsed_loc.country_code or posting.country_code or "").upper())
    target_countries = {
        str(loc.get("country_code", "")).upper()
        for loc in (pref.target_locations_json or [])
        if loc.get("country_code")
    }
    relocation_targets = {c.upper() for c in (profile.relocation_targets_json or []) if c}

    if scope == "remote_worldwide":
        return len(failures) == 0, failures, codes

    if scope == "remote_region":
        eligible_countries = auth_countries | ({res_country} if res_country else set())
        if parsed_loc.allowed_regions:
            if not eligible_countries:
                if pref.unknown_eligibility_policy == "skip":
                    failures.append(f"Job requires remote work authorization in {','.join(parsed_loc.allowed_regions)}, and candidate work authorization is unset")
                    _add_code(codes, "WORK_AUTHORIZATION_MISMATCH")
            elif not any(country_in_region(cc, reg) for cc in eligible_countries for reg in parsed_loc.allowed_regions):
                failures.append(
                    f"Remote region restriction ({','.join(parsed_loc.allowed_regions)}) does not match candidate residence/work authorization"
                )
                _add_code(codes, "WORK_AUTHORIZATION_MISMATCH")
        return len(failures) == 0, failures, codes

    # onsite, hybrid, or remote_country
    if job_country:
        if target_countries and job_country not in target_countries:
            failures.append(f"Job country ({job_country}) is outside user target locations ({','.join(sorted(target_countries))})")
            _add_code(codes, "OUTSIDE_TARGET_COUNTRY")
        if scope in ("onsite", "hybrid", "unknown"):
            city_targets = {
                str(loc.get("city") or "").strip().casefold()
                for loc in (pref.target_locations_json or [])
                if str(loc.get("country_code") or "").upper() == job_country
            }
            # A country-wide selection accepts every city in that country.
            if city_targets and "" not in city_targets:
                city = (parsed_loc.city or posting.city or "").strip()
                if city and city.casefold() not in city_targets:
                    failures.append(f"Job city ({city}, {job_country}) is outside selected cities ({', '.join(sorted(city_targets))})")
                    _add_code(codes, "OUTSIDE_TARGET_CITY")
                elif not city:
                    message = f"City in {job_country} is unknown for a city-specific target"
                    if pref.unknown_location_policy == "require_review":
                        review_reasons.append(message)
                    else:
                        failures.append(message)
                        _add_code(codes, "JOB_LOCATION_UNKNOWN")
        if scope == "remote_country":
            if job_country not in auth_countries and job_country != res_country:
                if pref.unknown_eligibility_policy == "skip" or auth_countries or res_country:
                    failures.append(f"Remote-within-country ({job_country}) requires authorization/residence in {job_country}")
                    _add_code(codes, "OUTSIDE_TARGET_COUNTRY" if (auth_countries or res_country) and not target_countries else "WORK_AUTHORIZATION_MISMATCH")
        else:
            has_auth = job_country in auth_countries
            can_relocate = bool(profile.willing_to_relocate and pref.allow_relocation and (not relocation_targets or job_country in relocation_targets))
            if not has_auth and not can_relocate:
                if pref.unknown_eligibility_policy == "skip" or auth_countries or res_country:
                    failures.append(f"Candidate lacks work authorization or confirmed relocation willingness for {job_country}")
                    if not target_countries and res_country and job_country != res_country:
                        _add_code(codes, "OUTSIDE_TARGET_COUNTRY")
                    else:
                        _add_code(codes, "WORK_AUTHORIZATION_MISMATCH")
    else:
        if pref.unknown_location_policy == "skip" or target_countries:
            reason = (
                "Job location only inferred from domain hint without confirmed text location"
                if loc_conf == "domain_hint"
                else "Job country/location could not be determined"
            )
            failures.append(reason)
            _add_code(codes, "JOB_LOCATION_UNKNOWN")

    return len(failures) == 0, failures, codes


def evaluate_job_against_cv(
    *,
    posting: JobPosting,
    cv_version: CvVersion,
    facts: list[CvExtractedFact],
    pref: SearchPreference,
    profile: CandidateProfile,
    now_utc: datetime | None = None,
    static_fx_rates: dict[str, float] | None = None,
    has_supporting_attachments: bool | None = None,
) -> MatchEvaluation:
    """
    Evaluate one job posting against one immutable CV version.
    Hard eligibility filters run separately and CANNOT be overridden by semantic/fit scores.
    """
    now = now_utc or datetime.now(timezone.utc)
    failures: list[str] = []
    failure_codes: list[str] = []
    review_reasons: list[str] = []
    attachment_ready = True
    jd_low = (posting.jd_text or "").lower()
    title_low = (posting.title or "").lower()
    company_low = (posting.company_name or "").lower()

    # 0. Evidence-based Mandatory Eligibility Validation (nationality, residency, permits, certs, languages, experience, immediacy)
    facts = [f for f in facts if f.cv_version_id == cv_version.id and f.user_id == cv_version.user_id]
    if any(f.confidence == "superseded" for f in facts):
        review_reasons.append("Candidate corrected an extracted fact. Review the original attachment and correction.")
    eligibility_cv, facts = effective_cv_evidence(cv_version, facts)
    elig_reqs, elig_res = assess_posting_mandatory_eligibility(
        posting=posting,
        profile=profile,
        cv_version=eligibility_cv,
        facts=facts,
        check_cv_requirements=True,
    )
    if not posting.eligibility_requirements_json:
        posting.eligibility_requirements_json = elig_reqs.to_dict()
    for ef in elig_res.failures:
        failures.append(ef["reason"])
    for ec in elig_res.failure_codes:
        _add_code(failure_codes, ec)

    # 1. Excluded companies & keywords
    for exc_co in pref.excluded_companies_json or []:
        if exc_co.strip() and exc_co.strip().lower() in company_low:
            failures.append(f"Company '{posting.company_name}' matches excluded company '{exc_co}'")
            _add_code(failure_codes, "QUALIFICATION_MISMATCH")
    for exc_kw in pref.excluded_keywords_json or []:
        if exc_kw.strip() and (exc_kw.strip().lower() in title_low or exc_kw.strip().lower() in jd_low):
            failures.append(f"Posting contains excluded keyword '{exc_kw}'")
            _add_code(failure_codes, "QUALIFICATION_MISMATCH")

    # 2. Freshness & dates policy
    if posting.posted_at:
        posted = posting.posted_at if posting.posted_at.tzinfo else posting.posted_at.replace(tzinfo=timezone.utc)
        if posted < now - timedelta(days=pref.freshness_days):
            failures.append(f"Posting age exceeds freshness limit ({pref.freshness_days} days)")
            _add_code(failure_codes, "EXPIRED_OR_STALE")
    elif pref.unknown_dates_policy == "skip":
        failures.append("Publication date unknown and unknown-dates policy is set to skip")
        _add_code(failure_codes, "POSTING_DATE_UNVERIFIED")

    # 3. Location, remote scope, and work authorization
    parsed_loc = parse_job_location(posting.location_raw, posting.jd_text, title=posting.title, source_url=posting.source_url)
    loc_ok, loc_failures, loc_codes = _check_location_and_authorization(
        posting=posting,
        parsed_loc=parsed_loc,
        review_reasons=review_reasons,
        pref=pref,
        profile=profile,
    )
    failures.extend(loc_failures)
    for lc in loc_codes:
        _add_code(failure_codes, lc)

    # 4. Employment type & hours
    def _norm_emp(val: str) -> str:
        s = str(val or "").strip().strip("[]'\" ")
        s = re.sub(r"[\s\-]+", "_", s.lower())
        return "full_time" if s in ("fulltime", "full_time") else ("part_time" if s in ("parttime", "part_time") else s)

    allowed_emp = {_norm_emp(e) for e in (pref.employment_types_json or []) if _norm_emp(e)}
    raw_emp_str = (posting.employment_type or "unknown").strip()
    if raw_emp_str.startswith("[") and raw_emp_str.endswith("]"):
        raw_parts = [p.strip(" '\"") for p in raw_emp_str[1:-1].split(",") if p.strip(" '\"")]
        emp_candidates = [_norm_emp(p) for p in raw_parts if _norm_emp(p)]
    else:
        emp_candidates = [_norm_emp(raw_emp_str)]
    emp_type = emp_candidates[0] if emp_candidates else "unknown"
    if allowed_emp and emp_type and emp_type != "unknown" and not any(ec in allowed_emp for ec in emp_candidates):
        failures.append(f"Employment type '{emp_type}' not in preferred types")
        _add_code(failure_codes, "QUALIFICATION_MISMATCH")
    if pref.max_weekly_hours and posting.weekly_hours and posting.weekly_hours > pref.max_weekly_hours:
        failures.append(f"Stated hours ({posting.weekly_hours}/wk) exceed max weekly hours ({pref.max_weekly_hours})")
        _add_code(failure_codes, "QUALIFICATION_MISMATCH")

    # 5. Salary policy
    if pref.min_salary_amount is not None:
        sal_cmp = compare_salary_to_minimum(
            job_min=posting.salary_min,
            job_max=posting.salary_max,
            job_currency=posting.salary_currency,
            job_period=posting.salary_period,
            job_weekly_hours=posting.weekly_hours,
            user_min=pref.min_salary_amount,
            user_currency=pref.min_salary_currency or posting.salary_currency,
            user_period=pref.min_salary_period,
            static_fx_rates=static_fx_rates,
        )
        if sal_cmp.status == "below_minimum":
            failures.append(f"Salary below minimum: {sal_cmp.reason}")
            _add_code(failure_codes, "QUALIFICATION_MISMATCH")
        elif sal_cmp.status == "unknown" and pref.unknown_salary_policy == "skip":
            failures.append(f"Salary unknown/unconvertible ({sal_cmp.reason}) and unknown-salary policy is set to skip")
            _add_code(failure_codes, "QUALIFICATION_MISMATCH")
        elif sal_cmp.status == "unknown" and pref.unknown_salary_policy == "require_review":
            review_reasons.append(f"Disclosed salary missing or unconvertible ({sal_cmp.reason}); human review required")

    if pref.unknown_location_policy == "require_review" and (parsed_loc.is_ambiguous or not (parsed_loc.country_code or posting.country_code)):
        review_reasons.append("Job location ambiguity or missing country requires human review")

    # 6. Language support
    app_lang, lang_supported, lang_msg = choose_application_language(
        job_language=posting.detected_language,
        mode=pref.application_language_mode,
        explicit_language=pref.explicit_application_language,
    )
    if not lang_supported:
        failures.append(lang_msg)
        _add_code(failure_codes, "QUALIFICATION_MISMATCH")

    # 7. CV parse status, DOCX-to-PDF conversion readiness & requested attachment format
    if cv_version.parse_status != "parsed":
        failures.append(f"CV version status is '{cv_version.parse_status}', not parsed")
        _add_code(failure_codes, "QUALIFICATION_MISMATCH")
        attachment_ready = False
    reqs = set(posting.attachment_requirements_json or [])
    if requires_docx_only(posting.attachment_requirements_json):
        attachment_ready = False
        failures.append(DOCX_ONLY_REASON)
        _add_code(failure_codes, "ATTACHMENT_NOT_READY")
    elif cv_version.mime_type == DOCX_MIME:
        ok_pdf, pdf_msg = ensure_cv_version_pdf_ready(cv_version)
        if not ok_pdf:
            attachment_ready = False
            failures.append(f"DOCX-to-PDF conversion not ready: {pdf_msg}")
            _add_code(failure_codes, "ATTACHMENT_NOT_READY")
    elif "pdf" in reqs and "docx" not in reqs and cv_version.mime_type != PDF_MIME and cv_version.pdf_conversion_status != "ready":
        attachment_ready = False
        failures.append("Employer strictly requested PDF format, and verified PDF attachment is not ready")
        _add_code(failure_codes, "ATTACHMENT_NOT_READY")

    # Supplementary document checks (portfolio / work sample / references / video)
    extra_doc_mentions: list[str] = []
    if "portfolio" in reqs or re.search(r"\b(?:attach|include|submit|send)\b[^.\n]{0,40}\b(?:portfolio|work\s+samples?)\b", jd_low):
        extra_doc_mentions.append("portfolio / work sample")
    if re.search(r"\b(?:attach|include|submit|provide)\b[^.\n]{0,40}\b(?:reference\s+letters?|two\s+references)\b", jd_low):
        extra_doc_mentions.append("reference letters")
    if re.search(r"\b(?:intro(?:duction)?\s+video|video\s+(?:cv|resume)|loom\s+video)\b", jd_low):
        extra_doc_mentions.append("introduction video")
    if extra_doc_mentions:
        has_portfolio_links = any(lk.get("enabled") and lk.get("url") for lk in (profile.portfolio_links_json or []))
        if not has_supporting_attachments and not (extra_doc_mentions == ["portfolio / work sample"] and has_portfolio_links):
            review_reasons.append(
                f"Posting requests supplementary artifact(s) ({', '.join(extra_doc_mentions)}); confirm attachments before sending"
            )

    # 8. Experience gap check (skip duplicate notice if strict mandatory minimum already flagged)
    req_years = _extract_required_years_from_jd(posting.jd_text)
    cv_years = round(eligibility_cv.total_experience_months / 12.0, 2)
    if (
        "MANDATORY_EXPERIENCE_GAP" not in failure_codes
        and req_years is not None
        and (req_years - cv_years) > pref.max_experience_gap_years
    ):
        failures.append(
            f"Required experience ({req_years} yrs) exceeds CV evidenced experience ({cv_years} yrs) by more than {pref.max_experience_gap_years} yrs"
        )
        _add_code(failure_codes, "EXPERIENCE_GAP")

    # --- Explainable Fit Score Calculation (estimated match, never probability of hiring) ---
    cv_text_low = (eligibility_cv.extracted_text or "").lower()
    fact_values = [(f, f.value.lower(), f.title.lower()) for f in facts]
    cv_skill_terms = [f.value.strip() for f in facts if f.category in ("skill", "certification") and len(f.value.strip()) <= 40]

    mandatory_terms, preferred_terms = _extract_jd_keywords(posting.jd_text, extra_cv_terms=cv_skill_terms)
    matched_facts: list[dict[str, str]] = []
    missing_reqs: list[str] = []

    def _find_fact_for_term(term: str) -> CvExtractedFact | None:
        tlow = term.lower()
        for f, vlow, tlow_f in fact_values:
            if re.search(rf"(?<!\w){re.escape(tlow)}(?!\w)", f"{vlow} {tlow_f}"):
                return f
        return None

    matched_mandatory = 0
    for term in mandatory_terms:
        f = _find_fact_for_term(term)
        if f:
            matched_mandatory += 1
            matched_facts.append({"fact_id": f.id, "category": f.category, "term": term, "evidence": f.evidence_quote[:180]})
        elif re.search(rf"(?<!\w){re.escape(term.lower())}(?!\w)", cv_text_low):
            matched_mandatory += 1
            matched_facts.append({"fact_id": "", "category": "cv_text", "term": term, "evidence": f"Mentioned in CV text: {term}"})
        else:
            missing_reqs.append(f"Mandatory: {term}")

    matched_preferred = 0
    for term in preferred_terms:
        f = _find_fact_for_term(term)
        if f:
            matched_preferred += 1
            matched_facts.append({"fact_id": f.id, "category": f.category, "term": term, "evidence": f.evidence_quote[:180]})
        else:
            missing_reqs.append(f"Preferred: {term}")

    # Duties & Title similarity (30 pts) using clean role_extracted and real CV facts
    stop_words = {
        "and", "the", "for", "with", "senior", "junior", "careers", "general",
        "invitation", "posting", "job", "jobs", "hiring", "wearehiring", "vacancy",
        "role", "position", "staff", "work", "company", "team", "instagram", "linkedin", "facebook",
    }
    role_extracted_val = (getattr(posting, "role_extracted", "") or "").strip()
    parsing_ver = getattr(posting, "parsing_version", "") or ""
    v3_evaluated = parsing_ver.startswith("v3") and (
        bool(getattr(posting, "extraction_evidence_json", None))
        or getattr(posting, "vacancy_confidence", "unknown") not in ("", "unknown")
    )
    if role_extracted_val:
        raw_role_for_score = role_extracted_val
    elif v3_evaluated:
        # v3 parser explicitly cleared unverified/hashtag-only title
        raw_role_for_score = ""
    else:
        raw_role_for_score = re.sub(r"#[^\s#]+", " ", posting.title or "")

    role_low = raw_role_for_score.lower()
    title_words = [w for w in re.findall(r"[\w\u0600-\u06FF]{3,}", role_low) if w not in stop_words]
    evidenced_fact_blob = " ".join(
        f"{f.title} {f.value}"
        for f in facts
        if f.category in ("role", "experience", "skill", "project", "achievement", "tool", "certification")
    ).lower()
    cv_name_stem = re.sub(r"[_\-\.]+", " ", (cv_version.original_filename or "").rsplit(".", 1)[0]).lower()
    cv_header_blob = (eligibility_cv.extracted_text or "")[:800].lower()
    cv_own_blob = f"{cv_name_stem} {cv_header_blob} {evidenced_fact_blob}".strip()
    pref_target_blob = (
        " ".join(pref.target_titles_json or []).lower()
        + " "
        + " ".join(pref.role_families_json or []).lower()
    )
    target_blob = f"{cv_own_blob} {pref_target_blob}".strip()
    off_domain_occupation_tokens = {
        "accountant", "accounting", "bookkeeper", "tax", "auditor", "auditing",
        "nurse", "clinical", "pharmacist", "doctor", "dentist", "dental",
        "chef", "cook", "baker", "waiter", "waitress", "steward",
        "driver", "chauffeur", "electrician", "plumber", "carpenter", "mason",
        "welder", "mechanic", "cleaner", "housekeeping", "cashier", "receptionist",
        "teacher", "sales", "telecaller", "commercial", "merchandising",
    }
    has_off_domain_title = (
        not posting.is_standing_invitation
        and any(
            w in off_domain_occupation_tokens
            and not re.search(rf"(?<!\w){re.escape(w)}(?!\w)", pref_target_blob)
            for w in title_words
        )
    )
    if posting.is_standing_invitation and target_blob:
        name_tokens = set(re.findall(r"[^\W_]+", (profile.full_name or "").lower()))
        pref_tokens = [
            w
            for w in re.findall(r"[\w\u0600-\u06FF]{3,}", pref_target_blob)
            if w not in stop_words
        ]
        cv_headline_tokens = [
            w
            for w in re.findall(r"[\w\u0600-\u06FF]{3,}", f"{cv_name_stem} {cv_header_blob[:350]}")
            if w not in stop_words and w not in name_tokens and w not in {"cv", "resume", "page", "pdf", "docx"}
        ]
        combined_spec_tokens = set(pref_tokens) | set(cv_headline_tokens)
        if combined_spec_tokens:
            hits = sum(1 for w in combined_spec_tokens if w in jd_low or w in role_low)
            name_bonus = 4.0 if any(len(w) >= 3 and w in jd_low for w in re.findall(r"[a-z]{3,}", cv_name_stem) if w not in name_tokens and w not in {"cv", "pdf"}) else 0.0
            base_standing = 15.0 if hits > 0 else 10.0
            duties_score = round(min(30.0, base_standing + 12.0 * (hits / max(1, min(len(combined_spec_tokens), 4))) + name_bonus), 1)
        else:
            duties_score = 12.0
    elif title_words:
        # Differentiate CV variants using THIS CV's own headline, filename specialization, and evidenced facts
        own_hits = sum(1 for w in title_words if re.search(rf"(?<!\w){re.escape(w)}(?!\w)", cv_own_blob))
        name_hits = sum(1 for w in title_words if len(w) >= 3 and w in cv_name_stem)
        header_hits = sum(1 for w in title_words if re.search(rf"(?<!\w){re.escape(w)}(?!\w)", cv_header_blob[:350]))
        pref_hits = sum(1 for w in title_words if re.search(rf"(?<!\w){re.escape(w)}(?!\w)", pref_target_blob))
        cv_ratio = own_hits / len(title_words)
        pref_ratio = pref_hits / len(title_words)
        spec_bonus = min(5.0, name_hits * 3.0 + header_hits * 1.0)
        duties_score = round(min(30.0, 17.0 * cv_ratio + 9.0 * pref_ratio + spec_bonus), 1)
        if has_off_domain_title:
            duties_score = min(duties_score, 8.0)
    else:
        duties_score = 0.0

    # Tools & Skills (30 pts): strictly require JD/skill alignment — never grant phantom free bonuses
    if mandatory_terms or matched_preferred:
        mand_ratio = matched_mandatory / max(1, len(mandatory_terms)) if mandatory_terms else 1.0
        pref_ratio = matched_preferred / max(1, len(preferred_terms)) if preferred_terms else 0.7
        if mandatory_terms and matched_mandatory == 0:
            tools_score = 0.0
        else:
            tools_score = round(22.0 * mand_ratio + 8.0 * pref_ratio, 1)
    elif posting.is_standing_invitation:
        tools_score = 16.0 if duties_score >= 15.0 else 10.0
    else:
        # Concise posting without explicit software list: award CV skill overlaps, or neutral alignment only if role matches strongly
        direct_cv_hits = sum(
            1
            for term in cv_skill_terms
            if re.search(rf"(?<!\w){re.escape(term.lower())}(?!\w)", jd_low)
        )
        if direct_cv_hits > 0:
            tools_score = round(min(20.0, 8.0 + 4.0 * direct_cv_hits), 1)
        elif duties_score >= 18.0:
            tools_score = round(min(13.0, duties_score * 0.45), 1)
        else:
            tools_score = 0.0

    # Experience duration (15 pts)
    if req_years is None:
        exp_score = 12.0 if cv_years >= 1.0 else 8.0
    elif cv_years >= req_years:
        exp_score = 15.0
    else:
        exp_score = round(max(0.0, 15.0 * (cv_years / max(1, req_years))), 1)

    # Projects & Achievements (10 pts)
    proj_ach_facts = [f for f in facts if f.category in ("project", "achievement")]
    proj_score = round(min(10.0, len(proj_ach_facts) * 3.5), 1)

    # Qualifications & Certs (10 pts)
    edu_cert_facts = [f for f in facts if f.category in ("education", "certification")]
    qual_score = round(min(10.0, len(edu_cert_facts) * 5.0), 1)

    # Guard against phantom score inflation on irrelevant or off-domain roles
    if has_off_domain_title or (not posting.is_standing_invitation and duties_score < 12.0):
        exp_score = min(exp_score, 5.0)
        proj_score = min(proj_score, 3.0)
        qual_score = min(qual_score, 3.0)
    elif not posting.is_standing_invitation and not matched_facts and duties_score < 10.0:
        exp_score = min(exp_score, 4.0)
        proj_score = min(proj_score, 2.0)
        qual_score = min(qual_score, 2.0)

    # Language & Location bonus (5 pts)
    lang_loc_score = 5.0 if lang_supported and loc_ok else 0.0

    total_score = round(min(100.0, duties_score + tools_score + exp_score + proj_score + qual_score + lang_loc_score), 1)

    # Hard threshold, role-mismatch, & missing mandatory requirement guard
    if has_off_domain_title:
        failures.append(f"Role '{raw_role_for_score}' belongs to an unrelated occupation outside candidate targets/CV")
        _add_code(failure_codes, "ROLE_MISMATCH")
    if mandatory_terms and matched_mandatory == 0:
        failures.append("CV has zero evidenced matches against mandatory technical requirements")
        _add_code(failure_codes, "MISSING_MANDATORY_SKILL")
    if not posting.is_standing_invitation and duties_score == 0.0 and not matched_facts:
        failures.append("Role title and core duties do not align with target roles or evidenced CV experience")
        _add_code(failure_codes, "ROLE_MISMATCH")
    if total_score < pref.min_match_score:
        failures.append(f"Estimated fit score ({total_score}) is below configured threshold ({pref.min_match_score})")
        _add_code(failure_codes, "FIT_THRESHOLD")

    passed = len(failures) == 0 and elig_res.passed and attachment_ready
    verified_skills = sorted({m["term"] for m in matched_facts if m.get("term")})[:12]
    reason = (
        f"Eligible match estimate {total_score}/100 ({len(matched_facts)} evidenced facts matched)"
        if passed
        else f"Skipped: {'; '.join(failures)}"
    )
    why_cv = (
        f"Selected CV scored {total_score}/100 (Duties {duties_score}/30, Tools {tools_score}/30, Exp {exp_score}/15). "
        f"Verified skills: {', '.join(verified_skills[:5]) or 'none identified'}."
    )
    if missing_reqs:
        why_cv += f" Gaps noted: {', '.join(missing_reqs[:3])}."

    return MatchEvaluation(
        cv_version_id=cv_version.id,
        hard_filter_passed=passed,
        hard_filter_failures=failures,
        failure_codes=failure_codes,
        eligibility_status=elig_res.status,
        eligibility_check={
            "status": elig_res.status,
            "failure_codes": elig_res.failure_codes,
            "failures": elig_res.failures,
            "preference_notes": elig_res.preference_notes,
        },
        fit_score=total_score,
        score_components={
            "duties_and_title": duties_score,
            "tools_and_skills": tools_score,
            "experience_duration": exp_score,
            "projects_and_achievements": proj_score,
            "qualifications_and_certs": qual_score,
            "location_and_language": lang_loc_score,
        },
        supporting_facts=matched_facts[:12],
        missing_requirements=missing_reqs[:12],
        decision_reason=reason,
        application_language=app_lang,
        review_required=len(review_reasons) > 0,
        review_reasons=review_reasons,
        why_this_cv=why_cv,
        verified_skills=verified_skills,
        required_gaps=missing_reqs[:12],
        attachment_ready=attachment_ready,
    )


def select_best_cv_for_posting(
    *,
    posting: JobPosting,
    cv_candidates: list[tuple[CvVersion, list[CvExtractedFact]]],
    pref: SearchPreference,
    profile: CandidateProfile,
    now_utc: datetime | None = None,
    static_fx_rates: dict[str, float] | None = None,
    has_supporting_attachments: bool | None = None,
) -> tuple[MatchEvaluation | None, list[MatchEvaluation]]:
    """
    Evaluate all non-archived active CV versions and choose the highest-scoring eligible one.
    Rejects profile-level mandatory eligibility restrictions BEFORE selecting or scoring CVs.
    """
    reqs, prof_elig = assess_posting_mandatory_eligibility(
        posting=posting,
        profile=profile,
        check_cv_requirements=False,
    )
    if not posting.eligibility_requirements_json:
        posting.eligibility_requirements_json = reqs.to_dict()

    evaluations: list[MatchEvaluation] = []
    for cv_ver, facts in cv_candidates:
        ev = evaluate_job_against_cv(
            posting=posting,
            cv_version=cv_ver,
            facts=facts,
            pref=pref,
            profile=profile,
            now_utc=now_utc,
            static_fx_rates=static_fx_rates,
            has_supporting_attachments=has_supporting_attachments,
        )
        evaluations.append(ev)

    if not prof_elig.passed:
        best_failed = evaluations[0] if evaluations else None
        return best_failed, evaluations

    eligible = [e for e in evaluations if e.hard_filter_passed]
    if not eligible:
        best_failed = (
            max(
                evaluations,
                key=lambda e: (1 if e.eligibility_status == "eligible" else 0, e.fit_score),
            )
            if evaluations
            else None
        )
        return best_failed, evaluations
    ranked = sorted(eligible, key=lambda e: e.fit_score, reverse=True)
    best = ranked[0]
    if len(ranked) > 1:
        runner_up = ranked[1]
        margin = round(best.fit_score - runner_up.fit_score, 1)
        best.why_this_cv = f"{best.why_this_cv} Outscored next CV variant by {margin} pts."
        if margin <= 3.0 and "Narrow CV variant tie" not in " ".join(best.review_reasons):
            best.review_reasons.append(f"Narrow CV variant tie ({margin} pt margin) between multiple eligible CVs")
            best.review_required = True
    return best, evaluations
