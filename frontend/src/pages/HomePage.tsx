/**
 * Home page.
 *
 * Landing view of the application. Intentionally minimal in Phase 1 — it
 * confirms the shell renders and links into the dashboard.
 */
import { Link } from 'react-router';

export function HomePage() {
  return (
    <div className="mx-auto max-w-2xl">
      <h2 className="text-2xl font-semibold">Welcome to ApexScan</h2>
      <p className="mt-2 text-neutral-400">
        Professional trading scanner platform. This is the Phase 1
        infrastructure shell — strategies and market data arrive in later
        phases.
      </p>
      <Link
        to="/dashboard"
        className="mt-6 inline-block rounded bg-blue-600 px-4 py-2 text-sm font-medium hover:bg-blue-500"
      >
        Open Dashboard
      </Link>
    </div>
  );
}
