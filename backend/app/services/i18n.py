"""Multilingual detection, localized search phrases, and application language selection."""

from __future__ import annotations

import re

from langdetect import DetectorFactory, detect

DetectorFactory.seed = 42

SUPPORTED_APP_LANGUAGES = {"en", "ar", "fr", "de", "es"}

# Localized phrases explicitly inviting email CV applications (used as search signals ONLY)
LOCALIZED_SEND_CV_PHRASES: dict[str, list[str]] = {
    "en": [
        '"send your CV"',
        '"send your resume"',
        '"email your CV"',
        '"share your CV"',
        '"apply by email"',
        '"send CV to"',
        '"recruitment@"',
        '"careers@"',
        '"hr@"',
        '"we are hiring"',
    ],
    "ar": [
        '"أرسل السيرة الذاتية"',
        '"إرسال السيرة الذاتية"',
        '"ارسل سيرتك الذاتية"',
        '"التقديم عبر البريد الإلكتروني"',
        '"للتقديم عبر الإيميل"',
        '"نحن نوظف"',
    ],
    "fr": ['"envoyez votre CV"', '"postuler par email"', '"adresser votre candidature"'],
    "de": ['"Bewerbung per E-Mail"', '"Lebenslauf senden an"', '"Bewerbungsunterlagen per E-Mail"'],
    "es": ['"enviar CV a"', '"postular por correo"', '"envía tu currículum"'],
}


def detect_text_language(text: str) -> tuple[str, float]:
    """Detect language code and rough confidence using script heuristics + langdetect."""
    sample = (text or "").strip()
    if len(sample) < 20:
        return "unknown", 0.0

    # Fast script check for Arabic
    arabic_chars = len(re.findall(r"[\u0600-\u06FF]", sample))
    latin_chars = len(re.findall(r"[A-Za-z]", sample))
    if arabic_chars > 20 and arabic_chars > latin_chars:
        return "ar", 0.92

    try:
        lang = detect(sample[:4000])
        return lang.lower(), 0.80
    except Exception:
        return "unknown", 0.0


def choose_application_language(
    *,
    job_language: str,
    mode: str,
    explicit_language: str,
) -> tuple[str, bool, str]:
    """
    Determine target language for application email.

    Returns (language_code, supported, warning_or_reason).
    """
    if mode == "explicit":
        lang = (explicit_language or "en").lower()
        if lang in SUPPORTED_APP_LANGUAGES:
            return lang, True, ""
        return lang, False, f"Selected application language '{lang}' is not supported"

    # employer_language_if_supported
    jlang = (job_language or "unknown").lower()
    if jlang in SUPPORTED_APP_LANGUAGES:
        return jlang, True, ""
    if jlang == "unknown":
        fallback = (explicit_language or "en").lower()
        return fallback, True, "Job language could not be detected; using configured fallback language"
    return jlang, False, f"Employer language '{jlang}' is unsupported for automated drafting"


def localized_search_phrases_for_languages(languages: list[str]) -> list[tuple[str, str]]:
    """Return (language, phrase) pairs across requested search languages."""
    pairs: list[tuple[str, str]] = []
    for lang in languages or ["en"]:
        phrases = LOCALIZED_SEND_CV_PHRASES.get(lang.lower(), LOCALIZED_SEND_CV_PHRASES["en"])
        for phrase in phrases:
            pairs.append((lang.lower(), phrase))
    return pairs
