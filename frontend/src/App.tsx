import { lazy, Suspense, type ReactNode } from 'react';
import { Navigate, Route, Routes } from 'react-router';
import { useAuth } from './auth/AuthContext';
import { AppLayout } from './components/AppLayout';
import { LoadingState } from './components/Common';
import { LoginPage } from './pages/LoginPage';

const DashboardPage = lazy(() => import('./pages/DashboardPage').then((module) => ({ default: module.DashboardPage })));
const TenantsPage = lazy(() => import('./pages/TenantsPage').then((module) => ({ default: module.TenantsPage })));
const ModelsPage = lazy(() => import('./pages/ModelsPage').then((module) => ({ default: module.ModelsPage })));
const LedgerPage = lazy(() => import('./pages/LedgerPage').then((module) => ({ default: module.LedgerPage })));
const RequestsPage = lazy(() => import('./pages/RequestsPage').then((module) => ({ default: module.RequestsPage })));
const ReservationsPage = lazy(() => import('./pages/ReservationsPage').then((module) => ({ default: module.ReservationsPage })));
const CustomerRoutes = lazy(() => import('./customer/CustomerRoutes').then((module) => ({ default: module.CustomerRoutes })));
const AdminPaymentsPage = lazy(() => import('./pages/FinancePages').then((module) => ({ default: module.AdminPaymentsPage })));
const AdminRefundsPage = lazy(() => import('./pages/FinancePages').then((module) => ({ default: module.AdminRefundsPage })));

function Protected({ children }: { children: ReactNode }) {
  const { authenticated } = useAuth();
  return authenticated ? children : <Navigate to="/login" replace />;
}

const deferred = (page: ReactNode) => <Suspense fallback={<LoadingState />}>{page}</Suspense>;

export function App() {
  return <Routes>
    <Route path="/login" element={<LoginPage />} />
    <Route path="/portal/*" element={deferred(<CustomerRoutes />)} />
    <Route element={<Protected><AppLayout /></Protected>}>
      <Route index element={deferred(<DashboardPage />)} />
      <Route path="tenants" element={deferred(<TenantsPage />)} />
      <Route path="models" element={deferred(<ModelsPage />)} />
      <Route path="ledger" element={deferred(<LedgerPage />)} />
      <Route path="requests" element={deferred(<RequestsPage />)} />
      <Route path="reservations" element={deferred(<ReservationsPage />)} />
      <Route path="customers" element={deferred(<TenantsPage />)} />
      <Route path="payments" element={deferred(<AdminPaymentsPage />)} />
      <Route path="refunds" element={deferred(<AdminRefundsPage />)} />
    </Route>
    <Route path="*" element={<Navigate to="/portal/login" replace />} />
  </Routes>;
}
