"""Evidence-based international mandatory eligibility extraction and deterministic validation."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import pycountry

from app.db.models import CandidateProfile, CvExtractedFact, CvVersion, JobPosting
from app.services.cv_parser import effective_cv_evidence
from app.services.geo import COUNTRY_NAME_ALIASES, COUNTRY_TO_REGIONS, parse_job_location

GCC_COUNTRY_CODES = {"AE", "SA", "QA", "OM", "KW", "BH"}
EU_COUNTRY_CODES = {cc for cc, regs in COUNTRY_TO_REGIONS.items() if "EU" in regs}

# Common demonyms mapped to ISO 3166-1 alpha-2 codes (supplemented dynamically with pycountry country names)
DEMONYM_TO_ISO: dict[str, set[str]] = {
    "emirati": {"AE"},
    "emiratis": {"AE"},
    "uae national": {"AE"},
    "uae nationals": {"AE"},
    "uae citizen": {"AE"},
    "uae citizens": {"AE"},
    "emiratisation": {"AE"},
    "emiratization": {"AE"},
    "tawteen": {"AE"},
    "saudi": {"SA"},
    "saudis": {"SA"},
    "saudi national": {"SA"},
    "saudi nationals": {"SA"},
    "saudi citizen": {"SA"},
    "saudi citizens": {"SA"},
    "saudi arabian": {"SA"},
    "ksa national": {"SA"},
    "ksa nationals": {"SA"},
    "saudisation": {"SA"},
    "saudization": {"SA"},
    "nitaqat": {"SA"},
    "qatari": {"QA"},
    "qataris": {"QA"},
    "qatari national": {"QA"},
    "qatari nationals": {"QA"},
    "qatarisation": {"QA"},
    "qatarization": {"QA"},
    "omani": {"OM"},
    "omanis": {"OM"},
    "omani national": {"OM"},
    "omani nationals": {"OM"},
    "omanisation": {"OM"},
    "omanization": {"OM"},
    "kuwaiti": {"KW"},
    "kuwaitis": {"KW"},
    "kuwaiti national": {"KW"},
    "kuwaiti nationals": {"KW"},
    "kuwaitisation": {"KW"},
    "kuwaitization": {"KW"},
    "bahraini": {"BH"},
    "bahrainis": {"BH"},
    "bahraini national": {"BH"},
    "bahraini nationals": {"BH"},
    "bahrainisation": {"BH"},
    "bahrainization": {"BH"},
    "gcc national": set(GCC_COUNTRY_CODES),
    "gcc nationals": set(GCC_COUNTRY_CODES),
    "gcc citizen": set(GCC_COUNTRY_CODES),
    "gcc citizens": set(GCC_COUNTRY_CODES),
    "american": {"US"},
    "us citizen": {"US"},
    "us citizens": {"US"},
    "u.s. citizen": {"US"},
    "u.s. citizens": {"US"},
    "us national": {"US"},
    "us nationals": {"US"},
    "british": {"GB"},
    "uk citizen": {"GB"},
    "uk citizens": {"GB"},
    "uk national": {"GB"},
    "uk nationals": {"GB"},
    "canadian": {"CA"},
    "canadian citizen": {"CA"},
    "australian": {"AU"},
    "australian citizen": {"AU"},
    "new zealander": {"NZ"},
    "german": {"DE"},
    "french": {"FR"},
    "dutch": {"NL"},
    "spanish": {"ES"},
    "italian": {"IT"},
    "irish": {"IE"},
    "swiss": {"CH"},
    "portuguese": {"PT"},
    "polish": {"PL"},
    "belgian": {"BE"},
    "austrian": {"AT"},
    "swedish": {"SE"},
    "norwegian": {"NO"},
    "danish": {"DK"},
    "finnish": {"FI"},
    "singaporean": {"SG"},
    "malaysian": {"MY"},
    "indian": {"IN"},
    "pakistani": {"PK"},
    "filipino": {"PH"},
    "filipina": {"PH"},
    "egyptian": {"EG"},
    "jordanian": {"JO"},
    "lebanese": {"LB"},
    "south african": {"ZA"},
    "nigerian": {"NG"},
    "kenyan": {"KE"},
    "japanese": {"JP"},
    "korean": {"KR"},
    "eu citizen": set(EU_COUNTRY_CODES),
    "eu citizens": set(EU_COUNTRY_CODES),
    "eu national": set(EU_COUNTRY_CODES),
    "eu nationals": set(EU_COUNTRY_CODES),
    "eea citizen": set(EU_COUNTRY_CODES) | {"NO", "IS", "LI"},
    "eea national": set(EU_COUNTRY_CODES) | {"NO", "IS", "LI"},
    # Arabic demonyms & nationalization terms
    "إماراتي": {"AE"},
    "اماراتي": {"AE"},
    "إماراتيين": {"AE"},
    "اماراتيين": {"AE"},
    "مواطن إماراتي": {"AE"},
    "مواطني الإمارات": {"AE"},
    "توطين": {"AE"},
    "سعودي": {"SA"},
    "سعوديين": {"SA"},
    "للسعوديين": {"SA"},
    "مواطن سعودي": {"SA"},
    "سعودة": {"SA"},
    "نطاقات": {"SA"},
    "قطري": {"QA"},
    "قطريين": {"QA"},
    "عماني": {"OM"},
    "عمانيين": {"OM"},
    "كويتي": {"KW"},
    "كويتيين": {"KW"},
    "بحريني": {"BH"},
    "بحرينيين": {"BH"},
    "خليجي": set(GCC_COUNTRY_CODES),
    "خليجيين": set(GCC_COUNTRY_CODES),
}

PREFERENCE_MARKERS_RE = re.compile(
    r"\b(preferred|preferably|preference|desirable|nice\s+to\s+have|advantageous|advantage|an\s+asset|"
    r"a\s+plus|bonus|encouraged\s+to\s+apply|good\s+to\s+have|would\s+be\s+a\s+plus|optional)\b|"
    r"(?:يفضل|أفضلية|ميزة\s+إضافية|مستحب)",
    re.IGNORECASE,
)

MANDATORY_MARKERS_RE = re.compile(
    r"\b(only|exclusively|must|mandatory|required|strictly|essential|prerequisite|compulsory|"
    r"needed|need\s+to\s+(?:hold|have|be)|should\s+(?:hold|possess|be)|no\s+exception)\b|"
    r"(?:فقط|حصراً|حصرا|إلزامي|الزامي|مطلوب|يشترط|شرط\s+أساسي)",
    re.IGNORECASE,
)

NEGATED_NATIONALITY_RE = re.compile(
    r"\b(all\s+nationalities|any\s+nationality|irrespective\s+of\s+nationality|open\s+to\s+all|"
    r"regardless\s+of\s+nationality|no\s+nationality\s+restriction|expats?\s+welcome|non[\-\s]?emirati\s+may\s+apply)\b|"
    r"(?:جميع\s+الجنسيات|مفتوح\s+لجميع\s+الجنسيات)",
    re.IGNORECASE,
)

LANGUAGE_ALIASES: dict[str, tuple[str, ...]] = {
    "en": ("english", "إنجليزي", "انجليزي", "الإنجليزية", "الانجليزية"),
    "ar": ("arabic", "عربي", "العربية"),
    "fr": ("french", "الفرنسية", "فرنسي"),
    "de": ("german", "الألمانية", "ألماني"),
    "es": ("spanish", "الإسبانية"),
    "hi": ("hindi", "الهندية"),
    "ur": ("urdu", "الأردية"),
    "ta": ("tamil",),
    "zh": ("mandarin", "chinese", "الصينية"),
    "ja": ("japanese", "اليابانية"),
    "pt": ("portuguese", "البرتغالية"),
    "it": ("italian", "الإيطالية"),
    "nl": ("dutch", "الهولندية"),
    "ru": ("russian", "الروسية"),
    "tr": ("turkish", "التركية"),
}

LANG_LEVEL_ORDER = {"none": 0, "low": 1, "medium": 2, "high": 3}

# Canonical certification / licence patterns: (name, detection_regex, evidence_tokens_in_cv, country_hint)
KNOWN_LICENCES_AND_CERTS: list[tuple[str, str, tuple[str, ...], str]] = [
    ("UAE Driving Licence", r"\buae\s+(?:valid\s+)?(?:manual|light\s+vehicle\s+|auto(?:matic)?\s+)?driving\s+licen[cs]e\b|\bvalid\s+uae\s+(?:manual|light\s+vehicle\s+|auto(?:matic)?\s+)?driving\s+licen[cs]e\b|\bdriving\s+licen[cs]e\s*[:\-]\s*(?:valid\s+)?uae\b", ("uae driving licen", "emirates driving licen", "uae valid driving", "valid uae driving licen", "driving licence - uae", "driving license - uae", "driving licence: uae", "driving license: uae"), "AE"),
    ("Saudi Driving Licence", r"\b(?:saudi|ksa)\s+(?:valid\s+)?driving\s+licen[cs]e\b", ("saudi driving licen", "ksa driving licen", "valid saudi driving licen"), "SA"),
    ("GCC Driving Licence", r"\bgcc\s+(?:valid\s+)?driving\s+licen[cs]e\b", ("gcc driving licen", "uae driving licen", "saudi driving licen"), ""),
    ("Valid Driving Licence", r"\b(?:valid\s+)?driving\s+licen[cs]e\b", ("driving licen", "driver's license", "drivers license"), ""),
    ("PMP Certification", r"\bpmp\b(?:\s+certif(?:ied|ication))?|\bproject\s+management\s+professional\b", ("pmp", "project management professional"), ""),
    ("PRINCE2 Certification", r"\bprince2\b", ("prince2",), ""),
    ("NEBOSH Certification", r"\bnebosh\b", ("nebosh",), ""),
    ("IOSH Certification", r"\biosh\b", ("iosh",), ""),
    ("ISO 41001 Certification", r"\biso\s*41001\b", ("iso 41001", "iso41001"), ""),
    ("ASHRAE Certification", r"\bashrae\b", ("ashrae",), ""),
    ("ITIL Certification", r"\bitil\b", ("itil",), ""),
    ("CPA", r"\bcpa\b", ("cpa", "certified public accountant"), ""),
    ("ACCA", r"\bacca\b", ("acca",), ""),
    ("CMA", r"\bcma\b", ("cma", "certified management accountant"), ""),
    ("CFA", r"\bcfa\b", ("cfa", "chartered financial analyst"), ""),
    ("SOCPA", r"\bsocpa\b", ("socpa",), "SA"),
    ("SCE Registration (Saudi Council of Engineers)", r"\bsce\b|\bsaudi\s+council\s+of\s+engineers\b", ("saudi council of engineers", "sce"), "SA"),
    ("MMUP / UPDA Registration", r"\b(?:mmup|upda)\b", ("mmup", "upda"), "QA"),
    ("PE / P.Eng / CEng Licence", r"\b(?:p\.?eng|ceng|chartered\s+engineer|pe\s+licen[cs]e)\b", ("p.eng", "ceng", "chartered engineer", "pe license"), ""),
    ("DHA / DOH / MOH Healthcare Licence", r"\b(?:dha|doh|haad|moh|scfhs)\s+licen[cs]e\b", ("dha", "doh", "haad", "moh", "scfhs"), ""),
    ("CISSP", r"\bcissp\b", ("cissp",), ""),
    ("CISA", r"\bcisa\b", ("cisa",), ""),
    ("Security Clearance", r"\bsecurity\s+clearance\b|\bbaseline\s+clearance\b|\bnv1\b|\bsc\s+clearance\b", ("security clearance", "baseline clearance", "nv1"), ""),
]


@dataclass
class MandatoryEligibilityRequirements:
    nationality_restrictions: list[dict[str, Any]] = field(default_factory=list)
    preferred_nationalities: list[dict[str, Any]] = field(default_factory=list)
    local_residency_requirements: list[dict[str, Any]] = field(default_factory=list)
    work_permit_requirements: list[dict[str, Any]] = field(default_factory=list)
    certification_requirements: list[dict[str, Any]] = field(default_factory=list)
    education_requirements: list[dict[str, Any]] = field(default_factory=list)
    experience_requirement: dict[str, Any] = field(default_factory=dict)
    language_requirements: list[dict[str, Any]] = field(default_factory=list)
    immediate_joining_requirement: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "nationality_restrictions": self.nationality_restrictions,
            "preferred_nationalities": self.preferred_nationalities,
            "local_residency_requirements": self.local_residency_requirements,
            "work_permit_requirements": self.work_permit_requirements,
            "certification_requirements": self.certification_requirements,
            "education_requirements": self.education_requirements,
            "experience_requirement": self.experience_requirement,
            "language_requirements": self.language_requirements,
            "immediate_joining_requirement": self.immediate_joining_requirement,
        }


@dataclass
class MandatoryEligibilityResult:
    passed: bool
    profile_passed: bool
    status: str  # eligible | incompatible | unknown_eligibility
    failure_codes: list[str] = field(default_factory=list)
    failures: list[dict[str, str]] = field(default_factory=list)
    preference_notes: list[str] = field(default_factory=list)

    def failure_messages(self) -> list[str]:
        return [f["reason"] for f in self.failures]


def _resolve_country_token(token: str) -> set[str]:
    low = re.sub(r"[^a-z\u0600-\u06FF.\s\-]", " ", (token or "").lower()).strip()
    low = re.sub(r"\s+", " ", low)
    if not low:
        return set()
    if low in DEMONYM_TO_ISO:
        return set(DEMONYM_TO_ISO[low])
    # Check multi-word or single demonyms inside string
    matched: set[str] = set()
    for key, codes in DEMONYM_TO_ISO.items():
        if re.search(rf"(?<!\w){re.escape(key)}(?!\w)", low):
            matched |= codes
    if matched:
        return matched
    if low in COUNTRY_NAME_ALIASES:
        return {COUNTRY_NAME_ALIASES[low]}
    for c in pycountry.countries:
        cname = c.name.lower()
        if cname == low or getattr(c, "common_name", "").lower() == low:
            return {c.alpha_2.upper()}
    return set()


def _infer_clause_level(clause: str, section_default: str = "neutral") -> str:
    if re.search(r"\b(?:not\s+(?:required|mandatory|essential)|no\s+requirement\s+for)\b", clause, re.I):
        return "preferred"
    has_pref = bool(PREFERENCE_MARKERS_RE.search(clause))
    has_mand = bool(MANDATORY_MARKERS_RE.search(clause))
    if has_mand and not has_pref:
        return "mandatory"
    if has_pref and not has_mand:
        return "preferred"
    if has_mand and has_pref:
        # Check if 'only'/'must'/'mandatory' outweighs e.g. 'preferred' in another phrase
        if re.search(r"\b(only|mandatory|strictly|must\s+be|exclusively)\b", clause, re.IGNORECASE):
            return "mandatory"
        return "preferred"
    return section_default


def _normalize_clauses(
    *,
    title: str,
    jd_text: str,
    subject_instruction: str,
    evidence_quote: str,
    structured_data: dict[str, Any] | None,
) -> list[tuple[str, str, str]]:
    """Return list of (source_name, section_default, clause_text) preserving provenance & context."""
    blocks: list[tuple[str, str]] = []
    if (subject_instruction or "").strip():
        blocks.append(("subject_instruction", (subject_instruction or "").strip()))
    if (title or "").strip():
        blocks.append(("title", (title or "").strip()))
    if (evidence_quote or "").strip():
        blocks.append(("application_instructions", (evidence_quote or "").strip()))
    if (jd_text or "").strip():
        blocks.append(("jd_text", (jd_text or "").strip()))

    if structured_data:
        extra_bits: list[str] = []
        for k in ("qualifications", "skills", "experienceRequirements", "educationRequirements", "occupationalCategory", "applicantLocationRequirements"):
            val = structured_data.get(k)
            if isinstance(val, list):
                extra_bits.extend(str(x) for x in val if x)
            elif isinstance(val, dict):
                extra_bits.append(str(val))
            elif isinstance(val, str) and val.strip():
                extra_bits.append(val.strip())
        if extra_bits:
            blocks.append(("structured_data", "\n".join(extra_bits)))

    clauses: list[tuple[str, str, str]] = []
    for src, text in blocks:
        section_default = "neutral"
        for raw_line in text.splitlines():
            line = re.sub(r"\s+", " ", raw_line).strip()
            if not line:
                continue
            low_line = line.lower()
            if re.match(r"^(?:preferred|nice\s+to\s+have|desirable|good\s+to\s+have|plus|bonus|يفضل)\b[:\s]*$", low_line):
                section_default = "preferred"
                continue
            if re.match(r"^(?:requirements?|mandatory|minimum\s+requirements?|eligibility|qualifications?|general\s+employment\s+conditions|الشروط|المتطلبات)\b[:\s]*$", low_line):
                section_default = "mandatory"
                continue
            # Split long lines along sentence boundaries or bullet separators, keeping full clauses intact
            parts = [p.strip() for p in re.split(r"(?<=[.!?;])\s+|[•🔹]\s*", line) if p.strip()]
            for part in parts or [line]:
                # Separate independently qualified requirements, while preserving lists
                # such as "Arabic and English required" that share one qualifier.
                items = re.split(r",\s*|\s+(?:but|and)\s+", part, flags=re.I)
                if len(items) > 1 and all(MANDATORY_MARKERS_RE.search(i) or PREFERENCE_MARKERS_RE.search(i) for i in items):
                    clauses.extend((src, section_default, item) for item in items)
                else:
                    clauses.append((src, section_default, part))
    return clauses


def _extract_language_level_from_phrase(clause: str) -> str:
    low = clause.lower()
    if re.search(
        r"\b(native|mother\s+tongue|bilingual|fluent|excellent|proficient|proficiency|c1|c2|full\s+professional|written\s+and\s+spoken\s+fluently|must\s+speak\s+fluently)\b|"
        r"(?:طلاقة|لغة\s+أم|إتقان|اتقان|إجادة\s+تامة|ممتاز)",
        low,
    ):
        return "high"
    if re.search(r"\b(good|intermediate|conversational|working\s+knowledge|b1|b2|spoken\s+and\s+written)\b|(?:جيد|متوسط)", low):
        return "medium"
    return "any"


def extract_mandatory_eligibility_requirements(
    *,
    title: str = "",
    jd_text: str = "",
    subject_instruction: str = "",
    evidence_quote: str = "",
    location_raw: str = "",
    country_code: str = "",
    structured_data: dict[str, Any] | None = None,
) -> MandatoryEligibilityRequirements:
    """Extract mandatory vs preferred international eligibility rules from all posting evidence sources."""
    req = MandatoryEligibilityRequirements()
    clauses = _normalize_clauses(
        title=title,
        jd_text=jd_text,
        subject_instruction=subject_instruction,
        evidence_quote=evidence_quote,
        structured_data=structured_data,
    )
    fallback_country = (country_code or "").upper().strip()
    if not fallback_country and (location_raw or "").strip():
        fallback_country = parse_job_location(location_raw or "", "").country_code

    seen_nat: set[tuple[str, tuple[str, ...]]] = set()
    seen_res: set[tuple[str, tuple[str, ...], str]] = set()
    seen_permits: set[tuple[str, str]] = set()
    seen_certs: set[tuple[str, str]] = set()
    seen_edu: set[tuple[str, str]] = set()
    seen_langs: dict[str, dict[str, Any]] = {}

    for src, sec_def, clause in clauses:
        low = clause.lower()
        level = _infer_clause_level(clause, sec_def)

        # --- 1. Nationality / Citizenship / Nationalization Restrictions ---
        # Skip mandatory clauses if sentence explicitly opens role to all nationalities ("expats welcome", "all nationalities")
        negated_nat = bool(NEGATED_NATIONALITY_RE.search(clause))
        if not negated_nat or PREFERENCE_MARKERS_RE.search(clause):
            nat_codes: set[str] = set()
            nat_level = "preferred" if negated_nat else level
            nat_reason = ""

            # a) Subject-line explicit nationality format e.g. "UAE National – [Position Applied For]"
            if src == "subject_instruction" or re.search(r"^(?:email\s+)?subject(?:\s+line)?\s*[:\-]", clause, re.IGNORECASE):
                subj_m = re.search(
                    r"\b(uae\s+national|emirati|saudi\s+national|qatari\s+national|omani\s+national|kuwaiti\s+national|bahraini\s+national|us\s+citizen|uk\s+citizen|eu\s+citizen)\b",
                    clause,
                    re.IGNORECASE,
                )
                if subj_m and nat_level != "preferred":
                    nat_codes |= _resolve_country_token(subj_m.group(1))
                    nat_level = "mandatory"
                    nat_reason = f"Required application email subject demands '{subj_m.group(1)}'"

            # b) Explicit "Nationality: <Demonym/Country>" field (avoiding "Nationality: Open / Any")
            nat_field_m = re.search(
                r"\b(?:nationality|citizenship|الجنسية)\s*[:\-]\s*([A-Za-z\u0600-\u06FF\s,/&()]{2,80})",
                clause,
                re.IGNORECASE,
            )
            if nat_field_m:
                raw_val = nat_field_m.group(1).strip()
                resolved = _resolve_country_token(raw_val)
                if resolved:
                    nat_codes |= resolved
                    if nat_level != "preferred":
                        nat_level = "mandatory"
                    nat_reason = f"Explicit nationality requirement: '{clause[:160]}'"

            # c) Explicit "<Demonym/Country> nationals/citizens [only | preferred]" or "must be a <X> citizen"
            nat_phrase_patterns = [
                r"\b([A-Za-z.\s]{2,40}?)\s+(?:nationals?|citizens?|passport\s+holders?)\s+(?:(?:are|is)\s+)?(only|exclusively|required|mandatory|preferred|preferably|encouraged)\b",
                r"\b(only|exclusively)\s+(?:for\s+)?([A-Za-z.\s]{2,40}?)\s+(?:nationals?|citizens?)\b",
                r"\b(?:must\s+be\s+(?:a|an)\s+|restricted\s+to\s+)([A-Za-z.\s]{2,40}?)\s+(?:national|nationals|citizen|citizens)\b",
                r"\b(citizens?\s+or\s+permanent\s+residents?\s+(?:of|in)\s+(?:the\s+)?[A-Za-z.\s]{2,35}|us\s+citizens?\s+or\s+green\s+card(?:\s+holders?)?|[A-Za-z.\s]{2,30}\s+citizens?\s+or\s+(?:pr|p\.r\.?|permanent\s+residents?))\b",
                r"(للمواطنين(?:\s+(?:الإماراتيين|السعوديين|القطريين|العمانيين|الكويتيين|البحرينيين))?\s+فقط|للسعوديين\s+فقط|للإماراتيين\s+فقط|مواطنين\s+(?:فقط|حصراً))",
            ]
            for pat in nat_phrase_patterns:
                m = re.search(pat, clause, re.IGNORECASE)
                if not m:
                    continue
                matched_text = m.group(0)
                resolved = _resolve_country_token(matched_text)
                if not resolved and m.lastindex and m.lastindex >= 1:
                    resolved = _resolve_country_token(m.group(1))
                # If country was omitted in Arabic "للمواطنين فقط", use fallback job country
                if not resolved and "للمواطنين" in matched_text and fallback_country:
                    resolved = {fallback_country}
                if resolved:
                    nat_codes |= resolved
                    if PREFERENCE_MARKERS_RE.search(matched_text) or level == "preferred":
                        if not re.search(r"\b(only|exclusively|must)\b", matched_text, re.IGNORECASE):
                            nat_level = "preferred"
                        else:
                            nat_level = "mandatory"
                    else:
                        nat_level = "mandatory"
                    nat_reason = f"Explicit nationality clause: '{clause[:160]}'"

            # d) Nationalization programs (#emiratisation, #saudisation, Nitaqat, Tawteen, etc.)
            # Contextual interpretation: ignore unrelated negations or purely DEI mentions unless hiring/program quota signal
            nat_prog_m = re.search(
                r"(#?(?:emiratisation|emiratization|saudisation|saudization|omanisation|omanization|"
                r"qatarisation|qatarization|kuwaitisation|kuwaitization|bahrainisation|bahrainization|"
                r"tawteen|nitaqat)|#?emirates?talent|emirati\s+talent)\b",
                clause,
                re.IGNORECASE,
            )
            if nat_prog_m:
                tok = nat_prog_m.group(1).lstrip("#").lower()
                if "emirat" in tok or "tawteen" in tok:
                    prog_codes = {"AE"}
                elif "saud" in tok or "nitaqat" in tok:
                    prog_codes = {"SA"}
                elif "oman" in tok:
                    prog_codes = {"OM"}
                elif "qatar" in tok:
                    prog_codes = {"QA"}
                elif "kuwait" in tok:
                    prog_codes = {"KW"}
                elif "bahrain" in tok:
                    prog_codes = {"BH"}
                else:
                    prog_codes = set()
                # Check if general DEI note vs actual nationalization role / hashtag campaign / quota
                is_non_quota_note = bool(re.search(r"\b(not\s+(?:an?\s+)?emiratisation|supports?\s+diverse\s+talent\s+alongside)\b", low))
                if prog_codes and not is_non_quota_note:
                    nat_codes |= prog_codes
                    if nat_level != "preferred":
                        nat_level = "mandatory"
                    nat_reason = nat_reason or f"Nationalization requirement indicator ({nat_prog_m.group(0)})"

            if nat_codes:
                sorted_codes = tuple(sorted(nat_codes))
                key = (nat_level, sorted_codes)
                if key not in seen_nat:
                    seen_nat.add(key)
                    entry = {
                        "country_codes": list(sorted_codes),
                        "level": nat_level if nat_level == "preferred" else "mandatory",
                        "source": src,
                        "label": nat_reason or clause[:120],
                        "evidence": clause[:260],
                        "allows_permanent_resident": bool(re.search(r"\b(permanent\s+resident|green\s+card|pr\s+holder|pr|p\.r\.?|settled\s+status)\b", low)),
                    }
                    if entry["level"] == "preferred":
                        req.preferred_nationalities.append(entry)
                    else:
                        req.nationality_restrictions.append(entry)

        # --- 2. Local Residency / "Already in Country" Restrictions ---
        local_res_m = re.search(
            r"(?:"
            r"(?:candidates?|applicants?)\s+(?:must\s+(?:already\s+)?be\s+|currently\s+)?(?:in|inside|within|based\s+in|residing\s+in|living\s+in|located\s+in|available\s+in)\s+(?P<loc1>[A-Za-z\u0600-\u06FF\s,.-]{2,45}?)(?=\s+(?:only|exclusively|for|with|and|$))|"
            r"(?:currently\s+(?:residing|based|living|located)\s+in|must\s+(?:already\s+)?(?:reside|live|be\s+based|be\s+located|be\s+present|be\s+available)\s+in|locally\s+available(?:\s+in)?)\s*(?P<loc2>[A-Za-z\u0600-\u06FF\s,.-]{0,45})|"
            r"(?P<generic>in[\-\s]?country\s+(?:candidates?|applicants?)\s+only|local\s+(?:candidates?|applicants?|hire|hires)\s+only|locally\s+available\s+(?:candidates?\s+)?only|no\s+overseas\s+(?:candidates?|applicants?)|must\s+be\s+available\s+for\s+(?:an?\s+)?in[\-\s]?person\s+interview)|"
            r"(?:للمقيمين\s+(?:حالياً\s+|داخل\s+)?(?:في\s+)?(?P<loc_ar>[^\n,.]{2,35})?\s*فقط)"
            r")",
            clause,
            re.IGNORECASE,
        )
        if local_res_m:
            raw_loc = (local_res_m.group("loc1") or local_res_m.group("loc2") or local_res_m.group("loc_ar") or "").strip(" ,.-")
            # Avoid matching generic role header without mandatory local restriction like "Currently based in Dubai office" unless mandatory indicator present
            has_local_intent = bool(
                local_res_m.group("generic")
                or re.search(r"\b(only|must|currently\s+residing|locally\s+available|already\s+in|inside\s+(?:the\s+)?|no\s+overseas|residing\s+in\s+[A-Za-z]+\s+only)\b", low)
                or local_res_m.group("loc_ar") is not None
            )
            if has_local_intent:
                res_level = "preferred" if level == "preferred" and not re.search(r"\b(only|must|no\s+overseas|in-country\s+candidates?\s+only)\b", low) else "mandatory"
                parsed_res = parse_job_location(raw_loc, "") if raw_loc else None
                res_cc = parsed_res.country_code if parsed_res and parsed_res.country_code else fallback_country
                res_city = parsed_res.city if parsed_res and parsed_res.city else ""
                if res_cc:
                    rkey = (res_level, (res_cc,), res_city.lower())
                    if rkey not in seen_res:
                        seen_res.add(rkey)
                        req.local_residency_requirements.append(
                            {
                                "country_codes": [res_cc],
                                "city": res_city,
                                "level": res_level,
                                "source": src,
                                "label": f"Local residency required in {res_city + ', ' if res_city else ''}{res_cc}",
                                "evidence": clause[:260],
                            }
                        )

        # --- 3. Work Permits, Transferable Sponsorship (Iqama / QID), and No-Sponsorship Clauses ---
        permit_checks: list[tuple[str, str, str, str]] = [
            (
                "SA_TRANSFERABLE_IQAMA",
                "SA",
                r"\b(?:transferable\s+iqama|iqama\s+(?:transfer(?:able)?|with\s+noc)|valid\s+(?:transferable\s+)?iqama)\b|(?:نقل\s+كفالة(?:\s+سارية)?|إقامة\s+قابلة\s+للتحويل)",
                "Transferable Saudi Iqama required",
            ),
            (
                "QA_TRANSFERABLE_QID",
                "QA",
                r"\b(?:transferable\s+qid|qatar\s+id\s+(?:with\s+noc|transfer(?:able)?)|valid\s+qid\s+with\s+noc)\b|(?:نقل\s+كفالة\s+قطر|إقامة\s+قطرية\s+مع\s+عدم\s+ممانعة)",
                "Transferable Qatar ID (QID with NOC) required",
            ),
            (
                "NO_SPONSORSHIP",
                fallback_country,
                r"\b(?:no\s+visa\s+sponsorship|cannot\s+sponsor|unable\s+to\s+sponsor|without\s+(?:visa\s+)?sponsorship|"
                r"sponsorship\s+(?:is\s+)?not\s+(?:available|offered|provided)|must\s+not\s+require\s+(?:visa\s+)?sponsorship|"
                r"must\s+already\s+(?:hold|have|possess)\s+(?:a\s+)?(?:valid\s+)?work\s+(?:permit|visa|authorization)|"
                r"(?:under\s+|with\s+)?own\s+(?:valid\s+)?(?:uae\s+|residence\s+)?visa|spouse\s+visa|freelance\s+visa|husband'?s?\s+visa|"
                r"no\s+employment\s+visa\s+(?:will\s+be\s+)?provided)\b",
                "Valid existing work authorization without employer visa sponsorship required",
            ),
        ]
        for pcode, pcountry, ppat, plabel in permit_checks:
            if re.search(ppat, clause, re.IGNORECASE):
                plev = "preferred" if level == "preferred" and pcode != "NO_SPONSORSHIP" else "mandatory"
                if (pcode, pcountry) not in seen_permits:
                    seen_permits.add((pcode, pcountry))
                    req.work_permit_requirements.append(
                        {
                            "permit_code": pcode,
                            "country_code": pcountry,
                            "level": plev,
                            "source": src,
                            "label": plabel,
                            "evidence": clause[:260],
                        }
                    )

        # --- 4. Required Qualifications, Statutory Licences & Certifications ---
        for cname, cpat, ctokens, ccountry in KNOWN_LICENCES_AND_CERTS:
            # Specific driving licence guard: avoid matching general licence if specific UAE/Saudi/GCC matched on same clause
            if re.search(cpat, clause, re.IGNORECASE):
                if cname == "Valid Driving Licence" and any(x in clause.lower() for x in ("uae ", "saudi", "ksa ", "gcc ")):
                    continue
                # Notice generic mention in body without 'preferred' vs explicit 'required'/'mandatory' vs under preferred
                clev = level
                if clev == "neutral":
                    # Under a generic unlabelled bullet, only make driving licences / security clearance / statutory registrations
                    # strictly mandatory if clause explicitly states requirement/must/hold/valid; otherwise require explicit mandatory indicator for acronyms
                    if re.search(r"\b(must|mandatory|required|hold|possess|valid|approved)\b", low) or sec_def == "mandatory":
                        clev = "mandatory"
                    else:
                        clev = "preferred"
                ckey = (cname, clev)
                if ckey not in seen_certs:
                    seen_certs.add(ckey)
                    req.certification_requirements.append(
                        {
                            "name": cname,
                            "tokens": list(ctokens),
                            "country_code": ccountry,
                            "level": clev,
                            "source": src,
                            "evidence": clause[:260],
                        }
                    )

        # Mandatory degrees (Bachelor's / Master's / PhD) ONLY when explicitly required/mandatory
        deg_m = re.search(
            r"\b(?:bachelor'?s?|bsc|b\.sc|b\.e|b\.tech|master'?s?|msc|mba|phd|doctorate)(?:\s+degree)?\b",
            clause,
            re.IGNORECASE,
        )
        if deg_m and level == "mandatory":
            deg_tok = deg_m.group(0).lower()
            deg_name = "master" if any(x in deg_tok for x in ("master", "msc", "mba")) else ("phd" if "phd" in deg_tok or "doctor" in deg_tok else "bachelor")
            if (deg_name, "mandatory") not in seen_edu:
                seen_edu.add((deg_name, "mandatory"))
                req.education_requirements.append(
                    {
                        "degree": deg_name,
                        "level": "mandatory",
                        "source": src,
                        "evidence": clause[:260],
                    }
                )

        # --- 5. Mandatory Professional Experience Years ---
        # Distinguish explicit hard minimum ("minimum 5 years", "at least 5 years", "must have 5+ yrs")
        strict_exp_m = re.search(
            r"(?:minimum(?:\s+of)?|at\s+least|no\s+less\s+than|not\s+less\s+than|must\s+have(?:\s+at\s+least)?|"
            r"(?:لا\s+تقل\s+(?:الخبرة\s+)?عن|حد\s+أدنى))\s*(\d{1,2})\s*(?:[-–to]+\s*\d{1,2})?\s*\+?\s*(?:years?|yrs?|سنوات|سنة)",
            clause,
            re.IGNORECASE,
        )
        range_exp_m = re.search(
            r"(\d{1,2})\s*[-–to]+\s*\d{1,2}\s*(?:years?|yrs?)\s+(?:of\s+)?(?:relevant\s+)?experience",
            clause,
            re.IGNORECASE,
        )
        if strict_exp_m and level != "preferred":
            yrs = int(strict_exp_m.group(1))
            if yrs > (req.experience_requirement.get("min_years") or 0):
                req.experience_requirement = {
                    "min_years": yrs,
                    "level": "mandatory",
                    "strict_minimum": True,
                    "source": src,
                    "evidence": clause[:260],
                }
        elif range_exp_m and level == "mandatory" and not req.experience_requirement.get("min_years"):
            yrs = int(range_exp_m.group(1))
            req.experience_requirement = {
                "min_years": yrs,
                "level": "mandatory",
                "strict_minimum": True,
                "source": src,
                "evidence": clause[:260],
            }

        # --- 6. Mandatory Language Proficiency ---
        lang_context = bool(
            re.search(
                r"\b(fluent|native|bilingual|excellent|proficient|proficiency|good\s+spoken|written\s+and\s+spoken|"
                r"must\s+speak|c1|c2|b2|communication\s+skills|speaker|speakers)\b|"
                r"(?:إجادة|إتقان|اتقان|طلاقة|تحدث|لغة)",
                low,
            )
        )
        if lang_context:
            clause_level = level
            if clause_level == "neutral":
                # Phrases like "Excellent Arabic and English communication skills" or "Fluent Arabic required" are treated as mandatory requirements
                # unless explicitly marked preferred/nice-to-have
                clause_level = "mandatory" if re.search(
                    r"\b(fluent|native|bilingual|excellent|proficient|c1|c2|must\s+speak|required|mandatory)\b|(?:إجادة|طلاقة|إتقان|مطلوب)",
                    low,
                ) else "preferred"
            req_tier = _extract_language_level_from_phrase(clause)
            for lcode, aliases in LANGUAGE_ALIASES.items():
                if any(re.search(rf"(?<!\w){re.escape(al)}(?!\w)", low) for al in aliases):
                    prev = seen_langs.get(lcode)
                    cand_entry = {
                        "lang_code": lcode,
                        "min_level": req_tier,
                        "level": clause_level,
                        "source": src,
                        "evidence": clause[:260],
                    }
                    if not prev or (clause_level == "mandatory" and prev["level"] != "mandatory") or LANG_LEVEL_ORDER.get(req_tier, 0) > LANG_LEVEL_ORDER.get(prev["min_level"], 0):
                        seen_langs[lcode] = cand_entry

        # --- 7. Immediate Joining / Maximum Notice Period ---
        imm_m = re.search(
            r"\b(?:immediate\s+join(?:er|ers|ing)|must\s+join\s+immediately|available\s+(?:to\s+join\s+)?immediately|"
            r"immediately\s+available)\b|(?:انضمام\s+فوري|متاح\s+فوراً|مباشرة\s+العمل\s+فوراً)",
            clause,
            re.IGNORECASE,
        )
        notice_m = re.search(
            r"\b(?:max(?:imum)?|up\s+to|within|less\s+than|under)\s+(\d{1,2})\s+days?\s*(?:'?\s*)?notice\b",
            clause,
            re.IGNORECASE,
        )
        if notice_m and level != "preferred":
            days_cap = int(notice_m.group(1))
            req.immediate_joining_requirement = {
                "max_notice_days": days_cap,
                "level": "mandatory",
                "source": src,
                "evidence": clause[:260],
            }
        elif imm_m:
            imm_level = "preferred" if level == "preferred" and not re.search(r"\b(only|must|mandatory|required)\b", low) else (
                "mandatory" if (level == "mandatory" or re.search(r"\b(only|must\s+join|immediate\s+joiner(?:s)?\s+only|immediately\s+required)\b|(?:انضمام\s+فوري)", low)) else "preferred"
            )
            if not req.immediate_joining_requirement or imm_level == "mandatory":
                req.immediate_joining_requirement = {
                    "max_notice_days": 14,
                    "level": imm_level,
                    "source": src,
                    "evidence": clause[:260],
                }

    req.language_requirements = list(seen_langs.values())
    return req


def _extract_candidate_languages(
    *,
    profile: CandidateProfile,
    cv_version: CvVersion | None,
    facts: list[CvExtractedFact],
) -> tuple[dict[str, str], set[str]]:
    """
    Collect candidate language proficiencies ONLY from explicit CV language facts/text and explicit profile entries.
    Never infer citizenship or native language solely from country/name.
    Returns (best_level_by_lang, all_known_langs).
    """
    levels: dict[str, str] = {}

    def _upgrade(code: str, lvl: str) -> None:
        cur = levels.get(code, "none")
        if LANG_LEVEL_ORDER.get(lvl, 0) > LANG_LEVEL_ORDER.get(cur, 0):
            levels[code] = lvl

    for item in getattr(profile, "confirmed_languages_json", []) or []:
        if isinstance(item, dict):
            raw_lang = str(item.get("language", "")).strip().lower()
            raw_lvl = str(item.get("level", "medium")).strip().lower()
        else:
            raw_lang = str(item).strip().lower()
            raw_lvl = "medium"
        mapped_lvl = "low" if any(k in raw_lvl for k in ("begin", "basic", "elem", "a1", "a2")) else (
            "high" if any(k in raw_lvl for k in ("native", "fluent", "excell", "profic", "c1", "c2", "bilingual")) else "medium"
        )
        for lcode, aliases in LANGUAGE_ALIASES.items():
            if raw_lang == lcode or any(al == raw_lang or al in raw_lang for al in aliases):
                _upgrade(lcode, mapped_lvl)

    lang_lines: list[str] = []
    for f in facts:
        if f.category == "language":
            lang_lines.append(f"{f.title} {f.value} {f.proficiency_level}".lower())
    if cv_version and cv_version.extracted_text:
        for ln in cv_version.extracted_text.splitlines():
            low_ln = ln.lower()
            if re.search(r"\b(language|languages|fluent|native|bilingual|beginner|intermediate|elementary|c1|c2|b1|b2)\b|(?:اللغات)", low_ln):
                lang_lines.append(low_ln)

    for ln in lang_lines:
        # Handle multi-language lines e.g. "Languages: Tamil (Native), English (Fluent), Arabic (Beginner)"
        parts = [p.strip() for p in re.split(r"[,•|;/]+", ln) if p.strip()]
        for part in parts:
            part_lvl = "medium"
            if re.search(r"\b(beginner|basic|elementary|a1|a2)\b", part):
                part_lvl = "low"
            elif re.search(r"\b(native|fluent|proficient|excellent|mother\s+tongue|c1|c2|bilingual)\b|(?:لغة\s+أم|طلاقة)", part):
                part_lvl = "high"
            elif re.search(r"\b(intermediate|conversational|working|b1|b2)\b", part):
                part_lvl = "medium"
            for lcode, aliases in LANGUAGE_ALIASES.items():
                if any(re.search(rf"(?<!\w){re.escape(al)}(?!\w)", part) for al in aliases):
                    _upgrade(lcode, part_lvl)

    return levels, set(levels.keys())


def _extract_candidate_certifications(
    *,
    profile: CandidateProfile,
    cv_version: CvVersion | None,
    facts: list[CvExtractedFact],
) -> tuple[str, set[str]]:
    """Build candidate certification/education evidence text strictly from verified CV & profile inputs."""
    cert_chunks: list[str] = []
    for cert in getattr(profile, "confirmed_certifications_json", []) or []:
        if cert:
            cert_chunks.append(str(cert).lower())
    deg_levels: set[str] = set()

    for f in facts:
        if f.category in ("certification", "education", "skill", "tool"):
            cert_chunks.append(f"{f.title} {f.value} {f.evidence_quote}".lower())
        if f.category == "education":
            blob = f"{f.title} {f.value}".lower()
            if any(w in blob for w in ("master", "msc", "mba", "m.sc", "ماجستير")):
                deg_levels.update({"bachelor", "master"})
            if any(w in blob for w in ("phd", "doctorate", "دكتوراه")):
                deg_levels.update({"bachelor", "master", "phd"})
            if any(w in blob for w in ("bachelor", "bsc", "b.sc", "b.e", "b.tech", "بكالوريوس")):
                deg_levels.add("bachelor")

    if cv_version and cv_version.extracted_text:
        cv_low = cv_version.extracted_text.lower()
        cert_chunks.append(cv_low)
        if re.search(r"\b(?:phd|doctorate|دكتوراه)\b", cv_low):
            deg_levels.update({"bachelor", "master", "phd"})
        if re.search(r"\b(?:master'?s?|msc|mba|m\.sc|ماجستير)\b", cv_low):
            deg_levels.update({"bachelor", "master"})
        if re.search(r"\b(?:bachelor'?s?|bsc|b\.sc|b\.e|b\.tech|بكالوريوس)\b", cv_low):
            deg_levels.add("bachelor")

    return " \n ".join(cert_chunks), deg_levels


def validate_candidate_mandatory_eligibility(
    *,
    requirements: MandatoryEligibilityRequirements,
    profile: CandidateProfile,
    cv_version: CvVersion | None = None,
    facts: list[CvExtractedFact] | None = None,
    check_cv_requirements: bool = True,
    max_experience_gap_years: int = 2,
) -> MandatoryEligibilityResult:
    """
    Validate candidate eligibility against mandatory posting restrictions.
    Never infers nationality/citizenship from name, location, or language.
    Unknown candidate eligibility against a mandatory rule fails/holds safely.
    """
    failures: list[dict[str, str]] = []
    codes: list[str] = []
    pref_notes: list[str] = []

    def _fail(code: str, status: str, requirement: str, reason: str, evidence: str = "") -> None:
        if code not in codes:
            codes.append(code)
        failures.append(
            {
                "code": code,
                "status": status,
                "requirement": requirement,
                "reason": reason,
                "evidence": evidence[:260],
            }
        )

    confirmed_nat = {c.upper().strip() for c in (getattr(profile, "nationalities_json", []) or []) if c and str(c).strip()}
    permits = {str(p).upper().strip() for p in (getattr(profile, "residency_permits_json", []) or []) if p and str(p).strip()}
    auth_countries = {c.upper().strip() for c in (profile.work_authorizations_json or []) if c and str(c).strip()}
    res_country = (profile.residence_country_code or "").upper().strip()
    res_city = (profile.residence_city or "").strip().lower()

    # Log preferred traits cleanly without rejecting
    for pref_nat in requirements.preferred_nationalities:
        pref_notes.append(f"Preference only (not mandatory): {pref_nat.get('label', '')}")

    # 1. Mandatory Nationality / Citizenship Restrictions
    for nr in requirements.nationality_restrictions:
        allowed_cc = {c.upper() for c in nr.get("country_codes", [])}
        allows_pr = bool(nr.get("allows_permanent_resident"))
        pr_match = allows_pr and any(p in permits for p in ("PR", "GREEN_CARD", "US_GREEN_CARD", "PERMANENT_RESIDENT", "SETTLED_STATUS"))
        if pr_match:
            continue
        if not confirmed_nat:
            _fail(
                "NATIONALITY_RESTRICTION",
                "unknown",
                nr.get("label", "Nationality restriction"),
                f"Job imposes a mandatory nationality/citizenship restriction ({', '.join(sorted(allowed_cc))}), and candidate nationality/citizenship is unconfirmed (unknown eligibility)",
                nr.get("evidence", ""),
            )
        elif not (confirmed_nat & allowed_cc):
            _fail(
                "NATIONALITY_RESTRICTION",
                "incompatible",
                nr.get("label", "Nationality restriction"),
                f"Job restricts eligibility to citizens/nationals of {', '.join(sorted(allowed_cc))} only, which is incompatible with confirmed candidate nationality ({', '.join(sorted(confirmed_nat))})",
                nr.get("evidence", ""),
            )

    # 2. Local Residency / In-Country Restrictions
    for lr in requirements.local_residency_requirements:
        if lr.get("level") != "mandatory":
            pref_notes.append(f"Residency preference only: {lr.get('label', '')}")
            continue
        req_cc = {c.upper() for c in lr.get("country_codes", [])}
        req_city = (lr.get("city") or "").strip().lower()
        if not res_country:
            _fail(
                "RESIDENCY_REQUIREMENT",
                "unknown",
                lr.get("label", "Local residency requirement"),
                f"Job requires candidate to currently reside locally in {', '.join(sorted(req_cc))}, and candidate country of residence is unconfirmed",
                lr.get("evidence", ""),
            )
        elif res_country not in req_cc:
            _fail(
                "RESIDENCY_REQUIREMENT",
                "incompatible",
                lr.get("label", "Local residency requirement"),
                f"Job requires local/in-country candidates currently residing in {', '.join(sorted(req_cc))} (relocation not accepted), whereas candidate resides in {res_country}",
                lr.get("evidence", ""),
            )
        elif req_city and res_city and req_city not in res_city and res_city not in req_city:
            _fail(
                "RESIDENCY_REQUIREMENT",
                "incompatible",
                lr.get("label", "Local residency requirement"),
                f"Job requires candidate currently based in {lr.get('city')}, whereas candidate residence city is {profile.residence_city}",
                lr.get("evidence", ""),
            )

    # 3. Work Permits, Transferable Iqama/QID & Sponsorship Rules
    for wp in requirements.work_permit_requirements:
        pcode = wp.get("permit_code", "")
        pcountry = (wp.get("country_code") or "").upper()
        if wp.get("level") != "mandatory":
            pref_notes.append(f"Permit preference: {wp.get('label', '')}")
            continue
        if pcode == "SA_TRANSFERABLE_IQAMA":
            if "SA" in confirmed_nat or "SA_TRANSFERABLE_IQAMA" in permits or "TRANSFERABLE_IQAMA" in permits:
                continue
            status = "incompatible" if (confirmed_nat or permits or res_country) else "unknown"
            _fail(
                "WORK_AUTHORIZATION_MISMATCH",
                status,
                wp.get("label", "Transferable Saudi Iqama"),
                "Job requires a transferable Saudi Iqama (or Saudi citizenship), which is missing or unconfirmed in candidate profile",
                wp.get("evidence", ""),
            )
        elif pcode == "QA_TRANSFERABLE_QID":
            if "QA" in confirmed_nat or "QA_TRANSFERABLE_QID" in permits or "TRANSFERABLE_QID" in permits:
                continue
            status = "incompatible" if (confirmed_nat or permits or res_country) else "unknown"
            _fail(
                "WORK_AUTHORIZATION_MISMATCH",
                status,
                wp.get("label", "Transferable QID"),
                "Job requires a transferable Qatar ID (QID with NOC) or Qatari citizenship, which is missing or unconfirmed in candidate profile",
                wp.get("evidence", ""),
            )
        elif pcode == "NO_SPONSORSHIP":
            if profile.requires_sponsorship is True:
                _fail(
                    "WORK_AUTHORIZATION_MISMATCH",
                    "incompatible",
                    wp.get("label", "No visa sponsorship"),
                    "Job explicitly excludes visa sponsorship / requires existing independent work permit, and candidate requires sponsorship",
                    wp.get("evidence", ""),
                )
            elif profile.requires_sponsorship is None:
                # If unknown whether candidate needs sponsorship or holds independent right to work
                has_independent_auth = pcountry and (pcountry in auth_countries or pcountry in confirmed_nat)
                if not has_independent_auth:
                    _fail(
                        "WORK_AUTHORIZATION_MISMATCH",
                        "unknown",
                        wp.get("label", "No visa sponsorship"),
                        "Job states no visa sponsorship is available, and candidate sponsorship status / work permit independence is unconfirmed",
                        wp.get("evidence", ""),
                    )

    # 4. Immediate Joining / Mandatory Notice Period Cap
    imm = requirements.immediate_joining_requirement or {}
    if imm.get("level") == "mandatory" and imm.get("max_notice_days") is not None:
        max_days = int(imm["max_notice_days"])
        avail_low = (profile.availability_note or "").lower()
        explicitly_immediate = profile.notice_period_days == 0 or "immediate" in avail_low
        if profile.notice_period_days is None and not explicitly_immediate:
            _fail(
                "MANDATORY_AVAILABILITY_MISMATCH",
                "unknown",
                f"Notice period <= {max_days} days",
                f"Job mandates immediate joining / <= {max_days} days notice, and candidate notice period is unconfirmed",
                imm.get("evidence", ""),
            )
        elif profile.notice_period_days is not None and profile.notice_period_days > max_days and not explicitly_immediate:
            _fail(
                "MANDATORY_AVAILABILITY_MISMATCH",
                "incompatible",
                f"Notice period <= {max_days} days",
                f"Job mandates immediate joining (<= {max_days} days notice), incompatible with candidate notice period ({profile.notice_period_days} days)",
                imm.get("evidence", ""),
            )

    profile_passed = len(failures) == 0

    # --- CV-Dependent Mandatory Checks (Experience, Certifications/Licences, Education, Languages) ---
    if check_cv_requirements and cv_version is not None:
        fact_list = facts or []
        cert_blob, deg_levels = _extract_candidate_certifications(
            profile=profile,
            cv_version=cv_version,
            facts=fact_list,
        )

        # 5. Certifications & Licences
        for cert in requirements.certification_requirements:
            if cert.get("level") != "mandatory":
                pref_notes.append(f"Preferred certification/licence: {cert.get('name', '')}")
                continue
            tokens = [t.lower() for t in cert.get("tokens", []) if t]
            if not any(tok in cert_blob for tok in tokens):
                _fail(
                    "REQUIRED_CERTIFICATION_MISSING",
                    "incompatible" if cert_blob.strip() else "unknown",
                    cert.get("name", "Required certification/licence"),
                    f"Mandatory certification/licence '{cert.get('name')}' is missing from candidate CV and confirmed profile",
                    cert.get("evidence", ""),
                )

        # 6. Mandatory Degree / Education
        for edu in requirements.education_requirements:
            if edu.get("level") != "mandatory":
                continue
            req_deg = edu.get("degree", "bachelor")
            if req_deg not in deg_levels:
                _fail(
                    "REQUIRED_CERTIFICATION_MISSING",
                    "incompatible" if deg_levels else "unknown",
                    f"Mandatory {req_deg} degree",
                    f"Job requires a mandatory {req_deg} degree, which is not evidenced in candidate education records",
                    edu.get("evidence", ""),
                )

        # 7. Mandatory Experience Years (Strict minimum)
        exp_req = requirements.experience_requirement or {}
        if exp_req.get("level") == "mandatory" and exp_req.get("min_years") is not None:
            min_yrs = int(exp_req["min_years"])
            cv_yrs = round((cv_version.total_experience_months or 0) / 12.0, 2)
            if cv_version.total_experience_months <= 0:
                _fail(
                    "MANDATORY_EXPERIENCE_GAP",
                    "unknown",
                    f"Minimum {min_yrs} years experience",
                    f"Job requires a mandatory minimum of {min_yrs} years experience, and CV experience duration could not be verified",
                    exp_req.get("evidence", ""),
                )
            elif cv_yrs < min_yrs:
                _fail(
                    "MANDATORY_EXPERIENCE_GAP",
                    "incompatible",
                    f"Minimum {min_yrs} years experience",
                    f"Job mandates a minimum of {min_yrs} years experience, exceeding candidate evidenced experience ({cv_yrs} yrs)",
                    exp_req.get("evidence", ""),
                )

        # 8. Mandatory Language Proficiency
        cand_lang_levels, _ = _extract_candidate_languages(
            profile=profile,
            cv_version=cv_version,
            facts=fact_list,
        )
        for lreq in requirements.language_requirements:
            if lreq.get("level") != "mandatory":
                pref_notes.append(f"Preferred language: {lreq.get('lang_code', '')}")
                continue
            lcode = lreq.get("lang_code", "")
            req_lvl = lreq.get("min_level", "medium")
            cand_lvl = cand_lang_levels.get(lcode, "none")
            if cand_lvl == "none":
                _fail(
                    "MANDATORY_LANGUAGE_MISMATCH",
                    "unknown",
                    f"Mandatory language: {lcode}",
                    f"Job requires mandatory proficiency in language '{lcode}' ({req_lvl}), which is not evidenced in candidate CV or confirmed languages",
                    lreq.get("evidence", ""),
                )
            elif LANG_LEVEL_ORDER.get(cand_lvl, 0) < LANG_LEVEL_ORDER.get(req_lvl, 0):
                _fail(
                    "MANDATORY_LANGUAGE_MISMATCH",
                    "incompatible",
                    f"Mandatory language: {lcode} ({req_lvl})",
                    f"Job requires {req_lvl} proficiency in '{lcode}', whereas candidate evidence shows '{cand_lvl}' proficiency",
                    lreq.get("evidence", ""),
                )

    status = "eligible"
    if failures:
        has_incompatible = any(f["status"] == "incompatible" for f in failures)
        status = "incompatible" if has_incompatible else "unknown_eligibility"

    return MandatoryEligibilityResult(
        passed=len(failures) == 0,
        profile_passed=profile_passed,
        status=status,
        failure_codes=codes,
        failures=failures,
        preference_notes=pref_notes,
    )


def assess_posting_mandatory_eligibility(
    *,
    posting: JobPosting,
    profile: CandidateProfile,
    cv_version: CvVersion | None = None,
    facts: list[CvExtractedFact] | None = None,
    check_cv_requirements: bool = True,
) -> tuple[MandatoryEligibilityRequirements, MandatoryEligibilityResult]:
    """Convenience wrapper extracting requirements across all posting fields and validating against candidate."""
    structured = {}
    if isinstance(posting.raw_metadata_json, dict):
        structured.update(posting.raw_metadata_json)
    if isinstance(posting.extraction_evidence_json, dict):
        structured.update(posting.extraction_evidence_json)

    reqs = extract_mandatory_eligibility_requirements(
        title=posting.title,
        jd_text=posting.jd_text,
        subject_instruction=posting.subject_instruction,
        evidence_quote=posting.evidence_quote,
        location_raw=posting.location_raw,
        country_code=posting.country_code,
        structured_data=structured,
    )
    if cv_version and facts and any(f.confidence in ("retired", "superseded") for f in facts):
        cv_version, facts = effective_cv_evidence(cv_version, facts)
    res = validate_candidate_mandatory_eligibility(
        requirements=reqs,
        profile=profile,
        cv_version=cv_version,
        facts=facts,
        check_cv_requirements=check_cv_requirements,
    )
    return reqs, res
