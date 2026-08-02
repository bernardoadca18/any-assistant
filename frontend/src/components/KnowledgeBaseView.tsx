/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useEffect, useRef, useState } from 'react';
import {
  CheckCircle2,
  Clock,
  Code,
  FileCode,
  FileText,
  FolderUp,
  Loader2,
  Play,
  RefreshCw,
  Terminal,
  UploadCloud,
  XCircle,
} from 'lucide-react';
import { useAppStore } from '@/store/useAppStore';
import * as ingestionService from '@/services/ingestionService';
import type { IngestionJobStatus } from '@/types';

export const KnowledgeBaseView: React.FC = () => {
  const { activeDomainId, activeCollection, activeTenant } = useAppStore();

  const [folderPath, setFolderPath] = useState('/app/source_code/samples');
  const [contentType, setContentType] = useState<'source_code' | 'documentation'>('source_code');
  const [projectSource, setProjectSource] = useState('UE5_Engine');
  const [sampleMode, setSampleMode] = useState(true);
  const [domainId, setDomainId] = useState(activeDomainId);

  const [isSubmittingFolder, setIsSubmittingFolder] = useState(false);
  const [isUploadingFiles, setIsUploadingFiles] = useState(false);
  const [uploadProgress, setUploadProgress] = useState<number | null>(null);
  const [statusMessage, setStatusMessage] = useState<{
    type: 'success' | 'error';
    text: string;
  } | null>(null);

  const [jobs, setJobs] = useState<IngestionJobStatus[]>([]);
  const fileInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    setJobs(ingestionService.getIngestionJobHistory());
  }, []);

  useEffect(() => {
    setDomainId(activeDomainId);
  }, [activeDomainId]);

  const handleProcessFolder = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!folderPath.trim() || isSubmittingFolder) return;

    setIsSubmittingFolder(true);
    setStatusMessage(null);

    try {
      const res = await ingestionService.processFolder({
        folder_path: folderPath.trim(),
        content_type: contentType,
        project_source: projectSource,
        sample_mode: sampleMode,
        domain_id: domainId,
      });

      setStatusMessage({
        type: 'success',
        text: res.message || `Ingestão agendada com sucesso para a pasta '${folderPath}'.`,
      });
      setJobs(ingestionService.getIngestionJobHistory());
    } catch (err: unknown) {
      const message =
        err instanceof Error ? err.message : 'Falha ao agendar ingestão da pasta via API.';
      setStatusMessage({
        type: 'error',
        text: message,
      });
    } finally {
      setIsSubmittingFolder(false);
    }
  };

  const handleMultipartUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files;
    if (!files || files.length === 0) return;

    setIsUploadingFiles(true);
    setUploadProgress(0);
    setStatusMessage(null);

    try {
      const file = files[0];
      const res = await ingestionService.uploadFileMultipart(
        {
          file,
          content_type: contentType,
          project_source: projectSource,
          sample_mode: sampleMode,
          domain_id: domainId,
        },
        (progress) => {
          setUploadProgress(progress);
        }
      );

      setStatusMessage({
        type: 'success',
        text: `Arquivo '${file.name}' indexado/agendado no Qdrant: ${res.message}`,
      });
      setJobs(ingestionService.getIngestionJobHistory());
    } catch (err: unknown) {
      const message =
        err instanceof Error ? err.message : 'Erro no upload multipart/form-data.';
      setStatusMessage({
        type: 'error',
        text: message,
      });
    } finally {
      setIsUploadingFiles(false);
      setUploadProgress(null);
      if (fileInputRef.current) fileInputRef.current.value = '';
    }
  };

  return (
    <div className="flex-1 overflow-y-auto bg-[#0A0A0B] text-[#E4E4E7] p-8 space-y-8 select-none">
      {/* Top Description Box */}
      <div className="max-w-5xl mx-auto space-y-2">
        <div className="flex items-center gap-2 text-indigo-400 text-xs font-mono uppercase tracking-wider">
          <FolderUp className="w-4 h-4" />
          <span>ETL Code-to-Doc // Qdrant Vector DB</span>
        </div>
        <h1 className="text-2xl font-bold tracking-tight text-zinc-100">
          Ingestão de Documentação & Código-Fonte
        </h1>
        <p className="text-sm text-zinc-400 max-w-3xl leading-relaxed">
          O endpoint <code className="text-indigo-400 bg-[#1C1C1F] px-1.5 py-0.5 rounded border border-[#27272A]">POST /v1/ingest/process-folder</code> dispara o pipeline de duplo canal C++/UDN em background com resposta imediata HTTP 202. Com <code className="text-indigo-400">sample_mode=true</code>, o Canal C analisa arquivos <code className="text-zinc-300">.cpp</code> focando na implementação da mecânica.
        </p>
      </div>

      <div className="max-w-5xl mx-auto grid grid-cols-1 lg:grid-cols-3 gap-8">
        {/* Left Column: Process Folder & Multipart Form */}
        <div className="lg:col-span-2 space-y-6">
          {/* Status Alert */}
          {statusMessage && (
            <div
              className={`p-4 rounded-lg border flex items-start gap-3 ${
                statusMessage.type === 'success'
                  ? 'bg-emerald-950/30 border-emerald-800/60 text-emerald-300'
                  : 'bg-rose-950/30 border-rose-800/60 text-rose-300'
              }`}
            >
              {statusMessage.type === 'success' ? (
                <CheckCircle2 className="w-5 h-5 flex-shrink-0 mt-0.5" />
              ) : (
                <XCircle className="w-5 h-5 flex-shrink-0 mt-0.5" />
              )}
              <div className="text-xs leading-relaxed">{statusMessage.text}</div>
            </div>
          )}

          {/* Trigger Container Folder Ingestion */}
          <form
            onSubmit={handleProcessFolder}
            className="bg-[#121214] border border-[#27272A] rounded-xl p-6 space-y-6"
          >
            <div className="flex items-center justify-between border-b border-[#27272A] pb-4">
              <div className="flex items-center gap-2">
                <Terminal className="w-4 h-4 text-indigo-400" />
                <h2 className="text-base font-semibold text-zinc-100">
                  Processamento de Pasta no Container
                </h2>
              </div>
              <span className="text-[10px] font-mono bg-indigo-950 text-indigo-400 px-2 py-0.5 rounded border border-indigo-800/50">
                HTTP 202 Accepted
              </span>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <div className="space-y-1.5 md:col-span-2">
                <label className="text-xs font-semibold text-zinc-300">
                  Caminho da Pasta no Container (folder_path)
                </label>
                <input
                  type="text"
                  value={folderPath}
                  onChange={(e) => setFolderPath(e.target.value)}
                  placeholder="/app/source_code/samples"
                  className="w-full bg-[#1C1C1F] border border-[#27272A] rounded-lg px-3 py-2 text-xs text-zinc-200 font-mono outline-none focus:border-indigo-500"
                  required
                />
              </div>

              <div className="space-y-1.5">
                <label className="text-xs font-semibold text-zinc-300">
                  Tipo de Conteúdo (content_type)
                </label>
                <select
                  value={contentType}
                  onChange={(e) =>
                    setContentType(e.target.value as 'source_code' | 'documentation')
                  }
                  className="w-full bg-[#1C1C1F] border border-[#27272A] rounded-lg px-3 py-2 text-xs text-zinc-200 outline-none focus:border-indigo-500"
                >
                  <option value="source_code">source_code (.h C++, .cpp, .bp.json)</option>
                  <option value="documentation">documentation (.udn, .md)</option>
                </select>
              </div>

              <div className="space-y-1.5">
                <label className="text-xs font-semibold text-zinc-300">
                  Origem do Projeto (project_source)
                </label>
                <input
                  type="text"
                  value={projectSource}
                  onChange={(e) => setProjectSource(e.target.value)}
                  placeholder="Lyra, CitySample, UE5_Engine"
                  className="w-full bg-[#1C1C1F] border border-[#27272A] rounded-lg px-3 py-2 text-xs text-zinc-200 outline-none focus:border-indigo-500 font-mono"
                  required
                />
              </div>

              <div className="space-y-1.5">
                <label className="text-xs font-semibold text-zinc-300">
                  Domain Target (domain_id)
                </label>
                <select
                  value={domainId}
                  onChange={(e) => setDomainId(e.target.value)}
                  className="w-full bg-[#1C1C1F] border border-[#27272A] rounded-lg px-3 py-2 text-xs text-zinc-200 outline-none focus:border-indigo-500 font-mono"
                >
                  <option value="godot_4">godot_4 (Godot Engine)</option>
                  <option value="ue5">ue5 (Unreal Engine 5)</option>
                </select>
              </div>

              <div className="flex items-center pt-5">
                <label className="flex items-center gap-2.5 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={sampleMode}
                    onChange={(e) => setSampleMode(e.target.checked)}
                    className="w-4 h-4 rounded accent-indigo-600 bg-[#1C1C1F] border-[#27272A]"
                  />
                  <div className="flex flex-col">
                    <span className="text-xs font-semibold text-zinc-200">
                      Ativar Sample Mode (Canal C)
                    </span>
                    <span className="text-[10px] text-zinc-400">
                      Varre .cpp da implementação e desativa filtro de palavra-chave
                    </span>
                  </div>
                </label>
              </div>
            </div>

            <div className="pt-2 flex justify-end">
              <button
                type="submit"
                disabled={isSubmittingFolder}
                className="bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-white px-5 py-2 rounded-lg text-xs font-semibold flex items-center gap-2 shadow-md transition-all cursor-pointer"
              >
                {isSubmittingFolder ? (
                  <>
                    <Loader2 className="w-4 h-4 animate-spin" />
                    <span>Disparando ETL no Backend...</span>
                  </>
                ) : (
                  <>
                    <Play className="w-3.5 h-3.5" />
                    <span>Disparar Ingestão (/v1/ingest/process-folder)</span>
                  </>
                )}
              </button>
            </div>
          </form>

          {/* Multipart Upload Card */}
          <div className="bg-[#121214] border border-[#27272A] rounded-xl p-6 space-y-4">
            <div className="flex items-center justify-between border-b border-[#27272A] pb-4">
              <div className="flex items-center gap-2">
                <UploadCloud className="w-4 h-4 text-indigo-400" />
                <h2 className="text-base font-semibold text-zinc-100">
                  Upload Multipart Individual (FormData)
                </h2>
              </div>
              <span className="text-[10px] font-mono bg-zinc-800 text-zinc-400 px-2 py-0.5 rounded">
                Arquivo Único / ZIP
              </span>
            </div>

            <input
              ref={fileInputRef}
              type="file"
              onChange={handleMultipartUpload}
              className="hidden"
              accept=".h,.cpp,.cs,.gd,.py,.json,.udn,.md,.zip"
            />

            <div
              onClick={() => !isUploadingFiles && fileInputRef.current?.click()}
              className="border-2 border-dashed border-[#27272A] hover:border-indigo-500/80 rounded-xl p-8 flex flex-col items-center justify-center cursor-pointer transition-all bg-[#1C1C1F]/40 hover:bg-[#1C1C1F]/80 group text-center"
            >
              {isUploadingFiles ? (
                <div className="space-y-3 w-full max-w-xs">
                  <Loader2 className="w-8 h-8 animate-spin text-indigo-400 mx-auto" />
                  <p className="text-xs font-semibold text-zinc-300">
                    Enviando arquivo ({uploadProgress ?? 0}%)...
                  </p>
                  <div className="w-full h-1.5 bg-[#27272A] rounded-full overflow-hidden">
                    <div
                      className="h-full bg-indigo-500 transition-all duration-300"
                      style={{ width: `${uploadProgress ?? 0}%` }}
                    />
                  </div>
                </div>
              ) : (
                <div className="space-y-2">
                  <UploadCloud className="w-8 h-8 text-zinc-500 group-hover:text-indigo-400 transition-colors mx-auto" />
                  <p className="text-xs font-medium text-zinc-300">
                    Clique para selecionar arquivo para indexação RAG
                  </p>
                  <p className="text-[11px] text-zinc-500">
                    Suporta arquivos C++, GDScript, UDN Markdown ou arquivos individuais
                  </p>
                </div>
              )}
            </div>
          </div>
        </div>

        {/* Right Column: Ingestion Jobs History */}
        <div className="bg-[#121214] border border-[#27272A] rounded-xl p-6 flex flex-col h-full space-y-4">
          <div className="flex items-center justify-between border-b border-[#27272A] pb-4">
            <h3 className="text-sm font-semibold text-zinc-100 flex items-center gap-2">
              <Clock className="w-4 h-4 text-indigo-400" />
              <span>Jobs Ativos & Recentes</span>
            </h3>
            <button
              onClick={() => {
                ingestionService.clearIngestionJobHistory();
                setJobs([]);
              }}
              type="button"
              className="text-[11px] text-zinc-500 hover:text-rose-400 transition-colors"
            >
              Limpar histórico
            </button>
          </div>

          <div className="flex-1 space-y-3 overflow-y-auto max-h-[500px] pr-1">
            {jobs.length === 0 ? (
              <div className="text-center py-12 text-zinc-500 text-xs">
                Nenhum job de ingestão disparado recentemente.
              </div>
            ) : (
              jobs.map((job) => (
                <div
                  key={job.id}
                  className="p-3.5 bg-[#1C1C1F] border border-[#27272A] rounded-lg space-y-2"
                >
                  <div className="flex items-center justify-between">
                    <span className="text-xs font-semibold text-zinc-200 truncate max-w-[160px]">
                      {job.folder_path.split('/').pop() || job.folder_path}
                    </span>
                    <span
                      className={`text-[10px] px-2 py-0.5 rounded font-mono uppercase ${
                        job.status === 'completed'
                          ? 'bg-emerald-950 text-emerald-400 border border-emerald-800'
                          : job.status === 'failed'
                          ? 'bg-rose-950 text-rose-400 border border-rose-800'
                          : 'bg-indigo-950 text-indigo-400 border border-indigo-800 animate-pulse'
                      }`}
                    >
                      {job.status}
                    </span>
                  </div>
                  <div className="text-[11px] text-zinc-400 truncate font-mono">
                    {job.folder_path}
                  </div>
                  <div className="flex items-center justify-between text-[10px] text-zinc-500 font-mono">
                    <span>{job.project_source}</span>
                    <span>{job.domain_id}</span>
                  </div>
                  {job.message && (
                    <div className="text-[11px] text-indigo-400/90 pt-1 border-t border-[#27272A] truncate">
                      {job.message}
                    </div>
                  )}
                </div>
              ))
            )}
          </div>
        </div>
      </div>
    </div>
  );
};
