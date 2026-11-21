"""Worldwide geographic dataset, city disambiguation, and remote-work eligibility classification."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

import pycountry

# Region memberships for remote-within-region eligibility checks
COUNTRY_TO_REGIONS: dict[str, set[str]] = {
    # EU / EEA / Europe
    **{
        cc: {"EU", "EEA", "EUROPE", "EMEA"}
        for cc in [
            "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR",
            "HU", "IE", "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK",
            "SI", "ES", "SE",
        ]
    },
    **{cc: {"EEA", "EUROPE", "EMEA"} for cc in ["IS", "LI", "NO"]},
    **{cc: {"EUROPE", "EMEA"} for cc in ["GB", "CH", "UA", "RS", "TR"]},
    # MENA / GCC
    **{cc: {"GCC", "MENA", "EMEA"} for cc in ["AE", "SA", "QA", "KW", "BH", "OM"]},
    **{cc: {"MENA", "EMEA"} for cc in ["EG", "JO", "LB", "MA", "TN", "DZ", "IQ"]},
    # North America
    **{cc: {"NORTH_AMERICA", "AMERICAS"} for cc in ["US", "CA", "MX"]},
    # LATAM
    **{cc: {"LATAM", "AMERICAS"} for cc in ["BR", "AR", "CL", "CO", "PE", "UY", "CR"]},
    # APAC
    **{cc: {"APAC"} for cc in ["AU", "NZ", "SG", "JP", "KR", "IN", "ID", "MY", "PH", "TH", "VN"]},
}

REGION_ALIASES: dict[str, str] = {
    "eu": "EU",
    "european union": "EU",
    "eea": "EEA",
    "europe": "EUROPE",
    "emea": "EMEA",
    "mena": "MENA",
    "middle east": "MENA",
    "gcc": "GCC",
    "north america": "NORTH_AMERICA",
    "us/canada": "NORTH_AMERICA",
    "latam": "LATAM",
    "latin america": "LATAM",
    "americas": "AMERICAS",
    "apac": "APAC",
    "asia pacific": "APAC",
}


@dataclass(frozen=True)
class GeoCityEntry:
    geo_id: str
    city: str
    admin_region: str
    country_code: str
    country_name: str
    aliases: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        if self.admin_region:
            return f"{self.city}, {self.admin_region}, {self.country_name}"
        return f"{self.city}, {self.country_name}"


# Curated worldwide geographic dataset including explicitly ambiguous city names across countries
CURATED_CITIES: tuple[GeoCityEntry, ...] = (
    GeoCityEntry("eg-cairo", "Cairo", "Cairo Governorate", "EG", "Egypt", ("القاهرة",)),
    GeoCityEntry("us-il-cairo", "Cairo", "Illinois", "US", "United States"),
    GeoCityEntry("eg-alexandria", "Alexandria", "Alexandria Governorate", "EG", "Egypt", ("الإسكندرية",)),
    GeoCityEntry("us-va-alexandria", "Alexandria", "Virginia", "US", "United States"),
    GeoCityEntry("ae-dubai", "Dubai", "Dubai", "AE", "United Arab Emirates", ("دبي",)),
    GeoCityEntry("ae-abudhabi", "Abu Dhabi", "Abu Dhabi", "AE", "United Arab Emirates", ("أبوظبي",)),
    GeoCityEntry("ae-sharjah", "Sharjah", "Sharjah", "AE", "United Arab Emirates", ("الشارقة",)),
    GeoCityEntry("sa-riyadh", "Riyadh", "Riyadh Province", "SA", "Saudi Arabia", ("الرياض",)),
    GeoCityEntry("sa-jeddah", "Jeddah", "Makkah Province", "SA", "Saudi Arabia", ("جدة",)),
    GeoCityEntry("sa-dammam", "Dammam", "Eastern Province", "SA", "Saudi Arabia", ("الدمام",)),
    GeoCityEntry("sa-khobar", "Al Khobar", "Eastern Province", "SA", "Saudi Arabia", ("Khobar", "الخبر")),
    GeoCityEntry("qa-doha", "Doha", "Doha", "QA", "Qatar", ("الدوحة",)),
    GeoCityEntry("kw-kuwaitcity", "Kuwait City", "Al Asimah", "KW", "Kuwait", ("Kuwait", "مدينة الكويت")),
    GeoCityEntry("om-muscat", "Muscat", "Muscat", "OM", "Oman", ("مسقط",)),
    GeoCityEntry("bh-manama", "Manama", "Capital Governorate", "BH", "Bahrain", ("المنامة",)),
    GeoCityEntry("jo-amman", "Amman", "Amman", "JO", "Jordan", ("عمان",)),
    GeoCityEntry("gb-london", "London", "England", "GB", "United Kingdom"),
    GeoCityEntry("ca-on-london", "London", "Ontario", "CA", "Canada"),
    GeoCityEntry("gb-cambridge", "Cambridge", "England", "GB", "United Kingdom"),
    GeoCityEntry("us-ma-cambridge", "Cambridge", "Massachusetts", "US", "United States"),
    GeoCityEntry("fr-paris", "Paris", "Île-de-France", "FR", "France"),
    GeoCityEntry("us-tx-paris", "Paris", "Texas", "US", "United States"),
    GeoCityEntry("de-berlin", "Berlin", "Berlin", "DE", "Germany"),
    GeoCityEntry("de-munich", "Munich", "Bavaria", "DE", "Germany", ("München",)),
    GeoCityEntry("nl-amsterdam", "Amsterdam", "North Holland", "NL", "Netherlands"),
    GeoCityEntry("ie-dublin", "Dublin", "Leinster", "IE", "Ireland"),
    GeoCityEntry("es-madrid", "Madrid", "Community of Madrid", "ES", "Spain"),
    GeoCityEntry("es-barcelona", "Barcelona", "Catalonia", "ES", "Spain"),
    GeoCityEntry("ch-zurich", "Zurich", "Zurich", "CH", "Switzerland", ("Zürich",)),
    GeoCityEntry("us-ny-nyc", "New York", "New York", "US", "United States", ("NYC",)),
    GeoCityEntry("us-ca-sf", "San Francisco", "California", "US", "United States", ("SF",)),
    GeoCityEntry("us-ca-sanjose", "San Jose", "California", "US", "United States", ("San José",)),
    GeoCityEntry("cr-sanjose", "San José", "San José", "CR", "Costa Rica", ("San Jose",)),
    GeoCityEntry("us-il-springfield", "Springfield", "Illinois", "US", "United States"),
    GeoCityEntry("us-ma-springfield", "Springfield", "Massachusetts", "US", "United States"),
    GeoCityEntry("us-mo-springfield", "Springfield", "Missouri", "US", "United States"),
    GeoCityEntry("ca-on-toronto", "Toronto", "Ontario", "CA", "Canada"),
    GeoCityEntry("ca-bc-vancouver", "Vancouver", "British Columbia", "CA", "Canada"),
    GeoCityEntry("au-nsw-sydney", "Sydney", "New South Wales", "AU", "Australia"),
    GeoCityEntry("au-vic-melbourne", "Melbourne", "Victoria", "AU", "Australia"),
    GeoCityEntry("ca-bc-victoria", "Victoria", "British Columbia", "CA", "Canada"),
    GeoCityEntry("sc-victoria", "Victoria", "Mahé", "SC", "Seychelles"),
    GeoCityEntry("sg-singapore", "Singapore", "Singapore", "SG", "Singapore"),
    GeoCityEntry("jp-tokyo", "Tokyo", "Tokyo", "JP", "Japan", ("東京",)),
    GeoCityEntry("in-bengaluru", "Bengaluru", "Karnataka", "IN", "India", ("Bangalore",)),
    GeoCityEntry("br-saopaulo", "São Paulo", "São Paulo", "BR", "Brazil", ("Sao Paulo",)),
    GeoCityEntry("za-capetown", "Cape Town", "Western Cape", "ZA", "South Africa"),
    GeoCityEntry("ly-tripoli", "Tripoli", "Tripoli", "LY", "Libya", ("طرابلس",)),
    GeoCityEntry("lb-tripoli", "Tripoli", "North Governorate", "LB", "Lebanon", ("طرابلس",)),
)

COUNTRY_NAME_ALIASES: dict[str, str] = {
    "uk": "GB",
    "u.k.": "GB",
    "united kingdom": "GB",
    "great britain": "GB",
    "england": "GB",
    "usa": "US",
    "u.s.a.": "US",
    "u.s.": "US",
    "united states": "US",
    "united states of america": "US",
    "uae": "AE",
    "u.a.e.": "AE",
    "united arab emirates": "AE",
    "الإمارات": "AE",
    "السعودية": "SA",
    "المملكة العربية السعودية": "SA",
    "مصر": "EG",
    "قطر": "QA",
    "الأردن": "JO",
    "ألمانيا": "DE",
    "فرنسا": "FR",
    "كندا": "CA",
    "أستراليا": "AU",
    "ksa": "SA",
    "kingdom of saudi arabia": "SA",
    "deutschland": "DE",
}


def _norm(text: str) -> str:
    return unicodedata.normalize("NFKC", (text or "")).strip().lower()


def list_countries() -> list[dict[str, str]]:
    """Return sorted ISO-3166-1 countries from pycountry."""
    items = []
    for country in pycountry.countries:
        items.append({"code": country.alpha_2, "name": getattr(country, "common_name", country.name)})
    items.sort(key=lambda item: item["name"])
    return items


def resolve_country_code(text: str) -> str:
    """Resolve country name, ISO alpha-2/3 code, or localized alias to ISO alpha-2."""
    cleaned = _norm(text)
    if not cleaned:
        return ""
    if cleaned in COUNTRY_NAME_ALIASES:
        return COUNTRY_NAME_ALIASES[cleaned]
    if len(cleaned) == 2:
        c = pycountry.countries.get(alpha_2=cleaned.upper())
        if c:
            return c.alpha_2
    if len(cleaned) == 3:
        c = pycountry.countries.get(alpha_3=cleaned.upper())
        if c:
            return c.alpha_2
    try:
        matches = pycountry.countries.search_fuzzy(text.strip())
        if matches:
            return matches[0].alpha_2
    except LookupError:
        pass
    return ""


def search_geo_locations(query: str, limit: int = 15) -> list[dict[str, str]]:
    """Search countries and curated cities for unambiguous target selection."""
    q = _norm(query)
    results: list[dict[str, str]] = []
    for entry in CURATED_CITIES:
        haystack = [_norm(entry.city), _norm(entry.admin_region), _norm(entry.country_name), *[_norm(a) for a in entry.aliases]]
        if not q or any(q in h for h in haystack):
            results.append(
                {
                    "geo_id": entry.geo_id,
                    "kind": "city",
                    "city": entry.city,
                    "admin_region": entry.admin_region,
                    "country_code": entry.country_code,
                    "country_name": entry.country_name,
                    "label": entry.label,
                }
            )
            if len(results) >= limit:
                return results
    for c in list_countries():
        if not q or q in _norm(c["name"]) or q == _norm(c["code"]):
            results.append(
                {
                    "geo_id": f"country-{c['code'].lower()}",
                    "kind": "country",
                    "city": "",
                    "admin_region": "",
                    "country_code": c["code"],
                    "country_name": c["name"],
                    "label": c["name"],
                }
            )
            if len(results) >= limit:
                break
    return results


@dataclass(frozen=True)
class ParsedLocationResult:
    raw_text: str
    country_code: str
    city: str
    remote_scope: str
    allowed_regions: tuple[str, ...]
    is_ambiguous: bool
    candidates: tuple[str, ...]
    location_confidence: str = "explicit"  # explicit | inferred_text | domain_hint | unknown


def parse_job_location(
    location_raw: str,
    jd_text: str = "",
    *,
    title: str = "",
    source_url: str = "",
) -> ParsedLocationResult:
    """
    Parse job location & remote scope carefully without silently guessing ambiguous cities.

    If a city name exists in multiple countries (e.g. 'Cairo', 'Alexandria', 'Springfield', 'London')
    and no country or state qualifier is present, mark `is_ambiguous=True` and leave `country_code=""`.
    """
    raw = (location_raw or "").strip()
    inferred_conf = "explicit" if raw else "unknown"
    if not raw and (title or jd_text or source_url):
        raw, inferred_conf = infer_location_hint_with_confidence(title=title, url=source_url, jd_text=jd_text)

    combined = f"{raw}\n{title}\n{jd_text[:1800]}".lower()

    # Classify remote scope carefully (avoid Workday "remote type: On-site" trap!)
    remote_scope = "onsite"
    allowed_regions: list[str] = []
    if re.search(r"remote\s*type\s*[:\-]?\s*on[-\s]?site", combined):
        remote_scope = "onsite"
    elif re.search(r"\b(worldwide remote|remote\s*[-–]?\s*worldwide|work from anywhere|global remote|remote globally|anyone anywhere|عن بعد\s*من أي مكان)\b", combined):
        remote_scope = "remote_worldwide"
    elif re.search(r"\b(fully\s+remote|100%\s+remote|remote\s+work|remote\s+role|remote\s+position|work\s+from\s+home|wfh|\(remote\)|remote\s+in\b|عن بعد)\b", combined) or (
        raw and re.search(r"\bremote\b", raw, re.IGNORECASE)
    ):
        for alias, region_code in REGION_ALIASES.items():
            if re.search(rf"\bremote\b.*\b{re.escape(alias)}\b|\b{re.escape(alias)}\b.*\bremote\b|\b{re.escape(alias)}\s+only\b", combined):
                remote_scope = "remote_region"
                if region_code not in allowed_regions:
                    allowed_regions.append(region_code)
        if remote_scope != "remote_region":
            remote_scope = "remote_country"
    elif re.search(r"\bhybrid\b|\bهجين\b", combined):
        remote_scope = "hybrid"

    if not raw:
        return ParsedLocationResult(
            raw_text="",
            country_code="",
            city="",
            remote_scope="remote_worldwide" if remote_scope == "remote_worldwide" else ("unknown" if remote_scope == "onsite" else remote_scope),
            allowed_regions=tuple(allowed_regions),
            is_ambiguous=False,
            candidates=(),
            location_confidence="unknown",
        )

    # Split comma/slash/paren parts
    tokens = [t.strip() for t in re.split(r"[,/|()\-–]+", raw) if t.strip()]
    resolved_country = ""
    for tok in reversed(tokens):
        cc = resolve_country_code(tok)
        if cc:
            resolved_country = cc
            break

    # US state abbreviation disambiguation if present (e.g. "Springfield, IL" or "Cambridge, MA")
    us_state_map = {"il": "Illinois", "ma": "Massachusetts", "mo": "Missouri", "va": "Virginia", "ny": "New York", "ca": "California", "tx": "Texas"}
    resolved_admin = ""
    for tok in tokens:
        low = _norm(tok)
        if low in us_state_map:
            resolved_admin = us_state_map[low]
            resolved_country = "US"

    # Match curated cities
    matching_cities: list[GeoCityEntry] = []
    for tok in tokens:
        ntok = _norm(tok)
        for entry in CURATED_CITIES:
            if ntok == _norm(entry.city) or ntok in [_norm(a) for a in entry.aliases]:
                matching_cities.append(entry)

    # Check if raw or title embeds a curated city alongside resolved_country (e.g. "jobs in dubai uae")
    if not matching_cities and resolved_country and inferred_conf != "domain_hint":
        head_probe = f"{raw} {title} {(jd_text or '')[:450]}"
        for entry in CURATED_CITIES:
            if entry.country_code == resolved_country:
                for nm in (entry.city, *entry.aliases):
                    if len(nm) >= 4 and re.search(rf"(?<!\w){re.escape(nm)}(?!\w)", head_probe, re.IGNORECASE):
                        matching_cities.append(entry)
                        break

    if matching_cities:
        if resolved_country:
            filtered = [e for e in matching_cities if e.country_code == resolved_country]
            if resolved_admin:
                filtered_admin = [e for e in filtered if _norm(e.admin_region) == _norm(resolved_admin)]
                if filtered_admin:
                    filtered = filtered_admin
            if len(filtered) == 1:
                chosen = filtered[0]
                return ParsedLocationResult(
                    raw_text=raw,
                    country_code=chosen.country_code,
                    city=chosen.city,
                    remote_scope=remote_scope,
                    allowed_regions=tuple(allowed_regions),
                    is_ambiguous=False,
                    candidates=(chosen.label,),
                    location_confidence=inferred_conf,
                )
            if len(filtered) > 1:
                return ParsedLocationResult(
                    raw_text=raw,
                    country_code=resolved_country,
                    city=filtered[0].city,
                    remote_scope=remote_scope,
                    allowed_regions=tuple(allowed_regions),
                    is_ambiguous=True,
                    candidates=tuple(e.label for e in filtered),
                    location_confidence=inferred_conf,
                )
        elif len({e.country_code for e in matching_cities}) > 1 or len(matching_cities) > 1:
            return ParsedLocationResult(
                raw_text=raw,
                country_code="",
                city=matching_cities[0].city,
                remote_scope=remote_scope,
                allowed_regions=tuple(allowed_regions),
                is_ambiguous=True,
                candidates=tuple(e.label for e in matching_cities),
                location_confidence="unknown",
            )
        else:
            only = matching_cities[0]
            return ParsedLocationResult(
                raw_text=raw,
                country_code=only.country_code,
                city=only.city,
                remote_scope=remote_scope,
                allowed_regions=tuple(allowed_regions),
                is_ambiguous=False,
                candidates=(only.label,),
                location_confidence=inferred_conf,
            )

    # Check if raw text mentions a country directly even when city wasn't in curated list
    city_guess = tokens[0] if (len(tokens) >= 2 and resolved_country) else ""
    if city_guess and resolve_country_code(city_guess) == resolved_country:
        city_guess = ""

    return ParsedLocationResult(
        raw_text=raw,
        country_code=resolved_country,
        city=city_guess,
        remote_scope=remote_scope,
        allowed_regions=tuple(allowed_regions),
        is_ambiguous=False,
        candidates=(),
        location_confidence=inferred_conf if resolved_country else "unknown",
    )


def country_in_region(country_code: str, region_code: str) -> bool:
    if not country_code or not region_code:
        return False
    return region_code.upper() in COUNTRY_TO_REGIONS.get(country_code.upper(), set())


_UNAMBIGUOUS_CITY_COUNTS: dict[str, int] = {}
for _ce in CURATED_CITIES:
    _UNAMBIGUOUS_CITY_COUNTS[_norm(_ce.city)] = _UNAMBIGUOUS_CITY_COUNTS.get(_norm(_ce.city), 0) + 1

_KNOWN_COUNTRY_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(?:united\s+arab\s+emirates|u\.?a\.?e\.?|الإمارات)\b", re.IGNORECASE), "United Arab Emirates"),
    (re.compile(r"\b(?:saudi\s+arabia|k\.?s\.?a\.?|السعودية|المملكة العربية السعودية)\b", re.IGNORECASE), "Saudi Arabia"),
    (re.compile(r"\b(?:united\s+kingdom|great\s+britain|england|scotland|wales|within\s+uk\s+only|in\s+the\s+uk)\b", re.IGNORECASE), "United Kingdom"),
    (re.compile(r"\b(?:united\s+states|u\.?s\.?a\.?|north\s+carolina\s*\(usa\)|\(usa\))\b", re.IGNORECASE), "United States"),
    (re.compile(r"\b(?:qatar|قطر)\b", re.IGNORECASE), "Qatar"),
    (re.compile(r"\b(?:oman|عمان)\b", re.IGNORECASE), "Oman"),
    (re.compile(r"\b(?:kuwait|الكويت)\b", re.IGNORECASE), "Kuwait"),
    (re.compile(r"\b(?:bahrain|البحرين)\b", re.IGNORECASE), "Bahrain"),
    (re.compile(r"\b(?:egypt|مصر)\b", re.IGNORECASE), "Egypt"),
    (re.compile(r"\b(?:germany|deutschland|ألمانيا)\b", re.IGNORECASE), "Germany"),
    (re.compile(r"\b(?:canada)\b", re.IGNORECASE), "Canada"),
    (re.compile(r"\b(?:australia)\b", re.IGNORECASE), "Australia"),
    (re.compile(r"\b(?:singapore)\b", re.IGNORECASE), "Singapore"),
    (re.compile(r"\b(?:india)\b", re.IGNORECASE), "India"),
    (re.compile(r"\b(?:indonesia)\b", re.IGNORECASE), "Indonesia"),
    (re.compile(r"\b(?:ireland)\b", re.IGNORECASE), "Ireland"),
]

_CCTLD_COUNTRY_MAP: dict[str, str] = {
    ".co.uk": "United Kingdom",
    ".ac.uk": "United Kingdom",
    ".gov.uk": "United Kingdom",
    ".org.uk": "United Kingdom",
    ".ae": "United Arab Emirates",
    ".sa": "Saudi Arabia",
    ".qa": "Qatar",
    ".kw": "Kuwait",
    ".om": "Oman",
    ".bh": "Bahrain",
    ".de": "Germany",
    ".fr": "France",
    ".ca": "Canada",
    ".au": "Australia",
    ".sg": "Singapore",
    ".in": "India",
    ".co.id": "Indonesia",
}


def infer_location_hint_with_confidence(*, title: str = "", url: str = "", jd_text: str = "") -> tuple[str, str]:
    """
    Extract location hint with explicit provenance confidence (`explicit` | `inferred_text` | `domain_hint` | `unknown`).
    Prioritizes explicit labeled lines and title/top-body city+country over unrelated footer text or bare domain suffixes.
    """
    head_text = (jd_text or "")[:800]
    title_str = title or ""
    combined = f"{title_str}\n{head_text}"

    # 1. Explicit "Location: ..." field line in structured HTML/snippet
    loc_line = re.search(
        r"(?:^|\n)\s*(?:location|city|country|based\s+in|work\s+location)\s*[:\-]\s*([^\n|]{2,80})",
        combined,
        re.IGNORECASE,
    )
    if loc_line:
        candidate = loc_line.group(1).strip(" 📍•🔹\t")
        if candidate and candidate.lower() not in ("remote", "hybrid", "full time", "permanent", "flexible", "unspecified"):
            return candidate, "explicit"

    def _find_city(text_blob: str) -> GeoCityEntry | None:
        for entry in CURATED_CITIES:
            if _UNAMBIGUOUS_CITY_COUNTS.get(_norm(entry.city), 0) > 1:
                continue
            for nm in (entry.city, *entry.aliases):
                if len(nm) >= 4 and re.search(rf"(?<!\w){re.escape(nm)}(?!\w)", text_blob, re.IGNORECASE):
                    return entry
        return None

    def _find_country(text_blob: str) -> str:
        for pat, cname in _KNOWN_COUNTRY_PATTERNS:
            if pat.search(text_blob):
                return cname
        return ""

    # 2. Title city/country takes precedence so deep feed/classifieds footer ads never overwrite headline city
    title_city = _find_city(title_str)
    title_country = _find_country(title_str)
    if title_city:
        if not title_country or _norm(title_city.country_name) == _norm(title_country):
            return f"{title_city.city}, {title_city.country_name}", "inferred_text"
    if title_country:
        body_city = _find_city(head_text[:400])
        if body_city and _norm(body_city.country_name) == _norm(title_country):
            return f"{body_city.city}, {title_country}", "inferred_text"
        return title_country, "inferred_text"

    # 3. Top & full body unambiguous city & country (avoiding deep classifieds sidebar override via head_text first)
    body_city = _find_city(head_text)
    body_country = _find_country(head_text)
    if body_city and (not body_country or _norm(body_city.country_name) == _norm(body_country)):
        return f"{body_city.city}, {body_city.country_name}", "inferred_text"
    if body_country:
        return body_country, "inferred_text"

    mid_text = (jd_text or "")[:3200]
    if len(jd_text or "") > 800:
        deep_city = _find_city(mid_text)
        deep_country = _find_country(mid_text)
        if deep_city and (not deep_country or _norm(deep_city.country_name) == _norm(deep_country)):
            return f"{deep_city.city}, {deep_city.country_name}", "inferred_text"
        if deep_country:
            return deep_country, "inferred_text"

    tail_text = (jd_text or "")[-1200:]
    if len(jd_text or "") > 3200 and tail_text:
        tail_city = _find_city(tail_text)
        tail_country = _find_country(tail_text)
        if tail_city and (not tail_country or _norm(tail_city.country_name) == _norm(tail_country)):
            return f"{tail_city.city}, {tail_city.country_name}", "inferred_text"
        if tail_country:
            return tail_country, "inferred_text"

    full_contact_slice = f"{(jd_text or '')[:3200]}\n{tail_text}"
    if re.search(r"\+971[\s\-]?\d", full_contact_slice) or re.search(r"P\.?\s*O\.?\s*Box[^\n]{0,40}\b(?:Dubai|Abu\s+Dhabi|Sharjah|Ajman|UAE)\b", full_contact_slice, re.IGNORECASE):
        return "United Arab Emirates", "inferred_text"
    if re.search(r"\+966[\s\-]?\d", full_contact_slice):
        return "Saudi Arabia", "inferred_text"
    if re.search(r"\+974[\s\-]?\d", full_contact_slice):
        return "Qatar", "inferred_text"

    # 4. Regional phrases in top text
    if re.search(r"\bGreater\s+London\b|\bStoke-[Oo]n-[Tt]rent\b|\bKingston\s+[Uu]pon\s+Thames\b|\bCannock\b", combined):
        return "United Kingdom", "inferred_text"

    # 5. Authoritative country host subdomains / ccTLDs (strictly marked as domain_hint, NOT definitive location!)
    host = ""
    if url:
        from urllib.parse import urlparse

        host = (urlparse(url).hostname or "").lower()
    if host:
        if host.startswith(("uk.", "gb.")):
            return "United Kingdom", "domain_hint"
        if host.startswith("us.") and re.search(r"\$\d|usa\b|united\s+states", combined, re.IGNORECASE):
            return "United States", "domain_hint"
        if "khaleejtimes.com" in host or "dubaivacancy.ae" in host:
            return "United Arab Emirates", "domain_hint"
        for suffix, cname in _CCTLD_COUNTRY_MAP.items():
            if host.endswith(suffix):
                return cname, "domain_hint"

    return "", "unknown"


def infer_location_hint_from_metadata(*, title: str = "", url: str = "", jd_text: str = "") -> str:
    """Backwards-compatible helper returning only the inferred location string."""
    hint, _conf = infer_location_hint_with_confidence(title=title, url=url, jd_text=jd_text)
    return hint


COUNTRY_QUERY_SHORT_NAMES: dict[str, tuple[str, ...]] = {
    "AE": ("UAE", "United Arab Emirates"),
    "SA": ("Saudi Arabia", "KSA"),
    "QA": ("Qatar",),
    "KW": ("Kuwait",),
    "OM": ("Oman",),
    "BH": ("Bahrain",),
    "EG": ("Egypt",),
    "GB": ("UK", "United Kingdom"),
    "US": ("USA", "United States"),
    "DE": ("Germany",),
    "FR": ("France",),
    "CA": ("Canada",),
    "AU": ("Australia",),
    "SG": ("Singapore",),
}

ARABIC_COUNTRY_NAMES: dict[str, tuple[str, ...]] = {
    "AE": ("الإمارات", "دبي", "أبوظبي"),
    "SA": ("السعودية", "الرياض", "جدة"),
    "QA": ("قطر", "الدوحة"),
    "KW": ("الكويت",),
    "OM": ("عمان", "مسقط"),
    "BH": ("البحرين", "المنامة"),
    "EG": ("مصر", "القاهرة"),
    "JO": ("الأردن", "عمان"),
}


def resolve_user_search_locations(
    *,
    target_locations: list[dict] | None,
    residence_country_code: str = "",
    residence_city: str = "",
    work_authorizations: list[str] | None = None,
    relocation_targets: list[str] | None = None,
    willing_to_relocate: bool = False,
    allow_relocation: bool = False,
    search_languages: list[str] | None = None,
) -> tuple[list[dict], list[str]]:
    """
    Build structured search-location slots and target ISO country codes from preferences,
    falling back to candidate residence/work-authorization country rather than bare 'Remote'.
    """
    include_ar = any((lng or "").lower() == "ar" for lng in (search_languages or []))
    country_slots: dict[str, dict] = {}

    def _add_slot(cc: str, cname: str = "", city_name: str = "") -> None:
        code = (cc or "").upper().strip()
        if not code:
            return
        if code not in country_slots:
            resolved_name = cname
            if not resolved_name:
                c_obj = pycountry.countries.get(alpha_2=code)
                resolved_name = getattr(c_obj, "common_name", c_obj.name) if c_obj else code
            short_names = list(COUNTRY_QUERY_SHORT_NAMES.get(code, (resolved_name,)))
            if resolved_name not in short_names:
                short_names.append(resolved_name)
            ar_terms = list(ARABIC_COUNTRY_NAMES.get(code, ())) if include_ar else []
            country_slots[code] = {
                "country_code": code,
                "country_name": resolved_name,
                "cities": [],
                "terms": short_names,
                "ar_terms": ar_terms,
            }
        if city_name and city_name not in country_slots[code]["cities"]:
            country_slots[code]["cities"].append(city_name)
        if not city_name or len(country_slots[code]["cities"]) < 2:
            # Add top curated hubs for country-level coverage after primary residence city
            for ce in CURATED_CITIES:
                if ce.country_code == code and ce.city not in country_slots[code]["cities"]:
                    country_slots[code]["cities"].append(ce.city)
                    if len(country_slots[code]["cities"]) >= 3:
                        break

    for loc in target_locations or []:
        if not loc:
            continue
        cc = str(loc.get("country_code") or "").upper().strip()
        if not cc and loc.get("country_name"):
            cc = resolve_country_code(str(loc["country_name"]))
        if not cc and loc.get("label"):
            parsed = parse_job_location(str(loc["label"]))
            cc = parsed.country_code
        _add_slot(cc, str(loc.get("country_name") or ""), str(loc.get("city") or ""))

    if not country_slots:
        if residence_country_code:
            _add_slot(residence_country_code, city_name=residence_city)
        for auth_cc in work_authorizations or []:
            if auth_cc:
                _add_slot(auth_cc)
        if willing_to_relocate and allow_relocation:
            for rel_cc in relocation_targets or []:
                if rel_cc:
                    _add_slot(rel_cc)

    ordered_slots = list(country_slots.values())
    return ordered_slots, list(country_slots.keys())
