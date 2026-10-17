import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { MarkdownMessage } from '../src/components/chat/MarkdownMessage';

const render = (content: string) => renderToStaticMarkup(<MarkdownMessage content={content} />);
test('escapes raw HTML and rejects executable link schemes', () => {
  const html = render('<script>alert(1)</script> [unsafe](javascript:alert) [safe](https://example.org/docs)');
  assert.ok(html.includes('&lt;script&gt;'));
  assert.ok(!html.includes('href="javascript:'));
  assert.ok(html.includes('href="https://example.org/docs"'));
  assert.ok(html.includes('rel="noopener noreferrer"'));
});
test('renders tables with accessible headers, preserves code literally and groups lists', () => {
  const html = render('| API | Uso |\n| --- | --- |\n| `Node` | Ciclo de vida |\n\n1. Primeiro\n2. Segundo\n\n```gdscript\nif a < b:\n  print("teste")\n```');
  assert.ok(html.includes('<table>'));
  assert.ok(html.includes('scope="col"'));
  assert.ok(html.includes('<ol'));
  assert.ok(html.includes('if a &lt; b:'));
  assert.ok(html.includes('Copiar código'));
});
test('renders an unfinished code fence during streaming without executing markup', () => {
  const html = render('```html\n<img src=x onerror=alert(1)>');
  assert.ok(html.includes('&lt;img'));
  assert.ok(!html.includes('<img'));
});
