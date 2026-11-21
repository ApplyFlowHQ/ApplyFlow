import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { AlertTriangle, CheckCircle2, RefreshCw, Send, XCircle } from 'lucide-react';
import { useI18n } from '../i18n';
import { apiRequest, ApiError } from '../lib/api';
import {
  canBulkApproveReady,
  reviewedDraftSelection,
  type DraftApprovalResponse,
  canSendApprovedItem,
  isApprovedLocked,
  isDuplicateItem,
  isQueuedInOutbox,
  isRejectedOrBlocked,
  isSendingInOutbox,
  isUncertainSend,
  needsHumanReviewOverride,
  type CvDocumentSummary,
  type DraftApplicationItem,
  type WorkflowCounts,
} from '../lib/workflowTypes';
import { WorkflowSummaryStrip } from './WorkflowSummaryStrip';
import { PreparedApplicationCard } from './PreparedApplicationCard';
import { SendApprovedConfirmModal } from './SendApprovedConfirmModal';

const PAGE_SIZE = 100;

interface ReviewQueueTabProps {
  scope?: 'real' | 'demo';
  initialBatchId?: string;
  onStatusMessage: (msg: string, isError?: boolean) => void;
}

type QueueStatusFilter = 'all' | 'ready' | 'needs_review' | 'approved' | 'queued' | 'duplicate_blocked' | 'accepted' | 'failed' | 'uncertain';

function computeFallbackCounts(items: DraftApplicationItem[]): WorkflowCounts {
  let needs_review = 0;
  let ready = 0;
  let duplicate = 0;
  let blocked = 0;
  let approved = 0;
  let queued = 0;
  let provider_accepted = 0;
  let failed = 0;
  let uncertain = 0;

  for (const item of items) {
    const dup = isDuplicateItem(item);
    const blk = isRejectedOrBlocked(item);
    const lck = isApprovedLocked(item);
    if (dup) duplicate += 1;
    if (blk && !dup) blocked += 1;
    if (item.transport_status === 'sent' || item.transport_status === 'provider_accepted' || item.outbox_status === 'sent') {
      provider_accepted += 1;
    } else if (isUncertainSend(item)) {
      uncertain += 1;
    } else if (isQueuedInOutbox(item) || isSendingInOutbox(item)) {
      queued += 1;
    } else if (item.transport_status === 'failed' || item.outbox_status === 'failed') {
      failed += 1;
    } else if (lck) {
      approved += 1;
    } else if (!blk && needsHumanReviewOverride(item)) {
      needs_review += 1;
    } else if (!blk) {
      ready += 1;
    }
  }

  return {
    imported: items.length,
    needs_review,
    ready,
    duplicate,
    blocked,
    approved,
    queued,
    provider_accepted,
    failed,
    uncertain,
  };
}

export const ReviewQueueTab: React.FC<ReviewQueueTabProps> = ({
  scope = 'real',
  initialBatchId = '',
  onStatusMessage,
}) => {
  const { t, formatDateTime } = useI18n();
  const [items, setItems] = useState<DraftApplicationItem[]>([]);
  const [batches, setBatches] = useState<any[]>([]);
  const [cvs, setCvs] = useState<CvDocumentSummary[]>([]);
  const [selectedBatchId, setSelectedBatchId] = useState<string>(initialBatchId);
  const [showAll, setShowAll] = useState<boolean>(true);
  const [offset, setOffset] = useState(0);
  const [total, setTotal] = useState(0);
  const [loadedQuery, setLoadedQuery] = useState('');
  const [statusFilter, setStatusFilter] = useState<QueueStatusFilter>('all');
  const [workflowCounts, setWorkflowCounts] = useState<WorkflowCounts | null>(null);
  const [liveEnabled, setLiveEnabled] = useState<boolean>(false);
  const [liveReason, setLiveReason] = useState<string>('');
  const [busyBulk, setBusyBulk] = useState<string | null>(null);
  const [sendModalItems, setSendModalItems] = useState<DraftApplicationItem[]>([]);

  const params = new URLSearchParams({ scope, include_all: String(showAll), offset: String(offset), limit: String(PAGE_SIZE) });
  if (selectedBatchId) params.set('batch_id', selectedBatchId);
  const query = params.toString();
  const activeQuery = useRef(query);
  const requestSequence = useRef(0);
  const [pollError, setPollError] = useState(false);
  const loadQueue = async (includeLookups = true) => {
    if (activeQuery.current !== query) return;
    const sequence = ++requestSequence.current;
    const [qRes, bRes, cvRes] = await Promise.all([
      apiRequest<{
        items: DraftApplicationItem[];
        total: number;
        workflow_counts?: WorkflowCounts;
        live_sending_enabled: boolean;
        live_sending_reason: string;
      }>(`/review-queue?${query}`),
      includeLookups ? apiRequest<{ batches: any[] }>('/batches').catch(() => null) : null,
      includeLookups ? apiRequest<{ cvs: CvDocumentSummary[] }>('/cvs').catch(() => null) : null,
    ]);
    if (sequence !== requestSequence.current) return;
    const lastOffset = Math.max(0, Math.ceil(qRes.total / PAGE_SIZE) - 1) * PAGE_SIZE;
    setTotal(qRes.total);
    if (offset > lastOffset) {
      setOffset(lastOffset);
      return;
    }
    setLoadedQuery(query);
    setPollError(false);
    const loadedItems = qRes.items || [];
    setItems(loadedItems);
    setWorkflowCounts(qRes.workflow_counts || computeFallbackCounts(loadedItems));
    setLiveEnabled(Boolean(qRes.live_sending_enabled));
    setLiveReason(qRes.live_sending_reason || '');
    if (bRes) setBatches(bRes.batches || []);
    if (cvRes) setCvs(cvRes.cvs || []);
  };

  useEffect(() => {
    activeQuery.current = query;
    setItems([]);
    setWorkflowCounts(null);
    loadQueue().catch((e) => onStatusMessage(String(e), true));
    return () => { requestSequence.current++; };
  }, [query]);

  const inFlight = items.some(item => isQueuedInOutbox(item) || isSendingInOutbox(item));
  useEffect(() => {
    if (!inFlight || sendModalItems.length) return;
    let cancelled = false;
    let timer: number;
    async function refresh() {
      try {
        await loadQueue(false);
      } catch {
        if (!cancelled) setPollError(true);
      } finally {
        if (!cancelled) timer = window.setTimeout(refresh, 4000);
      }
    }
    timer = window.setTimeout(refresh, 3000);
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [query, inFlight, sendModalItems]);

  const matchesFilter = (d: DraftApplicationItem, filter: QueueStatusFilter): boolean => {
    switch (filter) {
      case 'ready':
        return (
          !isRejectedOrBlocked(d) &&
          !needsHumanReviewOverride(d) &&
          !isApprovedLocked(d) &&
          !isQueuedInOutbox(d) &&
          !isSendingInOutbox(d) &&
          !isUncertainSend(d) &&
          d.transport_status !== 'sent' &&
          d.transport_status !== 'provider_accepted' &&
          d.transport_status !== 'failed'
        );
      case 'needs_review':
        return !isRejectedOrBlocked(d) && needsHumanReviewOverride(d) && !isApprovedLocked(d);
      case 'approved':
        return (
          isApprovedLocked(d) &&
          !isQueuedInOutbox(d) &&
          !isSendingInOutbox(d) &&
          !isUncertainSend(d) &&
          d.transport_status !== 'sent' &&
          d.transport_status !== 'provider_accepted' &&
          d.transport_status !== 'failed'
        );
      case 'queued':
        return isQueuedInOutbox(d) || isSendingInOutbox(d);
      case 'duplicate_blocked':
        return isDuplicateItem(d) || isRejectedOrBlocked(d);
      case 'accepted':
        return d.transport_status === 'sent' || d.transport_status === 'provider_accepted' || d.outbox_status === 'sent';
      case 'failed':
        return d.transport_status === 'failed' || d.outbox_status === 'failed';
      case 'uncertain':
        return isUncertainSend(d);
      default:
        return true;
    }
  };

  const visibleItems = useMemo(
    () => loadedQuery === query ? items.filter((d) => matchesFilter(d, statusFilter)) : [],
    [items, statusFilter, loadedQuery, query],
  );

  const readyItems = useMemo(() => visibleItems.filter(canBulkApproveReady), [visibleItems]);
  const approvedItems = useMemo(() => visibleItems.filter(canSendApprovedItem), [visibleItems]);
  const approvedIds = useMemo(() => approvedItems.map((i) => i.id), [approvedItems]);
  const queuedUnsentIds = useMemo(
    () =>
      visibleItems
        .filter((i) => isQueuedInOutbox(i) && !isUncertainSend(i) && !isSendingInOutbox(i) && (i.outbox_attempt_count ?? 0) === 0 && i.outbox_handoff_outcome !== 'provider_accepted')
        .map((i) => i.id),
    [visibleItems],
  );

  const handleBulkApproveReady = async () => {
    if (readyItems.length === 0) return;
    setBusyBulk('bulk-approve');
    try {
      const res = await apiRequest<DraftApprovalResponse>('/drafts/approve', {
        method: 'POST',
        body: JSON.stringify({ ...reviewedDraftSelection(readyItems), allow_third_party_override: false }),
      });
      await loadQueue();
      onStatusMessage(
        [`${t.workflow.approved} (${res.approved_ids.length}/${readyItems.length})`, ...res.failed.map(row => row.error)].join(' · '),
        res.failed.length > 0,
      );
    } catch (e2) {
      onStatusMessage(e2 instanceof ApiError ? e2.message : 'Bulk approve failed', true);
    } finally {
      setBusyBulk(null);
    }
  };

  const handleBulkSendApproved = () => {
    if (approvedItems.length === 0) return;
    if (!liveEnabled) {
      onStatusMessage('Live Sending is locked in Settings. Unlock Live Sending before transmitting external emails.', true);
      return;
    }
    setSendModalItems(approvedItems);
  };

  const handleCancelAllQueued = async () => {
    if (queuedUnsentIds.length === 0) return;
    setBusyBulk('cancel-queued');
    try {
      const outcomes = await Promise.allSettled(queuedUnsentIds.map(id =>
        apiRequest(`/applications/${id}/cancel-outbox`, { method: 'POST', body: JSON.stringify({}) })
      ));
      await loadQueue();
      const cancelled = outcomes.filter(result => result.status === 'fulfilled').length;
      onStatusMessage(`${t.daily.cancelled} (${cancelled}/${outcomes.length})`, cancelled < outcomes.length);
    } catch (e2) {
      onStatusMessage(e2 instanceof ApiError ? e2.message : 'Unable to cancel queued entries', true);
    } finally {
      setBusyBulk(null);
    }
  };

  const filterTabs: { id: QueueStatusFilter; label: string }[] = [
    { id: 'all', label: t.batches.filterAll },
    { id: 'ready', label: t.batches.filterReady },
    { id: 'needs_review', label: t.batches.filterNeedsReview },
    { id: 'approved', label: t.batches.filterApproved },
    { id: 'queued', label: t.batches.filterQueued },
    { id: 'duplicate_blocked', label: t.batches.filterDuplicateBlocked },
    { id: 'accepted', label: t.batches.filterAccepted },
    { id: 'failed', label: t.batches.filterFailed },
    { id: 'uncertain', label: t.batches.filterUncertain },
  ];

  return (
    <div className="space-y-4">
      {pollError && <p role="status">{t.daily.refreshFail}</p>}
      {!liveEnabled && (
        <div className="p-3 rounded-xl bg-amber-50 border border-amber-200 text-amber-900 text-xs flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <AlertTriangle className="h-4 w-4 shrink-0" aria-hidden="true" />
            <span>
              {t.daily.sendingLocked} {liveReason}
            </span>
          </div>
          <Link to="/settings" className="px-2.5 py-1 rounded bg-amber-900 text-white font-semibold hover:bg-amber-800">
            {t.nav.settings}
          </Link>
        </div>
      )}

      {/* Batch Filter & Include-All Toolbar */}
      <div className="bg-white rounded-xl border border-slate-200 p-3.5 flex flex-wrap items-center justify-between gap-3 text-xs">
        <div className="flex flex-wrap items-center gap-3">
          <div className="flex items-center gap-2">
            <label htmlFor="review-batch-select" className="font-semibold text-slate-700">
              {t.review.batchFilterLabel}:
            </label>
            <select
              id="review-batch-select"
              data-testid="review-batch-select"
              value={selectedBatchId}
              onChange={(e) => { setOffset(0); setSelectedBatchId(e.target.value); }}
              className="rounded-lg border border-slate-300 px-2.5 py-1.5 bg-white text-slate-900"
            >
              <option value="">{t.review.allBatches}</option>
              {batches.map((b) => (
                <option key={b.id} value={b.id}>
                  {b.id.slice(0, 8)} · {b.mode} ({b.status}) — {formatDateTime(b.created_at)}
                </option>
              ))}
            </select>
          </div>

          <label className="inline-flex items-center gap-1.5 text-slate-700 cursor-pointer font-medium">
            <input
              type="checkbox"
              data-testid="review-include-all-checkbox"
              checked={showAll}
              onChange={(e) => { setOffset(0); setShowAll(e.target.checked); }}
            />
            <span>{t.review.includeAllToggle}</span>
          </label>
        </div>

        <button
          type="button"
          onClick={() => loadQueue().catch((e) => onStatusMessage(String(e), true))}
          className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg border border-slate-200 hover:bg-slate-50 font-semibold text-slate-700 cursor-pointer"
        >
          <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
          <span>{t.controls.refresh}</span>
        </button>
      </div>

      <nav aria-label={t.review.queuePages} className="flex flex-wrap items-center justify-between gap-2 text-sm">
        <p role="status" data-testid="review-page-range">
          {loadedQuery === query
            ? t.review.pageRange.replace('{start}', String(total ? offset + 1 : 0))
              .replace('{end}', String(offset + items.length)).replace('{total}', String(total))
            : t.controls.loading}
        </p>
        <div className="flex gap-2">
          <button type="button" data-testid="review-page-prev" disabled={!offset || busyBulk !== null}
            onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
            className="rounded-lg border border-slate-300 px-3 py-1.5 disabled:opacity-40">{t.review.previousPage}</button>
          <button type="button" data-testid="review-page-next" disabled={loadedQuery !== query || offset + PAGE_SIZE >= total || busyBulk !== null}
            onClick={() => setOffset(offset + PAGE_SIZE)}
            className="rounded-lg border border-slate-300 px-3 py-1.5 disabled:opacity-40">{t.review.nextPage}</button>
        </div>
      </nav>

      {/* Workflow Counts Summary Strip */}
      {items.length > 0 && workflowCounts && (
        <WorkflowSummaryStrip counts={workflowCounts} showImportNotSendNotice={items.some(item => item.import_origin === 'gemini_zip')} />
      )}

      {/* Bulk Approve / Bulk Send Header & Status Filters */}
      {items.length > 0 && (
        <div className="space-y-2.5 p-3.5 rounded-xl bg-white border border-slate-200 text-xs">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="text-slate-700">
              {t.review.pageSelection} ({visibleItems.length})
            </div>
            <div className="flex flex-wrap gap-2">
              {queuedUnsentIds.length > 0 && (
                <button
                  type="button"
                  disabled={busyBulk !== null}
                  data-testid="cancel-all-queued-btn"
                  onClick={handleCancelAllQueued}
                  className="inline-flex items-center gap-1 px-3 py-1.5 rounded-lg border border-rose-300 bg-white text-rose-700 hover:bg-rose-50 font-semibold cursor-pointer disabled:opacity-40"
                >
                  <XCircle className="h-3.5 w-3.5" aria-hidden="true" />
                  <span>{t.daily.cancelUnsent} ({queuedUnsentIds.length})</span>
                </button>
              )}
              <button
                type="button"
                disabled={busyBulk !== null || readyItems.length === 0}
                data-testid="review-bulk-approve-ready-btn"
                onClick={handleBulkApproveReady}
                className="inline-flex items-center gap-1 px-3 py-1.5 rounded-lg bg-emerald-600 hover:bg-emerald-700 text-white font-semibold cursor-pointer disabled:opacity-40"
              >
                <CheckCircle2 className="h-3.5 w-3.5" aria-hidden="true" />
                <span>{t.daily.approveReady} ({readyItems.length})</span>
              </button>
              <button
                type="button"
                disabled={busyBulk !== null || approvedIds.length === 0 || !liveEnabled}
                data-testid="review-bulk-send-approved-btn"
                onClick={handleBulkSendApproved}
                className="inline-flex items-center gap-1 px-3 py-1.5 rounded-lg bg-slate-900 hover:bg-slate-800 text-white font-semibold cursor-pointer disabled:opacity-40"
              >
                <Send className="h-3.5 w-3.5" aria-hidden="true" />
                <span>{t.review.sendShownApproved} ({approvedIds.length})</span>
              </button>
            </div>
          </div>

          <div role="group" aria-label={t.review.filterPage} className="flex flex-wrap items-center gap-1.5 pt-1 border-t border-slate-100">
            <span>{t.review.filterPage}</span>
            {filterTabs.map((ft) => {
              const cnt = items.filter((d) => matchesFilter(d, ft.id)).length;
              return (
                <button
                  key={ft.id}
                  type="button"
                  data-testid={`review-filter-${ft.id}`}
                  aria-pressed={statusFilter === ft.id}
                  onClick={() => setStatusFilter(ft.id)}
                  className={`px-2.5 py-1 rounded-full border cursor-pointer ${
                    statusFilter === ft.id
                      ? 'border-indigo-600 bg-indigo-50 text-indigo-900 font-semibold'
                      : 'border-slate-200 bg-white text-slate-700 hover:bg-slate-50'
                  }`}
                >
                  {ft.label} ({cnt})
                </button>
              );
            })}
          </div>
        </div>
      )}

      {query !== loadedQuery ? null : visibleItems.length === 0 ? (
        <div className="bg-white rounded-xl border border-slate-200 p-8 text-center text-sm text-slate-500">
          {t.daily.empty} <Link to="/batches" className="font-semibold text-indigo-700 hover:underline">{t.daily.find}</Link>
        </div>
      ) : (
        <div className="space-y-3">
          {visibleItems.map((item, idx) => (
            <PreparedApplicationCard
              key={item.id}
              item={item}
              index={idx}
              cvs={cvs}
              liveEnabled={liveEnabled}
              showGmailDraftButton={true}
              onChanged={loadQueue}
              onFeedback={onStatusMessage}
              onRequestSendConfirm={(it) => setSendModalItems([it])}
            />
          ))}
        </div>
      )}

      <SendApprovedConfirmModal
        open={sendModalItems.length > 0}
        items={sendModalItems}
        onClose={() => { setSendModalItems([]); loadQueue().catch(e => onStatusMessage(String(e), true)); }}
        onCompleted={async (summaryText, isError) => {
          await loadQueue();
          onStatusMessage(summaryText, isError);
        }}
      />
    </div>
  );
};
