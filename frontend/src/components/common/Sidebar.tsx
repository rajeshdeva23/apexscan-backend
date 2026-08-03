/**
 * Sidebar navigation.
 *
 * Primary navigation rail for the platform. Links are declared statically for
 * now; they will grow as scanner/strategy views are added. Presentation only.
 */
import { NavLink } from 'react-router';

// Navigation entries. Extend as new feature pages come online.
const navItems = [
  { to: '/', label: 'Home', end: true },
  { to: '/dashboard', label: 'Dashboard', end: false },
];

export function Sidebar() {
  return (
    <aside className="w-56 shrink-0 border-r border-neutral-800 bg-neutral-900 p-4">
      <div className="mb-6 text-lg font-semibold tracking-tight">ApexScan</div>
      <nav className="flex flex-col gap-1">
        {navItems.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.end}
            className={({ isActive }) =>
              `rounded px-3 py-2 text-sm transition-colors ${
                isActive
                  ? 'bg-neutral-800 text-white'
                  : 'text-neutral-400 hover:bg-neutral-800/60 hover:text-white'
              }`
            }
          >
            {item.label}
          </NavLink>
        ))}
      </nav>
    </aside>
  );
}
