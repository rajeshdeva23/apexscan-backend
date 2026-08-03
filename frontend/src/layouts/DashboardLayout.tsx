/**
 * Dashboard layout shell.
 *
 * Provides the persistent application frame — sidebar + header — around the
 * routed page content rendered via <Outlet />. Layout only; no data logic.
 */
import { Outlet } from 'react-router';

import { Sidebar } from '@/components/common/Sidebar';
import { Header } from '@/components/common/Header';

export function DashboardLayout() {
  return (
    <div className="flex h-screen bg-neutral-950 text-neutral-100">
      <Sidebar />
      <div className="flex flex-1 flex-col overflow-hidden">
        <Header />
        <main className="flex-1 overflow-auto p-6">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
