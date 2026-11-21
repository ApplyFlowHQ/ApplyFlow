import React, { useEffect, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { KeyRound, Shield, Sparkles, Trash2 } from 'lucide-react';
import { useI18n } from '../i18n';
import { apiRequest, ApiError } from '../lib/api';
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

export const SettingsPage: React.FC<{ user: UserSummary; onRefreshUser: () => Promise<void>; onLoggedOut: () => void }> = ({
  user,
  onRefreshUser,
  onLoggedOut,
}) => {
  const { t } = useI18n();
  const [searchParams, setSearchParams] = useSearchParams();
  const [setupStatus, setSetupStatus] = useState<SetupStatusResponse | null>(null);
  const [operatorData, setOperatorData] = useState<any>(null);
  const [msg, setMsg] = useState('');
  const [err, setErr] = useState('');

  useEffect(() => {
    const mbStatus = searchParams.get('mailbox');
    const mbErr = searchParams.get('mailbox_error');
    if (mbStatus === 'gmail_connected') {
      setMsg('Gmail OAuth mailbox connected and active.');
      setErr('');
      const next = new URLSearchParams(searchParams);
      next.delete('mailbox');
      setSearchParams(next, { replace: true });
    } else if (mbErr) {
      setErr(mbErr);
      setMsg('');
    }
  }, [searchParams, setSearchParams]);

  const handleFeedback = (text: string, isError = false) => {
    if (isError) {
      setErr(text);
      setMsg('');
    } else {
      setMsg(text);
      setErr('');
    }
  };

  const loadAll = async () => {
    const [sRes, opRes] = await Promise.all([
      apiRequest<SetupStatusResponse>('/setup/status'),
      user.is_operator ? apiRequest('/operator/overview') : Promise.resolve(null),
    ]);
    setSetupStatus(sRes);
    setOperatorData(opRes);
  };

  useEffect(() => {
    loadAll().catch((e) => setErr(e instanceof ApiError ? e.message : String(e)));
  }, [user.id, user.is_operator]);

  const handleDeleteAccount = async () => {
    if (!window.confirm('Delete your ApplyFlow account, revoke connected mailboxes, and remove stored CV files permanently?')) {
      return;
    }
    await apiRequest('/account', { method: 'DELETE' });
    onLoggedOut();
  };

  return (
    <div className="space-y-8">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-bold text-slate-900">{t.settings.title}</h1>
        {user.can_manage_installation && (
          <Link
            to="/setup"
            className="inline-flex items-center gap-1.5 px-4 py-2 rounded-lg bg-indigo-600 hover:bg-indigo-700 text-white text-xs font-semibold"
          >
            <Sparkles className="h-3.5 w-3.5" aria-hidden="true" />
            <span>{t.settings.reopenWizard}</span>
          </Link>
        )}
      </div>

      {msg && <div role="status" className="p-3 rounded-lg bg-emerald-50 border border-emerald-200 text-emerald-800 text-sm">{msg}</div>}
      {err && <div role="alert" className="p-3 rounded-lg bg-rose-50 border border-rose-200 text-rose-800 text-sm">{err}</div>}

      {/* Connected Mailboxes (Personal to each user) */}
      <section className="bg-white rounded-xl border border-slate-200 p-5 shadow-xs space-y-4">
        <PersonalMailboxSection user={user} onRefreshUser={onRefreshUser} onFeedback={handleFeedback} />
      </section>

      {/* Personal AI & Search Overrides (Tenant-Isolated) */}
      {setupStatus && (
        <section className="bg-white rounded-xl border border-slate-200 p-5 shadow-xs space-y-6">
          <div className="flex items-center justify-between gap-2 border-b border-slate-100 pb-3">
            <div className="flex items-center gap-2">
              <KeyRound className="h-5 w-5 text-indigo-600" aria-hidden="true" />
              <h2 className="text-base font-semibold text-slate-900">{t.settings.personalIntegrationsTitle}</h2>
            </div>
            <span className="text-xs text-slate-500">Private to your account</span>
          </div>
          <AiConfigPanel scope="user" statusData={setupStatus} onUpdated={setSetupStatus} onFeedback={handleFeedback} />
          <div className="border-t border-slate-100 pt-5">
            <SearchConfigPanel scope="user" statusData={setupStatus} onUpdated={setSetupStatus} onFeedback={handleFeedback} />
          </div>
        </section>
      )}

      {/* Installation-Wide Integrations (Installation Owner Only) */}
      {setupStatus?.can_manage_installation && setupStatus.installation && (
        <section className="bg-white rounded-xl border border-slate-200 p-5 shadow-xs space-y-6">
          <div className="flex flex-wrap items-start justify-between gap-3 border-b border-slate-100 pb-3">
            <div>
              <h2 className="text-base font-semibold text-slate-900">{t.settings.installationIntegrationsTitle}</h2>
              <p className="text-xs text-slate-500">
                Change, test, replace, or disconnect installation-wide providers. Empty secret inputs always preserve saved encrypted keys.
              </p>
            </div>
            <div className="flex flex-wrap items-start gap-2">
              <div className="pt-1">
                <StatusBadge status={setupStatus.readiness?.live_sending.status || 'disabled'} />
              </div>
              <LiveSendingConfirmPanel
                compact
                enabled={Boolean(setupStatus.installation.live_sending_enabled)}
                lockedByEnv={Boolean(setupStatus.installation.lock_env_config)}
                onToggle={async (target) => {
                  const next = await apiRequest<SetupStatusResponse>('/setup/live-sending', {
                    method: 'POST',
                    body: JSON.stringify({ enabled: target }),
                  });
                  setSetupStatus(next);
                  await onRefreshUser();
                  handleFeedback(
                    target
                      ? 'Live external email sending enabled for approved applications.'
                      : 'Live external email sending disabled (kill switch engaged).',
                  );
                }}
              />
            </div>
          </div>

          <AiConfigPanel scope="installation" statusData={setupStatus} onUpdated={setSetupStatus} onFeedback={handleFeedback} />
          <div className="border-t border-slate-100 pt-5">
            <SearchConfigPanel scope="installation" statusData={setupStatus} onUpdated={setSetupStatus} onFeedback={handleFeedback} />
          </div>
          <div className="border-t border-slate-100 pt-5">
            <OAuthAppsPanel statusData={setupStatus} onUpdated={setSetupStatus} onFeedback={handleFeedback} />
          </div>
          <div className="border-t border-slate-100 pt-5">
            <TransactionalEmailPanel statusData={setupStatus} onUpdated={setSetupStatus} onFeedback={handleFeedback} />
          </div>
        </section>
      )}

      {/* Privacy & Account Controls */}
      <section className="bg-white rounded-xl border border-slate-200 p-5 shadow-xs space-y-4">
        <div className="flex items-center gap-2">
          <Shield className="h-5 w-5 text-indigo-600" aria-hidden="true" />
          <h2 className="text-base font-semibold text-slate-900">{t.settings.privacyTitle}</h2>
        </div>
        <p className="text-xs text-slate-600">{t.settings.privacyDisclosure}</p>
        <div className="flex flex-wrap gap-3">
          <a
            href="/api/v1/account/export"
            className="px-4 py-2 rounded-lg border border-slate-300 text-xs font-semibold text-slate-800 hover:bg-slate-50"
          >
            {t.settings.exportAccountBtn}
          </a>
          <button
            type="button"
            onClick={handleDeleteAccount}
            className="inline-flex items-center gap-1.5 px-4 py-2 rounded-lg bg-rose-600 hover:bg-rose-700 text-white text-xs font-semibold cursor-pointer"
          >
            <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
            <span>{t.settings.deleteAccountBtn}</span>
          </button>
        </div>
      </section>

      {/* Operator Admin Panel */}
      {user.is_operator && operatorData && (
        <section className="bg-white rounded-xl border border-slate-200 p-5 shadow-xs space-y-3">
          <h2 className="text-base font-semibold text-slate-900">{t.settings.operatorTitle}</h2>
          <div className="text-xs text-slate-600">
            Queued outbox: {operatorData.queue.queued_outbox} · Uncertain sends: {operatorData.queue.uncertain_outbox} · Live toggle: {String(operatorData.live_sending_enabled)}
          </div>
          <div className="space-y-1.5 text-xs">
            {(operatorData.users || []).map((u: any) => (
              <div key={u.id} className="flex items-center justify-between p-2 rounded border border-slate-200">
                <span>{u.email} {u.is_suspended ? '(SUSPENDED)' : ''}</span>
                <button
                  type="button"
                  onClick={async () => {
                    await apiRequest(`/operator/users/${u.id}/${u.is_suspended ? 'unsuspend' : 'suspend'}`, { method: 'POST' });
                    await loadAll();
                  }}
                  className="px-2 py-1 rounded border border-slate-200 cursor-pointer"
                >
                  {u.is_suspended ? 'Unsuspend' : 'Suspend'}
                </button>
              </div>
            ))}
          </div>
        </section>
      )}
    </div>
  );
};
