"""Email-Application-First rotating search query generator driven dynamically by user CV, preferences, and locale."""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.services.discovery.source_directory import (
    classify_industry_buckets,
    select_directory_sources,
)
from app.services.geo import resolve_country_code, resolve_user_search_locations
from app.services.i18n import LOCALIZED_SEND_CV_PHRASES


@dataclass(frozen=True)
class PlannedSearchQuery:
    query: str
    source_group: str  # email_first_directory | direct_email_posts | social_hiring_posts | inbox_handle_signals | company_careers | localized
    country_code: str
    country_name: str
    location_term: str
    title_term: str
    language: str
    page: int = 0
    exclude_generic_portals: bool = True
    preferred_domains: tuple[str, ...] = ()
    provider_hint: str = ""  # "" | "tavily" | "brave"


PORTAL_NOISE_DOMAINS: tuple[str, ...] = (
    "indeed.com",
    "uk.indeed.com",
    "ae.indeed.com",
    "sa.indeed.com",
    "myworkdayjobs.com",
    "jobleads.com",
    "beapplied.com",
    "remotejobs.dedyn.io",
    "jooble.org",
    "sa.jooble.org",
    "gulftalent.com",
    "bayt.com",
    "naukrigulf.com",
    "womenforhire.com",
    "higheredjobs.com",
    "jobs.ac.uk",
    "jobsitedirect.co.uk",
    "tavily.com",
    "resumecreatorhub.com",
    "canva.com",
    "glassdoor.com",
    "freejobalert.com",
    "jaabz.com",
    "buzzon.khaleejtimes.com",
    "expatmedia.net",
    "cv-prime.in",
    "cvbyai.com",
    "hnhiring.com",
    "resumebuilder.ae",
)

NON_CRAWLABLE_SOCIAL_DOMAINS: tuple[str, ...] = (
    "linkedin.com",
    "facebook.com",
    "instagram.com",
    "x.com",
    "twitter.com",
    "tiktok.com",
    "youtube.com",
    "youtu.be",
)

_COUNTRY_CCTLDS: dict[str, str] = {
    "AE": ".ae",
    "SA": ".sa",
    "QA": ".qa",
    "KW": ".kw",
    "OM": ".om",
    "BH": ".bh",
    "GB": ".co.uk",
    "DE": ".de",
}


def _select_diverse_titles(titles: list[str], limit: int = 8, rotation_offset: int = 0) -> list[str]:
    """Spread query selection across distinct occupational families with bounded rotation on core searchable roles."""
    clean_titles = [
        t
        for t in titles
        if t
        and len(t) <= 55
        and not any(bad in t.lower() for bad in ("llc", "ltd", "inc.", "university", "institute", "college", "b.a.", "b.sc.", "dubai hills"))
    ]
    if not clean_titles:
        clean_titles = list(titles)
    if len(clean_titles) <= limit:
        return clean_titles

    niche_markers = ("trainer", "sla ", "kpi ", "data processing", "qa/qc")
    primary_titles = [t for t in clean_titles if not any(nm in t.lower() for nm in niche_markers)]
    tail_titles = [t for t in clean_titles if t not in primary_titles]
    pool_titles = primary_titles + tail_titles

    generic = {"administrator", "coordinator", "specialist", "executive", "analyst", "planner", "manager", "officer", "associate", "support", "junior", "senior", "lead"}
    clusters: dict[str, list[str]] = {}
    cluster_order: list[str] = []
    for t in pool_titles:
        toks = [w for w in t.lower().replace("/", " ").replace("-", " ").split() if len(w) >= 2]
        core = [w for w in toks if w not in generic]
        anchor = core[0] if core else (toks[0] if toks else t.lower())
        # Group closely overlapping role tracks so CAFM, Data/BI Analytics, Operations, Document Control, and Automation all run
        if anchor in ("cafm", "cmms", "maximo", "ibm", "ppm", "fm", "facilities", "maintenance", "asset", "service", "technical"):
            key = "cafm_fm"
        elif anchor in ("data", "bi", "power", "reporting", "mis"):
            key = "data_bi_mis"
        elif anchor in ("business", "kpi", "sla", "performance", "process", "quality", "qa"):
            key = "business_process"
        elif anchor in ("operations", "project"):
            key = "ops_project"
        elif "trainer" in t.lower():
            key = "z_trainer"
        else:
            key = anchor
        if key not in clusters:
            clusters[key] = []
            cluster_order.append(key)
        clusters[key].append(t)

    # Keep broad primary clusters ahead of trailing singleton tracks like z_trainer
    cluster_order.sort(key=lambda k: (1 if k.startswith("z_") else 0))

    picked: list[str] = []
    idx = 0
    while len(picked) < limit and idx < 10:
        progressed = False
        for k in cluster_order:
            bucket = clusters[k]
            if idx < len(bucket):
                # Cycle among top 4 marketable variations in each track rather than drifting into rare phrases
                cap = min(len(bucket), 4)
                shift = max(0, rotation_offset // 2) % cap if idx == 0 else max(0, rotation_offset // 2)
                picked.append(bucket[(idx + shift) % len(bucket)])
                progressed = True
                if len(picked) >= limit:
                    break
        if not progressed:
            break
        idx += 1
    return picked


def extract_title_variations_from_inputs(
    *,
    target_titles: list[str],
    role_families: list[str],
    cv_titles: list[str] | None = None,
    industries: list[str] | None = None,
) -> list[str]:
    """Combine preferences titles, CV-evidenced titles, and concise role families without duplicates."""
    seen: set[str] = set()
    ordered: list[str] = []

    def _add(item: str) -> None:
        cleaned = " ".join((item or "").strip().split())
        if not cleaned or len(cleaned) < 2:
            return
        low = cleaned.lower()
        if low not in seen:
            seen.add(low)
            ordered.append(cleaned)

    for t in target_titles or []:
        _add(t)
    if not ordered:
        for t in cv_titles or []:
            _add(t)
    # Only mix broad role-family nouns into search titles if explicit/CV titles are sparse
    if len(ordered) < 4:
        for rf in role_families or []:
            short_rf = rf.split("&")[0].strip() if "&" in rf else rf.strip()
            _add(short_rf)
    if not ordered:
        for ind in industries or []:
            _add(ind)
    return ordered


def _clean_brave_title(title: str) -> str:
    """Simplify punctuation like '/' or '&' inside exact Brave title quotes so site: queries match."""
    cleaned = title.replace("/", " ").replace("&", " ")
    return " ".join(cleaned.split())


def format_query_for_provider(planned: PlannedSearchQuery, provider_name: str) -> str:
    """
    Adapt query string syntax per provider:
    - Brave uses domain/operator queries while suppressing non-crawlable social & SEO guide domains.
    - Tavily uses clean boolean/natural phrases paired with `include_domains`/`exclude_domains`/`country`
      (and never receives invalid `site:.ccTLD` suffix wildcards that cause off-country results).
    """
    loc = planned.location_term.strip()
    if provider_name == "brave":
        b_title = _clean_brave_title(planned.title_term)
        if planned.source_group == "localized":
            return planned.query
        if planned.source_group == "social_hiring_posts" and planned.preferred_domains:
            dom = planned.preferred_domains[0].split("/", 1)[0]
            # Avoid parenthesized multi-OR expressions that cause Brave site: filters to return 0 results
            return f'site:{dom} "{b_title}" "send your CV" {loc}'.strip()
        if planned.source_group == "email_first_directory" and planned.preferred_domains:
            dom = planned.preferred_domains[0].split("/", 1)[0]
            return f'site:{dom} "{b_title}" ("send your CV" OR "careers@" OR "hr@" OR "apply") {loc}'.strip()
        excl = (
            "-site:linkedin.com -site:facebook.com -site:instagram.com -site:tiktok.com "
            "-site:freejobalert.com -site:buzzon.khaleejtimes.com -site:jaabz.com "
            "-site:enhancv.com -site:jobglance.app -site:facilio.com "
            "-site:curtindubai.ac.ae -site:coursetakers.ae -site:nobleprog.ae -site:henryharvin.ae "
            "-inurl:resume -inurl:what-is"
            if planned.exclude_generic_portals
            else ""
        )
        if planned.source_group == "inbox_handle_signals":
            return f'"{b_title}" ("recruitment@" OR "careers@" OR "hr@" OR "jobs@") {loc} {excl}'.strip()
        if planned.source_group == "company_careers":
            cctld = _COUNTRY_CCTLDS.get((planned.country_code or "").upper(), "")
            if cctld and planned.provider_hint == "brave":
                return f'site:{cctld} "{b_title}" ("careers@" OR "jobs@" OR "send your CV" OR "email your CV") {loc}'.strip()
            return f'"{b_title}" ("careers" OR "vacancies") ("send your CV" OR "email your CV" OR "careers@" OR "jobs@") {loc} {excl}'.strip()
        return f'"{b_title}" ("send your CV" OR "send your resume" OR "email your CV" OR "send CV to") {loc} {excl}'.strip()

    # Tavily & other providers: never pass broken `site:.ae` wildcards
    raw_q = planned.query.strip()
    if raw_q.startswith("site:."):
        return f'"{planned.title_term}" ("careers" OR "vacancies") ("send your CV" OR "careers@" OR "hr@") {loc}'.strip()
    return raw_q


def build_email_application_discovery_plan(
    *,
    target_titles: list[str],
    role_families: list[str],
    target_locations: list[dict],
    search_languages: list[str],
    include_standing_invitations: bool,
    cv_titles: list[str] | None = None,
    industries: list[str] | None = None,
    residence_country_code: str = "",
    residence_city: str = "",
    work_authorizations: list[str] | None = None,
    relocation_targets: list[str] | None = None,
    willing_to_relocate: bool = False,
    allow_relocation: bool = False,
    allowed_remote_modes: list[str] | None = None,
    rotation_offset: int = 0,
    max_queries: int = 6,
    excluded_keywords: list[str] | None = None,
) -> list[PlannedSearchQuery]:
    """
    Build an Email Application Discovery plan combining:
    - CV-supported & user-target job titles and variations
    - User-selected countries, cities, and local languages (never silently substituting bare 'Remote')
    - Explicit email-intent recruitment phrases & inbox prefixes ('send your CV', 'careers@', etc.)
    - Public social hiring posts (linkedin.com/posts, facebook.com) & official company careers pages
    - Cross-batch rotation & page offsets so repeated batches inspect fresh results.
    """
    titles = extract_title_variations_from_inputs(
        target_titles=target_titles,
        role_families=role_families,
        cv_titles=cv_titles,
        industries=industries,
    )
    exclusions = [re.compile(rf"(?<!\w){re.escape(term.strip())}(?!\w)", re.I)
                  for term in excluded_keywords or [] if term.strip()]
    titles = [t for t in titles if not any(pattern.search(t) for pattern in exclusions)]
    if not titles or max_queries <= 0:
        return []
    langs = [lg.lower().strip() for lg in (search_languages or ["en"]) if lg.strip()] or ["en"]
    location_slots, _country_codes = resolve_user_search_locations(
        target_locations=target_locations,
        residence_country_code=residence_country_code,
        residence_city=residence_city,
        work_authorizations=work_authorizations,
        relocation_targets=relocation_targets,
        willing_to_relocate=willing_to_relocate,
        allow_relocation=allow_relocation,
        search_languages=langs,
    )

    # resolve_user_search_locations supplies commercial hubs for country-wide targets.
    # An explicit city selection takes precedence over those suggested hubs.
    selected_cities: dict[str, list[str]] = {}
    for location in target_locations or []:
        code = str(location.get("country_code") or resolve_country_code(location.get("country_name") or "") or "").upper()
        cities = list(location.get("cities") or []) if isinstance(location.get("cities"), list) else []
        cities.append(location.get("city"))
        for city in cities:
            if code and isinstance(city, str) and city.strip():
                selected_cities.setdefault(code, []).append(city.strip())
    for slot in location_slots:
        cities = selected_cities.get(slot["country_code"])
        if cities:
            slot["cities"] = list(dict.fromkeys(cities))
            slot["terms"] = []
            slot["ar_terms"] = []
    allow_global_remote = "remote_worldwide" in set(allowed_remote_modes or []) and not location_slots
    if not location_slots:
        if allow_global_remote:
            location_slots = [
                {
                    "country_code": "",
                    "country_name": "",
                    "cities": ["Remote Worldwide"],
                    "terms": ['"remote worldwide"'],
                    "ar_terms": [],
                }
            ]
        else:
            location_slots = [{"country_code": "", "country_name": "", "cities": [""], "terms": [""], "ar_terms": []}]

    title_pool = _select_diverse_titles(titles, limit=10, rotation_offset=rotation_offset)
    industry_hint = industries[0].strip() if industries else ""
    industry_buckets = classify_industry_buckets(target_titles, role_families, industries)
    page_num = (rotation_offset // 2) % 2

    en_direct_phrases = [
        '"send your CV"',
        '"email your CV"',
        '"send your resume"',
        '"apply by email"',
    ]
    en_handle_phrases = [
        '"careers@"',
        '"recruitment@"',
        '"hr@"',
        '"jobs@"',
    ]

    pool: list[PlannedSearchQuery] = []

    pairs: list[tuple[int, str, dict]] = []
    max_titles = min(len(title_pool), 8)
    for t_idx in range(max_titles):
        title = title_pool[t_idx]
        for l_idx, slot in enumerate(location_slots):
            pairs.append((t_idx + l_idx, title, slot))

    # Group 0 (High-yield): International Email-First Job Source Directory arms matched per country & industry
    data_kw = ("data", "mis", "bi", "power", "reporting", "business", "automation")
    ops_kw = ("cafm", "cmms", "maximo", "facilities", "maintenance", "operations", "project", "document")
    niche_skip = ("trainer", "sla ", "kpi ", "data processing", "junior ", "senior ")
    for slot_idx, slot in enumerate(location_slots):
        cc = slot["country_code"]
        cname = slot.get("country_name", "")
        dir_entries = select_directory_sources(
            country_codes=[cc] if cc else ["GLOBAL"],
            industry_buckets=industry_buckets,
            include_portals=False,
        )
        if not dir_entries:
            continue
        loc_candidates = slot["cities"][:2] + slot["terms"][:2]
        loc_candidates = [lc for lc in loc_candidates if lc] or [""]
        loc_term = loc_candidates[(rotation_offset + slot_idx) % len(loc_candidates)]

        # Build country-wide location clause prioritizing primary commercial cities + compact country name (e.g. Dubai, Abu Dhabi, UAE)
        loc_or_items: list[str] = []
        compact_terms = [t for t in (slot.get("terms") or []) if len((t or "").strip()) <= 15]
        for raw_l in list(slot.get("cities") or [])[:2] + compact_terms + [loc_term, cname]:
            val = (raw_l or "").strip()
            if val and (len(val) <= 15 or val in (slot.get("cities") or [])) and val not in loc_or_items:
                loc_or_items.append(val)
            if len(loc_or_items) >= 3:
                break
        if not loc_or_items and cname:
            loc_or_items = [cname]
        dir_loc = (
            "(" + " OR ".join(f'"{x}"' if " " in x else x for x in loc_or_items) + ")"
            if len(loc_or_items) >= 2
            else (loc_or_items[0] if loc_or_items else (cname or ""))
        )
        if cc in selected_cities:
            country_term = "UAE" if cc == "AE" else cname
            dir_loc = f'"{loc_term}" {country_term}'.strip()

        board_domains = tuple(e.domain for e in dir_entries if e.category == "email_first_board")
        classified_domains = tuple(e.domain for e in dir_entries if e.category in ("classifieds", "newspaper_classifieds", "startup_community"))

        # Partition candidate role titles cleanly into two high-yield tracks (Data/BI vs CAFM/Ops/Doc)
        # while prioritizing core marketable titles over verbose variations.
        data_titles = [
            t for t in titles
            if any(k in t.lower() for k in data_kw) and not any(ns in t.lower() for ns in niche_skip)
        ]
        # Prefer concise 1-3 word titles (e.g. Data Analyst, Business Analyst, MIS Analyst) first
        data_titles.sort(key=lambda t: (len(t.split()) > 3, t.lower() not in ("data analyst", "business analyst", "mis analyst", "power bi analyst")))

        raw_ops = [
            t for t in titles
            if any(k in t.lower() for k in ops_kw) and t not in data_titles and not any(ns in t.lower() for ns in niche_skip)
        ]
        # If candidate has CAFM/Maximo roles, include concise "CAFM" token alongside core role titles for board recall
        ops_titles: list[str] = []
        if any("cafm" in t.lower() for t in raw_ops):
            ops_titles.append("CAFM")
        for ot in raw_ops:
            if ot not in ops_titles:
                ops_titles.append(ot)
        ops_titles.sort(key=lambda t: (len(t.split()) > 2 and t != "Document Controller", t.lower() not in ("cafm", "facilities coordinator", "maintenance coordinator", "document controller", "operations coordinator")))

        t_shift = (rotation_offset // 2 + slot_idx)
        if len(data_titles) >= 2:
            top_d = data_titles[:6]
            s_d = t_shift % min(len(top_d), 3)
            rot_data = (top_d[s_d:] + top_d[:s_d])[:4]
        else:
            rot_data = title_pool[:4]
        if len(ops_titles) >= 2:
            top_o = ops_titles[:6]
            s_o = t_shift % min(len(top_o), 3)
            rot_ops = (top_o[s_o:] + top_o[:s_o])[:4]
        else:
            rot_ops = title_pool[4:8] or title_pool[:4]

        if (rotation_offset // 2) % 2 == 0:
            cluster_a, cluster_b = rot_data, rot_ops
        else:
            cluster_a, cluster_b = rot_ops, rot_data

        if board_domains:
            rot_boards = board_domains[(rotation_offset % len(board_domains)):] + board_domains[:(rotation_offset % len(board_domains))]
            or_roles_a = " OR ".join(f'"{t}"' for t in cluster_a)
            pool.append(
                PlannedSearchQuery(
                    query=f'({or_roles_a}) ("send CV" OR "send your CV" OR "email" OR "careers@" OR "hr@") {dir_loc}'.strip(),
                    source_group="email_first_directory",
                    country_code=cc,
                    country_name=cname,
                    location_term=loc_term,
                    title_term=cluster_a[0],
                    language="en",
                    page=page_num,
                    exclude_generic_portals=False,
                    preferred_domains=rot_boards,
                    provider_hint="tavily",
                )
            )
            if len(cluster_b) >= 2:
                or_roles_b = " OR ".join(f'"{t}"' for t in cluster_b)
                pool.append(
                    PlannedSearchQuery(
                        query=f'({or_roles_b}) ("send your CV" OR "email your CV" OR "recruitment@" OR "hr@") {dir_loc}'.strip(),
                        source_group="email_first_directory",
                        country_code=cc,
                        country_name=cname,
                        location_term=loc_term,
                        title_term=cluster_b[0],
                        language="en",
                        page=page_num,
                        exclude_generic_portals=False,
                        preferred_domains=rot_boards,
                        provider_hint="tavily",
                    )
                )

        if classified_domains:
            rot_cls = classified_domains[(rotation_offset % len(classified_domains)):] + classified_domains[:(rotation_offset % len(classified_domains))]
            or_cls = " OR ".join(f'"{t}"' for t in cluster_a)
            cls_loc = dir_loc or loc_term or cname
            pool.append(
                PlannedSearchQuery(
                    query=f'({or_cls}) ("we are hiring" OR "send your CV" OR "email CV" OR "careers@") {cls_loc} -"job seekers" -"open to relocate"'.strip(),
                    source_group="email_first_directory",
                    country_code=cc,
                    country_name=cname,
                    location_term=loc_term,
                    title_term=cluster_a[0],
                    language="en",
                    page=page_num,
                    exclude_generic_portals=False,
                    preferred_domains=rot_cls,
                    provider_hint="tavily",
                )
            )

    for t_idx, title, slot in pairs:
        cc = slot["country_code"]
        cname = slot.get("country_name", "")
        loc_candidates = slot["cities"][:2] + slot["terms"][:2]
        loc_candidates = [lc for lc in loc_candidates if lc] or [""]
        loc_term = loc_candidates[(rotation_offset + t_idx) % len(loc_candidates)]

        # Group 1: Direct email-application search (suppress portals & blocked social domains on primary crawlable searches)
        direct_phrase = en_direct_phrases[(rotation_offset + t_idx) % len(en_direct_phrases)]
        pool.append(
            PlannedSearchQuery(
                query=f'"{title}" {direct_phrase} {loc_term} -"resume builder" -"how to get"'.strip(),
                source_group="direct_email_posts",
                country_code=cc,
                country_name=cname,
                location_term=loc_term,
                title_term=title,
                language="en",
                page=page_num,
                exclude_generic_portals=True,
                provider_hint="brave" if t_idx % 2 == 0 else "tavily",
            )
        )

        # Group 2: Recruitment email prefixes & hiring signals
        handle_phrase = en_handle_phrases[(rotation_offset + t_idx) % len(en_handle_phrases)]
        ind_part = f'"{industry_hint}" ' if industry_hint and t_idx % 2 == 1 else ""
        pool.append(
            PlannedSearchQuery(
                query=f'"{title}" {ind_part}{handle_phrase} {loc_term}'.strip(),
                source_group="inbox_handle_signals",
                country_code=cc,
                country_name=cname,
                location_term=loc_term,
                title_term=title,
                language="en",
                page=page_num,
                exclude_generic_portals=True,
                provider_hint="brave",
            )
        )

        # Group 3: Public indexed recruiter & social posts (kept sparse: 1-2 per pool cycle since many block HTTP fetchers)
        if t_idx == 0 or t_idx == 3:
            use_linkedin = (t_idx + rotation_offset) % 2 == 0
            dom = "linkedin.com/posts" if use_linkedin else "facebook.com"
            short_title = " ".join(title.split()[:2]) if len(title.split()) > 2 else title
            social_q = (
                f'site:linkedin.com/posts/ "{short_title}" "send your CV" {loc_term}'.strip()
                if use_linkedin
                else f'site:facebook.com/ "{title}" ("send your CV" OR "send your resume" OR "email") {loc_term}'.strip()
            )
            pool.append(
                PlannedSearchQuery(
                    query=social_q,
                    source_group="social_hiring_posts",
                    country_code=cc,
                    country_name=cname,
                    location_term=loc_term,
                    title_term=short_title if use_linkedin else title,
                    language="en",
                    page=page_num,
                    exclude_generic_portals=False,
                    preferred_domains=(dom,),
                )
            )

        # Group 4: Company career pages & open career invitations (crawlable official employer websites)
        careers_phrase = (
            '"send your CV" OR "careers@" OR "jobs@"'
            if include_standing_invitations
            else '"send your CV" OR "apply by email"'
        )
        cctld = _COUNTRY_CCTLDS.get(cc, "")
        career_q = f'"{title}" ("careers" OR "vacancies") ({careers_phrase}) {loc_term}'.strip()
        career_brave = bool(cctld and (t_idx + rotation_offset) % 2 == 1)
        pool.append(
            PlannedSearchQuery(
                query=career_q,
                source_group="company_careers",
                country_code=cc,
                country_name=cname,
                location_term=loc_term,
                title_term=title,
                language="en",
                page=0 if career_brave else page_num,
                exclude_generic_portals=True,
                preferred_domains=(cctld,) if cctld else (),
                provider_hint="brave" if career_brave else "tavily",
            )
        )

        # Group 5: Local-language queries (Arabic etc.)
        for lang in langs:
            if lang == "en":
                continue
            phrases = LOCALIZED_SEND_CV_PHRASES.get(lang, [])
            if not phrases:
                continue
            phrase = phrases[(rotation_offset + t_idx) % len(phrases)]
            loc_local = (slot.get("ar_terms") or [loc_term])[0] if lang == "ar" else loc_term
            pool.append(
                PlannedSearchQuery(
                    query=f'"{title}" {phrase} {loc_local}'.strip(),
                    source_group="localized",
                    country_code=cc,
                    country_name=cname,
                    location_term=loc_local,
                    title_term=title,
                    language=lang,
                    page=page_num,
                    exclude_generic_portals=True,
                )
            )

    pool = [item for item in pool if item.language in langs]
    if not pool:
        return []

    n = len(pool)
    start = rotation_offset % n
    ordered = pool[start:] + pool[:start]

    selected: list[PlannedSearchQuery] = []
    seen_q: set[str] = set()
    used_groups: set[str] = set()
    used_countries: set[str] = set()
    used_titles: set[str] = set()

    # Pass 1a: ensure every target country/region is covered first (preferring crawlable queries & fresh titles)
    non_social = [it for it in ordered if it.source_group != "social_hiring_posts"]
    for require_new_title in (True, False):
        for item in (non_social + ordered):
            if (
                item.country_code not in used_countries
                and item.query not in seen_q
                and (not require_new_title or item.title_term not in used_titles)
            ):
                selected.append(item)
                seen_q.add(item.query)
                used_groups.add(item.source_group)
                used_countries.add(item.country_code)
                used_titles.add(item.title_term)
            if len(selected) >= max_queries:
                return selected

    # Pass 1b: ensure every distinct source channel is covered, preferring untouched job titles (never skip directory arms)
    for require_new_title in (True, False):
        for item in ordered:
            if (
                item.source_group not in used_groups
                and item.query not in seen_q
                and (item.source_group == "email_first_directory" or not require_new_title or item.title_term not in used_titles)
            ):
                selected.append(item)
                seen_q.add(item.query)
                used_groups.add(item.source_group)
                used_countries.add(item.country_code)
                used_titles.add(item.title_term)
            if len(selected) >= max_queries:
                return selected

    # Pass 1c: include secondary directory tracks & classifieds before general title expansion
    for item in pool:
        if item.source_group == "email_first_directory" and item.query not in seen_q:
            selected.append(item)
            seen_q.add(item.query)
            used_groups.add(item.source_group)
            used_countries.add(item.country_code)
            used_titles.add(item.title_term)
        if len(selected) >= max_queries:
            return selected

    for item in ordered:
        if item.title_term not in used_titles and item.query not in seen_q:
            selected.append(item)
            seen_q.add(item.query)
            used_groups.add(item.source_group)
            used_countries.add(item.country_code)
            used_titles.add(item.title_term)
        if len(selected) >= max_queries:
            return selected

    for item in ordered:
        if item.query not in seen_q:
            selected.append(item)
            seen_q.add(item.query)
        if len(selected) >= max_queries:
            break
    return selected


def build_rotated_queries(
    *,
    target_titles: list[str],
    role_families: list[str],
    target_locations: list[dict],
    search_languages: list[str],
    include_standing_invitations: bool,
    rotation_offset: int = 0,
    max_queries: int = 6,
) -> list[str]:
    """Backwards-compatible flat query list returning rotated Email Application Discovery strings."""
    plan = build_email_application_discovery_plan(
        target_titles=target_titles,
        role_families=role_families,
        target_locations=target_locations,
        search_languages=search_languages,
        include_standing_invitations=include_standing_invitations,
        rotation_offset=rotation_offset,
        max_queries=max_queries,
    )
    return [item.query for item in plan]
