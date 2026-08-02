/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 *
 * ChatWorkspace — página principal de chat (Fase 2):
 * - Painel esquerdo: histórico de sessões (GET /v1/sessions, DELETE /v1/sessions/{id})
 * - Área central: mensagens com Markdown, blocos de código copiáveis,
 *   accordion de fontes citadas (retrieved_chunks) e streaming SSE
 * - Barra de input com Popover de parâmetros (modelo, top_k, project_source_filter)
 *
 * Estado de mensagens é local a esta view (sessão selecionada). Ao trocar
 * de sessão ou criar uma nova, o histórico é buscado via GET /v1/sessions/{id}.
 */

import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
  Bot,
  Loader2,
  MessageSquarePlus,
  RefreshCw,
  Send,
  Square,
  Trash2,
  User as UserIcon,
} from 'lucide-react';
import { useAppStore } from '@/store/useAppStore';
import * as sessionsService from '@/services/sessionsService';
import { sendChatMessageStream, listModels } from '@/services/chatService';
import { MarkdownMessage } from '@/components/chat/MarkdownMessage';
import { SourcesPanel } from '@/components/chat/SourcesPanel';
import { ChatSettingsPopover, type ChatSettings } from '@/components/chat/ChatSettingsPopover';
import type { MessageResponse, RetrievedChunk, SessionSummaryResponse, ModelInfo } from '@/types';

interface UiMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  status: 'success' | 'error' | 'pending' | 'streaming';
  retrieved_chunks?: RetrievedChunk[];
  model_used?: string | null;
  created_at: string;
}

function newLocalId(): string {
  return `local-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

function generateSessionId(): string {
  return `session-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

function mapMessageResponse(m: MessageResponse): UiMessage {
  return {
    id: m.id,
    role: m.role === 'assistant' ? 'assistant' : 'user',
    content: m.content,
    status: (m.status as UiMessage['status']) || 'success',
    retrieved_chunks: (m.retrieved_chunks as RetrievedChunk[]) || [],
    model_used: m.model_used,
    created_at: m.created_at,
  };
}

export const ChatWorkspace: React.FC = () => {
  const { activeDomainId } = useAppStore();

  const [sessions, setSessions] = useState<SessionSummaryResponse[]>([]);
  const [sessionsLoading, setSessionsLoading] = useState(false);
  const [activeSessionId, setActiveSessionId] = useState<string>(() => generateSessionId());
  const [messages, setMessages] = useState<UiMessage[]>([]);
  const [messagesLoading, setMessagesLoading] = useState(false);

  const [input, setInput] = useState('');
  const [isStreaming, setIsStreaming] = useState(false);
  const abortControllerRef = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  const [chatSettings, setChatSettings] = useState<ChatSettings>({
    model: '',
    top_k: 5,
    project_source_filter: '',
  });

  const [availableModels, setAvailableModels] = useState<ModelInfo[]>([]);
  const [modelsLoading, setModelsLoading] = useState(false);

  const isNewUnsavedSession = useMemo(
    () => !sessions.some((s) => s.id === activeSessionId),
    [sessions, activeSessionId]
  );

  const loadSessions = async () => {
    setSessionsLoading(true);
    try {
      const list = await sessionsService.listSessions();
      setSessions(list);
    } catch {
      // Falha silenciosa: histórico é conveniência, não bloqueia o chat
      setSessions([]);
    } finally {
      setSessionsLoading(false);
    }
  };

  useEffect(() => {
    loadSessions();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Fetch models on mount
  useEffect(() => {
    const fetchModels = async () => {
      setModelsLoading(true);
      try {
        const models = await listModels();
        setAvailableModels(models);
        // Auto-select first model if none selected
        if (models.length > 0) {
          setChatSettings((prev) => {
            if (!prev.model) {
              return { ...prev, model: models[0].id };
            }
            return prev;
          });
        }
      } catch (err) {
        console.error('Failed to fetch models:', err);
      } finally {
        setModelsLoading(false);
      }
    };
    fetchModels();
  }, []);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' });
  }, [messages]);

  const handleSelectSession = async (sessionId: string) => {
    if (isStreaming) return;
    setActiveSessionId(sessionId);
    setMessagesLoading(true);
    try {
      const detail = await sessionsService.getSession(sessionId);
      setMessages(detail.messages.map(mapMessageResponse));
      if (detail.default_model) {
        setChatSettings((s) => ({
          ...s,
          model: detail.default_model,
          project_source_filter: detail.project_source_filter || '',
        }));
      }
    } catch {
      setMessages([]);
    } finally {
      setMessagesLoading(false);
    }
  };

  const handleNewConversation = () => {
    if (isStreaming) return;
    setActiveSessionId(generateSessionId());
    setMessages([]);
  };

  const handleDeleteSession = async (e: React.MouseEvent, sessionId: string) => {
    e.stopPropagation();
    try {
      await sessionsService.deleteSession(sessionId);
      setSessions((prev) => prev.filter((s) => s.id !== sessionId));
      if (activeSessionId === sessionId) {
        handleNewConversation();
      }
    } catch {
      // Mantém a sessão na lista se a remoção falhar no backend
    }
  };

  const handleStop = () => {
    abortControllerRef.current?.abort();
    setIsStreaming(false);
  };

  const handleSend = async () => {
    const trimmed = input.trim();
    if (!trimmed || isStreaming) return;

    // Validate model is selected
    if (!chatSettings.model) {
      alert('Selecione um modelo antes de enviar a mensagem.');
      return;
    }

    const userMessage: UiMessage = {
      id: newLocalId(),
      role: 'user',
      content: trimmed,
      status: 'success',
      created_at: new Date().toISOString(),
    };

    const assistantId = newLocalId();
    const assistantPlaceholder: UiMessage = {
      id: assistantId,
      role: 'assistant',
      content: '',
      status: 'streaming',
      retrieved_chunks: [],
      created_at: new Date().toISOString(),
    };

    setMessages((prev) => [...prev, userMessage, assistantPlaceholder]);
    setInput('');
    setIsStreaming(true);

    const controller = new AbortController();
    abortControllerRef.current = controller;

    let accumulated = '';

    try {
      await sendChatMessageStream(
        {
          session_id: activeSessionId,
          message: trimmed,
          model: chatSettings.model,
          top_k: chatSettings.top_k,
          project_source_filter: chatSettings.project_source_filter || null,
          domain_id: activeDomainId,
          stream: true,
        },
        {
          onSources: (chunks) => {
            setMessages((prev) =>
              prev.map((m) => (m.id === assistantId ? { ...m, retrieved_chunks: chunks } : m))
            );
          },
          onToken: (token) => {
            accumulated += token;
            setMessages((prev) =>
              prev.map((m) => (m.id === assistantId ? { ...m, content: accumulated } : m))
            );
          },
          onDone: (summary) => {
            setMessages((prev) =>
              prev.map((m) =>
                m.id === assistantId
                  ? { ...m, status: 'success', model_used: summary.model_used }
                  : m
              )
            );
          },
          onError: (detail) => {
            setMessages((prev) =>
              prev.map((m) =>
                m.id === assistantId
                  ? { ...m, status: 'error', content: m.content || `Erro: ${detail}` }
                  : m
              )
            );
          },
        },
        controller.signal
      );
    } catch {
      setMessages((prev) =>
        prev.map((m) =>
          m.id === assistantId && m.status === 'streaming'
            ? { ...m, status: 'error', content: m.content || 'Falha na conexão com o assistente.' }
            : m
        )
      );
    } finally {
      setIsStreaming(false);
      abortControllerRef.current = null;
      loadSessions();
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  return (
    <div className="flex w-full h-full overflow-hidden">
      {/* =========================== SESSIONS PANEL =========================== */}
      <aside className="w-64 shrink-0 border-r border-[#1F1F22] bg-[#0B0B0D] flex flex-col overflow-hidden">
        <div className="p-3 border-b border-[#1F1F22]">
          <button
            type="button"
            onClick={handleNewConversation}
            className="w-full flex items-center justify-center gap-2 rounded-lg bg-indigo-600 hover:bg-indigo-500 text-white text-sm font-medium py-2 transition-colors"
          >
            <MessageSquarePlus className="w-4 h-4" />
            Nova Conversa
          </button>
        </div>

        <div className="flex items-center justify-between px-3 py-2">
          <span className="text-[11px] font-semibold uppercase tracking-wider text-zinc-600">
            Sessões
          </span>
          <button
            type="button"
            onClick={loadSessions}
            className="text-zinc-600 hover:text-zinc-300 transition-colors"
            title="Atualizar lista"
          >
            <RefreshCw className={`w-3.5 h-3.5 ${sessionsLoading ? 'animate-spin' : ''}`} />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto px-2 pb-3 space-y-1">
          {isNewUnsavedSession && (
            <div className="flex items-center gap-2 px-2.5 py-2 rounded-lg bg-indigo-600/15 text-indigo-300 text-xs">
              <MessageSquarePlus className="w-3.5 h-3.5 shrink-0" />
              <span className="truncate">Nova conversa (ainda não salva)</span>
            </div>
          )}

          {sessions.length === 0 && !sessionsLoading && !isNewUnsavedSession && (
            <div className="px-2.5 py-4 text-center text-xs text-zinc-600">
              Nenhuma sessão encontrada.
            </div>
          )}

          {sessions.map((session) => {
            const active = session.id === activeSessionId;
            return (
              <button
                key={session.id}
                type="button"
                onClick={() => handleSelectSession(session.id)}
                className={`w-full group flex items-start gap-2 px-2.5 py-2 rounded-lg text-left transition-colors ${
                  active ? 'bg-[#1C1C1F] text-zinc-100' : 'text-zinc-400 hover:bg-[#141416] hover:text-zinc-200'
                }`}
              >
                <div className="flex-1 min-w-0">
                  <div className="text-xs font-medium truncate">{session.title || 'Sem título'}</div>
                  <div className="text-[10px] text-zinc-600 truncate mt-0.5">
                    {session.default_model}
                  </div>
                </div>
                <span
                  role="button"
                  onClick={(e) => handleDeleteSession(e, session.id)}
                  className="opacity-0 group-hover:opacity-100 text-zinc-600 hover:text-rose-400 transition-all shrink-0 p-0.5"
                  title="Excluir sessão"
                >
                  <Trash2 className="w-3.5 h-3.5" />
                </span>
              </button>
            );
          })}
        </div>
      </aside>

      {/* =========================== CHAT AREA =========================== */}
      <div className="flex-1 flex flex-col min-w-0">
        <div ref={scrollRef} className="flex-1 overflow-y-auto px-4 md:px-8 py-6">
          {messagesLoading ? (
            <div className="h-full flex items-center justify-center text-zinc-600 text-sm gap-2">
              <Loader2 className="w-4 h-4 animate-spin" />
              Carregando histórico...
            </div>
          ) : messages.length === 0 ? (
            <div className="h-full flex flex-col items-center justify-center text-center gap-3">
              <div className="w-12 h-12 rounded-2xl bg-indigo-600/15 flex items-center justify-center">
                <Bot className="w-6 h-6 text-indigo-400" />
              </div>
              <div>
                <p className="text-sm font-medium text-zinc-300">Comece uma conversa</p>
                <p className="text-xs text-zinc-600 mt-1">
                  Pergunte algo sobre {activeDomainId === 'ue5' ? 'Unreal Engine 5' : 'Godot 4.x'}.
                </p>
              </div>
            </div>
          ) : (
            <div className="max-w-3xl mx-auto space-y-6">
              {messages.map((msg) => (
                <div key={msg.id} className="flex gap-3">
                  <div
                    className={`w-7 h-7 rounded-lg flex items-center justify-center shrink-0 mt-0.5 ${
                      msg.role === 'user' ? 'bg-[#27272A]' : 'bg-indigo-600/20'
                    }`}
                  >
                    {msg.role === 'user' ? (
                      <UserIcon className="w-3.5 h-3.5 text-zinc-300" />
                    ) : (
                      <Bot className="w-3.5 h-3.5 text-indigo-400" />
                    )}
                  </div>

                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 mb-1">
                      <span className="text-xs font-semibold text-zinc-200">
                        {msg.role === 'user' ? 'Você' : 'Assistente'}
                      </span>
                      {msg.model_used && (
                        <span className="text-[10px] font-mono text-zinc-600">{msg.model_used}</span>
                      )}
                      {msg.status === 'streaming' && (
                        <span className="flex items-center gap-1 text-[10px] text-indigo-400">
                          <Loader2 className="w-2.5 h-2.5 animate-spin" />
                          digitando...
                        </span>
                      )}
                      {msg.status === 'error' && (
                        <span className="text-[10px] text-rose-400">erro</span>
                      )}
                    </div>

                    {msg.role === 'assistant' ? (
                      <>
                        <MarkdownMessage content={msg.content || (msg.status === 'streaming' ? '▍' : '')} />
                        {msg.status !== 'streaming' && msg.retrieved_chunks && msg.retrieved_chunks.length > 0 && (
                          <SourcesPanel chunks={msg.retrieved_chunks} />
                        )}
                      </>
                    ) : (
                      <p className="text-sm leading-relaxed text-zinc-200 whitespace-pre-wrap">
                        {msg.content}
                      </p>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* =========================== INPUT BAR =========================== */}
        <div className="border-t border-[#1F1F22] bg-[#0B0B0D] p-4">
          <div className="max-w-3xl mx-auto">
            <div className="flex items-end gap-2 bg-[#141416] border border-[#27272A] rounded-xl p-2 focus-within:border-indigo-500 transition-colors">
              <textarea
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={handleKeyDown}
                placeholder="Pergunte sobre Blueprints, GDScript, C++, arquitetura de gameplay..."
                rows={1}
                className="flex-1 bg-transparent resize-none outline-none text-sm text-zinc-200 placeholder:text-zinc-600 max-h-40 py-1.5 px-2"
                style={{ minHeight: '2.25rem' }}
              />

              <ChatSettingsPopover settings={chatSettings} onChange={setChatSettings} />

              {isStreaming ? (
                <button
                  type="button"
                  onClick={handleStop}
                  className="flex items-center justify-center w-9 h-9 rounded-lg bg-rose-600 hover:bg-rose-500 text-white transition-colors shrink-0"
                  title="Parar geração"
                >
                  <Square className="w-3.5 h-3.5" />
                </button>
              ) : (
                <button
                  type="button"
                  onClick={handleSend}
                  disabled={!input.trim()}
                  className="flex items-center justify-center w-9 h-9 rounded-lg bg-indigo-600 hover:bg-indigo-500 disabled:bg-[#27272A] disabled:text-zinc-600 text-white transition-colors shrink-0"
                  title="Enviar mensagem"
                >
                  <Send className="w-3.5 h-3.5" />
                </button>
              )}
            </div>
            <p className="text-[10px] text-zinc-700 mt-1.5 text-center">
              top_k={chatSettings.top_k} · modelo={chatSettings.model} · domínio={activeDomainId}
            </p>
          </div>
        </div>
      </div>
    </div>
  );
};
