import { AddOutlined } from '@mui/icons-material';
import { Alert, Box, Button, Card, Dialog, DialogActions, DialogContent, DialogTitle, FormControl, InputLabel, MenuItem, Select, Stack, Table, TableBody, TableCell, TableContainer, TableHead, TableRow, TextField, ToggleButton, ToggleButtonGroup, Typography } from '@mui/material';
import { useState } from 'react';
import useSWR from 'swr';
import { apiRequest, swrFetcher } from '../api/client';
import { EmptyState, ErrorState, formatDate, LoadingState, PageHeader, StatusChip } from '../components/Common';
import type { ModelRoute, PageResult } from '../types';
import { formatMicroCny, parseMicroInput, yuanToMicro } from '../utils/money';

export function ModelsPage() {
  const models = useSWR<PageResult<ModelRoute>>('/admin/models', swrFetcher);
  const [modelOpen, setModelOpen] = useState(false);
  const [priceOpen, setPriceOpen] = useState(false);
  const [selectedModel, setSelectedModel] = useState('');
  if (models.isLoading) return <LoadingState />;
  if (models.error) return <ErrorState error={models.error} />;
  const routes = models.data?.items ?? [];
  const priceRows = routes.flatMap((model) => model.prices.map((price) => ({ model, price })));

  return <>
    <PageHeader title="模型与价格" description="配置公开模型的主备路由与不可变价格版本" action={<Stack direction="row" spacing={1}><Button variant="outlined" startIcon={<AddOutlined />} onClick={() => setModelOpen(true)}>创建模型路由</Button><Button variant="contained" startIcon={<AddOutlined />} onClick={() => setPriceOpen(true)}>追加价格版本</Button></Stack>} />
    <Stack spacing={3}>
      <Card><Box p={2.5}><Typography variant="h6" mb={2}>主备路由</Typography><TableContainer><Table><TableHead><TableRow><TableCell>公开模型</TableCell><TableCell>上游路由</TableCell><TableCell>顺序</TableCell><TableCell>状态</TableCell><TableCell>创建时间</TableCell></TableRow></TableHead><TableBody>{routes.map((model) => <TableRow key={model.id}><TableCell><Typography fontWeight={650}>{model.public_name}</Typography><Typography variant="caption" color="text.secondary">{model.id}</Typography></TableCell><TableCell>{model.litellm_model}</TableCell><TableCell>{model.route_order === 0 ? '主路由' : '备用路由'}</TableCell><TableCell><StatusChip value={model.active} /></TableCell><TableCell>{formatDate(model.created_at)}</TableCell></TableRow>)}</TableBody></Table></TableContainer>{!routes.length && <EmptyState />}</Box></Card>
      <Card><Box p={2.5}><Typography variant="h6" mb={2}>价格版本</Typography><TableContainer><Table><TableHead><TableRow><TableCell>模型</TableCell><TableCell>输入 / 百万 Token</TableCell><TableCell>输出 / 百万 Token</TableCell><TableCell>生效时间</TableCell></TableRow></TableHead><TableBody>{priceRows.map(({ model, price }) => <TableRow key={price.id}><TableCell>{model.public_name} · {model.route_order === 0 ? '主' : '备'}</TableCell><TableCell>{formatMicroCny(price.input_micro_cny_per_million)}</TableCell><TableCell>{formatMicroCny(price.output_micro_cny_per_million)}</TableCell><TableCell>{formatDate(price.effective_at)}</TableCell></TableRow>)}</TableBody></Table></TableContainer>{!priceRows.length && <EmptyState />}</Box></Card>
    </Stack>
    <ModelDialog open={modelOpen} onClose={() => setModelOpen(false)} onSuccess={async () => { setModelOpen(false); await models.mutate(); }} />
    <PriceDialog open={priceOpen} models={routes} selectedModel={selectedModel} setSelectedModel={setSelectedModel} onClose={() => setPriceOpen(false)} onSuccess={async () => { setPriceOpen(false); await models.mutate(); }} />
  </>;
}

function ModelDialog({ open, onClose, onSuccess }: { open: boolean; onClose: () => void; onSuccess: () => Promise<void> }) {
  const [publicName, setPublicName] = useState('');
  const [upstream, setUpstream] = useState('');
  const [order, setOrder] = useState('0');
  const [error, setError] = useState('');
  const submit = async () => { try { await apiRequest('/admin/models', { method: 'POST', body: { public_name: publicName, litellm_model: upstream, route_order: Number(order) } }); setPublicName(''); setUpstream(''); setError(''); await onSuccess(); } catch (reason) { setError(reason instanceof Error ? reason.message : '创建失败'); } };
  return <Dialog open={open} onClose={onClose} fullWidth maxWidth="sm"><DialogTitle>创建模型路由</DialogTitle><DialogContent><Stack spacing={2} pt={1}>{error && <Alert severity="error">{error}</Alert>}<TextField label="公开模型名称" value={publicName} onChange={(event) => setPublicName(event.target.value)} required /><TextField label="LiteLLM 路由别名" value={upstream} onChange={(event) => setUpstream(event.target.value)} helperText="填写 primary 或 fallback 等服务端已配置别名" required /><FormControl size="small"><InputLabel>路由顺序</InputLabel><Select value={order} label="路由顺序" onChange={(event) => setOrder(event.target.value)}><MenuItem value="0">主路由</MenuItem><MenuItem value="1">备用路由</MenuItem></Select></FormControl></Stack></DialogContent><DialogActions><Button onClick={onClose}>取消</Button><Button variant="contained" onClick={submit}>创建</Button></DialogActions></Dialog>;
}

function PriceDialog({ open, models, selectedModel, setSelectedModel, onClose, onSuccess }: { open: boolean; models: ModelRoute[]; selectedModel: string; setSelectedModel: (value: string) => void; onClose: () => void; onSuccess: () => Promise<void> }) {
  const [mode, setMode] = useState<'yuan' | 'micro'>('yuan');
  const [input, setInput] = useState('');
  const [output, setOutput] = useState('');
  const [effective, setEffective] = useState('');
  const [error, setError] = useState('');
  const submit = async () => {
    try {
      const converter = mode === 'yuan' ? yuanToMicro : parseMicroInput;
      await apiRequest('/admin/prices', { method: 'POST', body: { model_id: selectedModel, input_micro_cny_per_million: converter(input), output_micro_cny_per_million: converter(output), effective_at: new Date(effective).toISOString() } });
      setInput(''); setOutput(''); setEffective(''); setError(''); await onSuccess();
    } catch (reason) { setError(reason instanceof Error ? reason.message : '创建失败'); }
  };
  return <Dialog open={open} onClose={onClose} fullWidth maxWidth="sm"><DialogTitle>追加价格版本</DialogTitle><DialogContent><Stack spacing={2} pt={1}>{error && <Alert severity="error">{error}</Alert>}<FormControl size="small"><InputLabel>模型路由</InputLabel><Select value={selectedModel} label="模型路由" onChange={(event) => setSelectedModel(event.target.value)}>{models.map((model) => <MenuItem key={model.id} value={model.id}>{model.public_name} · {model.route_order === 0 ? '主' : '备'}</MenuItem>)}</Select></FormControl><ToggleButtonGroup exclusive size="small" value={mode} onChange={(_, value) => value && setMode(value)}><ToggleButton value="yuan">人民币 / 百万 Token</ToggleButton><ToggleButton value="micro">micro-CNY / 百万 Token</ToggleButton></ToggleButtonGroup><TextField label={`输入价格（${mode === 'yuan' ? '人民币' : 'micro-CNY'}）`} value={input} onChange={(event) => setInput(event.target.value)} /><TextField label={`输出价格（${mode === 'yuan' ? '人民币' : 'micro-CNY'}）`} value={output} onChange={(event) => setOutput(event.target.value)} /><TextField label="生效时间" type="datetime-local" value={effective} onChange={(event) => setEffective(event.target.value)} slotProps={{ inputLabel: { shrink: true } }} /></Stack></DialogContent><DialogActions><Button onClick={onClose}>取消</Button><Button variant="contained" disabled={!selectedModel || !input || !output || !effective} onClick={submit}>追加版本</Button></DialogActions></Dialog>;
}
