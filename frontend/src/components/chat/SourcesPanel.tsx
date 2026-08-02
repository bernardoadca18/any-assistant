/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 *
 * SourcesPanel — accordion de fontes citadas (RAG retrieved_chunks).
 * Cada mensagem do assistente pode expandir para ver score, source_file,
 * summary_markdown e raw_content de cada chunk recuperado do Qdrant.
 */

import React, { useState } from 'react';
import { ChevronDown, ChevronRight, Code2, FileText, Layers, Target } from 'lucide-react';
import type { RetrievedChunk } from '@/types';

interface SourcesPanelProps {
  chunks: RetrievedChunk[];
}

function scoreColor(score: number): string {
  if (score >= 0.8) return 'text-emerald-400 bg-emerald-950/40 border-emerald-800/50';
  if (score >= 0.6) return 'text-amber-400 bg-amber-950/40 border-amber-800/50';
  return 'text-zinc-400 bg-zinc-900/40 border-zinc-700/50';
}

const ChunkItem: React.FC<{ chunk: RetrievedChunk; index: number }> = ({ chunk, index }) => {
  const [expanded, setExpanded] = useState(false);

  return (
    <div className="border border-[#27272A] rounded-lg overflow-hidden bg-[#0E0E10]">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="w-full flex items-center gap-2 px-3 py-2 hover:bg-[#141416] transition-colors text-left"
      >
        {expanded ? (
          <ChevronDown className="w-3.5 h-3.5 text-zinc-500 shrink-0" />
        ) : (
          <ChevronRight className="w-3.5 h-3.5 text-zinc-500 shrink-0" />
        )}
        <span className="text-[10px] font-mono text-zinc-600 shrink-0">#{index + 1}</span>
        <FileText className="w-3.5 h-3.5 text-zinc-500 shrink-0" />
        <span className="text-xs text-zinc-300 truncate flex-1 font-mono">
          {chunk.source_file || chunk.title}
        </span>
        <span className={`shrink-0 flex items-center gap-1 text-[10px] font-mono px-1.5 py-0.5 rounded border ${scoreColor(chunk.score)}`}>
          <Target className="w-2.5 h-2.5" />
          {chunk.score.toFixed(3)}
        </span>
      </button>

      {expanded && (
        <div className="px-3 pb-3 pt-1 space-y-2.5 border-t border-[#1F1F22]">
          <div className="flex flex-wrap gap-1.5 pt-2">
            {chunk.title && chunk.title !== 'Sem título' && (
              <span className="text-[10px] px-2 py-0.5 rounded-full bg-[#1C1C1F] border border-[#27272A] text-zinc-400">
                {chunk.title}
              </span>
            )}
            {chunk.project_source && (
              <span className="text-[10px] px-2 py-0.5 rounded-full bg-indigo-950/40 border border-indigo-800/40 text-indigo-300 flex items-center gap-1">
                <Layers className="w-2.5 h-2.5" />
                {chunk.project_source}
              </span>
            )}
            {chunk.doc_type && (
              <span className="text-[10px] px-2 py-0.5 rounded-full bg-[#1C1C1F] border border-[#27272A] text-zinc-400">
                {chunk.doc_type}
              </span>
            )}
            {chunk.ue_version && (
              <span className="text-[10px] px-2 py-0.5 rounded-full bg-[#1C1C1F] border border-[#27272A] text-zinc-400">
                v{chunk.ue_version}
              </span>
            )}
            <span className="text-[10px] px-2 py-0.5 rounded-full bg-[#1C1C1F] border border-[#27272A] text-zinc-500">
              chunk #{chunk.chunk_id}
            </span>
          </div>

          {chunk.summary_markdown && (
            <div>
              <div className="text-[10px] font-semibold uppercase tracking-wider text-zinc-600 mb-1">
                Resumo
              </div>
              <p className="text-xs text-zinc-400 leading-relaxed whitespace-pre-wrap">
                {chunk.summary_markdown}
              </p>
            </div>
          )}

          {chunk.raw_content && (
            <div>
              <div className="text-[10px] font-semibold uppercase tracking-wider text-zinc-600 mb-1 flex items-center gap-1">
                <Code2 className="w-3 h-3" />
                Conteúdo bruto
              </div>
              <pre className="text-[11px] font-mono text-zinc-300 bg-[#0A0A0B] border border-[#1F1F22] rounded-md p-2.5 overflow-x-auto max-h-56 overflow-y-auto whitespace-pre-wrap">
                {chunk.raw_content}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
};

export const SourcesPanel: React.FC<SourcesPanelProps> = ({ chunks }) => {
  const [open, setOpen] = useState(false);

  if (!chunks || chunks.length === 0) return null;

  return (
    <div className="mt-2">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-1.5 text-[11px] font-medium text-zinc-500 hover:text-zinc-300 transition-colors"
      >
        {open ? <ChevronDown className="w-3.5 h-3.5" /> : <ChevronRight className="w-3.5 h-3.5" />}
        <Layers className="w-3.5 h-3.5" />
        <span>
          {chunks.length} fonte{chunks.length > 1 ? 's' : ''} citada{chunks.length > 1 ? 's' : ''}
        </span>
      </button>

      {open && (
        <div className="mt-2 space-y-1.5">
          {chunks.map((chunk, i) => (
            <ChunkItem key={`${chunk.chunk_id}-${i}`} chunk={chunk} index={i} />
          ))}
        </div>
      )}
    </div>
  );
};
