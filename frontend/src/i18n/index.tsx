import React, { createContext, useContext, useEffect, useState } from 'react';
import { en, type TranslationDict } from './en';
import { ar } from './ar';

export type Locale = 'en' | 'ar';

interface I18nContextValue {
  locale: Locale;
  dir: 'ltr' | 'rtl';
  t: TranslationDict;
  setLocale: (loc: Locale) => void;
  toggleLocale: () => void;
  formatDateTime: (iso?: string | null, timeZone?: string) => string;
}

const I18nContext = createContext<I18nContextValue | null>(null);

export const I18nProvider: React.FC<{ initialLocale?: Locale; children: React.ReactNode }> = ({
  initialLocale = 'en',
  children,
}) => {
  const [locale, setLocaleState] = useState<Locale>(() => {
    try {
      const saved = localStorage.getItem('applyflow_locale');
      return saved === 'ar' || saved === 'en' ? saved : initialLocale;
    } catch {
      return initialLocale;
    }
  });

  const dir: 'ltr' | 'rtl' = locale === 'ar' ? 'rtl' : 'ltr';
  const t = locale === 'ar' ? ar : en;

  useEffect(() => {
    document.documentElement.lang = locale;
    document.documentElement.dir = dir;
    try {
      localStorage.setItem('applyflow_locale', locale);
    } catch {
      // Keep the active language in React state when browser storage is unavailable.
    }
  }, [locale, dir]);

  const setLocale = (loc: Locale) => setLocaleState(loc);
  const toggleLocale = () => setLocaleState((prev) => (prev === 'en' ? 'ar' : 'en'));

  const formatDateTime = (iso?: string | null, timeZone?: string) => {
    if (!iso) return '—';
    try {
      const dt = new Date(iso);
      return new Intl.DateTimeFormat(locale === 'ar' ? 'ar-AE' : 'en-GB', {
        dateStyle: 'medium',
        timeStyle: 'short',
        timeZone: timeZone || undefined,
      }).format(dt);
    } catch {
      return iso;
    }
  };

  return (
    <I18nContext.Provider value={{ locale, dir, t, setLocale, toggleLocale, formatDateTime }}>
      {children}
    </I18nContext.Provider>
  );
};

export function useI18n(): I18nContextValue {
  const ctx = useContext(I18nContext);
  if (!ctx) throw new Error('useI18n must be used within I18nProvider');
  return ctx;
}
