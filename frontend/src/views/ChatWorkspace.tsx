import React, { useCallback, useEffect, useRef, useState } from 'react';
import { ArrowDown, ArrowUp, BookOpen, Check, Copy, History, Loader2, MessageSquare, Plus, RefreshCw, Sparkles, Square, Trash2, X } from 'lucide-react';
import { useAppStore } from '@/store/useAppStore';
import * as sessionsService from '@/services/sessionsService';
import { sendChatMessageStream, listModels } from '@/services/chatService';
import { MarkdownMessage } from '@/components/chat/MarkdownMessage';
import { SourcesPanel } from '@/components/chat/SourcesPanel';
import { ChatSettingsPopover, type ChatSettings } from '@/components/chat/ChatSettingsPopover';
import { Dialog } from '@/components/ui/Dialog';
import type { MessageResponse, RetrievedChunk, SessionSummaryResponse, ModelInfo } from '@/types';

interface UiMessage { id: string; role: 'user' | 'assistant'; content: string; status: 'success' | 'error' | 'pending' | 'streaming'; retrieved_chunks?: RetrievedChunk[]; model_used?: string | null; created_at: string; }
const newId = () => crypto.randomUUID();
function mapMessage(m: MessageResponse): UiMessage {
  const chunks = (m.retrieved_chunks || []).map(chunk => ({ ...chunk,
    doc_type: typeof chunk.doc_type === 'string' ? chunk.doc_type : chunk.doc_type?.code,
    project_source: 'project_source' in chunk ? chunk.project_source : 'project' in chunk ? chunk.project?.code : undefined,
  }));
  return { ...m, role: m.role === 'assistant' ? 'assistant' : 'user', status: (m.status as UiMessage['status']) || 'success', retrieved_chunks: chunks };
}
function CopyResponse({ content }: { content: string }) {
  const [state, setState] = useState('');
  return <button className="text-button copy-response" onClick={async () => { try { await navigator.clipboard.writeText(content); setState('Copiado'); } catch { setState('Não foi possível copiar'); } }} aria-label="Copiar resposta">{state === 'Copiado' ? <Check size={14} /> : <Copy size={14} />}<span aria-live="polite">{state || 'Copiar resposta'}</span></button>;
}
export function ChatWorkspace() {
  const { activeDomainId } = useAppStore();
  const domain = activeDomainId === 'ue5' ? 'Unreal Engine 5' : 'Godot 4';
  const [sessions, setSessions] = useState<SessionSummaryResponse[]>([]);
  const [sessionsLoading, setSessionsLoading] = useState(false);
  const [sessionsError, setSessionsError] = useState('');
  const [activeSessionId, setActiveSessionId] = useState<string>(newId);
  const [messages, setMessages] = useState<UiMessage[]>([]);
  const [messagesLoading, setMessagesLoading] = useState(false);
  const [input, setInput] = useState('');
  const [isStreaming, setIsStreaming] = useState(false);
  const [notice, setNotice] = useState('');
  const [historyOpen, setHistoryOpen] = useState(false);
  const [search, setSearch] = useState('');
  const [deleteId, setDeleteId] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [atBottom, setAtBottom] = useState(true);
  const scrollRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const followRef = useRef(true);
  const busyRef = useRef(false);
  const controllerRef = useRef<AbortController | null>(null);
  const selectionRef = useRef(0);
  const mountedRef = useRef(true);
  const [settings, setSettings] = useState<ChatSettings>({ model: '', top_k: 5, project_source_filter: '' });
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [modelsLoading, setModelsLoading] = useState(false);
  const [modelsError, setModelsError] = useState('');
  const loadSessions = useCallback(async () => {
    setSessionsLoading(true); setSessionsError('');
    try { const result = await sessionsService.listSessions(); if (mountedRef.current) setSessions(result); }
    catch { if (mountedRef.current) setSessionsError('Não foi possível atualizar o histórico.'); }
    finally { if (mountedRef.current) setSessionsLoading(false); }
  }, []);
  const loadModels = useCallback(async () => {
    setModelsLoading(true); setModelsError('');
    try { const result = await listModels(); if (!mountedRef.current) return; setModels(result); setSettings(s => ({ ...s, model: s.model || result[0]?.id || '' })); if (!result.length) setModelsError('Nenhum modelo disponível no serviço.'); }
    catch { if (mountedRef.current) setModelsError('Não foi possível carregar os modelos. Verifique o acesso e os serviços.'); }
    finally { if (mountedRef.current) setModelsLoading(false); }
  }, []);
  useEffect(() => { mountedRef.current = true; loadSessions(); loadModels(); return () => { mountedRef.current = false; selectionRef.current++; controllerRef.current?.abort(); }; }, [loadSessions, loadModels]);
  useEffect(() => { if (followRef.current && scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight; }, [messages]);
  useEffect(() => { const el = textareaRef.current; if (el) { el.style.height = 'auto'; el.style.height = `${Math.min(el.scrollHeight, 180)}px`; } }, [input]);
  const newConversation = () => {
    if (busyRef.current || messagesLoading) return;
    selectionRef.current++; setActiveSessionId(newId()); setMessages([]); setNotice(''); setInput(''); setHistoryOpen(false); followRef.current = true; setAtBottom(true); textareaRef.current?.focus();
  };
  const selectSession = async (id: string) => {
    if (busyRef.current) return;
    const selection = ++selectionRef.current; setMessagesLoading(true); setNotice('');
    try {
      const detail = await sessionsService.getSession(id);
      if (selection !== selectionRef.current) return;
      setActiveSessionId(id); setMessages(detail.messages.map(mapMessage)); setInput(''); setHistoryOpen(false); followRef.current = true;
      setSettings(s => ({ ...s, model: detail.default_model || s.model, project_source_filter: detail.project_source_filter || '' }));
    } catch { if (selection === selectionRef.current) setNotice('Não foi possível abrir a conversa. O conteúdo atual foi preservado. Tente novamente pelo histórico.'); }
    finally { if (selection === selectionRef.current) setMessagesLoading(false); }
  };
  const removeSession = async () => {
    if (!deleteId || deleting || busyRef.current) return;
    setDeleting(true);
    try { await sessionsService.deleteSession(deleteId); setSessions(list => list.filter(s => s.id !== deleteId)); if (deleteId === activeSessionId) newConversation(); setDeleteId(null); }
    catch { setDeleteId(null); setNotice('Não foi possível excluir a conversa. Ela foi mantida no histórico.'); }
    finally { setDeleting(false); }
  };
  const send = async () => {
    const text = input.trim();
    if (!text || busyRef.current || messagesLoading || !settings.model) return;
    busyRef.current = true; setIsStreaming(true); setNotice(''); setInput(''); followRef.current = true; setAtBottom(true);
    const assistantId = newId(); const created = new Date().toISOString();
    setMessages(list => [...list, { id: newId(), role: 'user', content: text, status: 'success', created_at: created }, { id: assistantId, role: 'assistant', content: '', status: 'streaming', retrieved_chunks: [], created_at: created }]);
    const controller = new AbortController(); controllerRef.current = controller;
    let failed = false; let finished = false; let accumulated = '';
    const update = (patch: Partial<UiMessage>) => { if (mountedRef.current) setMessages(list => list.map(m => m.id === assistantId ? { ...m, ...patch } : m)); };
    const fail = () => { failed = true; update({ status: 'error' }); if (mountedRef.current) { setNotice('A resposta foi interrompida. O conteúdo recebido foi mantido e a pergunta voltou ao campo para você tentar novamente.'); setInput(current => current || text); } };
    try {
      await sendChatMessageStream({ session_id: activeSessionId, message: text, model: settings.model, top_k: settings.top_k, project_source_filter: settings.project_source_filter || null, domain_id: activeDomainId, stream: true }, {
        onSources: chunks => update({ retrieved_chunks: chunks }),
        onToken: token => { accumulated += token; update({ content: accumulated }); },
        onDone: summary => { finished = true; update({ status: 'success', model_used: summary.model_used }); },
        onError: () => fail(),
      }, controller.signal);
      if (!finished && !failed && !controller.signal.aborted) fail();
    } catch { if (!failed && !controller.signal.aborted) fail(); }
    finally {
      if (controller.signal.aborted && mountedRef.current) {
        update({ status: 'error' });
        setNotice('Recebimento interrompido. O conteúdo foi mantido. O processamento pode continuar no servidor; interromper a leitura não cancela a geração.');
        setInput(current => current || text);
      }
      busyRef.current = false; controllerRef.current = null;
      if (mountedRef.current) { setIsStreaming(false); loadSessions(); textareaRef.current?.focus(); }
    }
  };
  const history = <div className="history-content"><div className="history-heading"><h2>Suas conversas</h2><button className="icon-button" onClick={loadSessions} disabled={sessionsLoading} aria-label="Atualizar histórico"><RefreshCw size={15} className={sessionsLoading ? 'animate-spin' : ''} /></button></div>
    <button className="new-conversation" disabled={isStreaming || messagesLoading} onClick={newConversation}><Plus size={17} />Nova conversa</button>
    {sessions.length > 0 && <input className="history-search" aria-label="Buscar conversas" placeholder="Buscar no histórico…" value={search} onChange={e => setSearch(e.target.value)} />}
    {sessionsError && <p role="status" className="history-error">{sessionsError} Use o botão de atualizar para tentar novamente.</p>}
    <div className="session-list" aria-busy={sessionsLoading}>{!sessions.length && !sessionsLoading && !sessionsError && <div className="history-empty"><MessageSquare size={22} /><p>Seu conhecimento,<br />uma conversa de cada vez.</p><small>As conversas salvas aparecerão aqui.</small></div>}
      {sessionsLoading && !sessions.length && <p className="muted">Carregando conversas…</p>}
      {sessions.filter(s => (s.title || 'Sem título').toLocaleLowerCase().includes(search.toLocaleLowerCase())).map(s => <div className={`session-row ${s.id === activeSessionId ? 'selected' : ''}`} key={s.id}><button className="session-select" disabled={isStreaming || messagesLoading} aria-current={s.id === activeSessionId ? 'true' : undefined} onClick={() => selectSession(s.id)}><MessageSquare size={14} /><span>{s.title || 'Sem título'}</span></button><button className="icon-button delete-session" disabled={isStreaming || messagesLoading} onClick={() => setDeleteId(s.id)} aria-label={`Excluir conversa ${s.title || 'Sem título'}`}><Trash2 size={14} /></button></div>)}
      {sessions.length > 0 && !sessions.some(s => (s.title || 'Sem título').toLocaleLowerCase().includes(search.toLocaleLowerCase())) && <p className="muted">Nenhuma conversa corresponde à busca.</p>}
    </div><div className="history-context"><BookOpen size={16} /><div>Contexto ativo<strong>{domain}</strong></div></div></div>;
  return <div className="chat-workspace">
    <aside className="conversation-sidebar">{history}</aside>
    <Dialog open={historyOpen} onClose={() => setHistoryOpen(false)} title="Histórico de conversas" className="history-dialog"><button className="icon-button close-nav" onClick={() => setHistoryOpen(false)} aria-label="Fechar histórico"><X size={20} /></button>{history}</Dialog>
    <Dialog open={!!deleteId} onClose={() => { if (!deleting) setDeleteId(null); }} title="Excluir conversa"><div className="dialog-heading"><h2>Excluir esta conversa?</h2></div><p className="dialog-description">O histórico desta conversa será removido permanentemente. Esta ação não pode ser desfeita.</p><div className="dialog-actions"><button className="text-button" disabled={deleting} onClick={() => setDeleteId(null)}>Cancelar</button><button className="danger-button" disabled={deleting} onClick={removeSession}>{deleting ? 'Excluindo…' : 'Excluir conversa'}</button></div></Dialog>
    <section className="conversation-main" aria-label="Conversa com o assistente">
      <div className="conversation-toolbar"><div><span className="context-dot" />{domain}<span className="toolbar-separator">/</span><span className="conversation-title">{sessions.find(s => s.id === activeSessionId)?.title || 'Nova conversa'}</span></div><button className="text-button mobile-history" onClick={() => setHistoryOpen(true)}><History size={17} /><span>Histórico</span></button></div>
      <div ref={scrollRef} className="message-scroll" onScroll={() => { const el = scrollRef.current; if (el) { const near = el.scrollHeight - el.scrollTop - el.clientHeight < 90; followRef.current = near; setAtBottom(near); } }}>
        {messagesLoading && <div className="loading-history" role="status"><Loader2 size={17} className="animate-spin" />Abrindo conversa…</div>}
        {!messages.length ? <div className="chat-welcome"><div className="welcome-symbol"><Sparkles size={31} /></div><p className="eyebrow">CONVERSE. EXPLORE. CONSTRUA.</p><h1>Da pergunta ao<br /><span>próximo passo.</span></h1><p className="welcome-description">Explore {domain} com o apoio da sua base de conhecimento. Consulte os trechos recuperados e confira o contexto de cada resposta.</p><div className="suggestions">{[`Como organizar um projeto em ${domain}?`, `Explique o ciclo de vida de ${activeDomainId === 'ue5' ? 'um Actor' : 'um nó'}.`, 'Como investigar um problema de desempenho?'].map((prompt, i) => <button key={prompt} onClick={() => { setInput(prompt); textareaRef.current?.focus(); }}><span className="suggestion-index">0{i + 1}</span><span>{prompt}</span><ArrowUp size={16} /></button>)}</div><p className="welcome-note"><BookOpen size={14} />A qualidade da resposta depende dos documentos e do modelo disponíveis.</p></div> : <div className="message-list">{messages.map(msg => <article key={msg.id} className={`message message-${msg.role}`}><div className="message-avatar">{msg.role === 'assistant' ? <Sparkles size={17} /> : 'V'}</div><div className="message-body"><div className="message-heading"><strong>{msg.role === 'assistant' ? 'Any Assistant' : 'Você'}</strong>{msg.status === 'streaming' && <span className="generating"><Loader2 size={12} className="animate-spin" />Respondendo</span>}{msg.status === 'error' && <span className="message-error">Resposta interrompida</span>}</div>{msg.role === 'user' ? <p className="user-text">{msg.content}</p> : <><MarkdownMessage content={msg.content} />{!msg.content && msg.status === 'streaming' && <p className="thinking">Consultando o contexto e preparando a resposta…</p>}{msg.status !== 'streaming' && <><SourcesPanel chunks={msg.retrieved_chunks || []} /><div className="response-footer">{msg.content && <CopyResponse content={msg.content} />}{msg.model_used && <span className="response-model">{msg.model_used}</span>}</div></>}</>}</div></article>)}</div>}
      </div>
      {!atBottom && messages.length > 0 && <button className="jump-to-bottom" onClick={() => { followRef.current = true; setAtBottom(true); scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight }); }}><ArrowDown size={15} />Ir para o final</button>}
      <div className="composer-area">{notice && <div className="inline-error chat-notice" role="alert">{notice}<button className="icon-button" aria-label="Dispensar aviso" onClick={() => setNotice('')}><X size={15} /></button></div>}
        {modelsError && <div className="model-warning" role="status">{modelsError}<button className="text-button" onClick={loadModels} disabled={modelsLoading}>Tentar novamente</button></div>}
        <div className="composer"><label htmlFor="chat-input" className="sr-only">Sua mensagem</label><textarea id="chat-input" ref={textareaRef} rows={2} value={input} onChange={e => setInput(e.target.value)} onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); send(); } }} placeholder={`O que você quer explorar em ${domain}?`} />
          <div className="composer-controls"><div className="composer-model" title={settings.model || 'Nenhum modelo disponível'}><span className="context-dot" />{modelsLoading ? 'Carregando modelos…' : settings.model || 'Modelo indisponível'}</div><ChatSettingsPopover settings={settings} onChange={setSettings} models={models} loading={modelsLoading} error={modelsError} onRetry={loadModels} disabled={isStreaming} />{isStreaming ? <button className="send-button" onClick={() => controllerRef.current?.abort()} aria-label="Interromper recebimento" title="Interromper leitura da resposta. O processamento pode continuar no servidor."><Square size={15} /></button> : <button className="send-button" disabled={!input.trim() || messagesLoading || !settings.model} onClick={send} aria-label="Enviar mensagem" title={!settings.model ? 'Configure um modelo disponível' : 'Enviar mensagem'}><ArrowUp size={20} /></button>}</div>
        </div><div className="composer-caption"><span>IA pode cometer erros. Confira as fontes.</span><span>Enter para enviar <span>·</span> Shift + Enter para nova linha</span></div><span className="sr-only" role="status">{isStreaming ? 'Resposta em andamento' : messages.length ? 'Pronto para sua próxima mensagem' : ''}</span>
      </div>
    </section>
  </div>;
}
