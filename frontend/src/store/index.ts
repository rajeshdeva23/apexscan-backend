/**
 * Client-side global state (Zustand).
 *
 * Holds ephemeral UI/app state that is not server data (server data belongs to
 * TanStack Query). A single minimal store is provided as the pattern seed;
 * feature slices will be added as the UI grows.
 */
import { create } from 'zustand';

/** Shape of the global UI store. */
interface AppState {
  /** Whether the sidebar is collapsed. */
  sidebarCollapsed: boolean;
  /** Toggle the sidebar collapsed state. */
  toggleSidebar: () => void;
}

export const useAppStore = create<AppState>((set) => ({
  sidebarCollapsed: false,
  toggleSidebar: () => set((state) => ({ sidebarCollapsed: !state.sidebarCollapsed })),
}));
