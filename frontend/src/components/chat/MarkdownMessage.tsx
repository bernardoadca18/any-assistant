/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 *
 * MarkdownMessage — renderizador leve de Markdown para mensagens do chat.
 *
 * O package.json da Fase 1 não inclui `react-markdown` nem `remark`/`rehype`,
 * então este componente implementa um parser minimalista (sem dependências
 * externas) suficiente para: parágrafos, blocos de código com linguagem
 * (```lang), código inline (`code`), negrito (**bold**), itálico (*it*),
 * listas (- item / 1. item) e headings (#, ##, ###).
 *
 * Se o time preferir, basta trocar o corpo de `renderInlineMarkdown` e o
 * parser de blocos por `react-markdown` — a API pública do componente
 * (prop `content`) não muda.
 */

import React, { useState } from 'react';
import { Check, Copy } from 'lucide-react';

interface CodeBlock {
  type: 'code';
  language: string;
  content: string;
}

interface TextBlock {
  type: 'paragraph' | 'heading1' | 'heading2' | 'heading3' | 'list-item' | 'ordered-item';
  content: string;
}

type Block = CodeBlock | TextBlock;

function parseBlocks(markdown: string): Block[] {
  const lines = markdown.replace(/\r\n/g, '\n').split('\n');
  const blocks: Block[] = [];

  let i = 0;
  let paragraphBuffer: string[] = [];

  const flushParagraph = () => {
    if (paragraphBuffer.length > 0) {
      blocks.push({ type: 'paragraph', content: paragraphBuffer.join(' ').trim() });
      paragraphBuffer = [];
    }
  };

  while (i < lines.length) {
    const line = lines[i];

    // Fenced code block ```lang ... ```
    const fenceMatch = line.match(/^```(\w*)\s*$/);
    if (fenceMatch) {
      flushParagraph();
      const language = fenceMatch[1] || 'text';
      const codeLines: string[] = [];
      i += 1;
      while (i < lines.length && !lines[i].match(/^```\s*$/)) {
        codeLines.push(lines[i]);
        i += 1;
      }
      blocks.push({ type: 'code', language, content: codeLines.join('\n') });
      i += 1; // skip closing fence
      continue;
    }

    if (/^###\s+/.test(line)) {
      flushParagraph();
      blocks.push({ type: 'heading3', content: line.replace(/^###\s+/, '') });
      i += 1;
      continue;
    }
    if (/^##\s+/.test(line)) {
      flushParagraph();
      blocks.push({ type: 'heading2', content: line.replace(/^##\s+/, '') });
      i += 1;
      continue;
    }
    if (/^#\s+/.test(line)) {
      flushParagraph();
      blocks.push({ type: 'heading1', content: line.replace(/^#\s+/, '') });
      i += 1;
      continue;
    }

    if (/^\s*[-*]\s+/.test(line)) {
      flushParagraph();
      blocks.push({ type: 'list-item', content: line.replace(/^\s*[-*]\s+/, '') });
      i += 1;
      continue;
    }

    if (/^\s*\d+\.\s+/.test(line)) {
      flushParagraph();
      blocks.push({ type: 'ordered-item', content: line.replace(/^\s*\d+\.\s+/, '') });
      i += 1;
      continue;
    }

    if (line.trim() === '') {
      flushParagraph();
      i += 1;
      continue;
    }

    paragraphBuffer.push(line.trim());
    i += 1;
  }

  flushParagraph();
  return blocks;
}

/** Renderiza negrito, itálico e código inline dentro de um texto simples. */
function renderInlineMarkdown(text: string, keyPrefix: string): React.ReactNode[] {
  const nodes: React.ReactNode[] = [];
  const regex = /(\*\*(.+?)\*\*|`([^`]+?)`|\*(.+?)\*)/g;
  let lastIndex = 0;
  let match: RegExpExecArray | null;
  let idx = 0;

  while ((match = regex.exec(text)) !== null) {
    if (match.index > lastIndex) {
      nodes.push(text.slice(lastIndex, match.index));
    }
    if (match[2] !== undefined) {
      nodes.push(
        <strong key={`${keyPrefix}-b-${idx}`} className="font-semibold text-zinc-100">
          {match[2]}
        </strong>
      );
    } else if (match[3] !== undefined) {
      nodes.push(
        <code
          key={`${keyPrefix}-c-${idx}`}
          className="px-1.5 py-0.5 rounded bg-[#1C1C1F] border border-[#27272A] text-[0.85em] font-mono text-indigo-300"
        >
          {match[3]}
        </code>
      );
    } else if (match[4] !== undefined) {
      nodes.push(
        <em key={`${keyPrefix}-i-${idx}`} className="italic text-zinc-200">
          {match[4]}
        </em>
      );
    }
    lastIndex = regex.lastIndex;
    idx += 1;
  }
  if (lastIndex < text.length) {
    nodes.push(text.slice(lastIndex));
  }
  return nodes;
}

const CopyableCodeBlock: React.FC<{ language: string; content: string }> = ({ language, content }) => {
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(content);
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch {
      // Clipboard indisponível (ex: contexto não seguro) — falha silenciosa
    }
  };

  return (
    <div className="my-2 rounded-lg border border-[#27272A] bg-[#0E0E10] overflow-hidden">
      <div className="flex items-center justify-between px-3 py-1.5 bg-[#141416] border-b border-[#27272A]">
        <span className="text-[11px] font-mono text-zinc-500 uppercase tracking-wide">{language}</span>
        <button
          type="button"
          onClick={handleCopy}
          className="flex items-center gap-1 text-[11px] text-zinc-400 hover:text-zinc-200 transition-colors"
        >
          {copied ? (
            <>
              <Check className="w-3 h-3 text-emerald-400" />
              <span className="text-emerald-400">Copiado</span>
            </>
          ) : (
            <>
              <Copy className="w-3 h-3" />
              <span>Copiar código</span>
            </>
          )}
        </button>
      </div>
      <pre className="p-3 overflow-x-auto text-xs leading-relaxed font-mono text-zinc-200">
        <code>{content}</code>
      </pre>
    </div>
  );
};

interface MarkdownMessageProps {
  content: string;
  className?: string;
}

export const MarkdownMessage: React.FC<MarkdownMessageProps> = ({ content, className = '' }) => {
  const blocks = parseBlocks(content);

  // Agrupa list-items consecutivos em <ul>/<ol>
  const rendered: React.ReactNode[] = [];
  let listBuffer: TextBlock[] = [];
  let listType: 'list-item' | 'ordered-item' | null = null;

  const flushList = (key: string) => {
    if (listBuffer.length === 0) return;
    const Tag = listType === 'ordered-item' ? 'ol' : 'ul';
    rendered.push(
      <Tag key={key} className={`my-1.5 pl-5 space-y-1 text-sm text-zinc-300 ${Tag === 'ol' ? 'list-decimal' : 'list-disc'}`}>
        {listBuffer.map((item, idx) => (
          <li key={idx}>{renderInlineMarkdown(item.content, `${key}-li-${idx}`)}</li>
        ))}
      </Tag>
    );
    listBuffer = [];
    listType = null;
  };

  blocks.forEach((block, i) => {
    if (block.type === 'code') {
      flushList(`list-${i}`);
      rendered.push(<CopyableCodeBlock key={`code-${i}`} language={block.language} content={block.content} />);
      return;
    }
    if (block.type === 'list-item' || block.type === 'ordered-item') {
      if (listType && listType !== block.type) {
        flushList(`list-${i}`);
      }
      listType = block.type;
      listBuffer.push(block);
      return;
    }
    flushList(`list-${i}`);

    if (block.type === 'heading1') {
      rendered.push(
        <h1 key={i} className="text-base font-bold text-zinc-100 mt-2 mb-1">
          {renderInlineMarkdown(block.content, `h1-${i}`)}
        </h1>
      );
    } else if (block.type === 'heading2') {
      rendered.push(
        <h2 key={i} className="text-sm font-bold text-zinc-100 mt-2 mb-1">
          {renderInlineMarkdown(block.content, `h2-${i}`)}
        </h2>
      );
    } else if (block.type === 'heading3') {
      rendered.push(
        <h3 key={i} className="text-sm font-semibold text-zinc-200 mt-2 mb-1">
          {renderInlineMarkdown(block.content, `h3-${i}`)}
        </h3>
      );
    } else {
      rendered.push(
        <p key={i} className="text-sm leading-relaxed text-zinc-300 my-1">
          {renderInlineMarkdown(block.content, `p-${i}`)}
        </p>
      );
    }
  });
  flushList('list-end');

  return <div className={className}>{rendered}</div>;
};
