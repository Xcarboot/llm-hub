import { Navigate, Route, Routes } from 'react-router';
import { CustomerAuthProvider } from './CustomerAuthContext';
import { CustomerProtected, CustomerShell, CustomerTheme } from './CustomerShell';
import { CustomerLoginPage } from './AuthPages';
import { CustomerDocsPage, CustomerHomePage, CustomerUsagePage } from './AccountPages';
import { CustomerKeysPage } from './KeysPage';
import { CustomerPaymentsPage } from './PaymentsPage';
import { CustomerRefundsPage } from './RefundsPage';

export function CustomerRoutes() {
  return <CustomerTheme><CustomerAuthProvider><Routes>
    <Route path="login" element={<CustomerLoginPage />} />
    <Route path="activate" element={<CustomerLoginPage activation />} />
    <Route element={<CustomerProtected />}><Route element={<CustomerShell />}>
      <Route index element={<CustomerHomePage />} />
      <Route path="home" element={<CustomerHomePage />} />
      <Route path="keys" element={<CustomerKeysPage />} />
      <Route path="usage" element={<CustomerUsagePage />} />
      <Route path="payments" element={<CustomerPaymentsPage />} />
      <Route path="refunds" element={<CustomerRefundsPage />} />
      <Route path="docs" element={<CustomerDocsPage />} />
    </Route></Route>
    <Route path="*" element={<Navigate to="/portal" replace />} />
  </Routes></CustomerAuthProvider></CustomerTheme>;
}
