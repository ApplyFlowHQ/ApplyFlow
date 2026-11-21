import React, { useEffect, useState } from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  Copy,
  ExternalLink,
  KeyRound,
  MailCheck,
  Search,
  Sparkles,
  Trash2,
} from 'lucide-react';
import { useI18n } from '../i18n';
import { apiRequest, ApiError } from '../lib/api';
import type { ModelOption, SetupStatusCode, SetupStatusResponse, ValidationEntry } from '../lib/setupTypes';

export const StatusBadge: React.FC<{ status: SetupStatusCode }> = ({ status }) => {
  const { t } = useI18n();
  const label = (t.setup.statusLabels as Record<string, string>)[status] || status;
  const tone =
    status === 'verified'
      ? 'bg-emerald-50 text-emerald-700 border-emerald-200'
      : status === 'needs_attention'
        ? 'bg-rose-50 text-rose-700 border-rose-200'
        : status === 'saved_not_tested'
          ? 'bg-amber-50 text-amber-800 border-amber-200'
          : status === 'skipped' || status === 'disabled'
            ? 'bg-slate-100 text-slate-600 border-slate-200'
            : 'bg-slate-50 text-slate-600 border-slate-200';
  return <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-[11px] font-medium border ${tone}`}>{label}</span>;
};

export const SourceBadge: React.FC<{ source: string }> = ({ source }) => {
  const { t } = useI18n();
  const label = (t.setup.sourceLabels as Record<string, string>)[source] || source;
  return (
    <span className="inline-flex items-center px-2 py-0.5 rounded-md text-[11px] font-medium bg-indigo-50 text-indigo-700 border border-indigo-200">
      {label}
    </span>
  );
};

interface AiPanelProps {
  scope: 'installation' | 'user';
  statusData: SetupStatusResponse;
  onUpdated: (nextStatus: SetupStatusResponse) => void;
  onFeedback?: (msg: string, isError?: boolean) => void;
}

export const AiConfigPanel: React.FC<AiPanelProps> = ({ scope, statusData, onUpdated, onFeedback }) => {
  const { t } = useI18n();
  const isInst = scope === 'installation';
  const instAi = statusData.installation?.ai;
  const userAi = statusData.user_integrations?.ai;
  const initialProvider = (isInst ? instAi?.provider : userAi?.provider) || (isInst ? 'none' : 'inherit');
  const initialModel = (isInst ? instAi?.model : userAi?.model) || '';

  const [provider, setProvider] = useState<string>(initialProvider);
  const [model, setModel] = useState<string>(initialModel);
  const [apiKeyInput, setApiKeyInput] = useState('');
  const [allowShared, setAllowShared] = useState<boolean>(Boolean(statusData.installation?.allow_shared_ai_for_users));
  const [busySave, setBusySave] = useState(false);
  const [busyTest, setBusyTest] = useState(false);
  const [testResult, setTestResult] = useState<ValidationEntry | null>(
    (isInst ? instAi?.validation : userAi?.validation) || null
  );
  const [discoveredModels, setDiscoveredModels] = useState<string[]>([]);

  useEffect(() => {
    setProvider((isInst ? instAi?.provider : userAi?.provider) || (isInst ? 'none' : 'inherit'));
    setModel((isInst ? instAi?.model : userAi?.model) || '');
    setAllowShared(Boolean(statusData.installation?.allow_shared_ai_for_users));
    setTestResult((isInst ? instAi?.validation : userAi?.validation) || null);
  }, [isInst, instAi?.provider, instAi?.model, userAi?.provider, userAi?.model, statusData.installation?.allow_shared_ai_for_users]);

  const modelChoices: ModelOption[] =
    provider === 'openai'
      ? statusData.supported_models?.openai || []
      : provider === 'gemini'
        ? statusData.supported_models?.gemini || []
        : [];

  const mergedModels = [...modelChoices];
  for (const m of discoveredModels) {
    if (!mergedModels.some((item) => item.id === m)) {
      mergedModels.push({ id: m, label: `${m} (Discovered)` });
    }
  }

  const effectiveSource = isInst ? instAi?.effective_source || 'not_configured' : userAi?.effective_source || 'not_configured';
  const lockedByEnv = isInst ? Boolean(instAi?.locked_by_env) : Boolean(userAi?.locked_by_env);
  const hasStoredKey =
    provider === 'openai'
      ? Boolean(isInst ? instAi?.has_openai_key : userAi?.has_openai_key)
      : provider === 'gemini'
        ? Boolean(isInst ? instAi?.has_gemini_key : userAi?.has_gemini_key)
        : false;
  const maskedHint =
    provider === 'openai'
      ? (isInst ? instAi?.masked_openai_key : userAi?.masked_openai_key) || ''
      : provider === 'gemini'
        ? (isInst ? instAi?.masked_gemini_key : userAi?.masked_gemini_key) || ''
        : '';

  const saveConfig = async (overrideProvider?: string, removeKey = false) => {
    setBusySave(true);
    try {
      const targetProvider = overrideProvider ?? provider;
      let next: SetupStatusResponse;
      if (isInst) {
        next = await apiRequest<SetupStatusResponse>('/setup/installation/ai', {
          method: 'PUT',
          body: JSON.stringify({
            provider: targetProvider,
            model,
            api_key: apiKeyInput.trim() || null,
            remove_api_key: removeKey,
            allow_shared_ai_for_users: allowShared,
          }),
        });
      } else {
        next = await apiRequest<SetupStatusResponse>('/integrations/user', {
          method: 'PUT',
          body: JSON.stringify({
            ai_provider: targetProvider,
            ai_model: model,
            openai_api_key: targetProvider === 'openai' && apiKeyInput.trim() ? apiKeyInput.trim() : null,
            gemini_api_key: targetProvider === 'gemini' && apiKeyInput.trim() ? apiKeyInput.trim() : null,
            remove_openai_key: removeKey && targetProvider === 'openai',
            remove_gemini_key: removeKey && targetProvider === 'gemini',
          }),
        });
      }
      setApiKeyInput('');
      onUpdated(next);
      onFeedback?.(removeKey ? 'AI API key removed.' : 'AI configuration saved.');
    } catch (err) {
      onFeedback?.(err instanceof ApiError ? err.message : 'Failed to save AI configuration', true);
    } finally {
      setBusySave(false);
    }
  };

  const handleTest = async () => {
    if (provider !== 'openai' && provider !== 'gemini') return;
    setBusyTest(true);
    try {
      const res = await apiRequest<ValidationEntry>('/integrations/test', {
        method: 'POST',
        body: JSON.stringify({
          scope,
          integration: provider,
          model,
          api_key: apiKeyInput.trim() || null,
        }),
      });
      setTestResult(res);
      if (Array.isArray(res.details?.sample_models)) {
        setDiscoveredModels(res.details!.sample_models.slice(0, 8));
      }
      // Refresh full status snapshot if saved credentials were tested
      if (!apiKeyInput.trim()) {
        const refreshed = await apiRequest<SetupStatusResponse>('/setup/status');
        onUpdated(refreshed);
      }
    } catch (err) {
      setTestResult({
        ok: false,
        status: 'needs_attention',
        message: err instanceof ApiError ? err.message : 'Connection test failed',
      });
    } finally {
      setBusyTest(false);
    }
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <Sparkles className="h-4 w-4 text-indigo-600" aria-hidden="true" />
          <span className="text-sm font-semibold text-slate-900">
            {isInst ? 'Installation AI Provider' : 'Personal AI Override'}
          </span>
        </div>
        <div className="flex items-center gap-2">
          <SourceBadge source={effectiveSource} />
          {testResult?.status && <StatusBadge status={testResult.status} />}
        </div>
      </div>

      {lockedByEnv && (
        <div className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800">
          LOCK_ENV_CONFIG=true is enabled in host environment variables. Environment AI credentials take priority over database settings.
        </div>
      )}

      <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
        {!isInst && (
          <button
            type="button"
            onClick={() => setProvider('inherit')}
            className={`px-3 py-2 rounded-lg border text-xs font-medium text-start cursor-pointer ${
              provider === 'inherit' ? 'border-indigo-600 bg-indigo-50 text-indigo-800' : 'border-slate-200 bg-white text-slate-700'
            }`}
          >
            Inherit Installation Default
          </button>
        )}
        <button
          type="button"
          onClick={() => {
            setProvider('openai');
            if (!model || !model.startsWith('gpt')) {
              setModel(statusData.supported_models?.openai?.[0]?.id || 'gpt-4.1-mini');
            }
          }}
          className={`px-3 py-2 rounded-lg border text-xs font-medium text-start cursor-pointer ${
            provider === 'openai' ? 'border-indigo-600 bg-indigo-50 text-indigo-800' : 'border-slate-200 bg-white text-slate-700'
          }`}
        >
          OpenAI (GPT)
        </button>
        <button
          type="button"
          onClick={() => {
            setProvider('gemini');
            if (!model || model.startsWith('gpt')) {
              setModel(statusData.supported_models?.gemini?.[0]?.id || 'gemini-3.8-flash');
            }
          }}
          className={`px-3 py-2 rounded-lg border text-xs font-medium text-start cursor-pointer ${
            provider === 'gemini' ? 'border-indigo-600 bg-indigo-50 text-indigo-800' : 'border-slate-200 bg-white text-slate-700'
          }`}
        >
          Google Gemini
        </button>
        <button
          type="button"
          onClick={() => setProvider('none')}
          className={`px-3 py-2 rounded-lg border text-xs font-medium text-start cursor-pointer ${
            provider === 'none' ? 'border-slate-900 bg-slate-100 text-slate-900' : 'border-slate-200 bg-white text-slate-700'
          }`}
        >
          Disable / None
        </button>
      </div>

      {(provider === 'openai' || provider === 'gemini') && (
        <div className="rounded-xl border border-slate-200 bg-slate-50/70 p-4 space-y-3">
          <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-slate-600">
            <span>Used only for grounded CV parsing and truthful job application draft generation.</span>
            <a
              href={provider === 'openai' ? 'https://platform.openai.com/api-keys' : 'https://aistudio.google.com/apikey'}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1 font-medium text-indigo-600 hover:underline"
            >
              <span>{provider === 'openai' ? 'OpenAI API Keys Console' : 'Google AI Studio Keys'}</span>
              <ExternalLink className="h-3 w-3" aria-hidden="true" />
            </a>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            <div>
              <label className="block text-xs font-medium text-slate-700 mb-1">
                {provider === 'openai' ? 'OpenAI API Key' : 'Gemini API Key'}
              </label>
              <input
                type="password"
                autoComplete="off"
                value={apiKeyInput}
                onChange={(e) => setApiKeyInput(e.target.value)}
                placeholder={hasStoredKey ? `Saved (${maskedHint}) — leave blank to keep` : 'Paste API key...'}
                className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-xs"
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-slate-700 mb-1">Model</label>
              <select
                value={model || mergedModels[0]?.id || ''}
                onChange={(e) => setModel(e.target.value)}
                className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-xs"
              >
                {mergedModels.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.label}
                  </option>
                ))}
                {model && !mergedModels.some((m) => m.id === model) && (
                  <option value={model}>{model}</option>
                )}
              </select>
            </div>
          </div>

          {isInst && (
            <label className="flex items-center gap-2 text-xs text-slate-700 cursor-pointer">
              <input
                type="checkbox"
                checked={allowShared}
                onChange={(e) => setAllowShared(e.target.checked)}
                className="rounded border-slate-300"
              />
              <span>Allow standard workspace users without personal keys to use this installation AI key</span>
            </label>
          )}
        </div>
      )}

      {provider === 'none' && (
        <div className="rounded-lg border border-slate-200 bg-slate-50 p-3 text-xs text-slate-600">
          AI features disabled: grounded AI draft generation and automated CV matching remain paused until an OpenAI or Gemini key is configured.
        </div>
      )}

      {testResult && (
        <div
          className={`rounded-lg border p-3 text-xs flex items-start gap-2 ${
            testResult.status === 'verified'
              ? 'border-emerald-200 bg-emerald-50 text-emerald-800'
              : 'border-rose-200 bg-rose-50 text-rose-800'
          }`}
        >
          {testResult.status === 'verified' ? (
            <CheckCircle2 className="h-4 w-4 shrink-0 mt-0.5" aria-hidden="true" />
          ) : (
            <AlertTriangle className="h-4 w-4 shrink-0 mt-0.5" aria-hidden="true" />
          )}
          <span>{testResult.message}</span>
        </div>
      )}

      <div className="flex flex-wrap gap-2">
        {(provider === 'openai' || provider === 'gemini') && (
          <button
            type="button"
            disabled={busyTest}
            onClick={handleTest}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-slate-300 bg-white text-xs font-medium text-slate-800 hover:bg-slate-50 cursor-pointer disabled:opacity-50"
          >
            <KeyRound className="h-3.5 w-3.5" aria-hidden="true" />
            <span>{busyTest ? t.setup.testingBtn : t.setup.testBtn}</span>
          </button>
        )}
        <button
          type="button"
          disabled={busySave}
          onClick={() => saveConfig()}
          className="px-4 py-1.5 rounded-lg bg-indigo-600 hover:bg-indigo-700 text-white text-xs font-semibold cursor-pointer disabled:opacity-50"
        >
          {busySave ? t.controls.saving : t.setup.saveBtn}
        </button>
        {hasStoredKey && (provider === 'openai' || provider === 'gemini') && (
          <button
            type="button"
            disabled={busySave}
            onClick={() => saveConfig(isInst ? 'none' : 'inherit', true)}
            className="inline-flex items-center gap-1 px-3 py-1.5 rounded-lg border border-rose-200 text-rose-700 hover:bg-rose-50 text-xs font-medium cursor-pointer"
          >
            <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
            <span>{t.setup.disconnectBtn}</span>
          </button>
        )}
      </div>
    </div>
  );
};

interface SearchPanelProps {
  scope: 'installation' | 'user';
  statusData: SetupStatusResponse;
  onUpdated: (nextStatus: SetupStatusResponse) => void;
  onFeedback?: (msg: string, isError?: boolean) => void;
}

export const SearchConfigPanel: React.FC<SearchPanelProps> = ({ scope, statusData, onUpdated, onFeedback }) => {
  const { t } = useI18n();
  const isInst = scope === 'installation';
  const instS = statusData.installation?.search;
  const userS = statusData.user_integrations?.search;

  const [provider, setProvider] = useState<string>((isInst ? instS?.provider : userS?.provider) || (isInst ? 'none' : 'inherit'));
  const [tavilyInput, setTavilyInput] = useState('');
  const [braveInput, setBraveInput] = useState('');
  const [allowShared, setAllowShared] = useState<boolean>(Boolean(statusData.installation?.allow_shared_search_for_users));
  const [busySave, setBusySave] = useState(false);
  const [busyTest, setBusyTest] = useState<string>('');
  const [testResult, setTestResult] = useState<ValidationEntry | null>(
    (isInst ? instS?.validation : userS?.validation) || null
  );

  useEffect(() => {
    setProvider((isInst ? instS?.provider : userS?.provider) || (isInst ? 'none' : 'inherit'));
    setAllowShared(Boolean(statusData.installation?.allow_shared_search_for_users));
    setTestResult((isInst ? instS?.validation : userS?.validation) || null);
  }, [isInst, instS?.provider, userS?.provider, statusData.installation?.allow_shared_search_for_users]);

  const effectiveSource = isInst ? instS?.effective_source || 'not_configured' : userS?.effective_source || 'not_configured';
  const hasTavily = Boolean(isInst ? instS?.has_tavily_key : userS?.has_tavily_key);
  const hasBrave = Boolean(isInst ? instS?.has_brave_key : userS?.has_brave_key);
  const maskedTavily = (isInst ? instS?.masked_tavily_key : userS?.masked_tavily_key) || '';
  const maskedBrave = (isInst ? instS?.masked_brave_key : userS?.masked_brave_key) || '';

  const saveSearch = async (removeTavily = false, removeBrave = false, overrideProv?: string) => {
    setBusySave(true);
    try {
      const targetProv = overrideProv ?? provider;
      let next: SetupStatusResponse;
      if (isInst) {
        next = await apiRequest<SetupStatusResponse>('/setup/installation/search', {
          method: 'PUT',
          body: JSON.stringify({
            provider: targetProv,
            tavily_api_key: tavilyInput.trim() || null,
            brave_api_key: braveInput.trim() || null,
            remove_tavily_key: removeTavily,
            remove_brave_key: removeBrave,
            allow_shared_search_for_users: allowShared,
          }),
        });
      } else {
        next = await apiRequest<SetupStatusResponse>('/integrations/user', {
          method: 'PUT',
          body: JSON.stringify({
            search_provider: targetProv,
            tavily_api_key: tavilyInput.trim() || null,
            brave_api_key: braveInput.trim() || null,
            remove_tavily_key: removeTavily,
            remove_brave_key: removeBrave,
          }),
        });
      }
      setTavilyInput('');
      setBraveInput('');
      onUpdated(next);
      onFeedback?.('Search provider settings updated.');
    } catch (err) {
      onFeedback?.(err instanceof ApiError ? err.message : 'Failed to save search settings', true);
    } finally {
      setBusySave(false);
    }
  };

  const runTest = async (which: 'tavily' | 'brave') => {
    setBusyTest(which);
    try {
      const key = which === 'tavily' ? tavilyInput.trim() || null : braveInput.trim() || null;
      const res = await apiRequest<ValidationEntry>('/integrations/test', {
        method: 'POST',
        body: JSON.stringify({ scope, integration: which, api_key: key }),
      });
      setTestResult(res);
      if (!key) {
        onUpdated(await apiRequest<SetupStatusResponse>('/setup/status'));
      }
    } catch (err) {
      setTestResult({ ok: false, status: 'needs_attention', message: err instanceof ApiError ? err.message : 'Test failed' });
    } finally {
      setBusyTest('');
    }
  };

  const needsTavily = provider === 'tavily' || provider === 'both';
  const needsBrave = provider === 'brave' || provider === 'both';

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <Search className="h-4 w-4 text-indigo-600" aria-hidden="true" />
          <span className="text-sm font-semibold text-slate-900">
            {isInst ? 'Installation Job-Search Provider' : 'Personal Search Override'}
          </span>
        </div>
        <div className="flex items-center gap-2">
          <SourceBadge source={effectiveSource} />
          {testResult?.status && <StatusBadge status={testResult.status} />}
        </div>
      </div>

      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
        {!isInst && (
          <button
            type="button"
            onClick={() => setProvider('inherit')}
            className={`px-3 py-2 rounded-lg border text-xs font-medium cursor-pointer ${
              provider === 'inherit' ? 'border-indigo-600 bg-indigo-50 text-indigo-800' : 'border-slate-200 bg-white'
            }`}
          >
            Inherit Default
          </button>
        )}
        <button
          type="button"
          onClick={() => setProvider('tavily')}
          className={`px-3 py-2 rounded-lg border text-xs font-medium cursor-pointer ${
            provider === 'tavily' ? 'border-indigo-600 bg-indigo-50 text-indigo-800' : 'border-slate-200 bg-white'
          }`}
        >
          Tavily
        </button>
        <button
          type="button"
          onClick={() => setProvider('brave')}
          className={`px-3 py-2 rounded-lg border text-xs font-medium cursor-pointer ${
            provider === 'brave' ? 'border-indigo-600 bg-indigo-50 text-indigo-800' : 'border-slate-200 bg-white'
          }`}
        >
          Brave Search
        </button>
        <button
          type="button"
          onClick={() => setProvider('both')}
          className={`px-3 py-2 rounded-lg border text-xs font-medium cursor-pointer ${
            provider === 'both' ? 'border-indigo-600 bg-indigo-50 text-indigo-800' : 'border-slate-200 bg-white'
          }`}
        >
          Both
        </button>
        <button
          type="button"
          onClick={() => setProvider('none')}
          className={`px-3 py-2 rounded-lg border text-xs font-medium cursor-pointer ${
            provider === 'none' ? 'border-slate-900 bg-slate-100 text-slate-900' : 'border-slate-200 bg-white'
          }`}
        >
          None / Manual Only
        </button>
      </div>

      {needsTavily && (
        <div className="rounded-xl border border-slate-200 bg-slate-50/70 p-4 space-y-2">
          <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
            <label className="font-semibold text-slate-800">Tavily API Key (non-billable /usage check)</label>
            <a href="https://app.tavily.com/home" target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-indigo-600 hover:underline">
              <span>Tavily Dashboard</span>
              <ExternalLink className="h-3 w-3" aria-hidden="true" />
            </a>
          </div>
          <div className="flex flex-wrap gap-2">
            <input
              type="password"
              autoComplete="off"
              value={tavilyInput}
              onChange={(e) => setTavilyInput(e.target.value)}
              placeholder={hasTavily ? `Saved (${maskedTavily}) — leave blank to keep` : 'tvly-...'}
              className="flex-1 min-w-[200px] rounded-lg border border-slate-300 bg-white px-3 py-2 text-xs"
            />
            <button
              type="button"
              disabled={busyTest === 'tavily'}
              onClick={() => runTest('tavily')}
              className="px-3 py-1.5 rounded-lg border border-slate-300 bg-white text-xs font-medium hover:bg-slate-50 cursor-pointer"
            >
              {busyTest === 'tavily' ? t.setup.testingBtn : t.setup.testBtn}
            </button>
            {hasTavily && (
              <button
                type="button"
                onClick={() => saveSearch(true, false)}
                className="px-3 py-1.5 rounded-lg border border-rose-200 text-rose-700 hover:bg-rose-50 text-xs cursor-pointer"
              >
                Remove
              </button>
            )}
          </div>
        </div>
      )}

      {needsBrave && (
        <div className="rounded-xl border border-slate-200 bg-slate-50/70 p-4 space-y-2">
          <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
            <label className="font-semibold text-slate-800">Brave Search API Key (uses 1 test query)</label>
            <a href="https://api.search.brave.com/app/dashboard" target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-indigo-600 hover:underline">
              <span>Brave Search API</span>
              <ExternalLink className="h-3 w-3" aria-hidden="true" />
            </a>
          </div>
          <div className="flex flex-wrap gap-2">
            <input
              type="password"
              autoComplete="off"
              value={braveInput}
              onChange={(e) => setBraveInput(e.target.value)}
              placeholder={hasBrave ? `Saved (${maskedBrave}) — leave blank to keep` : 'BSA...'}
              className="flex-1 min-w-[200px] rounded-lg border border-slate-300 bg-white px-3 py-2 text-xs"
            />
            <button
              type="button"
              disabled={busyTest === 'brave'}
              onClick={() => runTest('brave')}
              className="px-3 py-1.5 rounded-lg border border-slate-300 bg-white text-xs font-medium hover:bg-slate-50 cursor-pointer"
            >
              {busyTest === 'brave' ? t.setup.testingBtn : t.setup.testBtn}
            </button>
            {hasBrave && (
              <button
                type="button"
                onClick={() => saveSearch(false, true)}
                className="px-3 py-1.5 rounded-lg border border-rose-200 text-rose-700 hover:bg-rose-50 text-xs cursor-pointer"
              >
                Remove
              </button>
            )}
          </div>
        </div>
      )}

      {provider === 'none' && (
        <div className="rounded-lg border border-slate-200 bg-slate-50 p-3 text-xs text-slate-600">
          Automated web discovery is paused. You can still paste public job URLs manually and import historical CSV/XLSX applications.
        </div>
      )}

      {isInst && (needsTavily || needsBrave) && (
        <label className="flex items-center gap-2 text-xs text-slate-700 cursor-pointer">
          <input
            type="checkbox"
            checked={allowShared}
            onChange={(e) => setAllowShared(e.target.checked)}
            className="rounded border-slate-300"
          />
          <span>Allow standard workspace users without personal keys to use this shared search configuration</span>
        </label>
      )}

      {testResult && (
        <div
          className={`rounded-lg border p-3 text-xs flex items-start gap-2 ${
            testResult.status === 'verified'
              ? 'border-emerald-200 bg-emerald-50 text-emerald-800'
              : 'border-rose-200 bg-rose-50 text-rose-800'
          }`}
        >
          {testResult.status === 'verified' ? <CheckCircle2 className="h-4 w-4 shrink-0 mt-0.5" /> : <AlertTriangle className="h-4 w-4 shrink-0 mt-0.5" />}
          <span>{testResult.message}</span>
        </div>
      )}

      <button
        type="button"
        disabled={busySave}
        onClick={() => saveSearch()}
        className="px-4 py-1.5 rounded-lg bg-indigo-600 hover:bg-indigo-700 text-white text-xs font-semibold cursor-pointer disabled:opacity-50"
      >
        {busySave ? t.controls.saving : t.setup.saveBtn}
      </button>
    </div>
  );
};

interface OwnerEmailProps {
  statusData: SetupStatusResponse;
  onUpdated: (nextStatus: SetupStatusResponse) => void;
  onFeedback?: (msg: string, isError?: boolean) => void;
}

export const OAuthAppsPanel: React.FC<OwnerEmailProps> = ({ statusData, onUpdated, onFeedback }) => {
  const { t } = useI18n();
  const g = statusData.installation?.google_oauth;
  const m = statusData.installation?.ms_graph;

  const [gClientId, setGClientId] = useState(g?.client_id || '');
  const [gSecret, setGSecret] = useState('');
  const [mClientId, setMClientId] = useState(m?.client_id || '');
  const [mTenant, setMTenant] = useState(m?.tenant_id || 'common');
  const [mSecret, setMSecret] = useState('');
  const [busy, setBusy] = useState('');
  const [gTest, setGTest] = useState<ValidationEntry | null>(g?.validation || null);
  const [mTest, setMTest] = useState<ValidationEntry | null>(m?.validation || null);

  useEffect(() => {
    setGClientId(g?.client_id || '');
    setMClientId(m?.client_id || '');
    setMTenant(m?.tenant_id || 'common');
    setGTest(g?.validation || null);
    setMTest(m?.validation || null);
  }, [g?.client_id, m?.client_id, m?.tenant_id]);

  const copyUri = (uri: string) => {
    navigator.clipboard?.writeText(uri);
    onFeedback?.(`Copied callback URL: ${uri}`);
  };

  return (
    <div className="space-y-5">
      {/* Google OAuth App */}
      <div className="rounded-xl border border-slate-200 p-4 space-y-3 bg-white">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <h4 className="text-sm font-semibold text-slate-900">Google OAuth Client (Sign-In, Gmail, Sheets)</h4>
            <p className="text-xs text-slate-500">
              Register the exact redirect URIs below in Google Cloud Console &gt; APIs &amp; Services &gt; Credentials. Operator approval/verification in Google Cloud still requires manual console setup.
            </p>
          </div>
          {gTest?.status && <StatusBadge status={gTest.status} />}
        </div>

        <div className="rounded-lg bg-slate-50 border border-slate-200 p-3 space-y-1.5 text-[11px] font-mono text-slate-700">
          {[
            { label: 'App Sign-In (openid)', uri: g?.redirect_uri_auth || '' },
            { label: 'Gmail OAuth (gmail.*)', uri: g?.redirect_uri_gmail || '' },
            { label: 'Google Sheets (drive.file)', uri: g?.redirect_uri_sheets || '' },
          ].map((item) => (
            <div key={item.label} className="flex flex-wrap items-center justify-between gap-2">
              <span className="font-sans text-slate-500">{item.label}:</span>
              <div className="flex items-center gap-1.5">
                <code className="bg-white px-2 py-0.5 rounded border border-slate-200 break-all">{item.uri}</code>
                <button
                  type="button"
                  onClick={() => copyUri(item.uri)}
                  aria-label={`Copy ${item.label}`}
                  className="p-1 rounded border border-slate-200 bg-white hover:bg-slate-100 cursor-pointer"
                >
                  <Copy className="h-3 w-3" />
                </button>
              </div>
            </div>
          ))}
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          <input
            type="text"
            value={gClientId}
            onChange={(e) => setGClientId(e.target.value)}
            placeholder="Google Client ID (*.apps.googleusercontent.com)"
            className="rounded-lg border border-slate-300 px-3 py-2 text-xs"
          />
          <input
            type="password"
            autoComplete="off"
            value={gSecret}
            onChange={(e) => setGSecret(e.target.value)}
            placeholder={g?.has_client_secret ? `Saved (${g.masked_client_secret}) — blank keeps current` : 'Google Client Secret'}
            className="rounded-lg border border-slate-300 px-3 py-2 text-xs"
          />
        </div>

        {gTest && <div className="text-xs text-slate-700">{gTest.message}</div>}

        <div className="flex flex-wrap gap-2">
          <a
            href="https://console.cloud.google.com/apis/credentials"
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1 px-3 py-1.5 rounded-lg border border-slate-200 text-xs text-indigo-600 hover:bg-slate-50"
          >
            <span>Google Cloud Console</span>
            <ExternalLink className="h-3 w-3" />
          </a>
          <button
            type="button"
            disabled={busy === 'gtest'}
            onClick={async () => {
              setBusy('gtest');
              try {
                const res = await apiRequest<ValidationEntry>('/integrations/test', {
                  method: 'POST',
                  body: JSON.stringify({ scope: 'installation', integration: 'google_oauth', client_id: gClientId, client_secret: gSecret.trim() || null }),
                });
                setGTest(res);
              } finally {
                setBusy('');
              }
            }}
            className="px-3 py-1.5 rounded-lg border border-slate-300 text-xs font-medium cursor-pointer"
          >
            {busy === 'gtest' ? t.setup.testingBtn : t.setup.testBtn}
          </button>
          <button
            type="button"
            disabled={busy === 'gsave'}
            onClick={async () => {
              setBusy('gsave');
              try {
                const next = await apiRequest<SetupStatusResponse>('/setup/installation/google-oauth', {
                  method: 'PUT',
                  body: JSON.stringify({ client_id: gClientId, client_secret: gSecret.trim() || null }),
                });
                setGSecret('');
                onUpdated(next);
                onFeedback?.('Google OAuth client saved.');
              } catch (err) {
                onFeedback?.(err instanceof ApiError ? err.message : 'Save failed', true);
              } finally {
                setBusy('');
              }
            }}
            className="px-4 py-1.5 rounded-lg bg-indigo-600 text-white text-xs font-semibold cursor-pointer"
          >
            {t.setup.saveBtn}
          </button>
          {g?.client_id && (
            <button
              type="button"
              onClick={async () => {
                const next = await apiRequest<SetupStatusResponse>('/setup/installation/google-oauth', {
                  method: 'PUT',
                  body: JSON.stringify({ client_id: '', disconnect: true }),
                });
                onUpdated(next);
                onFeedback?.('Google OAuth disconnected.');
              }}
              className="px-3 py-1.5 rounded-lg border border-rose-200 text-rose-700 text-xs cursor-pointer"
            >
              {t.setup.disconnectBtn}
            </button>
          )}
        </div>
      </div>

      {/* Microsoft Graph App */}
      <div className="rounded-xl border border-slate-200 p-4 space-y-3 bg-white">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <h4 className="text-sm font-semibold text-slate-900">Microsoft Entra / Graph OAuth (Outlook &amp; M365)</h4>
            <p className="text-xs text-slate-500">
              Add the redirect URI in Microsoft Entra App Registrations with Mail.Send, Mail.Read, offline_access, and User.Read scopes.
            </p>
          </div>
          {mTest?.status && <StatusBadge status={mTest.status} />}
        </div>

        <div className="rounded-lg bg-slate-50 border border-slate-200 p-3 flex flex-wrap items-center justify-between gap-2 text-[11px] font-mono text-slate-700">
          <span className="font-sans text-slate-500">Redirect URI:</span>
          <div className="flex items-center gap-1.5">
            <code className="bg-white px-2 py-0.5 rounded border border-slate-200 break-all">{m?.redirect_uri || ''}</code>
            <button
              type="button"
              onClick={() => copyUri(m?.redirect_uri || '')}
              aria-label="Copy Microsoft redirect URI"
              className="p-1 rounded border border-slate-200 bg-white hover:bg-slate-100 cursor-pointer"
            >
              <Copy className="h-3 w-3" />
            </button>
          </div>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
          <input
            type="text"
            value={mClientId}
            onChange={(e) => setMClientId(e.target.value)}
            placeholder="Application (Client) ID"
            className="rounded-lg border border-slate-300 px-3 py-2 text-xs"
          />
          <input
            type="text"
            value={mTenant}
            onChange={(e) => setMTenant(e.target.value)}
            placeholder="Tenant ID (default: common)"
            className="rounded-lg border border-slate-300 px-3 py-2 text-xs"
          />
          <input
            type="password"
            autoComplete="off"
            value={mSecret}
            onChange={(e) => setMSecret(e.target.value)}
            placeholder={m?.has_client_secret ? `Saved (${m.masked_client_secret}) — blank keeps` : 'Client Secret'}
            className="rounded-lg border border-slate-300 px-3 py-2 text-xs"
          />
        </div>

        {mTest && <div className="text-xs text-slate-700">{mTest.message}</div>}

        <div className="flex flex-wrap gap-2">
          <a
            href="https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade"
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1 px-3 py-1.5 rounded-lg border border-slate-200 text-xs text-indigo-600 hover:bg-slate-50"
          >
            <span>Microsoft Entra Portal</span>
            <ExternalLink className="h-3 w-3" />
          </a>
          <button
            type="button"
            disabled={busy === 'mtest'}
            onClick={async () => {
              setBusy('mtest');
              try {
                const res = await apiRequest<ValidationEntry>('/integrations/test', {
                  method: 'POST',
                  body: JSON.stringify({ scope: 'installation', integration: 'ms_graph', client_id: mClientId, tenant_id: mTenant, client_secret: mSecret.trim() || null }),
                });
                setMTest(res);
              } finally {
                setBusy('');
              }
            }}
            className="px-3 py-1.5 rounded-lg border border-slate-300 text-xs font-medium cursor-pointer"
          >
            {busy === 'mtest' ? t.setup.testingBtn : t.setup.testBtn}
          </button>
          <button
            type="button"
            disabled={busy === 'msave'}
            onClick={async () => {
              setBusy('msave');
              try {
                const next = await apiRequest<SetupStatusResponse>('/setup/installation/ms-graph', {
                  method: 'PUT',
                  body: JSON.stringify({ client_id: mClientId, tenant_id: mTenant, client_secret: mSecret.trim() || null }),
                });
                setMSecret('');
                onUpdated(next);
                onFeedback?.('Microsoft Graph OAuth client saved.');
              } catch (err) {
                onFeedback?.(err instanceof ApiError ? err.message : 'Save failed', true);
              } finally {
                setBusy('');
              }
            }}
            className="px-4 py-1.5 rounded-lg bg-indigo-600 text-white text-xs font-semibold cursor-pointer"
          >
            {t.setup.saveBtn}
          </button>
        </div>
      </div>
    </div>
  );
};

export const TransactionalEmailPanel: React.FC<OwnerEmailProps> = ({ statusData, onUpdated, onFeedback }) => {
  const { t } = useI18n();
  const tx = statusData.installation?.transactional_email;
  const [provider, setProvider] = useState<'console' | 'smtp'>(tx?.provider || 'console');
  const [fromEmail, setFromEmail] = useState(tx?.from_email || '');
  const [host, setHost] = useState(tx?.smtp_host || '');
  const [port, setPort] = useState(tx?.smtp_port || 587);
  const [username, setUsername] = useState(tx?.smtp_username || '');
  const [password, setPassword] = useState('');
  const [useTls, setUseTls] = useState(tx?.smtp_use_tls ?? true);
  const [busy, setBusy] = useState('');
  const [testResult, setTestResult] = useState<ValidationEntry | null>(tx?.validation || null);

  useEffect(() => {
    if (!tx) return;
    setProvider(tx.provider);
    setFromEmail(tx.from_email);
    setHost(tx.smtp_host);
    setPort(tx.smtp_port);
    setUsername(tx.smtp_username);
    setUseTls(tx.smtp_use_tls);
    setTestResult(tx.validation || null);
  }, [tx?.provider, tx?.smtp_host, tx?.from_email]);

  return (
    <div className="rounded-xl border border-slate-200 p-4 space-y-3 bg-white">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <MailCheck className="h-4 w-4 text-indigo-600" aria-hidden="true" />
          <div>
            <h4 className="text-sm font-semibold text-slate-900">Transactional System Email (Verification &amp; Password Reset)</h4>
            <p className="text-xs text-slate-500">Separate from personal job-application mailboxes. Required for external multi-user signup verification.</p>
          </div>
        </div>
        {testResult?.status && <StatusBadge status={testResult.status} />}
      </div>

      <div className="flex gap-2">
        <button
          type="button"
          onClick={() => setProvider('console')}
          className={`px-3 py-1.5 rounded-lg border text-xs font-medium cursor-pointer ${
            provider === 'console' ? 'border-indigo-600 bg-indigo-50 text-indigo-800' : 'border-slate-200'
          }`}
        >
          Console Log (Local Dev / Single Owner)
        </button>
        <button
          type="button"
          onClick={() => setProvider('smtp')}
          className={`px-3 py-1.5 rounded-lg border text-xs font-medium cursor-pointer ${
            provider === 'smtp' ? 'border-indigo-600 bg-indigo-50 text-indigo-800' : 'border-slate-200'
          }`}
        >
          SMTP Relay (Multi-User Hosted)
        </button>
      </div>

      {provider === 'smtp' && (
        <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
          <input
            type="email"
            value={fromEmail}
            onChange={(e) => setFromEmail(e.target.value)}
            placeholder="From Email (no-reply@yourdomain.com)"
            className="rounded-lg border border-slate-300 px-3 py-2 text-xs"
          />
          <input
            type="text"
            value={host}
            onChange={(e) => setHost(e.target.value)}
            placeholder="SMTP Host (e.g. smtp.mailgun.org)"
            className="rounded-lg border border-slate-300 px-3 py-2 text-xs"
          />
          <input
            type="number"
            value={port}
            onChange={(e) => setPort(Number(e.target.value))}
            placeholder="Port (587 or 465)"
            className="rounded-lg border border-slate-300 px-3 py-2 text-xs"
          />
          <input
            type="text"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            placeholder="SMTP Username"
            className="rounded-lg border border-slate-300 px-3 py-2 text-xs"
          />
          <input
            type="password"
            autoComplete="off"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder={tx?.has_smtp_password ? `Saved (${tx.masked_smtp_password}) — blank keeps` : 'SMTP Password'}
            className="rounded-lg border border-slate-300 px-3 py-2 text-xs"
          />
          <label className="flex items-center gap-2 text-xs text-slate-700 px-2">
            <input type="checkbox" checked={useTls} onChange={(e) => setUseTls(e.target.checked)} />
            <span>Require STARTTLS / TLS</span>
          </label>
        </div>
      )}

      {testResult && <div className="text-xs text-slate-700">{testResult.message}</div>}

      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          disabled={busy === 'test'}
          onClick={async () => {
            setBusy('test');
            try {
              const res = await apiRequest<ValidationEntry>('/integrations/test', {
                method: 'POST',
                body: JSON.stringify({
                  scope: 'installation',
                  integration: 'transactional_email',
                  email_provider: provider,
                  from_email: fromEmail,
                  smtp_host: host,
                  smtp_port: port,
                  smtp_username: username,
                  smtp_password: password.trim() || null,
                  smtp_use_tls: useTls,
                }),
              });
              setTestResult(res);
            } finally {
              setBusy('');
            }
          }}
          className="px-3 py-1.5 rounded-lg border border-slate-300 text-xs font-medium cursor-pointer"
        >
          {busy === 'test' ? t.setup.testingBtn : t.setup.testBtn}
        </button>
        <button
          type="button"
          disabled={busy === 'save'}
          onClick={async () => {
            setBusy('save');
            try {
              const next = await apiRequest<SetupStatusResponse>('/setup/installation/transactional-email', {
                method: 'PUT',
                body: JSON.stringify({
                  provider,
                  from_email: fromEmail,
                  smtp_host: host,
                  smtp_port: port,
                  smtp_username: username,
                  smtp_password: password.trim() || null,
                  smtp_use_tls: useTls,
                }),
              });
              setPassword('');
              onUpdated(next);
              onFeedback?.('Transactional email configuration saved.');
            } catch (err) {
              onFeedback?.(err instanceof ApiError ? err.message : 'Save failed', true);
            } finally {
              setBusy('');
            }
          }}
          className="px-4 py-1.5 rounded-lg bg-indigo-600 text-white text-xs font-semibold cursor-pointer"
        >
          {t.setup.saveBtn}
        </button>
        <button
          type="button"
          disabled={busy === 'send_self'}
          onClick={async () => {
            setBusy('send_self');
            try {
              const res = await apiRequest<ValidationEntry>('/integrations/send-test-email', { method: 'POST' });
              setTestResult(res);
              onFeedback?.(res.message);
            } catch (err) {
              onFeedback?.(err instanceof ApiError ? err.message : 'Failed to send test email', true);
            } finally {
              setBusy('');
            }
          }}
          className="px-3 py-1.5 rounded-lg border border-indigo-200 text-indigo-700 hover:bg-indigo-50 text-xs font-medium cursor-pointer"
        >
          {t.setup.sendTestEmailBtn}
        </button>
      </div>
    </div>
  );
};
