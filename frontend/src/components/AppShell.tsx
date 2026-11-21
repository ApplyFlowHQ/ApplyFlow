import React, { useEffect, useState } from 'react';
import { NavLink, Outlet } from 'react-router-dom';
import {
  Briefcase,
  FileText,
  Globe,
  LayoutDashboard,
  LogOut,
  PlayCircle,
  Settings,
  Sliders,
  Sparkles,
  Square,
  Table2,
  WifiOff,
} from 'lucide-react';
import { useI18n } from '../i18n';
import { apiRequest } from '../lib/api';

export interface UserSummary {
  id: string;
  email: string;
  full_name: string;
  email_verified: boolean;
  ui_locale: 'en' | 'ar';
  timezone: string;
  is_operator: boolean;
  is_owner?: boolean;
  can_manage_installation?: boolean;
  setup_state?: 'unclaimed' | 'in_progress' | 'completed';
  automation_stopped_at: string | null;
  retention_days: number;
  live_sending_enabled_on_server: boolean;
  csrf_token: string;
  onboarding: {
    email_verified: boolean;
    cv_uploaded: boolean;
    profile_confirmed: boolean;
    preferences_configured: boolean;
    mailbox_connected: boolean;
    automation_active: boolean;
  };
}

interface AppShellProps {
  user: UserSummary;
  onRefreshUser: () => Promise<void>;
  onLogout: () => Promise<void>;
  pwaNeedsRefresh?: boolean;
  onUpdatePwa?: () => void;
}

export const AppShell: React.FC<AppShellProps> = ({
  user,
  onRefreshUser,
  onLogout,
  pwaNeedsRefresh,
  onUpdatePwa,
}) => {
  const { t, locale, toggleLocale } = useI18n();
  const [isOnline, setIsOnline] = useState<boolean>(navigator.onLine);
  const [busyStop, setBusyStop] = useState(false);

  useEffect(() => {
    const on = () => setIsOnline(true);
    const off = () => setIsOnline(false);
    window.addEventListener('online', on);
    window.addEventListener('offline', off);
    return () => {
      window.removeEventListener('online', on);
      window.removeEventListener('offline', off);
    };
  }, []);

  const handleToggleAutomation = async () => {
    setBusyStop(true);
    try {
      if (user.automation_stopped_at) {
        await apiRequest('/account/resume-automation', { method: 'POST' });
      } else {
        await apiRequest('/account/stop-automation', { method: 'POST' });
      }
      await onRefreshUser();
    } finally {
      setBusyStop(false);
    }
  };

  const handleToggleLocale = async () => {
    const nextLocale = locale === 'en' ? 'ar' : 'en';
    toggleLocale();
    if (navigator.onLine) {
      try {
        await apiRequest('/account/settings', {
          method: 'PATCH',
          body: JSON.stringify({ ui_locale: nextLocale }),
        });
      } catch {
        // local toggle already applied cleanly
      }
    }
  };

  const navItems = [
    { to: '/dashboard', label: t.nav.dashboard, icon: LayoutDashboard },
    ...(user.can_manage_installation ? [{ to: '/setup', label: t.nav.setup, icon: Sparkles }] : []),
    { to: '/cvs', label: t.nav.cvs, icon: FileText },
    { to: '/preferences', label: t.nav.preferences, icon: Sliders },
    { to: '/batches', label: t.nav.batches, icon: PlayCircle },
    { to: '/tracker', label: t.nav.tracker, icon: Table2 },
    { to: '/settings', label: t.nav.settings, icon: Settings },
  ];

  return (
    <div className="min-h-screen bg-slate-50 text-slate-900 flex flex-col lg:flex-row">
      {/* Desktop Sidebar */}
      <aside className="hidden lg:flex lg:w-64 lg:flex-col lg:fixed lg:inset-y-0 lg:start-0 bg-slate-950 text-slate-100 border-e border-slate-800">
        <div className="flex items-center gap-3 px-5 py-5 border-b border-slate-800">
          <div className="h-9 w-9 rounded-xl bg-indigo-600 flex items-center justify-center shrink-0">
            <Briefcase className="h-5 w-5 text-white" aria-hidden="true" />
          </div>
          <div className="min-w-0">
            <div className="font-semibold text-sm tracking-tight truncate">{t.appName}</div>
            <div className="text-xs text-slate-400 truncate">{user.email}</div>
          </div>
        </div>

        <nav className="flex-1 px-3 py-4 space-y-1" aria-label="Main navigation">
          {navItems.map((item) => {
            const Icon = item.icon;
            return (
              <NavLink
                key={item.to}
                to={item.to}
                className={({ isActive }) =>
                  `flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium transition-colors ${
                    isActive
                      ? 'bg-indigo-600 text-white'
                      : 'text-slate-300 hover:bg-slate-800/80 hover:text-white'
                  }`
                }
              >
                <Icon className="h-4 w-4 shrink-0" aria-hidden="true" />
                <span>{item.label}</span>
              </NavLink>
            );
          })}
        </nav>

        <div className="p-4 border-t border-slate-800 space-y-2">
          <button
            type="button"
            onClick={handleToggleAutomation}
            disabled={busyStop || !isOnline}
            className={`w-full flex items-center justify-center gap-2 px-3 py-2.5 rounded-lg text-sm font-semibold transition-colors cursor-pointer disabled:opacity-50 ${
              user.automation_stopped_at
                ? 'bg-emerald-600 hover:bg-emerald-500 text-white'
                : 'bg-rose-600 hover:bg-rose-500 text-white'
            }`}
          >
            <Square className="h-4 w-4" aria-hidden="true" />
            <span>{user.automation_stopped_at ? t.controls.resumeAutomation : t.controls.stopAutomation}</span>
          </button>

          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={handleToggleLocale}
              className="flex-1 flex items-center justify-center gap-1.5 px-3 py-2 rounded-lg text-xs font-medium bg-slate-800 hover:bg-slate-700 text-slate-200 cursor-pointer"
            >
              <Globe className="h-3.5 w-3.5" aria-hidden="true" />
              <span>{t.controls.languageToggle}</span>
            </button>
            <button
              type="button"
              onClick={onLogout}
              className="flex items-center justify-center gap-1.5 px-3 py-2 rounded-lg text-xs font-medium bg-slate-800 hover:bg-slate-700 text-slate-200 cursor-pointer"
              aria-label={t.nav.logout}
              title={t.nav.logout}
            >
              <LogOut className="h-3.5 w-3.5" aria-hidden="true" />
            </button>
          </div>
        </div>
      </aside>

      {/* Main Content Area */}
      <div className="flex-1 lg:ps-64 flex flex-col min-w-0 pb-20 lg:pb-6">
        {/* Mobile Top Bar */}
        <header className="lg:hidden sticky top-0 z-20 bg-slate-950 text-white px-4 py-3 flex items-center justify-between gap-2 border-b border-slate-800">
          <div className="flex items-center gap-2 min-w-0">
            <Briefcase className="h-5 w-5 text-indigo-400 shrink-0" aria-hidden="true" />
            <span className="font-semibold text-sm truncate">{t.appName}</span>
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={handleToggleLocale}
              className="px-2.5 py-1.5 rounded-md bg-slate-800 text-xs font-medium text-slate-100"
            >
              {t.controls.languageToggle}
            </button>
            <button
              type="button"
              onClick={handleToggleAutomation}
              disabled={busyStop || !isOnline}
              className={`px-2.5 py-1.5 rounded-md text-xs font-semibold ${
                user.automation_stopped_at ? 'bg-emerald-600 text-white' : 'bg-rose-600 text-white'
              }`}
            >
              {user.automation_stopped_at ? t.controls.resumeAutomation : t.controls.stopAutomation}
            </button>
          </div>
        </header>

        {/* Status Banners */}
        {!isOnline && (
          <div role="status" className="bg-amber-50 border-b border-amber-200 px-4 py-2.5 text-amber-900 text-xs sm:text-sm flex items-center gap-2">
            <WifiOff className="h-4 w-4 shrink-0" aria-hidden="true" />
            <span>{t.controls.offlineBanner}</span>
          </div>
        )}

        {user.automation_stopped_at && (
          <div role="alert" className="bg-rose-50 border-b border-rose-200 px-4 py-2.5 text-rose-900 text-xs sm:text-sm flex items-center justify-between gap-3">
            <span>{t.controls.automationStoppedBanner}</span>
            <button
              type="button"
              onClick={handleToggleAutomation}
              disabled={busyStop || !isOnline}
              className="px-3 py-1 rounded bg-rose-700 text-white text-xs font-semibold shrink-0 cursor-pointer"
            >
              {t.controls.resumeAutomation}
            </button>
          </div>
        )}

        {pwaNeedsRefresh && onUpdatePwa && (
          <div className="bg-indigo-50 border-b border-indigo-200 px-4 py-2 text-indigo-900 text-xs sm:text-sm flex items-center justify-between gap-2">
            <span>{t.controls.pwaUpdateAvailable}</span>
            <button
              type="button"
              onClick={onUpdatePwa}
              className="px-3 py-1 rounded bg-indigo-600 text-white text-xs font-semibold cursor-pointer"
            >
              {t.controls.reloadNow}
            </button>
          </div>
        )}

        {user.can_manage_installation && user.setup_state && user.setup_state !== 'completed' && (
          <div className="bg-indigo-50/90 border-b border-indigo-200 px-4 py-2 text-indigo-900 text-xs sm:text-sm flex items-center justify-between gap-2">
            <span>{t.setup.wizardResumeBanner}</span>
            <NavLink
              to="/setup"
              className="px-3 py-1 rounded bg-indigo-600 text-white text-xs font-semibold shrink-0"
            >
              {t.nav.setup}
            </NavLink>
          </div>
        )}

        <main className="flex-1 w-full max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-6">
          <Outlet />
        </main>
      </div>

      {/* Mobile Bottom Nav */}
      <nav
        className="lg:hidden fixed bottom-0 inset-x-0 z-20 bg-white border-t border-slate-200 flex justify-around py-1.5 px-1"
        aria-label="Mobile navigation"
      >
        {navItems.map((item) => {
          const Icon = item.icon;
          return (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) =>
                `flex flex-col items-center gap-0.5 px-2 py-1 rounded-md text-[11px] font-medium min-w-[48px] ${
                  isActive ? 'text-indigo-600' : 'text-slate-600'
                }`
              }
            >
              <Icon className="h-4 w-4" aria-hidden="true" />
              <span className="truncate max-w-[60px]">{item.label}</span>
            </NavLink>
          );
        })}
      </nav>
    </div>
  );
};
