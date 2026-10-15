import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router';
import { CssBaseline, ThemeProvider } from '@mui/material';
import { SWRConfig } from 'swr';
import { App } from './App';
import { AuthProvider } from './auth/AuthContext';
import { theme } from './theme';

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ThemeProvider theme={theme}>
      <CssBaseline />
      <SWRConfig value={{ provider: () => new Map(), shouldRetryOnError: false, revalidateOnFocus: false }}>
        <AuthProvider>
          <BrowserRouter><App /></BrowserRouter>
        </AuthProvider>
      </SWRConfig>
    </ThemeProvider>
  </StrictMode>
);
