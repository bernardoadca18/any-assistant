# Any Assistant — repaginação visual

Implementação em 17/10/2026, baseada no commit `1036327`. Sem commit ou push.

## Checkout e conservação

- Original: `C:\workspaces\any-assistant`, branch `main`. A descoberta encontrou somente `.git` e **129 exclusões já preparadas no índice**. Nenhuma exclusão foi revertida ou modificada.
- Implementação: `C:\workspaces\any-assistant-visual`, branch `codex/visual-refresh`, worktree isolado do mesmo repositório. Abrir este diretório para revisar/executar o resultado.
- O criador de worktrees do aplicativo falhou porque o HEAD inclui arquivos `:Zone.Identifier`, incompatíveis com Windows. Foi usado `git worktree add --no-checkout` e checkout esparso excluindo esses metadados. A exceção `core.protectNTFS=false` foi usada somente no comando de materialização; não foi persistida. O checkout esparso está configurado somente no novo worktree.
- Não foram alterados backend, autenticação, persistência, contratos HTTP, coleções, uploads, provedores ou dependências de execução. Não houve ingestão nem inferência paga.

## Implementado

- Identidade Any Assistant / Bolas Industries com indicação de projeto em desenvolvimento, tokens locais, superfícies grafite, destaque verde discreto, tipografia de sistema e foco visível.
- Navegação reorganizada em português; chat como entrada principal. Menus mobile, seleção de domínio/organização, diagnóstico com estado real do healthcheck e salto para o conteúdo.
- Histórico com busca local, seleção, atualização, preservação da lista em falhas e confirmação acessível de exclusão. Abrir uma sessão com erro mantém a conversa atual.
- Tela inicial com sugestões editáveis coerentes com Godot/UE5. Compositor adaptativo, Enter/Shift+Enter, proteção contra envio duplicado e composição IME.
- Mensagens com largura confortável, tabelas, links HTTP(S) seguros, código copiável, cópia da resposta e aviso de falha da área de transferência. HTML continua sendo texto escapado.
- Fontes contextuais expansíveis: título, caminho, tipo, projeto, resumo e trecho original; pontuação de recuperação identificada como diferente de confiança. Aceita os metadados tanto do streaming quanto do histórico. Sem referências inventadas ou vínculos de citação inexistentes.
- Streaming com estado explícito, conteúdo parcial preservado, retorno da pergunta ao compositor em falhas e acompanhamento de rolagem apenas enquanto a pessoa está próxima do final. Botão para voltar ao fim.
- Interrupção do **recebimento** preserva o conteúdo e esclarece que não garante cancelamento da geração no servidor. Não há alegação de cancelamento do provedor.
- Configurações com modelo real retornado pela API, limite de trechos e filtro por projeto, sem segunda consulta de modelos ao abrir. Modal nativo com Escape, contenção e retorno de foco.
- Rascunho e conversa permanecem montados ao navegar pelas áreas do produto. Trocar organização, domínio ou credenciais cria contexto local novo e interrompe a leitura anterior.
- Corrigida falha preexistente de TypeScript: `AuthView` usava `user.username`, ausente no tipo `User`; agora usa `user.name`.

Áreas principais: `frontend/src/components/layout/AppLayout.tsx`, `frontend/src/views/ChatWorkspace.tsx`, `frontend/src/components/chat/*`, `frontend/src/components/ui/Dialog.tsx`, `frontend/src/index.css`, `frontend/src/App.tsx` e metadados HTML. As telas administrativas existentes continuam acessíveis; não receberam uma reformulação profunda nesta rodada.

## Validação efetiva

| Verificação | Resultado |
| --- | --- |
| `npm ci --no-audit --no-fund` com package-lock existente | PASS |
| Baseline `npm run lint` | FAIL preexistente: `AuthView.tsx`, `User.username` |
| `npm run lint` final (script existente = `tsc --noEmit`) | PASS |
| `npm run build` | PASS — Vite, sem dependências novas |
| `npm test` | PASS — 3 testes de Markdown, segurança, tabelas/listas e código parcial |
| `npm run test:visual` com Playwright/Edge instalado no ambiente | PASS — desktop 1440×900 e mobile 390×844, sem erros JavaScript |
| Rolagem com entrega incremental de bytes SSE | PASS — leitura anterior preservada, botão de voltar ao final funciona |
| `git diff --check` | PASS |
| API real `http://localhost:8000/v1/health` | SKIP — conexão recusada |
| E2E real com PostgreSQL, Redis, Qdrant e LiteLLM | SKIP — backend indisponível; dependências individuais não foram inicializadas nem verificadas |
| Testes de backend | SKIP — backend não foi modificado; não há suíte unitária local `app/tests` neste HEAD. Scripts de QA existentes requerem serviços reais |

O projeto não possui ESLint separado: o comando `lint` executa o typecheck. Os testes de frontend foram adicionados usando `node:test`, React SSR e `tsx`, já presentes no lockfile. Playwright é ferramenta externa do ambiente de validação, não uma dependência nova do aplicativo.

**Todos os dados das capturas após a mudança são fixtures sintéticas de teste.** As rotas `/v1/*` são interceptadas somente no processo de teste; não existe modo demo, mock ou resposta fixa incorporada ao produto. As capturas antes da mudança foram geradas do HEAD em diretório temporário com a API bloqueada.

Fluxos testados com fixtures: histórico (busca, abrir, atualizar e falha), nova conversa, envio e prevenção de duplicação, SSE sources/token/done, falha de rede, erro SSE e conexão encerrada sem done, interrupção de recebimento, preservação de pergunta/conteúdo, cópia de resposta e código, consulta de fonte/resumo/original, aplicar/cancelar configurações, retorno de foco, Enter/Shift+Enter, Escape, drawers mobile e ausência de overflow. Troca de credencial, autenticação real, ingestão e consultas reais de conhecimento permanecem SKIP.

## Capturas

| Estado | Desktop | Mobile |
| --- | --- | --- |
| Antes (HEAD) | [Antes desktop](evidence/before-desktop.png) | [Antes mobile](evidence/before-mobile.png) |
| Entrada | [Desktop](evidence/desktop-empty.png) | [Mobile](evidence/mobile-empty.png) |
| Resposta | [Desktop](evidence/desktop-chat.png) | [Mobile](evidence/mobile-chat.png) |
| Fontes | [Desktop](evidence/desktop-sources.png) | [Mobile](evidence/mobile-sources.png) |
| Configurações / falha | — | [Configurações](evidence/mobile-settings.png), [API indisponível](evidence/mobile-offline.png) |

Resultados de navegação: [browser-results.json](evidence/browser-results.json).

## Reproduzir

No PowerShell, a partir do novo checkout:

```powershell
cd C:\workspaces\any-assistant-visual\frontend
rtk npm ci --no-audit --no-fund
rtk npm run lint
rtk npm test
rtk npm run build
rtk npm run dev -- --host 127.0.0.1
```

Abrir `http://127.0.0.1:3000`. Sem API, a interface informa indisponibilidade e bloqueia envio sem modelo. A API continua sendo configurada por `VITE_API_BASE_URL`, com padrão `http://localhost:8000/v1`. Para integração real, usar a configuração de serviços e credenciais existente; nenhuma foi criada nesta rodada.

Em outro terminal, com Playwright disponível (o teste usa o canal `msedge` por padrão; `BROWSER_CHANNEL` permite outro canal):

```powershell
cd C:\workspaces\any-assistant-visual\frontend
$env:PLAYWRIGHT_MODULE = 'C:/Users/Bernardo.DESKTOP-G7D68S2/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'
rtk npm run test:visual
rtk node tests/capture-before.cjs
```

`APP_URL` permite testar outro endereço do frontend. As fixtures bloqueiam todas as chamadas à API de teste; o teste incremental substitui `fetch` somente naquela página e somente para uma chamada de teste. O script de captura anterior deixa uma cópia temporária sem credenciais para reprodução e encerra seu servidor ao concluir.

## Git e diff

Alterações da rodada permanecem sem commit na branch `codex/visual-refresh`. O índice original em `main` mantém suas 129 exclusões preexistentes. Arquivos novos estão marcados apenas com intenção de adição para constarem no diff; não há conteúdo preparado para commit neste worktree.

Patch completo: `C:\workspaces\any-assistant-visual.patch`. Revisar primeiro no worktree isolado. Não aplicar sobre o checkout original com exclusões pendentes antes de decidir o destino dessas alterações preexistentes.

```powershell
cd C:\workspaces\any-assistant-visual
rtk git status --short
rtk git diff --stat
rtk git diff --check
```
