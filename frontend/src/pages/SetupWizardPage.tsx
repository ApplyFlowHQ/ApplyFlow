import React, { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Briefcase,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  Globe,
  RefreshCw,
  ShieldCheck,
  Sparkles,
} from 'lucide-react';
import { useI18n } from '../i18n';
import { apiRequest, ApiError, setCsrfToken } from '../lib/api';
import type { SetupStatusResponse } from '../lib/setupTypes';
import type { UserSummary } from '../components/AppShell';
import {
  AiConfigPanel,
  OAuthAppsPanel,
  SearchConfigPanel,
  StatusBadge,
  TransactionalEmailPanel,
} from '../components/IntegrationPanels';
import { PersonalMailboxSection } from '../components/PersonalMailboxSection';
import { LiveSendingConfirmPanel } from '../components/LiveSendingConfirmPanel';

interface SetupWizardProps {
  user: UserSummary | null;
  setupStatus: SetupStatusResponse | null;
  onRefreshStatus: () => Promise<void>;
  onAuthenticated: (user: UserSummary) => Promise<void>;
}

export const SetupWizardPage: React.FC<SetupWizardProps> = ({
  user,
  setupStatus,
  onRefreshStatus,
  onAuthenticated,
}) => {
  const { t, locale, setLocale, toggleLocale } = useI18n();
  const navigate = useNavigate();

  // Claim ownership state
  const [setupCode, setSetupCode] = useState('');
  const [ownerEmail, setOwnerEmail] = useState('');
  const [ownerPassword, setOwnerPassword] = useState('');
  const [ownerName, setOwnerName] = useState('');
  const [timezone, setTimezone] = useState(() => Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC');
  const [claimBusy, setClaimBusy] = useState(false);

  // Wizard state
  const [localStatus, setLocalStatus] = useState<SetupStatusResponse | null>(setupStatus);
  const [step, setStep] = useState<number>(setupStatus?.current_step || 1);
  const [deploymentMode, setDeploymentMode] = useState<'local' | 'hosted'>(setupStatus?.deployment_mode || 'local');
  const [defaultLocale, setDefaultLocale] = useState<'en' | 'ar'>(setupStatus?.default_locale || 'en');
  const [defaultTimezone, setDefaultTimezone] = useState<string>(setupStatus?.default_timezone || 'UTC');
  const [sheetsStatus, setSheetsStatus] = useState<any>(null);

  const [msg, setMsg] = useState('');
  const [err, setErr] = useState('');
  const [busyStep, setBusyStep] = useState(false);

  useEffect(() => {
    setLocalStatus(setupStatus);
    if (setupStatus?.current_step) setStep(setupStatus.current_step);
    if (setupStatus?.deployment_mode) setDeploymentMode(setupStatus.deployment_mode);
    if (setupStatus?.default_locale) setDefaultLocale(setupStatus.default_locale);
    if (setupStatus?.default_timezone) setDefaultTimezone(setupStatus.default_timezone);
  }, [setupStatus]);

  useEffect(() => {
    if (user) {
      apiRequest('/sheets')
        .then(setSheetsStatus)
        .catch(() => {});
    }
  }, [user]);

  const handleFeedback = (text: string, isError = false) => {
    if (isError) {
      setErr(text);
      setMsg('');
    } else {
      setMsg(text);
      setErr('');
    }
  };

  const handleClaim = async (e: React.FormEvent) => {
    e.preventDefault();
    setClaimBusy(true);
    setErr('');
    setMsg('');
    try {
      const res = await apiRequest<{ user: UserSummary; setup: SetupStatusResponse }>('/setup/claim', {
        method: 'POST',
        body: JSON.stringify({
          setup_code: setupCode.trim(),
          email: user ? undefined : ownerEmail.trim(),
          password: user ? undefined : ownerPassword,
          full_name: ownerName.trim(),
          ui_locale: locale,
          timezone,
        }),
      });
      setCsrfToken(res.user.csrf_token);
      setLocalStatus(res.setup);
      setStep(res.setup.current_step || 1);
      await onAuthenticated(res.user);
      await onRefreshStatus();
      setMsg('Installation ownership verified. Welcome to the Setup Wizard.');
    } catch (e2) {
      setErr(e2 instanceof ApiError ? e2.message : 'Claim verification failed.');
    } finally {
      setClaimBusy(false);
    }
  };

  // Unclaimed or signed-in user claiming installation ownership
  if (!localStatus || localStatus.requires_claim) {
    return (
      <div className="min-h-screen bg-slate-950 flex flex-col justify-center items-center px-4 py-10">
        <div className="w-full max-w-lg bg-white rounded-2xl shadow-xl border border-slate-200 p-6 sm:p-8 space-y-5">
          <div className="flex items-start justify-between gap-3">
            <div className="flex items-center gap-3">
              <div className="h-10 w-10 rounded-xl bg-indigo-600 flex items-center justify-center shrink-0">
                <ShieldCheck className="h-5 w-5 text-white" aria-hidden="true" />
              </div>
              <div>
                <div className="text-[11px] font-semibold uppercase tracking-wider text-indigo-600">
                  {t.setup.claimBadge}
                </div>
                <h1 className="text-lg font-bold text-slate-900">{t.setup.claimTitle}</h1>
              </div>
            </div>
            <button
              type="button"
              onClick={toggleLocale}
              className="inline-flex items-center gap-1 px-2.5 py-1 rounded-md border border-slate-200 text-xs font-medium text-slate-700 hover:bg-slate-50 cursor-pointer"
            >
              <Globe className="h-3.5 w-3.5" aria-hidden="true" />
              <span>{t.controls.languageToggle}</span>
            </button>
          </div>

          <p className="text-xs text-slate-600 leading-relaxed">{t.setup.claimSubtitle}</p>

          {err && (
            <div role="alert" className="p-3 rounded-lg bg-rose-50 border border-rose-200 text-rose-800 text-xs">
              {err}
            </div>
          )}

          <form onSubmit={handleClaim} className="space-y-3">
            <div>
              <label htmlFor="claim-setup-code" className="block text-xs font-medium text-slate-700 mb-1">{t.setup.setupCodeLabel}</label>
              <input
                id="claim-setup-code"
                type="password"
                required
                autoComplete="off"
                value={setupCode}
                onChange={(e) => setSetupCode(e.target.value)}
                placeholder="af_setup_..."
                className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm font-mono"
              />
            </div>

            {!user ? (
              <>
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  <div>
                    <label htmlFor="claim-owner-email" className="block text-xs font-medium text-slate-700 mb-1">{t.setup.ownerEmailLabel}</label>
                    <input
                      id="claim-owner-email"
                      type="email"
                      required
                      value={ownerEmail}
                      onChange={(e) => setOwnerEmail(e.target.value)}
                      className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
                    />
                  </div>
                  <div>
                    <label htmlFor="claim-owner-name" className="block text-xs font-medium text-slate-700 mb-1">{t.auth.fullName}</label>
                    <input
                      id="claim-owner-name"
                      type="text"
                      value={ownerName}
                      onChange={(e) => setOwnerName(e.target.value)}
                      className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
                    />
                  </div>
                </div>
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  <div>
                    <label htmlFor="claim-owner-password" className="block text-xs font-medium text-slate-700 mb-1">{t.setup.ownerPasswordLabel}</label>
                    <input
                      id="claim-owner-password"
                      type="password"
                      required
                      minLength={10}
                      value={ownerPassword}
                      onChange={(e) => setOwnerPassword(e.target.value)}
                      className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
                    />
                  </div>
                  <div>
                    <label htmlFor="claim-timezone" className="block text-xs font-medium text-slate-700 mb-1">{t.auth.timezone}</label>
                    <input
                      id="claim-timezone"
                      type="text"
                      required
                      value={timezone}
                      onChange={(e) => setTimezone(e.target.value)}
                      className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
                    />
                  </div>
                </div>
                <p className="text-[11px] text-slate-500">{t.setup.claimExistingNote}</p>
              </>
            ) : (
              <div className="rounded-lg bg-slate-50 border border-slate-200 p-3 text-xs text-slate-700">
                Claiming ownership as signed-in account <strong>{user.email}</strong>.
              </div>
            )}

            <button
              type="submit"
              disabled={claimBusy}
              className="w-full py-2.5 px-4 rounded-lg bg-indigo-600 hover:bg-indigo-700 text-white text-sm font-semibold cursor-pointer disabled:opacity-50"
            >
              {claimBusy ? t.controls.saving : t.setup.claimBtn}
            </button>
          </form>
        </div>
      </div>
    );
  }

  // Ordinary multi-user view if a non-owner opens /setup
  if (user && !localStatus.can_manage_installation) {
    return (
      <div className="space-y-6">
        <div>
          <h1 className="text-2xl font-bold text-slate-900">Personal Workspace Setup</h1>
          <p className="text-sm text-slate-600 mt-1">
            Installation-wide provider credentials are managed by the installation owner. You can connect your own AI/search keys, personal mailbox, and optional Google Sheets below.
          </p>
        </div>
        {msg && <div className="p-3 rounded-lg bg-emerald-50 border border-emerald-200 text-emerald-800 text-xs">{msg}</div>}
        {err && <div className="p-3 rounded-lg bg-rose-50 border border-rose-200 text-rose-800 text-xs">{err}</div>}
        <section className="bg-white rounded-xl border border-slate-200 p-5 space-y-5">
          <AiConfigPanel scope="user" statusData={localStatus} onUpdated={setLocalStatus} onFeedback={handleFeedback} />
        </section>
        <section className="bg-white rounded-xl border border-slate-200 p-5 space-y-5">
          <SearchConfigPanel scope="user" statusData={localStatus} onUpdated={setLocalStatus} onFeedback={handleFeedback} />
        </section>
        <section className="bg-white rounded-xl border border-slate-200 p-5 space-y-5">
          <PersonalMailboxSection user={user} onRefreshUser={() => onRefreshStatus()} onFeedback={handleFeedback} />
        </section>
      </div>
    );
  }

  if (!user) {
    return null;
  }

  const updateProgress = async (
    nextStep: number,
    opts?: { completeStep?: number; skipStep?: number; saveWelcome?: boolean }
  ) => {
    setBusyStep(true);
    setErr('');
    try {
      const payload: Record<string, any> = { current_step: nextStep };
      if (opts?.completeStep) payload.complete_step = opts.completeStep;
      if (opts?.skipStep) payload.skip_step = opts.skipStep;
      if (opts?.saveWelcome) {
        payload.deployment_mode = deploymentMode;
        payload.default_locale = defaultLocale;
        payload.default_timezone = defaultTimezone;
        setLocale(defaultLocale);
      }
      const next = await apiRequest<SetupStatusResponse>('/setup/progress', {
        method: 'PATCH',
        body: JSON.stringify(payload),
      });
      setLocalStatus(next);
      setStep(nextStep);
    } catch (e2) {
      setErr(e2 instanceof ApiError ? e2.message : 'Unable to save wizard progress.');
    } finally {
      setBusyStep(false);
    }
  };

  const handleFinishSetup = async () => {
    setBusyStep(true);
    setErr('');
    try {
      const next = await apiRequest<SetupStatusResponse>('/setup/complete', { method: 'POST' });
      setLocalStatus(next);
      await onRefreshStatus();
      navigate('/dashboard');
    } catch (e2) {
      setErr(e2 instanceof ApiError ? e2.message : 'Unable to finalize setup.');
    } finally {
      setBusyStep(false);
    }
  };

  const installKindLabel =
    localStatus.installation_kind === 'existing'
      ? t.setup.existingInstallLabel
      : localStatus.installation_kind === 'resumed'
        ? t.setup.resumedInstallLabel
        : t.setup.freshInstallLabel;

  const readiness = localStatus.readiness;
  const completedSteps = localStatus.completed_steps || [];
  const skippedSteps = localStatus.skipped_steps || [];

  return (
    <div className="space-y-6 max-w-4xl mx-auto">
      {/* Header & Progress Bar */}
      <div className="bg-white rounded-2xl border border-slate-200 p-5 shadow-xs space-y-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-3">
            <div className="h-9 w-9 rounded-xl bg-indigo-600 flex items-center justify-center shrink-0">
              <Sparkles className="h-5 w-5 text-white" aria-hidden="true" />
            </div>
            <div>
              <h1 className="text-xl font-bold text-slate-900">{t.setup.wizardTitle}</h1>
              <div className="flex flex-wrap items-center gap-2 text-xs text-slate-500 mt-0.5">
                <span className="px-2 py-0.5 rounded bg-slate-100 text-slate-700 font-medium">{installKindLabel}</span>
                <span>Step {step} of 7</span>
              </div>
            </div>
          </div>
          {localStatus.setup_state === 'completed' && (
            <button
              type="button"
              onClick={() => navigate('/dashboard')}
              className="px-3 py-1.5 rounded-lg border border-slate-200 text-xs font-medium text-slate-700 hover:bg-slate-50 cursor-pointer"
            >
              {t.nav.dashboard}
            </button>
          )}
        </div>

        {/* Stepper pills */}
        <div className="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-7 gap-2" role="tablist" aria-label="Setup steps">
          {[1, 2, 3, 4, 5, 6, 7].map((s) => {
            const label = (t.setup.steps as Record<number, string>)[s];
            const isCurrent = step === s;
            const isDone = completedSteps.includes(s);
            const isSkipped = skippedSteps.includes(s);
            return (
              <button
                key={s}
                type="button"
                onClick={() => updateProgress(s)}
                className={`px-2.5 py-2 rounded-lg border text-start text-xs transition-colors cursor-pointer ${
                  isCurrent
                    ? 'border-indigo-600 bg-indigo-50 text-indigo-900 font-semibold'
                    : isDone
                      ? 'border-emerald-200 bg-emerald-50/50 text-emerald-800'
                      : isSkipped
                        ? 'border-slate-200 bg-slate-50 text-slate-500'
                        : 'border-slate-200 bg-white text-slate-600 hover:bg-slate-50'
                }`}
              >
                <div className="text-[10px] opacity-75">
                  {s}. {isDone ? '✓' : isSkipped ? 'Skipped' : ''}
                </div>
                <div className="truncate">{label}</div>
              </button>
            );
          })}
        </div>
      </div>

      {msg && (
        <div role="status" className="p-3 rounded-lg bg-emerald-50 border border-emerald-200 text-emerald-800 text-xs">
          {msg}
        </div>
      )}
      {err && (
        <div role="alert" className="p-3 rounded-lg bg-rose-50 border border-rose-200 text-rose-800 text-xs">
          {err}
        </div>
      )}

      {/* Active Step Body */}
      <div className="bg-white rounded-2xl border border-slate-200 p-5 sm:p-6 shadow-xs space-y-5">
        {step === 1 && (
          <div className="space-y-4">
            <h2 className="text-lg font-semibold text-slate-900">{(t.setup.steps as any)[1]}</h2>
            <p className="text-sm text-slate-600">
              This guided setup configures only the external services you choose to enable. Missing optional services never block startup and remain clearly marked as unconfigured or skipped.
            </p>

            {readiness && (
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
                <div className="p-3 rounded-xl border border-slate-200 bg-slate-50 space-y-1 text-xs">
                  <div className="font-semibold text-slate-800">Database</div>
                  <StatusBadge status={readiness.database.status} />
                  <div className="text-slate-500 truncate">{readiness.database.dialect}</div>
                </div>
                <div className="p-3 rounded-xl border border-slate-200 bg-slate-50 space-y-1 text-xs">
                  <div className="font-semibold text-slate-800">AI Drafting</div>
                  <StatusBadge status={readiness.ai.status} />
                  <div className="text-slate-500 truncate">{readiness.ai.provider}</div>
                </div>
                <div className="p-3 rounded-xl border border-slate-200 bg-slate-50 space-y-1 text-xs">
                  <div className="font-semibold text-slate-800">Job Search</div>
                  <StatusBadge status={readiness.search.status} />
                  <div className="text-slate-500 truncate">{readiness.search.provider}</div>
                </div>
                <div className="p-3 rounded-xl border border-slate-200 bg-slate-50 space-y-1 text-xs">
                  <div className="font-semibold text-slate-800">Live Sending</div>
                  <StatusBadge status={readiness.live_sending.status} />
                  <div className="text-slate-500 truncate">{readiness.live_sending.enabled ? 'Enabled' : 'Disabled'}</div>
                </div>
              </div>
            )}

            <div className="grid grid-cols-1 md:grid-cols-3 gap-4 pt-2">
              <div>
                <label className="block text-xs font-medium text-slate-700 mb-1">Default Language / Locale</label>
                <select
                  value={defaultLocale}
                  onChange={(e) => {
                    const nextLoc = e.target.value as 'en' | 'ar';
                    setDefaultLocale(nextLoc);
                    setLocale(nextLoc);
                  }}
                  className="w-full rounded-lg border border-slate-300 px-3 py-2 text-xs"
                >
                  <option value="en">English (LTR)</option>
                  <option value="ar">العربية (RTL)</option>
                </select>
              </div>
              <div>
                <label className="block text-xs font-medium text-slate-700 mb-1">Default Timezone (IANA)</label>
                <input
                  type="text"
                  value={defaultTimezone}
                  onChange={(e) => setDefaultTimezone(e.target.value)}
                  className="w-full rounded-lg border border-slate-300 px-3 py-2 text-xs"
                />
              </div>
              <div>
                <label className="block text-xs font-medium text-slate-700 mb-1">Deployment Mode</label>
                <select
                  value={deploymentMode}
                  onChange={(e) => setDeploymentMode(e.target.value as 'local' | 'hosted')}
                  className="w-full rounded-lg border border-slate-300 px-3 py-2 text-xs"
                >
                  <option value="local">Local / Single-Host Workspace</option>
                  <option value="hosted">Hosted Multi-User Team Server</option>
                </select>
              </div>
            </div>
          </div>
        )}

        {step === 2 && (
          <div className="space-y-4">
            <h2 className="text-lg font-semibold text-slate-900">{(t.setup.steps as any)[2]}</h2>
            <p className="text-xs text-slate-600">
              Choose either OpenAI or Google Gemini to enable grounded CV matching and truthful email draft generation. You do not need both, and skipping preserves manual workspace notes.
            </p>
            <AiConfigPanel scope="installation" statusData={localStatus} onUpdated={setLocalStatus} onFeedback={handleFeedback} />
          </div>
        )}

        {step === 3 && (
          <div className="space-y-4">
            <h2 className="text-lg font-semibold text-slate-900">{(t.setup.steps as any)[3]}</h2>
            <p className="text-xs text-slate-600">
              Choose Tavily, Brave Search, or both for automated public job-board discovery. Skipping leaves manual URL pasting and CSV/XLSX application import ready to use.
            </p>
            <SearchConfigPanel scope="installation" statusData={localStatus} onUpdated={setLocalStatus} onFeedback={handleFeedback} />
          </div>
        )}

        {step === 4 && (
          <div className="space-y-6">
            <div>
              <h2 className="text-lg font-semibold text-slate-900">{(t.setup.steps as any)[4]}</h2>
              <p className="text-xs text-slate-600 mt-1">
                Installation owner configuration (shared Google/Microsoft OAuth apps and system transactional email) is kept separate from each user’s personal sending mailbox connection.
              </p>
            </div>

            <div className="space-y-4">
              <h3 className="text-xs font-bold uppercase tracking-wider text-slate-500">
                1. Installation Owner App &amp; Transactional Email Config
              </h3>
              <OAuthAppsPanel statusData={localStatus} onUpdated={setLocalStatus} onFeedback={handleFeedback} />
              <TransactionalEmailPanel statusData={localStatus} onUpdated={setLocalStatus} onFeedback={handleFeedback} />
            </div>

            <div className="pt-4 border-t border-slate-200 space-y-4">
              <h3 className="text-xs font-bold uppercase tracking-wider text-slate-500">
                2. Your Personal Sending Mailbox Connection
              </h3>
              <PersonalMailboxSection user={user} onRefreshUser={() => onRefreshStatus()} onFeedback={handleFeedback} />
            </div>
          </div>
        )}

        {step === 5 && (
          <div className="space-y-4">
            <h2 className="text-lg font-semibold text-slate-900">{(t.setup.steps as any)[5]}</h2>
            <p className="text-xs text-slate-600">
              Google Sheets (`drive.file` least-privilege scope) mirrors your application tracker to a Google spreadsheet. Skipping this keeps ApplyFlow’s built-in tracker and CSV/XLSX exports fully operational.
            </p>
            <div className="rounded-xl border border-slate-200 p-4 space-y-3 bg-slate-50/70">
              <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
                <span className="font-semibold text-slate-800">Google Sheets Sync</span>
                <StatusBadge status={sheetsStatus?.connected ? 'verified' : localStatus.readiness?.optional_sheets.status || 'not_configured'} />
              </div>
              {sheetsStatus?.connected ? (
                <div className="flex flex-wrap items-center gap-3 text-xs">
                  <span className="text-emerald-700">Connected spreadsheet ready.</span>
                  {sheetsStatus.spreadsheet_url && (
                    <a href={sheetsStatus.spreadsheet_url} target="_blank" rel="noreferrer" className="text-indigo-600 underline">
                      Open Google Sheet
                    </a>
                  )}
                  <button
                    type="button"
                    onClick={async () => {
                      await apiRequest('/sheets/disconnect', { method: 'POST' });
                      setSheetsStatus(await apiRequest('/sheets'));
                      await onRefreshStatus();
                    }}
                    className="px-3 py-1.5 rounded-lg border border-rose-200 text-rose-700 text-xs cursor-pointer"
                  >
                    Disconnect Google Sheets
                  </button>
                </div>
              ) : (
                <button
                  type="button"
                  disabled={!localStatus.shared_capabilities?.google_oauth_available}
                  onClick={async () => {
                    try {
                      const r = await apiRequest<{ authorization_url: string }>('/sheets/connect-url');
                      window.location.href = r.authorization_url;
                    } catch (e2) {
                      handleFeedback(e2 instanceof ApiError ? e2.message : 'Configure Google OAuth first in Step 4.', true);
                    }
                  }}
                  className="px-4 py-2 rounded-lg bg-indigo-600 hover:bg-indigo-700 disabled:bg-slate-200 disabled:text-slate-500 text-white text-xs font-semibold cursor-pointer"
                >
                  {t.tracker.sheetsConnect}
                </button>
              )}
              {!localStatus.shared_capabilities?.google_oauth_available && (
                <p className="text-xs text-slate-500">Google OAuth client credentials (Step 4) are needed to authorize Google Sheets.</p>
              )}
            </div>
          </div>
        )}

        {step === 6 && readiness && (
          <div className="space-y-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h2 className="text-lg font-semibold text-slate-900">{(t.setup.steps as any)[6]}</h2>
              <button
                type="button"
                onClick={async () => {
                  const fresh = await apiRequest<SetupStatusResponse>('/setup/status');
                  setLocalStatus(fresh);
                }}
                className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-slate-200 text-xs font-medium hover:bg-slate-50 cursor-pointer"
              >
                <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
                <span>{t.setup.refreshChecksBtn}</span>
              </button>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
              {[
                { title: '1. Application & Database', item: readiness.database },
                { title: '2. Background Worker & Scheduler', item: readiness.worker },
                { title: '3. AI Provider & Validation', item: readiness.ai },
                { title: '4. Job-Search Provider', item: readiness.search },
                { title: '5. Sending Mailbox & Capabilities', item: readiness.mailbox },
                { title: '6. Transactional Email', item: readiness.transactional_email },
                { title: '7. Optional Integrations (Sheets)', item: readiness.optional_sheets },
                { title: '8. Live Sending Safety Gate', item: readiness.live_sending },
              ].map((card) => (
                <div key={card.title} className="rounded-xl border border-slate-200 p-3.5 space-y-1.5 bg-slate-50/50">
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-xs font-semibold text-slate-900">{card.title}</span>
                    <StatusBadge status={card.item.status} />
                  </div>
                  <p className="text-xs text-slate-600">{card.item.message}</p>
                </div>
              ))}
            </div>

            {/* Explicit owner-only Live Sending toggle (default OFF) */}
            <div className="rounded-xl border border-amber-200 bg-amber-50 p-4 flex flex-wrap items-start justify-between gap-3">
              <div className="text-xs text-amber-900 space-y-1 max-w-xl">
                <div className="font-semibold">Live Sending Safety Control</div>
                <div>
                  Live email delivery defaults to <strong>DISABLED</strong> (safe dry-run mode) even after finishing setup. Toggle this only when your mailbox, limits, and review workflow are ready.
                </div>
              </div>
              <LiveSendingConfirmPanel
                enabled={Boolean(readiness.live_sending.enabled)}
                lockedByEnv={Boolean(localStatus.installation?.lock_env_config)}
                onToggle={async (target) => {
                  const next = await apiRequest<SetupStatusResponse>('/setup/live-sending', {
                    method: 'POST',
                    body: JSON.stringify({ enabled: target }),
                  });
                  setLocalStatus(next);
                  await onRefreshStatus();
                }}
              />
            </div>
          </div>
        )}

        {step === 7 && readiness && (
          <div className="space-y-4">
            <h2 className="text-lg font-semibold text-slate-900">{(t.setup.steps as any)[7]}</h2>
            <div className="rounded-xl border border-indigo-200 bg-indigo-50 p-4 text-xs text-indigo-900 space-y-1">
              <div className="font-semibold flex items-center gap-1.5">
                <CheckCircle2 className="h-4 w-4 text-indigo-600" aria-hidden="true" />
                <span>Ready to launch ApplyFlow Global</span>
              </div>
              <p>{t.setup.finishDisclaimer}</p>
            </div>

            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 text-xs">
              <div className="p-3 rounded-lg border border-slate-200">
                <div className="font-semibold text-slate-900">AI Drafting</div>
                <div className="text-slate-600 mt-1">{readiness.ai.message}</div>
              </div>
              <div className="p-3 rounded-lg border border-slate-200">
                <div className="font-semibold text-slate-900">Automated Discovery</div>
                <div className="text-slate-600 mt-1">{readiness.search.message}</div>
              </div>
              <div className="p-3 rounded-lg border border-slate-200">
                <div className="font-semibold text-slate-900">Mailbox &amp; Email</div>
                <div className="text-slate-600 mt-1">{readiness.mailbox.message}</div>
              </div>
              <div className="p-3 rounded-lg border border-slate-200">
                <div className="font-semibold text-slate-900">Live Sending</div>
                <div className="text-slate-600 mt-1">{readiness.live_sending.message}</div>
              </div>
            </div>
          </div>
        )}

        {/* Wizard Footer Navigation */}
        <div className="pt-4 border-t border-slate-100 flex flex-wrap items-center justify-between gap-3">
          <button
            type="button"
            disabled={step <= 1 || busyStep}
            onClick={() => updateProgress(Math.max(1, step - 1))}
            className="inline-flex items-center gap-1 px-4 py-2 rounded-lg border border-slate-300 text-xs font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-40 cursor-pointer"
          >
            <ChevronLeft className="h-4 w-4 rtl:rotate-180" aria-hidden="true" />
            <span>{t.setup.backBtn}</span>
          </button>

          <div className="flex flex-wrap items-center gap-2">
            {step >= 2 && step <= 5 && (
              <button
                type="button"
                disabled={busyStep}
                onClick={() => updateProgress(step + 1, { skipStep: step })}
                className="px-4 py-2 rounded-lg border border-slate-300 text-xs font-medium text-slate-600 hover:bg-slate-50 cursor-pointer"
              >
                {t.setup.skipBtn}
              </button>
            )}

            {step < 7 ? (
              <button
                type="button"
                disabled={busyStep}
                onClick={() =>
                  updateProgress(step + 1, {
                    completeStep: step,
                    saveWelcome: step === 1,
                  })
                }
                className="inline-flex items-center gap-1 px-5 py-2 rounded-lg bg-indigo-600 hover:bg-indigo-700 text-white text-xs font-semibold cursor-pointer disabled:opacity-50"
              >
                <span>{t.setup.nextBtn}</span>
                <ChevronRight className="h-4 w-4 rtl:rotate-180" aria-hidden="true" />
              </button>
            ) : (
              <button
                type="button"
                disabled={busyStep}
                onClick={handleFinishSetup}
                className="inline-flex items-center gap-1.5 px-5 py-2 rounded-lg bg-emerald-600 hover:bg-emerald-700 text-white text-xs font-semibold cursor-pointer disabled:opacity-50"
              >
                <Briefcase className="h-4 w-4" aria-hidden="true" />
                <span>{t.setup.finishBtn}</span>
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
};
