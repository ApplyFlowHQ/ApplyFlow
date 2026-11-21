export interface WorkflowCounts {
  imported?: number;
  verified?: number;
  needs_review?: number;
  ready?: number;
  duplicate?: number;
  blocked?: number;
  approved?: number;
  queued?: number;
  provider_accepted?: number;
  failed?: number;
  uncertain?: number;
}

export interface IgnoredBundleFile {
  filename: string;
  reason: string;
}

export interface ImportBundleResponse {
  batch_id: string;
  batch: any;
  duplicate_import: boolean;
  created_applications: number;
  workflow_counts: WorkflowCounts;
  ignored_files: IgnoredBundleFile[];
  dedup_coverage?: any;
  message?: string;
}

export interface CvVersionSummary {
  id: string;
  version_number?: number;
  original_filename: string;
  mime_type: string;
  sha256: string;
  derived_pdf_sha256?: string | null;
  pdf_conversion_status?: string;
  pdf_page_count?: number | null;
  parse_status?: string;
}

export interface CvDocumentSummary {
  id: string;
  variant_label: string;
  is_archived?: boolean;
  versions: CvVersionSummary[];
}

export interface CvPdfOption {
  versionId: string;
  documentId: string;
  label: string;
  filename: string;
  pdfReady: boolean;
  shaPrefix: string;
}

export interface DraftApplicationItem {
  id: string;
  batch_id: string;
  run_mode?: string;
  import_origin?: string;
  is_demo?: boolean;
  company_name?: string | null;
  role_title?: string | null;
  country_code?: string | null;
  city?: string | null;
  source_url?: string | null;
  evidence_excerpt?: string | null;
  location?: string | null;
  verification_explanation?: string | null;
  posted_at?: string | null;
  requested_subject?: string | null;
  required_documents?: string[];
  location_raw?: string | null;
  recipient_email?: string | null;
  cc_emails?: string[];
  subject?: string | null;
  body_text?: string | null;
  language?: string | null;
  selected_cv_version_id?: string | null;
  selected_cv_document_id?: string | null;
  frozen_cv_filename?: string | null;
  attachment_pdf_sha256?: string | null;
  approval_state?: string | null;
  transport_status?: string | null;
  queue_state?: 'ready' | 'approved' | 'queued' | 'sending' | 'sent' | 'failed' | 'blocked' | 'uncertain' | string | null;
  outbox_id?: string | null;
  outbox_status?: string | null;
  outbox_attempt_count?: number | null;
  outbox_max_attempts?: number | null;
  outbox_next_attempt_at?: string | null;
  outbox_last_error?: string | null;
  outbox_handoff_outcome?: string | null;
  outbox_created_at?: string | null;
  outbox_updated_at?: string | null;
  source_verification_tier?: string | null;
  corroboration_status?: string | null;
  corroboration_url?: string | null;
  dns_check_status?: string | null;
  evidence_url?: string | null;
  evidence_quote?: string | null;
  primary_skip_code?: string | null;
  skip_or_error_reason?: string | null;
  review_reasons?: string[];
  review_notes?: string | null;
  draft_content_hash?: string | null;
  approved_content_hash?: string | null;
  approved_at?: string | null;
  gmail_draft_id?: string | null;
  provider_message_id?: string | null;
  sent_at?: string | null;
  fit_score?: number | null;
  why_this_cv?: string | null;
  verified_skills?: string[];
  required_gaps?: string[];
  discovered_at?: string | null;
}

export interface RevalidateResponse {
  application_id: string;
  outcome_bucket?: string;
  approval_state?: string;
  transport_status?: string;
  source_verification_tier?: string;
  dns_status?: string;
  review_reasons?: string[];
  block_codes?: string[];
  skip_reason?: string;
  evidence_quote?: string;
  draft?: DraftApplicationItem;
}

export interface ReviseOrRegenerateResponse {
  approval_invalidated: boolean;
  draft: DraftApplicationItem;
}

export interface RetrySendResponse {
  ok: boolean;
  application_id: string;
  outbox_entry_id?: string;
  outcome?: string;
  reused_existing_outbox?: boolean;
  error?: string;
}

export interface CancelOutboxResponse {
  ok: boolean;
  application_id: string;
  cancelled_outbox_ids?: string[];
  already_cancelled?: boolean;
  transport_status?: string;
  approval_state?: string;
  draft?: DraftApplicationItem;
}

export interface ReconcileOutboxResponse {
  ok: boolean;
  application_id: string;
  reconciled_outcome: 'sent' | 'still_uncertain';
  transport_status?: string;
  provider_message_id?: string | null;
  draft?: DraftApplicationItem;
}

export interface SelfTestResponse {
  ok: boolean;
  outcome: string;
  provider: string;
  recipient_email: string;
  provider_message_id?: string | null;
  rfc_message_id?: string | null;
  attachment_filename?: string | null;
  attachment_sha256?: string | null;
  reconciled_in_sent?: boolean;
  error_message?: string | null;
}

export function isApprovedLocked(item: DraftApplicationItem): boolean {
  return (
    item.approval_state === 'approved' &&
    Boolean(item.approved_content_hash) &&
    item.approved_content_hash === item.draft_content_hash
  );
}

export function isApprovalStale(item: DraftApplicationItem): boolean {
  return (
    Boolean(item.approved_content_hash) &&
    Boolean(item.draft_content_hash) &&
    item.approved_content_hash !== item.draft_content_hash
  );
}

export function isRejectedOrBlocked(item: DraftApplicationItem): boolean {
  return (
    item.approval_state === 'rejected' ||
    item.approval_state === 'blocked' ||
    item.transport_status === 'skipped' ||
    item.transport_status === 'cancelled' ||
    item.transport_status === 'blocked'
  );
}

export function isQueuedInOutbox(item: DraftApplicationItem): boolean {
  return (
    item.transport_status === 'queued' ||
    item.outbox_status === 'queued' ||
    item.outbox_status === 'leased' ||
    item.queue_state === 'queued'
  );
}

export function isSendingInOutbox(item: DraftApplicationItem): boolean {
  return (
    item.transport_status === 'sending' ||
    item.outbox_status === 'sending' ||
    item.queue_state === 'sending'
  );
}

export function isUncertainSend(item: DraftApplicationItem): boolean {
  return (
    item.transport_status === 'send_uncertain' ||
    item.outbox_status === 'send_uncertain' ||
    item.outbox_handoff_outcome === 'ambiguous_handoff' ||
    item.queue_state === 'uncertain'
  );
}

export function getApplicationQueueState(
  item: DraftApplicationItem,
): 'ready' | 'approved' | 'queued' | 'sending' | 'sent' | 'failed' | 'blocked' | 'uncertain' {
  if (item.transport_status === 'sent' || item.transport_status === 'provider_accepted' || item.outbox_status === 'sent') {
    return 'sent';
  }
  if (isUncertainSend(item)) {
    return 'uncertain';
  }
  if (isSendingInOutbox(item)) {
    return 'sending';
  }
  if (isQueuedInOutbox(item)) {
    return 'queued';
  }
  if (item.transport_status === 'failed' || item.outbox_status === 'failed') {
    return 'failed';
  }
  if (isRejectedOrBlocked(item)) {
    return 'blocked';
  }
  if (isApprovedLocked(item)) {
    return 'approved';
  }
  return 'ready';
}

export function isTerminalSentOrInFlight(item: DraftApplicationItem): boolean {
  return (
    item.transport_status === 'sent' ||
    item.transport_status === 'provider_accepted' ||
    item.outbox_status === 'sent' ||
    isQueuedInOutbox(item) ||
    isSendingInOutbox(item) ||
    isUncertainSend(item)
  );
}

export function needsHumanReviewOverride(item: DraftApplicationItem): boolean {
  return (
    item.approval_state === 'needs_human_review' ||
    item.source_verification_tier === 'tier_3_third_party_claim'
  );
}

type ReviewedDraft = DraftApplicationItem & { draft_content_hash: string };

export function canApproveItem(item: DraftApplicationItem): item is ReviewedDraft {
  return (
    Boolean(item.draft_content_hash) &&
    !isRejectedOrBlocked(item) &&
    !isDuplicateItem(item) &&
    !isTerminalSentOrInFlight(item) &&
    !isApprovedLocked(item) &&
    item.transport_status !== 'failed'
  );
}

export function reviewedDraftSelection(items: ReviewedDraft[]) {
  return {
    application_ids: items.map(item => item.id),
    expected_hashes: Object.fromEntries(items.map(item => [item.id, item.draft_content_hash])),
  };
}

export interface DraftApprovalResponse {
  approved_ids: string[];
  failed: { id: string; error: string }[];
}

export function canBulkApproveReady(item: DraftApplicationItem): item is ReviewedDraft {
  return canApproveItem(item) && !needsHumanReviewOverride(item) && !isDuplicateItem(item);
}

export function canSendApprovedItem(item: DraftApplicationItem): boolean {
  const isAlreadySentOrActive =
    item.transport_status === 'sent' ||
    item.transport_status === 'provider_accepted' ||
    item.outbox_status === 'sent' ||
    isSendingInOutbox(item) ||
    isUncertainSend(item);
  return (
    isApprovedLocked(item) &&
    !isRejectedOrBlocked(item) &&
    !isAlreadySentOrActive &&
    !isQueuedInOutbox(item) &&
    item.transport_status !== 'failed' &&
    item.outbox_status !== 'failed'
  );
}

export function isDuplicateItem(item: DraftApplicationItem): boolean {
  return (
    item.primary_skip_code === 'PREVIOUSLY_CONTACTED' ||
    item.primary_skip_code === 'DUPLICATE_SOURCE' ||
    item.primary_skip_code === 'DUPLICATE_IMPORT'
  );
}

export function flattenCvPdfOptions(
  cvs: CvDocumentSummary[],
  alwaysIncludeVersionId?: string | null,
): CvPdfOption[] {
  const options: CvPdfOption[] = [];
  for (const doc of cvs || []) {
    for (const ver of doc.versions || []) {
      const isSelected = Boolean(alwaysIncludeVersionId && ver.id === alwaysIncludeVersionId);
      if (doc.is_archived && !isSelected) continue;
      const pdfReady = ver.mime_type === 'application/pdf' || ver.pdf_conversion_status === 'ready';
      const sha = ver.derived_pdf_sha256 || ver.sha256 || '';
      options.push({
        versionId: ver.id,
        documentId: doc.id,
        label: `${doc.variant_label} — ${ver.original_filename}${pdfReady ? ' (PDF Ready)' : ' (PDF Pending)'}`,
        filename: ver.original_filename,
        pdfReady,
        shaPrefix: sha.slice(0, 10),
      });
    }
  }
  return options;
}

export function getCvPdfDownloadUrl(
  item: DraftApplicationItem,
  cvs: CvDocumentSummary[],
): string | null {
  if (item.selected_cv_document_id && item.selected_cv_version_id) {
    return `/api/v1/cvs/${item.selected_cv_document_id}/versions/${item.selected_cv_version_id}/download-pdf`;
  }
  if (item.selected_cv_version_id) {
    for (const doc of cvs || []) {
      const found = (doc.versions || []).find((v) => v.id === item.selected_cv_version_id);
      if (found) {
        return `/api/v1/cvs/${doc.id}/versions/${found.id}/download-pdf`;
      }
    }
  }
  return null;
}
