import React, { useMemo, useState } from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  Clock,
  Download,
  Edit3,
  ExternalLink,
  FileCheck2,
  MailPlus,
  RefreshCw,
  RotateCcw,
  Send,
  ShieldAlert,
  ShieldCheck,
  Sparkles,
  XCircle,
} from 'lucide-react';
import { useI18n } from '../i18n';
import { apiRequest, ApiError } from '../lib/api';
import {
  canApproveItem,
  canSendApprovedItem,
  flattenCvPdfOptions,
  getApplicationQueueState,
  getCvPdfDownloadUrl,
  isApprovalStale,
  isApprovedLocked,
  isQueuedInOutbox,
  isRejectedOrBlocked,
  isSendingInOutbox,
  isUncertainSend,
  needsHumanReviewOverride,
  reviewedDraftSelection,
  type DraftApprovalResponse,
  type CancelOutboxResponse,
  type CvDocumentSummary,
  type DraftApplicationItem,
  type ReconcileOutboxResponse,
  type RetrySendResponse,
  type RevalidateResponse,
  type ReviseOrRegenerateResponse,
} from '../lib/workflowTypes';

const TIER_META: Record<string, { label: string; cls: string }> = {
  tier_1_official: { label: 'Tier 1 Official', cls: 'bg-emerald-100 text-emerald-800 border-emerald-200' },
  tier_2_authorized: { label: 'Tier 2 Authorized', cls: 'bg-indigo-100 text-indigo-800 border-indigo-200' },
  tier_3_third_party_claim: { label: 'Tier 3 Third-Party Claim', cls: 'bg-amber-100 text-amber-900 border-amber-200' },
  tier_4_non_email_or_closed: { label: 'Tier 4 Non-Email / Closed', cls: 'bg-slate-100 text-slate-700 border-slate-200' },
  tier_5_unrelated_or_prohibited: { label: 'Tier 5 Prohibited / Excluded', cls: 'bg-rose-100 text-rose-800 border-rose-200' },
};

function getDnsBadgeClass(dnsStatus?: string | null): string {
  const s = (dnsStatus || '').toLowerCase();
  if (s === 'valid' || s === 'ok' || s === 'mx_valid' || s === 'passed') {
    return 'bg-emerald-50 text-emerald-800 border-emerald-200';
  }
  if (s === 'invalid' || s === 'failed' || s === 'no_mx' || s === 'nxdomain') {
    return 'bg-rose-50 text-rose-800 border-rose-200';
  }
  return 'bg-slate-100 text-slate-600 border-slate-200';
}

function getTransportBadgeClass(status?: string | null): string {
  switch (status) {
    case 'sent':
    case 'provider_accepted':
      return 'bg-emerald-100 text-emerald-900 border-emerald-300';
    case 'queued':
    case 'sending':
      return 'bg-indigo-100 text-indigo-800 border-indigo-200';
    case 'failed':
      return 'bg-rose-100 text-rose-800 border-rose-200';
    case 'send_uncertain':
      return 'bg-purple-100 text-purple-900 border-purple-200';
    case 'skipped':
    case 'cancelled':
    case 'blocked':
      return 'bg-slate-100 text-slate-700 border-slate-300';
    default:
      return 'bg-slate-100 text-slate-700 border-slate-200';
  }
}

export interface PreparedApplicationCardProps {
  item: DraftApplicationItem;
  index?: number;
  cvs: CvDocumentSummary[];
  liveEnabled: boolean;
  defaultExpanded?: boolean;
  showGmailDraftButton?: boolean;
  onChanged: () => Promise<void>;
  onFeedback: (msg: string, isError?: boolean) => void;
  onRequestSendConfirm: (item: DraftApplicationItem) => void;
}

export const PreparedApplicationCard: React.FC<PreparedApplicationCardProps> = ({
  item,
  index,
  cvs,
  liveEnabled,
  defaultExpanded = false,
  showGmailDraftButton = false,
  onChanged,
  onFeedback,
  onRequestSendConfirm,
}) => {
  const { t, formatDateTime } = useI18n();
  const [expanded, setExpanded] = useState<boolean>(defaultExpanded);
  const [isEditing, setIsEditing] = useState<boolean>(false);
  const [overrideReason, setOverrideReason] = useState('');
  const [showOverride, setShowOverride] = useState(false);
  const [editSubject, setEditSubject] = useState<string>('');
  const [editRecipient, setEditRecipient] = useState<string>('');
  const [editCc, setEditCc] = useState<string>('');
  const [editCvVersionId, setEditCvVersionId] = useState<string>('');
  const [editBody, setEditBody] = useState<string>('');
  const [editNotes, setEditNotes] = useState<string>('');

  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [localNotice, setLocalNotice] = useState<{ text: string; isError?: boolean } | null>(null);

  const cvOptions = useMemo(
    () => flattenCvPdfOptions(cvs, item.selected_cv_version_id),
    [cvs, item.selected_cv_version_id],
  );
  const pdfDownloadUrl = useMemo(() => getCvPdfDownloadUrl(item, cvs), [item, cvs]);

  const approvedLocked = isApprovedLocked(item);
  const approvalStale = isApprovalStale(item);
  const blockedOrRejected = isRejectedOrBlocked(item);
  const needsOverride = needsHumanReviewOverride(item);
  const approvable = canApproveItem(item);
  const sendable = canSendApprovedItem(item);
  const isQueued = isQueuedInOutbox(item);
  const isSending = isSendingInOutbox(item);
  const isUncertain = isUncertainSend(item);
  const queueState = getApplicationQueueState(item);
  const canRetry = !blockedOrRejected && !isUncertain && queueState === 'failed';
  const isAccepted = item.transport_status === 'sent' || item.transport_status === 'provider_accepted' || item.outbox_status === 'sent';

  const queueLabels = {
    sent: t.review.acceptedByProvider, sending: t.review.outboxSendingLabel, queued: t.review.outboxQueuedLabel,
    uncertain: t.workflow.uncertain, failed: t.workflow.failed, blocked: t.workflow.blocked,
    ready: needsOverride ? t.workflow.needsReview : t.workflow.ready, approved: t.workflow.approved,
  };
  const cancelledSend = item.transport_status === 'cancelled' || item.outbox_status === 'cancelled';

  const tierInfo = item.source_verification_tier
    ? TIER_META[item.source_verification_tier] || {
        label: item.source_verification_tier,
        cls: 'bg-slate-100 text-slate-700 border-slate-200',
      }
    : null;

  const source = item.source_url || item.evidence_url;
  const excerpt = item.evidence_excerpt || item.evidence_quote;
  const editable = !isAccepted && !isSending && !isQueued && !isUncertain && !blockedOrRejected;
  const locationDisplay = item.location || [item.city, item.country_code].filter(Boolean).join(', ') || item.location_raw || '';

  const report = (text: string, isError = false) => {
    setLocalNotice({ text, isError });
    onFeedback(text, isError);
  };

  const openEditor = () => {
    setEditSubject(item.subject || '');
    setEditRecipient(item.recipient_email || '');
    setEditCc((item.cc_emails || []).join(', '));
    setEditCvVersionId(item.selected_cv_version_id || '');
    setEditBody(item.body_text || '');
    setEditNotes(item.review_notes || '');
    setIsEditing(true);
    setExpanded(true);
  };

  const handleSaveRevise = async () => {
    setBusyAction('revise');
    setLocalNotice(null);
    try {
      const ccList = editCc
        .split(',')
        .map((s) => s.trim())
        .filter(Boolean);
      const payload: Record<string, any> = {
        subject: editSubject,
        body_text: editBody,
        recipient_email: editRecipient.trim(),
        cc_emails: ccList,
        review_notes: editNotes,
      };
      if (editCvVersionId) {
        payload.selected_cv_version_id = editCvVersionId;
      }
      const res = await apiRequest<ReviseOrRegenerateResponse>(`/drafts/${item.id}/revise`, {
        method: 'POST',
        body: JSON.stringify(payload),
      });
      setIsEditing(false);
      await onChanged();
      report(
        res.approval_invalidated
          ? t.daily.saveAgain
          : t.daily.saveOk,
      );
    } catch (e2) {
      report(e2 instanceof ApiError ? e2.message : 'Failed to revise draft', true);
    } finally {
      setBusyAction(null);
    }
  };

  async function applyReviewDecision(action: 'reject' | 'override') {
    setBusyAction(action);
    try {
      if (action === 'reject') {
        await apiRequest(`/drafts/${item.id}/reject`, { method: 'POST' });
      } else {
        await apiRequest(`/applications/${item.id}/duplicate-override`, {
          method: 'POST', body: JSON.stringify({ reason: overrideReason.trim() }),
        });
      }
      setShowOverride(false);
      await onChanged();
      report(action === 'reject' ? t.daily.rejected : t.daily.overrideSaved);
    } catch (error) {
      report(error instanceof Error ? error.message : t.daily.saveError, true);
    } finally {
      setBusyAction(null);
    }
  }

  const handleRevalidate = async () => {
    setBusyAction('revalidate');
    setLocalNotice(null);
    try {
      const res = await apiRequest<RevalidateResponse>(`/drafts/${item.id}/revalidate`, {
        method: 'POST',
        body: JSON.stringify({}),
      });
      await onChanged();
      const summaryParts = [
        res.outcome_bucket ? `Bucket: ${res.outcome_bucket}` : null,
        res.approval_state ? `Approval: ${res.approval_state}` : null,
        res.dns_status ? `DNS: ${res.dns_status}` : null,
        res.review_reasons?.length ? `Reasons: ${res.review_reasons.join('; ')}` : null,
        res.skip_reason ? `Note: ${res.skip_reason}` : null,
      ].filter(Boolean);
      report(`Revalidated (${item.role_title || item.id.slice(0, 8)}): ${summaryParts.join(' · ') || 'Complete'}`);
    } catch (e2) {
      report(e2 instanceof ApiError ? e2.message : 'Revalidation failed', true);
    } finally {
      setBusyAction(null);
    }
  };

  const handleRegenerate = async () => {
    setBusyAction('regenerate');
    setLocalNotice(null);
    try {
      const res = await apiRequest<ReviseOrRegenerateResponse>(`/drafts/${item.id}/regenerate`, {
        method: 'POST',
        body: JSON.stringify({}),
      });
      setIsEditing(false);
      setExpanded(true);
      await onChanged();
      report(
        res.approval_invalidated
          ? 'Grounded draft regenerated — prior approval invalidated; review and re-approve before sending.'
          : 'Grounded draft regenerated strictly from verified CV facts and posting evidence.',
      );
    } catch (e2) {
      report(e2 instanceof ApiError ? e2.message : 'Grounded draft regeneration failed', true);
    } finally {
      setBusyAction(null);
    }
  };

  const handleApproveClick = async () => {
    if (!approvable) return;
    setBusyAction('approve');
    setLocalNotice(null);
    try {
      const res = await apiRequest<DraftApprovalResponse>('/drafts/approve', {
        method: 'POST',
        body: JSON.stringify({
          ...reviewedDraftSelection([item]),
          review_notes: item.review_notes || '',
          allow_third_party_override: needsOverride,
        }),
      });
      await onChanged();
      if (res.failed?.length > 0) {
        report(res.failed[0].error || 'Approval rejected', true);
      } else {
        report(`${t.workflow.approved} · ${item.role_title || item.company_name || item.id.slice(0, 8)}`);
      }
    } catch (e2) {
      report(e2 instanceof ApiError ? e2.message : 'Approval failed', true);
    } finally {
      setBusyAction(null);
    }
  };

  const handleRetryFailedSend = async () => {
    if (!canRetry) return;
    setBusyAction('retry');
    setLocalNotice(null);
    try {
      const res = await apiRequest<RetrySendResponse>(`/applications/${item.id}/retry-send`, {
        method: 'POST',
        body: JSON.stringify({ bypass_send_window: false }),
      });
      await onChanged();
      if (res.ok) {
        report(
          res.outcome === 'sent'
            ? 'Application transmitted and accepted by mail provider.'
            : `Queue action completed (${res.outcome || 'queued'}).`,
        );
      } else {
        report(res.error || `Retry outcome: ${res.outcome || 'failed'}`, true);
      }
    } catch (e2) {
      report(e2 instanceof ApiError ? e2.message : 'Retry failed send error', true);
    } finally {
      setBusyAction(null);
    }
  };

  const handleCancelQueuedOutbox = async () => {
    if (!isQueued) return;
    setBusyAction('cancel_outbox');
    setLocalNotice(null);
    try {
      const res = await apiRequest<CancelOutboxResponse>(`/applications/${item.id}/cancel-outbox`, {
        method: 'POST',
        body: JSON.stringify({ reason: 'Cancelled queued send from dashboard' }),
      });
      await onChanged();
      if (res.ok) {
        report('Queued outbox entry cancelled and recipient reservation released. Application returned to Approved state.');
      }
    } catch (e2) {
      report(e2 instanceof ApiError ? e2.message : 'Unable to cancel queued outbox entry', true);
    } finally {
      setBusyAction(null);
    }
  };

  const handleReconcileUncertainOutbox = async () => {
    if (!isUncertain) return;
    setBusyAction('reconcile_outbox');
    setLocalNotice(null);
    try {
      const res = await apiRequest<ReconcileOutboxResponse>(`/applications/${item.id}/reconcile-outbox`, {
        method: 'POST',
      });
      await onChanged();
      if (res.reconciled_outcome === 'sent') {
        report([t.review.reconciledSent, res.provider_message_id].filter(Boolean).join(' · '));
      } else {
        report(t.review.reconciledUncertain, true);
      }
    } catch (e2) {
      report(e2 instanceof ApiError ? e2.message : 'Unable to reconcile uncertain send', true);
    } finally {
      setBusyAction(null);
    }
  };

  const handleSaveGmailDraft = async () => {
    setBusyAction('gmail_draft');
    setLocalNotice(null);
    try {
      const res = await apiRequest<{ gmail_draft_id: string }>(`/applications/${item.id}/create-gmail-draft`, {
        method: 'POST',
      });
      await onChanged();
      report(`Saved Gmail Draft (${res.gmail_draft_id}) without sending.`);
    } catch (e2) {
      report(e2 instanceof ApiError ? e2.message : 'Unable to create Gmail Draft', true);
    } finally {
      setBusyAction(null);
    }
  };

  const busy = busyAction !== null;

  return (
    <article
      data-testid={`prepared-app-card-${item.id}`}
      className={`bg-white rounded-xl border p-4 shadow-xs space-y-3 text-xs ${
        blockedOrRejected
          ? 'border-slate-300 bg-slate-50/40'
          : isAccepted
            ? 'border-emerald-300'
            : approvedLocked
              ? 'border-emerald-200'
              : needsOverride
                ? 'border-amber-200'
                : 'border-slate-200'
      }`}
    >
      {/* Top Header & Actions */}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="space-y-1.5 min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-1.5">
            {index !== undefined && (
              <span className="text-[11px] font-mono text-slate-500 font-semibold">#{index + 1}</span>
            )}
            <h3 className="text-sm font-bold text-slate-900">
              {item.role_title || 'Unverified Role'} — {item.company_name || 'Unverified Employer'}
            </h3>
            {item.import_origin === 'gemini_zip' && (
              <span className="px-2 py-0.5 rounded-full bg-indigo-50 text-indigo-800 border border-indigo-200 font-semibold text-[10px]">
                {t.batches.geminiZipBadge}
              </span>
            )}
            {tierInfo && (
              <span className={`px-2 py-0.5 rounded-full border font-semibold text-[10px] ${tierInfo.cls}`}>
                {tierInfo.label}
              </span>
            )}
            {item.dns_check_status && (
              <span
                className={`px-2 py-0.5 rounded-full border font-medium text-[10px] ${getDnsBadgeClass(item.dns_check_status)}`}
              >
                {t.review.dnsStatus}: {item.dns_check_status}
              </span>
            )}
            <span
              className={`px-2 py-0.5 rounded-full font-semibold text-[10px] ${
                blockedOrRejected
                  ? 'bg-rose-100 text-rose-800'
                  : item.approval_state === 'needs_human_review'
                    ? 'bg-amber-100 text-amber-900'
                    : approvedLocked
                      ? 'bg-emerald-100 text-emerald-800'
                      : 'bg-slate-100 text-slate-700'
              }`}
            >
              {item.approval_state === 'rejected' ? t.daily.rejected : blockedOrRejected ? t.workflow.blocked : approvedLocked ? t.workflow.approved : needsOverride ? t.workflow.needsReview : t.workflow.ready}
            </span>
            <span
              data-testid={`queue-state-badge-${item.id}`}
              className={`px-2 py-0.5 rounded-full border font-semibold text-[10px] ${getTransportBadgeClass(
                isQueued ? 'queued' : isSending ? 'sending' : isUncertain ? 'send_uncertain' : item.transport_status,
              )}`}
            >
              {cancelledSend ? t.daily.cancelled : queueLabels[queueState]}
            </span>
            {item.primary_skip_code && (
              <span className="px-2 py-0.5 rounded-full bg-amber-100 text-amber-950 font-mono font-semibold text-[10px]">
                {item.primary_skip_code}
              </span>
            )}
          </div>

          {/* Recipient, Location, CV PDF & Source URL */}
          <div className="text-slate-600 flex flex-wrap items-center gap-x-3 gap-y-1">
            {locationDisplay && (
              <span>
                {t.daily.location} · <strong className="text-slate-800">{locationDisplay}</strong>
              </span>
            )}
            <span>
              {t.review.recipientLabel} · <strong className="text-slate-900">{item.recipient_email || '—'}</strong>
            </span>
            {item.cc_emails && item.cc_emails.length > 0 && (
              <span>
                CC: <strong className="text-slate-800">{item.cc_emails.join(', ')}</strong>
              </span>
            )}
            <span className="inline-flex items-center gap-1">
              <FileCheck2 className="h-3.5 w-3.5 text-emerald-600 shrink-0" aria-hidden="true" />
              <span>
                CV: <strong className="text-slate-900">{item.frozen_cv_filename || t.daily.noAttachment}</strong>
                {item.attachment_pdf_sha256 && (
                  <span className="font-mono text-[10px] text-slate-500 ms-1" title={item.attachment_pdf_sha256}>
                    ({item.attachment_pdf_sha256.slice(0, 12)}…)
                  </span>
                )}
              </span>
              {item.attachment_pdf_sha256 && item.selected_cv_version_id && (
                <a href={`/api/v1/applications/${encodeURIComponent(item.id)}/attachment-preview`} target="_blank" rel="noopener noreferrer"
                  data-testid={`preview-attachment-${item.id}`} className="text-indigo-700 underline">{t.daily.previewLink}</a>
              )}
              {pdfDownloadUrl && (
                <a
                  href={pdfDownloadUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                  data-testid={`download-cv-pdf-${item.id}`}
                  className="inline-flex items-center gap-0.5 ms-1 px-1.5 py-0.5 rounded border border-emerald-200 bg-emerald-50 text-emerald-800 font-semibold hover:bg-emerald-100 text-[10px]"
                >
                  <Download className="h-3 w-3" aria-hidden="true" />
                  <span>{t.review.downloadPdf}</span>
                </a>
              )}
            </span>
            {item.fit_score !== null && item.fit_score !== undefined && (
              <span>
                Fit: <strong className="text-slate-900">{item.fit_score}</strong>
              </span>
            )}
          </div>

          {/* Clickable Source URL & Corroboration URL */}
          {(source || item.corroboration_url) && (
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px]">
              {source && /^https?:\/\//i.test(source) && (
                <a
                  href={source}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-1 text-indigo-600 hover:underline break-all"
                >
                  <ExternalLink className="h-3 w-3 shrink-0" aria-hidden="true" />
                  <span>{source}</span>
                </a>
              )}
              {item.corroboration_url && item.corroboration_url.startsWith('http') && (
                <a
                  href={item.corroboration_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-1 text-emerald-700 hover:underline break-all"
                >
                  <ShieldCheck className="h-3 w-3 shrink-0" aria-hidden="true" />
                  <span>Corroborated: {item.corroboration_url}</span>
                </a>
              )}
            </div>
          )}
        </div>

        {/* Action Buttons */}
        <div className="flex flex-wrap items-center gap-1.5">
          <button
            type="button"
            aria-expanded={expanded}
            onClick={() => setExpanded((v) => !v)}
            className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg border border-slate-300 bg-white hover:bg-slate-50 font-semibold text-slate-700 cursor-pointer"
          >
            {expanded ? <ChevronUp className="h-3.5 w-3.5" aria-hidden="true" /> : <ChevronDown className="h-3.5 w-3.5" aria-hidden="true" />}
            <span>{expanded ? t.review.hideBody : t.review.previewBody}</span>
          </button>

          {!isEditing && editable && (
            <button
              type="button"
              disabled={busy}
              data-testid={`edit-draft-btn-${item.id}`}
              onClick={openEditor}
              className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg border border-slate-300 bg-white hover:bg-slate-50 font-semibold text-slate-800 cursor-pointer disabled:opacity-40"
            >
              <Edit3 className="h-3.5 w-3.5" aria-hidden="true" />
              <span>{t.review.reviseBtn}</span>
            </button>
          )}

          {editable && !isEditing && (
            <button
              type="button"
              disabled={busy}
              data-testid={`revalidate-draft-btn-${item.id}`}
              onClick={handleRevalidate}
              className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg border border-indigo-200 bg-white text-indigo-700 hover:bg-indigo-50 font-semibold cursor-pointer disabled:opacity-40"
            >
              <RefreshCw className={`h-3.5 w-3.5 ${busyAction === 'revalidate' ? 'animate-spin' : ''}`} aria-hidden="true" />
              <span>{t.review.revalidateBtn}</span>
            </button>
          )}

          {editable && !isEditing && (
            <button
              type="button"
              disabled={busy || blockedOrRejected}
              data-testid={`regenerate-draft-btn-${item.id}`}
              title={t.review.regenerateHint}
              onClick={handleRegenerate}
              className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg border border-slate-300 bg-white text-slate-700 hover:bg-slate-50 font-semibold cursor-pointer disabled:opacity-40"
            >
              <Sparkles className="h-3.5 w-3.5 text-indigo-600" aria-hidden="true" />
              <span>{busyAction === 'regenerate' ? t.controls.loading : t.review.regenerateBtn}</span>
            </button>
          )}

          <button
            type="button"
            disabled={busy || isEditing || !approvable}
            data-testid={`approve-draft-btn-${item.id}`}
            title={blockedOrRejected ? t.review.blockedCannotApprove : undefined}
            onClick={handleApproveClick}
            className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg bg-emerald-600 hover:bg-emerald-700 text-white font-semibold cursor-pointer disabled:opacity-40"
          >
            <CheckCircle2 className="h-3.5 w-3.5" aria-hidden="true" />
            <span>
              {approvedLocked
                ? t.review.approvedLockedBtn
                : blockedOrRejected
                  ? t.review.blockedCannotApprove
                  : t.review.approveBtn}
            </span>
          </button>

          {editable && !isEditing && (
            <button type="button" disabled={busy} onClick={() => applyReviewDecision('reject')}
              data-testid={`reject-draft-btn-${item.id}`} className="rounded-lg border border-rose-300 px-2.5 py-1.5 font-semibold text-rose-800">
              {t.daily.reject}
            </button>
          )}
          {item.primary_skip_code === 'PREVIOUSLY_CONTACTED' && !isAccepted && !isSending && !isQueued && !isUncertain && (
            <button type="button" aria-expanded={showOverride} onClick={() => setShowOverride(!showOverride)}
              className="rounded-lg border border-amber-300 px-2.5 py-1.5 font-semibold text-amber-900">{t.daily.override}</button>
          )}

          {showGmailDraftButton && editable && !isEditing && (
            <button
              type="button"
              disabled={busy}
              onClick={handleSaveGmailDraft}
              className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg border border-indigo-200 text-indigo-700 hover:bg-indigo-50 font-semibold cursor-pointer disabled:opacity-40"
            >
              <MailPlus className="h-3.5 w-3.5" aria-hidden="true" />
              <span>{item.gmail_draft_id ? t.review.gmailDraftSaved : t.review.saveGmailDraftBtn}</span>
            </button>
          )}

          {isQueued && !isUncertain && !isSending && (
            <>
              <button
                type="button"
                disabled={busy}
                data-testid={`cancel-outbox-btn-${item.id}`}
                onClick={handleCancelQueuedOutbox}
                className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg border border-rose-300 bg-white text-rose-700 hover:bg-rose-50 font-semibold cursor-pointer disabled:opacity-40"
              >
                <XCircle className="h-3.5 w-3.5" aria-hidden="true" />
                <span>{t.review.cancelOutboxBtn}</span>
              </button>

            </>
          )}

          {isUncertain && (
            <button
              type="button"
              disabled={busy}
              data-testid={`reconcile-outbox-btn-${item.id}`}
              onClick={handleReconcileUncertainOutbox}
              className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg bg-purple-700 hover:bg-purple-800 text-white font-semibold cursor-pointer disabled:opacity-40"
            >
              <RefreshCw className={`h-3.5 w-3.5 ${busyAction === 'reconcile_outbox' ? 'animate-spin' : ''}`} aria-hidden="true" />
              <span>{t.review.reconcileOutboxBtn}</span>
            </button>
          )}

          {canRetry && !isQueued && (
            <button
              type="button"
              disabled={busy || !liveEnabled}
              data-testid={`retry-send-btn-${item.id}`}
              onClick={handleRetryFailedSend}
              className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg bg-amber-600 hover:bg-amber-700 text-white font-semibold cursor-pointer disabled:opacity-40"
            >
              <RotateCcw className="h-3.5 w-3.5" aria-hidden="true" />
              <span>{t.review.retrySendBtn}</span>
            </button>
          )}

          {!isAccepted && !isQueued && !isSending && !isUncertain && (
            <button
              type="button"
              disabled={busy || isEditing || !sendable || !liveEnabled}
              data-testid={`send-approved-btn-${item.id}`}
              onClick={() => onRequestSendConfirm(item)}
              className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg bg-slate-900 hover:bg-slate-800 text-white font-semibold cursor-pointer disabled:opacity-40"
            >
              <Send className="h-3.5 w-3.5" aria-hidden="true" />
              <span>{t.review.sendApprovedBtn}</span>
            </button>
          )}
        </div>
      </div>

      {showOverride && <form className="space-y-2 rounded-lg border border-amber-200 p-3" onSubmit={event => { event.preventDefault(); applyReviewDecision('override'); }}>
        <label className="block font-semibold">{t.daily.reason}
          <textarea required value={overrideReason} onChange={event => setOverrideReason(event.target.value)} rows={2}
            className="mt-1 block w-full rounded border border-slate-300 p-2 font-normal" />
        </label>
        <button disabled={busy || !overrideReason.trim()} className="rounded border border-amber-300 px-3 py-1.5 disabled:opacity-50">{t.daily.applyOverride}</button>
      </form>}

      {/* Inline Card Feedback */}
      {localNotice && (
        <div
          role={localNotice.isError ? 'alert' : 'status'}
          className={`p-2 rounded-lg border text-xs font-medium ${
            localNotice.isError
              ? 'bg-rose-50 border-rose-200 text-rose-800'
              : 'bg-emerald-50 border-emerald-200 text-emerald-900'
          }`}
        >
          {localNotice.text}
        </div>
      )}

      {/* Stale Approval Notice */}
      {approvalStale && !isAccepted && (
        <div className="p-2.5 rounded-lg bg-amber-50 border border-amber-200 text-amber-900 flex items-start gap-2">
          <AlertTriangle className="h-4 w-4 shrink-0 mt-0.5" aria-hidden="true" />
          <span>{t.daily.saveAgain}</span>
        </div>
      )}

      {/* Outbox Queue / Uncertain Handoff Status Banner */}
      {(isQueued || isSending) && (
        <div
          data-testid={`outbox-status-banner-${item.id}`}
          className="p-2.5 rounded-lg bg-indigo-50 border border-indigo-200 text-indigo-950 flex flex-wrap items-center justify-between gap-2"
        >
          <div className="flex items-start gap-2">
            <Clock className="h-4 w-4 text-indigo-700 shrink-0 mt-0.5" aria-hidden="true" />
            <div>
              <strong>{isSending ? t.review.outboxSendingLabel : t.review.outboxQueuedLabel}</strong>{' '}
              {item.outbox_last_error || item.skip_or_error_reason || t.daily.queuedHint}
              <span className="text-[11px] text-indigo-800 ms-2">
                {item.outbox_next_attempt_at && `${t.daily.nextAttempt} · ${formatDateTime(item.outbox_next_attempt_at)}`}
              </span>
            </div>
          </div>
        </div>
      )}

      {isUncertain && (
        <div
          data-testid={`uncertain-outbox-banner-${item.id}`}
          className="p-2.5 rounded-lg bg-purple-50 border border-purple-200 text-purple-950 flex items-start gap-2"
        >
          <AlertTriangle className="h-4 w-4 text-purple-700 shrink-0 mt-0.5" aria-hidden="true" />
          <div>
            <strong>{t.workflow.uncertain}</strong> · {t.review.uncertainHint}
            {(item.outbox_last_error || item.skip_or_error_reason) && <p>{item.outbox_last_error || item.skip_or_error_reason}</p>}
          </div>
        </div>
      )}

      {/* Review Reasons & Skip/Error Reason */}
      {item.review_reasons && item.review_reasons.length > 0 && (
        <div className="p-2.5 rounded-lg bg-amber-50 border border-amber-200 text-amber-900 flex items-start gap-2">
          <ShieldAlert className="h-4 w-4 shrink-0 mt-0.5" aria-hidden="true" />
          <div>
            <strong>{t.daily.reviewReasons}</strong> · {item.review_reasons.join('; ')}
          </div>
        </div>
      )}

      {item.skip_or_error_reason && !isQueued && !isSending && !isUncertain && (
        <div
          data-testid={`blocked-reason-banner-${item.id}`}
          className={`p-2.5 rounded-lg border flex items-start gap-2 ${
            item.transport_status === 'failed' || blockedOrRejected
              ? 'bg-rose-50 border-rose-200 text-rose-900'
              : 'bg-amber-50 border-amber-200 text-amber-900'
          }`}
        >
          <AlertTriangle className="h-4 w-4 shrink-0 mt-0.5" aria-hidden="true" />
          <div>
            <strong>{item.primary_skip_code ? `${item.primary_skip_code}: ` : 'Blocked / Status Reason: '}</strong>
            {item.skip_or_error_reason}
          </div>
        </div>
      )}

      {/* Delivery Receipt */}
      {(item.provider_message_id || item.sent_at) && (
        <div className="p-2.5 rounded-lg bg-emerald-50 border border-emerald-200 text-emerald-900 flex flex-wrap gap-x-3 gap-y-1">
          <span className="font-semibold">{t.review.acceptedByProvider}</span>
          {item.sent_at && <span>Sent: {formatDateTime(item.sent_at)}</span>}
          {item.provider_message_id && (
            <span className="font-mono text-[11px]">{t.daily.receipt} · {item.provider_message_id}</span>
          )}
        </div>
      )}

      {/* Evidence Quote & CV Rationale */}
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-slate-700">
        <span>{t.daily.posted} · {item.posted_at ? formatDateTime(item.posted_at) : t.daily.unknown}</span>
        {item.requested_subject && <span>{t.daily.requested} · {item.requested_subject}</span>}
        {!!item.required_documents?.length && <span>{t.daily.documents} · {item.required_documents.join(', ')}</span>}
      </div>
      {item.verification_explanation && <p><strong>{t.daily.verification}</strong> · {item.verification_explanation}</p>}
      {excerpt && (
        <div className="p-2.5 rounded-lg bg-emerald-50/60 border border-emerald-200 text-emerald-900">
          <strong>{t.daily.evidence}</strong> · “{excerpt}”
        </div>
      )}

      {(item.why_this_cv || item.required_gaps?.length || item.verified_skills?.length) ? (
        <div className="p-2.5 rounded-lg bg-slate-50 border border-slate-200 text-slate-700 space-y-1">
          <div>
            {item.why_this_cv && <><strong>{t.daily.why}</strong> · {item.why_this_cv}</>}
          </div>
          {((item.verified_skills && item.verified_skills.length > 0) ||
            (item.required_gaps && item.required_gaps.length > 0)) && (
            <div className="flex flex-wrap gap-1 pt-0.5">
              {(item.verified_skills || []).map((sk) => (
                <span key={`sk-${sk}`} className="px-1.5 py-0.5 rounded bg-emerald-100 text-emerald-800 text-[10px]">
                  ✓ {sk}
                </span>
              ))}
              {(item.required_gaps || []).map((gp) => (
                <span key={`gp-${gp}`} className="px-1.5 py-0.5 rounded bg-amber-100 text-amber-900 text-[10px]">
                  {t.daily.gaps} · {gp}
                </span>
              ))}
            </div>
          )}
        </div>
      ) : null}

      {/* Inline Editor OR Subject/Body Preview */}
      {isEditing && editable ? (
        <form onSubmit={event => { event.preventDefault(); handleSaveRevise(); }}
          data-testid={`edit-draft-panel-${item.id}`}
          className="p-3.5 rounded-xl bg-slate-50 border border-indigo-200 space-y-3"
        >
          {approvedLocked && (
            <div className="p-2 rounded-lg bg-amber-50 border border-amber-200 text-amber-900 flex items-start gap-2 text-[11px]">
              <AlertTriangle className="h-3.5 w-3.5 shrink-0 mt-0.5" aria-hidden="true" />
              <span>{t.review.approvalInvalidatedWarn}</span>
            </div>
          )}

          <div className="grid grid-cols-1 md:grid-cols-2 gap-2.5">
            <div>
              <label htmlFor={`edit-subject-${item.id}`} className="block text-[11px] font-semibold text-slate-700 mb-1">
                {t.review.subjectLabel}
              </label>
              <input
                id={`edit-subject-${item.id}`}
                type="text"
                value={editSubject}
                onChange={(e) => setEditSubject(e.target.value)}
                className="w-full rounded-lg border border-slate-300 px-2.5 py-1.5 bg-white text-slate-900"
              />
            </div>
            <div>
              <label htmlFor={`edit-recipient-${item.id}`} className="block text-[11px] font-semibold text-slate-700 mb-1">
                {t.review.recipientLabel}
              </label>
              <input
                id={`edit-recipient-${item.id}`}
                required dir="ltr" type="email"
                value={editRecipient}
                onChange={(e) => setEditRecipient(e.target.value)}
                className="w-full rounded-lg border border-slate-300 px-2.5 py-1.5 bg-white text-slate-900"
              />
            </div>
            <div>
              <label htmlFor={`edit-cc-${item.id}`} className="block text-[11px] font-semibold text-slate-700 mb-1">
                {t.review.ccEmailsLabel}
              </label>
              <input
                id={`edit-cc-${item.id}`}
                multiple dir="ltr" type="email"
                value={editCc}
                onChange={(e) => setEditCc(e.target.value)}
                placeholder="hr@example.com, team@example.com"
                className="w-full rounded-lg border border-slate-300 px-2.5 py-1.5 bg-white text-slate-900"
              />
            </div>
            <div>
              <label htmlFor={`edit-cv-${item.id}`} className="block text-[11px] font-semibold text-slate-700 mb-1">
                {t.review.changeCvLabel}
              </label>
              <select
                id={`edit-cv-${item.id}`}
                data-testid={`edit-cv-select-${item.id}`}
                value={editCvVersionId}
                onChange={(e) => setEditCvVersionId(e.target.value)}
                className="w-full rounded-lg border border-slate-300 px-2.5 py-1.5 bg-white text-slate-900"
              >
                <option value="">{t.review.noCvAvailable}</option>
                {cvOptions.map((opt) => (
                  <option key={opt.versionId} value={opt.versionId} disabled={!opt.pdfReady}>
                    {opt.label}
                  </option>
                ))}
              </select>
            </div>
          </div>

          <div>
            <label htmlFor={`edit-body-${item.id}`} className="block text-[11px] font-semibold text-slate-700 mb-1">
              {t.review.bodyLabel}
            </label>
            <textarea
              id={`edit-body-${item.id}`}
              rows={6}
              value={editBody}
              onChange={(e) => setEditBody(e.target.value)}
              className="w-full rounded-lg border border-slate-300 px-2.5 py-1.5 bg-white font-sans text-slate-900"
            />
          </div>

          <div>
            <label htmlFor={`edit-notes-${item.id}`} className="block text-[11px] font-semibold text-slate-700 mb-1">
              {t.review.reviewNotesLabel}
            </label>
            <input
              id={`edit-notes-${item.id}`}
              type="text"
              value={editNotes}
              onChange={(e) => setEditNotes(e.target.value)}
              placeholder="Reviewer notes (optional)"
              className="w-full rounded-lg border border-slate-300 px-2.5 py-1.5 bg-white text-slate-900"
            />
          </div>

          <div className="flex flex-wrap gap-2">
            <button
              type="submit"
              disabled={busy}
              data-testid={`save-revision-btn-${item.id}`}
              className="px-3.5 py-1.5 rounded-lg bg-indigo-600 hover:bg-indigo-700 text-white font-semibold cursor-pointer disabled:opacity-50"
            >
              {busyAction === 'revise' ? t.controls.saving : t.review.saveRevisionBtn}
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => setIsEditing(false)}
              className="px-3.5 py-1.5 rounded-lg border border-slate-300 bg-white text-slate-700 font-semibold hover:bg-slate-100 cursor-pointer"
            >
              {t.controls.cancel}
            </button>
          </div>
        </form>
      ) : (
        <div className="p-2.5 rounded-lg bg-slate-50 border border-slate-200 space-y-1.5">
          <div className="text-slate-900">
            <strong>{t.review.subjectLabel}:</strong> {item.subject || '—'}
          </div>
          {expanded && (
            <pre className="whitespace-pre-wrap font-sans text-slate-800 bg-white p-2.5 rounded border border-slate-200 max-h-64 overflow-y-auto">
              {item.body_text || '—'}
            </pre>
          )}
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[10px] text-slate-500">
            {item.approved_content_hash && approvedLocked && (
              <span className="text-emerald-700 font-mono">
                Approved Hash: {item.approved_content_hash.slice(0, 16)}…
              </span>
            )}
            {item.review_notes && (
              <span>
                <strong>Notes:</strong> {item.review_notes}
              </span>
            )}
          </div>
        </div>
      )}
    </article>
  );
};
