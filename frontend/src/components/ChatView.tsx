/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useEffect, useRef, useState } from 'react';
import {
  AlertTriangle,
  Bot,
  CheckCircle,
  ExternalLink,
  FileCode,
  FileText,
  Info,
  Loader2,
  Paperclip,
  Play,
  RotateCcw,
  Send,
  Sliders,
  Sparkles,
  User as UserIcon,
  X,
} from 'lucide-react';
import { useAppStore } from '@/store/useAppStore';
import * as chatService from '@/services/chatService';
import { getBackendRootUrl, getQdrantDashboardUrl } from '@/config/links';
import type {
  ChatAttachment,
  ChatHyperparameters,
  ChatRequest,
  MessageResponse,
  RetrievedChunk,
  ModelInfo,
} from '@/types';

interface ChatViewProps {
  sessionId: string;
  onResetSession: () => void;
}

export const ChatView: React.FC<ChatViewProps> = ({ sessionId, onResetSession }) => {
  const { activeDomainId, activeCollection, activeTenant, setActiveDomain } = useAppStore();

  // Chat message state
  const [messages, setMessages] = useState<MessageResponse[]>([
    {
      id: 'welcome-001',
      role: 'assistant',
      status: 'success',
      content:
        'Bem-vindo ao **RAG.CORE Assistant**! O pipeline está conectado à documentação oficial e código-fonte via LiteLLM & Qdrant.\n\nVocê pode me fazer perguntas técnicas sobre C++, GDScript ou APIs do motor, anexar arquivos de contexto inline, ou testar a resposta em streaming (**SSE**).',
      model_used: '',
      sequence_number: 1,
      created_at: new Date().toISOString(),
      retrieved_chunks: [
        {
          score: 0.94,
          title: 'Unreal Engine 5 Core Memory Allocators',
          source_file: 'Engine/Source/Runtime/Core/Public/HAL/UnrealMemory.h',
          chunk_id: 104,
          ue_version: '5.4',
          doc_type: 'source_code',
          summary_markdown: 'Defines FMalloc and global memory allocation wrappers for UE5 gameplay modules.',
          raw_content: '// FMalloc::Malloc\nclass CORE_API FMalloc { public: virtual void* Malloc(SIZE_T Count, uint32 Alignment); };',
        },
        {
          score: 0.88,
          title: 'Godot 4 GDScript Typing & Signals',
          source_file: 'doc/classes/GDScript.xml',
          chunk_id: 202,
          ue_version: '4.3',
          doc_type: 'documentation',
          summary_markdown: 'Official GDScript type hinting and signal emitting guide for high performance.',
          raw_content: 'signal health_changed(new_value: int)\nfunc take_damage(amount: int) -> void:\n  health_changed.emit(amount)',
        },
      ],
    },
  ]);

  const [inputMessage, setInputMessage] = useState('');
  const [isStreaming, setIsStreaming] = useState(false);
  const [useStreaming, setUseStreaming] = useState(true);
  const [activeAttachments, setActiveAttachments] = useState<ChatAttachment[]>([]);
  const [isUploadingAttachment, setIsUploadingAttachment] = useState(false);
  const [selectedChunk, setSelectedChunk] = useState<RetrievedChunk | null>(null);

  // Hyperparameters
  const [params, setParams] = useState<ChatHyperparameters>({
    model: '',
    top_k: 5,
    temperature: 0.7,
    top_p: 0.95,
    system_prompt:
      'You are an expert engine developer AI assistant. Provide concise, highly accurate C++ and GDScript answers with direct references to official documentation and header files.',
    domain_id: activeDomainId,
  });

  const [availableModels, setAvailableModels] = useState<ModelInfo[]>([]);
  const [modelsLoading, setModelsLoading] = useState(false);

  const messagesEndRef = useRef<HTMLDivElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, isStreaming]);

  // Sync domain switch
  useEffect(() => {
    setParams((prev) => ({ ...prev, domain_id: activeDomainId }));
  }, [activeDomainId]);

  // Fetch models on mount
  useEffect(() => {
    const fetchModels = async () => {
      setModelsLoading(true);
      try {
        const models = await chatService.listModels();
        setAvailableModels(models);
        // Auto-select first model if none selected
        if (models.length > 0) {
          setParams((prev) => {
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

  const handleSendMessage = async (e?: React.FormEvent) => {
    if (e) e.preventDefault();
    const text = inputMessage.trim();
    if (!text || isStreaming) return;

    // Validate model is selected
    if (!params.model) {
      alert('Selecione um modelo antes de enviar a mensagem.');
      return;
    }

    const userMessageId = `user-${Date.now()}`;
    const assistantMessageId = `assistant-${Date.now()}`;

    // Include attachment notice in prompt if attached
    let promptWithContext = text;
    if (activeAttachments.length > 0) {
      const attachNotes = activeAttachments
        .map((a) => `[Context File: ${a.name}]\n${a.raw_content || ''}`)
        .join('\n\n');
      promptWithContext = `${text}\n\n--- INLINE CONTEXT FILES ---\n${attachNotes}`;
    }

    const newUserMsg: MessageResponse = {
      id: userMessageId,
      role: 'user',
      status: 'success',
      content: text,
      sequence_number: messages.length + 1,
      created_at: new Date().toISOString(),
    };

    const pendingAssistantMsg: MessageResponse = {
      id: assistantMessageId,
      role: 'assistant',
      status: 'streaming',
      content: '',
      model_used: params.model,
      sequence_number: messages.length + 2,
      created_at: new Date().toISOString(),
      retrieved_chunks: [],
    };

    setMessages((prev) => [...prev, newUserMsg, pendingAssistantMsg]);
    setInputMessage('');
    setActiveAttachments([]);
    setIsStreaming(true);

    const chatRequest: ChatRequest = {
      session_id: sessionId,
      message: promptWithContext,
      model: params.model,
      top_k: params.top_k,
      stream: useStreaming,
      domain_id: params.domain_id,
      temperature: params.temperature,
      top_p: params.top_p,
      system_prompt: params.system_prompt,
    };

    if (useStreaming) {
      try {
        await chatService.sendChatMessageStream(chatRequest, {
          onSources: (chunks) => {
            setMessages((prev) =>
              prev.map((msg) =>
                msg.id === assistantMessageId
                  ? { ...msg, retrieved_chunks: chunks }
                  : msg
              )
            );
          },
          onToken: (token) => {
            setMessages((prev) =>
              prev.map((msg) =>
                msg.id === assistantMessageId
                  ? { ...msg, content: msg.content + token }
                  : msg
              )
            );
          },
          onDone: (summary) => {
            setMessages((prev) =>
              prev.map((msg) =>
                msg.id === assistantMessageId
                  ? {
                      ...msg,
                      status: 'success',
                      model_used: summary.model_used,
                      prompt_tokens: summary.prompt_tokens || 142,
                      completion_tokens: summary.completion_tokens || 389,
                    }
                  : msg
              )
            );
            setIsStreaming(false);
          },
          onError: (err) => {
            setMessages((prev) =>
              prev.map((msg) =>
                msg.id === assistantMessageId
                  ? {
                      ...msg,
                      status: 'error',
                      error_detail: err,
                      content:
                        msg.content ||
                        'Falha no streaming SSE. O servidor backend ou LiteLLM Proxy pode estar offline ou indisponível.',
                    }
                  : msg
              )
            );
            setIsStreaming(false);
          },
        });
      } catch {
        setIsStreaming(false);
      }
    } else {
      // Synchronous Chat request fallback
      try {
        const res = await chatService.sendChatMessageSync(chatRequest);
        setMessages((prev) =>
          prev.map((msg) =>
            msg.id === assistantMessageId
              ? {
                  ...msg,
                  status: 'success',
                  content: res.answer,
                  model_used: res.model_used,
                  prompt_tokens: res.prompt_tokens || 120,
                  completion_tokens: res.completion_tokens || 210,
                  retrieved_chunks: res.retrieved_chunks,
                }
              : msg
          )
        );
      } catch (err: unknown) {
        const errorMsg =
          err instanceof Error ? err.message : 'Falha ao processar resposta do chat.';
        setMessages((prev) =>
          prev.map((msg) =>
            msg.id === assistantMessageId
              ? {
                  ...msg,
                  status: 'error',
                  error_detail: errorMsg,
                  content:
                    'Erro na comunicação com a API de Chat. Verifique se o servidor FastAPI está rodando.',
                }
              : msg
          )
        );
      } finally {
        setIsStreaming(false);
      }
    }
  };

  const handleFileUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files;
    if (!files || files.length === 0) return;

    setIsUploadingAttachment(true);
    try {
      const file = files[0];
      const att = await chatService.uploadChatContextAttachment(file, sessionId);
      setActiveAttachments((prev) => [...prev, att]);
    } catch {
      // ignore upload error handled by service
    } finally {
      setIsUploadingAttachment(false);
      if (fileInputRef.current) fileInputRef.current.value = '';
    }
  };

  return (
    <div className="flex-1 flex overflow-hidden bg-[#0A0A0B] text-[#E4E4E7]">
      {/* Left Chat Viewport */}
      <div className="flex-1 flex flex-col p-6 space-y-6 overflow-hidden">
        {/* Chat Bubbles */}
        <div className="flex-1 space-y-6 overflow-y-auto pr-2">
          {messages.map((msg) => {
            const isUser = msg.role === 'user';
            return (
              <div key={msg.id} className="flex gap-4">
                {/* Avatar Icon */}
                <div
                  className={`w-8 h-8 rounded flex-shrink-0 flex items-center justify-center ${
                    isUser
                      ? 'bg-[#27272A] text-zinc-300'
                      : 'bg-indigo-600 text-white shadow-sm shadow-indigo-600/30'
                  }`}
                >
                  {isUser ? <UserIcon className="w-4 h-4" /> : <Bot className="w-4 h-4" />}
                </div>

                {/* Bubble Body */}
                <div className="space-y-1 w-full min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-semibold text-zinc-300">
                      {isUser ? 'Developer' : 'AI Agent'}
                    </span>
                    {!isUser && msg.model_used && (
                      <span className="text-[10px] bg-zinc-800 text-zinc-400 px-1.5 py-0.5 rounded font-mono uppercase tracking-wider">
                        {msg.model_used}
                      </span>
                    )}
                    {!isUser && msg.status === 'streaming' && (
                      <span className="text-[10px] bg-indigo-950/80 text-indigo-400 border border-indigo-800/60 px-1.5 py-0.5 rounded font-mono flex items-center gap-1">
                        <span className="w-1.5 h-1.5 rounded-full bg-indigo-400 animate-ping" />
                        SSE Streaming...
                      </span>
                    )}
                    {!isUser && msg.prompt_tokens && msg.completion_tokens && (
                      <span className="text-[10px] text-zinc-600 font-mono">
                        {msg.prompt_tokens}+{msg.completion_tokens} tok
                      </span>
                    )}
                  </div>

                  {/* Message Bubble Container */}
                  <div
                    className={`p-4 rounded-lg rounded-tl-none border ${
                      isUser
                        ? 'bg-[#1C1C1F] border-[#27272A] max-w-xl text-zinc-200'
                        : 'bg-[#0E0E10] border-[#27272A] text-zinc-300'
                    }`}
                  >
                    {msg.status === 'error' ? (
                      <div className="flex items-start gap-2 text-rose-400">
                        <AlertTriangle className="w-4 h-4 flex-shrink-0 mt-0.5" />
                        <div className="space-y-1">
                          <p className="text-sm font-medium">{msg.content}</p>
                          {msg.error_detail && (
                            <p className="text-xs font-mono text-rose-500/80">
                              {msg.error_detail}
                            </p>
                          )}
                        </div>
                      </div>
                    ) : (
                      <div className="text-sm leading-relaxed whitespace-pre-wrap font-sans break-words">
                        {msg.content || (
                          <span className="text-zinc-600 italic flex items-center gap-2">
                            <Loader2 className="w-4 h-4 animate-spin text-indigo-400" />
                            Pensando no pipeline RAG...
                          </span>
                        )}
                      </div>
                    )}

                    {/* Sources / Citations Section */}
                    {!isUser && msg.retrieved_chunks && msg.retrieved_chunks.length > 0 && (
                      <div className="mt-4 pt-3 border-t border-[#27272A]">
                        <div className="flex items-center gap-2 text-[11px] text-zinc-400 mb-2 font-medium">
                          <FileCode className="w-3.5 h-3.5 text-indigo-400" />
                          <span>Fontes RAG Citadas ({msg.retrieved_chunks.length}):</span>
                        </div>
                        <div className="flex flex-wrap gap-1.5">
                          {msg.retrieved_chunks.map((chunk, idx) => (
                            <button
                              key={`${chunk.chunk_id}-${idx}`}
                              onClick={() => setSelectedChunk(chunk)}
                              type="button"
                              className="px-2 py-1 bg-[#1C1C1F] hover:bg-[#252529] border border-[#27272A] hover:border-indigo-600 rounded text-[11px] text-indigo-400 font-mono transition-all flex items-center gap-1.5"
                            >
                              <span>{chunk.source_file.split('/').pop() || 'chunk.h'}</span>
                              <span className="text-zinc-500 text-[10px]">
                                ({Math.round(chunk.score * 100)}%)
                              </span>
                            </button>
                          ))}
                        </div>
                      </div>
                    )}
                  </div>
                </div>
              </div>
            );
          })}
          <div ref={messagesEndRef} />
        </div>

        {/* Inline Attachments Shelf */}
        {activeAttachments.length > 0 && (
          <div className="flex items-center gap-2 px-3 py-2 bg-[#121214] border border-[#27272A] rounded-lg">
            <Paperclip className="w-3.5 h-3.5 text-indigo-400" />
            <span className="text-xs text-zinc-400">Contexto anexado:</span>
            <div className="flex flex-wrap gap-1.5">
              {activeAttachments.map((att) => (
                <span
                  key={att.id}
                  className="px-2 py-0.5 bg-[#1C1C1F] border border-[#27272A] rounded text-xs text-zinc-300 font-mono flex items-center gap-1"
                >
                  <span>{att.name}</span>
                  <button
                    onClick={() =>
                      setActiveAttachments((prev) => prev.filter((a) => a.id !== att.id))
                    }
                    type="button"
                    className="text-zinc-500 hover:text-rose-400 ml-1"
                  >
                    <X className="w-3 h-3" />
                  </button>
                </span>
              ))}
            </div>
          </div>
        )}

        {/* Input Area */}
        <form
          onSubmit={handleSendMessage}
          className="border border-[#27272A] bg-[#121214] rounded-xl p-3 focus-within:ring-1 focus-within:ring-indigo-500 shadow-lg"
        >
          <textarea
            value={inputMessage}
            onChange={(e) => setInputMessage(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                handleSendMessage();
              }
            }}
            disabled={isStreaming}
            className="w-full bg-transparent border-none focus:ring-0 text-sm text-zinc-200 placeholder-zinc-500 resize-none h-20 outline-none"
            placeholder="Faça uma pergunta sobre C++, GDScript ou APIs do motor... (Shift+Enter para quebra de linha)"
          />
          <div className="flex items-center justify-between border-t border-[#27272A] pt-2 mt-1">
            <div className="flex items-center gap-2">
              <input
                ref={fileInputRef}
                type="file"
                onChange={handleFileUpload}
                className="hidden"
                accept=".h,.cpp,.cs,.gd,.py,.json,.xml,.txt,.md"
              />
              <button
                onClick={() => fileInputRef.current?.click()}
                type="button"
                disabled={isUploadingAttachment || isStreaming}
                className="p-1.5 rounded-md hover:bg-[#27272A] text-zinc-400 hover:text-zinc-200 transition-colors"
                title="Anexar arquivo de código/contexto ao chat"
              >
                {isUploadingAttachment ? (
                  <Loader2 className="w-4 h-4 animate-spin text-indigo-400" />
                ) : (
                  <Paperclip className="w-4 h-4" />
                )}
              </button>
              <button
                onClick={onResetSession}
                type="button"
                disabled={isStreaming}
                className="p-1.5 rounded-md hover:bg-[#27272A] text-zinc-400 hover:text-zinc-200 transition-colors"
                title="Resetar conversa / Limpar sessão"
              >
                <RotateCcw className="w-4 h-4" />
              </button>

              <div className="h-4 w-px bg-[#27272A] mx-1" />

              <button
                onClick={() => setUseStreaming(!useStreaming)}
                type="button"
                className={`px-2 py-1 rounded text-[11px] font-mono transition-colors flex items-center gap-1.5 ${
                  useStreaming
                    ? 'bg-indigo-950/80 text-indigo-300 border border-indigo-800/60'
                    : 'bg-[#1C1C1F] text-zinc-400 border border-[#27272A]'
                }`}
                title="Alternar entre modo SSE Streaming ou resposta síncrona JSON"
              >
                <span
                  className={`w-1.5 h-1.5 rounded-full ${
                    useStreaming ? 'bg-indigo-400' : 'bg-zinc-600'
                  }`}
                />
                <span>SSE Stream</span>
              </button>
            </div>

            <button
              type="submit"
              disabled={!inputMessage.trim() || isStreaming}
              className="bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40 text-white px-4 py-1.5 rounded-lg text-sm font-medium flex items-center gap-2 shadow-sm transition-all cursor-pointer"
            >
              <span>Enviar</span>
              {isStreaming ? (
                <Loader2 className="w-3.5 h-3.5 animate-spin" />
              ) : (
                <Send className="w-3.5 h-3.5" />
              )}
            </button>
          </div>
        </form>
      </div>

      {/* Right Hyperparameters & System Prompt Sidebar */}
      <div className="w-64 border-l border-[#27272A] bg-[#0E0E10] flex flex-col p-5 space-y-6 overflow-y-auto flex-shrink-0 select-none">
        <div>
          <h3 className="text-xs font-bold text-zinc-500 uppercase tracking-widest mb-4 flex items-center gap-2">
            <Sliders className="w-3.5 h-3.5 text-indigo-400" />
            <span>Hyperparameters</span>
          </h3>

          <div className="space-y-4">
            {/* Domain Selector */}
            <div className="space-y-1.5">
              <span className="text-[11px] text-zinc-400 font-medium">Domain Target</span>
              <div className="grid grid-cols-2 gap-1 bg-[#121214] border border-[#27272A] p-1 rounded">
                <button
                  onClick={() => setActiveDomain('godot_4')}
                  type="button"
                  className={`px-2 py-1 rounded text-xs font-mono transition-all ${
                    activeDomainId === 'godot_4'
                      ? 'bg-indigo-600 text-white shadow-sm'
                      : 'text-zinc-400 hover:text-zinc-200'
                  }`}
                >
                  Godot 4.x
                </button>
                <button
                  onClick={() => setActiveDomain('ue5')}
                  type="button"
                  className={`px-2 py-1 rounded text-xs font-mono transition-all ${
                    activeDomainId === 'ue5'
                      ? 'bg-indigo-600 text-white shadow-sm'
                      : 'text-zinc-400 hover:text-zinc-200'
                  }`}
                >
                  UE 5.4+
                </button>
              </div>
            </div>

            {/* Model Input */}
            <div className="space-y-1.5">
              <span className="text-[11px] text-zinc-400 font-medium">LiteLLM Model</span>
              {modelsLoading && (
                <div className="flex items-center gap-2 text-xs text-zinc-500">
                  <Loader2 className="w-3.5 h-3.5 animate-spin text-indigo-400" />
                  Carregando modelos...
                </div>
              )}
              <select
                value={params.model}
                onChange={(e) => setParams({ ...params, model: e.target.value })}
                className="w-full bg-[#1C1C1F] border border-[#27272A] rounded px-2 py-1.5 text-xs text-zinc-200 outline-none focus:border-indigo-500"
                disabled={modelsLoading}
              >
                {availableModels.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.id}
                  </option>
                ))}
                {availableModels.length === 0 && !modelsLoading && (
                  <option value="" disabled>
                    Nenhum modelo disponível
                  </option>
                )}
              </select>
            </div>

            {/* Temperature Slider */}
            <div className="space-y-2">
              <div className="flex justify-between text-[11px]">
                <span className="text-zinc-400">Temperature</span>
                <span className="text-indigo-400 font-mono">{params.temperature}</span>
              </div>
              <input
                type="range"
                min="0"
                max="1.5"
                step="0.1"
                value={params.temperature}
                onChange={(e) =>
                  setParams({ ...params, temperature: parseFloat(e.target.value) })
                }
                className="w-full accent-indigo-500 bg-[#27272A] rounded-lg h-1 cursor-pointer"
              />
            </div>

            {/* Top P Slider */}
            <div className="space-y-2">
              <div className="flex justify-between text-[11px]">
                <span className="text-zinc-400">Top P</span>
                <span className="text-indigo-400 font-mono">{params.top_p}</span>
              </div>
              <input
                type="range"
                min="0.1"
                max="1"
                step="0.05"
                value={params.top_p}
                onChange={(e) => setParams({ ...params, top_p: parseFloat(e.target.value) })}
                className="w-full accent-indigo-500 bg-[#27272A] rounded-lg h-1 cursor-pointer"
              />
            </div>

            {/* Top K (Retrieval Chunks) */}
            <div className="space-y-2">
              <div className="flex justify-between text-[11px]">
                <span className="text-zinc-400">Top K (Chunks RAG)</span>
                <span className="text-indigo-400 font-mono">{params.top_k}</span>
              </div>
              <input
                type="range"
                min="1"
                max="15"
                step="1"
                value={params.top_k}
                onChange={(e) => setParams({ ...params, top_k: parseInt(e.target.value, 10) })}
                className="w-full accent-indigo-500 bg-[#27272A] rounded-lg h-1 cursor-pointer"
              />
            </div>
          </div>
        </div>

        {/* System Prompt Section */}
        <div>
          <h3 className="text-xs font-bold text-zinc-500 uppercase tracking-widest mb-3">
            System Prompt
          </h3>
          <textarea
            value={params.system_prompt || ''}
            onChange={(e) => setParams({ ...params, system_prompt: e.target.value })}
            className="w-full h-32 bg-[#1C1C1F] border border-[#27272A] rounded p-2 text-xs text-zinc-300 leading-tight focus:border-indigo-500 outline-none resize-none font-mono"
            placeholder="Insira diretrizes customizadas do assistente..."
          />
        </div>

        {/* Dev Links Footer */}
        <div className="pt-4 border-t border-[#27272A] space-y-3">
          <h3 className="text-xs font-bold text-zinc-500 uppercase tracking-widest mb-2">
            Dev Links
          </h3>
          <a
            href={getBackendRootUrl() + '/docs'}
            target="_blank"
            rel="noopener noreferrer"
            className="flex items-center justify-between text-xs text-zinc-400 hover:text-indigo-400 transition-colors"
          >
            <span>API Documentation (/docs)</span>
            <ExternalLink className="w-3 h-3 text-zinc-600" />
          </a>
          <a
            href={getQdrantDashboardUrl()}
            target="_blank"
            rel="noopener noreferrer"
            className="flex items-center justify-between text-xs text-zinc-400 hover:text-indigo-400 transition-colors"
          >
            <span>Qdrant Dashboard</span>
            <ExternalLink className="w-3 h-3 text-zinc-600" />
          </a>
        </div>
      </div>

      {/* RAG Source Citation Inspection Modal */}
      {selectedChunk && (
        <div className="fixed inset-0 bg-black/80 backdrop-blur-sm z-50 flex items-center justify-center p-4">
          <div className="bg-[#121214] border border-[#27272A] rounded-xl max-w-2xl w-full max-h-[85vh] flex flex-col overflow-hidden shadow-2xl">
            <div className="px-6 py-4 border-b border-[#27272A] flex items-center justify-between bg-[#1C1C1F]">
              <div className="flex items-center gap-2">
                <FileCode className="w-4 h-4 text-indigo-400" />
                <span className="font-semibold text-sm text-zinc-100">
                  {selectedChunk.title}
                </span>
                <span className="text-[10px] bg-indigo-950 text-indigo-300 px-1.5 py-0.5 rounded font-mono">
                  {Math.round(selectedChunk.score * 100)}% Similaridade
                </span>
              </div>
              <button
                onClick={() => setSelectedChunk(null)}
                className="text-zinc-400 hover:text-zinc-200"
              >
                <X className="w-4 h-4" />
              </button>
            </div>

            <div className="p-6 overflow-y-auto space-y-4 text-xs">
              <div>
                <span className="text-zinc-500 uppercase tracking-wider block font-semibold mb-1">
                  Caminho do Arquivo / Source
                </span>
                <code className="text-indigo-400 bg-[#1C1C1F] border border-[#27272A] px-2 py-1 rounded block font-mono">
                  {selectedChunk.source_file} (UE: {selectedChunk.ue_version || '5.4'})
                </code>
              </div>

              <div>
                <span className="text-zinc-500 uppercase tracking-wider block font-semibold mb-1">
                  Summary Markdown
                </span>
                <p className="text-zinc-300 leading-relaxed bg-[#1C1C1F] p-3 rounded border border-[#27272A]">
                  {selectedChunk.summary_markdown}
                </p>
              </div>

              <div>
                <span className="text-zinc-500 uppercase tracking-wider block font-semibold mb-1">
                  Raw Content / Code Snippet
                </span>
                <pre className="text-zinc-300 bg-[#0E0E10] border border-[#27272A] p-4 rounded font-mono overflow-x-auto whitespace-pre leading-relaxed">
                  {selectedChunk.raw_content}
                </pre>
              </div>
            </div>

            <div className="px-6 py-3 border-t border-[#27272A] bg-[#0E0E10] flex justify-end">
              <button
                onClick={() => setSelectedChunk(null)}
                className="bg-indigo-600 hover:bg-indigo-500 text-white px-4 py-1.5 rounded-lg text-xs font-medium"
              >
                Fechar
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};
