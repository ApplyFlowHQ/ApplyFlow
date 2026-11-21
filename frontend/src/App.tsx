import React, { useEffect, useState } from 'react';
import { BrowserRouter, Navigate, Route, Routes, useNavigate } from 'react-router-dom';
import { I18nProvider, useI18n } from './i18n';
import { apiRequest, setCsrfToken } from './lib/api';
import type { SetupStatusResponse } from './lib/setupTypes';
import { AppShell, type UserSummary } from './components/AppShell';
import { AuthPage } from './pages/AuthPage';
import { SetupWizardPage } from './pages/SetupWizardPage';
import { DashboardPage } from './pages/DashboardPage';
import { CvContainerPage } from './pages/CvContainerPage';
import { PreferencesPage } from './pages/PreferencesPage';
import { BatchesPage } from './pages/BatchesPage';
import { TrackerPage } from './pages/TrackerPage';
import { SettingsPage } from './pages/SettingsPage';

interface AppProps {
  pwaNeedsRefresh: boolean;
  onUpdatePwa: () => void;
}

const AppRouter: React.FC<AppProps> = ({ pwaNeedsRefresh, onUpdatePwa }) => {
  const { setLocale } = useI18n();
  const navigate = useNavigate();
  const [user, setUser] = useState<UserSummary | null>(null);
  const [setupStatus, setSetupStatus] = useState<SetupStatusResponse | null>(null);
  const [showAuthOnClaim, setShowAuthOnClaim] = useState(false);
  const [loading, setLoading] = useState(true);

  const loadAllState = async () => {
    try {
      const [meRes, setupRes] = await Promise.allSettled([
        apiRequest<{ user: UserSummary }>('/auth/me'),
        apiRequest<SetupStatusResponse>('/setup/status'),
      ]);
      if (meRes.status === 'fulfilled') {
        const u = meRes.value.user;
        setUser(u);
        setCsrfToken(u.csrf_token);
        if (u.ui_locale === 'ar' || u.ui_locale === 'en') {
          setLocale(u.ui_locale);
        }
      } else {
        setUser(null);
      }
      if (setupRes.status === 'fulfilled') {
        setSetupStatus(setupRes.value);
      }
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadAllState();
  }, []);

  const handleLogout = async () => {
    try {
      await apiRequest('/auth/logout', { method: 'POST' });
    } catch {
      // clear local state regardless
    }
    setCsrfToken('');
    setUser(null);
    navigate('/', { replace: true });
    await loadAllState();
  };

  if (loading) {
    return (
      <div className="min-h-screen bg-slate-50 flex items-center justify-center text-sm text-slate-600">
        Loading ApplyFlow Global...
      </div>
    );
  }

  return (
    <>
      {!user ? (
        <Routes>
          <Route
            path="/setup"
            element={
              <SetupWizardPage
                user={null}
                setupStatus={setupStatus}
                onRefreshStatus={loadAllState}
                onAuthenticated={async (u) => {
                  setUser(u);
                  if (u.ui_locale === 'ar' || u.ui_locale === 'en') setLocale(u.ui_locale);
                }}
              />
            }
          />
          <Route
            path="*"
            element={
              setupStatus?.requires_claim && !showAuthOnClaim ? (
                <SetupWizardPage
                  user={null}
                  setupStatus={setupStatus}
                  onRefreshStatus={loadAllState}
                  onAuthenticated={async (u) => {
                    setUser(u);
                    if (u.ui_locale === 'ar' || u.ui_locale === 'en') setLocale(u.ui_locale);
                  }}
                />
              ) : (
                <AuthPage
                  googleSignInEnabled={Boolean(setupStatus?.google_signin_enabled)}
                  requiresClaim={Boolean(setupStatus?.requires_claim)}
                  onGoToClaim={() => setShowAuthOnClaim(false)}
                  onAuthenticated={(u) => {
                    setUser(u);
                    if (u.ui_locale === 'ar' || u.ui_locale === 'en') setLocale(u.ui_locale);
                    navigate(u.can_manage_installation && u.setup_state !== 'completed' ? '/setup' : '/dashboard', { replace: true });
                    loadAllState();
                  }}
                />
              )
            }
          />
        </Routes>
      ) : (
        <Routes>
          <Route
            element={
              <AppShell
                user={user}
                onRefreshUser={loadAllState}
                onLogout={handleLogout}
                pwaNeedsRefresh={pwaNeedsRefresh}
                onUpdatePwa={onUpdatePwa}
              />
            }
          >
            <Route path="/dashboard" element={<DashboardPage user={user} onRefreshUser={loadAllState} />} />
            <Route
              path="/setup"
              element={
                <SetupWizardPage
                  user={user}
                  setupStatus={setupStatus}
                  onRefreshStatus={loadAllState}
                  onAuthenticated={async (u) => setUser(u)}
                />
              }
            />
            <Route path="/cvs" element={<CvContainerPage onUpdated={loadAllState} />} />
            <Route path="/preferences" element={<PreferencesPage onUpdated={loadAllState} />} />
            <Route path="/batches" element={<BatchesPage automationStopped={Boolean(user.automation_stopped_at)} />} />
            <Route path="/tracker" element={<TrackerPage />} />
            <Route path="/settings" element={<SettingsPage user={user} onRefreshUser={loadAllState} onLoggedOut={() => setUser(null)} />} />
            <Route
              path="*"
              element={
                <Navigate
                  to={user.can_manage_installation && user.setup_state !== 'completed' ? '/setup' : '/dashboard'}
                  replace
                />
              }
            />
          </Route>
        </Routes>
      )}
    </>
  );
};

export const App: React.FC<AppProps> = (props) => (
  <I18nProvider>
    <BrowserRouter>
      <AppRouter {...props} />
    </BrowserRouter>
  </I18nProvider>
);

export default App;
