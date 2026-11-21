import React, { useEffect, useState } from 'react';
import { Plus, X } from 'lucide-react';
import { useI18n } from '../i18n';
import { apiRequest, ApiError } from '../lib/api';

const REMOTE_MODES = ['onsite', 'hybrid', 'remote_country', 'remote_region', 'remote_worldwide'] as const;

export const PreferencesPage: React.FC<{ onUpdated?: () => void }> = ({ onUpdated }) => {
  const { t } = useI18n();
  const [pref, setPref] = useState<any>(null);
  const [explanations, setExplanations] = useState<Record<string, string>>({});
  const [hardCap, setHardCap] = useState(50);
  const [geoQuery, setGeoQuery] = useState('');
  const [geoSuggestions, setGeoSuggestions] = useState<any[]>([]);
  const [titlesText, setTitlesText] = useState('');
  const [familiesText, setFamiliesText] = useState('');
  const [excKeywordsText, setExcKeywordsText] = useState('');
  const [excCompaniesText, setExcCompaniesText] = useState('');
  const [excludedRecipients, setExcludedRecipients] = useState('');
  const [searchLangsText, setSearchLangsText] = useState('en');
  const [msg, setMsg] = useState('');
  const [err, setErr] = useState('');
  const [saving, setSaving] = useState(false);

  const loadPrefs = async () => {
    const res = await apiRequest('/preferences');
    const p = res.preferences;
    setPref(p);
    setExplanations(res.default_explanations || {});
    setHardCap(res.operator_daily_hard_cap || 50);
    setTitlesText((p.target_titles || []).join(', '));
    setFamiliesText((p.role_families || []).join(', '));
    setExcKeywordsText((p.excluded_keywords || []).join(', '));
    setExcCompaniesText((p.excluded_companies || []).join(', '));
    setExcludedRecipients((p.excluded_recipients || []).join(', '));
    setSearchLangsText((p.search_languages || ['en']).join(', '));
  };

  useEffect(() => {
    loadPrefs().catch((e) => setErr(String(e)));
  }, []);

  useEffect(() => {
    if (!geoQuery.trim()) {
      setGeoSuggestions([]);
      return;
    }
    const timer = setTimeout(() => {
      apiRequest<{ locations: any[] }>(`/geo/search?q=${encodeURIComponent(geoQuery)}&limit=8`)
        .then((r) => setGeoSuggestions(r.locations || []))
        .catch(() => {});
    }, 180);
    return () => clearTimeout(timer);
  }, [geoQuery]);

  const addTargetLocation = (loc: any) => {
    const current = pref.target_locations || [];
    if (!current.some((x: any) => x.geo_id === loc.geo_id)) {
      setPref({ ...pref, target_locations: [...current, loc] });
    }
    setGeoQuery('');
    setGeoSuggestions([]);
  };

  const toggleRemoteMode = (mode: string) => {
    const current: string[] = pref.allowed_remote_modes || [];
    const next = current.includes(mode) ? current.filter((m) => m !== mode) : [...current, mode];
    setPref({ ...pref, allowed_remote_modes: next });
  };

  const handleSave = async (e: React.FormEvent) => {
    e.preventDefault();
    setSaving(true);
    setErr('');
    setMsg('');
    try {
      await apiRequest('/preferences', {
        method: 'PUT',
        body: JSON.stringify({
          ...pref,
          target_titles: titlesText.split(',').map((s) => s.trim()).filter(Boolean),
          role_families: familiesText.split(',').map((s) => s.trim()).filter(Boolean),
          excluded_keywords: excKeywordsText.split(',').map((s) => s.trim()).filter(Boolean),
          excluded_companies: excCompaniesText.split(',').map((s) => s.trim()).filter(Boolean),
          excluded_recipients: excludedRecipients.split(',').map(email => email.trim()).filter(Boolean),
          search_languages: searchLangsText.split(',').map((s) => s.trim()).filter(Boolean),
          min_salary_amount: pref.min_salary_amount === '' || pref.min_salary_amount === null ? null : Number(pref.min_salary_amount),
          max_weekly_hours: pref.max_weekly_hours === '' || pref.max_weekly_hours === null ? null : Number(pref.max_weekly_hours),
        }),
      });
      setMsg(t.preferences.savedMsg);
      await loadPrefs();
      onUpdated?.();
    } catch (e2) {
      setErr(e2 instanceof ApiError ? e2.message : 'Failed to save preferences');
    } finally {
      setSaving(false);
    }
  };

  if (!pref) {
    return err ? <p role="alert">{err}</p> : <p role="status">{t.controls.loading}</p>;
  }

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold text-slate-900">{t.preferences.title}</h1>
      <p className="text-sm text-slate-600">{t.daily.intro}</p>
      {msg && <div role="status" className="p-3 rounded-lg bg-emerald-50 border border-emerald-200 text-emerald-800 text-sm">{msg}</div>}
      {err && <div role="alert" className="p-3 rounded-lg bg-rose-50 border border-rose-200 text-rose-800 text-sm">{err}</div>}

      <form onSubmit={handleSave} className="space-y-6">
        <section className="bg-white rounded-xl border border-slate-200 p-5 shadow-xs grid grid-cols-1 md:grid-cols-2 gap-4">
          <div>
            <label className="block text-xs font-semibold text-slate-700 mb-1">{t.preferences.targetTitles}</label>
            <input
              type="text"
              value={titlesText}
              onChange={(e) => setTitlesText(e.target.value)}
              placeholder="Data Analyst, BI Specialist, Operations Lead"
              className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
            />
          </div>
          <div>
            <label className="block text-xs font-semibold text-slate-700 mb-1">{t.preferences.roleFamilies}</label>
            <input
              type="text"
              value={familiesText}
              onChange={(e) => setFamiliesText(e.target.value)}
              placeholder="Analytics, Finance, Engineering"
              className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
            />
          </div>
          <div>
            <label className="block text-xs font-semibold text-slate-700 mb-1">{t.preferences.excludedKeywords}</label>
            <input
              type="text"
              value={excKeywordsText}
              onChange={(e) => setExcKeywordsText(e.target.value)}
              className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
            />
          </div>
          <div>
            <label className="block text-xs font-semibold text-slate-700 mb-1">{t.preferences.excludedCompanies}</label>
            <input
              type="text"
              value={excCompaniesText}
              onChange={(e) => setExcCompaniesText(e.target.value)}
              className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
            />
          </div>

          <label className="block text-xs font-semibold text-slate-700">
            {t.daily.excludedEmails}
            <input type="email" multiple dir="ltr" value={excludedRecipients}
              onChange={event => setExcludedRecipients(event.target.value)}
              placeholder="jobs@example.com, hr@example.org"
              className="mt-1 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm font-normal" />
          </label>

          <label className="block text-xs font-semibold text-slate-700">
            {t.daily.freshness}
            <input required type="number" min={1} max={90} value={pref.freshness_days ?? 14}
              onChange={event => setPref({ ...pref, freshness_days: Number(event.target.value) })}
              className="mt-1 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm" />
          </label>

          {/* Disambiguated Target Locations */}
          <div className="md:col-span-2 space-y-2">
            <label className="block text-xs font-semibold text-slate-700">{t.preferences.locationSearch}</label>
            <div className="relative">
              <input
                type="text"
                value={geoQuery}
                onChange={(e) => setGeoQuery(e.target.value)}
                placeholder="Type city or country (e.g. Cairo, Dubai, London, Berlin)..."
                className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
              />
              {geoSuggestions.length > 0 && (
                <ul className="absolute z-10 mt-1 w-full bg-white border border-slate-200 rounded-lg shadow-lg max-h-56 overflow-y-auto">
                  {geoSuggestions.map((loc) => (
                    <li key={loc.geo_id}>
                      <button
                        type="button"
                        onClick={() => addTargetLocation(loc)}
                        className="w-full text-start px-3 py-2 text-sm hover:bg-indigo-50 flex items-center justify-between cursor-pointer"
                      >
                        <span>{loc.label}</span>
                        <Plus className="h-4 w-4 text-indigo-600" aria-hidden="true" />
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </div>
            <div className="flex flex-wrap gap-2">
              {(pref.target_locations || []).map((loc: any, idx: number) => (
                <span key={idx} className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-indigo-50 text-indigo-800 text-xs font-medium">
                  <span>{loc.label || loc.country_code}</span>
                  <button
                    type="button"
                    onClick={() =>
                      setPref({
                        ...pref,
                        target_locations: pref.target_locations.filter((_: any, i: number) => i !== idx),
                      })
                    }
                    className="hover:text-rose-600 cursor-pointer"
                    aria-label={`Remove ${loc.label}`}
                  >
                    <X className="h-3.5 w-3.5" />
                  </button>
                </span>
              ))}
            </div>
          </div>

          <div className="md:col-span-2">
            <div className="text-xs font-semibold text-slate-700 mb-2">{t.preferences.remoteModes}</div>
            <div className="flex flex-wrap gap-3">
              {REMOTE_MODES.map((m) => (
                <label key={m} className="inline-flex items-center gap-1.5 text-sm text-slate-700">
                  <input
                    type="checkbox"
                    checked={(pref.allowed_remote_modes || []).includes(m)}
                    onChange={() => toggleRemoteMode(m)}
                  />
                  <span>{m}</span>
                </label>
              ))}
            </div>
          </div>
        </section>

        {/* Money & Unknown Policies */}
        <section className="bg-white rounded-xl border border-slate-200 p-5 shadow-xs grid grid-cols-1 md:grid-cols-3 gap-4">
          <div>
            <label className="block text-xs font-semibold text-slate-700 mb-1">{t.preferences.salaryMin}</label>
            <input
              type="number"
              min={0}
              value={pref.min_salary_amount ?? ''}
              onChange={(e) => setPref({ ...pref, min_salary_amount: e.target.value })}
              className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
            />
          </div>
          <div>
            <label className="block text-xs font-semibold text-slate-700 mb-1">{t.preferences.salaryCurrency}</label>
            <input
              type="text"
              value={pref.min_salary_currency}
              onChange={(e) => setPref({ ...pref, min_salary_currency: e.target.value.toUpperCase() })}
              className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
            />
          </div>
          <div>
            <label className="block text-xs font-semibold text-slate-700 mb-1">{t.preferences.salaryPeriod}</label>
            <select
              value={pref.min_salary_period}
              onChange={(e) => setPref({ ...pref, min_salary_period: e.target.value })}
              className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm bg-white"
            >
              <option value="hourly">hourly</option>
              <option value="daily">daily</option>
              <option value="weekly">weekly</option>
              <option value="monthly">monthly</option>
              <option value="annual">annual</option>
            </select>
          </div>

          <div className="md:col-span-3 pt-2 border-t border-slate-100">
            <h2 className="text-sm font-semibold text-slate-900 mb-3">{t.preferences.unknownPolicies}</h2>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <div>
                <label className="block text-xs font-semibold text-slate-700 mb-1">{t.preferences.unknownSalary}</label>
                <select
                  value={pref.unknown_salary_policy}
                  onChange={(e) => setPref({ ...pref, unknown_salary_policy: e.target.value })}
                  className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm bg-white"
                >
                  <option value="allow_neutral">allow_neutral</option>
                  <option value="skip">skip</option>
                  <option value="require_review">require_review</option>
                </select>
                <p className="text-[11px] text-slate-500 mt-1">{explanations.unknown_salary_policy}</p>
              </div>
              <div>
                <label className="block text-xs font-semibold text-slate-700 mb-1">{t.preferences.unknownLocation}</label>
                <select
                  value={pref.unknown_location_policy}
                  onChange={(e) => setPref({ ...pref, unknown_location_policy: e.target.value })}
                  className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm bg-white"
                >
                  <option value="skip">skip</option>
                  <option value="allow_if_remote_worldwide">allow_if_remote_worldwide</option>
                  <option value="require_review">require_review</option>
                </select>
                <p className="text-[11px] text-slate-500 mt-1">{explanations.unknown_location_policy}</p>
              </div>
              <div>
                <label className="block text-xs font-semibold text-slate-700 mb-1">{t.preferences.unknownDates}</label>
                <select
                  value={pref.unknown_dates_policy}
                  onChange={(e) => setPref({ ...pref, unknown_dates_policy: e.target.value })}
                  className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm bg-white"
                >
                  <option value="allow_neutral">allow_neutral</option>
                  <option value="skip">skip</option>
                </select>
              </div>
              <div>
                <label className="block text-xs font-semibold text-slate-700 mb-1">{t.preferences.unknownEligibility}</label>
                <select
                  value={pref.unknown_eligibility_policy}
                  onChange={(e) => setPref({ ...pref, unknown_eligibility_policy: e.target.value })}
                  className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm bg-white"
                >
                  <option value="skip">skip</option>
                  <option value="allow_neutral">allow_neutral</option>
                  <option value="require_review">require_review</option>
                </select>
                <p className="text-[11px] text-slate-500 mt-1">{explanations.unknown_eligibility_policy}</p>
              </div>
            </div>
          </div>
        </section>

        {/* Languages, Limits, Windows, Follow-ups */}
        <section className="bg-white rounded-xl border border-slate-200 p-5 shadow-xs space-y-4">
          <h2 className="text-base font-semibold text-slate-900">{t.preferences.languagesTitle}</h2>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            <div>
              <label className="block text-xs font-semibold text-slate-700 mb-1">{t.preferences.searchLanguages}</label>
              <input
                type="text"
                value={searchLangsText}
                onChange={(e) => setSearchLangsText(e.target.value)}
                className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
              />
            </div>
            <div>
              <label className="block text-xs font-semibold text-slate-700 mb-1">{t.preferences.appLanguageMode}</label>
              <select
                value={pref.application_language_mode}
                onChange={(e) => setPref({ ...pref, application_language_mode: e.target.value })}
                className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm bg-white"
              >
                <option value="employer_language_if_supported">employer_language_if_supported</option>
                <option value="explicit">explicit</option>
              </select>
            </div>
            <div>
              <label className="block text-xs font-semibold text-slate-700 mb-1">{t.preferences.explicitLang}</label>
              <select
                value={pref.explicit_application_language}
                onChange={(e) => setPref({ ...pref, explicit_application_language: e.target.value })}
                className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm bg-white"
              >
                <option value="en">en</option>
                <option value="ar">ar</option>
                <option value="fr">fr</option>
                <option value="de">de</option>
                <option value="es">es</option>
              </select>
            </div>
            <div>
              <label className="block text-xs font-semibold text-slate-700 mb-1">{t.daily.requestedCount}</label>
              <input
                type="number"
                min={1}
                max={50}
                value={pref.batch_size}
                onChange={(e) => setPref({ ...pref, batch_size: Number(e.target.value) })}
                className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
              />
            </div>
            <div>
              <label className="block text-xs font-semibold text-slate-700 mb-1">{t.preferences.searchQueryBudget}</label>
              <input
                type="number"
                min={1}
                max={20}
                value={pref.max_search_queries_per_batch ?? 6}
                onChange={(e) => setPref({ ...pref, max_search_queries_per_batch: Number(e.target.value) })}
                className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
              />
            </div>
            <div>
              <label className="block text-xs font-semibold text-slate-700 mb-1">
                {t.preferences.dailyLimit} (max {hardCap})
              </label>
              <input
                type="number"
                min={1}
                max={hardCap}
                value={pref.daily_recipient_limit}
                onChange={(e) => setPref({ ...pref, daily_recipient_limit: Number(e.target.value) })}
                className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
              />
              <p className="text-[11px] text-slate-500 mt-1">{explanations.daily_recipient_limit}</p>
            </div>
            <div>
              <label className="block text-xs font-semibold text-slate-700 mb-1">Match Threshold (0-100)</label>
              <input
                type="number"
                min={0}
                max={100}
                value={pref.min_match_score}
                onChange={(e) => setPref({ ...pref, min_match_score: Number(e.target.value) })}
                className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
              />
            </div>
            <div>
              <label className="block text-xs font-semibold text-slate-700 mb-1">Send Window (Local HH:MM)</label>
              <div className="flex gap-2">
                <input
                  type="text"
                  value={pref.send_window_start_hhmm}
                  onChange={(e) => setPref({ ...pref, send_window_start_hhmm: e.target.value })}
                  className="w-1/2 rounded-lg border border-slate-300 px-2 py-2 text-sm"
                />
                <input
                  type="text"
                  value={pref.send_window_end_hhmm}
                  onChange={(e) => setPref({ ...pref, send_window_end_hhmm: e.target.value })}
                  className="w-1/2 rounded-lg border border-slate-300 px-2 py-2 text-sm"
                />
              </div>
            </div>
            <div>
              <label className="block text-xs font-semibold text-slate-700 mb-1">Quiet Hours (Local HH:MM)</label>
              <div className="flex gap-2">
                <input
                  type="text"
                  value={pref.quiet_hours_start_hhmm}
                  onChange={(e) => setPref({ ...pref, quiet_hours_start_hhmm: e.target.value })}
                  className="w-1/2 rounded-lg border border-slate-300 px-2 py-2 text-sm"
                />
                <input
                  type="text"
                  value={pref.quiet_hours_end_hhmm}
                  onChange={(e) => setPref({ ...pref, quiet_hours_end_hhmm: e.target.value })}
                  className="w-1/2 rounded-lg border border-slate-300 px-2 py-2 text-sm"
                />
              </div>
            </div>
            <div>
              <label className="block text-xs font-semibold text-slate-700 mb-1">Timezone (IANA)</label>
              <input
                type="text"
                value={pref.timezone}
                onChange={(e) => setPref({ ...pref, timezone: e.target.value })}
                className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
              />
            </div>
            <div>
              <label className="block text-xs font-semibold text-slate-700 mb-1">{t.preferences.followupInterval}</label>
              <input
                type="number"
                min={2}
                max={30}
                value={pref.followup_interval_days}
                onChange={(e) => setPref({ ...pref, followup_interval_days: Number(e.target.value) })}
                className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
              />
            </div>
            <div>
              <label className="block text-xs font-semibold text-slate-700 mb-1">{t.preferences.followupMax}</label>
              <input
                type="number"
                min={0}
                max={2}
                value={pref.followup_max_count}
                onChange={(e) => setPref({ ...pref, followup_max_count: Number(e.target.value) })}
                className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
              />
            </div>
            <div className="flex items-center pt-4">
              <label className="inline-flex items-center gap-2 text-sm text-slate-700">
                <input
                  type="checkbox"
                  checked={pref.include_standing_career_invitations}
                  onChange={(e) => setPref({ ...pref, include_standing_career_invitations: e.target.checked })}
                />
                <span>{t.preferences.standingInvites}</span>
              </label>
            </div>
          </div>

          <div className="pt-4 border-t border-slate-100 space-y-3">
            <h3 className="text-sm font-semibold text-slate-900">{t.preferences.campaignModeTitle}</h3>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <div>
                <label className="block text-xs font-semibold text-slate-700 mb-1">{t.preferences.campaignModeLabel}</label>
                <select
                  value={pref.campaign_mode || 'dry_run'}
                  onChange={(e) => setPref({ ...pref, campaign_mode: e.target.value })}
                  className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm bg-white"
                >
                  <option value="dry_run">{t.daily.dry}</option>
                  <option value="approval_required">{t.daily.approval}</option>
                  {pref.campaign_mode === 'autopilot' && <option value="autopilot" disabled>Autopilot</option>}
                </select>
              </div>
              <div>
                <label className="block text-xs font-semibold text-slate-700 mb-1">{t.preferences.targetGoalLabel}</label>
                <input
                  type="number"
                  min={1}
                  max={50}
                  value={pref.target_goal_count ?? 10}
                  onChange={(e) => setPref({ ...pref, target_goal_count: Number(e.target.value) })}
                  className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
                />
              </div>
            </div>
            {pref.campaign_mode === 'autopilot' && <label className="flex items-start gap-2 text-xs text-slate-700 cursor-pointer p-3 rounded-lg bg-slate-50 border border-slate-200">
              <input
                type="checkbox"
                checked={Boolean(pref.live_consent_granted)}
                onChange={(e) => setPref({ ...pref, live_consent_granted: e.target.checked })}
                className="mt-0.5"
              />
              <span>{t.preferences.liveConsentLabel}</span>
            </label>}
            <label className="flex items-center gap-2 text-xs text-slate-700 cursor-pointer">
              <input
                type="checkbox"
                checked={Boolean(pref.save_drafts_in_gmail)}
                onChange={(e) => setPref({ ...pref, save_drafts_in_gmail: e.target.checked })}
              />
              <span>{t.preferences.saveDraftsInGmailLabel}</span>
            </label>
          </div>

          <button
            type="submit"
            disabled={saving}
            className="px-5 py-2.5 rounded-lg bg-indigo-600 hover:bg-indigo-700 text-white text-sm font-semibold cursor-pointer disabled:opacity-50"
          >
            {saving ? t.controls.saving : t.preferences.saveBtn}
          </button>
        </section>
      </form>
    </div>
  );
};
