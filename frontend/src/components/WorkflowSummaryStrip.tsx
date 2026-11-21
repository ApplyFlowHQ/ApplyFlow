import React from 'react';
import { Info } from 'lucide-react';
import { useI18n } from '../i18n';
import type { WorkflowCounts } from '../lib/workflowTypes';

interface WorkflowSummaryStripProps {
  counts?: WorkflowCounts | null;
  showImportNotSendNotice?: boolean;
  isDiscoveryBatch?: boolean;
}

export const WorkflowSummaryStrip: React.FC<WorkflowSummaryStripProps> = ({
  counts,
  showImportNotSendNotice = true,
  isDiscoveryBatch = false,
}) => {
  const { t } = useI18n();
  if (!counts) return null;

  const blockedCount = counts.blocked ?? 0;

  const tiles: {
    key: string;
    label: string;
    value: number;
    accent: string;
  }[] = [
    {
      key: 'imported',
      label: t.workflow.imported,
      value: counts.imported ?? 0,
      accent: 'border-slate-200 bg-white text-slate-900',
    },
    {
      key: 'needs_review',
      label: t.workflow.needsReview,
      value: counts.needs_review ?? 0,
      accent:
        (counts.needs_review ?? 0) > 0
          ? 'border-amber-300 bg-amber-50/70 text-amber-900'
          : 'border-slate-200 bg-white text-slate-900',
    },
    {
      key: 'ready',
      label: t.workflow.ready,
      value: counts.ready ?? 0,
      accent:
        (counts.ready ?? 0) > 0
          ? 'border-indigo-200 bg-indigo-50/60 text-indigo-900'
          : 'border-slate-200 bg-white text-slate-900',
    },
    {
      key: 'duplicate',
      label: t.workflow.duplicate,
      value: counts.duplicate ?? 0,
      accent: 'border-slate-200 bg-slate-50 text-slate-800',
    },
    ...(blockedCount > 0
      ? [
          {
            key: 'blocked',
            label: t.workflow.blocked,
            value: blockedCount,
            accent: 'border-rose-200 bg-rose-50/70 text-rose-900',
          },
        ]
      : []),
    {
      key: 'approved',
      label: t.workflow.approved,
      value: counts.approved ?? 0,
      accent:
        (counts.approved ?? 0) > 0
          ? 'border-emerald-200 bg-emerald-50/60 text-emerald-900'
          : 'border-slate-200 bg-white text-slate-900',
    },
    {
      key: 'queued',
      label: t.workflow.queued,
      value: counts.queued ?? 0,
      accent:
        (counts.queued ?? 0) > 0
          ? 'border-indigo-300 bg-indigo-50 text-indigo-950'
          : 'border-slate-200 bg-white text-slate-900',
    },
    {
      key: 'provider_accepted',
      label: t.workflow.providerAccepted,
      value: counts.provider_accepted ?? 0,
      accent:
        (counts.provider_accepted ?? 0) > 0
          ? 'border-emerald-400 bg-emerald-100/70 text-emerald-950'
          : 'border-slate-200 bg-white text-slate-900',
    },
    {
      key: 'failed',
      label: t.workflow.failed,
      value: counts.failed ?? 0,
      accent:
        (counts.failed ?? 0) > 0
          ? 'border-rose-300 bg-rose-50 text-rose-900'
          : 'border-slate-200 bg-white text-slate-900',
    },
    {
      key: 'uncertain',
      label: t.workflow.uncertain,
      value: counts.uncertain ?? 0,
      accent:
        (counts.uncertain ?? 0) > 0
          ? 'border-purple-300 bg-purple-50 text-purple-900'
          : 'border-slate-200 bg-white text-slate-900',
    },
  ];

  return (
    <div
      role="region"
      aria-label={t.workflow.summaryRegionLabel}
      data-testid="workflow-summary-strip"
      className="space-y-2"
    >
      <div className="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-8 xl:grid-cols-9 gap-2">
        {tiles.filter(tile => tile.key !== 'imported' || !isDiscoveryBatch).map((tile) => (
          <div
            key={tile.key}
            data-testid={`wf-count-${tile.key}`}
            className={`rounded-lg border px-2.5 py-2 ${tile.accent}`}
          >
            <div className="text-[10px] font-medium opacity-80 leading-tight">{tile.label}</div>
            <div className="text-base font-bold mt-0.5 tabular-nums">{tile.value}</div>
          </div>
        ))}
      </div>
      {showImportNotSendNotice && !isDiscoveryBatch && (
        <div className="flex items-start gap-1.5 text-[11px] text-slate-600 bg-slate-50 border border-slate-200 rounded-lg px-2.5 py-1.5">
          <Info className="h-3.5 w-3.5 text-indigo-600 shrink-0 mt-0.5" aria-hidden="true" />
          <span>{t.workflow.importNotSendNotice}</span>
        </div>
      )}
    </div>
  );
};
