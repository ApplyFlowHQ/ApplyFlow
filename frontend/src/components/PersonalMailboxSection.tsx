import React, { useEffect, useMemo, useState } from 'react';
import { CheckCircle2, FileCheck2, Lock, Mail, RefreshCw, Send, ShieldCheck, Unplug } from 'lucide-react';
import { useI18n } from '../i18n';
import { apiRequest, ApiError } from '../lib/api';
import type { ValidationEntry } from '../lib/setupTypes';
import {
  flattenCvPdfOptions,
  type CvDocumentSummary,
  type SelfTestResponse,
} from '../lib/workflowTypes';
import type { UserSummary } from './AppShell';

interface MailboxSectionProps {
  user: UserSummary;
  onRefreshUser: () => Promise<void>;
  onFeedback?: (msg: string, isError?: boolean) => void;
}

export const PersonalMailboxSection: React.FC<MailboxSectionProps> = ({ user, onRefreshUser, onFeedback }) => {
  const { t, locale, formatDateTime } = useI18n();
  const [mailboxes, setMailboxes] = useState<any[]>([]);
  const [cvs, setCvs] = useState<CvDocumentSummary[]>([]);
  const [oauthAvail, setOauthAvail] = useState({ google: false, msGraph: false });
  const [includeCompose, setIncludeCompose] = useState(false);
  const [syncingMailboxId, setSyncingMailboxId] = useState<string | null>(null);

  // Self-test inline confirmation state
  const [selfTestMailboxId, setSelfTestMailboxId] = useState<string | null>(null);
  const [selfTestCvVersionId, setSelfTestCvVersionId] = useState<string>('');
  const [selfTestSubject, setSelfTestSubject] = useState<string>('ApplyFlow Self-Test Verification');
  const [selfTestNote, setSelfTestNote] = useState<string>('');
  const [selfTestConfirmed, setSelfTestConfirmed] = useState<boolean>(false);
  const [selfTestBusy, setSelfTestBusy] = useState<boolean>(false);
  const [selfTestResult, setSelfTestResult] = useState<{ mailboxId: string; res: SelfTestResponse } | null>(null);
  const [selfTestError, setSelfTestError] = useState<string>('');

  const [smtpForm, setSmtpForm] = useState({
    label: 'Custom TLS SMTP',
    sender_email: user.email,
    username: user.email,
    password: '',
    smtp_host: '',
    smtp_port: 587,
    smtp_use_tls: true,
    imap_host: '',
    imap_port: 993,
    imap_use_tls: true,
    inbox_poll_enabled: false,
  });
  const [smtpTest, setSmtpTest] = useState<ValidationEntry | null>(null);
  const [testingSmtp, setTestingSmtp] = useState(false);

  const cvOptions = useMemo(() => flattenCvPdfOptions(cvs), [cvs]);

  const loadMailboxes = async () => {
    const [mRes, cvRes] = await Promise.all([
      apiRequest<{
        mailboxes: any[];
        google_oauth_configured?: boolean;
        ms_graph_configured?: boolean;
      }>('/mailboxes'),
      apiRequest<{ cvs: CvDocumentSummary[] }>('/cvs').catch(() => ({ cvs: [] })),
    ]);
    setMailboxes(mRes.mailboxes || []);
    setCvs(cvRes.cvs || []);
    setOauthAvail({
      google: Boolean(mRes.google_oauth_configured),
      msGraph: Boolean(mRes.ms_graph_configured),
    });
  };

  useEffect(() => {
    loadMailboxes().catch((e) => onFeedback?.(String(e), true));
  }, []);

  const openSelfTestPanel = (mb: any) => {
    setSelfTestMailboxId(mb.id);
    setSelfTestConfirmed(false);
    setSelfTestError('');
    const firstReadyCv = cvOptions.find((o) => o.pdfReady) || cvOptions[0];
    setSelfTestCvVersionId(firstReadyCv ? firstReadyCv.versionId : '');
  };

  const handleSendSelfTest = async (mb: any) => {
    if (!selfTestConfirmed) return;
    setSelfTestBusy(true);
    setSelfTestError('');
    setSelfTestResult(null);
    try {
      const payload: Record<string, any> = {
        explicit_confirm: true,
        recipient_email: mb.sender_email,
        subject: selfTestSubject.trim() || undefined,
        note: selfTestNote.trim() || undefined,
      };
      if (selfTestCvVersionId) {
        payload.cv_version_id = selfTestCvVersionId;
      }
      const res = await apiRequest<SelfTestResponse>(`/mailboxes/${mb.id}/send-test`, {
        method: 'POST',
        body: JSON.stringify(payload),
      });
      setSelfTestResult({ mailboxId: mb.id, res });
      if (res.ok) {
        onFeedback?.(`Self-test email sent to ${res.recipient_email} (${res.provider_message_id || res.outcome}).`);
      } else {
        setSelfTestError(res.error_message || `Self-test outcome: ${res.outcome}`);
        onFeedback?.(res.error_message || 'Self-test failed', true);
      }
      await loadMailboxes();
    } catch (e2) {
      const errMsg = e2 instanceof ApiError ? e2.message : 'Self-test send failed';
      setSelfTestError(errMsg);
      onFeedback?.(errMsg, true);
    } finally {
      setSelfTestBusy(false);
    }
  };

  const startOAuth = async (path: string) => {
    try {
      const res = await apiRequest<{ authorization_url: string }>(path);
      window.location.href = res.authorization_url;
    } catch (e2) {
      onFeedback?.(e2 instanceof ApiError ? e2.message : 'OAuth provider not configured yet by installation owner.', true);
    }
  };

  const handleTestSmtp = async () => {
    setTestingSmtp(true);
    setSmtpTest(null);
    try {
      const res = await apiRequest<ValidationEntry>('/integrations/test', {
        method: 'POST',
        body: JSON.stringify({
          scope: 'mailbox_smtp',
          integration: 'user_smtp',
          smtp_host: smtpForm.smtp_host,
          smtp_port: smtpForm.smtp_port,
          smtp_username: smtpForm.username,
          smtp_password: smtpForm.password,
          smtp_use_tls: smtpForm.smtp_use_tls,
          imap_host: smtpForm.imap_host,
          imap_port: smtpForm.imap_port,
          imap_username: smtpForm.username,
          imap_password: smtpForm.password,
        }),
      });
      setSmtpTest(res);
    } catch (err) {
      setSmtpTest({ ok: false, status: 'needs_attention', message: err instanceof ApiError ? err.message : 'SMTP test failed' });
    } finally {
      setTestingSmtp(false);
    }
  };

  const handleConnectSmtp = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      await apiRequest('/mailboxes/smtp', {
        method: 'POST',
        body: JSON.stringify(smtpForm),
      });
      onFeedback?.('Personal TLS SMTP mailbox connected.');
      setSmtpForm({ ...smtpForm, password: '' });
      await loadMailboxes();
      await onRefreshUser();
    } catch (e2) {
      onFeedback?.(e2 instanceof ApiError ? e2.message : 'SMTP connection failed', true);
    }
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <Mail className="h-4 w-4 text-indigo-600" aria-hidden="true" />
          <h4 className="text-sm font-semibold text-slate-900">{t.settings.mailboxesTitle}</h4>
        </div>
        <span className="text-[11px] text-slate-500">
          App login is separate from Gmail/Outlook mailbox send permissions.
        </span>
      </div>

      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          data-testid="connect-gmail-send-only-btn"
          onClick={() => startOAuth(`/mailboxes/gmail/start?include_inbox_readonly=false&include_compose_drafts=${includeCompose}`)}
          disabled={!oauthAvail.google}
          className="px-4 py-2 rounded-lg bg-indigo-600 hover:bg-indigo-700 disabled:bg-slate-200 disabled:text-slate-500 text-white text-xs font-semibold cursor-pointer"
        >
          Connect Gmail with Google (Recommended)
        </button>
        <button
          type="button"
          data-testid="connect-gmail-with-readonly-btn"
          onClick={() => startOAuth(`/mailboxes/gmail/start?include_inbox_readonly=true&include_compose_drafts=${includeCompose}`)}
          disabled={!oauthAvail.google}
          className="px-4 py-2 rounded-lg border border-indigo-300 text-indigo-800 disabled:opacity-50 text-xs font-semibold hover:bg-indigo-50 cursor-pointer"
        >
          Optional: Add Sent-History Deduplication &amp; Reply Tracking
        </button>
        <button
          type="button"
          onClick={() => startOAuth('/mailboxes/ms-graph/start')}
          disabled={!oauthAvail.msGraph}
          className="px-4 py-2 rounded-lg border border-slate-300 text-slate-800 disabled:opacity-50 text-xs font-semibold hover:bg-slate-50 cursor-pointer"
        >
          {t.settings.connectMsGraph}
        </button>
      </div>

      <label className="inline-flex items-center gap-2 text-xs text-slate-700 cursor-pointer">
        <input
          type="checkbox"
          checked={includeCompose}
          onChange={(e) => setIncludeCompose(e.target.checked)}
        />
        <span>{t.settings.connectGmailComposeToggle}</span>
      </label>

      {!oauthAvail.google && !oauthAvail.msGraph && (
        <p className="text-xs text-slate-500">
          One-click Google / Microsoft OAuth enables automatically once the ApplyFlow installation has OAuth credentials configured (or expand Advanced Fallback SMTP/IMAP below).
        </p>
      )}

      {mailboxes.length > 0 && (
        <div className="space-y-3">
          {mailboxes.map((m) => {
            const isSelfTestOpen = selfTestMailboxId === m.id;
            const myResult = selfTestResult && selfTestResult.mailboxId === m.id ? selfTestResult.res : null;
            const health = m.connection_health || {};
            const needsReconnect = Boolean(health.needs_reconnect || m.status === 'needs_reconnect' || m.status === 'revoked');
            return (
              <div key={m.id} data-testid={`mailbox-card-${m.provider}`} className="p-3.5 rounded-xl border border-slate-200 space-y-2.5 text-xs bg-white">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="space-y-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-semibold text-slate-900">
                        {m.sender_email}
                      </span>
                      <span
                        className={`px-2 py-0.5 rounded-full text-[10px] font-semibold ${
                          needsReconnect
                            ? 'bg-amber-100 text-amber-900 border border-amber-300'
                            : 'bg-emerald-100 text-emerald-900 border border-emerald-300'
                        }`}
                      >
                        {m.provider === 'gmail_oauth'
                          ? needsReconnect
                            ? 'Gmail Needs Reconnection'
                            : 'Gmail Connected'
                          : m.status === 'connected'
                            ? 'Fallback SMTP/IMAP Active'
                            : m.status}
                      </span>
                      {m.is_primary && (
                        <span className="px-2 py-0.5 rounded-full bg-indigo-50 text-indigo-800 border border-indigo-200 text-[10px] font-semibold">
                          Primary
                        </span>
                      )}
                    </div>
                    <div className="text-[11px] text-slate-600">
                      {health.health_message || `Status: ${m.status}`} · 24h budget: {m.rolling_24h_used}/{m.rolling_24h_limit}
                    </div>
                    <div className="flex flex-wrap gap-1.5 pt-0.5">
                      {health.has_send_scope && (
                        <span className="px-2 py-0.5 rounded bg-slate-100 text-slate-700 text-[10px] font-medium">
                          Send Approved Applications
                        </span>
                      )}
                      {health.has_readonly_scope && (
                        <span className="px-2 py-0.5 rounded bg-emerald-50 text-emerald-800 border border-emerald-200 text-[10px] font-medium">
                          Sent-History Dedup &amp; Replies
                        </span>
                      )}
                      {health.has_compose_scope && (
                        <span className="px-2 py-0.5 rounded bg-indigo-50 text-indigo-800 border border-indigo-200 text-[10px] font-medium">
                          Save Gmail Drafts
                        </span>
                      )}
                      {m.provider === 'gmail_oauth' && !health.has_readonly_scope && m.status === 'connected' && oauthAvail.google && (
                        <button
                          type="button"
                          onClick={() => startOAuth(`/mailboxes/gmail/start?include_inbox_readonly=true&include_compose_drafts=${Boolean(health.has_compose_scope)}`)}
                          className="px-2 py-0.5 rounded border border-indigo-200 bg-indigo-50/60 text-indigo-700 text-[10px] font-semibold hover:bg-indigo-100 cursor-pointer"
                        >
                          + Enable Optional Sent-History Dedup
                        </button>
                      )}
                    </div>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    {m.provider === 'gmail_oauth' && needsReconnect && oauthAvail.google && (
                      <button
                        type="button"
                        data-testid="reconnect-gmail-btn"
                        onClick={() => startOAuth(`/mailboxes/gmail/start?include_inbox_readonly=${Boolean(health.has_readonly_scope)}&include_compose_drafts=${Boolean(health.has_compose_scope)}`)}
                        className="inline-flex items-center gap-1 px-2.5 py-1 rounded bg-amber-600 hover:bg-amber-700 text-white font-semibold cursor-pointer"
                      >
                        <RefreshCw className="h-3 w-3" aria-hidden="true" />
                        <span>Reconnect Gmail</span>
                      </button>
                    )}
                    <button
                      type="button"
                      data-testid="open-self-test-modal-btn"
                      onClick={() => (isSelfTestOpen ? setSelfTestMailboxId(null) : openSelfTestPanel(m))}
                      className="inline-flex items-center gap-1 px-2.5 py-1 rounded border border-emerald-300 bg-emerald-50 text-emerald-900 font-semibold hover:bg-emerald-100 cursor-pointer"
                    >
                      <Send className="h-3 w-3" aria-hidden="true" />
                      <span>{t.mailboxTest.openBtn}</span>
                    </button>

                    {m.dedup_coverage?.has_gmail_readonly_scope && (
                      <button
                        type="button"
                        disabled={syncingMailboxId === m.id}
                        onClick={async () => {
                          setSyncingMailboxId(m.id);
                          try {
                            const r = await apiRequest<{ status: string; message?: string; dedup_coverage: { warning_banner: string } }>(
                              `/mailboxes/${m.id}/sync-sent`,
                              { method: 'POST' },
                            );
                            onFeedback?.(r.status === 'ok' ? (locale === 'ar' ? 'اكتملت مزامنة البريد المرسل' : 'Sent history synced.')
                              : r.dedup_coverage.warning_banner || r.message || r.status, r.status === 'error');
                            await loadMailboxes();
                          } catch (e2) {
                            onFeedback?.(e2 instanceof ApiError ? e2.message : 'Sent sync failed', true);
                          } finally {
                            setSyncingMailboxId(null);
                          }
                        }}
                        className="inline-flex items-center gap-1 px-2.5 py-1 rounded border border-indigo-200 text-indigo-700 hover:bg-indigo-50 cursor-pointer disabled:opacity-50"
                      >
                        <RefreshCw className="h-3 w-3" aria-hidden="true" />
                        <span>{syncingMailboxId === m.id ? 'Syncing...' : t.settings.syncSentNow}</span>
                      </button>
                    )}

                    {m.inbox_poll_enabled && (
                      <button
                        type="button"
                        onClick={async () => {
                          const r = await apiRequest<{ polled_events: number }>(`/mailboxes/${m.id}/poll-now`, { method: 'POST' });
                          onFeedback?.(`Polled ${r.polled_events} new inbox events.`);
                          await loadMailboxes();
                        }}
                        className="inline-flex items-center gap-1 px-2.5 py-1 rounded border border-slate-200 hover:bg-slate-50 cursor-pointer"
                      >
                        <RefreshCw className="h-3 w-3" aria-hidden="true" />
                        <span>{t.settings.pollNow}</span>
                      </button>
                    )}

                    <button
                      type="button"
                      onClick={async () => {
                        await apiRequest(`/mailboxes/${m.id}/disconnect`, { method: 'POST' });
                        await loadMailboxes();
                        await onRefreshUser();
                        onFeedback?.('Mailbox disconnected.');
                      }}
                      className="inline-flex items-center gap-1 px-2.5 py-1 rounded border border-rose-200 text-rose-700 hover:bg-rose-50 cursor-pointer"
                    >
                      <Unplug className="h-3 w-3" aria-hidden="true" />
                      <span>{t.settings.disconnect}</span>
                    </button>
                  </div>
                </div>

                {/* Inline Self-Test Confirmation Panel */}
                {isSelfTestOpen && (
                  <div
                    role="region"
                    aria-label={t.mailboxTest.title}
                    data-testid={`self-test-panel-${m.id}`}
                    className="rounded-xl border border-emerald-300 bg-emerald-50/60 p-3.5 space-y-3 text-slate-900"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <div className="font-bold text-emerald-950 flex items-center gap-1.5">
                        <ShieldCheck className="h-4 w-4 text-emerald-700" aria-hidden="true" />
                        <span>{t.mailboxTest.title}</span>
                      </div>
                      <button
                        type="button"
                        onClick={() => setSelfTestMailboxId(null)}
                        className="text-slate-600 hover:underline font-medium cursor-pointer"
                      >
                        {t.controls.cancel}
                      </button>
                    </div>

                    <div className="grid grid-cols-1 md:grid-cols-2 gap-2.5">
                      <div>
                        <label htmlFor={`self-test-target-${m.id}`} className="block text-[11px] font-semibold text-slate-700 mb-1">
                          {t.mailboxTest.targetLockedLabel}
                        </label>
                        <div className="flex items-center gap-1.5 rounded-lg border border-slate-300 bg-slate-100 px-2.5 py-1.5 text-slate-800 font-mono">
                          <Lock className="h-3.5 w-3.5 text-slate-500 shrink-0" aria-hidden="true" />
                          <input
                            id={`self-test-target-${m.id}`}
                            type="email"
                            readOnly
                            value={m.sender_email}
                            className="w-full bg-transparent focus:outline-none"
                          />
                        </div>
                      </div>

                      <div>
                        <label htmlFor={`self-test-cv-${m.id}`} className="block text-[11px] font-semibold text-slate-700 mb-1">
                          {t.mailboxTest.cvAttachmentLabel}
                        </label>
                        <select
                          id={`self-test-cv-${m.id}`}
                          data-testid="self-test-cv-select"
                          value={selfTestCvVersionId}
                          onChange={(e) => setSelfTestCvVersionId(e.target.value)}
                          className="w-full rounded-lg border border-slate-300 bg-white px-2.5 py-1.5 text-slate-900"
                        >
                          <option value="">{t.mailboxTest.noAttachmentOption}</option>
                          {cvOptions.map((opt) => (
                            <option key={opt.versionId} value={opt.versionId}>
                              {opt.label}
                            </option>
                          ))}
                        </select>
                      </div>

                      <div>
                        <label htmlFor={`self-test-subject-${m.id}`} className="block text-[11px] font-semibold text-slate-700 mb-1">
                          {t.mailboxTest.subjectLabel}
                        </label>
                        <input
                          id={`self-test-subject-${m.id}`}
                          type="text"
                          value={selfTestSubject}
                          onChange={(e) => setSelfTestSubject(e.target.value)}
                          className="w-full rounded-lg border border-slate-300 bg-white px-2.5 py-1.5 text-slate-900"
                        />
                      </div>

                      <div>
                        <label htmlFor={`self-test-note-${m.id}`} className="block text-[11px] font-semibold text-slate-700 mb-1">
                          {t.mailboxTest.noteLabel}
                        </label>
                        <input
                          id={`self-test-note-${m.id}`}
                          type="text"
                          value={selfTestNote}
                          onChange={(e) => setSelfTestNote(e.target.value)}
                          placeholder="Optional test note..."
                          className="w-full rounded-lg border border-slate-300 bg-white px-2.5 py-1.5 text-slate-900"
                        />
                      </div>
                    </div>

                    <label className="flex items-start gap-2 cursor-pointer font-medium text-emerald-950">
                      <input
                        type="checkbox"
                        data-testid="confirm-self-test-checkbox"
                        checked={selfTestConfirmed}
                        onChange={(e) => setSelfTestConfirmed(e.target.checked)}
                        className="mt-0.5"
                      />
                      <span>{t.mailboxTest.confirmCheckbox}</span>
                    </label>

                    {selfTestError && (
                      <div role="alert" className="p-2.5 rounded-lg bg-rose-50 border border-rose-200 text-rose-800">
                        {selfTestError}
                      </div>
                    )}

                    <div className="flex flex-wrap items-center gap-2">
                      <button
                        type="button"
                        disabled={!selfTestConfirmed || selfTestBusy}
                        data-testid="send-self-test-submit-btn"
                        onClick={() => handleSendSelfTest(m)}
                        className="inline-flex items-center gap-1.5 px-3.5 py-1.5 rounded-lg bg-emerald-600 hover:bg-emerald-700 text-white font-semibold cursor-pointer disabled:opacity-40"
                      >
                        <Send className="h-3.5 w-3.5" aria-hidden="true" />
                        <span>{selfTestBusy ? t.mailboxTest.sendingBtn : t.mailboxTest.confirmSendBtn}</span>
                      </button>
                    </div>
                  </div>
                )}

                {/* Self-Test Delivery Receipt */}
                {myResult && (
                  <div
                    role="status"
                    data-testid="self-test-result"
                    className={`rounded-xl border p-3 space-y-1.5 ${
                      myResult.ok
                        ? 'bg-emerald-50 border-emerald-300 text-emerald-950'
                        : 'bg-rose-50 border-rose-300 text-rose-900'
                    }`}
                  >
                    <div className="font-bold flex items-center gap-1.5">
                      <CheckCircle2 className="h-4 w-4 text-emerald-700" aria-hidden="true" />
                      <span>
                        Self-Test Result ({myResult.outcome}) — Recipient: {myResult.recipient_email}
                      </span>
                    </div>
                    <div className="flex flex-wrap gap-x-4 gap-y-1 text-[11px]">
                      {myResult.provider_message_id && (
                        <span>
                          <strong>{t.mailboxTest.resultProviderMsgId}:</strong>{' '}
                          <code className="font-mono">{myResult.provider_message_id}</code>
                        </span>
                      )}
                      {myResult.attachment_filename && (
                        <span className="inline-flex items-center gap-1">
                          <FileCheck2 className="h-3.5 w-3.5 text-emerald-700" aria-hidden="true" />
                          <strong>{t.mailboxTest.resultAttachment}:</strong> {myResult.attachment_filename}
                          {myResult.attachment_sha256 && (
                            <code className="font-mono text-[10px]">({myResult.attachment_sha256.slice(0, 12)}…)</code>
                          )}
                        </span>
                      )}
                      {myResult.reconciled_in_sent !== undefined && (
                        <span>
                          <strong>{t.mailboxTest.resultReconciled}:</strong>{' '}
                          {myResult.reconciled_in_sent ? t.mailboxTest.resultReconciledYes : t.mailboxTest.resultReconciledNo}
                        </span>
                      )}
                    </div>
                  </div>
                )}

                {m.dedup_coverage && (
                  <div
                    className={`p-2 rounded border text-[11px] ${
                      m.dedup_coverage.coverage_mode === 'applyflow_ledger_plus_gmail_sent'
                        ? 'bg-emerald-50 border-emerald-200 text-emerald-900'
                        : 'bg-amber-50 border-amber-200 text-amber-900'
                    }`}
                  >
                    <strong>{locale === 'ar' ? 'فحص التكرار' : 'Duplicate checking'}</strong>
                    <p>{m.dedup_coverage.warning_banner || (locale === 'ar' ? 'اكتمل فحص البريد المرسل' : 'Sent history scan completed.')}</p>
                    <p>{locale === 'ar' ? 'آخر مزامنة مكتملة' : 'Last completed sync'} · {formatDateTime(m.dedup_coverage.sent_last_synced_at)}</p>
                    <p>{locale === 'ar' ? 'تواريخ الرسائل المفحوصة' : 'Dates of inspected messages'} · {formatDateTime(m.dedup_coverage.sent_earliest_covered_at)} — {formatDateTime(m.dedup_coverage.sent_latest_covered_at)}</p>
                    {m.dedup_coverage.checked_through_at && <p>{locale === 'ar' ? 'تم الفحص حتى' : 'Checked through'} · {formatDateTime(m.dedup_coverage.checked_through_at)}</p>}
                    <p>{m.dedup_coverage.sent_records_count} {locale === 'ar' ? 'رسالة محفوظة' : 'messages cached'} · {m.dedup_coverage.prior_contact_count}
                      {' '}{locale === 'ar' ? 'عنوانًا سبق التواصل معه' : 'previously contacted addresses'} · {m.dedup_coverage.suppressed_job_recipients_count}
                      {' '}{locale === 'ar' ? 'له إشارات طلب توظيف' : 'with application signals'}</p>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {/* Advanced Fallback: Standard TLS SMTP / IMAP (Collapsed by default so Gmail users never need SMTP/App Passwords) */}
      <details className="pt-3 border-t border-slate-100 space-y-3">
        <summary className="text-xs font-semibold text-slate-700 cursor-pointer hover:text-slate-900">
          Advanced Fallback: {t.settings.smtpTitle} (Optional — not needed for Gmail OAuth)
        </summary>
        <div className="pt-3 space-y-3">
          <p className="text-xs text-slate-500">{t.settings.smtpGmailWarning}</p>
          <form onSubmit={handleConnectSmtp} className="grid grid-cols-1 md:grid-cols-3 gap-3">
          <input
            type="email"
            required
            placeholder="Sender Email"
            value={smtpForm.sender_email}
            onChange={(e) => setSmtpForm({ ...smtpForm, sender_email: e.target.value })}
            className="rounded-lg border border-slate-300 px-3 py-2 text-xs"
          />
          <input
            type="text"
            required
            placeholder="SMTP Host (e.g. mail.company.com)"
            value={smtpForm.smtp_host}
            onChange={(e) => setSmtpForm({ ...smtpForm, smtp_host: e.target.value })}
            className="rounded-lg border border-slate-300 px-3 py-2 text-xs"
          />
          <input
            type="number"
            required
            placeholder="SMTP Port (587 or 465)"
            value={smtpForm.smtp_port}
            onChange={(e) => setSmtpForm({ ...smtpForm, smtp_port: Number(e.target.value) })}
            className="rounded-lg border border-slate-300 px-3 py-2 text-xs"
          />
          <input
            type="text"
            placeholder="Username"
            value={smtpForm.username}
            onChange={(e) => setSmtpForm({ ...smtpForm, username: e.target.value })}
            className="rounded-lg border border-slate-300 px-3 py-2 text-xs"
          />
          <input
            type="password"
            required
            placeholder="SMTP / App Password"
            value={smtpForm.password}
            onChange={(e) => setSmtpForm({ ...smtpForm, password: e.target.value })}
            className="rounded-lg border border-slate-300 px-3 py-2 text-xs"
          />
          <input
            type="text"
            placeholder="Optional TLS IMAP Host (port 993)"
            value={smtpForm.imap_host}
            onChange={(e) =>
              setSmtpForm({ ...smtpForm, imap_host: e.target.value, inbox_poll_enabled: Boolean(e.target.value) })
            }
            className="rounded-lg border border-slate-300 px-3 py-2 text-xs"
          />
          {smtpTest && (
            <div
              className={`md:col-span-3 rounded-lg border p-2.5 text-xs ${
                smtpTest.ok ? 'border-emerald-200 bg-emerald-50 text-emerald-800' : 'border-rose-200 bg-rose-50 text-rose-800'
              }`}
            >
              {smtpTest.message}
            </div>
          )}
          <div className="md:col-span-3 flex flex-wrap gap-2">
            <button
              type="button"
              disabled={testingSmtp || !smtpForm.smtp_host}
              onClick={handleTestSmtp}
              className="px-3 py-2 rounded-lg border border-slate-300 text-xs font-medium hover:bg-slate-50 cursor-pointer disabled:opacity-50"
            >
              {testingSmtp ? t.setup.testingBtn : t.setup.testBtn}
            </button>
            <button type="submit" className="px-4 py-2 rounded-lg bg-slate-900 text-white text-xs font-semibold cursor-pointer">
              {t.settings.connectSmtpBtn}
            </button>
          </div>
          </form>
        </div>
      </details>
    </div>
  );
};
