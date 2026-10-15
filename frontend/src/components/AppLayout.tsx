import {
  AccountBalanceWalletOutlined, DashboardOutlined, KeyOutlined, LogoutOutlined, MenuOutlined,
  ModelTrainingOutlined, ReceiptLongOutlined, StorageOutlined, TimelineOutlined
} from '@mui/icons-material';
import { AppBar, Box, Button, Divider, Drawer, IconButton, List, ListItemButton, ListItemIcon, ListItemText, Toolbar, Typography, useMediaQuery, useTheme } from '@mui/material';
import { useState } from 'react';
import { NavLink, Outlet, useLocation } from 'react-router';
import { useAuth } from '../auth/AuthContext';

const drawerWidth = 248;
const navItems = [
  { path: '/', label: '总览', icon: <DashboardOutlined /> },
  { path: '/tenants', label: '租户与 Key', icon: <KeyOutlined /> },
  { path: '/models', label: '模型与价格', icon: <ModelTrainingOutlined /> },
  { path: '/ledger', label: '账本', icon: <AccountBalanceWalletOutlined /> },
  { path: '/requests', label: '请求', icon: <TimelineOutlined /> },
  { path: '/reservations', label: '预占', icon: <StorageOutlined /> },
  { path: '/customers', label: '客户门户开户', icon: <KeyOutlined /> },
  { path: '/payments', label: '充值订单', icon: <AccountBalanceWalletOutlined /> },
  { path: '/refunds', label: '退款审批', icon: <ReceiptLongOutlined /> }
];

export function AppLayout() {
  const theme = useTheme();
  const desktop = useMediaQuery(theme.breakpoints.up('md'));
  const [open, setOpen] = useState(false);
  const location = useLocation();
  const { logout } = useAuth();

  const navigation = (
    <Box sx={{ height: '100%', display: 'flex', flexDirection: 'column' }}>
      <Toolbar sx={{ px: 2.5 }}>
        <ReceiptLongOutlined color="primary" sx={{ mr: 1.25 }} />
        <Box>
          <Typography fontWeight={750}>LLM Gateway</Typography>
          <Typography variant="caption" color="text.secondary">管理控制台</Typography>
        </Box>
      </Toolbar>
      <Divider />
      <List sx={{ px: 1.5, py: 2 }}>
        {navItems.map((item) => (
          <ListItemButton key={item.path} component={NavLink} to={item.path} selected={location.pathname === item.path || (item.path !== '/' && location.pathname.startsWith(item.path))} onClick={() => setOpen(false)} sx={{ borderRadius: 1.5, mb: 0.5 }}>
            <ListItemIcon sx={{ minWidth: 40 }}>{item.icon}</ListItemIcon>
            <ListItemText primary={item.label} />
          </ListItemButton>
        ))}
      </List>
      <Box flexGrow={1} />
      <Box p={2}><Typography variant="caption" color="text.secondary">管理员操作会被审计记录</Typography></Box>
    </Box>
  );

  return (
    <Box sx={{ display: 'flex', minHeight: '100vh' }}>
      <AppBar position="fixed" color="inherit" elevation={0} sx={{ borderBottom: 1, borderColor: 'divider', ml: { md: `${drawerWidth}px` }, width: { md: `calc(100% - ${drawerWidth}px)` } }}>
        <Toolbar>
          {!desktop && <IconButton edge="start" onClick={() => setOpen(true)} aria-label="打开导航"><MenuOutlined /></IconButton>}
          <Box flexGrow={1} />
          <Box sx={{ width: 8, height: 8, bgcolor: 'success.main', borderRadius: '50%', mr: 1 }} />
          <Typography variant="body2" color="text.secondary" mr={2}>会话已验证</Typography>
          <Button color="inherit" startIcon={<LogoutOutlined />} onClick={logout}>退出</Button>
        </Toolbar>
      </AppBar>
      <Drawer variant={desktop ? 'permanent' : 'temporary'} open={desktop || open} onClose={() => setOpen(false)} ModalProps={{ keepMounted: true }} sx={{ width: drawerWidth, flexShrink: 0, '& .MuiDrawer-paper': { width: drawerWidth, boxSizing: 'border-box' } }}>{navigation}</Drawer>
      <Box component="main" sx={{ flexGrow: 1, minWidth: 0, pt: 8, ml: { xs: 0 }, width: { md: `calc(100% - ${drawerWidth}px)` } }}>
        <Box sx={{ p: { xs: 2, sm: 3, lg: 4 }, maxWidth: 1500, mx: 'auto' }}><Outlet /></Box>
      </Box>
    </Box>
  );
}
