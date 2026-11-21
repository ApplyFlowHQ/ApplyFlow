import React, { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { Download, ExternalLink, FileSpreadsheet, Search, ShieldBan, Upload } from 'lucide-react';
import { useI18n } from '../i18n';
import { apiRequest, ApiError } from '../lib/api';
import { ReviewQueueTab } from '../components/ReviewQueueTab';

type TrackerTab = 'real' | 'review' | 'discovery' | 'demo';

export const TrackerPage: React.FC = () => {
  const { t, formatDateTime, locale } = useI18n();
  const label = (en: string, ar: string) => locale === 'ar' ? ar : en;
  const statusLabels: Record<string, string> = {
    ready: t.workflow.ready,
    queued: t.workflow.queued,
    sending: t.daily.sending,
    failed: t.workflow.failed,
    cancelled: t.daily.cancelled,
    uncertain: t.workflow.uncertain,
    send_uncertain: t.workflow.uncertain,
    approved: t.workflow.approved,
    needs_human_review: t.workflow.needsReview,
    blocked: t.workflow.blocked,
    provider_accepted: t.workflow.providerAccepted,
    unknown: t.daily.unknown,
    imported: t.workflow.imported,
    draft: label('Draft', 'مسودة'),
    sent: label('Sent', 'تم الإرسال'),
    skipped: label('Skipped', 'تم التخطي'),
    leased: label('Reserved for sending', 'محجوز للإرسال'),
    deferred: label('Deferred', 'مؤجل'),
    open: label('Open', 'مفتوح'),
    interview: label('Interview', 'مقابلة'),
    rejected: label('Rejected', 'مرفوض'),
    withdrawn: label('Withdrawn', 'تم السحب'),
    archived: label('Archived', 'مؤرشف'),
    awaiting_approval: label('Awaiting approval', 'بانتظار الموافقة'),
    ready_for_approval: label('Ready for approval', 'جاهز للموافقة'),
    pending: label('Pending', 'قيد الانتظار'),
    verified: label('Verified', 'تم التحقق'),
    closed: label('Closed', 'مغلق'),
    suspicious: label('Suspicious', 'مثير للشك'),
    dns_transient: label('Temporary DNS issue', 'مشكلة مؤقتة في نظام أسماء النطاقات'),
    unverified: label('Unverified', 'لم يتم التحقق'),
    none: label('No response', 'لا يوجد رد'),
    human_reply: label('Personal reply', 'رد شخصي'),
    permanent_bounce: label('Permanent delivery failure', 'تعذّر التسليم نهائيًا'),
    delivery_delay: label('Delivery delayed', 'تأخر التسليم'),
    ooo: label('Out of office', 'خارج المكتب'),
    auto_ack: label('Automatic acknowledgement', 'تأكيد استلام تلقائي'),
    reserved: label('Reserved', 'محجوز'),
    drafted: label('Draft prepared', 'أُعدّت المسودة'),
    reconciled: label('Send confirmed', 'تم تأكيد الإرسال'),
    followup_draft: label('Follow-up drafted', 'أُعدّت مسودة المتابعة'),
    followup_approved: label('Follow-up approved', 'تمت الموافقة على المتابعة'),
    followup_sent: label('Follow-up sent', 'أُرسلت المتابعة'),
    send_attempted: label('Send attempted', 'جرت محاولة الإرسال'),
    different_vacancy_override: label('Different vacancy confirmed', 'تم تأكيد اختلاف الوظيفة'),
    outcome_updated: label('Outcome updated', 'تم تحديث النتيجة'),
    application: label('Application', 'طلب توظيف'),
    suppressed: label('Suppressed', 'مستبعد من المراسلة'),
    http_403: label('Access denied (403)', 'رُفض الوصول (403)'),
    http_404: label('Page not found (404)', 'الصفحة غير موجودة (404)'),
    fetched_200: label('Page retrieved', 'تم جلب الصفحة'),
    user_pasted: label('Provided by user', 'أضافه المستخدم'),
    bot_challenge: label('Bot check', 'فحص للتحقق من الزائر'),
    connect_timeout: label('Connection timed out', 'انتهت مهلة الاتصال'),
    robots_blocked: label('Blocked by robots.txt', 'محظور بموجب robots.txt'),
    ssrf_blocked: label('Blocked by network policy', 'حظرته سياسة الشبكة'),
    inaccessible: label('Inaccessible', 'يتعذر الوصول إليه'),
    snippet_only: label('Excerpt only', 'مقتطف فقط'),
    explicit: label('Explicit', 'مذكور صراحةً'),
    domain_hint: label('Suggested by domain', 'مرجّح من اسم النطاق'),
    meta_tag: label('Page metadata', 'بيانات الصفحة الوصفية'),
    inline_text: label('Page text', 'نص الصفحة'),
    unfetched: label('Not retrieved', 'لم يتم جلب الصفحة'),
    inferred_text: label('Inferred from text', 'مستنتج من النص'),
    inferred_url: label('Inferred from URL', 'مستنتج من الرابط'),
    jsonld: label('Structured data (JSON-LD)', 'بيانات منظّمة (JSON-LD)'),
    provider_metadata: label('Provider metadata', 'بيانات المزوّد الوصفية'),
    page_text: label('Page text', 'نص الصفحة'),
  };
  const statusLabel = (value?: string | null) => value ? statusLabels[value] ?? t.daily.unknown : '—';
  const categories: Record<string, string> = t.batches.categories;
  const skipCodes: Record<string, string> = t.batches.skipCodes;
  const skipLabel = (code: string) => skipCodes[code] ?? label('Other reason', 'سبب آخر');
  const [confirmInbox, setConfirmInbox] = useState(false);
  const [overrideReason, setOverrideReason] = useState('');
  const [actionBusy, setActionBusy] = useState(false);
  const [searchParams] = useSearchParams();
  const [activeTab, setActiveTab] = useState<TrackerTab>(() =>
    searchParams.get('tab') === 'review' ? 'review' : 'real',
  );
  const [apps, setApps] = useState<any[]>([]);
  const [discoveryItems, setDiscoveryItems] = useState<any[]>([]);
  const [demoExcludedCount, setDemoExcludedCount] = useState(0);
  const [duplicatesTotal, setDuplicatesTotal] = useState(0);
  const [showDuplicates, setShowDuplicates] = useState(false);
  const [categoryFilter, setCategoryFilter] = useState('');
  const [skipCodeFilter, setSkipCodeFilter] = useState('');
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [q, setQ] = useState('');
  const [transportFilter, setTransportFilter] = useState('');
  const [outcomeFilter, setOutcomeFilter] = useState('');
  const [selectedDetail, setSelectedDetail] = useState<any | null>(null);
  const [editOutcome, setEditOutcome] = useState('open');
  const [editNotes, setEditNotes] = useState('');
  const [sheetsStatus, setSheetsStatus] = useState<any>(null);
  const [msg, setMsg] = useState('');
  const [err, setErr] = useState('');

  const loadData = async () => {
    if (activeTab === 'review') {
      return;
    }
    if (activeTab === 'discovery') {
      const params = new URLSearchParams({
        q,
        scope: 'real',
        category: categoryFilter,
        skip_code: skipCodeFilter,
        duplicates_only: String(showDuplicates),
        page: String(page),
        page_size: '25',
      });
      const dRes = await apiRequest<any>(`/discovery-sources?${params.toString()}`);
      setDiscoveryItems(dRes.items || []);
      setDuplicatesTotal(dRes.duplicates_total || 0);
      setTotal(dRes.total || 0);
      return;
    }

    const params = new URLSearchParams({
      q,
      scope: activeTab === 'demo' ? 'demo' : 'real',
      transport_status: transportFilter,
      outcome_status: outcomeFilter,
      page: String(page),
      page_size: '20',
    });
    const [aRes, shRes] = await Promise.all([
      apiRequest<any>(`/applications?${params.toString()}`),
      apiRequest('/sheets'),
    ]);
    setApps(aRes.applications || []);
    setTotal(aRes.total || 0);
    setDemoExcludedCount(aRes.demo_excluded_count || 0);
    setSheetsStatus(shRes);
  };

  useEffect(() => {
    loadData().catch((e) => setErr(String(e)));
  }, [activeTab, page, transportFilter, outcomeFilter, showDuplicates, categoryFilter, skipCodeFilter]);

  const openDetail = async (id: string) => {
    const detail = await apiRequest(`/applications/${id}`);
    setSelectedDetail(detail);
    setConfirmInbox(false);
    setOverrideReason('');
    setEditOutcome(detail.application.outcome_status);
    setEditNotes(detail.application.notes || '');
  };

  const updateItem = async (path: string, payload?: object) => {
    const id = selectedDetail.application.id;
    setActionBusy(true);
    setErr('');
    try {
      await apiRequest(`/applications/${id}/${path}`, {
        method: 'POST', body: payload ? JSON.stringify(payload) : undefined,
      });
      await openDetail(id);
      await loadData();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e));
    } finally {
      setActionBusy(false);
    }
  };

  const handleSaveNotes = async () => {
    if (!selectedDetail) return;
    setErr('');
    setMsg('');
    try {
      await apiRequest(`/applications/${selectedDetail.application.id}`, {
        method: 'PATCH',
        body: JSON.stringify({ outcome_status: editOutcome, notes: editNotes }),
      });
      setMsg(label('Application updated.', 'تم تحديث الطلب.'));
      await openDetail(selectedDetail.application.id);
      await loadData();
    } catch (e2) {
      setErr(e2 instanceof ApiError ? e2.message : label('Failed to update application', 'تعذّر تحديث الطلب'));
    }
  };

  const handleSuppress = async () => {
    if (!selectedDetail) return;
    setErr('');
    setMsg('');
    try {
      await apiRequest(`/applications/${selectedDetail.application.id}/suppress`, { method: 'POST' });
      setMsg(label('Recipient added to suppression list and follow-ups stopped.', 'أُضيف المستلم إلى قائمة الاستبعاد وتوقفت المتابعات.'));
      await openDetail(selectedDetail.application.id);
    } catch (e2) {
      setErr(e2 instanceof ApiError ? e2.message : label('Failed to suppress recipient', 'تعذّر استبعاد المستلم'));
    }
  };

  const handleImportHistory = async (file: File | null) => {
    if (!file) return;
    setErr('');
    setMsg('');
    try {
      const form = new FormData();
      form.append('file', file);
      const res = await apiRequest<{ inserted: number; skipped: number }>('/exports/import-history', {
        method: 'POST',
        body: form,
      });
      setMsg(label(`Imported ${res.inserted} historical contacts (${res.skipped} skipped/duplicates).`,
        `جهات الاتصال المستوردة: ${res.inserted}. السجلات المتخطاة أو المكررة: ${res.skipped}.`));
    } catch (e2) {
      setErr(e2 instanceof ApiError ? e2.message : label('Failed to import history', 'تعذّر استيراد سجل التواصل'));
    }
  };

  const handleConnectSheets = async () => {
    setErr('');
    try {
      const res = await apiRequest<{ authorization_url: string }>('/sheets/connect-url');
      window.location.href = res.authorization_url;
    } catch (e2) {
      setErr(e2 instanceof ApiError ? e2.message : label('Google Sheets authorization unavailable', 'تعذّر الاتصال بجداول Google'));
    }
  };

  const handleSyncSheets = async () => {
    setErr('');
    try {
      const res = await apiRequest<{ rows_synced: number }>('/sheets/sync', { method: 'POST' });
      setMsg(label(`Synced ${res.rows_synced} rows to Google Sheets.`, `عدد الصفوف التي تمت مزامنتها مع جداول Google: ${res.rows_synced}.`));
      await loadData();
    } catch (e2) {
      setErr(e2 instanceof ApiError ? e2.message : label('Google Sheets sync failed', 'تعذّرت المزامنة مع جداول Google'));
    }
  };

  const exportScope = activeTab === 'demo' ? 'demo' : 'real';

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-bold text-slate-900">{t.tracker.title}</h1>
        <div className="flex flex-wrap gap-2">
          {activeTab === 'discovery' ? (
            <a
              href="/api/v1/exports/discovery.csv?scope=real"
              className="inline-flex items-center gap-1.5 px-3 py-2 rounded-lg border border-slate-300 bg-white text-xs font-semibold hover:bg-slate-50"
            >
              <Download className="h-3.5 w-3.5" aria-hidden="true" />
              <span>{t.tracker.exportDiscoveryCsv}</span>
            </a>
          ) : (
            <>
              <a
                href={`/api/v1/exports/csv?scope=${exportScope}`}
                className="inline-flex items-center gap-1.5 px-3 py-2 rounded-lg border border-slate-300 bg-white text-xs font-semibold hover:bg-slate-50"
              >
                <Download className="h-3.5 w-3.5" aria-hidden="true" />
                <span>{t.tracker.exportCsv}</span>
              </a>
              <a
                href={`/api/v1/exports/xlsx?scope=${exportScope}`}
                className="inline-flex items-center gap-1.5 px-3 py-2 rounded-lg border border-slate-300 bg-white text-xs font-semibold hover:bg-slate-50"
              >
                <FileSpreadsheet className="h-3.5 w-3.5" aria-hidden="true" />
                <span>{t.tracker.exportXlsx}</span>
              </a>
            </>
          )}
          <label className="inline-flex items-center gap-1.5 px-3 py-2 rounded-lg border border-slate-300 bg-white text-xs font-semibold hover:bg-slate-50 cursor-pointer">
            <Upload className="h-3.5 w-3.5" aria-hidden="true" />
            <span>{t.tracker.importHistory}</span>
            <input
              type="file"
              accept=".csv,.xlsx"
              className="hidden"
              onChange={(e) => handleImportHistory(e.target.files?.[0] || null)}
            />
          </label>
          {sheetsStatus?.connected ? (
            <button
              type="button"
              onClick={handleSyncSheets}
              className="px-3 py-2 rounded-lg bg-emerald-600 text-white text-xs font-semibold cursor-pointer"
            >
              {t.tracker.sheetsSync}
            </button>
          ) : (
            <button
              type="button"
              onClick={handleConnectSheets}
              className="px-3 py-2 rounded-lg border border-emerald-300 bg-emerald-50 text-emerald-800 text-xs font-semibold cursor-pointer"
            >
              {t.tracker.sheetsConnect}
            </button>
          )}
        </div>
      </div>

      {/* Separated Navigation Tabs: Real Applications | Raw Discovery & Evidence | Demo Sandbox */}
      <div className="flex flex-wrap gap-2 border-b border-slate-200 pb-2">
        <button
          type="button"
          onClick={() => {
            setSelectedDetail(null);
            setPage(1);
            setActiveTab('real');
          }}
          className={`px-4 py-2 rounded-lg text-xs font-semibold cursor-pointer ${
            activeTab === 'real' ? 'bg-slate-900 text-white' : 'bg-white border border-slate-200 text-slate-700 hover:bg-slate-50'
          }`}
        >
          {t.tracker.tabRealApps}
        </button>
        <button
          type="button"
          onClick={() => {
            setSelectedDetail(null);
            setPage(1);
            setActiveTab('review');
          }}
          className={`px-4 py-2 rounded-lg text-xs font-semibold cursor-pointer ${
            activeTab === 'review' ? 'bg-emerald-700 text-white' : 'bg-white border border-emerald-200 text-emerald-800 hover:bg-emerald-50'
          }`}
        >
          {t.tracker.tabReviewQueue}
        </button>
        <button
          type="button"
          onClick={() => {
            setSelectedDetail(null);
            setPage(1);
            setActiveTab('discovery');
          }}
          className={`px-4 py-2 rounded-lg text-xs font-semibold cursor-pointer ${
            activeTab === 'discovery' ? 'bg-indigo-600 text-white' : 'bg-white border border-slate-200 text-slate-700 hover:bg-slate-50'
          }`}
        >
          {t.tracker.tabDiscovery}
        </button>
        <button
          type="button"
          onClick={() => {
            setSelectedDetail(null);
            setPage(1);
            setActiveTab('demo');
          }}
          className={`px-4 py-2 rounded-lg text-xs font-semibold cursor-pointer ${
            activeTab === 'demo' ? 'bg-purple-700 text-white' : 'bg-white border border-purple-200 text-purple-800 hover:bg-purple-50'
          }`}
        >
          {t.tracker.tabDemo} ({demoExcludedCount})
        </button>
      </div>

      {activeTab === 'demo' && (
        <div className="p-3 rounded-xl bg-purple-50 border border-purple-200 text-purple-900 text-xs font-medium">
          {label('Demo entries use synthetic .example data and are excluded from live sending, schedules, and Sheets sync.', 'تعرض هذه المساحة بيانات تجريبية بنطاق ‎.example مستثناة من الإرسال الفعلي والجدولة والمزامنة مع جداول Google.')}
        </div>
      )}

      {msg && <div role="status" className="p-3 rounded-lg bg-emerald-50 border border-emerald-200 text-emerald-800 text-sm">{msg}</div>}
      {err && <div role="alert" className="p-3 rounded-lg bg-rose-50 border border-rose-200 text-rose-800 text-sm">{err}</div>}

      {/* Search & Filter Toolbar */}
      {activeTab !== 'review' && (
      <div className="bg-white rounded-xl border border-slate-200 p-4 flex flex-wrap gap-3 items-center">
        <div className="flex-1 min-w-[220px] flex items-center gap-2 border border-slate-300 rounded-lg px-3 py-1.5">
          <Search className="h-4 w-4 text-slate-400" aria-hidden="true" />
          <input
            type="text"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && loadData()}
            aria-label={t.tracker.searchPlaceholder}
            placeholder={t.tracker.searchPlaceholder}
            className="w-full text-sm focus:outline-none"
          />
        </div>

        {activeTab === 'discovery' ? (
          <>
            <select
              aria-label={label('Discovery category', 'فئة الاكتشاف')}
              value={categoryFilter}
              onChange={(e) => {
                setPage(1);
                setCategoryFilter(e.target.value);
              }}
              className="rounded-lg border border-slate-300 px-3 py-2 text-sm bg-white"
            >
              <option value="">{label('All Categories', 'كل الفئات')}</option>
              {Object.entries(categories).map(([k, v]) => (
                <option key={k} value={k}>{v}</option>
              ))}
            </select>
            <select
              aria-label={label('Primary skip reason', 'سبب التخطي الرئيسي')}
              value={skipCodeFilter}
              onChange={(e) => {
                setPage(1);
                setSkipCodeFilter(e.target.value);
              }}
              className="rounded-lg border border-slate-300 px-3 py-2 text-sm bg-white"
            >
              <option value="">{label('All Primary Skip Codes', 'كل أسباب التخطي الرئيسية')}</option>
              {Object.entries(skipCodes).map(([k, v]) => (
                <option key={k} value={k}>{v}</option>
              ))}
            </select>
            <button
              type="button"
              onClick={() => {
                setPage(1);
                setShowDuplicates((v) => !v);
              }}
              className={`px-3 py-2 rounded-lg border text-xs font-semibold cursor-pointer ${
                showDuplicates ? 'bg-amber-100 border-amber-400 text-amber-900' : 'bg-white border-slate-300 text-slate-700'
              }`}
            >
              {showDuplicates ? label('Showing duplicates', 'عرض السجلات المكررة') : `${label('Duplicates', 'السجلات المكررة')} (${duplicatesTotal})`}
            </button>
          </>
        ) : (
          <>
            <select
              aria-label={label('Transport status', 'حالة الإرسال')}
              value={transportFilter}
              onChange={(e) => setTransportFilter(e.target.value)}
              className="rounded-lg border border-slate-300 px-3 py-2 text-sm bg-white"
            >
              <option value="">{label('All Transport States', 'كل حالات الإرسال')}</option>
              <option value="draft">{statusLabel('draft')}</option>
              <option value="ready">{statusLabel('ready')}</option>
              <option value="queued">{statusLabel('queued')}</option>
              <option value="sending">{statusLabel('sending')}</option>
              <option value="sent">{statusLabel('sent')}</option>
              <option value="send_uncertain">{statusLabel('send_uncertain')}</option>
              <option value="skipped">{statusLabel('skipped')}</option>
              <option value="cancelled">{statusLabel('cancelled')}</option>
              <option value="failed">{statusLabel('failed')}</option>
            </select>
            <select
              aria-label={label('Outcome', 'النتيجة')}
              value={outcomeFilter}
              onChange={(e) => setOutcomeFilter(e.target.value)}
              className="rounded-lg border border-slate-300 px-3 py-2 text-sm bg-white"
            >
              <option value="">{label('All Outcomes', 'كل النتائج')}</option>
              <option value="open">{statusLabel('open')}</option>
              <option value="interview">{statusLabel('interview')}</option>
              <option value="rejected">{statusLabel('rejected')}</option>
              <option value="withdrawn">{statusLabel('withdrawn')}</option>
            </select>
          </>
        )}
        <button
          type="button"
          onClick={() => {
            setPage(1);
            loadData();
          }}
          className="px-4 py-2 rounded-lg bg-slate-900 text-white text-xs font-semibold cursor-pointer"
        >
          {label('Filter', 'تصفية')}
        </button>
      </div>
      )}

      {/* Main Table: Review Queue vs Applications vs Raw Discovery & Evidence */}
      {activeTab === 'review' ? (
        <ReviewQueueTab
          scope="real"
          initialBatchId={searchParams.get('batch_id') || ''}
          onStatusMessage={(text, isError) => {
            if (isError) {
              setErr(text);
              setMsg('');
            } else {
              setMsg(text);
              setErr('');
            }
          }}
        />
      ) : activeTab === 'discovery' ? (
        <div className="bg-white rounded-xl border border-slate-200 shadow-xs overflow-x-auto">
          {discoveryItems.length === 0 ? (
            <div className="p-8 text-center text-sm text-slate-500">{label('No discovery records match this filter.', 'لا توجد سجلات اكتشاف تطابق عوامل التصفية.')}</div>
          ) : (
            <table className="w-full text-start border-collapse text-xs">
              <thead>
                <tr className="bg-slate-50 border-b border-slate-200 text-slate-600">
                  <th className="p-3 text-start font-semibold">{label('Actual Employer', 'جهة التوظيف الفعلية')}</th>
                  <th className="p-3 text-start font-semibold">{label('Source Domain / Publisher', 'نطاق المصدر / الناشر')}</th>
                  <th className="p-3 text-start font-semibold">{label('Role Extracted', 'المسمى الوظيفي المستخرج')}</th>
                  <th className="p-3 text-start font-semibold">{label('Location (Confidence)', 'الموقع (درجة الثقة)')}</th>
                  <th className="p-3 text-start font-semibold">{label('Access', 'إمكانية الوصول')}</th>
                  <th className="p-3 text-start font-semibold">{label('Category', 'الفئة')}</th>
                  <th className="p-3 text-start font-semibold">{label('Primary & Secondary Skip Codes', 'أسباب التخطي الرئيسية والثانوية')}</th>
                  <th className="p-3 text-start font-semibold">{label('Recipient', 'المستلم')}</th>
                  <th className="p-3 text-start font-semibold">{label('Source', 'المصدر')}</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {discoveryItems.map((d) => (
                  <tr key={d.id} className="hover:bg-slate-50">
                    <td className="p-3 font-medium text-slate-900">{d.validated_employer || '—'}</td>
                    <td className="p-3 text-slate-600">{d.publisher || d.source_domain || '—'}</td>
                    <td className="p-3 text-slate-800">{d.role_extracted || d.raw_title || '—'}</td>
                    <td className="p-3 text-slate-600">
                      {[d.city, d.country_code].filter(Boolean).join(', ') || '—'}
                      {d.location_confidence ? ` (${statusLabel(d.location_confidence)})` : ''}
                    </td>
                    <td className="p-3 text-slate-600">{statusLabel(d.access_status)}</td>
                    <td className="p-3">
                      <span className="px-2 py-0.5 rounded-full bg-indigo-50 text-indigo-800 font-medium">
                        {categories[d.discovery_category] ?? t.daily.unknown}
                      </span>
                    </td>
                    <td className="p-3">
                      <div className="flex flex-wrap gap-1">
                        {d.primary_skip_code && (
                          <span className="px-1.5 py-0.5 rounded bg-amber-100 text-amber-900 font-mono text-[10px] font-semibold">
                            {skipLabel(d.primary_skip_code)}
                          </span>
                        )}
                        {(d.secondary_skip_codes || []).map((sc: string) => (
                          <span key={sc} title={sc} className="px-1.5 py-0.5 rounded bg-slate-100 text-slate-600 font-mono text-[10px]">
                            +{skipLabel(sc)}
                          </span>
                        ))}
                      </div>
                    </td>
                    <td className="p-3 text-slate-600">{d.application_email || '—'}</td>
                    <td className="p-3">
                      {d.canonical_url?.startsWith('http') && (
                        <a aria-label={label('Open source', 'فتح المصدر')} href={d.canonical_url} target="_blank" rel="noreferrer" className="text-indigo-600 hover:underline inline-flex items-center gap-1">
                          <ExternalLink className="h-3.5 w-3.5" aria-hidden="true" />
                        </a>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      ) : (
        <div className="bg-white rounded-xl border border-slate-200 shadow-xs overflow-x-auto">
          {apps.length === 0 ? (
            <div className="p-8 text-center text-sm text-slate-500">{t.tracker.noApplications}</div>
          ) : (
            <table className="w-full text-start border-collapse text-xs">
              <thead>
                <tr className="bg-slate-50 border-b border-slate-200 text-slate-600">
                  <th className="p-3 text-start font-semibold">{label('Actual Employer', 'جهة التوظيف الفعلية')}</th>
                  <th className="p-3 text-start font-semibold">{label('Source Domain', 'نطاق المصدر')}</th>
                  <th className="p-3 text-start font-semibold">{label('Role', 'المسمى الوظيفي')}</th>
                  <th className="p-3 text-start font-semibold">{t.daily.location}</th>
                  <th className="p-3 text-start font-semibold">{label('Recipient', 'المستلم')}</th>
                  <th className="p-3 text-start font-semibold">{label('CV', 'السيرة الذاتية')}</th>
                  <th className="p-3 text-start font-semibold">{label('Fit', 'مدى المطابقة')}</th>
                  <th className="p-3 text-start font-semibold">{label('State / Skip Codes', 'الحالة / أسباب التخطي')}</th>
                  <th className="p-3 text-start font-semibold">{t.tracker.manualOutcome}</th>
                  <th className="p-3 text-start font-semibold">{label('Date', 'التاريخ')}</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {apps.map((a) => (
                  <tr
                    key={a.id}
                    onClick={() => openDetail(a.id)}
                    className="hover:bg-indigo-50/40 cursor-pointer"
                  >
                    <td className="p-3 font-medium text-slate-900">
                      <div className="flex items-center gap-1.5">
                        <span>{a.validated_employer || a.company_name || '—'}</span>
                        {a.is_demo && (
                          <span className="px-1.5 py-0.5 rounded bg-purple-100 text-purple-800 text-[10px] font-bold">
                            {label('Demo entry', 'سجل تجريبي')}
                          </span>
                        )}
                      </div>
                    </td>
                    <td className="p-3 text-slate-500">{a.source_domain || a.publisher || '—'}</td>
                    <td className="p-3 text-slate-800">{a.role_title || '—'}</td>
                    <td className="p-3 text-slate-600">{[a.city, a.country_code].filter(Boolean).join(', ') || '—'}</td>
                    <td className="p-3 text-slate-600">{a.recipient_email || '—'}</td>
                    <td className="p-3 text-slate-600 max-w-[140px] truncate">{a.selected_cv || '—'}</td>
                    <td className="p-3 text-slate-700">{a.fit_score ?? '—'}</td>
                    <td className="p-3">
                      <div className="flex flex-wrap items-center gap-1">
                        <span
                          className={`px-2 py-0.5 rounded-full font-semibold ${
                            a.transport_status === 'sent'
                              ? 'bg-emerald-100 text-emerald-900'
                              : a.transport_status === 'queued' || a.transport_status === 'sending'
                                ? 'bg-indigo-100 text-indigo-900'
                                : a.transport_status === 'failed'
                                  ? 'bg-rose-100 text-rose-800'
                                  : a.transport_status === 'send_uncertain'
                                    ? 'bg-purple-100 text-purple-900'
                                    : 'bg-slate-100 text-slate-700'
                          }`}
                        >
                          {a.queue_state && a.queue_state !== a.transport_status
                            ? `${statusLabel(a.queue_state)} (${statusLabel(a.transport_status)})`
                            : statusLabel(a.transport_status)}
                        </span>
                        {a.primary_skip_code && (
                          <span className="px-1.5 py-0.5 rounded bg-amber-100 text-amber-900 font-mono text-[10px] font-semibold">
                            {skipLabel(a.primary_skip_code)}
                          </span>
                        )}
                      </div>
                      {(a.outbox_last_error || a.skip_or_error_reason) && (
                        <div className="text-[11px] text-rose-700 font-medium mt-0.5 max-w-[240px] truncate" title={a.outbox_last_error || a.skip_or_error_reason}>
                          {a.outbox_last_error || a.skip_or_error_reason}
                        </div>
                      )}
                      {a.provider_message_id && (
                        <div
                          data-testid={`tracker-provider-msg-id-${a.id}`}
                          className="text-[10px] font-mono text-emerald-800 mt-0.5 max-w-[220px] truncate"
                          title={a.provider_message_id}
                        >
                          {t.daily.receipt} · {a.provider_message_id}
                        </div>
                      )}
                    </td>
                    <td className="p-3 font-medium text-indigo-700">
                      {statusLabel(a.outcome_status)}
                      <div className="text-xs text-slate-600">{label('Response', 'الرد')} · {statusLabel(a.response_status)}</div>
                      {a.next_follow_up_at && <div className="text-xs text-slate-600">{label('Follow-up due', 'موعد المتابعة')} · {formatDateTime(a.next_follow_up_at)}</div>}
                    </td>
                    <td className="p-3 text-slate-500">
                      <div>{formatDateTime(a.sent_at || a.discovered_at)}</div>
                      {a.sent_at && (
                        <div className="text-[10px] font-semibold text-emerald-700">{label('Provider accepted', 'قبل المزوّد الرسالة')}</div>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}

      {/* Pagination */}
      {total > (activeTab === 'discovery' ? 25 : 20) && (
        <div className="flex items-center justify-between text-xs text-slate-600">
          <span>{label('Total', 'الإجمالي')} · {total}</span>
          <div className="flex gap-2">
            <button disabled={page <= 1} onClick={() => setPage((p) => p - 1)} className="px-3 py-1 rounded border border-slate-300 disabled:opacity-40">
              {t.review.previousPage}
            </button>
            <span>{label('Page', 'الصفحة')} {page}</span>
            <button
              disabled={page * (activeTab === 'discovery' ? 25 : 20) >= total}
              onClick={() => setPage((p) => p + 1)}
              className="px-3 py-1 rounded border border-slate-300 disabled:opacity-40"
            >
              {t.review.nextPage}
            </button>
          </div>
        </div>
      )}

      {/* Detail Panel */}
      {selectedDetail && (
        <section className="bg-white rounded-xl border border-indigo-200 p-5 shadow-sm space-y-4">
          <div className="flex items-center justify-between">
            <h2 className="text-base font-bold text-slate-900">
              {t.tracker.detailTitle}: {selectedDetail.application.role_title} @ {selectedDetail.application.company_name || label('Unverified employer', 'جهة توظيف غير مؤكدة')}
            </h2>
            <button type="button" onClick={() => setSelectedDetail(null)} className="text-xs text-slate-500 hover:underline cursor-pointer">
              {t.daily.close}
            </button>
          </div>

          <div className="text-sm text-slate-700">
            {label('Verification', 'التحقق')} · {statusLabel(selectedDetail.application.verification_status)}
            {' · '}{label('Approval', 'الموافقة')} · {statusLabel(selectedDetail.application.approval_state)}
            {' · '}{label('Response', 'الرد')} · {statusLabel(selectedDetail.application.response_status)}
          </div>
          {selectedDetail.application.primary_skip_code === 'PREVIOUSLY_CONTACTED' && (
            <div className="space-y-2">
              {(selectedDetail.contact_evidence || []).map((item: { address: string; source: string; classification: string }) => (
                <p key={item.address} className="text-sm">
                  <bdi>{item.address}</bdi> · {item.source === 'sent_history'
                    ? (item.classification === 'job_application' ? label('Sent history · application signals', 'البريد المرسل · مؤشرات طلب توظيف')
                      : label('Sent history · prior contact', 'البريد المرسل · تواصل سابق'))
                    : statusLabel(item.source)}
                </p>
              ))}
              <label className="block text-sm">
                {label('Explain why this is a different vacancy at the same recruitment inbox', 'وضّح لماذا هذه وظيفة مختلفة لدى نفس بريد التوظيف')}
                <textarea value={overrideReason} maxLength={500} onChange={e => setOverrideReason(e.target.value)}
                  className="block w-full border rounded p-2" />
              </label>
              <button disabled={actionBusy || !overrideReason.trim()}
                onClick={() => updateItem('duplicate-override', { reason: overrideReason.trim() })}
                className="rounded border px-3 py-1 disabled:opacity-50">{label('Recheck this vacancy', 'إعادة التحقق من الوظيفة')}</button>
            </div>
          )}
          {(selectedDetail.followup || selectedDetail.application.next_follow_up_at) && (
            <section className="rounded border border-slate-200 p-3 space-y-2">
              <h3 className="font-semibold">{label('Follow-up', 'المتابعة')}</h3>
              {selectedDetail.application.next_follow_up_at && <p>{label('Due', 'الموعد')} · {formatDateTime(selectedDetail.application.next_follow_up_at)}</p>}
              {selectedDetail.followup && <>
                <p>{label('Status', 'الحالة')} · {statusLabel(selectedDetail.followup.status)}</p>
                {selectedDetail.followup.error && <p role="status">{selectedDetail.followup.error}</p>}
                <p dir="auto">{selectedDetail.followup.sender} → {selectedDetail.followup.recipient}</p>
                <strong dir="auto">{selectedDetail.followup.subject}</strong>
                <p dir="auto" className="whitespace-pre-wrap">{selectedDetail.followup.body}</p>
              </>}
              {selectedDetail.followup?.status === 'send_uncertain' && (
                <button disabled={actionBusy} onClick={() => updateItem('reconcile-outbox')}
                  className="rounded border px-3 py-1">{label('Check Sent history for this follow-up', 'التحقق من إرسال المتابعة')}</button>
              )}
              {selectedDetail.followup?.status === 'awaiting_approval' && <>
                {selectedDetail.followup.confirm_inbox_required && <label className="flex gap-2 items-center">
                  <input type="checkbox" checked={confirmInbox} onChange={e => setConfirmInbox(e.target.checked)} />
                  {label('I checked the sending mailbox and found no reply', 'راجعت صندوق البريد وتأكدت من عدم وجود رد')}
                </label>}
                <button disabled={actionBusy || (selectedDetail.followup.confirm_inbox_required && !confirmInbox)}
                  onClick={() => updateItem('followup/approve', { draft_id: selectedDetail.followup.id, confirm_inbox: confirmInbox })}
                  className="rounded bg-indigo-700 text-white px-3 py-1 disabled:opacity-50">
                  {label('Approve and queue follow-up', 'الموافقة وإضافة المتابعة إلى قائمة الإرسال')}
                </button>
              </>}
              {selectedDetail.application.next_follow_up_at &&
                (!selectedDetail.followup || ['sent', 'failed', 'cancelled'].includes(selectedDetail.followup.status)) &&
                new Date(selectedDetail.application.next_follow_up_at).getTime() <= Date.now() &&
                <button disabled={actionBusy} onClick={() => updateItem('followup/draft')} className="border rounded px-3 py-1">
                  {label('Prepare follow-up draft', 'إعداد مسودة المتابعة')}
                </button>}
            </section>
          )}
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 text-xs">
            <div className="p-3 rounded-lg bg-slate-50 border border-slate-200 space-y-2">
              <div><strong>{label('Subject:', 'الموضوع:')}</strong> {selectedDetail.application.subject || '—'}</div>
              <div><strong>{label('To:', 'إلى:')}</strong> {selectedDetail.application.recipient_email} {selectedDetail.application.cc_emails?.length ? `(${label('CC', 'نسخة إلى')}: ${selectedDetail.application.cc_emails.join(', ')})` : ''}</div>
              <div><strong>{label('Frozen CV:', 'نسخة السيرة الذاتية المثبّتة:')}</strong> {selectedDetail.application.frozen_cv_filename} ({selectedDetail.application.frozen_cv_sha256?.slice(0, 12)}…)</div>
              <div>
                <strong>{label('Transport Status:', 'حالة الإرسال:')}</strong> <code>{statusLabel(selectedDetail.application.transport_status)}</code>
                {selectedDetail.application.sent_at && (
                  <span> · <strong>{label('Accepted At:', 'وقت قبول الرسالة:')}</strong> {formatDateTime(selectedDetail.application.sent_at)}</span>
                )}
              </div>
              {selectedDetail.application.provider_message_id && (
                <div>
                  <strong>{label('Provider Message ID:', 'معرّف الرسالة لدى المزوّد:')}</strong>{' '}
                  <code className="font-mono text-emerald-800">{selectedDetail.application.provider_message_id}</code>
                </div>
              )}
              {selectedDetail.application.rfc_message_id && (
                <div>
                  <strong>{label('RFC Message-ID:', 'معرّف الرسالة (RFC):')}</strong>{' '}
                  <code className="font-mono text-slate-700">{selectedDetail.application.rfc_message_id}</code>
                </div>
              )}
              {selectedDetail.application.skip_or_error_reason && (
                <div className="p-2 rounded bg-rose-50 border border-rose-200 text-rose-900">
                  <strong>{label('Failure / Skip Reason:', 'سبب الفشل أو التخطي:')}</strong> {selectedDetail.application.skip_or_error_reason}
                </div>
              )}
              <pre className="whitespace-pre-wrap font-sans bg-white p-3 rounded border border-slate-200 text-slate-800 max-h-60 overflow-y-auto">
                {selectedDetail.application.body_text || selectedDetail.application.skip_or_error_reason}
              </pre>
            </div>

            <div className="space-y-3">
              <div className="p-3 rounded-lg bg-slate-50 border border-slate-200 space-y-1">
                <div><strong>{label('Evidence URL:', 'رابط الدليل:')}</strong> {selectedDetail.source_evidence.canonical_url || selectedDetail.source_evidence.source_url}</div>
                <div><strong>{label('Publisher / Domain:', 'الناشر / النطاق:')}</strong> {selectedDetail.source_evidence.publisher || '—'} ({selectedDetail.application.source_domain || '—'})</div>
                <div><strong>{label('Access & Date Source:', 'إمكانية الوصول ومصدر التاريخ:')}</strong> {statusLabel(selectedDetail.source_evidence.access_status)} · {statusLabel(selectedDetail.source_evidence.date_source)} · {label('Location confidence', 'درجة الثقة بالموقع')} · {statusLabel(selectedDetail.source_evidence.location_confidence)}</div>
                {selectedDetail.application.primary_skip_code && (
                  <div>
                    <strong>{label('Skip Codes:', 'أسباب التخطي:')}</strong> <code>{skipLabel(selectedDetail.application.primary_skip_code)}</code>
                    {selectedDetail.application.secondary_skip_codes?.length > 0 && ` (+${selectedDetail.application.secondary_skip_codes.map(skipLabel).join(locale === 'ar' ? '، ' : ', ')})`}
                  </div>
                )}
                <div><strong>{label('Evidence Quote:', 'مقتطف الدليل:')}</strong> “{selectedDetail.source_evidence.evidence_quote || label('None', 'لا يوجد')}”</div>
                <div><strong>{label('Fit Score Estimate:', 'درجة المطابقة التقديرية:')}</strong> {selectedDetail.match_explanation.fit_score ?? '—'} — {selectedDetail.match_explanation.decision_reason}</div>
              </div>

              <div className="p-3 rounded-lg border border-slate-200 space-y-2">
                <div className="flex flex-wrap gap-2 items-center">
                  <label htmlFor="tracker-edit-outcome" className="font-semibold">{t.tracker.manualOutcome}:</label>
                  <select
                    id="tracker-edit-outcome"
                    value={editOutcome}
                    onChange={(e) => setEditOutcome(e.target.value)}
                    className="rounded border border-slate-300 px-2 py-1 bg-white"
                  >
                    <option value="open">{statusLabel('open')}</option>
                    <option value="interview">{statusLabel('interview')}</option>
                    <option value="rejected">{statusLabel('rejected')}</option>
                    <option value="withdrawn">{statusLabel('withdrawn')}</option>
                    <option value="archived">{statusLabel('archived')}</option>
                  </select>
                </div>
                <textarea
                  rows={2}
                  value={editNotes}
                  onChange={(e) => setEditNotes(e.target.value)}
                  placeholder={t.tracker.notes}
                  className="w-full rounded border border-slate-300 px-2.5 py-1.5"
                />
                <div className="flex flex-wrap gap-2">
                  <button
                    type="button"
                    onClick={handleSaveNotes}
                    className="px-3 py-1.5 rounded bg-indigo-600 text-white font-semibold cursor-pointer"
                  >
                    {t.tracker.saveNoteBtn}
                  </button>
                  <button
                    type="button"
                    onClick={handleSuppress}
                    className="inline-flex items-center gap-1 px-3 py-1.5 rounded border border-rose-200 text-rose-700 font-semibold cursor-pointer"
                  >
                    <ShieldBan className="h-3.5 w-3.5" aria-hidden="true" />
                    <span>{t.tracker.suppressBtn}</span>
                  </button>
                </div>
              </div>

              <div className="p-3 rounded-lg bg-slate-50 border border-slate-200 space-y-1 max-h-40 overflow-y-auto">
                <div className="font-semibold">{label('Timeline Events', 'سجل الأحداث')}</div>
                {(selectedDetail.events || []).map((ev: any) => (
                  <div key={ev.id} className="text-slate-600">
                    [{formatDateTime(ev.occurred_at)}] <strong>{statusLabel(ev.event_type)}</strong>: {ev.snippet}
                  </div>
                ))}
              </div>
            </div>
          </div>
        </section>
      )}
    </div>
  );
};
