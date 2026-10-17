// Isolated browser fixtures. No requests reach real APIs or model providers.
// PLAYWRIGHT_MODULE may point to a preinstalled Playwright package; no app dependency is added.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const output = path.resolve(__dirname, '../../evidence');
fs.mkdirSync(output, { recursive: true });
const chunk = { chunk_id: 42, title: 'Ciclo de vida de um nó — fixture de teste', source_file: 'fixtures/node_lifecycle.rst', score: .81, doc_type: 'documentation', project_source: 'Fixture isolada', summary_markdown: 'O exemplo de teste descreve **entrada na árvore**, inicialização e atualização.', raw_content: 'FIXTURE DE TESTE — conteúdo sintético, sem origem em documentos reais.\n_ready()\n_process(delta)' };
const answer = '## Entenda o ciclo de vida\n\nEsta é uma **resposta de teste isolada**, usada para validar a interface.\n\n1. Organize a estrutura de nós.\n2. Inicialize as referências.\n3. Atualize apenas o necessário.\n\n| Método | Momento |\n| --- | --- |\n| `_ready()` | Inicialização |\n| `_process(delta)` | Atualização |\n\n```gdscript\nfunc _ready():\n    print("Fixture de teste")\n```\n\nConsulte a [documentação de referência](https://docs.godotengine.org/en/stable/).';
const session = { id: 'fixture-session', title: 'Ciclo de vida dos nós · teste', default_model: 'fixture-model', created_at: '2026-10-17T12:00:00Z', updated_at: '2026-10-17T12:00:00Z', is_archived: false };
(async () => {
  const browser = await chromium.launch({ headless: true, channel: process.env.BROWSER_CHANNEL || 'msedge' });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, permissions: ['clipboard-read', 'clipboard-write'], reducedMotion: 'reduce' });
  const page = await context.newPage();
  let mode = 'success', requests = 0, modelRequests = 0, deleted = 0, failHistory = false, failDetail = false;
  const problems = [];
  page.on('pageerror', e => problems.push(e.message));
  await context.route('**/v1/**', async route => {
    const req = route.request(); const pathname = new URL(req.url()).pathname;
    if (mode === 'offline') return route.abort('connectionrefused');
    if (req.method() === 'OPTIONS') return route.fulfill({ status: 204, headers: { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*', 'access-control-allow-methods': '*' } });
    const json = value => route.fulfill({ json: value, headers: { 'access-control-allow-origin': '*' } });
    if (pathname.endsWith('/health')) return json({ status: 'degraded', version: 'test-fixture', environment: 'test', dependencies: {} });
    if (pathname.endsWith('/chat/models')) { modelRequests++; return json([{ id: 'fixture-model', object: 'model' }]); }
    if (pathname.endsWith('/sessions')) { if (failHistory) return route.fulfill({ status: 503, json: { detail: 'fixture unavailable' } }); return json(deleted ? [] : [session]); }
    if (pathname.endsWith('/sessions/fixture-session')) {
      if (req.method() === 'DELETE') { deleted++; return route.fulfill({ status: 204 }); }
      if (failDetail) return route.fulfill({ status: 503, json: { detail: 'fixture unavailable' } });
      return json({ ...session, messages: [{ id: 'q1', role: 'user', status: 'success', content: 'Como funciona o ciclo de vida de um nó?', created_at: session.created_at }, { id: 'a1', role: 'assistant', status: 'success', content: answer, model_used: 'fixture-model', created_at: session.created_at, retrieved_chunks: [{ ...chunk, doc_type: { code: 'documentation' }, project: { code: 'Fixture isolada' }, project_source: undefined }] }] });
    }
    if (pathname.endsWith('/chat')) {
      requests++; const payload = req.postDataJSON(); assert.equal(payload.model, 'fixture-model'); assert.equal(payload.stream, true);
      if (mode === 'network-error') return route.abort('connectionrefused');
      const events = [{ type: 'sources', retrieved_chunks: [chunk] }, { type: 'token', content: mode === 'long' ? (answer + '\n\n').repeat(30) : answer }];
      if (mode === 'error-event') events.push({ type: 'error', detail: 'Fixture interrupted' });
      else if (mode !== 'truncated') events.push({ type: 'done', session_id: payload.session_id, model_used: 'fixture-model', created_at: session.created_at });
      await new Promise(resolve => setTimeout(resolve, mode === 'abort' ? 1800 : 300));
      return route.fulfill({ status: 200, contentType: 'text/event-stream', headers: { 'access-control-allow-origin': '*' }, body: events.map(e => `data: ${JSON.stringify(e)}\n\n`).join('') });
    }
    return route.fulfill({ status: 404, json: { detail: 'Isolated fixture: unsupported route' } });
  });
  const labelFixture = () => page.evaluate(() => { if (document.getElementById('fixture-label')) return; const label = document.createElement('div'); label.id = 'fixture-label'; label.textContent = 'TESTE ISOLADO · respostas e fontes sintéticas'; label.style.cssText = 'position:fixed;bottom:1px;right:8px;z-index:10000;font:9px system-ui;color:#bdcabf;background:#14251d;padding:2px 6px;border-radius:3px;pointer-events:none'; document.body.append(label); });
  const shot = async name => { await labelFixture(); await page.screenshot({ path: path.join(output, name), fullPage: true }); };
  const noOverflow = async () => assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, 'viewport has no horizontal overflow');
  const send = async text => { await page.getByLabel('Sua mensagem').fill(text); await page.getByLabel('Enviar mensagem', { exact: true }).click(); await page.getByLabel('Enviar mensagem', { exact: true }).waitFor(); };
  await page.goto(process.env.APP_URL || 'http://127.0.0.1:3000');
  await page.getByText('fixture-model', { exact: true }).first().waitFor();
  console.log('Typography:', await page.locator('#chat-input').evaluate(el => getComputedStyle(el).fontFamily));
  await page.getByLabel('Buscar conversas').fill('não corresponde'); await page.getByText('Nenhuma conversa corresponde à busca.').waitFor(); await page.getByLabel('Buscar conversas').fill('');
  await noOverflow(); await shot('desktop-empty.png');
  await page.getByRole('button', { name: 'Configurações da conversa', exact: true }).click();
  await page.getByLabel('Trechos a consultar').fill('7');
  await page.keyboard.press('Escape');
  assert.equal(await page.getByRole('button', { name: 'Configurações da conversa', exact: true }).evaluate(el => el === document.activeElement), true);
  await page.getByRole('button', { name: 'Configurações da conversa', exact: true }).click();
  assert.equal(await page.getByLabel('Trechos a consultar').inputValue(), '5', 'cancel preserves settings');
  await page.getByLabel('Trechos a consultar').fill('7'); await page.getByText('Aplicar configurações', { exact: true }).click();
  assert.ok(modelRequests <= 2, 'settings reuses model request (StrictMode may mount twice)');
  await page.getByLabel('Sua mensagem').fill('Rascunho preservado');
  await page.getByRole('button', { name: 'Acesso e credenciais', exact: true }).click();
  await page.getByRole('button', { name: 'Conversas', exact: true }).click();
  assert.equal(await page.getByLabel('Sua mensagem').inputValue(), 'Rascunho preservado');
  await page.getByRole('button', { name: 'Ciclo de vida dos nós · teste', exact: true }).click();
  await page.getByRole('heading', { name: 'Entenda o ciclo de vida' }).waitFor();
  await page.getByText('1 fonte recuperada', { exact: true }).click();
  await page.getByText(chunk.title, { exact: true }).click();
  await page.getByText('Ver trecho original', { exact: true }).click();
  await noOverflow(); await shot('desktop-sources.png');
  await page.getByText('Copiar código', { exact: true }).click();
  assert.ok((await page.evaluate(() => navigator.clipboard.readText())).includes('func _ready'));
  await page.getByLabel('Copiar resposta').click();
  assert.equal((await page.evaluate(() => navigator.clipboard.readText())).replace(/\r\n/g, '\n'), answer);
  failHistory = true; await page.getByLabel('Atualizar histórico').click(); await page.getByText(/Não foi possível atualizar o histórico/).waitFor();
  assert.equal(await page.getByRole('button', { name: session.title, exact: true }).count(), 1); failHistory = false;
  failDetail = true; await page.getByRole('button', { name: session.title, exact: true }).click(); await page.getByText(/Não foi possível abrir a conversa/).waitFor();
  assert.equal(await page.getByRole('heading', { name: 'Entenda o ciclo de vida' }).count(), 1); failDetail = false;
  await page.getByRole('button', { name: `Excluir conversa ${session.title}`, exact: true }).click(); await page.keyboard.press('Escape'); assert.equal(deleted, 0);
  await page.getByRole('button', { name: 'Nova conversa', exact: true }).click();
  await page.getByLabel('Sua mensagem').fill('Pergunta\ncom duas linhas');
  await page.getByLabel('Sua mensagem').press('Shift+Enter'); assert.ok((await page.getByLabel('Sua mensagem').inputValue()).endsWith('\n'));
  await page.getByLabel('Sua mensagem').press('Enter'); await page.getByLabel('Sua mensagem').press('Enter');
  await page.getByRole('heading', { name: 'Entenda o ciclo de vida' }).waitFor(); assert.equal(requests, 1, 'duplicate send is prevented');
  await noOverflow(); await shot('desktop-chat.png');
  for (const failure of ['network-error', 'truncated', 'error-event']) { mode = failure; await send(`Pergunta de teste: ${failure}`); await page.getByText(/A resposta foi interrompida/).waitFor(); assert.equal(await page.getByLabel('Sua mensagem').inputValue(), `Pergunta de teste: ${failure}`); }
  mode = 'abort'; await page.getByLabel('Sua mensagem').fill('Interrupção de leitura'); await page.getByLabel('Enviar mensagem', { exact: true }).click(); await page.getByLabel('Interromper recebimento', { exact: true }).click(); await page.getByText(/Recebimento interrompido/).waitFor(); assert.equal(await page.getByLabel('Sua mensagem').inputValue(), 'Interrupção de leitura');
  mode = 'success'; await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole('button', { name: 'Histórico', exact: true }).click(); await page.getByRole('button', { name: 'Nova conversa', exact: true }).click();
  await noOverflow(); await shot('mobile-empty.png');
  await page.getByLabel('Abrir navegação').click(); await page.keyboard.press('Escape'); assert.equal(await page.getByLabel('Abrir navegação').evaluate(el => el === document.activeElement), true);
  await send('Teste mobile'); await noOverflow(); await shot('mobile-chat.png');
  await page.getByText('1 fonte recuperada', { exact: true }).click(); await page.getByText(chunk.title, { exact: true }).click(); await page.locator('.source-content').last().scrollIntoViewIfNeeded(); await noOverflow(); await shot('mobile-sources.png');
  await page.getByRole('button', { name: 'Configurações da conversa', exact: true }).click(); await noOverflow(); await shot('mobile-settings.png'); await page.keyboard.press('Escape');
  await page.getByRole('button', { name: 'Histórico', exact: true }).click(); await page.getByRole('button', { name: `Excluir conversa ${session.title}`, exact: true }).click(); await page.getByRole('button', { name: 'Excluir conversa', exact: true }).click(); await page.getByRole('dialog', { name: 'Excluir conversa', exact: true }).waitFor({ state: 'hidden' }); assert.equal(deleted, 1); await page.getByLabel('Fechar histórico').click();
  // Deliver actual incremental bytes in this page only to exercise live scrolling.
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.getByRole('button', { name: 'Nova conversa', exact: true }).click();
  await page.evaluate(() => {
    const realFetch = window.fetch;
    window.fetch = async (url, init) => {
      if (!String(url).endsWith('/chat')) return realFetch(url, init);
      window.fetch = realFetch;
      const encoder = new TextEncoder();
      return new Response(new ReadableStream({ start(controller) {
        const emit = event => controller.enqueue(encoder.encode(`data: ${JSON.stringify(event)}\n\n`));
        emit({ type: 'token', content: ('Primeiro trecho sintético de teste.\n\n').repeat(100) });
        setTimeout(() => { emit({ type: 'token', content: 'SEGUNDO TRECHO DO TESTE' }); emit({ type: 'done', model_used: 'fixture-model', session_id: 'fixture', created_at: '2026-10-17T12:00:00Z' }); controller.close(); }, 1200);
      } }), { headers: { 'Content-Type': 'text/event-stream' } });
    };
  });
  await page.getByLabel('Sua mensagem').fill('Teste de rolagem durante streaming'); await page.getByLabel('Enviar mensagem', { exact: true }).click();
  await page.waitForFunction(() => document.querySelector('.message-scroll')?.scrollHeight > 1500);
  await page.locator('.message-scroll').evaluate(el => { el.scrollTop = 0; el.dispatchEvent(new Event('scroll')); });
  await page.waitForFunction(() => document.querySelector('.markdown-message')?.textContent.includes('SEGUNDO TRECHO DO TESTE'));
  assert.equal(await page.locator('.message-scroll').evaluate(el => el.scrollTop), 0, 'streaming must not pull reader away from earlier messages');
  await page.getByRole('button', { name: 'Ir para o final', exact: true }).click();
  assert.ok(await page.locator('.message-scroll').evaluate(el => el.scrollTop > 1000));
  await page.setViewportSize({ width: 390, height: 844 });
  mode = 'offline'; await page.reload(); await page.getByText(/Não foi possível carregar os modelos/).waitFor(); assert.equal(await page.getByLabel('Enviar mensagem', { exact: true }).isDisabled(), true); await noOverflow(); await shot('mobile-offline.png');
  assert.deepEqual(problems, [], 'no browser runtime errors');
  fs.writeFileSync(path.join(output, 'browser-results.json'), JSON.stringify({ status: 'PASS', backend: 'SKIP: localhost:8000 unavailable; all /v1 routes intercepted', screenshots: fs.readdirSync(output).filter(x => x.endsWith('.png')), flows: ['empty state', 'history search/list/load', 'preserve draft across navigation', 'preserve current conversation on history errors', 'delete confirmation Escape and delete', 'SSE sources/token/done', 'duplicate prevention', 'Shift+Enter and Enter', 'HTTP/network and partial stream failures', 'copy response/code', 'source original and summary', 'model settings apply/cancel and focus return', 'mobile drawers/Escape/no overflow', 'offline models disable send'], requests, runtimeErrors: problems }, null, 2));
  await browser.close(); console.log('PASS: isolated desktop/mobile browser flows; screenshots in evidence/');
})().catch(error => { console.error(error); process.exit(1); });
