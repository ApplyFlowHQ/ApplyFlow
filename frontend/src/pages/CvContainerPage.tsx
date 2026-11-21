import React, { useEffect, useState } from 'react';
import { AlertTriangle, Archive, Download, FileText, RefreshCw, Trash2, Upload } from 'lucide-react';
import { useI18n } from '../i18n';
import { apiRequest, ApiError } from '../lib/api';

const arabicCvLabels: Record<string, string> = {
  parsed: 'اكتمل التحليل', pending: 'قيد الانتظار', failed: 'تعذّر التحليل', ocr_required: 'يتطلب التعرف الضوئي على النص',
  unsupported_language: 'لغة غير مدعومة', unknown: 'غير محدد', en: 'الإنجليزية', ar: 'العربية',
  tool: 'أداة', skill: 'مهارة', experience: 'خبرة', certification: 'شهادة مهنية', education: 'مؤهل دراسي',
  project: 'مشروع', achievement: 'إنجاز', language: 'لغة', role: 'وظيفة',
  evidenced: 'موثّق في السيرة الذاتية', confirmed: 'أكّده المستخدم', superseded: 'جرى تصحيحه', retired: 'بيان سابق',
};

export const CvContainerPage: React.FC<{ onUpdated?: () => void }> = ({ onUpdated }) => {
  const { t, locale, formatDateTime } = useI18n();
  const arabic = locale === 'ar';
  const label = (value: string) => (arabic && arabicCvLabels[value]) || value;
  const number = (value: number) => value.toLocaleString(locale);
  const versionLabel = (version: number) => arabic ? `الإصدار ${number(version)}` : `v${version}`;
  const [cvs, setCvs] = useState<any[]>([]);
  const [supportingFiles, setSupportingFiles] = useState<any[]>([]);
  const [selectedFacts, setSelectedFacts] = useState<any | null>(null);
  const [variantInput, setVariantLabel] = useState<string | null>(null);
  const variantLabel = variantInput ?? (arabic ? 'تحليلات' : 'Analytics');
  const [documentId, setDocumentId] = useState('');
  const [displayVersion, setDisplayVersion] = useState<Record<string, string>>({});
  const [editingFact, setEditingFact] = useState('');
  const [statement, setStatement] = useState('');
  const [reason, setReason] = useState('');
  const [cvFile, setCvFile] = useState<File | null>(null);
  const [sfLabel, setSfLabel] = useState('');
  const [sfKeywords, setSfKeywords] = useState('');
  const [sfFile, setSfFile] = useState<File | null>(null);
  const [profile, setProfile] = useState<any>({
    full_name: '',
    headline: '',
    contact_email: '',
    phone: '',
    residence_country_code: '',
    residence_city: '',
    notice_period_days: '',
    work_authorizations: '',
    nationalities: '',
    residency_permits: '',
    confirmed_certifications: '',
    confirmed_languages: '',
    requires_sponsorship: false,
    willing_to_relocate: false,
    relocation_targets: '',
    preferred_roles: '',
    salary_expectation_min: '',
    salary_currency: '',
    salary_period: 'monthly',
    salary_basis: 'unknown',
    signature_text: '',
    portfolio_url: '',
    portfolio_enabled: true,
    consistency_warnings: [],
  });
  const [msg, setMsg] = useState('');
  const [err, setErr] = useState('');
  const [busy, setBusy] = useState(false);

  const loadAll = async () => {
    const [cvRes, sfRes, profRes] = await Promise.all([
      apiRequest<{ cvs: any[] }>('/cvs'),
      apiRequest<{ supporting_files: any[] }>('/supporting-files'),
      apiRequest<{ profile: any }>('/profile'),
    ]);
    setCvs(cvRes.cvs || []);
    setSupportingFiles(sfRes.supporting_files || []);
    const p = profRes.profile || {};
    setProfile({
      ...p,
      full_name: p.full_name || '',
      headline: p.headline || '',
      contact_email: p.contact_email || '',
      phone: p.phone || '',
      residence_country_code: p.residence_country_code || '',
      residence_city: p.residence_city || '',
      notice_period_days: p.notice_period_days ?? '',
      work_authorizations: (p.work_authorizations || []).join(', '),
      nationalities: (p.nationalities || []).join(', '),
      residency_permits: (p.residency_permits || []).join(', '),
      confirmed_certifications: (p.confirmed_certifications || []).join(', '),
      confirmed_languages: (p.confirmed_languages || []).map((l: any) => `${l.language}:${l.level}`).join(', '),
      requires_sponsorship: p.requires_sponsorship ?? null,
      willing_to_relocate: Boolean(p.willing_to_relocate),
      relocation_targets: (p.relocation_targets || []).join(', '),
      preferred_roles: (p.preferred_roles || []).join(', '),
      salary_expectation_min: p.salary_expectation_min ?? '',
      salary_currency: p.salary_currency || '',
      salary_period: p.salary_period || 'monthly',
      salary_basis: p.salary_basis || 'unknown',
      signature_text: p.signature_text || '',
      portfolio_url: p.portfolio_links?.[0]?.url || '',
      portfolio_enabled: p.portfolio_links?.[0]?.enabled ?? true,
      consistency_warnings: p.consistency_warnings || [],
    });
  };

  useEffect(() => {
    loadAll().catch((e) => setErr(String(e)));
  }, []);

  const handleCvUpload = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!cvFile) return;
    setBusy(true);
    setErr('');
    setMsg('');
    try {
      const form = new FormData();
      form.append('variant_label', variantLabel || (arabic ? 'عامة' : 'General'));
      if (documentId) form.append('document_id', documentId);
      form.append('file', cvFile);
      const res = await apiRequest('/cvs/upload', { method: 'POST', body: form });
      setMsg(arabic
        ? `رُفعت السيرة الذاتية. حالة التحليل: ${label(res.parse_status)}. عدد البيانات المستخرجة: ${number(res.facts_count)}. أشهر الخبرة بعد استبعاد التداخل: ${number(res.total_experience_months)}.`
        : `Uploaded CV (${res.parse_status}, ${res.facts_count} extracted facts, ${res.total_experience_months} non-overlapping months).`);
      setCvFile(null);
      await loadAll();
      onUpdated?.();
    } catch (e2) {
      if (arabic && e2 instanceof ApiError && e2.code === 'FILE_TOO_LARGE') {
        setErr('يتجاوز حجم الملف الحد المسموح به.');
      } else {
        setErr(arabic ? 'تعذّر رفع السيرة الذاتية. تحقّق من الاتصال واختر ملف PDF أو DOCX صالحاً.' : e2 instanceof ApiError ? e2.message : 'Upload failed');
      }
    } finally {
      setBusy(false);
    }
  };

  const handleInspectVersion = async (docId: string, verId: string) => {
    const res = await apiRequest<{ version: any }>(`/cvs/${docId}/versions/${verId}`);
    setSelectedFacts(res.version);
    setEditingFact('');
  };

  const handleReparse = async (docId: string, verId: string) => {
    try {
      await apiRequest(`/cvs/${docId}/versions/${verId}/reparse`, { method: 'POST' });
      await loadAll();
      if (selectedFacts?.id === verId) await handleInspectVersion(docId, verId);
    } catch (e) {
      setErr(arabic ? 'تعذّرت إعادة تحليل السيرة الذاتية.' : e instanceof Error ? e.message : 'Reparse failed');
    }
  };

  const saveCorrection = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setErr('');
    try {
      await apiRequest(`/cvs/${selectedFacts.document_id}/versions/${selectedFacts.id}/facts/${editingFact}/corrections`, {
        method: 'POST', body: JSON.stringify({ statement, reason }),
      });
      await handleInspectVersion(selectedFacts.document_id, selectedFacts.id);
      setMsg(arabic ? 'حُفظ التصحيح الذي أكّدته.' : 'Saved your confirmed correction.');
      onUpdated?.();
    } catch (error) {
      setErr(arabic ? (error instanceof ApiError && error.status === 409
        ? 'تغيّر هذا البيان. أعد فتح الإصدار للاطلاع على أحدث تصحيح.' : 'تعذّر حفظ التصحيح.')
        : error instanceof Error ? error.message : 'Correction failed');
    } finally {
      setBusy(false);
    }
  };

  const handleArchive = async (docId: string) => {
    await apiRequest(`/cvs/${docId}/archive`, { method: 'POST' });
    await loadAll();
  };

  const handleSupportingUpload = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!sfFile || !sfLabel.trim()) return;
    const form = new FormData();
    form.append('label', sfLabel.trim());
    form.append('keywords', sfKeywords.replaceAll('،', ','));
    form.append('enabled_for_matching', 'true');
    form.append('file', sfFile);
    await apiRequest('/supporting-files', { method: 'POST', body: form });
    setSfLabel('');
    setSfKeywords('');
    setSfFile(null);
    await loadAll();
  };

  const handleSaveProfile = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setErr('');
    setMsg('');
    try {
      await apiRequest('/profile', {
        method: 'PUT',
        body: JSON.stringify({
          full_name: profile.full_name,
          headline: profile.headline,
          contact_email: profile.contact_email,
          availability_note: profile.availability_note,
          salary_expectation_max: profile.salary_expectation_max,
          residence_geo_id: profile.residence_geo_id,
          phone: profile.phone,
          residence_country_code: profile.residence_country_code,
          residence_city: profile.residence_city,
          notice_period_days: profile.notice_period_days === '' ? null : Number(profile.notice_period_days),
          work_authorizations: profile.work_authorizations.split(/[,،]/).map((s: string) => s.trim()).filter(Boolean),
          nationalities: profile.nationalities.split(/[,،]/).map((s: string) => s.trim()).filter(Boolean),
          residency_permits: profile.residency_permits.split(/[,،]/).map((s: string) => s.trim()).filter(Boolean),
          confirmed_certifications: profile.confirmed_certifications.split(/[,،]/).map((s: string) => s.trim()).filter(Boolean),
          confirmed_languages: profile.confirmed_languages
            .split(/[,،]/)
            .map((s: string) => s.trim())
            .filter(Boolean)
            .map((entry: string) => {
              const [language, level] = entry.split(':').map((x) => x.trim());
              return { language, level: level || 'unknown' };
            }),
          requires_sponsorship: profile.requires_sponsorship,
          willing_to_relocate: profile.willing_to_relocate,
          relocation_targets: profile.relocation_targets.split(/[,،]/).map((s: string) => s.trim()).filter(Boolean),
          preferred_roles: profile.preferred_roles.split(/[,،]/).map((s: string) => s.trim()).filter(Boolean),
          salary_expectation_min: profile.salary_expectation_min === '' ? null : Number(profile.salary_expectation_min),
          salary_currency: profile.salary_currency,
          salary_period: profile.salary_period,
          salary_basis: profile.salary_basis,
          signature_text: profile.signature_text,
          portfolio_links: profile.portfolio_url
            ? [{ label: profile.portfolio_links?.[0]?.label || 'Portfolio', url: profile.portfolio_url, enabled: profile.portfolio_enabled }, ...(profile.portfolio_links || []).slice(1)]
            : (profile.portfolio_links || []).slice(1),
          confirm_profile: true,
        }),
      });
      setMsg(arabic ? 'حُفظ ملف المرشح وأُكّدت بياناته.' : 'Candidate profile confirmed and saved.');
      await loadAll();
      onUpdated?.();
    } catch (e2) {
      setErr(arabic ? 'تعذّر حفظ ملف المرشح.' : e2 instanceof ApiError ? e2.message : 'Failed to save profile');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-8">
      <h1 className="text-2xl font-bold text-slate-900">{t.cvs.title}</h1>

      {msg && <div role="status" className="p-3 rounded-lg bg-emerald-50 border border-emerald-200 text-emerald-800 text-sm">{msg}</div>}
      {err && <div role="alert" className="p-3 rounded-lg bg-rose-50 border border-rose-200 text-rose-800 text-sm">{err}</div>}

      {profile.consistency_warnings?.length > 0 && (
        <div className="p-4 rounded-xl bg-amber-50 border border-amber-200 text-amber-900 space-y-1">
          <div className="font-semibold text-sm flex items-center gap-2">
            <AlertTriangle className="h-4 w-4" aria-hidden="true" />
            <span>{t.cvs.conflictsTitle}</span>
          </div>
          {profile.consistency_warnings.map((w: any, i: number) => (
            <p key={i} className="text-xs">{w.message}</p>
          ))}
        </div>
      )}

      {/* Upload CV Variant */}
      <section className="bg-white rounded-xl border border-slate-200 p-5 shadow-xs space-y-4">
        <h2 className="text-base font-semibold text-slate-900">{t.cvs.uploadHeading}</h2>
        <label className="block text-sm"><span id="cv-upload-destination">{arabic ? 'وجهة الرفع' : 'Upload to'}</span>
          <select aria-labelledby="cv-upload-destination" value={documentId} onChange={(e) => setDocumentId(e.target.value)} className="ms-2 rounded border p-1">
            <option value="">{arabic ? 'سيرة ذاتية جديدة' : 'New CV variant'}</option>
            {cvs.map(doc => <option key={doc.id} value={doc.id}>{doc.variant_label} · {arabic ? 'إصدار جديد' : 'new version'}</option>)}
          </select>
        </label>
        <form onSubmit={handleCvUpload} className="grid grid-cols-1 sm:grid-cols-3 gap-3 items-end">
          <div>
            <label className="block text-xs font-semibold text-slate-700 mb-1" htmlFor="cv-variant">
              {t.cvs.variantLabel}
            </label>
            <input
              id="cv-variant"
              type="text"
              disabled={Boolean(documentId)}
              value={documentId ? cvs.find(doc => doc.id === documentId)?.variant_label || '' : variantLabel}
              onChange={(e) => setVariantLabel(e.target.value)}
              className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
            />
          </div>
          <div>
            <label className="block text-xs font-semibold text-slate-700 mb-1" htmlFor="cv-file">
              PDF / DOCX
            </label>
            <input
              id="cv-file"
              type="file"
              accept=".pdf,.docx,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
              onChange={(e) => setCvFile(e.target.files?.[0] || null)}
              className="w-full text-sm text-slate-600 file:me-3 file:py-1.5 file:px-3 file:rounded-md file:border-0 file:bg-slate-100 file:text-slate-700"
            />
          </div>
          <button
            type="submit"
            disabled={busy || !cvFile}
            className="inline-flex items-center justify-center gap-2 px-4 py-2 rounded-lg bg-indigo-600 hover:bg-indigo-700 text-white text-sm font-semibold cursor-pointer disabled:opacity-50"
          >
            <Upload className="h-4 w-4" aria-hidden="true" />
            <span>{t.cvs.uploadBtn}</span>
          </button>
        </form>

        {/* CV List */}
        <div className="space-y-3 pt-2">
          {cvs.map((doc) => {
            const v = doc.versions?.find((item: any) => item.id === (displayVersion[doc.id] || doc.active_version_id)) || doc.versions?.[0];
            return (
              <div key={doc.id} className={`p-4 rounded-xl border ${doc.is_archived ? 'bg-slate-50 border-slate-200 opacity-75' : 'bg-white border-slate-200'}`}>
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex items-center gap-2">
                    <FileText className="h-5 w-5 text-indigo-600" aria-hidden="true" />
                    <div>
                      <div className="font-semibold text-sm text-slate-900">
                        {doc.variant_label} {v && <>· {versionLabel(v.version_number)} (<bdi>{v.original_filename}</bdi>)</>}
                      </div>
                      {v && (
                        <div className="text-xs text-slate-500 flex flex-wrap items-center gap-1.5">
                          <span>{arabic ? 'الحالة' : 'Status'}: <span className="font-medium">{label(v.parse_status)}</span> · {arabic ? 'اللغة' : 'Lang'}: {label(v.detected_language)} · {t.cvs.experienceMonths}: {number(v.total_experience_months)} · SHA256: <bdi>{v.sha256.slice(0, 12)}…</bdi></span>
                          <span
                            className={`px-1.5 py-0.5 rounded font-semibold text-[10px] ${
                              v.pdf_conversion_status === 'ready'
                                ? 'bg-emerald-100 text-emerald-800'
                                : 'bg-amber-100 text-amber-900'
                            }`}
                          >
                            {v.pdf_conversion_status === 'ready'
                              ? `${t.cvs.pdfReadyBadge}${v.pdf_page_count ? ` (${arabic ? 'عدد الصفحات: ' : ''}${number(v.pdf_page_count)}${arabic ? '' : 'p'})` : ''}`
                              : v.pdf_conversion_status === 'failed' ? (arabic ? 'ملف PDF غير متاح' : 'PDF unavailable') : t.cvs.pdfPendingBadge}
                          </span>
                        </div>
                      )}
                    </div>
                  </div>

                  {v && (
                    <div className="flex flex-wrap gap-2">
                      <button
                        type="button"
                        onClick={() => handleInspectVersion(doc.id, v.id)}
                        className="px-2.5 py-1.5 rounded-md border border-slate-200 text-xs font-medium hover:bg-slate-50 cursor-pointer"
                      >
                        {t.cvs.factsTitle}
                      </button>
                      <a
                        href={`/api/v1/cvs/${doc.id}/versions/${v.id}/download`}
                        className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-md border border-slate-200 text-xs font-medium hover:bg-slate-50"
                      >
                        <Download className="h-3.5 w-3.5" aria-hidden="true" />
                        <span>{t.cvs.downloadOriginal}</span>
                      </a>
                      {v.pdf_conversion_status === 'ready' && v.mime_type !== 'application/pdf' && (
                        <a
                          href={`/api/v1/cvs/${doc.id}/versions/${v.id}/download-pdf`}
                          className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-md border border-emerald-200 bg-emerald-50 text-emerald-800 text-xs font-medium hover:bg-emerald-100"
                        >
                          <Download className="h-3.5 w-3.5" aria-hidden="true" />
                          <span>{t.cvs.downloadPdf}</span>
                        </a>
                      )}
                      <button
                        type="button"
                        onClick={() => handleReparse(doc.id, v.id)}
                        className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-md border border-slate-200 text-xs font-medium hover:bg-slate-50 cursor-pointer"
                      >
                        <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
                        <span>{t.cvs.reparse}</span>
                      </button>
                      <button
                        type="button"
                        onClick={() => handleArchive(doc.id)}
                        className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-md border border-slate-200 text-xs font-medium hover:bg-slate-50 cursor-pointer"
                      >
                        <Archive className="h-3.5 w-3.5" aria-hidden="true" />
                        <span>{doc.is_archived ? t.cvs.unarchive : t.cvs.archive}</span>
                      </button>
                    </div>
                  )}
                </div>

                {doc.versions.length > 1 && (
                  <label className="text-xs"><span id={`cv-version-label-${doc.id}`}>{arabic ? 'الإصدار' : 'Version'}</span>
                    <select aria-labelledby={`cv-version-label-${doc.id}`} className="ms-2 p-1" value={v.id} onChange={e => {
                      setDisplayVersion({ ...displayVersion, [doc.id]: e.target.value });
                      setSelectedFacts(null);
                    }}>
                      {doc.versions.map((item: any) => <option key={item.id} value={item.id}>
                        {versionLabel(item.version_number)} · {item.original_filename}{item.id === doc.active_version_id ? (arabic ? ' (الحالي)' : ' (active)') : ''}
                      </option>)}
                    </select>
                  </label>
                )}
                {v?.pdf_conversion_status === 'failed' && (
                  <p className="mt-2 text-sm text-rose-800">{arabic ? 'تعذّر تجهيز ملف PDF. ارفع نسخة بصيغة PDF أو أعد تحليل الملف.' : `${v.parse_error || v.pdf_conversion_error}. Upload a PDF version or retry parsing.`}</p>
                )}
                {v?.parse_status === 'ocr_required' && (
                  <p className="mt-2 text-xs text-amber-800 bg-amber-50 p-2 rounded">{t.cvs.ocrRequiredWarning}</p>
                )}
              </div>
            );
          })}
        </div>

        {selectedFacts && (
          <div className="mt-4 p-4 rounded-xl bg-slate-50 border border-slate-200 space-y-2">
            <div className="flex items-center justify-between">
              <h3 className="text-sm font-semibold text-slate-900">
                {t.cvs.factsTitle}: {selectedFacts.original_filename}
              </h3>
              <button type="button" onClick={() => setSelectedFacts(null)} className="text-xs text-slate-500 hover:underline cursor-pointer">
                {arabic ? 'إغلاق' : 'Close'}
              </button>
            </div>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-2 max-h-72 overflow-y-auto">
              {(selectedFacts.facts || []).map((f: any) => (
                <div key={f.id} className="p-2.5 rounded-lg bg-white border border-slate-200 text-xs">
                  <div className="font-semibold text-indigo-700 uppercase">{label(f.category)} · {label(f.confidence)}{f.confidence === 'evidenced' && selectedFacts.mime_type === 'application/pdf' ? ` · ${arabic ? 'الصفحة' : 'p.'} ${number(f.source_page)}` : ''}</div>
                  <div className="font-medium text-slate-900 mt-0.5">{f.title}</div>
                  <div className="text-slate-500 mt-1 italic">“{f.evidence_quote}”</div>
                  {(selectedFacts.corrections || []).filter((change: any) => change.confirmed_fact_id === f.id).map((change: any) => (
                    <p key={change.confirmed_fact_id}>{arabic ? 'تاريخ التأكيد' : 'Confirmed'} {formatDateTime(change.confirmed_at)} · {change.reason}
                      <br />{arabic ? 'القيمة السابقة' : 'Previous value'}: {selectedFacts.facts.find((source: any) => source.id === change.source_fact_id)?.value}
                    </p>
                  ))}
                  {['evidenced', 'confirmed'].includes(f.confidence) && (
                    <button type="button" className="mt-1 underline" onClick={() => {
                      setEditingFact(f.id); setStatement(f.value); setReason('');
                    }}>{arabic ? 'تصحيح البيان' : 'Correct fact'}</button>
                  )}
                </div>
              ))}
            </div>
            {editingFact && (
              <form onSubmit={saveCorrection} className="grid gap-2 text-sm">
                <label>{arabic ? 'البيان المؤكّد' : 'Confirmed statement'}
                  <input required maxLength={240} value={statement} onChange={e => setStatement(e.target.value)} className="block w-full rounded border p-2" />
                </label>
                <label>{arabic ? 'سبب التصحيح' : 'Reason for correction'}
                  <input required maxLength={300} value={reason} onChange={e => setReason(e.target.value)} className="block w-full rounded border p-2" />
                </label>
                <button disabled={busy || !statement.trim() || !reason.trim()} className="justify-self-start rounded border px-3 py-1">{arabic ? 'حفظ التصحيح' : 'Save correction'}</button>
              </form>
            )}
          </div>
        )}
      </section>

      {/* Supporting Attachments */}
      <section className="bg-white rounded-xl border border-slate-200 p-5 shadow-xs space-y-4">
        <div>
          <h2 className="text-base font-semibold text-slate-900">{t.cvs.supportingTitle}</h2>
          <p className="text-xs text-slate-500">{t.cvs.supportingNote}</p>
        </div>
        <form onSubmit={handleSupportingUpload} className="grid grid-cols-1 sm:grid-cols-4 gap-3 items-end">
          <input
            type="text"
            placeholder={arabic ? 'اسم الملف (مثل دراسة حالة بصيغة PDF)' : 'Label (e.g. Case Study PDF)'}
            value={sfLabel}
            onChange={(e) => setSfLabel(e.target.value)}
            className="rounded-lg border border-slate-300 px-3 py-2 text-sm"
          />
          <input
            type="text"
            placeholder={arabic ? 'كلمات مفتاحية تصف الملف (مفصولة بفواصل)' : 'Relevance keywords (comma-separated)'}
            value={sfKeywords}
            onChange={(e) => setSfKeywords(e.target.value)}
            className="rounded-lg border border-slate-300 px-3 py-2 text-sm"
          />
          <input
            type="file"
            accept=".pdf,.docx"
            onChange={(e) => setSfFile(e.target.files?.[0] || null)}
            className="text-sm text-slate-600"
          />
          <button
            type="submit"
            disabled={!sfFile || !sfLabel.trim()}
            className="px-4 py-2 rounded-lg bg-slate-900 text-white text-sm font-semibold cursor-pointer disabled:opacity-50"
          >
            {t.cvs.supportingUploadBtn}
          </button>
        </form>
        {supportingFiles.length > 0 && (
          <ul className="space-y-2">
            {supportingFiles.map((sf) => (
              <li key={sf.id} className="flex items-center justify-between p-2.5 rounded-lg border border-slate-200 text-xs">
                <span>
                  <strong>{sf.label}</strong> ({sf.original_filename}) · {arabic ? 'الكلمات المفتاحية' : 'Keywords'}: {(sf.keywords || []).join(arabic ? '، ' : ', ') || (arabic ? 'عند طلب نماذج الأعمال فقط' : 'Portfolio request only')}
                </span>
                <button
                  type="button"
                  onClick={async () => {
                    await apiRequest(`/supporting-files/${sf.id}`, { method: 'DELETE' });
                    await loadAll();
                  }}
                  className="text-rose-600 hover:text-rose-800 cursor-pointer"
                  aria-label={arabic ? 'حذف الملف الداعم' : 'Delete supporting file'}
                >
                  <Trash2 className="h-4 w-4" aria-hidden="true" />
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>

      {/* Candidate Profile */}
      <section className="bg-white rounded-xl border border-slate-200 p-5 shadow-xs space-y-4">
        <div>
          <h2 className="text-base font-semibold text-slate-900">{t.cvs.profileHeading}</h2>
          <p className="text-xs text-slate-500">{t.cvs.profileNote}</p>
        </div>

        <form onSubmit={handleSaveProfile} className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div>
            <label className="block text-xs font-semibold text-slate-700 mb-1">{arabic ? 'الاسم الكامل' : 'Full Name'}</label>
            <input
              type="text"
              value={profile.full_name}
              onChange={(e) => setProfile({ ...profile, full_name: e.target.value })}
              className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
            />
          </div>
          <div>
            <label className="block text-xs font-semibold text-slate-700 mb-1">{arabic ? 'البريد الإلكتروني للتواصل' : 'Contact Email'}</label>
            <input
              type="email"
              value={profile.contact_email}
              onChange={(e) => setProfile({ ...profile, contact_email: e.target.value })}
              className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
            />
          </div>
          <div>
            <label className="block text-xs font-semibold text-slate-700 mb-1">{arabic ? 'رمز بلد الإقامة (مثل AE أو EG)' : 'Residence Country ISO Code (e.g. AE, EG, GB, DE)'}</label>
            <input
              type="text"
              value={profile.residence_country_code}
              onChange={(e) => setProfile({ ...profile, residence_country_code: e.target.value })}
              className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
            />
          </div>
          <div>
            <label className="block text-xs font-semibold text-slate-700 mb-1">{arabic ? 'مدينة الإقامة' : 'Residence City'}</label>
            <input
              type="text"
              value={profile.residence_city}
              onChange={(e) => setProfile({ ...profile, residence_city: e.target.value })}
              className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
            />
          </div>
          <div>
            <label className="block text-xs font-semibold text-slate-700 mb-1">{arabic ? 'تصاريح العمل (رموز الدول، مفصولة بفواصل)' : 'Work Authorizations (ISO country codes, comma-separated)'}</label>
            <input
              type="text"
              placeholder="AE, SA, EG"
              value={profile.work_authorizations}
              onChange={(e) => setProfile({ ...profile, work_authorizations: e.target.value })}
              className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
            />
          </div>
          <div>
            <label className="block text-xs font-semibold text-slate-700 mb-1">{arabic ? 'الجنسيات المؤكّدة (رموز الدول، اختياري)' : 'Confirmed Nationalities / Citizenships (ISO codes, optional — never inferred)'}</label>
            <input
              type="text"
              placeholder={arabic ? 'اتركه فارغاً إن لم تكن الجنسية مؤكّدة (مثل IN أو US)' : 'Leave blank if unconfirmed (e.g. IN, US, GB)'}
              value={profile.nationalities}
              onChange={(e) => setProfile({ ...profile, nationalities: e.target.value })}
              className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
            />
          </div>
          <div>
            <label className="block text-xs font-semibold text-slate-700 mb-1">{arabic ? 'تصاريح الإقامة أو العمل القابلة للنقل (اختياري)' : 'Residency / Transferable Work Permits (optional)'}</label>
            <input
              type="text"
              placeholder={arabic ? 'مثل SA_TRANSFERABLE_IQAMA أو US_GREEN_CARD' : 'e.g. SA_TRANSFERABLE_IQAMA, QA_TRANSFERABLE_QID, US_GREEN_CARD'}
              value={profile.residency_permits}
              onChange={(e) => setProfile({ ...profile, residency_permits: e.target.value })}
              className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
            />
          </div>
          <div>
            <label className="block text-xs font-semibold text-slate-700 mb-1">{arabic ? 'الرخص والشهادات المؤكّدة (اختياري)' : 'Confirmed Licences & Certifications (optional)'}</label>
            <input
              type="text"
              placeholder={arabic ? 'مثل رخصة قيادة إماراتية، PMP، NEBOSH' : 'e.g. UAE Driving Licence, PMP, NEBOSH'}
              value={profile.confirmed_certifications}
              onChange={(e) => setProfile({ ...profile, confirmed_certifications: e.target.value })}
              className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
            />
          </div>
          <div>
            <label className="block text-xs font-semibold text-slate-700 mb-1">{arabic ? 'اللغات ومستويات الإتقان المؤكّدة (اختياري)' : 'Confirmed Languages & Levels (optional)'}</label>
            <input
              type="text"
              placeholder={arabic ? 'مثل English: fluent، Arabic: beginner' : 'e.g. English: fluent, Arabic: beginner'}
              value={profile.confirmed_languages}
              onChange={(e) => setProfile({ ...profile, confirmed_languages: e.target.value })}
              className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
            />
          </div>
          <div>
            <label className="block text-xs font-semibold text-slate-700 mb-1">{arabic ? 'مدة الإشعار بالأيام (اتركها فارغة إن لم تكن مؤكّدة)' : 'Notice Period (days, blank if unconfirmed)'}</label>
            <input
              type="number"
              min={0}
              max={365}
              value={profile.notice_period_days}
              onChange={(e) => setProfile({ ...profile, notice_period_days: e.target.value })}
              className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
            />
          </div>
          <div>
            <label className="block text-xs font-semibold text-slate-700 mb-1">{arabic ? 'رابط نماذج الأعمال (اختياري)' : 'Portfolio Link (optional)'}</label>
            <input
              type="url"
              placeholder="https://..."
              value={profile.portfolio_url}
              onChange={(e) => setProfile({ ...profile, portfolio_url: e.target.value })}
              className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
            />
          </div>
          <div className="flex flex-col justify-center gap-2 pt-4">
            <label className="inline-flex items-center gap-2 text-sm text-slate-700">
              <input
                type="checkbox"
                checked={profile.requires_sponsorship === true}
                onChange={(e) => setProfile({ ...profile, requires_sponsorship: e.target.checked })}
              />
              <span>{arabic ? 'أحتاج إلى كفالة تأشيرة العمل' : 'Requires visa sponsorship'}</span>
            </label>
            <label className="inline-flex items-center gap-2 text-sm text-slate-700">
              <input
                type="checkbox"
                checked={profile.willing_to_relocate}
                onChange={(e) => setProfile({ ...profile, willing_to_relocate: e.target.checked })}
              />
              <span>{arabic ? 'أرغب في الانتقال للعمل' : 'Willing to relocate'}</span>
            </label>
          </div>
          <div className="md:col-span-2">
            <label className="block text-xs font-semibold text-slate-700 mb-1">{arabic ? 'توقيع البريد الإلكتروني' : 'Email Signature'}</label>
            <textarea
              rows={3}
              value={profile.signature_text}
              onChange={(e) => setProfile({ ...profile, signature_text: e.target.value })}
              className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
            />
          </div>
          <div className="md:col-span-2">
            <button
              type="submit"
              disabled={busy}
              className="px-5 py-2.5 rounded-lg bg-indigo-600 hover:bg-indigo-700 text-white text-sm font-semibold cursor-pointer disabled:opacity-50"
            >
              {t.cvs.confirmProfileBtn}
            </button>
          </div>
        </form>
      </section>
    </div>
  );
};
