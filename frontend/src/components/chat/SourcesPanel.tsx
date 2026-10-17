import React from 'react';
import { BookOpen, ChevronDown, FileText } from 'lucide-react';
import { MarkdownMessage } from './MarkdownMessage';
import type { RetrievedChunk } from '@/types';
export function SourcesPanel({ chunks }: { chunks: RetrievedChunk[] }) {
  if (!chunks.length) return <p className="no-evidence"><BookOpen size={14} />Nenhuma fonte recuperada nesta resposta. Confira as informações antes de utilizá-las.</p>;
  return <details className="sources-panel"><summary><BookOpen size={16} /><span>{chunks.length} {chunks.length === 1 ? 'fonte recuperada' : 'fontes recuperadas'}</span><span className="source-hint">Conferir evidências</span><ChevronDown size={15} /></summary>
    <div className="sources-body"><p className="sources-disclaimer">Trechos retornados pela busca. A recuperação não garante que todas as afirmações da resposta estejam fundamentadas.</p>
      {chunks.map((chunk, index) => <details className="source-item" key={`${chunk.chunk_id}-${index}`}><summary><span className="source-number">{index + 1}</span><FileText size={17} /><span><strong>{chunk.title || chunk.source_file || 'Referência sem título'}</strong><small>{chunk.source_file || 'Caminho da fonte não informado'}</small></span><ChevronDown size={15} /></summary>
        <div className="source-content"><div className="source-tags">{[chunk.doc_type, chunk.project_source, chunk.ue_version && `Versão ${chunk.ue_version}`].filter(Boolean).map((value, i) => <span key={i}>{value}</span>)}</div>
          {chunk.summary_markdown ? <><h4>Contexto recuperado</h4><MarkdownMessage content={chunk.summary_markdown} /></> : <p>Resumo não fornecido.</p>}
          {chunk.raw_content ? <details className="raw-source"><summary>Ver trecho original</summary><pre tabIndex={0}>{chunk.raw_content}</pre></details> : <p className="muted">Trecho original indisponível.</p>}
          <p className="source-metadata">Trecho #{chunk.chunk_id}{Number.isFinite(chunk.score) ? ` · Pontuação de busca: ${chunk.score.toFixed(3)} (não representa confiança da resposta)` : ''}</p>
        </div></details>)}
    </div></details>;
}
