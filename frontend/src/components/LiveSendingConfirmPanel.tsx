import React, { useState } from 'react';
import { ShieldAlert, ShieldOff } from 'lucide-react';
import { useI18n } from '../i18n';

interface LiveSendingConfirmPanelProps {
  enabled: boolean;
  lockedByEnv?: boolean;
  busy?: boolean;
  onToggle: (nextEnabled: boolean) => Promise<void>;
  compact?: boolean;
}

/**
 * Prevents accidental 1-click activation of live external email sending:
 * - Turning OFF (enabled -> false) is an instant 1-click kill switch.
 * - Turning ON (disabled -> true) opens an explicit inline confirmation step with checkbox + confirm button.
 */
export const LiveSendingConfirmPanel: React.FC<LiveSendingConfirmPanelProps> = ({
  enabled,
  lockedByEnv = false,
  busy = false,
  onToggle,
  compact = false,
}) => {
  const { t } = useI18n();
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [acknowledged, setAcknowledged] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  const isBusy = busy || submitting;

  const handleDisableImmediate = async () => {
    setSubmitting(true);
    try {
      await onToggle(false);
      setConfirmOpen(false);
      setAcknowledged(false);
    } finally {
      setSubmitting(false);
    }
  };

  const handleConfirmEnable = async () => {
    if (!acknowledged) return;
    setSubmitting(true);
    try {
      await onToggle(true);
      setConfirmOpen(false);
      setAcknowledged(false);
    } finally {
      setSubmitting(false);
    }
  };

  if (enabled) {
    return (
      <button
        type="button"
        disabled={lockedByEnv || isBusy}
        data-testid="disable-live-sending-btn"
        onClick={handleDisableImmediate}
        className="inline-flex items-center gap-1.5 px-3.5 py-2 rounded-lg bg-rose-600 hover:bg-rose-700 text-white text-xs font-semibold cursor-pointer disabled:opacity-50"
      >
        <ShieldOff className="h-3.5 w-3.5" aria-hidden="true" />
        <span>{isBusy ? t.controls.saving : t.liveSending.disableBtn}</span>
      </button>
    );
  }

  return (
    <div className={compact ? 'space-y-2' : 'space-y-3'}>
      {!confirmOpen ? (
        <button
          type="button"
          disabled={lockedByEnv || isBusy}
          data-testid="open-enable-live-sending-btn"
          onClick={() => {
            setAcknowledged(false);
            setConfirmOpen(true);
          }}
          className="px-3.5 py-2 rounded-lg bg-slate-900 hover:bg-slate-800 text-white text-xs font-semibold cursor-pointer disabled:opacity-50"
        >
          {t.liveSending.enableBtn}
        </button>
      ) : (
        <div
          role="region"
          aria-label={t.liveSending.confirmTitle}
          data-testid="live-sending-confirm-panel"
          className="rounded-xl border border-amber-300 bg-amber-50 p-3.5 text-xs text-amber-950 space-y-2.5 max-w-xl"
        >
          <div className="flex items-start gap-2">
            <ShieldAlert className="h-4 w-4 text-amber-700 shrink-0 mt-0.5" aria-hidden="true" />
            <div className="space-y-1">
              <div className="font-bold text-amber-950">{t.liveSending.confirmTitle}</div>
              <p className="text-amber-900 leading-relaxed">{t.liveSending.confirmDesc}</p>
            </div>
          </div>
          <label className="flex items-start gap-2 cursor-pointer font-medium text-amber-950">
            <input
              type="checkbox"
              data-testid="confirm-live-sending-checkbox"
              checked={acknowledged}
              onChange={(e) => setAcknowledged(e.target.checked)}
              className="mt-0.5"
            />
            <span>{t.liveSending.confirmCheckbox}</span>
          </label>
          <div className="flex flex-wrap gap-2 pt-1">
            <button
              type="button"
              disabled={!acknowledged || isBusy}
              data-testid="confirm-live-sending-btn"
              onClick={handleConfirmEnable}
              className="px-3.5 py-1.5 rounded-lg bg-emerald-600 hover:bg-emerald-700 text-white font-semibold cursor-pointer disabled:opacity-40"
            >
              {isBusy ? t.controls.saving : t.liveSending.confirmEnableBtn}
            </button>
            <button
              type="button"
              disabled={isBusy}
              onClick={() => {
                setConfirmOpen(false);
                setAcknowledged(false);
              }}
              className="px-3 py-1.5 rounded-lg border border-amber-300 bg-white text-amber-950 font-semibold hover:bg-amber-100 cursor-pointer"
            >
              {t.controls.cancel}
            </button>
          </div>
        </div>
      )}
    </div>
  );
};
