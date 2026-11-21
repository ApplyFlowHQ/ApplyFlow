"""Robust CV parsing (PDF/DOCX), zip-bomb/page bounds, optional OCR, provenance facts, and overlap-safe experience calculation."""

from __future__ import annotations

import io
import re
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

import docx
from pypdf import PdfReader
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import CvExtractedFact, CvVersion
from app.services.i18n import detect_text_language

# Known skills / tools dictionary for deterministic provenance extraction (supplemented by line parsing)
COMMON_TOOLS_AND_SKILLS = [
    "Python", "SQL", "PostgreSQL", "MySQL", "TypeScript", "JavaScript", "React", "FastAPI",
    "Django", "Docker", "Kubernetes", "AWS", "GCP", "Azure", "Excel", "Power BI", "Tableau",
    "Looker", "Pandas", "NumPy", "Spark", "Airflow", "dbt", "Terraform", "Git", "Linux",
    "Java", "C#", "Go", "Rust", "SAP", "Salesforce", "Jira", "Figma", "IFRS", "GAAP",
    "Financial Modeling", "Budgeting", "Forecasting", "Auditing", "Supply Chain",
    "Procurement", "Logistics", "Project Management", "Agile", "Scrum", "HRIS", "Payroll",
    "Recruitment", "NLP", "Machine Learning", "PyTorch", "TensorFlow", "Redis", "Celery",
    "CAFM", "CMMS", "IBM Maximo", "Maximo", "MAS 8", "PPM", "Power Automate", "Data Analytics",
    "MIS", "Facilities Management", "Asset Management", "Document Control", "KPI", "SLA",
]

MONTH_MAP = {
    "jan": 1, "january": 1, "يناير": 1,
    "feb": 2, "february": 2, "فبراير": 2,
    "mar": 3, "march": 3, "مارس": 3,
    "apr": 4, "april": 4, "أبريل": 4, "ابريل": 4,
    "may": 5, "مايو": 5,
    "jun": 6, "june": 6, "يونيو": 6,
    "jul": 7, "july": 7, "يوليو": 7,
    "aug": 8, "august": 8, "أغسطس": 8, "اغسطس": 8,
    "sep": 9, "sept": 9, "september": 9, "سبتمبر": 9,
    "oct": 10, "october": 10, "أكتوبر": 10, "اكتوبر": 10,
    "nov": 11, "november": 11, "نوفمبر": 11,
    "dec": 12, "december": 12, "ديسمبر": 12,
}

# Sensitive personal traits that must never be extracted into CV ranking or drafted copy
SENSITIVE_REDACT_PATTERNS = [
    re.compile(r"(?i)\b(passport\s*(?:no|number)?|national\s*id|ssn|social\s*security|date\s*of\s*birth|dob|marital\s*status|religion|nationality)\s*[:\-].*"),
]


@dataclass
class ExtractedFactItem:
    category: str  # skill|experience|education|certification|tool|project|language|achievement
    title: str
    organization: str
    value: str
    evidence_quote: str
    start_date: str | None = None
    end_date: str | None = None
    is_current: bool = False
    proficiency_level: str = "unknown"
    confidence: str = "evidenced"
    source_page: int = 1


@dataclass
class ParsedCvResult:
    parse_status: str  # parsed | ocr_required | failed | unsupported_language
    ocr_status: str  # not_needed | completed | required_unavailable | failed
    parse_error: str
    page_count: int
    detected_language: str
    extracted_text: str
    total_experience_months: int
    facts: list[ExtractedFactItem] = field(default_factory=list)
    detected_names: list[str] = field(default_factory=list)
    detected_emails: list[str] = field(default_factory=list)
    summary: dict[str, object] = field(default_factory=dict)


def _check_docx_decompression_bounds(data: bytes, max_uncompressed_bytes: int) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(data), "r") as zf:
            total_size = 0
            entries = 0
            for info in zf.infolist():
                entries += 1
                if entries > 2000:
                    raise ValueError("DOCX archive contains too many entries")
                if info.file_size < 0 or info.file_size > max_uncompressed_bytes:
                    raise ValueError("DOCX entry exceeds decompression limit")
                total_size += info.file_size
                if total_size > max_uncompressed_bytes:
                    raise ValueError("DOCX decompressed payload exceeds security limit")
    except zipfile.BadZipFile as exc:
        raise ValueError("Corrupted or invalid DOCX archive") from exc


def _extract_pdf_text(data: bytes, max_pages: int) -> tuple[str, int]:
    reader = PdfReader(io.BytesIO(data))
    page_count = len(reader.pages)
    if page_count == 0:
        raise ValueError("PDF has zero pages")
    if page_count > max_pages:
        raise ValueError(f"PDF exceeds maximum allowed page count ({page_count} > {max_pages})")
    pages_text: list[str] = []
    for idx, page in enumerate(reader.pages, start=1):
        txt = page.extract_text() or ""
        if txt.strip():
            pages_text.append(f"[Page {idx}]\n{txt.strip()}")
    return "\n\n".join(pages_text), page_count


def _extract_docx_text(data: bytes, max_uncompressed_bytes: int, max_pages: int) -> tuple[str, int]:
    _check_docx_decompression_bounds(data, max_uncompressed_bytes)
    document = docx.Document(io.BytesIO(data))
    lines: list[str] = []
    for p in document.paragraphs:
        if p.text.strip():
            lines.append(p.text.strip())
    for table in document.tables:
        for row in table.rows:
            row_cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if row_cells:
                lines.append(" | ".join(row_cells))
    full_text = "\n".join(lines)
    est_pages = max(1, (len(full_text) // 2800) + 1)
    if est_pages > max_pages:
        raise ValueError(f"DOCX exceeds maximum length ({est_pages} estimated pages > {max_pages})")
    return full_text, est_pages


def _try_tesseract_ocr_pdf(data: bytes) -> tuple[str, str]:
    settings = get_settings()
    if settings.ocr_provider != "tesseract":
        return "", "required_unavailable"
    exe = shutil.which(settings.tesseract_cmd)
    if not exe:
        return "", "required_unavailable"
    # Attempt PDF -> image OCR if pdftoppm is installed
    pdftoppm = shutil.which("pdftoppm")
    if not pdftoppm:
        return "", "required_unavailable"
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            pdf_path = Path(tmpdir) / "cv.pdf"
            pdf_path.write_bytes(data)
            subprocess.run(
                [pdftoppm, "-png", str(pdf_path), str(Path(tmpdir) / "page")],
                check=True,
                timeout=30,
                capture_output=True,
            )
            texts: list[str] = []
            for img in sorted(Path(tmpdir).glob("page-*.png")):
                res = subprocess.run(
                    [exe, str(img), "stdout"],
                    check=True,
                    timeout=30,
                    capture_output=True,
                    text=True,
                )
                if res.stdout.strip():
                    texts.append(res.stdout.strip())
            combined = "\n\n".join(texts).strip()
            return (combined, "completed") if combined else ("", "failed")
    except Exception:
        return "", "failed"


def _parse_year_month(token: str) -> date | None:
    tok = token.strip().lower()
    if tok in ("present", "current", "now", "حتى الآن", "الحالي"):
        return date.today().replace(day=1)
    m_ym = re.match(r"^(\d{4})[-/](\d{1,2})$", tok)
    if m_ym:
        y, m = int(m_ym.group(1)), int(m_ym.group(2))
        if 1960 <= y <= 2100 and 1 <= m <= 12:
            return date(y, m, 1)
    m_my = re.match(r"^(\d{1,2})[-/](\d{4})$", tok)
    if m_my:
        m, y = int(m_my.group(1)), int(m_my.group(2))
        if 1960 <= y <= 2100 and 1 <= m <= 12:
            return date(y, m, 1)
    m_mon_y = re.match(r"^([A-Za-z\u0600-\u06FF]+)\s+(\d{4})$", tok)
    if m_mon_y:
        mon_name, y_str = m_mon_y.group(1).lower(), m_mon_y.group(2)
        if mon_name in MONTH_MAP:
            return date(int(y_str), MONTH_MAP[mon_name], 1)
    m_y = re.match(r"^(\d{4})$", tok)
    if m_y:
        y = int(m_y.group(1))
        if 1960 <= y <= 2100:
            return date(y, 1, 1)
    return None


def compute_non_overlapping_experience_months(intervals: list[tuple[date, date]]) -> int:
    """Merge overlapping date ranges so concurrent roles are NEVER double-counted."""
    if not intervals:
        return 0
    normalized = sorted(
        [(min(s, e), max(s, e)) for s, e in intervals],
        key=lambda pair: (pair[0], pair[1]),
    )
    merged: list[tuple[date, date]] = [normalized[0]]
    for start, end in normalized[1:]:
        prev_start, prev_end = merged[-1]
        if start <= prev_end:
            merged[-1] = (prev_start, max(prev_end, end))
        else:
            merged.append((start, end))

    total_months = 0
    for start, end in merged:
        months = (end.year - start.year) * 12 + (end.month - start.month) + 1
        total_months += max(1, months)
    return total_months


def _sanitize_cv_text_for_facts(text: str) -> str:
    cleaned = text
    for pattern in SENSITIVE_REDACT_PATTERNS:
        cleaned = pattern.sub("[REDACTED_SENSITIVE_FIELD]", cleaned)
    return cleaned


def extract_facts_from_text(text: str) -> tuple[list[ExtractedFactItem], int, list[str], list[str]]:
    """Extract structured facts with evidence quotes and merge overlapping job dates."""
    sanitized = _sanitize_cv_text_for_facts(text)
    lines = [line.strip() for line in sanitized.splitlines() if line.strip()]
    facts: list[ExtractedFactItem] = []
    intervals: list[tuple[date, date]] = []

    emails = sorted(set(re.findall(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", sanitized)))
    names: list[str] = []
    if lines:
        first_line = re.sub(r"^\[Page \d+\]$", "", lines[0]).strip()
        if not first_line and len(lines) > 1:
            first_line = lines[1]
        if first_line and "@" not in first_line and len(first_line.split()) <= 5 and len(first_line) <= 60:
            names.append(first_line)

    # Skills & Tools provenance
    seen_skills: set[str] = set()
    for tool in COMMON_TOOLS_AND_SKILLS:
        pattern = re.compile(rf"(?<!\w){re.escape(tool)}(?!\w)", re.IGNORECASE)
        for line in lines:
            if pattern.search(line) and tool.lower() not in seen_skills:
                seen_skills.add(tool.lower())
                facts.append(
                    ExtractedFactItem(
                        category="tool",
                        title=tool,
                        organization="",
                        value=tool,
                        evidence_quote=line[:240],
                        confidence="evidenced",
                    )
                )
                break

    # Explicit "Skills:" lines
    for line in lines:
        if re.match(r"^(?:skills|technical skills|core competencies|المهارات)\s*[:\-]", line, re.IGNORECASE):
            after = line.split(":", 1)[-1] if ":" in line else line.split("-", 1)[-1]
            for item in re.split(r"[,•|;/]+", after):
                sk = item.strip()
                if 2 <= len(sk) <= 50 and sk.lower() not in seen_skills:
                    seen_skills.add(sk.lower())
                    facts.append(
                        ExtractedFactItem(
                            category="skill",
                            title=sk,
                            organization="",
                            value=sk,
                            evidence_quote=line[:240],
                            confidence="evidenced",
                        )
                    )

    # Experience intervals & roles
    date_range_re = re.compile(
        r"(?P<start>(?:\d{4}[-/]\d{1,2}|\d{1,2}[-/]\d{4}|[A-Za-z\u0600-\u06FF]+\s+\d{4}|\d{4}))"
        r"\s*(?:-|–|—|to|إلى)\s*"
        r"(?P<end>(?:\d{4}[-/]\d{1,2}|\d{1,2}[-/]\d{4}|[A-Za-z\u0600-\u06FF]+\s+\d{4}|\d{4}|present|current|now|حتى الآن|الحالي))",
        re.IGNORECASE,
    )
    for idx, line in enumerate(lines):
        m = date_range_re.search(line)
        if not m:
            continue
        start_dt = _parse_year_month(m.group("start"))
        end_raw = m.group("end")
        end_dt = _parse_year_month(end_raw)
        if not start_dt or not end_dt:
            continue
        is_edu = bool(re.search(r"\b(university|college|bachelor|master|bsc|msc|phd|diploma|degree|جامعة|بكالوريوس|ماجستير)\b", line, re.IGNORECASE))
        if is_edu:
            facts.append(
                ExtractedFactItem(
                    category="education",
                    title=line[:140],
                    organization="",
                    value=line[:240],
                    evidence_quote=line[:240],
                    start_date=start_dt.isoformat()[:7],
                    end_date=end_dt.isoformat()[:7],
                    confidence="evidenced",
                )
            )
            continue

        intervals.append((start_dt, end_dt))
        context_quote = line
        if idx + 1 < len(lines) and len(lines[idx + 1]) < 160:
            context_quote = f"{line} | {lines[idx + 1]}"
        title_part = date_range_re.sub("", line).strip(" |-–—,")
        facts.append(
            ExtractedFactItem(
                category="experience",
                title=title_part[:140] or "Role",
                organization="",
                value=title_part[:200] or line[:200],
                evidence_quote=context_quote[:300],
                start_date=start_dt.isoformat()[:7],
                end_date=end_dt.isoformat()[:7],
                is_current=end_raw.strip().lower() in ("present", "current", "now", "حتى الآن", "الحالي"),
                confidence="evidenced",
            )
        )

    # Certifications, licences, projects, languages, achievements
    seen_certs_extracted: set[str] = set()
    for line in lines:
        low = line.lower()
        dl_m = re.search(r"\b(?:valid\s+)?(?:uae|saudi|ksa|gcc|emirates)?\s*driving\s+licen[cs]e(?:\s*[:\-]\s*yes)?\b", line, re.IGNORECASE)
        if dl_m:
            dl_val = dl_m.group(0).strip()
            if dl_val.lower() not in seen_certs_extracted:
                seen_certs_extracted.add(dl_val.lower())
                facts.append(
                    ExtractedFactItem(
                        category="certification",
                        title=dl_val[:140],
                        organization="",
                        value=dl_val[:240],
                        evidence_quote=line[:240],
                        confidence="evidenced",
                    )
                )
        if any(kw in low for kw in ("certified", "certification", "certificate", "aws certified", "pmp", "cfa", "iso 41001", "شهادة")):
            facts.append(
                ExtractedFactItem(
                    category="certification",
                    title=line[:140],
                    organization="",
                    value=line[:240],
                    evidence_quote=line[:240],
                    confidence="evidenced",
                )
            )
        elif re.match(r"^(?:project|projects|المشاريع)\b", low):
            facts.append(
                ExtractedFactItem(
                    category="project",
                    title=line[:140],
                    organization="",
                    value=line[:240],
                    evidence_quote=line[:240],
                    confidence="evidenced",
                )
            )
        elif any(kw in low for kw in ("arabic", "english", "french", "german", "spanish", "العربية", "الإنجليزية", "الفرنسية")) and any(
            lvl in low for lvl in ("native", "fluent", "proficient", "intermediate", "b2", "c1", "c2", "لغة أم", "طلاقة")
        ):
            facts.append(
                ExtractedFactItem(
                    category="language",
                    title=line[:100],
                    organization="",
                    value=line[:180],
                    evidence_quote=line[:240],
                    confidence="evidenced",
                )
            )
        elif re.search(r"\b(reduced|increased|improved|delivered|automated|saved|launched|led|built|حقق|طور|أنجز)\b", low):
            facts.append(
                ExtractedFactItem(
                    category="achievement",
                    title=line[:120],
                    organization="",
                    value=line[:240],
                    evidence_quote=line[:240],
                    confidence="evidenced",
                )
            )

    total_months = compute_non_overlapping_experience_months(intervals)
    return facts, total_months, names, emails


def parse_cv_bytes(data: bytes, mime_type: str) -> ParsedCvResult:
    """Parse PDF or DOCX CV safely; never report empty scanned extraction as valid parsed CV."""
    settings = get_settings()
    try:
        if mime_type == "application/pdf":
            text, page_count = _extract_pdf_text(data, settings.cv_max_pages)
            ocr_status = "not_needed"
            if len(re.sub(r"\[Page \d+\]|\s+", "", text)) < 40:
                ocr_text, ocr_status = _try_tesseract_ocr_pdf(data)
                if ocr_text:
                    text = ocr_text
                else:
                    return ParsedCvResult(
                        parse_status="ocr_required",
                        ocr_status=ocr_status,
                        parse_error="PDF appears scanned or image-only; configure OCR or upload a selectable-text PDF/DOCX.",
                        page_count=page_count,
                        detected_language="unknown",
                        extracted_text="",
                        total_experience_months=0,
                    )
        elif mime_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
            text, page_count = _extract_docx_text(data, settings.cv_max_decompressed_bytes, settings.cv_max_pages)
            ocr_status = "not_needed"
            if len(re.sub(r"\s+", "", text)) < 30:
                return ParsedCvResult(
                    parse_status="failed",
                    ocr_status="not_needed",
                    parse_error="DOCX document contains insufficient readable text.",
                    page_count=page_count,
                    detected_language="unknown",
                    extracted_text="",
                    total_experience_months=0,
                )
        else:
            return ParsedCvResult(
                parse_status="failed",
                ocr_status="not_needed",
                parse_error=f"Unsupported MIME type: {mime_type}",
                page_count=0,
                detected_language="unknown",
                extracted_text="",
                total_experience_months=0,
            )
    except Exception as exc:
        return ParsedCvResult(
            parse_status="failed",
            ocr_status="not_needed",
            parse_error=str(exc),
            page_count=0,
            detected_language="unknown",
            extracted_text="",
            total_experience_months=0,
        )

    lang, lang_conf = detect_text_language(text)
    facts, total_months, names, emails = extract_facts_from_text(text)
    if mime_type == "application/pdf":
        pages = re.split(r"^\[Page (\d+)\]\n", text, flags=re.MULTILINE)
        for fact in facts:
            line = fact.evidence_quote.split(" | ", 1)[0]
            fact.source_page = next((int(page) for page, content in zip(pages[1::2], pages[2::2], strict=True) if line in content), 1)

    return ParsedCvResult(
        parse_status="parsed",
        ocr_status=ocr_status,
        parse_error="",
        page_count=page_count,
        detected_language=lang,
        extracted_text=text,
        total_experience_months=total_months,
        facts=facts,
        detected_names=names,
        detected_emails=emails,
        summary={
            "language": lang,
            "language_confidence": lang_conf,
            "facts_count": len(facts),
            "total_experience_months": total_months,
            "detected_names": names,
            "detected_emails": emails,
        },
    )


def apply_parsed_cv(db: Session, version: CvVersion, parsed: ParsedCvResult) -> None:
    """Refresh parser-owned values while retaining cited IDs, corrections and import metadata."""
    for attr in ("parse_status", "ocr_status", "parse_error", "page_count", "detected_language",
                 "extracted_text", "total_experience_months"):
        setattr(version, attr, getattr(parsed, attr))
    version.raw_summary_json = {**(version.raw_summary_json or {}), **parsed.summary}

    rows = db.scalars(select(CvExtractedFact).where(
        CvExtractedFact.cv_version_id == version.id, CvExtractedFact.user_id == version.user_id
    )).all()
    corrections = version.raw_summary_json.get("corrections", [])
    user_fact_ids = {change["confirmed_fact_id"] for change in corrections}
    existing = {}
    for row in rows:
        if row.id not in user_fact_ids:
            existing.setdefault((row.category, row.title, row.value, row.evidence_quote), []).append(row)
    for item in parsed.facts:
        matches = existing.get((item.category, item.title, item.value, item.evidence_quote))
        if matches:
            fact = matches.pop()
            fact.source_page = item.source_page
            if fact.confidence == "retired":
                fact.confidence = item.confidence
        else:
            db.add(CvExtractedFact(cv_version_id=version.id, user_id=version.user_id, **asdict(item)))
    for matches in existing.values():
        for fact in matches:
            if fact.confidence != "superseded":
                fact.confidence = "retired"


def current_cv_facts(facts: list[CvExtractedFact]) -> list[CvExtractedFact]:
    """Extractions quoting a disputed line cannot independently corroborate it."""
    disputed = {line for f in facts if f.confidence == "superseded" for line in f.evidence_quote.split(" | ")}
    return [f for f in facts if f.confidence == "confirmed" or (
        f.confidence == "evidenced" and f.evidence_quote and disputed.isdisjoint(f.evidence_quote.split(" | "))
    )]


def effective_cv_evidence(version: CvVersion, facts: list[CvExtractedFact]) -> tuple[CvVersion, list[CvExtractedFact]]:
    """Exclude disputed original claims from every mandatory eligibility check."""
    active = current_cv_facts(facts)
    if not any(f.confidence == "superseded" for f in facts):
        return version, active
    evidenced = [f for f in active if f.confidence == "evidenced"]
    text = "\n".join(dict.fromkeys(f.evidence_quote for f in evidenced))
    _, months, _, _ = extract_facts_from_text(text)
    effective = CvVersion(extracted_text=text, total_experience_months=months, detected_language=version.detected_language)
    return effective, evidenced
