/**
 * Top header bar.
 *
 * Holds the current section title and space for global actions (user menu,
 * connection status, theme toggle) as they are introduced. Presentation only.
 */
export function Header() {
  return (
    <header className="flex h-14 shrink-0 items-center justify-between border-b border-neutral-800 bg-neutral-900 px-6">
      <h1 className="text-sm font-medium text-neutral-300">Scanner Platform</h1>
      <div className="text-xs text-neutral-500">Phase 1 · Infrastructure</div>
    </header>
  );
}
