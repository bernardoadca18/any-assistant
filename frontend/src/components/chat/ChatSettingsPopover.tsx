import React, { useState } from 'react';
import { Settings2, X, RefreshCw } from 'lucide-react';
import { Dialog } from '@/components/ui/Dialog';
import type { ModelInfo } from '@/types';
export interface ChatSettings { model: string; top_k: number; project_source_filter: string; }
interface Props { settings: ChatSettings; onChange: (value: ChatSettings) => void; models: ModelInfo[]; loading: boolean; error: string; onRetry: () => void; disabled?: boolean; }
export function ChatSettingsPopover({ settings, onChange, models, loading, error, onRetry, disabled }: Props) {
  const [open, setOpen] = useState(false); const [draft, setDraft] = useState(settings);
  return <><button className="text-button settings-trigger" disabled={disabled} onClick={() => { setDraft(settings); setOpen(true); }} aria-label="Configurações da conversa"><Settings2 size={16} /><span>Configurações</span></button>
    <Dialog open={open} onClose={() => setOpen(false)} title="Configurações da conversa"><div className="dialog-heading"><div><p className="eyebrow">PREFERÊNCIAS</p><h2>Configurações da conversa</h2></div><button className="icon-button" onClick={() => setOpen(false)} aria-label="Fechar configurações"><X size={20} /></button></div>
      <div className="settings-fields"><label htmlFor="chat-model">Modelo de resposta</label><p>Modelos disponibilizados pelo serviço configurado.</p>
        {error && <div className="inline-error" role="alert">{error}<button className="text-button" onClick={onRetry}><RefreshCw size={14} />Tentar novamente</button></div>}
        <select id="chat-model" value={draft.model} disabled={loading} onChange={e => setDraft({ ...draft, model: e.target.value })}><option value="">{loading ? 'Carregando modelos…' : 'Selecione um modelo'}</option>{models.map(model => <option key={model.id} value={model.id}>{model.id}</option>)}{draft.model && !models.some(m => m.id === draft.model) && <option value={draft.model}>{draft.model} (configurado na conversa)</option>}</select>
        <label htmlFor="chat-top-k">Trechos a consultar <output>{draft.top_k}</output></label><p>Limite de trechos recuperados da base (top_k).</p><input id="chat-top-k" type="range" min={1} max={20} value={draft.top_k} onChange={e => setDraft({ ...draft, top_k: Number(e.target.value) })} />
        <label htmlFor="chat-source-filter">Filtrar por projeto <span>opcional</span></label><p>Use o identificador de origem presente nos documentos.</p><input id="chat-source-filter" value={draft.project_source_filter} placeholder="Ex.: GodotDocs" onChange={e => setDraft({ ...draft, project_source_filter: e.target.value })} /></div>
      <div className="dialog-actions"><button className="text-button" onClick={() => setOpen(false)}>Cancelar</button><button className="primary-button" onClick={() => { onChange(draft); setOpen(false); }}>Aplicar configurações</button></div>
    </Dialog></>;
}
