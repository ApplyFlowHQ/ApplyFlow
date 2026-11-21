import React, { useEffect, useState } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import { AlertTriangle, CheckCircle2, Circle, Clock, Mail, Play, RefreshCw } from 'lucide-react';
import { useI18n } from '../i18n';
import { apiRequest, ApiError } from '../lib/api';
import type { UserSummary } from '../components/AppShell';

interface DashboardPageProps {
  user: UserSummary;
  onRefreshUser: () => Promise<void>;
}

export const DashboardPage: React.FC<DashboardPageProps> = ({ user, onRefreshUser }) => {
  const { t, formatDateTime } = useI18n();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const [mailboxes, setMailboxes] = useState<any[]>([]);
  const [googleOAuthConfigured, setGoogleOAuthConfigured] = useState(false);
  const [batches, setBatches] = useState<any[]>([]);
  const [schedules, setSchedules] = useState<any[]>([]);
  const [usage, setUsage] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [actionBusy, setActionBusy] = useState(false);
  const [error, setError] = useState('');
  const [verifyMessage, setVerifyMessage] = useState('');
  const [googleBanner, setGoogleBanner] = useState('');

  useEffect(() => {
    const gAuth = searchParams.get('google_auth');
    const gmailOk = searchParams.get('gmail_connected');
    if (gAuth === 'connected') {
      setGoogleBanner(
        gmailOk === '1'
          ? `Gmail Connected (${user.email}) — you can prepare, approve, and queue approved applications once Live Sending is enabled.`
          : `Signed in with Google as ${user.email}.`,
      );
      const next = new URLSearchParams(searchParams);
      next.delete('google_auth');
      next.delete('gmail_connected');
      setSearchParams(next, { replace: true });
    }
  }, [searchParams, setSearchParams, user.email]);

  const loadData = async () => {
    setLoading(true);
    try {
      const [mRes, bRes, sRes, uRes] = await Promise.all([
        apiRequest<{ mailboxes: any[]; google_oauth_configured?: boolean }>('/mailboxes'),
        apiRequest<{ batches: any[] }>('/batches'),
        apiRequest<{ schedules: any[] }>('/schedules'),
        apiRequest('/account/usage'),
      ]);
      setMailboxes(mRes.mailboxes || []);
      setGoogleOAuthConfigured(Boolean(mRes.google_oauth_configured));
      setBatches(bRes.batches || []);
      setSchedules(sRes.schedules || []);
      setUsage(uRes);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Unable to load dashboard');
    } finally {
      setLoading(false);
    }
  };

  const handleConnectGmailOneClick = async () => {
    setError('');
    try {
      const res = await apiRequest<{ authorization_url: string }>(
        '/mailboxes/gmail/start?include_inbox_readonly=false&include_compose_drafts=false',
      );
      window.location.href = res.authorization_url;
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Unable to start Gmail connection');
    }
  };

  useEffect(() => {
    loadData();
  }, []);

  const startQuickBatch = async (mode: 'dry_run' | 'demo_fixture') => {
    setError('');
    setActionBusy(true);
    try {
      const res = await apiRequest<{ batch_id: string }>('/batches', {
        method: 'POST',
        idempotencyKey: `dash-${mode}-${Date.now()}`,
        body: JSON.stringify({ mode, run_mode: mode, run_immediately: true }),
      });
      navigate(`/batches?selected=${res.batch_id}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to start batch');
    } finally {
      setActionBusy(false);
    }
  };

  const handleResendVerify = async () => {
    const res = await apiRequest<{ message: string }>('/auth/resend-verification', { method: 'POST' });
    setVerifyMessage(res.message);
    await onRefreshUser();
  };

  const primaryMailbox = mailboxes.find((m) => m.is_primary) || mailboxes[0];
  const nextActiveSchedule = schedules.find((s) => s.is_active && s.next_run_at);

  const checklist = [
    { done: user.onboarding.email_verified, label: t.dashboard.stepVerify, to: '/settings' },
    { done: user.onboarding.cv_uploaded, label: t.dashboard.stepCv, to: '/cvs' },
    { done: user.onboarding.profile_confirmed, label: t.dashboard.stepProfile, to: '/cvs' },
    { done: user.onboarding.preferences_configured, label: t.dashboard.stepPrefs, to: '/preferences' },
    { done: user.onboarding.mailbox_connected, label: t.dashboard.stepMailbox, to: '/settings' },
  ];

  return (
    <div className="min-w-0 space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold text-slate-900">{t.dashboard.title}</h1>
          <p className="text-sm text-slate-600">{t.daily.intro}</p>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <button type="button" data-testid="dashboard-find-prepare" onClick={() => navigate('/batches')}
            disabled={actionBusy || Boolean(user.automation_stopped_at)}
            className="inline-flex items-center gap-2 rounded-lg bg-indigo-600 px-4 py-2 text-sm font-semibold text-white hover:bg-indigo-700 disabled:opacity-50">
            <Play className="h-4 w-4" aria-hidden="true" />
            {actionBusy ? t.daily.waiting : t.daily.find}
          </button>
          <Link to="/tracker?tab=review" className="text-sm font-semibold text-indigo-700 hover:underline">{t.batches.openReviewQueue}</Link>
          <details className="text-xs text-slate-700">
            <summary className="cursor-pointer">{t.daily.manual}</summary>
            <div className="mt-2 flex flex-wrap gap-3">
              <button disabled={actionBusy} onClick={() => startQuickBatch('dry_run')}>{t.daily.dry}</button>
              <button disabled={actionBusy} onClick={() => startQuickBatch('demo_fixture')}>{t.daily.demo}</button>
            </div>
          </details>
        </div>
      </div>

      {googleBanner && (
        <div
          role="status"
          data-testid="dashboard-gmail-connected-banner"
          className="p-4 rounded-xl bg-emerald-50 border border-emerald-200 text-emerald-900 text-sm flex items-center gap-3"
        >
          <CheckCircle2 className="h-5 w-5 text-emerald-600 shrink-0" aria-hidden="true" />
          <span className="font-medium">{googleBanner}</span>
        </div>
      )}

      {(() => {
        const gmailMb = mailboxes.find((m) => m.provider === 'gmail_oauth' && m.status !== 'revoked');
        const activeMb = mailboxes.find((m) => m.status === 'connected');
        if (gmailMb) {
          const needsReconnect = Boolean(gmailMb.connection_health?.needs_reconnect || gmailMb.status === 'needs_reconnect');
          return (
            <div
              data-testid="dashboard-mailbox-status-card"
              className={`p-4 rounded-xl border text-xs flex flex-wrap items-center justify-between gap-3 ${
                needsReconnect
                  ? 'bg-amber-50 border-amber-200 text-amber-950'
                  : 'bg-emerald-50/70 border-emerald-200 text-emerald-950'
              }`}
            >
              <div className="flex items-center gap-2.5">
                <Mail className={`h-4 w-4 shrink-0 ${needsReconnect ? 'text-amber-700' : 'text-emerald-700'}`} aria-hidden="true" />
                <div>
                  <div className="font-semibold">
                    {needsReconnect ? 'Gmail Needs Reconnection' : 'Gmail Connected'} · {gmailMb.sender_email}
                  </div>
                  <div className="text-[11px] opacity-85">
                    {gmailMb.connection_health?.health_message || `Status: ${gmailMb.status}`}
                  </div>
                </div>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                {needsReconnect ? (
                  <button
                    type="button"
                    onClick={handleConnectGmailOneClick}
                    className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-amber-900 hover:bg-amber-800 text-white font-semibold cursor-pointer"
                  >
                    <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
                    <span>Reconnect Gmail</span>
                  </button>
                ) : (
                  <Link
                    to="/settings"
                    className="px-3 py-1.5 rounded-lg border border-emerald-300 bg-white text-emerald-900 font-semibold hover:bg-emerald-50"
                  >
                    Manage Gmail &amp; Permissions
                  </Link>
                )}
              </div>
            </div>
          );
        }
        if (!activeMb && googleOAuthConfigured) {
          return (
            <div
              data-testid="dashboard-connect-gmail-cta"
              className="p-4 rounded-xl bg-indigo-50 border border-indigo-200 text-indigo-950 text-xs flex flex-wrap items-center justify-between gap-3"
            >
              <div className="flex items-center gap-2.5">
                <Mail className="h-4 w-4 text-indigo-700 shrink-0" aria-hidden="true" />
                <div>
                  <div className="font-semibold">Connect Gmail in One Click</div>
                  <div className="text-[11px] text-indigo-800">
                    Grant Gmail send permission directly with your Google account — no SMTP servers or App Passwords required.
                  </div>
                </div>
              </div>
              <button
                type="button"
                data-testid="dashboard-connect-gmail-btn"
                onClick={handleConnectGmailOneClick}
                className="inline-flex items-center gap-1.5 px-3.5 py-2 rounded-lg bg-indigo-600 hover:bg-indigo-700 text-white font-semibold cursor-pointer"
              >
                <span>Connect Gmail with Google</span>
              </button>
            </div>
          );
        }
        return null;
      })()}

      {!user.live_sending_enabled_on_server && (
        <div className="p-4 rounded-xl bg-amber-50 border border-amber-200 text-amber-900 text-sm flex items-start gap-3">
          <AlertTriangle className="h-5 w-5 shrink-0 mt-0.5" aria-hidden="true" />
          <span>{t.dashboard.serverLiveDisabled}</span>
        </div>
      )}

      {!user.email_verified && (
        <div className="p-4 rounded-xl bg-indigo-50 border border-indigo-200 text-indigo-900 text-sm flex flex-wrap items-center justify-between gap-3">
          <span>{verifyMessage || t.auth.verifyEmailBanner}</span>
          <button
            type="button"
            onClick={handleResendVerify}
            className="px-3 py-1.5 rounded-lg bg-indigo-600 text-white text-xs font-semibold cursor-pointer"
          >
            {t.auth.resendVerify}
          </button>
        </div>
      )}

      {error && (
        <div role="alert" className="p-4 rounded-xl bg-rose-50 border border-rose-200 text-rose-800 text-sm">
          {error}
        </div>
      )}

      {/* Key Metrics Cards */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <div className="bg-white rounded-xl border border-slate-200 p-5 shadow-xs">
          <div className="text-xs font-semibold uppercase tracking-wide text-slate-500">{t.dashboard.rollingBudget}</div>
          <div className="mt-2 text-2xl font-bold text-slate-900">
            {primaryMailbox ? `${primaryMailbox.rolling_24h_used} / ${primaryMailbox.rolling_24h_limit}` : '0 / 20'}
          </div>
          <div className="mt-1 text-xs text-slate-500">
            {primaryMailbox ? `${primaryMailbox.label} (${primaryMailbox.status})` : 'No mailbox connected'}
          </div>
        </div>

        <div className="bg-white rounded-xl border border-slate-200 p-5 shadow-xs">
          <div className="text-xs font-semibold uppercase tracking-wide text-slate-500">{t.dashboard.nextRun}</div>
          <div className="mt-2 text-lg font-bold text-slate-900 flex items-center gap-2">
            <Clock className="h-4 w-4 text-indigo-600 shrink-0" aria-hidden="true" />
            <span>{nextActiveSchedule ? formatDateTime(nextActiveSchedule.next_run_at, nextActiveSchedule.timezone) : t.dashboard.noSchedule}</span>
          </div>
          <div className="mt-1 text-xs text-slate-500">
            {nextActiveSchedule ? `${nextActiveSchedule.name} (${nextActiveSchedule.mode})` : 'Configure in Batches & Schedules'}
          </div>
        </div>

        <div className="bg-white rounded-xl border border-slate-200 p-5 shadow-xs">
          <div className="text-xs font-semibold uppercase tracking-wide text-slate-500">{t.dashboard.monthlySpend}</div>
          <div className="mt-2 text-2xl font-bold text-slate-900">
            ${usage ? Number(usage.monthly_estimated_cost_usd || 0).toFixed(3) : '0.000'}
          </div>
          <div className="mt-1 text-xs text-slate-500">
            Limit: ${usage ? Number(usage.monthly_limit_usd || 10).toFixed(2) : '10.00'} (estimated)
          </div>
        </div>
      </div>

      {/* Section 10.1 Latest Real Batch Funnel & Review Queue Access */}
      {(() => {
        const realBatches = batches.filter((b) => b.mode !== 'demo_fixture');
        const latestReal = realBatches[0];
        if (!latestReal) return null;
        const m = latestReal.metrics || {};
        const funnel: [string, number | undefined][] = [
          [t.daily.openings, latestReal.progress_counts?.openings_discovered ?? latestReal.discovered_count],
          [t.daily.suitable, latestReal.eligible_count],
          [t.daily.duplicates, latestReal.duplicates_removed_count],
          [t.daily.contacted, latestReal.skip_reason_counts?.PREVIOUSLY_CONTACTED ?? 0],
          [t.daily.prepared, latestReal.drafted_count],
          [t.daily.attention, m.needs_human_review],
          [t.review.acceptedByProvider, latestReal.sent_count],
        ];
        return (
          <section className="bg-white rounded-xl border border-slate-200 p-5 shadow-xs space-y-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div>
                <h2 className="text-base font-semibold text-slate-900">{t.daily.result}</h2>
                <p className="text-xs text-slate-500">
                  <Link to={`/batches?selected=${latestReal.id}`} className="font-medium text-indigo-700 underline">{t.daily.progress}</Link>
                </p>
              </div>
              <Link
                to={`/tracker?tab=review&batch_id=${latestReal.id}`}
                className="px-3 py-1.5 rounded-lg bg-emerald-700 hover:bg-emerald-800 text-white text-xs font-semibold"
              >
                {t.batches.openReviewQueue}
              </Link>
            </div>
            <div className="grid grid-cols-2 sm:grid-cols-5 gap-2">
              {funnel.map(([label, val]) => (
                <div key={label} className="rounded-lg border border-slate-200 bg-slate-50/70 px-3 py-2">
                  <div className="text-[10px] text-slate-500">{label}</div>
                  <div className="text-base font-bold text-slate-900">{val ?? '—'}</div>
                </div>
              ))}
            </div>
          </section>
        );
      })()}

      {/* Onboarding Checklist + Recent Batches */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <section className="bg-white rounded-xl border border-slate-200 p-5 shadow-xs space-y-4">
          <h2 className="text-base font-semibold text-slate-900">{t.dashboard.onboardingTitle}</h2>
          <ul className="space-y-2.5">
            {checklist.map((step, idx) => (
              <li key={idx}>
                <Link
                  to={step.to}
                  className="flex items-center gap-3 p-2.5 rounded-lg hover:bg-slate-50 border border-transparent hover:border-slate-200 transition-colors"
                >
                  {step.done ? (
                    <CheckCircle2 className="h-5 w-5 text-emerald-600 shrink-0" aria-hidden="true" />
                  ) : (
                    <Circle className="h-5 w-5 text-slate-300 shrink-0" aria-hidden="true" />
                  )}
                  <span className={`text-sm ${step.done ? 'text-slate-600 line-through' : 'text-slate-900 font-medium'}`}>
                    {step.label}
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        </section>

        <section className="bg-white rounded-xl border border-slate-200 p-5 shadow-xs space-y-4">
          <div className="flex items-center justify-between">
            <div>
              <h2 className="text-base font-semibold text-slate-900">{t.dashboard.recentBatches}</h2>
              <p className="text-[11px] text-slate-500">
                Real pipeline activity ({batches.filter((b) => b.mode !== 'demo_fixture').length} real ·{' '}
                {batches.filter((b) => b.mode === 'demo_fixture').length} demo excluded from live counts)
              </p>
            </div>
            <Link to="/batches" className="text-xs font-semibold text-indigo-600 hover:underline">
              {t.nav.batches}
            </Link>
          </div>

          {loading ? (
            <p className="text-sm text-slate-500">{t.controls.loading}</p>
          ) : batches.filter((b) => b.mode !== 'demo_fixture').length === 0 ? (
            <p className="text-sm text-slate-500">{t.dashboard.noBatchesYet}</p>
          ) : (
            <div className="space-y-2.5">
              {batches
                .filter((b) => b.mode !== 'demo_fixture')
                .slice(0, 5)
                .map((b) => {
                  const m = b.metrics || {};
                  return (
                    <Link
                      key={b.id}
                      to={`/batches?selected=${b.id}`}
                      className="block p-3 rounded-lg border border-slate-200 hover:bg-slate-50"
                    >
                      <div className="flex items-center justify-between gap-2">
                        <span className="text-sm font-semibold text-slate-900">
                          {b.mode} · {b.status}
                        </span>
                        <span className="text-xs text-slate-500">{formatDateTime(b.created_at, user.timezone)}</span>
                      </div>
                      <div className="mt-1 text-xs text-slate-600">
                        {t.daily.openings} ({b.discovered_count ?? 0}) · {t.daily.prepared} ({m.drafted ?? b.drafted_count ?? 0}) · {t.review.acceptedByProvider} ({m.sent_accepted ?? b.sent_count ?? 0})
                      </div>
                    </Link>
                  );
                })}
            </div>
          )}
        </section>
      </div>
    </div>
  );
};
