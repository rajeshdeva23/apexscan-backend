/**
 * Dashboard page.
 *
 * Empty dashboard placeholder. Future scanner grids (AG Grid) and price
 * charts (TradingView Lightweight Charts) will be composed here. No data
 * wiring in Phase 1.
 */
export function DashboardPage() {
  return (
    <div>
      <h2 className="text-xl font-semibold">Dashboard</h2>
      <div className="mt-4 grid gap-4 md:grid-cols-2">
        <div className="flex h-48 items-center justify-center rounded-lg border border-dashed border-neutral-700 text-neutral-500">
          Scanner grid placeholder
        </div>
        <div className="flex h-48 items-center justify-center rounded-lg border border-dashed border-neutral-700 text-neutral-500">
          Chart placeholder
        </div>
      </div>
    </div>
  );
}
