/**
 * Application route table.
 *
 * Central definition of client-side routes using React Router's data router.
 * The dashboard layout wraps feature pages so navigation chrome (sidebar,
 * header) is shared. Add new pages by registering child routes here.
 */
import { createBrowserRouter } from 'react-router';

import { DashboardLayout } from '@/layouts/DashboardLayout';
import { HomePage } from '@/pages/HomePage';
import { DashboardPage } from '@/pages/DashboardPage';

export const router = createBrowserRouter([
  {
    path: '/',
    element: <DashboardLayout />,
    children: [
      { index: true, element: <HomePage /> },
      { path: 'dashboard', element: <DashboardPage /> },
    ],
  },
]);
