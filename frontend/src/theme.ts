import { createTheme } from '@mui/material/styles';

export const theme = createTheme({
  palette: {
    mode: 'light',
    primary: { main: '#1677ff', dark: '#0958d9' },
    background: { default: '#f7f9fc', paper: '#ffffff' },
    text: { primary: '#1d2635', secondary: '#64748b' },
    divider: '#e8edf3',
    success: { main: '#18a058' },
    warning: { main: '#d97706' },
    error: { main: '#dc2626' }
  },
  shape: { borderRadius: 10 },
  typography: {
    fontFamily: 'Inter, "PingFang SC", "Microsoft YaHei", system-ui, sans-serif',
    h4: { fontSize: '1.6rem', fontWeight: 700 },
    h5: { fontWeight: 700 },
    h6: { fontWeight: 650 },
    button: { textTransform: 'none', fontWeight: 600 }
  },
  components: {
    MuiCard: { styleOverrides: { root: { border: '1px solid #e8edf3', boxShadow: '0 1px 3px rgba(15, 23, 42, .04)' } } },
    MuiButton: { defaultProps: { disableElevation: true } },
    MuiTableCell: { styleOverrides: { head: { color: '#475569', fontWeight: 650, background: '#fafbfc' } } },
    MuiTextField: { defaultProps: { size: 'small' } }
  }
});
