import React, { useEffect, useRef, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import {
  AlertTriangle,
  Archive,
  CalendarClock,
  CheckCircle2,
  ExternalLink,
  FileArchive,
  Pause,
  Play,
  RefreshCw,
  Send,
  Trash2,
  Upload,
  XCircle,
} from 'lucide-react';
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
  type ImportBundleResponse,
} from '../lib/workflowTypes';
import { WorkflowSummaryStrip } from '../components/WorkflowSummaryStrip';
import { PreparedApplicationCard } from '../components/PreparedApplicationCard';
import { SendApprovedConfirmModal } from '../components/SendApprovedConfirmModal';

const DAY_LABELS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

type AppStatusFilter = 'all' | 'ready' | 'needs_review' | 'approved' | 'queued' | 'duplicate_blocked' | 'accepted' | 'failed' | 'uncertain';

export const BatchesPage: React.FC = () => {
  const { t, formatDateTime } = useI18n();
  const [searchParams, setSearchParams] = useSearchParams();
  const [batches, setBatches] = useState<any[]>([]);
  const [selectedBatch, setSelectedBatch] = useState<any | null>(null);
  const [schedules, setSchedules] = useState<any[]>([]);
  const [cvs, setCvs] = useState<CvDocumentSummary[]>([]);
  const [mode, setMode] = useState<'dry_run' | 'approval_required'  | 'demo_fixture'>('approval_required');
  const [targetGoalCount, setTargetGoalCount] = useState<string>('');
  const [maxSearchQueries, setMaxSearchQueries] = useState<string>('');
  const [includeStandingInvitations, setIncludeStandingInvitations] = useState('');
  const [draftActionMsg, setDraftActionMsg] = useState<string>('');
  const [draftActionErr, setDraftActionErr] = useState<string>('');
  const [appStatusFilter, setAppStatusFilter] = useState<AppStatusFilter>('all');
  const [categoryFilter, setCategoryFilter] = useState<string>('all');
  const [skipCodeFilter, setSkipCodeFilter] = useState<string>('all');
  const [showSources, setShowSources] = useState<boolean>(false);
  const [showPaste, setShowPaste] = useState(false);
  const [pasteTitle, setPasteTitle] = useState('');
  const [pasteCompany, setPasteCompany] = useState('');
  const [pasteUrl, setPasteUrl] = useState('');
  const [pasteLocation, setPasteLocation] = useState('');
  const [pasteText, setPasteText] = useState('');
  const [zipFile, setZipFile] = useState<File | null>(null);
  const [importingZip, setImportingZip] = useState(false);
  const [importResult, setImportResult] = useState<ImportBundleResponse | null>(null);
  const [importErr, setImportErr] = useState('');
  const zipInputRef = useRef<HTMLInputElement | null>(null);
  const [schedName, setSchedName] = useState('Weekday Morning Discovery');
  const [schedMode, setSchedMode] = useState<'dry_run' | 'live'>('dry_run');
  const [schedTime, setSchedTime] = useState('09:00');
  const [schedTz, setSchedTz] = useState(() => Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC');
  const [schedDays, setSchedDays] = useState<number[]>([0, 1, 2, 3, 4]);
  const [err, setErr] = useState('');
  const [busy, setBusy] = useState(false);
  const [sendModalItems, setSendModalItems] = useState<DraftApplicationItem[] | null>(null);

  const loadSequence = useRef(0);
  const [pollError, setPollError] = useState(false);
  const loadAll = async (preferredBatchId?: string | null) => {
    const sequence = ++loadSequence.current;
    const [bRes, sRes, cvRes] = await Promise.all([
      apiRequest<{ batches: any[] }>('/batches'),
      apiRequest<{ schedules: any[] }>('/schedules'),
      apiRequest<{ cvs: CvDocumentSummary[] }>('/cvs').catch(() => ({ cvs: [] })),
    ]);
    if (sequence !== loadSequence.current) return;
    const list = bRes.batches || [];
    setBatches(list);
    setSchedules(sRes.schedules || []);
    setCvs(cvRes.cvs || []);
    const targetId = preferredBatchId || searchParams.get('selected') || list[0]?.id;
    if (targetId) {
      const detail = await apiRequest(`/batches/${targetId}`);
      if (sequence === loadSequence.current) {
        setSelectedBatch(detail);
        setPollError(false);
        if (searchParams.get('selected') !== targetId) {
          setSearchParams(previous => { previous.set('selected', targetId); return previous; }, { replace: true });
        }
      }
    }
  };

  useEffect(() => {
    loadAll().catch((e) => setErr(String(e)));
  }, []);

  const batch = selectedBatch?.batch;
  const stageKey = batch?.stage || batch?.status;
  const stageLabel = t.daily.stages[stageKey as keyof typeof t.daily.stages] || stageKey;
  const progress = batch?.progress_counts;
  const isImport = batch?.mode === 'imported_bundle' || batch?.import_origin === 'gemini_zip';
  const fetchAttempts = batch?.budget_usage?.pages_fetched ?? batch?.metrics?.pages_fetched;
  const loggedPrimaryFetches: number[] = (batch?.sources_searched ?? [])
    .map((source: { successful_fetches?: number }) => source.successful_fetches)
    .filter((count: unknown) => typeof count === 'number');
  const duplicateReasons = new Map<string, number>();
  for (const sighting of selectedBatch?.sightings ?? []) {
    if (!sighting.is_duplicate) continue;
    const reason = sighting.dedupe_reason ?? '';
    const label = reason.startsWith('prior_batch_') ? t.batches.seenEarlier
      : reason === 'duplicate_in_batch' ? t.batches.repeatedHere
      : reason === 'duplicate_normalized_vacancy' ? t.batches.matchingVacancy : t.batches.otherDuplicates;
    duplicateReasons.set(label, (duplicateReasons.get(label) ?? 0) + 1);
  }
  const activity = batch ? [
    [t.daily.queries, progress?.queries_executed],
    [t.daily.pages, fetchAttempts],
    [t.daily.primaryFetches, loggedPrimaryFetches.length ? loggedPrimaryFetches.reduce((sum, n) => sum + n, 0) : undefined],
    [t.daily.openings, progress?.openings_discovered ?? batch.discovered_count],
    [t.daily.suitable, batch.eligible_count],
    [t.daily.duplicates, batch.duplicates_removed_count],
    [t.daily.contacted, batch.skip_reason_counts?.PREVIOUSLY_CONTACTED ?? 0],
    [t.daily.prepared, batch.drafted_count],
    [t.daily.attention, selectedBatch.workflow_counts?.needs_review],
  ] : [];
  const active = ['queued', 'running', 'recovering'].includes(batch?.status);
  const activeOutbox = (selectedBatch?.all_applications || []).some((item: DraftApplicationItem) => isQueuedInOutbox(item) || isSendingInOutbox(item));
  useEffect(() => {
    if (!batch?.id || (!active && !activeOutbox) || sendModalItems?.length) return;
    const controller = new AbortController();
    let timer: number;
    async function poll() {
      const sequence = loadSequence.current;
      try {
        const detail = await apiRequest(`/batches/${batch.id}`, { signal: controller.signal });
        if (controller.signal.aborted || sequence !== loadSequence.current) return;
        setSelectedBatch(detail);
        setBatches(list => list.map(row => row.id === batch.id ? detail.batch : row));
        setPollError(false);
      } catch {
        if (!controller.signal.aborted) setPollError(true);
      } finally {
        if (!controller.signal.aborted) timer = window.setTimeout(poll, 3500);
      }
    }
    timer = window.setTimeout(poll, 2000);
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [batch?.id, active, activeOutbox, sendModalItems]);

  const handleImportGeminiZip = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!zipFile) return;
    setImportingZip(true);
    setImportErr('');
    setImportResult(null);
    setDraftActionMsg('');
    setDraftActionErr('');
    try {
      const formData = new FormData();
      formData.append('file', zipFile);
      const res = await apiRequest<ImportBundleResponse>('/batches/import-bundle', {
        method: 'POST',
        body: formData,
      });
      setImportResult(res);
      setZipFile(null);
      if (zipInputRef.current) {
        zipInputRef.current.value = '';
      }
      setAppStatusFilter('all');
      await loadAll(res.batch_id);
    } catch (e2) {
      setImportErr(e2 instanceof ApiError ? e2.message : 'Unable to import ZIP bundle');
    } finally {
      setImportingZip(false);
    }
  };

  const handleStartBatch = async (e?: React.FormEvent) => {
    e?.preventDefault();
    const runMode = e ? mode : 'approval_required';
    setBusy(true);
    setErr('');
    try {
      const body: any = {
        mode: runMode === 'approval_required' ? 'live' : runMode,
        run_mode: runMode,
        run_immediately: true,
      };
      if (e && includeStandingInvitations) body.include_standing_invitations = includeStandingInvitations === 'yes';
      if (e && targetGoalCount.trim()) {
        body.target_goal_count = Number(targetGoalCount);
      }
      if (e && maxSearchQueries.trim()) {
        body.max_search_queries = Number(maxSearchQueries);
      }
      if (e && showPaste && pasteText.trim().length >= 20) {
        body.pasted_posting = {
          title: pasteTitle || 'Public Job Posting',
          company_name: pasteCompany,
          source_url: pasteUrl,
          location_raw: pasteLocation,
          raw_text: pasteText,
        };
      }
      const res = await apiRequest<{ batch_id: string }>('/batches', {
        method: 'POST',
        idempotencyKey: `ui-batch-${Date.now()}`,
        body: JSON.stringify(body),
      });
      await loadAll(res.batch_id);
    } catch (e2) {
      setErr(e2 instanceof ApiError ? e2.message : 'Unable to start batch');
    } finally {
      setBusy(false);
    }
  };

  const handleBatchAction = async (batchId: string, action: 'pause' | 'resume' | 'cancel') => {
    setBusy(true);
    setErr('');
    try {
      await apiRequest(`/batches/${batchId}/${action}`, { method: 'POST' });
      await loadAll(batchId);
    } catch (error) {
      setErr(String(error));
    } finally {
      setBusy(false);
    }
  };

  const handleCreateSchedule = async (e: React.FormEvent) => {
    e.preventDefault();
    setErr('');
    try {
      await apiRequest('/schedules', {
        method: 'POST',
        body: JSON.stringify({
          name: schedName,
          mode: schedMode,
          days_of_week: schedDays,
          local_time_hhmm: schedTime,
          timezone: schedTz,
          is_active: true,
        }),
      });
      await loadAll();
    } catch (e2) {
      setErr(e2 instanceof ApiError ? e2.message : 'Failed to create schedule');
    }
  };

  return (
    <div className="space-y-8 max-w-full overflow-x-hidden">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-bold text-slate-900">{t.batches.title}</h1>
        <button
          type="button"
          onClick={() => loadAll(selectedBatch?.batch?.id).catch(error => setErr(String(error)))}
          className="inline-flex items-center gap-1.5 px-3 py-2 rounded-lg border border-slate-200 bg-white text-xs font-semibold hover:bg-slate-50 cursor-pointer"
        >
          <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
          <span>{t.controls.refresh}</span>
        </button>
      </div>

      {err && <div role="alert" className="p-3 rounded-lg bg-rose-50 border border-rose-200 text-rose-800 text-sm">{err}</div>}

      {/* Start Batch Form */}
      <section className="bg-white rounded-xl border border-slate-200 p-5 shadow-xs space-y-4">
        <h2 className="text-base font-semibold text-slate-900">{t.daily.find}</h2>
        <p className="text-sm text-slate-600">{t.daily.intro}</p>
        <button type="button" data-testid="find-and-prepare" disabled={busy} onClick={() => handleStartBatch()}
          className="rounded-lg bg-indigo-600 px-4 py-2 font-semibold text-white hover:bg-indigo-700 disabled:opacity-50">
          {busy ? t.daily.waiting : t.daily.find}
        </button>
        <details>
          <summary className="cursor-pointer text-sm font-medium text-slate-700">{t.daily.options}</summary>
        <form onSubmit={handleStartBatch} className="mt-4 space-y-4">
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-3">
            {[
              { id: 'dry_run', label: t.batches.modeDryRun },
              { id: 'approval_required', label: t.batches.modeApprovalRequired },
              { id: 'demo_fixture', label: t.batches.modeDemo },
            ].map((opt) => (
              <label
                key={opt.id}
                className={`p-3 rounded-xl border cursor-pointer text-sm flex items-start gap-2 ${
                  mode === opt.id ? 'border-indigo-600 bg-indigo-50/70' : 'border-slate-200 hover:bg-slate-50'
                }`}
              >
                <input
                  type="radio"
                  name="batch_mode"
                  checked={mode === opt.id}
                  onChange={() => setMode(opt.id as any)}
                  className="mt-1"
                />
                <span className="font-medium text-slate-800">{opt.label}</span>
              </label>
            ))}
          </div>

          <div className="flex flex-wrap items-center gap-4">
            <label className="inline-flex items-center gap-2 text-sm text-slate-700 cursor-pointer">
              {t.preferences.standingInvites}
              <select value={includeStandingInvitations} onChange={e => setIncludeStandingInvitations(e.target.value)}>
                <option value="">{t.daily.defaultPref}</option>
                <option value="yes">{t.daily.yes}</option>
                <option value="no">{t.daily.no}</option>
              </select>
            </label>
            <label className="inline-flex items-center gap-2 text-sm text-slate-700 cursor-pointer">
              <input type="checkbox" checked={showPaste} onChange={(e) => setShowPaste(e.target.checked)} />
              <span>{t.batches.pasteToggle}</span>
            </label>
            <div className="flex items-center gap-2 text-xs text-slate-700">
              <label htmlFor="batch-goal-count">{t.daily.requestedCount}</label>
              <input
                id="batch-goal-count"
                type="number"
                min={1}
                max={50}
                placeholder={t.daily.defaultPref}
                value={targetGoalCount}
                onChange={(e) => setTargetGoalCount(e.target.value)}
                className="w-28 rounded-lg border border-slate-300 px-2.5 py-1 text-xs"
              />
            </div>
            <div className="flex items-center gap-2 text-xs text-slate-700">
              <label htmlFor="batch-max-queries">{t.batches.queryBudgetLabel}:</label>
              <input
                id="batch-max-queries"
                type="number"
                min={1}
                max={30}
                placeholder={t.daily.defaultPref}
                value={maxSearchQueries}
                onChange={(e) => setMaxSearchQueries(e.target.value)}
                className="w-28 rounded-lg border border-slate-300 px-2.5 py-1 text-xs"
              />
            </div>
          </div>

          {showPaste && (
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3 p-4 rounded-xl bg-slate-50 border border-slate-200">
              <input
                type="text"
                placeholder="Role title"
                value={pasteTitle}
                onChange={(e) => setPasteTitle(e.target.value)}
                className="rounded-lg border border-slate-300 px-3 py-2 text-sm bg-white"
              />
              <input
                type="text"
                placeholder="Company name"
                value={pasteCompany}
                onChange={(e) => setPasteCompany(e.target.value)}
                className="rounded-lg border border-slate-300 px-3 py-2 text-sm bg-white"
              />
              <input
                type="url"
                placeholder="Public job URL (optional)"
                value={pasteUrl}
                onChange={(e) => setPasteUrl(e.target.value)}
                className="rounded-lg border border-slate-300 px-3 py-2 text-sm bg-white"
              />
              <input
                type="text"
                placeholder="Location (e.g. Dubai, UAE)"
                value={pasteLocation}
                onChange={(e) => setPasteLocation(e.target.value)}
                className="rounded-lg border border-slate-300 px-3 py-2 text-sm bg-white"
              />
              <textarea
                required minLength={20} rows={4}
                placeholder="Paste full public job posting text including explicit CV email instructions..."
                value={pasteText}
                onChange={(e) => setPasteText(e.target.value)}
                className="md:col-span-2 rounded-lg border border-slate-300 px-3 py-2 text-sm bg-white"
              />
            </div>
          )}

          <button
            type="submit"
            disabled={busy}
            className="inline-flex items-center gap-2 px-5 py-2.5 rounded-lg bg-indigo-600 hover:bg-indigo-700 text-white text-sm font-semibold cursor-pointer disabled:opacity-50"
          >
            <Play className="h-4 w-4" aria-hidden="true" />
            <span>{busy ? t.controls.loading : t.batches.startBtn}</span>
          </button>
        </form>
        </details>
      </section>

      <details className="rounded-xl border border-slate-200 bg-white p-3">
        <summary className="cursor-pointer text-sm font-semibold text-slate-700">{t.batches.importZipTitle}</summary>
      {/* Import Prepared Applications (Gemini ZIP) */}
      <section
        data-testid="import-gemini-zip-section"
        className="bg-white rounded-xl border border-indigo-200 p-5 shadow-xs space-y-4"
      >
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="space-y-1">
            <div className="flex items-center gap-2">
              <FileArchive className="h-5 w-5 text-indigo-600" aria-hidden="true" />
              <h2 className="text-base font-semibold text-slate-900">{t.batches.importZipTitle}</h2>
              <span className="px-2 py-0.5 rounded-full bg-indigo-50 text-indigo-800 border border-indigo-200 text-[11px] font-semibold">
                {t.batches.geminiZipBadge}
              </span>
            </div>
            <p className="text-xs text-slate-600 max-w-3xl">{t.batches.importZipSubtitle}</p>
          </div>
        </div>

        <form onSubmit={handleImportGeminiZip} className="flex flex-wrap items-center gap-3">
          <label className="inline-flex items-center gap-2 px-3.5 py-2 rounded-lg border border-slate-300 bg-slate-50 hover:bg-slate-100 text-xs font-semibold text-slate-800 cursor-pointer">
            <Upload className="h-3.5 w-3.5 text-indigo-600" aria-hidden="true" />
            <span>{zipFile ? zipFile.name : t.batches.importZipLabel}</span>
            <input
              ref={zipInputRef}
              type="file"
              accept=".zip,application/zip"
              data-testid="import-gemini-zip-input"
              onChange={(e) => setZipFile(e.target.files?.[0] || null)}
              className="sr-only"
            />
          </label>
          <button
            type="submit"
            disabled={!zipFile || importingZip}
            data-testid="import-gemini-zip-btn"
            className="inline-flex items-center gap-1.5 px-4 py-2 rounded-lg bg-indigo-600 hover:bg-indigo-700 text-white text-xs font-semibold cursor-pointer disabled:opacity-50"
          >
            <Archive className="h-3.5 w-3.5" aria-hidden="true" />
            <span>{importingZip ? t.batches.importingBtn : t.batches.importZipBtn}</span>
          </button>
        </form>

        {importErr && (
          <div role="alert" className="p-3 rounded-lg bg-rose-50 border border-rose-200 text-rose-800 text-xs">
            {importErr}
          </div>
        )}

        {importResult && (
          <div
            data-testid="import-bundle-result"
            className="rounded-xl border border-indigo-200 bg-indigo-50/40 p-4 space-y-3 text-xs"
          >
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-bold text-slate-900">
                  {t.review.batchFilterLabel} {importResult.batch_id.slice(0, 8)} · {t.batches.recordsCreated} ({importResult.created_applications})
                </span>
                {importResult.duplicate_import && (
                  <span
                    data-testid="duplicate-import-badge"
                    className="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full bg-amber-100 text-amber-900 border border-amber-300 font-semibold text-[11px]"
                  >
                    <AlertTriangle className="h-3 w-3" aria-hidden="true" />
                    <span>{t.batches.duplicateImportBadge}</span>
                  </span>
                )}
              </div>
              <Link
                to={`/tracker?tab=review&batch_id=${importResult.batch_id}`}
                className="inline-flex items-center gap-1 px-2.5 py-1 rounded-lg border border-indigo-300 bg-white text-indigo-700 font-semibold hover:bg-indigo-50"
              >
                <span>{t.batches.openReviewQueue}</span>
              </Link>
            </div>

            {importResult.message && <p className="text-slate-700">{importResult.message}</p>}

            <WorkflowSummaryStrip counts={importResult.workflow_counts} />

            {importResult.ignored_files && importResult.ignored_files.length > 0 && (
              <details
                open
                data-testid="ignored-bundle-files"
                className="rounded-lg border border-amber-200 bg-amber-50/80 p-2.5 text-amber-950"
              >
                <summary className="font-semibold cursor-pointer">
                  {t.batches.ignoredFilesTitle} ({importResult.ignored_files.length})
                </summary>
                <ul className="mt-1.5 space-y-1 ps-4 list-disc">
                  {importResult.ignored_files.map((ig, idx) => (
                    <li key={`${ig.filename}-${idx}`}>
                      <code className="font-mono font-semibold">{ig.filename}</code> — {ig.reason}
                    </li>
                  ))}
                </ul>
              </details>
            )}
          </div>
        )}
      </section>

      </details>

      {/* Batch History & Selected Batch Activity */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <section className="bg-white rounded-xl border border-slate-200 p-4 shadow-xs space-y-3">
          <h2 className="text-sm font-semibold text-slate-900">{t.daily.history}</h2>
          <div className="space-y-2 max-h-96 overflow-y-auto">
            {batches.map((b) => {
              const isGeminiBundle = b.mode === 'imported_bundle' || b.import_origin === 'gemini_zip';
              return (
                <button
                  key={b.id}
                  type="button"
                  onClick={() => loadAll(b.id).catch(error => setErr(String(error)))}
                  className={`w-full text-start p-3 rounded-lg border cursor-pointer ${
                    selectedBatch?.batch?.id === b.id ? 'border-indigo-600 bg-indigo-50/50' : 'border-slate-200 hover:bg-slate-50'
                  }`}
                >
                  <div className="flex flex-wrap items-center justify-between gap-1.5 text-xs font-semibold">
                    <span className="inline-flex items-center gap-1.5">
                      <span>{b.mode} · {b.status}</span>
                      {isGeminiBundle && (
                        <span className="px-1.5 py-0.5 rounded bg-indigo-100 text-indigo-800 text-[10px] font-bold">
                          {t.batches.geminiZipBadge}
                        </span>
                      )}
                    </span>
                    <span className="text-slate-500">{formatDateTime(b.created_at)}</span>
                  </div>
                  <div className="text-xs text-slate-600 mt-1">
                    {t.daily.openings} ({b.discovered_count ?? 0}) · {t.daily.prepared} ({b.drafted_count ?? 0})
                  </div>
                </button>
              );
            })}
          </div>
        </section>

        <section className="order-first min-w-0 lg:order-none lg:col-span-2 bg-white rounded-xl border border-slate-200 p-5 shadow-xs space-y-4">
          {selectedBatch?.batch ? (
            <>
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div>
                  <div className="flex flex-wrap items-center gap-2">
                    <h2 className="text-base font-semibold text-slate-900">
                      {t.review.batchFilterLabel} {batch.id.slice(0, 8)} · {stageLabel}
                    </h2>
                    {isImport ? (
                      <span className="px-2 py-0.5 rounded-full bg-indigo-50 text-indigo-800 border border-indigo-200 text-xs font-semibold">
                        {t.batches.geminiZipBadge}
                      </span>
                    ) : <span className="text-xs text-slate-600">{t.batches.discoveryBatch}</span>}
                  </div>
                  <p className="text-xs text-slate-500 mt-0.5">{selectedBatch.batch.coverage_note}</p>
                </div>
                <div className="flex gap-2">
                  <button
                    type="button"
                    disabled={busy || !active}
                    onClick={() => handleBatchAction(selectedBatch.batch.id, 'pause')}
                    className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded border border-slate-200 text-xs font-medium hover:bg-slate-50 cursor-pointer"
                  >
                    <Pause className="h-3.5 w-3.5" aria-hidden="true" />
                    <span>{t.batches.pause}</span>
                  </button>
                  <button
                    type="button"
                    disabled={busy || !['paused', 'quota_paused'].includes(batch.status)}
                    onClick={() => handleBatchAction(selectedBatch.batch.id, 'resume')}
                    className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded border border-slate-200 text-xs font-medium hover:bg-slate-50 cursor-pointer"
                  >
                    <Play className="h-3.5 w-3.5" aria-hidden="true" />
                    <span>{t.batches.resume}</span>
                  </button>
                  <button
                    type="button"
                    disabled={busy || (!active && !['paused', 'quota_paused'].includes(batch.status))}
                    onClick={() => handleBatchAction(selectedBatch.batch.id, 'cancel')}
                    className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded border border-rose-200 text-rose-700 text-xs font-medium hover:bg-rose-50 cursor-pointer"
                  >
                    <XCircle className="h-3.5 w-3.5" aria-hidden="true" />
                    <span>{t.batches.cancel}</span>
                  </button>
                </div>
              </div>

              {!isImport && <section className="space-y-2" data-testid="batch-progress">
                <h3 className="text-sm font-semibold">{active ? t.daily.progress : t.daily.result}</h3>
                <p role="status" className="text-xs text-slate-700">{stageLabel}
                  {progress?.candidates_found > 0 && ` · ${t.daily.checked} ${progress.candidates_processed ?? 0}/${progress.candidates_found}`}
                </p>
                <dl className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                  {activity.map(([label, count]) => <div key={label} className="rounded-lg border border-slate-200 px-3 py-2">
                    <dt className="text-xs text-slate-600">{label}</dt><dd className="font-semibold tabular-nums">{count ?? '—'}</dd>
                  </div>)}
                </dl>
              </section>}
              {pollError && <p role="status" className="text-sm text-amber-800">{t.daily.refreshFail}</p>}
              {batch.error_message && <p role="alert" className="text-sm text-rose-800">{batch.error_message}</p>}
              {selectedBatch.workflow_counts && (
                <WorkflowSummaryStrip counts={selectedBatch.workflow_counts} isDiscoveryBatch={!isImport} />
              )}

              {selectedBatch.batch.pause_reason && (
                <div className="p-3 rounded-lg bg-amber-50 border border-amber-200 text-xs text-amber-900">
                  {selectedBatch.batch.pause_reason}
                </div>
              )}

              {selectedBatch.batch.goal_shortfall_reason && (
                <div className="p-3 rounded-lg bg-indigo-50 border border-indigo-200 text-xs text-indigo-900">
                  <strong>{t.daily.result} ·</strong> {selectedBatch.batch.goal_shortfall_reason}
                </div>
              )}

              {(selectedBatch.batch.stage_status || []).length > 0 && (
                <div className="flex flex-wrap gap-1.5 text-[11px]">
                  {selectedBatch.batch.stage_status.map((st: any, idx: number) => (
                    <span
                      key={idx}
                      className="px-2.5 py-1 rounded-full bg-emerald-50 border border-emerald-200 text-emerald-800 font-medium"
                    >
                      {idx + 1}. {st.stage} ({st.status})
                    </span>
                  ))}
                </div>
              )}

              {selectedBatch.batch.source_errors?.length > 0 && (
                <div className="p-3 rounded-lg bg-rose-50 border border-rose-200 text-xs text-rose-800 space-y-1">
                  {selectedBatch.batch.source_errors.map((se: any, i: number) => (
                    <div key={i}>
                      Source {se.provider} {se.code ? `[${se.code}]` : ''}: {se.error}
                    </div>
                  ))}
                </div>
              )}

              {(() => {
                const records: DraftApplicationItem[] = selectedBatch.all_applications ?? selectedBatch.applications ?? selectedBatch.draft_applications ?? [];
                if (!records.length) return batch.status === 'completed' ? <p className="text-sm text-slate-700">{t.daily.preparedEmpty}</p> : null;
                const isExcluded = (row: DraftApplicationItem) =>
                  isDuplicateItem(row) || isRejectedOrBlocked(row) || row.queue_state === 'blocked';
                const prepared = records.filter(row => !isExcluded(row));
                const excluded = records.filter(isExcluded);
                const readyToApprove = prepared.filter(canBulkApproveReady);
                const readyToSend = prepared.filter(canSendApprovedItem);

                const matchesFilter = (d: DraftApplicationItem, filter: AppStatusFilter): boolean => {
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

                const filterTabs: { id: AppStatusFilter; label: string }[] = [
                  { id: 'all', label: t.batches.allPreparedTitle },
                  { id: 'ready', label: t.batches.filterReady },
                  { id: 'needs_review', label: t.batches.filterNeedsReview },
                  { id: 'approved', label: t.batches.filterApproved },
                  { id: 'queued', label: t.batches.filterQueued },
                  { id: 'duplicate_blocked', label: t.batches.excludedRecords },
                  { id: 'accepted', label: t.batches.filterAccepted },
                  { id: 'failed', label: t.batches.filterFailed },
                  { id: 'uncertain', label: t.batches.filterUncertain },
                ];

                const rowsForFilter = (filter: AppStatusFilter) =>
                  filter === 'duplicate_blocked' ? excluded : prepared.filter(row => matchesFilter(row, filter));
                const visibleRows = rowsForFilter(appStatusFilter);

                return (
                  <div className="rounded-xl border border-emerald-200 bg-emerald-50/40 p-4 space-y-3 text-xs">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <div>
                        <h3 className="text-sm font-bold text-slate-900">
                          {t.batches.applicationRecords} ({records.length})
                        </h3>
                        <p className="text-[11px] text-slate-600">
                          {prepared.length > 0 ? t.daily.proof : t.batches.excludedHint}
                        </p>
                      </div>
                      <div className="flex flex-wrap gap-2">
                        <button
                          type="button"
                          disabled={readyToApprove.length === 0}
                          data-testid="batch-approve-all-ready-btn"
                          onClick={async () => {
                            setDraftActionErr('');
                            setDraftActionMsg('');
                            try {
                              const res = await apiRequest<DraftApprovalResponse>('/drafts/approve', {
                                method: 'POST',
                                body: JSON.stringify(reviewedDraftSelection(readyToApprove)),
                              });
                              setDraftActionMsg(`${t.workflow.approved} (${res.approved_ids.length}/${readyToApprove.length})`);
                              setDraftActionErr(res.failed.map(row => row.error).join(' · '));
                              await loadAll(selectedBatch.batch.id);
                            } catch (e2) {
                              setDraftActionErr(e2 instanceof ApiError ? e2.message : 'Approval failed');
                            }
                          }}
                          className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg bg-emerald-600 hover:bg-emerald-700 text-white font-semibold cursor-pointer disabled:opacity-40"
                        >
                          <CheckCircle2 className="h-3.5 w-3.5" aria-hidden="true" />
                          <span>{t.daily.approveReady} ({readyToApprove.length})</span>
                        </button>
                        <button
                          type="button"
                          disabled={readyToSend.length === 0 || !selectedBatch.live_sending_enabled}
                          data-testid="batch-send-all-approved-btn"
                          onClick={() => {
                            setDraftActionErr('');
                            setDraftActionMsg('');
                            setSendModalItems(readyToSend);
                          }}
                          className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg bg-slate-900 hover:bg-slate-800 text-white font-semibold cursor-pointer disabled:opacity-40"
                        >
                          <Send className="h-3.5 w-3.5" aria-hidden="true" />
                          <span>{t.daily.sendAll} ({readyToSend.length})</span>
                        </button>
                        <Link
                          to={`/tracker?tab=review&batch_id=${selectedBatch.batch.id}`}
                          className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg border border-indigo-200 bg-white text-indigo-700 font-semibold hover:bg-indigo-50"
                        >
                          {t.batches.openReviewQueue}
                        </Link>
                      </div>
                    </div>

                    {!selectedBatch.live_sending_enabled && (
                      <div className="p-2.5 rounded-lg bg-amber-50 border border-amber-200 text-amber-900 flex flex-wrap items-center justify-between gap-2">
                        <span>{t.daily.sendingLocked}</span>
                        <Link to="/settings" className="font-semibold underline">{t.nav.settings}</Link>
                      </div>
                    )}
                    {draftActionMsg && <div className="p-2 rounded bg-emerald-100 text-emerald-900 font-medium">{draftActionMsg}</div>}
                    {draftActionErr && <div className="p-2 rounded bg-rose-100 text-rose-900 font-medium">{draftActionErr}</div>}

                    {/* Filter Tabs across Prepared Applications */}
                    <div className="flex flex-wrap gap-1.5">
                      {filterTabs.map((ft) => {
                        const cnt = rowsForFilter(ft.id).length;
                        return (
                          <button
                            key={ft.id}
                            type="button"
                            data-testid={`batch-app-filter-${ft.id}`}
                            aria-pressed={appStatusFilter === ft.id}
                            onClick={() => setAppStatusFilter(ft.id)}
                            className={`px-2.5 py-1 rounded-full border cursor-pointer ${
                              appStatusFilter === ft.id
                                ? 'border-indigo-600 bg-indigo-50 text-indigo-900 font-semibold'
                                : 'border-slate-200 bg-white text-slate-700 hover:bg-slate-50'
                            }`}
                          >
                            {ft.label} ({cnt})
                          </button>
                        );
                      })}
                    </div>

                    <div className="space-y-2.5">
                      {!visibleRows.length && <p className="text-sm text-slate-700">
                        {appStatusFilter === 'all' && batch.drafted_count === 0 ? t.daily.preparedEmpty : t.daily.empty}
                      </p>}
                      {visibleRows.map((d, idx) => (
                        <PreparedApplicationCard
                          key={d.id}
                          item={d}
                          index={idx}
                          cvs={cvs}
                          liveEnabled={Boolean(selectedBatch.live_sending_enabled)}
                          onChanged={() => loadAll(selectedBatch.batch.id)}
                          onRequestSendConfirm={(singleItem) => {
                            setDraftActionErr('');
                            setDraftActionMsg('');
                            setSendModalItems([singleItem]);
                          }}
                          onFeedback={(m, isError) => {
                            if (isError) {
                              setDraftActionErr(m);
                              setDraftActionMsg('');
                            } else {
                              setDraftActionMsg(m);
                              setDraftActionErr('');
                            }
                          }}
                        />
                      ))}
                    </div>
                  </div>
                );
              })()}

              {/* 12 Distinct Diagnostic Counters & Analytics */}
              <div className="rounded-xl border border-slate-200 bg-slate-50/70 p-3.5 space-y-3 text-xs">
                {(() => {
                  const m = selectedBatch.batch.metrics || {};
                  const kpis: [string, number][] = [
                    ['Search hits', m.search_hits ?? selectedBatch.batch.discovered_count ?? 0],
                    ['Unique URLs', m.unique_source_urls ?? selectedBatch.batch.discovered_count ?? 0],
                    ['Follow-up / Corrob', (m.followup_queries_executed ?? 0) + (m.corroboration_queries_executed ?? 0)],
                    ['Tier 1 Official', m.vacancies_tier_1_official ?? 0],
                    ['Tier 2 Authorized', m.vacancies_tier_2_authorized ?? 0],
                    ['Tier 3 Claim', m.vacancies_tier_3_third_party_claim ?? 0],
                    ['Needs Review', m.needs_human_review ?? 0],
                    ['Eligible (draft)', m.eligible_for_draft ?? selectedBatch.batch.eligible_count ?? 0],
                    [t.daily.prepared, selectedBatch.batch.drafted_count ?? m.drafted ?? 0],
                    ['Sent accepted', m.sent_accepted ?? selectedBatch.batch.sent_count ?? 0],
                    ['Skipped', m.skipped ?? selectedBatch.batch.skipped_count ?? 0],
                    [t.daily.duplicates, selectedBatch.batch.duplicates_removed_count ?? m.duplicates ?? 0],
                    ['Demo excluded', m.demo_excluded ?? (selectedBatch.batch.mode === 'demo_fixture' ? selectedBatch.batch.discovered_count : 0)],
                  ];
                  return (
                    <div className="grid grid-cols-2 sm:grid-cols-4 md:grid-cols-6 gap-2">
                      {kpis.map(([label, val]) => (
                        <div key={label} className="bg-white rounded-lg border border-slate-200 px-2.5 py-1.5">
                          <div className="text-[10px] text-slate-500">{label}</div>
                          <div className="text-sm font-bold text-slate-900">{val}</div>
                        </div>
                      ))}
                    </div>
                  );
                })()}
                {duplicateReasons.size > 0 && <details>
                  <summary className="cursor-pointer font-semibold">{t.batches.duplicateReasons}</summary>
                  <dl className="mt-2 space-y-1">
                    {Array.from(duplicateReasons, ([reason, count]) => <div key={reason} className="flex gap-3">
                      <dt>{reason}</dt><dd className="font-semibold">{count}</dd>
                    </div>)}
                  </dl>
                </details>}
                <div className="flex flex-wrap items-center justify-between gap-2 pt-1">
                  <div className="flex flex-wrap gap-3 text-slate-700">
                    <span>Queries: <strong>{(selectedBatch.batch.sources_searched || []).length}</strong></span>
                    {selectedBatch.sightings_count !== undefined && (
                      <span>Source sightings logged: <strong>{selectedBatch.sightings_count}</strong></span>
                    )}
                  </div>
                  {(selectedBatch.batch.sources_searched || []).length > 0 && (
                    <button
                      type="button"
                      onClick={() => setShowSources((v) => !v)}
                      className="text-indigo-600 font-medium hover:underline cursor-pointer"
                    >
                      {showSources ? 'Hide Source Queries' : `${t.batches.sourcesSearched} (${selectedBatch.batch.sources_searched.length})`}
                    </button>
                  )}
                </div>

                {showSources && (
                  <div className="space-y-1.5 max-h-44 overflow-y-auto border-t border-slate-200 pt-2">
                    {(selectedBatch.batch.sources_searched || []).map((src: any, idx: number) => (
                      <div key={idx} className="flex flex-wrap items-center justify-between gap-2 bg-white px-2.5 py-1.5 rounded border border-slate-200 text-[11px]">
                        <span className="font-mono text-slate-800 break-all">
                          [{src.provider}{src.country_code ? `/${src.country_code}` : ''}{src.page ? `/p${src.page}` : ''}] {src.query}
                        </span>
                        <span className="text-slate-600 shrink-0">
                          {src.source_group || 'search'} · raw:{src.results ?? 0} · emailHits:{src.email_invitations_found ?? 0} · dup:{src.duplicates_removed ?? 0}
                        </span>
                      </div>
                    ))}
                  </div>
                )}

                {/* Discovery Category Filters */}
                <div className="flex flex-wrap gap-1.5">
                  <button
                    type="button"
                    onClick={() => { setCategoryFilter('all'); setSkipCodeFilter('all'); }}
                    className={`px-2.5 py-1 rounded-full border cursor-pointer ${
                      categoryFilter === 'all' ? 'border-indigo-600 bg-indigo-50 text-indigo-800 font-semibold' : 'border-slate-200 bg-white text-slate-700'
                    }`}
                  >
                    {t.batches.categoryFilterAll} ({(selectedBatch.postings || []).length})
                  </button>
                  {Object.entries((t.batches.categories as Record<string, string>) || {}).map(([catKey, catLabel]) => {
                    const cnt = (selectedBatch.batch.category_counts || {})[catKey] ?? (selectedBatch.postings || []).filter((p: any) => p.discovery_category === catKey).length;
                    return (
                      <button
                        key={catKey}
                        type="button"
                        onClick={() => { setCategoryFilter(catKey); setSkipCodeFilter('all'); }}
                        className={`px-2.5 py-1 rounded-full border cursor-pointer ${
                          categoryFilter === catKey ? 'border-indigo-600 bg-indigo-50 text-indigo-800 font-semibold' : 'border-slate-200 bg-white text-slate-700'
                        }`}
                      >
                        {catLabel} ({cnt})
                      </button>
                    );
                  })}
                </div>

                {/* Skip Codes Summary */}
                {Object.keys(selectedBatch.batch.skip_reason_counts || {}).length > 0 && (
                  <div className="flex flex-wrap items-center gap-1.5 border-t border-slate-200 pt-2">
                    <span className="text-[11px] font-semibold text-slate-600">Skip reasons:</span>
                    {Object.entries(selectedBatch.batch.skip_reason_counts as Record<string, number>).map(([code, cnt]) => (
                      <button
                        key={code}
                        type="button"
                        onClick={() => setSkipCodeFilter((prev) => (prev === code ? 'all' : code))}
                        className={`px-2 py-0.5 rounded border text-[11px] font-mono cursor-pointer ${
                          skipCodeFilter === code ? 'border-amber-600 bg-amber-100 text-amber-900 font-semibold' : 'border-slate-200 bg-white text-slate-700'
                        }`}
                      >
                        {(t.batches.skipCodes as Record<string, string>)[code] || code} ({cnt})
                      </button>
                    ))}
                  </div>
                )}
              </div>

              <div className="space-y-2 max-h-[34rem] overflow-y-auto">
                {(selectedBatch.postings || [])
                  .filter((p: any) => categoryFilter === 'all' || p.discovery_category === categoryFilter)
                  .filter((p: any) => skipCodeFilter === 'all' || p.primary_skip_code === skipCodeFilter || (p.skip_codes || []).includes(skipCodeFilter))
                  .map((p: any) => {
                    const catLabel = (t.batches.categories as Record<string, string>)[p.discovery_category] || p.discovery_category || 'Opportunity';
                    const rejectedList = p.extraction_evidence?.rejected_emails || [];
                    const displayRole = p.role_extracted || p.title || 'Unverified Role';
                    const displayEmployer = p.validated_employer || p.company_name || 'Unverified Employer';
                    return (
                      <div key={p.id} className="p-3 rounded-lg border border-slate-200 text-xs space-y-1.5 bg-white">
                        <div className="flex flex-wrap items-center justify-between gap-2">
                          <div className="flex flex-wrap items-center gap-1.5 min-w-0">
                            <span className="font-semibold text-slate-900">{displayRole} — {displayEmployer}</span>
                            {p.company_domain && (
                              <span className="px-1.5 py-0.5 rounded bg-slate-100 text-slate-600 text-[10px]">
                                {p.publisher ? `${p.publisher} (${p.company_domain})` : p.company_domain}
                              </span>
                            )}
                            {p.is_demo && (
                              <span className="px-1.5 py-0.5 rounded bg-purple-100 text-purple-800 text-[10px] font-bold">
                                DEMO
                              </span>
                            )}
                            {p.source_url && p.source_url.startsWith('http') && (
                              <a
                                href={p.source_url}
                                target="_blank"
                                rel="noreferrer"
                                className="inline-flex items-center gap-0.5 text-indigo-600 hover:underline"
                                title={p.source_url}
                              >
                                <ExternalLink className="h-3 w-3" aria-hidden="true" />
                              </a>
                            )}
                          </div>
                          <div className="flex flex-wrap items-center gap-1.5">
                            <span className="px-2 py-0.5 rounded-full bg-indigo-50 text-indigo-800 border border-indigo-200 font-medium">
                              {catLabel}
                            </span>
                            {p.primary_skip_code && (
                              <span className="px-2 py-0.5 rounded-full bg-amber-100 text-amber-900 font-mono text-[10px] font-semibold">
                                {p.primary_skip_code}
                              </span>
                            )}
                            {(p.secondary_skip_codes || []).map((sc: string) => (
                              <span key={sc} className="px-1.5 py-0.5 rounded bg-slate-100 text-slate-600 font-mono text-[10px]">
                                +{sc}
                              </span>
                            ))}
                            <span className="px-2 py-0.5 rounded-full bg-slate-100 text-slate-700 font-medium">
                              {p.source_verification_tier || `Tier ${p.source_priority_tier ?? 5}`} · {p.approval_state || p.transport_status || p.verification_status}
                              {p.fit_score !== null && p.fit_score !== undefined ? ` · Fit ${p.fit_score}` : ''}
                            </span>
                          </div>
                        </div>

                        {p.why_this_cv && (
                          <div className="text-[11px] text-slate-700 bg-slate-50 border border-slate-200 rounded px-2 py-1">
                            <strong>CV Rationale:</strong> {p.why_this_cv}
                          </div>
                        )}

                        <div className="text-slate-600 flex flex-wrap gap-x-3 gap-y-0.5">
                          <span>Location: {p.location_raw || 'Unspecified'}{p.country_code ? ` (${p.country_code})` : ''} · {p.location_confidence || 'unknown'} · {p.remote_scope}</span>
                          <span>Access: {p.access_status || 'unknown'} · Date src: {p.date_source || 'unknown'}</span>
                          <span>Recipient: <strong>{p.application_email || 'None'}</strong></span>
                          {p.cc_emails?.length > 0 && <span>CC: {p.cc_emails.join(', ')}</span>}
                          {p.subject_instruction && <span>Subject req: <code>{p.subject_instruction}</code></span>}
                        </div>

                        {p.evidence_quote && (
                          <div className="rounded bg-emerald-50/70 border border-emerald-200 px-2.5 py-1.5 text-emerald-900">
                            <span className="font-semibold">Evidence quote:</span> "{p.evidence_quote}"
                          </div>
                        )}

                        {rejectedList.length > 0 && (
                          <div className="rounded bg-slate-50 border border-slate-200 px-2.5 py-1 text-[11px] text-slate-700">
                            Rejected addresses: {rejectedList.map((reItem: any) => `${reItem.email} (${reItem.reason})`).join('; ')}
                          </div>
                        )}

                        {p.eligibility_requirements && (
                          (p.eligibility_requirements.nationality_restrictions?.length > 0 ||
                            p.eligibility_requirements.local_residency_requirements?.length > 0 ||
                            p.eligibility_requirements.work_permit_requirements?.length > 0 ||
                            p.eligibility_requirements.language_requirements?.some((x: any) => x.level === 'mandatory') ||
                            p.eligibility_requirements.certification_requirements?.some((x: any) => x.level === 'mandatory') ||
                            p.eligibility_requirements.immediate_joining_requirement?.level === 'mandatory') && (
                            <div className="rounded bg-rose-50/70 border border-rose-200 px-2.5 py-1 text-[11px] text-rose-900 flex flex-wrap gap-1.5">
                              <span className="font-semibold">Mandatory eligibility gates:</span>
                              {(p.eligibility_requirements.nationality_restrictions || []).map((n: any, idx: number) => (
                                <span key={`nat-${idx}`} className="px-1.5 py-0.5 rounded bg-rose-100 border border-rose-200">
                                  Nationality: {(n.country_codes || []).join('/')}
                                </span>
                              ))}
                              {(p.eligibility_requirements.local_residency_requirements || []).filter((x: any) => x.level === 'mandatory').map((l: any, idx: number) => (
                                <span key={`res-${idx}`} className="px-1.5 py-0.5 rounded bg-rose-100 border border-rose-200">
                                  Local residency: {(l.country_codes || []).join('/')}
                                </span>
                              ))}
                              {(p.eligibility_requirements.work_permit_requirements || []).filter((x: any) => x.level === 'mandatory').map((w: any, idx: number) => (
                                <span key={`wp-${idx}`} className="px-1.5 py-0.5 rounded bg-rose-100 border border-rose-200">
                                  Permit: {w.permit_code}
                                </span>
                              ))}
                              {(p.eligibility_requirements.certification_requirements || []).filter((x: any) => x.level === 'mandatory').map((c: any, idx: number) => (
                                <span key={`cert-${idx}`} className="px-1.5 py-0.5 rounded bg-rose-100 border border-rose-200">
                                  Cert: {c.name}
                                </span>
                              ))}
                              {(p.eligibility_requirements.language_requirements || []).filter((x: any) => x.level === 'mandatory').map((lg: any, idx: number) => (
                                <span key={`lg-${idx}`} className="px-1.5 py-0.5 rounded bg-rose-100 border border-rose-200">
                                  Lang: {lg.lang_code} ({lg.min_level})
                                </span>
                              ))}
                              {p.eligibility_requirements.immediate_joining_requirement?.level === 'mandatory' && (
                                <span className="px-1.5 py-0.5 rounded bg-rose-100 border border-rose-200">
                                  Immediate join (&le; {p.eligibility_requirements.immediate_joining_requirement.max_notice_days}d)
                                </span>
                              )}
                            </div>
                          )
                        )}

                        {(p.primary_skip_code || p.skip_reason) && (
                          <div className="text-amber-900 bg-amber-50/70 border border-amber-200 rounded px-2.5 py-1.5 space-y-1">
                            {p.skip_codes?.length > 0 && (
                              <div className="flex flex-wrap gap-1">
                                {(p.skip_codes as string[]).map((c) => (
                                  <span key={c} className="px-1.5 py-0.5 rounded bg-amber-100 text-amber-900 font-mono text-[10px]">
                                    {c}
                                  </span>
                                ))}
                              </div>
                            )}
                            <div>{p.skip_reason}</div>
                          </div>
                        )}
                      </div>
                    );
                  })}
              </div>
            </>
          ) : (
            <p className="text-sm text-slate-500">Select or start a batch to view discovered postings and skip explanations.</p>
          )}
        </section>
      </div>

      {/* Recurring Schedules */}
      <section className="bg-white rounded-xl border border-slate-200 p-5 shadow-xs space-y-4">
        <div className="flex items-center gap-2">
          <CalendarClock className="h-5 w-5 text-indigo-600" aria-hidden="true" />
          <h2 className="text-base font-semibold text-slate-900">{t.batches.schedulesTitle}</h2>
        </div>

        <form onSubmit={handleCreateSchedule} className="grid grid-cols-1 md:grid-cols-5 gap-3 items-end">
          <input
            type="text"
            value={schedName}
            onChange={(e) => setSchedName(e.target.value)}
            placeholder="Schedule name"
            className="rounded-lg border border-slate-300 px-3 py-2 text-sm"
          />
          <select
            value={schedMode}
            onChange={(e) => setSchedMode(e.target.value as any)}
            className="rounded-lg border border-slate-300 px-3 py-2 text-sm bg-white"
          >
            <option value="dry_run">dry_run</option>
            <option value="live">live</option>
          </select>
          <input
            type="text"
            value={schedTime}
            onChange={(e) => setSchedTime(e.target.value)}
            placeholder="09:00"
            className="rounded-lg border border-slate-300 px-3 py-2 text-sm"
          />
          <input
            type="text"
            value={schedTz}
            onChange={(e) => setSchedTz(e.target.value)}
            placeholder="Asia/Dubai"
            className="rounded-lg border border-slate-300 px-3 py-2 text-sm"
          />
          <button
            type="submit"
            className="px-4 py-2 rounded-lg bg-slate-900 hover:bg-slate-800 text-white text-sm font-semibold cursor-pointer"
          >
            {t.batches.createScheduleBtn}
          </button>
          <div className="md:col-span-5 flex flex-wrap gap-2">
            {DAY_LABELS.map((dl, idx) => (
              <label key={dl} className="inline-flex items-center gap-1 text-xs text-slate-700">
                <input
                  type="checkbox"
                  checked={schedDays.includes(idx)}
                  onChange={() =>
                    setSchedDays((prev) => (prev.includes(idx) ? prev.filter((d) => d !== idx) : [...prev, idx]))
                  }
                />
                <span>{dl}</span>
              </label>
            ))}
          </div>
        </form>

        <div className="space-y-2">
          {schedules.map((s) => (
            <div key={s.id} className="flex flex-wrap items-center justify-between gap-2 p-3 rounded-lg border border-slate-200 text-xs">
              <div>
                <div className="font-semibold text-slate-900">
                  {s.name} · {s.mode} · {s.local_time_hhmm} ({s.timezone})
                </div>
                <div className="text-slate-500">
                  Next run: {formatDateTime(s.next_run_at, s.timezone)} · Active: {s.is_active ? 'Yes' : 'Paused'}
                </div>
              </div>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={async () => {
                    await apiRequest(`/schedules/${s.id}`, {
                      method: 'PATCH',
                      body: JSON.stringify({ is_active: !s.is_active }),
                    });
                    await loadAll();
                  }}
                  className="px-2.5 py-1 rounded border border-slate-200 hover:bg-slate-50 cursor-pointer"
                >
                  {s.is_active ? 'Pause' : 'Enable'}
                </button>
                <button
                  type="button"
                  onClick={async () => {
                    await apiRequest(`/schedules/${s.id}`, { method: 'DELETE' });
                    await loadAll();
                  }}
                  className="text-rose-600 hover:text-rose-800 cursor-pointer"
                  aria-label="Delete schedule"
                >
                  <Trash2 className="h-4 w-4" aria-hidden="true" />
                </button>
              </div>
            </div>
          ))}
        </div>
      </section>

      {sendModalItems && sendModalItems.length > 0 && (
        <SendApprovedConfirmModal
          open={true}
          items={sendModalItems}
          onClose={() => { setSendModalItems(null); loadAll(batch?.id).catch(e => setErr(String(e))); }}
          onCompleted={async (summary, isError) => {
            if (isError) {
              setDraftActionErr(summary);
              setDraftActionMsg('');
            } else {
              setDraftActionMsg(summary);
              setDraftActionErr('');
            }
            await loadAll(selectedBatch?.batch?.id);
          }}
        />
      )}
    </div>
  );
};
