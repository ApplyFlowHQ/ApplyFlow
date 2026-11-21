"""Public career page / JSON-LD / OpenGraph / mailto / excerpt extraction with robots.txt and SSRF guards."""

from __future__ import annotations

import html as html_lib
import json
import re
import unicodedata
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from urllib.parse import unquote, urlparse
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup

from app.core.ssrf import SSRFBlockedError, safe_fetch_public_url
from app.services.discovery.base import DiscoveredCandidate
from app.services.discovery.tavily import parse_provider_published_date
from app.services.geo import infer_location_hint_with_confidence

EMAIL_RE = re.compile(r"(?<![\w@.+%-])[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}(?![\w@-]|\.[\w-])")
BROKEN_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+(?:\.[ \t]+|[ \t]+\.)[A-Za-z]{2,}")
USER_AGENT = "ApplyFlowGlobalBot/1.0"

INVITE_KW = re.compile(
    r"(send\s+(?:your\s+)?(?:updated\s+)?(?:cv|resume|profile|application|details)|"
    r"email\s+(?:your\s+|us\s+your\s+|us\s+)?(?:cv|resume|profile|application|details)|"
    r"share\s+(?:your\s+)?(?:cv|resume|profile)|"
    r"submit\s+(?:your\s+)?(?:cv|resume|profile|application)(?:\s+(?:here|to|at|via))?|"
    r"forward\s+(?:your\s+)?(?:cv|resume|details)|drop\s+(?:your\s+)?(?:cv|resume)|"
    r"mail\s+(?:your\s+)?(?:cv|resume)|"
    r"apply\s+(?:via|by|through)\s+email|to\s+apply,?\s+(?:please\s+)?(?:email|contact)|"
    r"apply\s+(?:now\s*:\s*|at\b|to\b)|applications?\s+(?:should\s+be\s+(?:sent|emailed)\s+)?to\b|"
    r"attach\s+(?:your\s+)?(?:cv|resume)|apply\s+for\s+[^@.!?\n]{2,65}?\s+(?:via|by\s+email\s+to)|send\s+cv\s+to|send\s+resume\s+(?:to|at)|"
    r"interested\s+candidates\s+(?:may|can|should|are\s+invited\s+to)\s+(?:send|email|share|forward)|"
    r"if\s+there\s+is\s+no\s+position\s+of\s+interest[^\n.]{0,50}send\s+your\s+cv|"
    r"أرسل\s+(?:السيرة|سيرتك)|إرسال\s+السيرة|ارسل\s+سيرتك|"
    r"التقديم\s+(?:عبر|على)\s+(?:البريد|الإيميل|الايميل)|للتقديم\s+عبر\s+(?:البريد|الإيميل)|"
    r"envoyez\s+votre\s+cv|bewerbung\s+per\s+e-?mail)",
    re.IGNORECASE,
)

NON_APPLICATION_EMAIL_CONTEXT = re.compile(
    r"(accommodation|disability|accessibility|reasonable\s+adjustments?|only\s+to\s+request\s+an\s+accommodation|"
    r"hrscleaves|privacy\s+(?:policy|notice|officer)|data\s+protection|gdpr|dpo@|"
    r"opt[\s\-]?out|unsubscribe|report\s+(?:abuse|fraud|spam)|fraudulent\s+communications?|"
    r"recruitment\s+scams?|fraud\s+or\s+misleading|misleading\s+job|impersonat\w+|disclaimer:|"
    r"media\s+inquiries|press\s+inquiries|standards\s+corrections|"
    r"technical\s+(?:issues?|support)\s+with\s+(?:the\s+)?website)",
    re.IGNORECASE,
)

STANDING_KW = re.compile(
    r"(spontaneous\s+application|general\s+career\s+invitation|always\s+looking\s+for\s+(?:[A-Za-z\s]{1,40}\s+)?talent|"
    r"send\s+your\s+cv\s+for\s+future|join\s+our\s+talent\s+pool|no\s+specific\s+vacancy|"
    r"if\s+there\s+is\s+no\s+position\s+of\s+interest|even\s+if\s+there\s+are\s+no\s+(?:current\s+)?openings|"
    r"looking\s+for\s+talented\s+individuals\s+to\s+join\s+our\s+team|"
    r"تقديم\s+عام|للفرص\s+المستقبلية)",
    re.IGNORECASE,
)

PORTAL_DOMAINS = (
    "myworkdayjobs.com",
    "indeed.com",
    "linkedin.com/jobs",
    "beapplied.com",
    "greenhouse.io",
    "lever.co",
    "smartrecruiters.com",
    "icims.com",
    "taleo.net",
    "ashbyhq.com",
    "workable.com",
    "jobleads.com",
    "gulftalent.com",
    "bayt.com",
    "naukrigulf.com",
    "womenforhire.com",
    "jobs.ac.uk",
    "jobgether.com",
    "higheredjobs.com",
)

PORTAL_APPLY_PHRASES = re.compile(
    r"(apply\s+on\s+(?:company\s+)?site|click\s+apply\s+now|apply\s+via\s+our\s+(?:careers?\s+)?portal|"
    r"complete\s+(?:the\s+)?online\s+application|apply\s+with\s+indeed|easy\s+apply|"
    r"submit\s+(?:your\s+)?application\s+online|back\s+to\s+job\s+board)",
    re.IGNORECASE,
)

SOCIAL_POST_DOMAINS = (
    "linkedin.com",
    "facebook.com",
    "instagram.com",
    "x.com",
    "twitter.com",
    "tiktok.com",
    "youtube.com",
    "youtu.be",
)


@dataclass
class ExtractedApplicationEvidence:
    primary_email: str = ""
    cc_emails: list[str] = field(default_factory=list)
    subject_instruction: str = ""
    evidence_quote: str = ""
    attachment_requirements: list[str] = field(default_factory=list)
    is_standing_invitation: bool = False
    rejected_emails: list[dict[str, str]] = field(default_factory=list)
    portal_apply_detected: bool = False
    is_multi_role_digest: bool = False
    qualifications: list[str] = field(default_factory=list)
    extraction_method: str = "deterministic"


def normalize_public_text(raw: str) -> str:
    """Decode HTML entities cleanly and normalize mathematical bold/italic/fullwidth Unicode via NFKC."""
    return unicodedata.normalize("NFKC", html_lib.unescape(raw or ""))


_PLATFORM_PUBLISHERS: tuple[tuple[str, str, str], ...] = (
    ("linkedin.com", "LinkedIn", "social_platform"),
    ("facebook.com", "Facebook", "social_platform"),
    ("instagram.com", "Instagram", "social_platform"),
    ("x.com", "X", "social_platform"),
    ("twitter.com", "X", "social_platform"),
    ("myworkdayjobs.com", "Workday", "ats_portal"),
    ("indeed.com", "Indeed", "job_portal"),
    ("beapplied.com", "Applied (BeApplied)", "ats_portal"),
    ("greenhouse.io", "Greenhouse", "ats_portal"),
    ("lever.co", "Lever", "ats_portal"),
    ("smartrecruiters.com", "SmartRecruiters", "ats_portal"),
    ("icims.com", "iCIMS", "ats_portal"),
    ("taleo.net", "Taleo", "ats_portal"),
    ("ashbyhq.com", "Ashby", "ats_portal"),
    ("workable.com", "Workable", "ats_portal"),
    ("jobleads.com", "JobLeads", "job_portal"),
    ("gulftalent.com", "GulfTalent", "job_portal"),
    ("bayt.com", "Bayt", "job_portal"),
    ("naukrigulf.com", "NaukriGulf", "job_portal"),
    ("jooble.org", "Jooble", "job_portal"),
    ("jobs.ac.uk", "jobs.ac.uk", "job_portal"),
    ("womenforhire.com", "WomenForHire", "job_portal"),
    ("higheredjobs.com", "HigherEdJobs", "job_portal"),
    ("jobsitedirect.co.uk", "JobSiteDirect", "job_portal"),
    ("remotejobs.dedyn.io", "RemoteJobs", "job_portal"),
    ("khaleejtimes.com", "Khaleej Times Classifieds", "classifieds"),
    ("gocareer.club", "GoCareer Club", "classifieds"),
    ("jobsuccess.ae", "JobSuccess", "job_portal"),
    ("freshjobsworld.com", "FreshJobsWorld", "job_portal"),
    ("dmc-education.com", "DMC Education", "job_portal"),
    ("dubaivacancy.ae", "DubaiVacancy", "classifieds"),
    ("realjobsindubai.com", "RealJobsInDubai", "classifieds"),
    ("gccrecruitments.com", "GCCRecruitments", "classifieds"),
    ("freejobsindubai.com", "FreeJobsInDubai", "classifieds"),
    ("uaehelper.com", "UAEHelper", "classifieds"),
    ("expatriates.com", "Expatriates.com", "classifieds"),
    ("qatarliving.com", "Qatar Living", "classifieds"),
    ("mzadqatar.com", "MzadQatar", "classifieds"),
    ("omanjobshere.com", "Oman Jobs Here", "classifieds"),
    ("kuwaitjobshere.com", "Kuwait Jobs Here", "classifieds"),
    ("uaehrs.com", "UAEHRS", "classifieds"),
)

_NON_EMPLOYER_NAME_TOKENS = (
    "linkedin", "facebook", "instagram", "twitter", "indeed", "workday", "myworkdayjobs",
    "beapplied", "jobleads", "gulftalent", "bayt", "naukrigulf", "jooble", "khaleej times",
    "gocareer", "dubaivacancy", "realjobsindubai", "remotejobs", "higheredjobs",
    "womenforhire", "jobsitedirect", "jobs.ac.uk", "reels", "classifieds",
    "uaehrs", "new jobs", "walk in interview", "jobsora", "educationgrade", "cazajobz",
    "gccrecruitments", "freejobsindubai", "uaehelper", "expatriates.com", "qatarliving",
    "mzadqatar", "omanjobshere", "kuwaitjobshere",
)

_GENERIC_SECTION_NAMES = {
    "careers", "career", "jobs", "vacancies", "vacancy", "home", "apply", "apply now",
    "about", "about us", "contact", "contact us", "opportunities", "openings",
}

_GENERIC_JUNK_TITLES = re.compile(
    r"^(?:instagram|facebook|linkedin|public\s+job\s+posting|unfetched\s+public\s+source|"
    r"we\s+are\s+hiring!?|hiring\s+now!?|urgent\s+hiring!?|job\s+alert!?|vacancy|vacancies|"
    r"[A-Za-z0-9_.\s]+\s+on\s+reels)$",
    re.IGNORECASE,
)


def _decode_cf_email(hex_str: str) -> str:
    """Decode passive Cloudflare HTML email obfuscation (`data-cfemail`) from already-fetched HTTP 200 HTML."""
    try:
        raw = bytes.fromhex((hex_str or "").strip())
        if len(raw) < 2:
            return ""
        key = raw[0]
        decoded = bytes(b ^ key for b in raw[1:]).decode("utf-8", errors="ignore").strip()
        return decoded if EMAIL_RE.fullmatch(decoded) else ""
    except Exception:
        return ""


def _prepare_clean_soup_text(soup: BeautifulSoup) -> tuple[str, list[tuple[str, str]]]:
    """Decode passive email-protection spans, capture mailto anchors, and preserve inline sentence continuity."""
    for cf_tag in soup.select("[data-cfemail]"):
        decoded = _decode_cf_email(str(cf_tag.get("data-cfemail") or ""))
        if decoded:
            cf_tag.string = decoded
    for cf_link in soup.find_all("a", href=True):
        href = str(cf_link.get("href") or "")
        if "/cdn-cgi/l/email-protection#" in href:
            decoded = _decode_cf_email(href.split("#", 1)[1])
            if decoded:
                cf_link["href"] = f"mailto:{decoded}"
                cf_link.string = decoded

    mailto_candidates: list[tuple[str, str]] = []
    for a in soup.find_all("a", href=True):
        href = normalize_public_text(str(a["href"]))
        if href.lower().startswith("mailto:"):
            raw_addr = unquote(href[7:].split("?", 1)[0]).strip()
            if EMAIL_RE.fullmatch(raw_addr):
                label = normalize_public_text(a.get_text(" ", strip=True))
                visible_emails = EMAIL_RE.findall(label)
                if visible_emails and any(em.casefold() != raw_addr.casefold() for em in visible_emails):
                    a.string = "[conflicting mailto address]"
                else:
                    if not visible_emails:
                        a.string = f"{label} {raw_addr}".strip()
                    mailto_candidates.append((raw_addr, label))

    for inline_tag in soup.find_all(["a", "span", "strong", "b", "em", "i", "u", "small"]):
        if inline_tag.string is not None or list(inline_tag.children):
            inline_tag.insert_before(" ")
            inline_tag.insert_after(" ")
            inline_tag.unwrap()

    soup.smooth()
    raw_text = soup.get_text("\n", strip=True)
    # Collapse excess spaces on each line cleanly
    cleaned_lines = [" ".join(ln.split()) for ln in raw_text.splitlines() if ln.strip()]
    return "\n".join(cleaned_lines), mailto_candidates


def classify_publisher_and_platform(url: str) -> tuple[str, str]:
    """Return (publisher_name, platform_kind) cleanly separate from the hiring employer."""
    host = (urlparse(url or "").hostname or "").lower()
    for dom, pub, kind in _PLATFORM_PUBLISHERS:
        if host == dom or host.endswith(f".{dom}"):
            return pub, kind
    if any(agg in host for agg in ("uaehrs.", "jobsora.", "educationgrade.", "cazajobz.", "freejobsindubai.", "dubailivejobs.")):
        return host, "job_portal"
    return host, "company_site"


def _is_platform_or_aggregator_name(name: str) -> bool:
    low = (name or "").strip().lower()
    if not low or low in _GENERIC_SECTION_NAMES:
        return True
    if "." in low and re.match(r"^[a-z0-9.-]+\.[a-z]{2,}$", low):
        return True
    return any(tok in low for tok in _NON_EMPLOYER_NAME_TOKENS)


def clean_and_extract_role_and_employer(
    title: str,
    jd_text: str,
    url: str,
    *,
    jsonld_org: str = "",
    og_site_name: str = "",
) -> tuple[str, str, str, str, str]:
    """
    Separate publisher/platform from hiring employer and clean social hashtag/noise titles.
    Returns (role_extracted, validated_employer, purported_employer, employer_confidence, publisher).
    Never populates validated_employer with a search/aggregator hostname or guesses from contact email domain.
    """
    publisher, platform_kind = classify_publisher_and_platform(url)
    raw_title = normalize_public_text(title).strip()
    norm_jd = normalize_public_text(jd_text)

    # 1. Determine employer strictly without falling back to platform domains
    purported_employer = ""
    validated_employer = ""
    employer_confidence = "unknown"

    if jsonld_org and not _is_platform_or_aggregator_name(jsonld_org):
        validated_employer = jsonld_org.strip()[:180]
        purported_employer = validated_employer
        employer_confidence = "verified"

    if not validated_employer and platform_kind == "company_site" and og_site_name and not _is_platform_or_aggregator_name(og_site_name):
        purported_employer = og_site_name.strip()[:180]
        validated_employer = purported_employer
        employer_confidence = "inferred"

    # Title pattern: "... | Employer Name", "... - Employer 2026", "Careers, Company UAE", or "... at Employer"
    if not validated_employer and raw_title:
        pipe_parts = [p.strip() for p in re.split(r"\s*[|—]\s*", raw_title) if p.strip()]
        if len(pipe_parts) >= 2:
            tail = re.sub(r"\s+\b20\d{2}\b$", "", pipe_parts[-1]).strip()
            if not _is_platform_or_aggregator_name(tail) and not re.search(r"^(?:job\s+in|hiring|we\s+are\s+hiring|#)", tail, re.IGNORECASE):
                if len(tail.split()) <= 6:
                    purported_employer = validated_employer = tail[:180]
                    employer_confidence = "inferred"
        if not validated_employer and " - " in raw_title:
            dash_tail = re.sub(r"\s+\b20\d{2}\b$", "", raw_title.rsplit(" - ", 1)[-1]).strip()
            if dash_tail and not _is_platform_or_aggregator_name(dash_tail) and len(dash_tail.split()) <= 6 and not re.search(r"^(?:job|hiring|dubai|abu\s+dhabi|uae|qatar)\b", dash_tail, re.IGNORECASE):
                purported_employer = dash_tail[:180]
                validated_employer = purported_employer
                employer_confidence = "inferred"
        if not validated_employer:
            careers_comma = re.match(r"^careers\s*[,:\-]\s*([A-Za-z0-9&.\-\s]{2,55})$", raw_title, re.IGNORECASE)
            if careers_comma:
                comp = re.sub(r"\s+(?:dubai|abu\s+dhabi|uae|ksa|qatar).*$", "", careers_comma.group(1), flags=re.IGNORECASE).strip()
                if comp and not _is_platform_or_aggregator_name(comp):
                    purported_employer = comp[:180]
                    validated_employer = comp[:180]
                    employer_confidence = "inferred"
        if not validated_employer:
            at_m = re.search(r"\bat\s+([A-Z][A-Za-z0-9&.,\-\s]{2,55}?)(?:\s+[—|\-]|$)", raw_title)
            if at_m and not _is_platform_or_aggregator_name(at_m.group(1)):
                purported_employer = at_m.group(1).strip()[:180]
                validated_employer = purported_employer
                employer_confidence = "inferred"
            elif platform_kind == "ats_portal" and "myworkdayjobs.com" in (url or "").lower():
                sub = (urlparse(url).hostname or "").split(".")[0]
                if sub and len(sub) >= 2 and sub not in ("wd1", "wd3", "wd5"):
                    purported_employer = sub.upper() if len(sub) <= 4 else sub.replace("-", " ").title()
                    validated_employer = purported_employer
                    employer_confidence = "inferred"

    # Check JD text for explicit employer cues ("<Company> is hiring", "Hiring company:", "About <Company>")
    if not validated_employer and norm_jd:
        jd_emp_m = re.search(
            r"(?:employer|company|organization)\s*[:\-]\s*([A-Za-z0-9&.,'\-\s]{2,70})|"
            r"\b([A-Z][A-Za-z0-9&.,\-]{2,40}(?:\s+[A-Z][A-Za-z0-9&.,\-]{1,25}){0,4})\s+(?:in\s+[A-Za-z\s,]+\s+)?is\s+(?:hiring|seeking|looking\s+for)\b",
            norm_jd[:1200],
        )
        if jd_emp_m:
            cand_emp = (jd_emp_m.group(1) or jd_emp_m.group(2) or "").strip(" .,-")
            if cand_emp and not _is_platform_or_aggregator_name(cand_emp) and len(cand_emp.split()) <= 6:
                purported_employer = cand_emp[:180]
                validated_employer = cand_emp[:180]
                employer_confidence = "inferred"

    # Company-site evidenced brand fallback: only accept domain brand when explicitly corroborated in page text/title
    if not validated_employer and platform_kind == "company_site":
        host_lbl = (urlparse(url or "").hostname or "").lower().removeprefix("www.").removeprefix("careers.").removeprefix("jobs.")
        brand_slug = host_lbl.split(".")[0] if host_lbl else ""
        if (
            brand_slug
            and len(brand_slug) >= 4
            and brand_slug not in ("localhost", "careers", "jobs", "nodata", "example", "test")
            and re.search(rf"\b{re.escape(brand_slug)}", f"{raw_title}\n{norm_jd[:1500]}", re.IGNORECASE)
        ):
            validated_employer = brand_slug.replace("-", " ").title()
            purported_employer = validated_employer
            employer_confidence = "inferred"

    # 2. Extract clean role_extracted without hashtags or platform chrome
    cleaned_title = raw_title
    # Strip leading emojis & social alert banners
    cleaned_title = re.sub(r"^[\U00010000-\U0010ffff\u2600-\u27bf\s|]+", "", cleaned_title).strip()
    cleaned_title = re.sub(
        r"\s*[-–|]\s*(?:Indeed\.com|Myworkdayjobs\.com|LinkedIn|Facebook|Instagram|Job\s+in\s+[^\n|]+)$",
        "",
        cleaned_title,
        flags=re.IGNORECASE,
    ).strip()
    if validated_employer and cleaned_title.endswith(f"| {validated_employer}"):
        cleaned_title = cleaned_title[: -(len(validated_employer) + 2)].strip()
    if validated_employer and re.search(rf"\s+-\s+{re.escape(validated_employer)}(?:\s+\b20\d{{2}}\b)?$", cleaned_title, re.IGNORECASE):
        cleaned_title = re.sub(rf"\s+-\s+{re.escape(validated_employer)}(?:\s+\b20\d{{2}}\b)?$", "", cleaned_title, flags=re.IGNORECASE).strip()
    cleaned_title = re.sub(r"\s+Jobs?\s+(?:UAE|Dubai|Abu\s+Dhabi|Saudi|Qatar)$", "", cleaned_title, flags=re.IGNORECASE).strip()
    if validated_employer and cleaned_title.lower().endswith(f"at {validated_employer.lower()}"):
        cleaned_title = cleaned_title[: -(len(validated_employer) + 3)].strip()

    # Check if title is hashtag soup or generic social placeholder
    words_no_hash = [w for w in cleaned_title.split() if not w.startswith("#")]
    looks_like_hashtags_or_junk = (
        len(words_no_hash) <= 1
        or bool(_GENERIC_JUNK_TITLES.match(cleaned_title))
        or cleaned_title.count("#") >= 2
        or re.match(r"^(?:reputed\s+company|dubai\s+jobs|explore\s+specialist|we\s+are\s+pleased\s+to\s+welcome)\b", cleaned_title, re.IGNORECASE)
    )

    role_extracted = ""
    if not looks_like_hashtags_or_junk and len(cleaned_title) >= 3:
        role_extracted = re.sub(r"\s*#\S+", "", cleaned_title).strip(" |-–—,")[:240]
    else:
        # Attempt clean role extraction from explicit body role fields
        for ln in norm_jd.splitlines()[:25]:
            s = re.sub(r"^[\U00010000-\U0010ffff\u2600-\u27bf\s•*\-🔹]+", "", ln).strip()
            m_pos = re.match(r"^(?:position|role|job\s+title|vacancy|hiring\s+for)\s*[:\-]\s*(.{4,90})$", s, re.IGNORECASE)
            if m_pos:
                cand_r = m_pos.group(1).strip(" .,-")
                if not _GENERIC_JUNK_TITLES.match(cand_r):
                    role_extracted = cand_r[:240]
                    break
            m_wh = re.search(r"WE\s+ARE\s+HIRING\s*\|\s*([A-Za-z0-9/\-\s]{4,70}?)(?:\s*[–\-|]|$)", s, re.IGNORECASE)
            if m_wh:
                role_extracted = m_wh.group(1).strip()[:240]
                break
            m_num = re.match(r"^\d+\.\s*([A-Z][A-Z0-9/\-\s]{3,70})$", s)
            if m_num:
                role_extracted = m_num.group(1).strip().title()[:240]
                break

    if role_extracted and (_GENERIC_JUNK_TITLES.match(role_extracted) or _has_roundup_layout(raw_title, norm_jd)):
        role_extracted = ""

    return role_extracted, validated_employer, purported_employer, employer_confidence, publisher


def detect_informational_post(*, url: str, title: str, jd_text: str, has_email_invite: bool, has_portal_apply: bool) -> bool:
    """Identify career fairs, walk-in notices, candidate seeker resumes, SEO articles, or reels."""
    url_low = (url or "").lower()
    title_low = (title or "").lower()
    if any(seg in url_low for seg in ("/resume-examples", "/resume-template", "/resume-builder", "/cv-builder", "cvbyai.com", "cv-prime.")):
        return True
    if re.search(r"\b(resume\s+(?:example|template|builder)|ai\s+resume\s+builder|cv\s+format\s+20\d{2})\b", title_low):
        return True
    if re.search(r"\b(?:job\s+seekers?|open\s+to\s+(?:work|relocate))\b", title_low):
        return True
    combined_head = f"{title}\n{jd_text[:1000]}".lower()
    if re.search(r"\bi\s+am\s+(?:a|an)\s+[a-z\s]{2,40}(?:looking|seeking)\s+(?:for\s+)?(?:a\s+)?(?:job|position|opportunity|employment)\b", combined_head):
        return True
    if has_email_invite or has_portal_apply:
        return False
    combined = f"{title}\n{jd_text[:1200]}".lower()
    if any(k in combined for k in ("career fair", "walk-in interview", "walk in interview", "pleased to welcome", "congratulations to")):
        return True
    if any(seg in url_low for seg in ("/reel/", "/videos/")):
        return True
    return False


def infer_access_status(*, status_code: int | None = None, inaccessible_reason: str = "") -> str:
    low = (inaccessible_reason or "").lower()
    if "robots_disallowed" in low:
        return "robots_blocked"
    if "403" in low or status_code == 403:
        return "http_403"
    if "404" in low or status_code == 404:
        return "http_404"
    if "timeout" in low:
        return "connect_timeout"
    if "ssrf" in low:
        return "ssrf_blocked"
    if "anti-bot" in low or "interstitial" in low:
        return "bot_challenge"
    if status_code and 200 <= status_code < 400:
        return "fetched_200"
    if inaccessible_reason:
        return "inaccessible"
    return "snippet_only"


def classify_source_priority_tier(url: str, *, jd_text: str = "", has_email_invite: bool = False, is_standing: bool = False) -> int:
    """
    Rank source priority tiers per discovery strategy (1 = highest, 5 = generic portals/ATS):
    1: Direct employer / identifiable recruiter post with explicit email invitation
    2: Official company careers page inviting CVs directly via email
    3: Permitted indexed public social hiring posts (LinkedIn posts, Facebook, Instagram)
    4: Recruitment-agency post with clear email instructions
    5: Generic job portals / ATS / aggregators (still allowed ONLY if verified email evidence exists)
    """
    low_url = (url or "").lower()
    if any(pd in low_url for pd in PORTAL_DOMAINS):
        return 5
    if any(sd in low_url for sd in SOCIAL_POST_DOMAINS) and "/jobs/" not in low_url:
        if re.search(r"\b(recruitment\s+agency|staffing|manpower|candidzone|talent\s+acquisition\s+partner)\b", jd_text or "", re.IGNORECASE):
            return 4
        return 3
    if is_standing or "/careers" in low_url or "/jobs" in low_url:
        return 2 if has_email_invite else 5
    if has_email_invite:
        return 1
    return 4 if re.search(r"\b(recruiter|recruitment|agency)\b", jd_text or "", re.IGNORECASE) else 5


def _extract_qualifications(text: str) -> list[str]:
    quals: list[str] = []
    for raw_line in normalize_public_text(text).splitlines():
        line = raw_line.strip(" -•*🔹\t")
        if len(line) < 12 or len(line) > 220:
            continue
        if re.search(
            r"\b(bachelor|master|degree|diploma|certified|certification|years?\s+(?:of\s+)?experience|"
            r"proficiency|fluent|skills?\s*:|requirements?\s*:|qualifications?\s*:)\b",
            line,
            re.IGNORECASE,
        ):
            if line not in quals:
                quals.append(line)
        if len(quals) >= 8:
            break
    return quals


def _score_segment_relevance(segment: str, target_role_terms: list[str]) -> int:
    if not target_role_terms:
        return 0
    low = segment.lower()
    score = 0
    for term in target_role_terms:
        for tok in re.split(r"[\s/,&\-]+", term.lower()):
            if len(tok) >= 3 and tok in low:
                score += 2
    return score


_RECIPIENT_BRIDGE = re.compile(
    r"(?:(?:your|their|updated|cv|résumé|resume|application|details|and|with|include|portfolio|cover|letter|"
    r"in|pdf|docx?|format|only|by|via|e-?mail|at|to|here|the|our|following|address|recruitment|team|"
    r"for|future|roles|openings|opportunities|الذاتية|عبر|البريد|الإلكتروني|إلى|على|à|an)\b|[\s:,\-])*", re.I,
)
_INSTRUCTION_CONTINUATION = re.compile(
    r"^(?:(?:please\s+)?(?:cc\b|copy\s+to\b|(?:use|include|mention)\s+(?:the\s+)?subject\b|"
    r"subject\b|attach\b|required\s+documents\b|attachments?\b)|عنوان\s+الرسالة)", re.I,
)
_CC_LIST = re.compile(
    rf"\b(?:cc|copy\s+to)\s*[:\-]?\s*({EMAIL_RE.pattern}(?:(?:\s*[,;]\s*|\s+and\s+){EMAIL_RE.pattern})*)", re.I,
)

_APPLICATION_CONTACT = re.compile(
    rf"^Application contact:[ \t]*\n(?:[^\W\d_]+(?:[ '-][^\W\d_]+){{0,5}}[ \t]*\n)?"
    rf"E-?mail:[ \t]*(?P<email>{EMAIL_RE.pattern})[ \t]*[.]?[ \t]*$", re.I | re.M,
)
_APPLICATION_EMAIL = re.compile(
    rf"^(?:Interested candidates (?:can|may) apply by sending (?:an? |their |your )?(?:updated )?(?:CV|resume)"
    rf" to the recruitment email address below)[.:]?[ \t]*\nApplication email:[ \t]*(?P<email>{EMAIL_RE.pattern})[ \t]*[.]?[ \t]*$",
    re.M | re.I,
)
_NAMED_VACANCY_CONTACT = re.compile(
    rf"^Submit your CV to the recruitment email provided in the corresponding vacancy announcement[.][ \t]*\n"
    rf"Verified Application Contact[^:\n]{{0,70}}:[ \t]*\nContact Person:[ \t]*[^\n:]{{1,80}}\n"
    rf"Position:[ \t]*(?P<role>[^\n:]{{2,85}})\nEmail:[ \t]*(?P<email>{EMAIL_RE.pattern})[ \t]*[.]?[ \t]*$", re.M | re.I,
)
_EXACT_DESIGNATION = re.compile(
    r"^[ \t]*\n(?:Email )?subject:[ \t]*Use the exact (?:designation|job title|position title) you are applying for[.!]?[ \t]*(?:\n|$)",
    re.I,
)
_NUMBERED_TITLE = re.compile(r"^(?P<number>[1-9]\d?)[.)][ \t]+(?P<title>[^\n]{2,85})$", re.M)
_POSITION_LIST = re.compile(
    r"^(?:List of (?:vacant|available|open) positions[^\n]*|The following (?:\d+ )?positions are available:"
    r"|[^\n]{1,120}accepting applications for [^\n:]{0,65}positions:)[ \t]*$", re.M | re.I,
)


def _is_role_heading(title: str) -> bool:
    if not re.fullmatch(r"[A-Z][\w &'()/–—,+-]{1,80}", title) or len(title.split()) > 11:
        return False
    return not re.search(
        r"^(?:how to|to learn|apply|about|careers at|requirements|responsibilities|qualifications|benefits|"
        r"application (?:instructions|process)|job (?:description|details))\b|\b(?:jobs|vacancies|positions)$", title, re.I,
    )


def _numbered_roles(text: str) -> list[re.Match]:
    return [m for m in _NUMBERED_TITLE.finditer(text) if _is_role_heading(m["title"].strip())]


def _has_roundup_layout(title: str, text: str) -> bool:
    if _POSITION_LIST.search(text) or len(_numbered_roles(text)) >= 2:
        return True
    count = re.search(r"\b([1-9]\d*)\s+(?:jobs|(?:job\s+)?vacancies|open positions)\b", title, re.I)
    return bool(count and int(count[1]) > 1)


def extract_structured_application_evidence(
    text: str,
    *,
    html_text: str = "",
    source_url: str = "",
    target_role_terms: list[str] | None = None,
) -> ExtractedApplicationEvidence:
    """
    Extract verified email invitation details cleanly with Unicode NFKC normalization,
    mailto anchor inspection, accommodation/legal email filtering, malformed-email guard,
    and multi-role social digest disambiguation.
    """
    norm_text = normalize_public_text(text)
    mailtos: list[tuple[str, str]] = []
    if html_text:
        page_text, mailtos = _prepare_clean_soup_text(BeautifulSoup(html_text, "html.parser"))
        if not norm_text:
            norm_text = normalize_public_text(page_text)
    norm_text = re.sub(r"</?(?:strong|b|em|i|span)\b[^>]*>", "", norm_text, flags=re.I)
    out = ExtractedApplicationEvidence(
        is_standing_invitation=bool(STANDING_KW.search(norm_text)),
        portal_apply_detected=bool(PORTAL_APPLY_PHRASES.search(norm_text)
                                   or any(pd in (source_url or "").lower() for pd in PORTAL_DOMAINS)),
        qualifications=_extract_qualifications(norm_text),
    )
    # A directory page is never itself the recipient's vacancy. The splitter
    # re-extracts each bounded role before the verifier checks text and quote.
    if _has_roundup_layout(norm_text.partition("\n")[0], norm_text):
        out.is_multi_role_digest = True
        return out

    records: list[tuple[int, str, str, str]] = []
    for pattern in (_APPLICATION_CONTACT, _APPLICATION_EMAIL, _NAMED_VACANCY_CONTACT):
        for bound in pattern.finditer(norm_text):
            quote = bound.group().strip()
            records.append((0, bound["email"], quote, quote))
    def mark_malformed(match: re.Match) -> str:
        raw = match.group()
        if EMAIL_RE.fullmatch(raw.split()[0].rstrip(".")):
            return raw
        out.rejected_emails.append({"email": raw, "reason": "Malformed email whitespace in domain"})
        return "[malformed address]"

    norm_text = BROKEN_EMAIL_RE.sub(mark_malformed, norm_text)
    # A continuation may be a bare mailbox, optionally preceded by an Email: label.
    # Keep paragraphs, sentences, and other role entries as boundaries.
    norm_text = re.sub(
        r"(?<=\S)[ \t]*\n[ \t]*(?:(?:e-?mail)\s*:[ \t]*(?:\n[ \t]*)?)?"
        r"(?=[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})",
        " ", norm_text, flags=re.I,
    )
    segments = [seg.strip() for seg in re.split(r"[\n・🔹•|]+|(?<=[.!?;])\s+", norm_text) if seg.strip()]
    role_context = ""
    for index, seg in enumerate(segments):
        for match in EMAIL_RE.finditer(seg):
            email = match.group()
            prefix = seg[:match.start()]
            if NON_APPLICATION_EMAIL_CONTEXT.search(seg) or re.search(r"\b(?:adjustments?|for questions|inquiries)\b", prefix, re.I):
                out.rejected_emails.append({"email": email, "reason": "Non-application notice address"})
                continue
            invites = list(INVITE_KW.finditer(prefix))
            if not invites:
                continue
            invite = invites[-1]
            if re.search(r"\b(?:not|never|don't|cannot|do\s+not)\s+(?:please\s+)?$", prefix[:invite.start()], re.I):
                continue
            bridge = prefix[invite.end():].strip()
            if not _RECIPIENT_BRIDGE.fullmatch(bridge):
                continue
            quote = seg[invite.start():match.end()]
            if len(quote) > 400:
                continue
            next_invite = INVITE_KW.search(seg, match.end())
            instruction = seg[invite.start():next_invite.start() if next_invite else len(seg)]
            for following in segments[index + 1:]:
                if next_invite or not _INSTRUCTION_CONTINUATION.match(following) or INVITE_KW.search(following):
                    break
                instruction += "\n" + following
            score = _score_segment_relevance(f"{role_context} {seg}", target_role_terms or [])
            records.append((score, email, quote, instruction))
        # Carry a role heading forward only to the immediately following submission sentence.
        role_context = "" if EMAIL_RE.search(seg) else seg

    out.is_multi_role_digest = len({rec[1].casefold() for rec in records}) > 1
    if records:
        _, out.primary_email, out.evidence_quote, instruction = max(records, key=lambda row: row[0])
        for cc in _CC_LIST.finditer(instruction):
            for addr in EMAIL_RE.findall(cc.group(1)):
                if addr.casefold() != out.primary_email.casefold() and addr.casefold() not in {x.casefold() for x in out.cc_emails}:
                    out.cc_emails.append(addr)
        subject = re.search(
            r"""(?:\bsubject(?:\s+(?:line|format|title))?|عنوان\s+الرسالة)\s*(?:[:\-]|as\b|is\b)?\s*["“']([^"”'\n]{1,160})["”']|"""
            r"(?:\bsubject(?:\s+(?:line|format|title))?|عنوان\s+الرسالة)\s*[:\-]\s*([^\n.;]{1,140})",
            instruction, re.I,
        )
        if subject:
            out.subject_instruction = (subject.group(1) or subject.group(2)).strip()
        else:
            after_address = instruction[instruction.casefold().find(out.primary_email.casefold()) + len(out.primary_email):]
            suffix = re.match(
                r"[, \t]*with[ \t]+([^,.;\n@]{1,140}?)[ \t]+in the (?:email )?subject(?: line)?\b",
                after_address, re.I,
            )
            if suffix:
                out.subject_instruction = suffix[1].strip(" \"'“”")
        for pattern, requirement in ((r"\bpdf\b", "pdf"), (r"\bdocx?\b", "docx"),
                                     (r"\bportfolio\b", "portfolio"), (r"\bcover\s+letter\b", "cover_letter"),
                                     (r"\bwork\s+samples?\b", "work_samples")):
            if re.search(pattern, instruction, re.I):
                out.attachment_requirements.append(requirement)
        out.portal_apply_detected = False
        if any(addr.casefold() == out.primary_email.casefold() for addr, _ in mailtos):
            out.extraction_method = "html_mailto"
    return out


def extract_application_evidence_from_text(text: str) -> tuple[str, list[str], str, str, list[str], bool]:
    """Backwards-compatible 6-tuple wrapper over `extract_structured_application_evidence`."""
    ev = extract_structured_application_evidence(text)
    return (
        ev.primary_email,
        ev.cc_emails,
        ev.subject_instruction,
        ev.evidence_quote,
        ev.attachment_requirements,
        ev.is_standing_invitation,
    )


async def check_robots_allowed(url: str, transport: httpx.AsyncBaseTransport | None = None) -> tuple[bool, str]:
    """Verify robots.txt allows fetching this public page without bypassing restrictions."""
    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    try:
        _, status_code, _, body = await safe_fetch_public_url(
            robots_url,
            max_bytes=250_000,
            timeout_seconds=6.0,
            user_agent=USER_AGENT,
            transport=transport,
        )
        if status_code in (404, 410):
            return True, "robots_absent"
        if not 200 <= status_code < 300:
            return False, f"robots_unavailable_http_{status_code}"
        rp = RobotFileParser()
        rp.parse(body.decode("utf-8", errors="ignore").splitlines())
        allowed = rp.can_fetch(USER_AGENT, url)
        return allowed, "robots_allowed" if allowed else "robots_disallowed"
    except SSRFBlockedError:
        return False, "ssrf_blocked"
    except Exception:
        return False, "robots_unavailable"


def _parse_iso_datetime(val: str | None) -> datetime | None:
    if not isinstance(val, str) or not val.strip():
        return None
    cleaned = val.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _extract_dates_from_html_or_text(soup: BeautifulSoup | None, text: str, now: datetime) -> tuple[datetime | None, datetime | None, str]:
    posted_at: datetime | None = None
    closing_at: datetime | None = None
    date_src = "unknown"
    if soup is not None:
        for prop in ("article:published_time", "datePublished", "datePosted"):
            tag = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
            if tag and tag.get("content"):
                posted_at = parse_provider_published_date(str(tag["content"]), now)
                if posted_at:
                    date_src = "meta_tag"
                    break
        if not posted_at:
            t_tag = soup.find("time", attrs={"datetime": True, "itemprop": re.compile(r"^(datePosted|datePublished)$", re.I)})
            if t_tag and t_tag.get("datetime"):
                posted_at = parse_provider_published_date(str(t_tag["datetime"]), now)
                if posted_at:
                    date_src = "meta_tag"

    norm = normalize_public_text(text)
    if not posted_at:
        m = re.search(
            r"\b(?:published|posted(?:\s+on)?|date\s+posted)\s*[:\-]?\s*"
            r"(\d{4}-\d{2}-\d{2}(?:T[\d:.+-]+Z?)?|\d+\s+(?:minutes?|hours?|days?|weeks?|months?)\s+ago)\b",
            norm, re.IGNORECASE,
        )
        if m:
            posted_at = parse_provider_published_date(m.group(1), now)
            if posted_at:
                date_src = "inline_text"
    if not closing_at:
        m2 = re.search(r"(?:closing\s+date|apply\s+before|deadline|valid\s+through|expires)\s*[:\-]\s*(\d{4}-\d{2}-\d{2})", norm, re.IGNORECASE)
        if m2:
            closing_at = _parse_iso_datetime(m2.group(1))
    return posted_at, closing_at, date_src


def _infer_company_name(soup: BeautifulSoup | None, title: str, domain: str) -> str:
    og_site = ""
    if soup is not None:
        og_tag = soup.find("meta", attrs={"property": "og:site_name"})
        if og_tag and og_tag.get("content"):
            og_site = str(og_tag["content"]).strip()
    _, val_emp, _, _, _ = clean_and_extract_role_and_employer(title, "", f"https://{domain}", og_site_name=og_site)
    return val_emp


def extract_jsonld_job_postings(html_text: str) -> list[dict]:
    """Safely extract schema.org/JobPosting objects from HTML script tags."""
    soup = BeautifulSoup(html_text, "html.parser")
    results: list[dict] = []
    seen: set[str] = set()
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = tag.string or tag.get_text() or ""
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        pending = [obj]
        while pending:
            node = pending.pop()
            if isinstance(node, list):
                pending.extend(reversed(node))
                continue
            if not isinstance(node, dict):
                continue
            typ = node.get("@type", "")
            types = typ if isinstance(typ, list) else [typ]
            if any(t in ("JobPosting", "https://schema.org/JobPosting", "http://schema.org/JobPosting") for t in types):
                fingerprint = json.dumps(node, sort_keys=True, separators=(",", ":"))
                if fingerprint not in seen:
                    results.append(node)
                    seen.add(fingerprint)
                continue
            for key in ("mainEntity", "item", "itemListElement", "@graph"):
                if key in node:
                    pending.append(node[key])
    return results


def _build_excerpt_fallback_candidate(
    *,
    url: str,
    domain: str,
    existing_snippet: str,
    seed_title: str = "",
    seed_location: str = "",
    seed_location_confidence: str = "unknown",
    seed_posted_at: datetime | None = None,
    seed_date_source: str = "unknown",
    seed_search_query: str = "",
    inaccessible_reason: str = "",
    target_role_terms: list[str] | None = None,
) -> tuple[DiscoveredCandidate | None, str]:
    now = datetime.now(timezone.utc)
    if not existing_snippet and not inaccessible_reason:
        return None, "no_excerpt"

    norm_snip = normalize_public_text(existing_snippet or "")
    norm_title = normalize_public_text(seed_title or "")
    ev = extract_structured_application_evidence(
        norm_snip,
        source_url=url,
        target_role_terms=target_role_terms,
    )
    if seed_location:
        loc, loc_conf = seed_location, seed_location_confidence
    else:
        loc, loc_conf = infer_location_hint_with_confidence(title=norm_title, url=url, jd_text=norm_snip)

    role_ext, val_emp, purp_emp, emp_conf, publisher = clean_and_extract_role_and_employer(
        norm_title,
        norm_snip,
        url,
    )
    is_info = detect_informational_post(
        url=url,
        title=norm_title,
        jd_text=norm_snip,
        has_email_invite=bool(ev.primary_email),
        has_portal_apply=ev.portal_apply_detected,
    )
    acc_status = infer_access_status(inaccessible_reason=inaccessible_reason)
    tier = classify_source_priority_tier(url, jd_text=norm_snip, has_email_invite=bool(ev.primary_email), is_standing=ev.is_standing_invitation)

    if ev.primary_email and ev.evidence_quote:
        return (
            DiscoveredCandidate(
                source_provider="search_excerpt",
                source_url=url,
                source_type="standing_invitation" if ev.is_standing_invitation else "job_post",
                title=role_ext or norm_title,
                company_name=val_emp,
                company_domain=domain,
                jd_text=norm_snip,
                location_raw=loc,
                location_confidence=loc_conf,
                retrieved_at=now,
                posted_at=seed_posted_at,
                date_source=seed_date_source if seed_posted_at else "unknown",
                application_email=ev.primary_email,
                cc_emails=ev.cc_emails,
                subject_instruction=ev.subject_instruction,
                attachment_requirements=ev.attachment_requirements,
                evidence_quote=ev.evidence_quote,
                is_standing_invitation=ev.is_standing_invitation,
                qualifications=ev.qualifications,
                extraction_method="search_excerpt",
                source_priority_tier=tier,
                portal_apply_detected=ev.portal_apply_detected,
                rejected_emails=ev.rejected_emails,
                page_inaccessible_reason=inaccessible_reason,
                is_multi_role_digest=ev.is_multi_role_digest,
                is_informational_post=is_info,
                publisher=publisher,
                purported_employer=purp_emp,
                validated_employer=val_emp,
                employer_confidence=emp_conf,
                role_extracted=role_ext,
                access_status=acc_status,
                search_query=seed_search_query,
            ),
            "qualified_from_excerpt",
        )

    return (
        DiscoveredCandidate(
            source_provider="search_excerpt",
            source_url=url,
            source_type="job_post",
            title=role_ext or norm_title or "Unfetched Public Source",
            company_name=val_emp,
            company_domain=domain,
            jd_text=norm_snip,
            location_raw=loc,
            location_confidence=loc_conf,
            retrieved_at=now,
            posted_at=seed_posted_at,
            date_source=seed_date_source if seed_posted_at else "unknown",
            attachment_requirements=ev.attachment_requirements,
            is_standing_invitation=ev.is_standing_invitation,
            qualifications=ev.qualifications,
            extraction_method="search_excerpt_partial",
            source_priority_tier=tier,
            portal_apply_detected=ev.portal_apply_detected,
            rejected_emails=ev.rejected_emails,
            page_inaccessible_reason=inaccessible_reason or "Source excerpt lacks explicit CV email invitation",
            is_multi_role_digest=ev.is_multi_role_digest,
            is_informational_post=is_info,
            publisher=publisher,
            purported_employer=purp_emp,
            validated_employer=val_emp,
            employer_confidence=emp_conf,
            role_extracted=role_ext,
            access_status=acc_status,
            search_query=seed_search_query,
        ),
        inaccessible_reason or "excerpt_incomplete",
    )


async def fetch_and_parse_career_page(
    url: str,
    *,
    existing_snippet: str = "",
    seed_title: str = "",
    seed_location: str = "",
    seed_location_confidence: str = "unknown",
    seed_posted_at: datetime | None = None,
    seed_date_source: str = "unknown",
    seed_search_query: str = "",
    target_role_terms: list[str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[DiscoveredCandidate | None, str]:
    """
    Fetch a public career/job page respecting robots.txt and SSRF defenses.
    If inaccessible, uses authorized provider excerpt cleanly and preserves honest diagnostic state.
    """
    allowed, robots_reason = await check_robots_allowed(url, transport=transport)
    now = datetime.now(timezone.utc)
    domain = (urlparse(url).hostname or "").lower()

    if not allowed:
        if robots_reason == "ssrf_blocked":
            return None, "Blocked by SSRF policy"
        return _build_excerpt_fallback_candidate(
            url=url,
            domain=domain,
            existing_snippet=existing_snippet,
            seed_title=seed_title,
            seed_location=seed_location,
            seed_location_confidence=seed_location_confidence,
            seed_posted_at=seed_posted_at,
            seed_date_source=seed_date_source,
            seed_search_query=seed_search_query,
            inaccessible_reason=f"Source access restricted ({robots_reason})",
            target_role_terms=target_role_terms,
        )

    try:
        final_url, status_code, content_type, body = await safe_fetch_public_url(url, transport=transport)
    except SSRFBlockedError as exc:
        return None, f"Blocked by SSRF policy: {exc}"
    except Exception as exc:
        return _build_excerpt_fallback_candidate(
            url=url,
            domain=domain,
            existing_snippet=existing_snippet,
            seed_title=seed_title,
            seed_location=seed_location,
            seed_location_confidence=seed_location_confidence,
            seed_posted_at=seed_posted_at,
            seed_date_source=seed_date_source,
            seed_search_query=seed_search_query,
            inaccessible_reason=f"Primary page inaccessible ({exc.__class__.__name__})",
            target_role_terms=target_role_terms,
        )

    if status_code >= 400:
        return _build_excerpt_fallback_candidate(
            url=url,
            domain=domain,
            existing_snippet=existing_snippet,
            seed_title=seed_title,
            seed_location=seed_location,
            seed_location_confidence=seed_location_confidence,
            seed_posted_at=seed_posted_at,
            seed_date_source=seed_date_source,
            seed_search_query=seed_search_query,
            inaccessible_reason=f"Source returned HTTP {status_code}",
            target_role_terms=target_role_terms,
        )

    html = body.decode("utf-8", errors="ignore")
    # Detect bot-block / login-wall interstitial pages cleanly without pretending they are JDs
    if re.search(
        r"incapsula\s+incident\s+id|cloudflare\s+ray\s+id|enable\s+javascript\s+and\s+cookies\s+to\s+continue|"
        r"verifying\s+you\s+are\s+not\s+a\s+robot|checking\s+(?:if\s+the\s+site\s+connection\s+is\s+secure|your\s+browser\s+before)",
        html,
        re.IGNORECASE,
    ):
        return _build_excerpt_fallback_candidate(
            url=url,
            domain=domain,
            existing_snippet=existing_snippet,
            seed_title=seed_title,
            seed_location=seed_location,
            seed_location_confidence=seed_location_confidence,
            seed_posted_at=seed_posted_at,
            seed_date_source=seed_date_source,
            seed_search_query=seed_search_query,
            inaccessible_reason="Source gated by anti-bot challenge or JavaScript interstitial",
            target_role_terms=target_role_terms,
        )

    soup = BeautifulSoup(html, "html.parser")
    for bad in soup(["script", "style", "noscript", "nav", "aside"]):
        if bad.name == "script" and bad.get("type") == "application/ld+json":
            continue
        bad.decompose()
    for noise_sel in (".post-labels", ".blog-pager", ".related-posts", ".tagcloud", ".post-footer", "[class*='related-post']", "[class*='share-']"):
        for node in soup.select(noise_sel):
            node.decompose()

    jsonlds = extract_jsonld_job_postings(html)
    if len(jsonlds) > 1:
        from app.services.research_planner import serialize_candidate

        children = []
        for index, posting in enumerate(jsonlds, 1):
            child = _parse_career_posting(
                posting, final_url=final_url, now=now, content_type=content_type,
                target_role_terms=target_role_terms, seed_search_query=seed_search_query,
            )
            child.source_url = f"{final_url.split('#')[0]}#role-{index}"
            child.canonical_url = final_url
            child.raw_metadata["split_role_index"] = index
            children.append(child)
        # The discovery pipeline can add page-wide provider hints to the carrier.
        # Keep a separate snapshot for the first child as well as its siblings.
        return replace(children[0], source_url=final_url, raw_metadata={
            "has_jsonld": True, "content_type": content_type,
            "jsonld_job_postings": [serialize_candidate(child) for child in children],
        }), "fetched"

    return _parse_career_posting(
        jsonlds[0] if jsonlds else None, final_url=final_url, now=now, content_type=content_type,
        soup=soup, seed_title=seed_title, seed_location=seed_location, seed_posted_at=seed_posted_at,
        seed_location_confidence=seed_location_confidence, seed_date_source=seed_date_source,
        target_role_terms=target_role_terms, seed_search_query=seed_search_query,
    ), "fetched"


def expand_jsonld_job_postings(candidate: DiscoveredCandidate) -> list[DiscoveredCandidate] | None:
    """Restore individual postings before applying the text-based roundup rules."""
    snapshots = candidate.raw_metadata.get("jsonld_job_postings")
    if not snapshots:
        return None
    from app.services.research_planner import restore_candidate

    children = [restore_candidate(row) for row in snapshots]
    for child in children:
        child.canonical_url = candidate.canonical_url or child.canonical_url
        child.search_query = candidate.search_query
    return children


def _parse_career_posting(
    jp: dict | None, *, final_url: str, now: datetime, content_type: str,
    target_role_terms: list[str] | None, seed_search_query: str,
    soup: BeautifulSoup | None = None, seed_title: str = "", seed_location: str = "",
    seed_location_confidence: str = "unknown", seed_posted_at: datetime | None = None,
    seed_date_source: str = "unknown",
) -> DiscoveredCandidate:
    """Use whole-page fallbacks only when the page represents a single vacancy."""
    title = ""
    jsonld_org = ""
    og_site_name = ""
    location_raw = ""
    loc_conf = "unknown"
    posted_at = seed_posted_at
    date_source = seed_date_source if seed_posted_at else "unknown"
    closing_at = None
    employment_type = "unknown"

    jd_text = ""
    if soup is not None:
        og_tag = soup.find("meta", attrs={"property": "og:site_name"})
        if og_tag and og_tag.get("content"):
            og_site_name = normalize_public_text(str(og_tag["content"])).strip()
        main_container = (
            soup.find("article")
            or soup.find("main")
            or soup.find("div", class_=re.compile(r"(?:post-body|entry-content|job-description|vacancy-content)", re.IGNORECASE))
        )
        jd_text, _ = _prepare_clean_soup_text(main_container or soup)

    if jp is not None:
        title = normalize_public_text(str(jp.get("title", "")))[:240]
        org = jp.get("hiringOrganization")
        if isinstance(org, dict):
            jsonld_org = normalize_public_text(str(org.get("name", "")))[:180]
        jp_dt = _parse_iso_datetime(jp.get("datePosted"))
        if jp_dt:
            posted_at = jp_dt
            date_source = "jsonld"
        closing_at = _parse_iso_datetime(jp.get("validThrough"))
        raw_emp = jp.get("employmentType", "unknown")
        if isinstance(raw_emp, (list, tuple)):
            raw_emp = raw_emp[0] if raw_emp else "unknown"
        employment_type = re.sub(r"[\s\-]+", "_", str(raw_emp or "unknown").strip())[:32].lower() or "unknown"
        loc = jp.get("jobLocation")
        if isinstance(loc, dict):
            addr = loc.get("address", {})
            if isinstance(addr, dict):
                parts = [addr.get("addressLocality", ""), addr.get("addressRegion", ""), addr.get("addressCountry", "")]
                location_raw = ", ".join([str(p) for p in parts if p])
                if location_raw:
                    loc_conf = "explicit"
        if jp.get("jobLocationType") == "TELECOMMUTE":
            location_raw = f"{location_raw} (Remote)".strip()
        desc_html = jp.get("description")
        if isinstance(desc_html, str) and desc_html.strip():
            description, _ = _prepare_clean_soup_text(BeautifulSoup(desc_html, "html.parser"))
            jd_text = (description + "\n\n" + jd_text).strip()

    if not title and soup is not None and soup.title and soup.title.string:
        title = normalize_public_text(soup.title.string.strip())[:240]
    if not title:
        title = normalize_public_text(seed_title)

    ext_posted, ext_closing, ext_src = _extract_dates_from_html_or_text(soup, jd_text, now)
    if date_source != "jsonld" and ext_posted:
        posted_at = ext_posted
        date_source = ext_src
    closing_at = closing_at or ext_closing

    if not location_raw:
        if seed_location:
            location_raw = seed_location
            loc_conf = seed_location_confidence if seed_location_confidence != "unknown" else "inferred_text"
        else:
            location_raw, loc_conf = infer_location_hint_with_confidence(
                title=title, url=final_url if soup is not None else "", jd_text=jd_text,
            )

    ev = extract_structured_application_evidence(
        jd_text,
        source_url=final_url,
        target_role_terms=target_role_terms,
    )
    qualifications = list(ev.qualifications)
    job_code = ""
    if jp is not None and soup is None:
        raw_quals = jp.get("qualifications", [])
        for value in raw_quals if isinstance(raw_quals, list) else [raw_quals]:
            if isinstance(value, str) and value.strip():
                qual, _ = _prepare_clean_soup_text(BeautifulSoup(value, "html.parser"))
                if qual and qual not in qualifications:
                    qualifications.append(qual)
                    jd_text = f"{jd_text}\n{qual}".strip()
        identifier = jp.get("identifier")
        if isinstance(identifier, dict):
            identifier = identifier.get("value")
        if isinstance(identifier, (str, int)):
            job_code = str(identifier).strip()[:64]
    resolved_domain = (urlparse(final_url).hostname or "").lower()
    role_ext, val_emp, purp_emp, emp_conf, publisher = clean_and_extract_role_and_employer(
        title,
        jd_text,
        final_url,
        jsonld_org=jsonld_org,
        og_site_name=og_site_name,
    )
    is_info = detect_informational_post(
        url=final_url,
        title=title,
        jd_text=jd_text,
        has_email_invite=bool(ev.primary_email),
        has_portal_apply=ev.portal_apply_detected,
    )
    tier = classify_source_priority_tier(
        final_url,
        jd_text=jd_text,
        has_email_invite=bool(ev.primary_email),
        is_standing=ev.is_standing_invitation,
    )

    return DiscoveredCandidate(
        source_provider="career_page",
        source_url=final_url,
        source_type="standing_invitation" if ev.is_standing_invitation else "job_post",
        title=role_ext or title or "Public Job Posting",
        company_name=val_emp,
        company_domain=resolved_domain,
        jd_text=normalize_public_text(jd_text)[:20000],
        location_raw=location_raw,
        location_confidence=loc_conf,
        retrieved_at=now,
        posted_at=posted_at,
        closing_at=closing_at,
        date_source=date_source,
        employment_type=employment_type,
        application_email=ev.primary_email,
        cc_emails=ev.cc_emails,
        subject_instruction=ev.subject_instruction,
        attachment_requirements=ev.attachment_requirements,
        evidence_quote=ev.evidence_quote,
        is_standing_invitation=ev.is_standing_invitation,
        qualifications=qualifications,
        job_code=job_code,
        extraction_method="jsonld" if jp is not None else ev.extraction_method,
        source_priority_tier=tier,
        portal_apply_detected=ev.portal_apply_detected,
        rejected_emails=ev.rejected_emails,
        is_multi_role_digest=ev.is_multi_role_digest,
        is_informational_post=is_info,
        publisher=publisher,
        purported_employer=purp_emp,
        validated_employer=val_emp,
        employer_confidence=emp_conf,
        role_extracted=role_ext,
        access_status="fetched_200",
        search_query=seed_search_query,
        raw_metadata={"has_jsonld": jp is not None, "content_type": content_type},
    )


def parse_user_pasted_posting(
    *,
    title: str,
    company_name: str,
    source_url: str,
    location_raw: str,
    raw_text: str,
) -> DiscoveredCandidate:
    """Parse an optional user-pasted public job posting into a candidate record for full verification & matching."""
    now = datetime.now(timezone.utc)
    norm_raw = normalize_public_text(raw_text)
    norm_title = normalize_public_text(title)
    ev = extract_structured_application_evidence(norm_raw, source_url=source_url, target_role_terms=[norm_title])
    posted_at, closing_at, date_source = _extract_dates_from_html_or_text(None, norm_raw, now)
    domain = (urlparse(source_url).hostname or "").lower() if source_url else ""
    if location_raw.strip():
        inferred_loc, loc_conf = location_raw.strip(), "explicit"
    else:
        inferred_loc, loc_conf = infer_location_hint_with_confidence(title=norm_title, url=source_url, jd_text=norm_raw)
    role_ext, val_emp, purp_emp, emp_conf, publisher = clean_and_extract_role_and_employer(
        norm_title,
        norm_raw,
        source_url or "",
        jsonld_org=company_name.strip(),
    )
    return DiscoveredCandidate(
        source_provider="user_pasted",
        source_url=source_url or "user://pasted-posting",
        source_type="user_pasted",
        title=role_ext or norm_title.strip() or "User-Provided Public Posting",
        company_name=company_name.strip() or val_emp,
        company_domain=domain,
        jd_text=norm_raw.strip(),
        posted_at=posted_at,
        closing_at=closing_at,
        date_source=date_source,
        location_raw=inferred_loc,
        location_confidence=loc_conf,
        retrieved_at=now,
        application_email=ev.primary_email,
        cc_emails=ev.cc_emails,
        subject_instruction=ev.subject_instruction,
        attachment_requirements=ev.attachment_requirements,
        evidence_quote=ev.evidence_quote,
        is_standing_invitation=ev.is_standing_invitation,
        qualifications=ev.qualifications,
        extraction_method="user_pasted",
        source_priority_tier=1 if ev.primary_email else 5,
        portal_apply_detected=ev.portal_apply_detected,
        rejected_emails=ev.rejected_emails,
        is_multi_role_digest=ev.is_multi_role_digest,
        publisher=publisher,
        purported_employer=purp_emp,
        validated_employer=company_name.strip() or val_emp,
        employer_confidence="verified" if company_name.strip() else emp_conf,
        role_extracted=role_ext or norm_title.strip(),
        access_status="user_pasted",
    )


_GLOBAL_HEADING = re.compile(
    r"^(?:[^\w\s]+[ \t]+\d+[.)][ \t]+|(?:how to apply|application guide|verified application contact|"
    r"(?:shared|general|common) requirements|requirements for all)\b)", re.M | re.I,
)
_LIMITED_SCOPE = re.compile(
    r"\b(?:some|certain|selected|one of the|particular)\s+(?:of the\s+)?(?:positions|roles|vacancies)\b", re.I,
)
_MANDATORY = re.compile(r"\b(?:only|must|requir(?:e[ds]?|ements?)|mandatory|minimum|at least|licen[cs]e)\b", re.I)
_ALL_ROLES = re.compile(r"\b(?:all (?:applicants|candidates|positions|vacancies|roles)|(?:common|shared) requirements)\b", re.I)
_JOB_CONDITION = re.compile(
    r"\b(?:must|required|mandatory|minimum|at least|only|prohibited|restrictions?|eligible|eligibility|"
    r"citizenship|sponsorship|nationals?|work permit|work authorization|licen[cs]e|certification|"
    r"salary|weekly hours|on[- ]site|hybrid|remote work|immediate joiners?|deadline|closing date)\b"
    r"|^\s*(?:experience:\s*)?\d+\+?\s+years?\b", re.I,
)


def _page_conditions(lines: list[str]) -> list[str]:
    """Keep hiring conditions from shared text without converting company history into duties."""
    result = []
    condition_section = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            condition_section = False
        elif stripped.endswith(":"):
            condition_section = bool(re.search(r"\b(?:requirements|qualifications|eligibility|conditions)\b", stripped, re.I))
        if condition_section or _ALL_ROLES.search(stripped) or _JOB_CONDITION.search(stripped):
            result.append(stripped)
    return result


def _all_role_conditions(lines: list[str]) -> list[str]:
    """Include multiline conditions explicitly declared for every applicant."""
    result = []
    in_section = False
    for line in lines:
        if _ALL_ROLES.search(line):
            result.append(line)
            in_section = line.rstrip().endswith(":")
        elif not line.strip() or line.rstrip().endswith(":") or _NUMBERED_TITLE.fullmatch(line):
            in_section = False
        elif in_section:
            result.append(line)
    return result


def _common_context(text: str, role: str, roles: list[str]) -> str | None:
    """Retain source conditions. Drop routing/examples and hold uncertain subset requirements."""
    kept = []
    for line in text.splitlines():
        line = line.strip()
        if not line or re.match(r"(?:Email subject|Subject|For example|Contact Person|Position):", line, re.I):
            continue
        if EMAIL_RE.search(line):
            if _MANDATORY.search(line):
                return None
            continue
        if _MANDATORY.search(line) and _LIMITED_SCOPE.search(line):
            return None
        named_roles = {r for r in roles if re.search(rf"(?<!\w){re.escape(r)}(?!\w)", line, re.I)}
        if named_roles and role not in named_roles:
            # A paragraph containing both a global and a role-specific condition
            # cannot safely be discarded for the other roles.
            if re.search(r"\ball (?:candidates|roles|positions|applicants)\b", line, re.I):
                return None
            continue
        kept.append(line)
    return "\n".join(kept)


def _listed_role_blocks(text: str) -> list[tuple[int, str, str, str]]:
    """A list-wide mailbox needs an explicit designation rule next to the address."""
    invitations = list(_APPLICATION_EMAIL.finditer(text))
    if len(invitations) != 1:
        return []
    invite = invitations[0]
    subject_rule = _EXACT_DESIGNATION.match(text[invite.end():])
    if not subject_rule:
        return []
    before = text[:invite.start()]
    if EMAIL_RE.search(before):
        return []  # Could be independent instructions for one of the lists.
    after = text[invite.end() + subject_rule.end():]
    if re.search(r"^(?:Application (?:contact|email)|(?:please )?(?:apply|send|submit) [^\n]*@)", after, re.I | re.M):
        return []
    # Example subjects can illustrate the designation rule. Another live subject
    # directive would make it ambiguous which subject should actually be used.
    in_examples = False
    for line in after.splitlines():
        if re.fullmatch(r"(?:for )?examples?:", line.strip(), re.I):
            in_examples = True
        elif re.match(r"(?:email )?subject:", line.strip(), re.I):
            if not in_examples:
                return []
        elif line.strip():
            in_examples = False
    lines = before.splitlines()
    roles = []
    groups = []
    for i, line in enumerate(lines):
        if not _POSITION_LIST.fullmatch(line):
            continue
        category = next((n for n in range(i - 1, -1, -1) if lines[n].strip()), None)
        start = category if category is not None and _NUMBERED_TITLE.fullmatch(lines[category]) else i
        group = []
        end = i + 1
        for j, item in enumerate(lines[i + 1:], i + 1):
            title = item.strip()
            if not title and not group:
                continue
            if not _is_role_heading(title):
                break
            group.append(title)
            end = j + 1
        declared = re.search(r"\b(\d+) positions\b", line, re.I)
        if not group or (declared and len(group) != int(declared[1])):
            return []
        roles.extend(group)
        groups.append((start, end, group))
    total = re.search(r"\b(\d+)\s+job vacancies\b", before, re.I)
    if total and len(roles) != int(total[1]):
        return []
    if len(roles) < 2 or len(roles) != len({role.casefold() for role in roles}):
        return []
    instructions = text[invite.start():invite.end() + subject_rule.end()].strip()
    global_lines = _page_conditions(lines[:groups[0][0]] + after.splitlines())
    global_lines.extend(_all_role_conditions(lines))
    blocks = []
    for g, (_, end, group) in enumerate(groups):
        next_start = groups[g + 1][0] if g + 1 < len(groups) else len(lines)
        context = "\n".join(dict.fromkeys(global_lines + lines[end:next_start]))
        for role in group:
            shared = _common_context(context, role, roles)
            if shared is None:
                return []
            blocks.append((len(blocks) + 1, role, f"{role}\n{instructions}", shared))
    return blocks


def _numbered_role_blocks(text: str, headings: list[re.Match], page_title: str) -> list[tuple[int, str, str, str]]:
    if [int(m["number"]) for m in headings] != list(range(1, len(headings) + 1)):
        return []
    blocks = []
    shared_parts = [text[:headings[0].start()]]
    shared_parts.extend(_all_role_conditions(text.splitlines()))
    roles = [m["title"].strip() for m in headings]
    for idx, heading in enumerate(headings):
        end = headings[idx + 1].start() if idx + 1 < len(headings) else len(text)
        block = text[heading.start():end]
        # The end-of-article application guide, repeated contact card and links
        # belong to the article, not the last numbered role.
        section = _GLOBAL_HEADING.search(block)
        if section:
            shared_parts.append(block[section.start():])
            block = block[:section.start()]
        block = block.strip()
        blocks.append((idx + 1, heading["title"].strip(), block))

    # Compact, explicitly shared multiple-opening notices retained by the legacy
    # importer have a standalone final invitation after a blank paragraph.
    if re.fullmatch(r"multiple (?:openings|vacancies|positions)", page_title.strip(), re.I):
        parts = re.split(r"\n[ \t]*\n", blocks[-1][2])
        if (len(parts) > 1 and INVITE_KW.match(parts[-1]) and not EMAIL_RE.search(text[:headings[-1].start()])
                and not any(role.casefold() in parts[-1].casefold() for _, role, _ in blocks)):
            blocks = [(i, role, f"{block}\n\n{parts[-1]}" if i < len(blocks) else block)
                      for i, role, block in blocks]
    result = []
    for i, role, block in blocks:
        shared = _common_context("\n".join(shared_parts), role, roles)
        if shared is not None:
            result.append((i, role, block, shared))
    return result


def split_clear_multi_role_post(candidate: DiscoveredCandidate) -> list[DiscoveredCandidate]:
    """Return separately evidenced roles, or one non-actionable roundup for review."""
    jsonld_children = expand_jsonld_job_postings(candidate)
    if jsonld_children is not None:
        return jsonld_children
    if candidate.source_provider == "demo_fixture" or candidate.raw_metadata.get("split_role_index"):
        return [candidate]
    text = normalize_public_text(candidate.jd_text or "")
    headings = _numbered_roles(text)
    listed = bool(_POSITION_LIST.search(text))
    if not _has_roundup_layout(candidate.title, text):
        return [candidate]

    blocks = []
    if text.count("To view or add a comment, sign in") < 2:
        if listed:
            blocks = _listed_role_blocks(text)
        elif len(headings) >= 2:
            blocks = _numbered_role_blocks(text, headings, candidate.title)
    named = list(_NAMED_VACANCY_CONTACT.finditer(text))
    children = []
    for number, role, block, shared in blocks:
        for card in named:
            if card["role"].strip().casefold() == role.casefold():
                block = f"{block}\n{card.group()}"
        ev = extract_structured_application_evidence(block, source_url=candidate.source_url, target_role_terms=[role])
        if not ev.primary_email or ev.is_multi_role_digest:
            continue
        named_roles = {card["role"].strip().casefold() for card in named
                       if card["email"].casefold() == ev.primary_email.casefold()}
        if named_roles and role.casefold() not in named_roles:
            continue
        posted, closing, date_source = _extract_dates_from_html_or_text(None, block, candidate.retrieved_at)
        location = re.search(r"(?im)^Location:[ \t]*(.+)$", block)
        # Preserve the source spelling in subjects (including acronyms, case and dashes).
        # The existing UI uses title-cased extracted roles for direct numbered blocks.
        extracted_role = role if listed or _APPLICATION_CONTACT.search(block) else role.title()
        children.append(replace(
            candidate, title=role, role_extracted=extracted_role, source_type="job_post",
            source_url=f"{candidate.source_url.split('#')[0]}#role-{number}",
            canonical_url=candidate.canonical_url or candidate.source_url,
            location_raw=location[1].strip() if location else candidate.location_raw,
            location_confidence="explicit" if location else candidate.location_confidence,
            jd_text=f"{block}\n{shared}".strip(), application_email=ev.primary_email, cc_emails=ev.cc_emails,
            job_code="", posted_at=posted, closing_at=closing, date_source=date_source,
            subject_instruction=role if listed else ev.subject_instruction,
            attachment_requirements=ev.attachment_requirements, evidence_quote=ev.evidence_quote,
            is_standing_invitation=False, portal_apply_detected=False, is_informational_post=False,
            qualifications=_extract_qualifications(f"{block}\n{shared}"), rejected_emails=ev.rejected_emails, is_multi_role_digest=True,
            raw_metadata={"split_role_index": number},
        ))
    if children:
        return children
    return [replace(
        candidate, role_extracted="", title="Vacancy roundup (role unverified)",
        application_email="", cc_emails=[], subject_instruction="", attachment_requirements=[],
        evidence_quote="", is_multi_role_digest=True, is_standing_invitation=False,
        is_informational_post=False, qualifications=[],
        extraction_method="unresolved_multi_role_digest",
        raw_metadata={"roundup_title": candidate.title},
    )]
