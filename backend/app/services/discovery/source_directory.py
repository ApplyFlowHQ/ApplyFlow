"""International Email-First Job Source Directory partitioned by country/region, industry, and crawlability."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True)
class EmailFirstSourceEntry:
    """Verified public platform, classifieds board, or career directory yielding direct CV-submission emails."""

    domain: str
    name: str
    country_codes: tuple[str, ...]  # ISO-2 codes or "GLOBAL"
    industries: tuple[str, ...]  # all | fm_engineering | data_analytics_tech | admin_ops | finance | hospitality | education_nonprofit
    category: str  # email_first_board | classifieds | newspaper_classifieds | startup_community | company_careers_seed | portal_email_desc
    email_yield: str  # very_high | high | medium_high
    crawlable_direct: bool = True
    recommended_domain_cap: int = 5
    allow_classification_ads_filter: bool = False  # Classifieds host both employer ads & candidate resumes


# Grounded in the verified GCC & International email-first source directory
INTERNATIONAL_EMAIL_FIRST_SOURCES: tuple[EmailFirstSourceEntry, ...] = (
    # --- Tier A: High-Yield Email-First Job Blogs & Classifieds (UAE / GCC) ---
    EmailFirstSourceEntry(
        domain="gocareer.club",
        name="GoCareer Club",
        country_codes=("AE", "SA", "QA", "OM", "KW", "BH"),
        industries=("all", "fm_engineering", "data_analytics_tech", "admin_ops", "finance"),
        category="email_first_board",
        email_yield="very_high",
        recommended_domain_cap=6,
    ),
    EmailFirstSourceEntry(
        domain="uaehrs.com",
        name="UAEHRS",
        country_codes=("AE",),
        industries=("all", "fm_engineering", "data_analytics_tech", "admin_ops", "hospitality"),
        category="email_first_board",
        email_yield="high",
        recommended_domain_cap=6,
    ),
    EmailFirstSourceEntry(
        domain="freejobsindubai.com",
        name="FreeJobsInDubai (Khaleej Times Aggregator)",
        country_codes=("AE",),
        industries=("all", "fm_engineering", "admin_ops", "hospitality", "data_analytics_tech"),
        category="email_first_board",
        email_yield="high",
        recommended_domain_cap=6,
    ),
    EmailFirstSourceEntry(
        domain="uaehelper.com",
        name="UAEHelper (Gulf News Aggregator)",
        country_codes=("AE",),
        industries=("all", "fm_engineering", "admin_ops", "data_analytics_tech"),
        category="email_first_board",
        email_yield="high",
        recommended_domain_cap=6,
    ),
    EmailFirstSourceEntry(
        domain="gccrecruitments.com",
        name="GCCRecruitments",
        country_codes=("AE", "SA", "QA", "OM", "KW", "BH"),
        industries=("all", "fm_engineering", "data_analytics_tech", "admin_ops"),
        category="email_first_board",
        email_yield="high",
        recommended_domain_cap=6,
    ),
    EmailFirstSourceEntry(
        domain="realjobsindubai.com",
        name="RealJobsInDubai",
        country_codes=("AE",),
        industries=("all", "fm_engineering", "admin_ops"),
        category="email_first_board",
        email_yield="high",
        recommended_domain_cap=5,
    ),
    EmailFirstSourceEntry(
        domain="dubaivacancy.ae",
        name="DubaiVacancy.ae",
        country_codes=("AE",),
        industries=("fm_engineering", "admin_ops", "hospitality", "all"),
        category="email_first_board",
        email_yield="high",
        recommended_domain_cap=5,
    ),
    EmailFirstSourceEntry(
        domain="expatriates.com",
        name="Expatriates.com Classifieds",
        country_codes=("AE", "SA", "QA", "OM", "KW", "BH"),
        industries=("all", "admin_ops", "data_analytics_tech", "fm_engineering", "finance"),
        category="classifieds",
        email_yield="very_high",
        recommended_domain_cap=6,
        allow_classification_ads_filter=True,
    ),
    EmailFirstSourceEntry(
        domain="dubizzle.com",
        name="Dubizzle Jobs",
        country_codes=("AE",),
        industries=("all", "admin_ops", "fm_engineering", "hospitality"),
        category="classifieds",
        email_yield="medium_high",
        recommended_domain_cap=4,
        allow_classification_ads_filter=True,
    ),
    # --- Tier A2: Regional GCC Email-First Boards & Newspaper Classifieds ---
    EmailFirstSourceEntry(
        domain="qatarliving.com",
        name="Qatar Living Jobs",
        country_codes=("QA",),
        industries=("all", "fm_engineering", "admin_ops", "data_analytics_tech"),
        category="classifieds",
        email_yield="medium_high",
        recommended_domain_cap=5,
    ),
    EmailFirstSourceEntry(
        domain="mzadqatar.com",
        name="MzadQatar Jobs",
        country_codes=("QA",),
        industries=("all", "fm_engineering", "admin_ops"),
        category="classifieds",
        email_yield="high",
        recommended_domain_cap=5,
    ),
    EmailFirstSourceEntry(
        domain="gulf-times.com",
        name="Gulf Times Classifieds",
        country_codes=("QA",),
        industries=("all", "fm_engineering", "admin_ops", "finance"),
        category="newspaper_classifieds",
        email_yield="very_high",
        recommended_domain_cap=4,
    ),
    EmailFirstSourceEntry(
        domain="omanjobshere.com",
        name="Oman Jobs Here",
        country_codes=("OM",),
        industries=("all", "fm_engineering", "admin_ops", "data_analytics_tech"),
        category="email_first_board",
        email_yield="high",
        recommended_domain_cap=5,
    ),
    EmailFirstSourceEntry(
        domain="kuwaitjobshere.com",
        name="Kuwait Jobs Here",
        country_codes=("KW",),
        industries=("all", "fm_engineering", "admin_ops", "data_analytics_tech"),
        category="email_first_board",
        email_yield="high",
        recommended_domain_cap=5,
    ),
    # --- Tier B: International Country & Industry Email-First Sources ---
    EmailFirstSourceEntry(
        domain="charityjob.co.uk",
        name="CharityJob UK",
        country_codes=("GB",),
        industries=("education_nonprofit", "data_analytics_tech", "admin_ops", "all"),
        category="email_first_board",
        email_yield="medium_high",
        recommended_domain_cap=5,
    ),
    EmailFirstSourceEntry(
        domain="jobs.ac.uk",
        name="jobs.ac.uk Academic & Research",
        country_codes=("GB", "IE"),
        industries=("education_nonprofit", "data_analytics_tech", "admin_ops"),
        category="email_first_board",
        email_yield="medium_high",
        recommended_domain_cap=4,
    ),
    EmailFirstSourceEntry(
        domain="jobsitedirect.co.uk",
        name="JobSiteDirect UK",
        country_codes=("GB",),
        industries=("fm_engineering", "admin_ops", "all"),
        category="email_first_board",
        email_yield="medium_high",
        recommended_domain_cap=4,
    ),
    EmailFirstSourceEntry(
        domain="arbeitnow.com",
        name="Arbeitnow Europe / DACH",
        country_codes=("DE", "AT", "CH", "NL"),
        industries=("data_analytics_tech", "all"),
        category="email_first_board",
        email_yield="medium_high",
        recommended_domain_cap=4,
    ),
    EmailFirstSourceEntry(
        domain="news.ycombinator.com",
        name="Hacker News Who Is Hiring",
        country_codes=("US", "CA", "GB", "GLOBAL"),
        industries=("data_analytics_tech",),
        category="startup_community",
        email_yield="high",
        recommended_domain_cap=4,
    ),
    # --- Tier C: Filterable Portals (email-in-description search fallback only) ---
    EmailFirstSourceEntry(
        domain="tanqeeb.com",
        name="Tanqeeb Regional Aggregator",
        country_codes=("AE", "SA", "QA", "OM", "KW", "BH"),
        industries=("all", "fm_engineering", "admin_ops", "data_analytics_tech"),
        category="portal_email_desc",
        email_yield="medium_high",
        recommended_domain_cap=3,
    ),
    EmailFirstSourceEntry(
        domain="jobsora.com",
        name="Jobsora Direct Descriptions",
        country_codes=("AE", "GB", "US", "CA", "AU", "GLOBAL"),
        industries=("all",),
        category="portal_email_desc",
        email_yield="medium_high",
        recommended_domain_cap=3,
    ),
    EmailFirstSourceEntry(
        domain="naukrigulf.com",
        name="NaukriGulf Email-in-Desc",
        country_codes=("AE", "SA", "QA", "OM", "KW", "BH"),
        industries=("fm_engineering", "data_analytics_tech", "admin_ops", "all"),
        category="portal_email_desc",
        email_yield="medium_high",
        recommended_domain_cap=3,
    ),
)

_YIELD_RANK = {"very_high": 0, "high": 1, "medium_high": 2}

EMAIL_FIRST_DOMAIN_CAPS: dict[str, int] = {
    entry.domain: entry.recommended_domain_cap for entry in INTERNATIONAL_EMAIL_FIRST_SOURCES
}

# Non-vacancy editorial articles, SEO resume builders, courses, and search index list paths
_NON_VACANCY_URL_MARKERS: tuple[str, ...] = (
    "/resume-builder",
    "/resume-examples",
    "/resume-template",
    "/cv-builder",
    "/how-to-get-",
    "/what-is-",
    "/salary-guide",
    "/interview-questions",
    "/courses-in-",
    "/soft-skills-training",
    "/training/",
    "/academy/",
    "/walk-in-interview",
    "/walk-in-interviews",
    "/email-hr-for-job",
    "/reels/",
    "/reel/",
    "/videos/",
)

_SEARCH_INDEX_PATH_PREFIXES: tuple[str, ...] = (
    "/q-",
    "/jobs-",
    "/jobs/",
    "/job-search",
)


def classify_industry_buckets(
    target_titles: list[str] | None = None,
    role_families: list[str] | None = None,
    industries: list[str] | None = None,
) -> set[str]:
    """Map user job titles, families, and industries to directory industry buckets."""
    blob = " ".join([*(target_titles or []), *(role_families or []), *(industries or [])]).lower()
    buckets: set[str] = {"all"}
    if any(k in blob for k in ("cafm", "cmms", "maximo", "facilities", "maintenance", "ppm", "mep", "asset management", "engineering")):
        buckets.add("fm_engineering")
    if any(k in blob for k in ("data", "analyst", "power bi", "tableau", "mis", "business intelligence", "reporting", "automation", "software", "python", "sql", "ai", "kpi", "sla")):
        buckets.add("data_analytics_tech")
    if any(k in blob for k in ("coordinator", "document control", "admin", "operations", "project", "executive", "service delivery", "qa/qc", "quality")):
        buckets.add("admin_ops")
    if any(k in blob for k in ("accountant", "finance", "audit", "payroll", "tax")):
        buckets.add("finance")
    if any(k in blob for k in ("hotel", "hospitality", "chef", "f&b")):
        buckets.add("hospitality")
    if any(k in blob for k in ("teacher", "academic", "research", "charity", "nonprofit", "university")):
        buckets.add("education_nonprofit")
    return buckets


def select_directory_sources(
    *,
    country_codes: list[str] | set[str],
    industry_buckets: set[str] | None = None,
    include_portals: bool = False,
    crawlable_only: bool = True,
) -> list[EmailFirstSourceEntry]:
    """Return country- and industry-matched email-first job sources ranked by email yield."""
    codes = {c.upper().strip() for c in (country_codes or []) if c and c.strip()}
    if not codes:
        codes = {"GLOBAL"}
    buckets = industry_buckets or {"all"}

    matched: list[tuple[tuple[int, int, int], EmailFirstSourceEntry]] = []
    for entry in INTERNATIONAL_EMAIL_FIRST_SOURCES:
        if crawlable_only and not entry.crawlable_direct:
            continue
        if not include_portals and entry.category == "portal_email_desc":
            continue
        country_hit = bool(codes & set(entry.country_codes)) or "GLOBAL" in entry.country_codes
        if not country_hit:
            continue
        ind_overlap = len((set(entry.industries) - {"all"}) & buckets)
        if not (ind_overlap > 0 or "all" in entry.industries):
            continue
        # Rank higher yield & exact country/industry specialization first
        exact_country = 0 if (len(entry.country_codes) == 1 and entry.country_codes[0] in codes) else 1
        y_rank = _YIELD_RANK.get(entry.email_yield, 2)
        matched.append(((y_rank, exact_country, -ind_overlap), entry))

    matched.sort(key=lambda item: item[0])
    return [item[1] for item in matched]


def get_recommended_domain_cap(host_or_url: str, default_cap: int = 3) -> int:
    """Allow higher per-domain fetch budget for verified multi-employer email-first boards."""
    raw = (host_or_url or "").strip().lower()
    host = (urlparse(raw).hostname or raw) if "://" in raw else raw
    host = host.removeprefix("www.")
    for dom, cap in EMAIL_FIRST_DOMAIN_CAPS.items():
        if host == dom or host.endswith(f".{dom}"):
            return max(default_cap, cap)
    return default_cap


def is_email_first_directory_domain(host_or_url: str) -> bool:
    raw = (host_or_url or "").strip().lower()
    host = (urlparse(raw).hostname or raw) if "://" in raw else raw
    host = host.removeprefix("www.")
    return any(
        (host == entry.domain or host.endswith(f".{entry.domain}"))
        for entry in INTERNATIONAL_EMAIL_FIRST_SOURCES
        if entry.category in ("email_first_board", "classifieds", "newspaper_classifieds", "startup_community")
    )


def is_non_vacancy_seo_or_index_url(url: str, title: str = "") -> bool:
    """Detect SEO guides, resume builders, multi-listing category indexes, and non-vacancy pages prior to HTTP fetch."""
    parsed = urlparse((url or "").strip())
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    title_low = (title or "").lower()

    if any(marker in path for marker in _NON_VACANCY_URL_MARKERS):
        return True
    if any(seg in host for seg in ("enhancv.com", "jobglance.app", "facilio.com", "cvjet.co", "cv-prime.", "cvbyai.com", "resumecreatorhub.com", "resumebuilder.ae", "nobleprog.", "coursetakers.", "henryharvin.", "informaconnect.com", "horizonhrconsultancy.", "bloggers.feedspot.")):
        return True
    if "curtindubai.ac.ae" in host and "/jobs/companies/" in path:
        return True
    # Directory/portal search listing indexes (keep individual classified/job pages)
    if ("indeed." in host or "jooble." in host or "jobsora." in host) and any(
        path.startswith(pref) for pref in _SEARCH_INDEX_PATH_PREFIXES
    ):
        return True
    if "expatriates.com" in host and ("/classifieds/" in path and "/index" in path):
        return True
    if any(tok in title_low for tok in ("walk-in interviews in uae today", "walk in interview in dubai today", "resume example", "resume builder", "job seekers,", "- dubai job seekers", "open to relocate")):
        return True
    return False
