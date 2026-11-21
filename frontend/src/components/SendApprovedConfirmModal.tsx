import React, { useEffect, useRef, useState } from 'react';
import { useI18n } from '../i18n';
import { apiRequest, ApiError } from '../lib/api';
import { getApplicationQueueState, type DraftApplicationItem } from '../lib/workflowTypes';

interface DispatchResponse {
  queued_ids: string[];
  sent_ids: string[];
  failed_ids?: string[];
  uncertain_ids?: string[];
  blocked: { id: string; error: string }[];
  results?: { application_id: string; provider_message_id?: string; sent_at?: string; error?: string }[];
}

interface SendingMailbox {
  id: string;
  sender_email: string;
  dedup_coverage?: {
    coverage_mode: string;
    sent_sync_status: string;
    sent_last_synced_at: string | null;
    sent_earliest_covered_at: string | null;
    sent_latest_covered_at: string | null;
    checked_through_at: string | null;
  } | null;
}

type SenderState = { status: 'loading' | 'missing' | 'error' } | { status: 'ready'; mailbox: SendingMailbox };

function historyCoverage(coverage: SendingMailbox['dedup_coverage']) {
  if (coverage?.coverage_mode === 'applyflow_ledger_plus_gmail_sent' && coverage.sent_sync_status === 'ok') return 'complete';
  if (coverage?.coverage_mode === 'partial') return 'partial';
  if (coverage?.coverage_mode === 'applyflow_ledger_only') return 'ledger_only';
  return 'unavailable';
}

type Phase = 'queued' | 'sending' | 'sent' | 'failed' | 'uncertain' | 'cancelled' | 'blocked' | 'pending';
interface Result {
  phase: Phase;
  detail?: string;
  at?: string | null;
  receipt?: string | null;
}

export interface SendApprovedConfirmModalProps {
  open: boolean;
  items: DraftApplicationItem[];
  onClose: () => void;
  onCompleted: (summary: string, isError?: boolean) => Promise<void>;
}

export function SendApprovedConfirmModal(props: SendApprovedConfirmModalProps) {
  return props.open && props.items.length > 0 ? <Confirmation {...props} /> : null;
}

function Confirmation({ items, onClose, onCompleted }: SendApprovedConfirmModalProps) {
  const { t, formatDateTime } = useI18n();
  const dialog = useRef<HTMLDialogElement>(null);
  const submitted = useRef(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [refreshFailed, setRefreshFailed] = useState(false);
  const [results, setResults] = useState<Record<string, Result> | null>(null);
  const [sender, setSender] = useState<SenderState>({ status: 'loading' });
  const [mailboxRefresh, setMailboxRefresh] = useState(0);
  const [acknowledged, setAcknowledged] = useState(false);
  const coverage = sender.status === 'ready' ? sender.mailbox.dedup_coverage : undefined;
  const history = historyCoverage(coverage);
  const canConfirm = sender.status === 'ready' && (history === 'complete' || acknowledged);
  const labels: Record<Phase, string> = {
    queued: t.workflow.queued, sending: t.daily.sending, sent: t.review.acceptedByProvider,
    failed: t.workflow.failed, uncertain: t.workflow.uncertain, cancelled: t.daily.cancelled,
    blocked: t.workflow.blocked, pending: t.daily.pending,
  };

  useEffect(() => {
    const el = dialog.current;
    el?.showModal();
    return () => el?.close();
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    apiRequest<{ sending_mailbox_id: string | null; mailboxes: SendingMailbox[] }>('/mailboxes', {
      signal: controller.signal, cache: 'no-store',
    }).then(response => {
      if (controller.signal.aborted) return;
      const mailbox = response.mailboxes.find(row => row.id === response.sending_mailbox_id);
      if (mailbox?.sender_email) setSender({ status: 'ready', mailbox });
      else setSender({ status: response.sending_mailbox_id === null ? 'missing' : 'error' });
    }).catch(() => {
      if (!controller.signal.aborted) setSender({ status: 'error' });
    });
    return () => controller.abort();
  }, [mailboxRefresh]);

  function refreshSender() {
    setAcknowledged(false);
    setSender({ status: 'loading' });
    setMailboxRefresh(value => value + 1);
  }

  const pendingIds = Object.entries(results ?? {})
    .filter(([, result]) => ['queued', 'sending', 'pending'].includes(result.phase))
    .map(([id]) => id).join(',');
  useEffect(() => {
    if (!pendingIds) return;
    const controller = new AbortController();
    let timer: number;
    async function refresh() {
      try {
        const params = new URLSearchParams({ scope: 'all', include_all: 'true', application_ids: pendingIds });
        const { items: rows } = await apiRequest<{ items: DraftApplicationItem[] }>(
          `/review-queue?${params}`, { signal: controller.signal }
        );
        if (controller.signal.aborted) return;
        setResults(previous => {
          if (!previous) return previous;
          const next = { ...previous };
          for (const row of rows) {
            const old = previous[row.id];
            if (!old || !['queued', 'sending', 'pending'].includes(old.phase)) continue;
            const phase = row.transport_status === 'cancelled' || row.outbox_status === 'cancelled'
              ? 'cancelled' : getApplicationQueueState(row);
            if (phase === 'ready' || phase === 'approved') continue;
            next[row.id] = { phase, detail: row.outbox_last_error || row.skip_or_error_reason || '', at: row.sent_at || row.outbox_updated_at, receipt: row.provider_message_id };
          }
          return next;
        });
        const received = new Set(rows.map(row => row.id));
        setRefreshFailed(pendingIds.split(',').some(id => !received.has(id)));
      } catch {
        if (!controller.signal.aborted) setRefreshFailed(true);
      } finally {
        if (!controller.signal.aborted) timer = window.setTimeout(refresh, 4000);
      }
    }
    timer = window.setTimeout(refresh, 2000);
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [pendingIds]);

  async function confirm() {
    if (submitted.current || sender.status !== 'ready' || !canConfirm) return;
    submitted.current = true;
    setBusy(true);
    setError('');
    const next: Record<string, Result> = {};
    try {
      const res = await apiRequest<DispatchResponse>('/drafts/send-approved', {
        method: 'POST',
        body: JSON.stringify({
          application_ids: items.map(item => item.id),
          expected_hashes: Object.fromEntries(items.map(item => [item.id, item.approved_content_hash])),
          expected_mailbox_id: sender.mailbox.id,
          acknowledge_incomplete_history: history !== 'complete' && acknowledged,
          dispatch_immediately: false,
        }),
      });
      for (const item of items) {
        const detail = res.results?.find(row => row.application_id === item.id);
        const blocked = res.blocked?.find(row => row.id === item.id);
        let phase: Phase = 'pending';
        if (res.sent_ids?.includes(item.id)) phase = 'sent';
        else if (res.uncertain_ids?.includes(item.id)) phase = 'uncertain';
        else if (res.failed_ids?.includes(item.id)) phase = 'failed';
        else if (blocked) phase = 'blocked';
        else if (res.queued_ids?.includes(item.id)) phase = 'queued';
        next[item.id] = { phase, detail: blocked?.error || detail?.error, at: detail?.sent_at, receipt: detail?.provider_message_id };
      }
    } catch (err) {
      if (err instanceof ApiError && err.status === 409 && ['SENDER_CHANGED', 'INCOMPLETE_SENT_HISTORY', 'NO_MAILBOX'].includes(err.code)) {
        setError(err.code === 'SENDER_CHANGED' ? t.sendCoverage.senderChanged
          : err.code === 'INCOMPLETE_SENT_HISTORY' ? t.sendCoverage.historyChanged : t.sendCoverage.noSender);
        submitted.current = false;
        setBusy(false);
        refreshSender();
        return;
      }
      const phase = err instanceof ApiError && err.status >= 400 && err.status < 500 ? 'blocked' : 'pending';
      for (const item of items) next[item.id] = { phase };
      setError(err instanceof Error ? err.message : t.daily.pending);
    }
    setResults(next);
    setBusy(false);
    const counts = Object.values(next).reduce((acc, r) => acc.set(r.phase, (acc.get(r.phase) || 0) + 1), new Map<Phase, number>());
    try {
      await onCompleted([...counts].map(([phase, count]) => `${labels[phase]} (${count})`).join(' · '), counts.has('failed') || counts.has('blocked') || counts.has('uncertain'));
    } catch (err) {
      setError(err instanceof Error ? err.message : t.daily.pending);
    }
  }

  const renderTime = (value?: string | null) => value
    ? <time dateTime={value} dir="auto">{formatDateTime(value)}</time>
    : t.sendCoverage.unrecorded;

  return (
    <dialog ref={dialog} aria-labelledby="send-approved-modal-title" data-testid="send-approved-confirm-modal"
      onCancel={event => { event.preventDefault(); if (!busy) onClose(); }}
      className="m-auto w-[calc(100%_-_2rem)] max-w-2xl rounded-xl border border-slate-200 bg-white p-0 text-sm text-slate-900 shadow-xl backdrop:bg-slate-900/50">
      <div className="flex max-h-[88dvh] flex-col">
        <header className="border-b border-slate-200 p-4">
          <h2 id="send-approved-modal-title" className="font-bold">{results ? t.daily.sendSummary : t.daily.reviewSend} ({items.length})</h2>
          {results && <p role="status" className="mt-1 font-medium">
            {(Object.keys(labels) as Phase[]).flatMap(phase => {
              const count = Object.values(results).filter(row => row.phase === phase).length;
              return count ? [`${labels[phase]} (${count})`] : [];
            }).join(' · ')}
          </p>}
          {(!results || pendingIds) && <p className="mt-1 text-xs text-slate-600">{results ? t.daily.queuedHint : t.daily.sendHint}</p>}
        </header>
        <div className="space-y-3 overflow-auto p-4">
          {error && <p role="alert">{error}</p>}
          {refreshFailed && <p role="status">{t.daily.refreshFail}</p>}
          {sender.status === 'ready' ? (
            <section className="space-y-2 rounded-lg border border-slate-200 bg-slate-50 p-3" aria-label={t.sendCoverage.sender}>
              <p className="break-all">{t.sendCoverage.sender} · <strong data-testid="send-confirm-sender"><bdi>{sender.mailbox.sender_email}</bdi></strong></p>
              <p data-testid="send-confirm-coverage" data-mode={history} className="text-sm font-medium">
                {t.sendCoverage[history]}
              </p>
              <dl className="grid gap-2 text-xs sm:grid-cols-3">
                <div><dt className="text-slate-600">{t.sendCoverage.lastSync}</dt><dd>{renderTime(coverage?.sent_last_synced_at)}</dd></div>
                <div><dt className="text-slate-600">{t.sendCoverage.inspectedDates}</dt><dd>
                  {coverage?.sent_earliest_covered_at || coverage?.sent_latest_covered_at
                    ? <>{renderTime(coverage.sent_earliest_covered_at)} — {renderTime(coverage.sent_latest_covered_at)}</> : t.sendCoverage.unrecorded}
                </dd></div>
                <div><dt className="text-slate-600">{t.sendCoverage.checkedThrough}</dt><dd>{renderTime(coverage?.checked_through_at)}</dd></div>
              </dl>
              {!results && history !== 'complete' && (
                <label className="flex items-start gap-2 rounded-lg border border-amber-200 bg-amber-50 p-2 text-sm text-amber-950">
                  <input type="checkbox" checked={acknowledged} disabled={busy} data-testid="send-confirm-history-ack"
                    className="mt-1 shrink-0" onChange={event => setAcknowledged(event.target.checked)} />
                  <span>{t.sendCoverage.acknowledgement}</span>
                </label>
              )}
            </section>
          ) : sender.status === 'loading' ? <p role="status">{t.sendCoverage.loading}</p> : (
            <div role="alert" className="space-y-2 text-sm">
              <p>{sender.status === 'missing' ? t.sendCoverage.noSender : t.sendCoverage.lookupFailed}</p>
              <button type="button" onClick={refreshSender} className="rounded-lg border px-3 py-1.5">{t.controls.refresh}</button>
            </div>
          )}
          {items.map(item => {
            const result = results?.[item.id];
            return <section key={item.id} data-testid={`send-confirm-item-${item.id}`} className="space-y-2 break-words rounded-lg border border-slate-200 p-3">
              <h3 className="font-semibold">{[item.role_title, item.company_name].filter(Boolean).join(' · ') || item.id}</h3>
              <div>{t.review.recipientLabel} · <bdi>{item.recipient_email}</bdi></div>
              {!!item.cc_emails?.length && <div>CC · <bdi>{item.cc_emails.join(', ')}</bdi></div>}
              <div>{t.review.subjectLabel} · <strong>{item.subject}</strong></div>
              <div>{t.review.changeCvLabel} · <bdi>{item.frozen_cv_filename || t.daily.noAttachment}</bdi></div>
              {item.attachment_pdf_sha256 && <a href={`/api/v1/applications/${encodeURIComponent(item.id)}/attachment-preview`}
                target="_blank" rel="noopener noreferrer" className="inline-block text-indigo-700 underline">{t.daily.previewLink}</a>}
              <details><summary className="cursor-pointer text-indigo-700">{t.review.bodyLabel}</summary><p className="mt-2 whitespace-pre-wrap" dir="auto">{item.body_text}</p></details>
              {result && <div role="status" className="space-y-1 border-t border-slate-200 pt-2 text-xs">
                <strong data-testid={`send-modal-item-status-${item.id}`}>{labels[result.phase]}</strong>
                {result.detail && <p>{result.detail}</p>}
                {result.at && <p>{t.daily.updated} · {formatDateTime(result.at)}</p>}
                {result.receipt && <p>{t.daily.receipt} · <bdi>{result.receipt}</bdi></p>}
              </div>}
            </section>;
          })}
        </div>
        <footer className="flex flex-wrap justify-end gap-2 border-t border-slate-200 p-3">
          <button type="button" autoFocus disabled={busy} onClick={onClose} data-testid={results ? 'done-send-confirm-modal-btn' : 'close-send-confirm-modal-btn'}
            className="rounded-lg border border-slate-300 px-4 py-2">{results ? t.daily.close : t.controls.cancel}</button>
          {!results && <button type="button" disabled={busy || !canConfirm} onClick={confirm} data-testid="confirm-send-approved-submit-btn"
            className="rounded-lg bg-slate-900 px-4 py-2 font-semibold text-white disabled:opacity-50">{busy ? t.daily.waiting : t.daily.confirmSend}</button>}
        </footer>
      </div>
    </dialog>
  );
}
