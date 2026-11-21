import React, { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { Briefcase, Globe } from 'lucide-react';
import { useI18n } from '../i18n';
import { apiRequest, ApiError, setCsrfToken } from '../lib/api';
import type { UserSummary } from '../components/AppShell';

interface AuthPageProps {
  onAuthenticated: (user: UserSummary) => void;
  googleSignInEnabled?: boolean;
  requiresClaim?: boolean;
  onGoToClaim?: () => void;
}

export const AuthPage: React.FC<AuthPageProps> = ({
  onAuthenticated,
  googleSignInEnabled = false,
  requiresClaim = false,
  onGoToClaim,
}) => {
  const { t, toggleLocale } = useI18n();
  const [searchParams, setSearchParams] = useSearchParams();
  const [mode, setMode] = useState<'login' | 'register' | 'forgot' | 'reset'>('login');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [fullName, setFullName] = useState('');
  const [timezone, setTimezone] = useState(() => Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC');
  const [resetToken, setResetToken] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const authErr = searchParams.get('auth_error');
    if (authErr) {
      setError(authErr);
      const next = new URLSearchParams(searchParams);
      next.delete('auth_error');
      next.delete('auth_error_code');
      setSearchParams(next, { replace: true });
    }
  }, [searchParams, setSearchParams]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    setNotice('');
    setBusy(true);
    try {
      if (mode === 'login') {
        const res = await apiRequest<{ user: UserSummary }>('/auth/login', {
          method: 'POST',
          body: JSON.stringify({ email, password }),
        });
        setCsrfToken(res.user.csrf_token);
        onAuthenticated(res.user);
      } else if (mode === 'register') {
        const res = await apiRequest<{ user: UserSummary }>('/auth/register', {
          method: 'POST',
          body: JSON.stringify({ email, password, full_name: fullName, timezone, ui_locale: document.documentElement.lang === 'ar' ? 'ar' : 'en' }),
        });
        setCsrfToken(res.user.csrf_token);
        onAuthenticated(res.user);
      } else if (mode === 'forgot') {
        const res = await apiRequest<{ message: string }>('/auth/forgot-password', {
          method: 'POST',
          body: JSON.stringify({ email }),
        });
        setNotice(res.message);
        setMode('reset');
      } else {
        const res = await apiRequest<{ message: string }>('/auth/reset-password', {
          method: 'POST',
          body: JSON.stringify({ token: resetToken, new_password: password }),
        });
        setNotice(res.message);
        setMode('login');
      }
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Unexpected error');
    } finally {
      setBusy(false);
    }
  };

  const handleGoogleSignIn = async (connectGmail = true) => {
    setError('');
    setBusy(true);
    try {
      const res = await apiRequest<{ authorization_url: string }>(
        `/auth/google/start?connect_gmail=${connectGmail ? 'true' : 'false'}`,
      );
      window.location.href = res.authorization_url;
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Google Sign-In unavailable');
      setBusy(false);
    }
  };

  return (
    <div className="min-h-screen bg-slate-950 flex flex-col justify-center items-center px-4 py-10">
      <div className="w-full max-w-md bg-white rounded-2xl shadow-xl border border-slate-200 p-6 sm:p-8 space-y-6">
        <div className="flex items-start justify-between gap-3">
          <div className="flex items-center gap-3">
            <div className="h-10 w-10 rounded-xl bg-indigo-600 flex items-center justify-center shrink-0">
              <Briefcase className="h-5 w-5 text-white" aria-hidden="true" />
            </div>
            <div>
              <h1 className="text-lg font-bold text-slate-900">{t.appName}</h1>
              <p className="text-xs text-slate-500">{t.tagline}</p>
            </div>
          </div>
          <button
            type="button"
            onClick={toggleLocale}
            className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg border border-slate-200 text-xs font-medium text-slate-700 hover:bg-slate-50 cursor-pointer"
          >
            <Globe className="h-3.5 w-3.5" aria-hidden="true" />
            <span>{t.controls.languageToggle}</span>
          </button>
        </div>

        <h2 className="text-base font-semibold text-slate-800">
          {mode === 'login' && t.auth.signInTitle}
          {mode === 'register' && t.auth.registerTitle}
          {(mode === 'forgot' || mode === 'reset') && t.auth.resetPasswordTitle}
        </h2>

        {error && (
          <div role="alert" data-testid="auth-error-banner" className="p-3 rounded-lg bg-rose-50 border border-rose-200 text-rose-800 text-sm">
            {error}
          </div>
        )}
        {notice && (
          <div role="status" className="p-3 rounded-lg bg-emerald-50 border border-emerald-200 text-emerald-800 text-sm">
            {notice}
          </div>
        )}

        {(mode === 'login' || mode === 'register') && googleSignInEnabled && (
          <div className="space-y-2.5">
            <button
              type="button"
              disabled={busy}
              data-testid="continue-with-google-btn"
              onClick={() => handleGoogleSignIn(true)}
              className="w-full py-2.5 px-4 rounded-xl border border-slate-300 bg-white hover:bg-slate-50 text-slate-900 text-sm font-semibold shadow-xs flex items-center justify-center gap-2.5 cursor-pointer disabled:opacity-50"
            >
              <svg className="h-4 w-4 shrink-0" viewBox="0 0 24 24" aria-hidden="true">
                <path fill="#4285F4" d="M23.49 12.27c0-.79-.07-1.54-.2-2.27H12v4.3h6.44a5.5 5.5 0 0 1-2.39 3.61v3h3.87c2.26-2.08 3.57-5.15 3.57-8.64z" />
                <path fill="#34A853" d="M12 24c3.24 0 5.95-1.07 7.93-2.9l-3.87-3c-1.07.72-2.45 1.15-4.06 1.15-3.12 0-5.77-2.11-6.71-4.95H1.29v3.1A12 12 0 0 0 12 24z" />
                <path fill="#FBBC05" d="M5.29 14.3a7.21 7.21 0 0 1 0-4.6V6.6H1.29a12 12 0 0 0 0 10.8l4-3.1z" />
                <path fill="#EA4335" d="M12 4.75c1.76 0 3.34.61 4.58 1.8l3.44-3.44C17.95 1.19 15.24 0 12 0A12 12 0 0 0 1.29 6.6l4 3.1c.94-2.84 3.59-4.95 6.71-4.95z" />
              </svg>
              <span>Continue with Google</span>
            </button>
            <p className="text-[11px] text-slate-500 text-center leading-relaxed">
              Sign in and connect Gmail in one step to send approved job applications — no SMTP or App Password needed.
              Optional Sent-history deduplication is requested separately.
            </p>
            <div className="text-center">
              <button
                type="button"
                disabled={busy}
                data-testid="google-signin-only-btn"
                onClick={() => handleGoogleSignIn(false)}
                className="text-[11px] text-indigo-600 hover:underline cursor-pointer disabled:opacity-50"
              >
                Sign in with Google without connecting Gmail yet
              </button>
            </div>
            <div className="relative flex items-center py-1">
              <div className="flex-grow border-t border-slate-200" />
              <span className="mx-3 text-[11px] font-medium text-slate-400 uppercase">or use email</span>
              <div className="flex-grow border-t border-slate-200" />
            </div>
          </div>
        )}

        <form onSubmit={handleSubmit} className="space-y-4">
          {mode === 'register' && (
            <>
              <div>
                <label className="block text-xs font-semibold text-slate-700 mb-1" htmlFor="auth-fullname">
                  {t.auth.fullName}
                </label>
                <input
                  id="auth-fullname"
                  type="text"
                  required
                  value={fullName}
                  onChange={(e) => setFullName(e.target.value)}
                  className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm focus:border-indigo-600 focus:outline-none"
                />
              </div>
              <div>
                <label className="block text-xs font-semibold text-slate-700 mb-1" htmlFor="auth-tz">
                  {t.auth.timezone}
                </label>
                <input
                  id="auth-tz"
                  type="text"
                  required
                  value={timezone}
                  onChange={(e) => setTimezone(e.target.value)}
                  className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm focus:border-indigo-600 focus:outline-none"
                />
              </div>
            </>
          )}

          {mode !== 'reset' && (
            <div>
              <label className="block text-xs font-semibold text-slate-700 mb-1" htmlFor="auth-email">
                {t.auth.email}
              </label>
              <input
                id="auth-email"
                type="email"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm focus:border-indigo-600 focus:outline-none"
              />
            </div>
          )}

          {mode === 'reset' && (
            <div>
              <label className="block text-xs font-semibold text-slate-700 mb-1" htmlFor="auth-reset-token">
                {t.auth.resetToken}
              </label>
              <input
                id="auth-reset-token"
                type="text"
                required
                value={resetToken}
                onChange={(e) => setResetToken(e.target.value)}
                className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm focus:border-indigo-600 focus:outline-none"
              />
            </div>
          )}

          {mode !== 'forgot' && (
            <div>
              <label className="block text-xs font-semibold text-slate-700 mb-1" htmlFor="auth-password">
                {mode === 'reset' ? t.auth.newPassword : t.auth.password}
              </label>
              <input
                id="auth-password"
                type="password"
                required
                minLength={mode === 'login' ? 1 : 10}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm focus:border-indigo-600 focus:outline-none"
              />
            </div>
          )}

          <button
            type="submit"
            disabled={busy}
            className="w-full py-2.5 px-4 rounded-lg bg-indigo-600 hover:bg-indigo-700 text-white text-sm font-semibold transition-colors cursor-pointer disabled:opacity-50"
          >
            {busy
              ? t.controls.loading
              : mode === 'login'
                ? t.auth.signInBtn
                : mode === 'register'
                  ? t.auth.registerBtn
                  : mode === 'forgot'
                    ? t.auth.sendResetBtn
                    : t.auth.applyResetBtn}
          </button>
        </form>

        {requiresClaim && onGoToClaim && (
          <div className="pt-2 border-t border-slate-100">
            <button
              type="button"
              onClick={onGoToClaim}
              className="w-full py-2 px-4 rounded-lg bg-slate-100 hover:bg-slate-200 text-slate-800 text-xs font-semibold cursor-pointer"
            >
              {t.setup.claimTitle}
            </button>
          </div>
        )}

        <div className="flex flex-wrap items-center justify-between gap-2 text-xs pt-2 border-t border-slate-100">
          {mode === 'login' ? (
            <>
              <button type="button" onClick={() => setMode('register')} className="text-indigo-600 hover:underline cursor-pointer">
                {t.auth.switchToRegister}
              </button>
              <button type="button" onClick={() => setMode('forgot')} className="text-slate-600 hover:underline cursor-pointer">
                {t.auth.forgotPassword}
              </button>
            </>
          ) : (
            <button type="button" onClick={() => setMode('login')} className="text-indigo-600 hover:underline cursor-pointer">
              {t.auth.switchToLogin}
            </button>
          )}
        </div>
      </div>
    </div>
  );
};
