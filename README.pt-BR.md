[English](./README.md) | Português (BR)

---

# Any Assistant AI

![Version](https://img.shields.io/badge/version-0.2.0-blue)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-async-009688)
![React](https://img.shields.io/badge/React-19-61DAFB)
![Status](https://img.shields.io/badge/status-active--development-yellow)

> Um assistente de IA técnico para **Unreal Engine 5**, construído sobre um pipeline avançado de Retrieval-Augmented Generation (RAG). Ele responde perguntas de gameplay e arquitetura de engine ancorando respostas da LLM em uma base de conhecimento semanticamente curada, derivada diretamente do código-fonte C++ da UE5, da documentação oficial da Epic e de lógica de Blueprint exportada.

Diferente de um sistema RAG ingênuo que simplesmente "embeda o código-fonte bruto", o princípio central de design deste projeto é que **C++ e grafos de Blueprint brutos não têm densidade semântica suficiente para um retrieval confiável**. Um motor de ingestão dedicado transforma código e scripting visual em documentação técnica gerada por LLM, legível por humanos, *antes* que qualquer coisa chegue ao banco vetorial — preservando o conteúdo original bruto como uma camada secundária de referência, soberana para sintaxe exata.

---

## Sumário

- [Funcionalidades](#funcionalidades)
- [Arquitetura e Tech Stack](#arquitetura-e-tech-stack)
- [Componentes Principais](#componentes-principais)
  - [Pipeline de Advanced RAG](#1-pipeline-de-advanced-rag)
  - [Motor de Ingestão Code-to-Doc](#2-motor-de-ingestão-code-to-doc-etl-multi-canal)
  - [Idempotência e Resiliência](#3-idempotência-e-resiliência)
  - [Interface Web](#4-interface-web)
- [Endpoints da API](#endpoints-da-api)
- [Início Rápido](#início-rápido)
- [Configuração de Ambiente](#configuração-de-ambiente)
- [Exemplos de Uso](#exemplos-de-uso)
- [Desenvolvimento](#desenvolvimento)
- [Estrutura do Projeto](#estrutura-do-projeto)
- [Roadmap](#roadmap)
- [Contribuindo](#contribuindo)
- [Licença](#licença)

---

## Funcionalidades

- **Suporte Multi-Engine** — Projetado para Unreal Engine 5 com arquitetura extensível para Godot 4
- **Pipeline de Advanced RAG** — Decomposição de query, reranking com cross-encoder e suporte multi-idioma
- **Transformação Code-to-Doc** — Documentação gerada por LLM a partir de headers C++, Blueprints e arquivos `.udn` da Epic
- **Memória de Conversação** — Histórico de chat persistente com Redis e PostgreSQL
- **Respostas em Streaming** — Streaming de respostas em tempo real para melhor UX
- **Pronto para Multi-Tenant** — Retrieval escopo por projeto e coleções isoladas
- **Interface Web** — Frontend React moderno com tema dark e design responsivo
- **Pronto para Produção** — Orquestração Docker Compose com health checks e limits de recursos

---

## Arquitetura e Tech Stack

O sistema é composto por um serviço FastAPI stateless cercado por infraestrutura especializada. **Nenhum componente jamais fala diretamente com um provedor de LLM** — toda chamada de modelo (chat, embeddings, decomposição de query, geração de documentação) é roteada através de um único **LiteLLM Proxy**, que abstrai o provedor subjacente (modelos locais Ollama ou modelos em cloud como Gemini).

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              CAMADA FRONTEND                                 │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  React 19 + Vite + TypeScript + Tailwind CSS (Porta 3001)           │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                              CAMADA API                                      │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  FastAPI (Porta 8000)                                               │   │
│  │  • /v1/chat          • /v1/ingest/process-folder                   │   │
│  │  • /v1/health        • /v1/sessions                                │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
          ┌───────────────────────────┼───────────────────────────┐
          ▼                           ▼                           ▼
┌──────────────────┐    ┌──────────────────────┐    ┌──────────────────────┐
│  Qdrant          │    │  Redis               │    │  PostgreSQL          │
│  Banco Vetorial  │    │  • Cache de sessão   │    │  • Histórico de chat │
│  (Porta 6333)    │    │  • Locks de ingestão │    │  • Mensagens         │
│                  │    │  • Checkpoints       │    │  • Citações          │
└──────────────────┘    └──────────────────────┘    └──────────────────────┘
          │
          ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                          ORQUESTRAÇÃO LLM                                    │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  LiteLLM Proxy (Porta 4000)                                         │   │
│  │  • Roteia para Ollama (local) ou Gemini (cloud)                     │   │
│  │  • API unificada para chat, embeddings, decomposições               │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────┘
```

| Componente | Função |
|---|---|
| **FastAPI** | Camada API async expondo `/v1/chat`, `/v1/ingest/process-folder` e `/v1/health`. Executa ingestão como `BackgroundTasks` no mesmo event loop, usando `asyncio.to_thread()` para descarregar I/O de disco bloqueante. |
| **React Frontend** | SPA moderna construída com React 19, Vite, TypeScript e Tailwind CSS. Features: streaming de respostas em tempo real, gerenciamento de sessões e UI dark-theme responsiva. |
| **LiteLLM Proxy** | Ponto de entrada único para *todo* tráfego de modelo — chat completions, embeddings, decomposição de query e geração de documentação em tempo de ingestão. Roteia internamente para Ollama ou Gemini dependendo do alias do modelo. Autenticado via master key. |
| **Qdrant** | Banco de dados vetorial armazenando chunks de documentação com payloads enriquecidos (`project_source`, `doc_type`, `ue_version`, `raw_content`). Suporta busca filtrada por payload (ex: restringir retrieval a um projeto específico). |
| **Redis** | Armazenamento de múltiplos propósitos: histórico de sessão de conversa (com TTL), o **manifesto de checkpoint** da ingestão (hashes SHA-256 de conteúdo por arquivo), um **rastreador de falhas/quarentena**, e **locks distribuídos** que serializam execuções de ingestão por coleção. |
| **PostgreSQL** | Persistência durável para sessões de chat, mensagens e chunks citados — a fonte autoritativa de histórico de conversa, com Redis servindo como cache de contexto de curto prazo. |
| **FlashRank** | Reranker Cross-Encoder leve, rodando apenas em CPU (runtime ONNX, sem necessidade de GPU), usado para reordenar os chunks recuperados por relevância semântica real antes de chegarem ao prompt final. |

---

## Componentes Principais

### 1. Pipeline de Advanced RAG

O pipeline (`app/services/ai/rag_service.py`) roda em três fases desenhadas para maximizar tanto o **recall** (encontrar toda documentação relevante para perguntas multi-tópico) quanto a **precisão** (filtrar ruído antes de chegar ao prompt final):

**Fase 1 — Decomposição e Tradução de Query**

O corpus vetorial está indexado inteiramente em **inglês técnico** (tanto a documentação gerada por LLM quanto os docs `.udn` oficiais da Epic). Como uma query em português sofreria um mismatch de embedding cross-lingual, `decompose_query()` chama uma LLM rápida (`gemini-3.1-flash-lite`, com um modelo de fallback distinto em caso de falha) para:

1. Produzir uma **tradução** literal em inglês da pergunta do usuário (`translated_query`) — esta é sempre usada como âncora de busca, nunca a query no idioma original.
2. Decompor a pergunta em até **3 sub-queries técnicas adicionais**, cada uma focada em um único conceito da UE5 (ex: dividir "como implemento pulo em C++ e configuro a animação de aterrissagem?" em uma query de input-binding em C++ e uma query separada de Animation Blueprint).

O parser de resposta é deliberadamente defensivo, com estratégias de fallback em camadas (JSON direto → JSON em bloco markdown → JSON embutido → extração de strings entre aspas → fallback para pergunta original) para garantir que o pipeline nunca quebre diante de uma resposta malformada da LLM.

**Fase 2 — Retrieval Expandido + Reranking via Cross-Encoder**

1. Cada query (tradução + sub-queries) é embedada e buscada no Qdrant de forma independente, cada uma trazendo até `rag_retrieval_limit_per_query` candidatos (padrão: 15) — priorizando recall em detrimento de precisão nesta etapa.
2. Os resultados de todas as queries são **deduplicados** em memória por `(source_file, chunk_id)`.
3. O pool deduplicado é reordenado por `rerank_with_flashrank()`, um Cross-Encoder (`ms-marco-MiniLM-L-12-v2` por padrão) que pontua cada chunk diretamente contra a query traduzida em inglês — nunca contra sub-queries individualmente — para julgar relevância contra a intenção completa da pergunta. O reranking roda em CPU via ONNX e é despachado para uma thread para não bloquear o event loop.
4. Um piso de qualidade opcional (`rag_rerank_min_score`) pode descartar chunks com score baixo após o reranking, sempre preservando ao menos o resultado de maior score, para que o prompt nunca fique vazio.
5. O retrieval pode, opcionalmente, ser restrito a um único `project_source` (ex: apenas Lyra, ou apenas código base da Engine) via filtro de payload no Qdrant.

**Fase 3 — Geração**

O contexto final é formatado em um prompt de dupla camada por chunk:

- **Análise Semântica** — o resumo conceitual gerado por LLM (o que o código *faz* e *por quê*).
- **Referência Direta** — o código fonte original, não modificado (`raw_content`: o header C++ limpo, `.udn` sanitizado, ou JSON de Blueprint formatado), que o prompt do sistema instrui explicitamente o modelo a tratar como a **fonte soberana de verdade** para assinaturas exatas, tipos e nomes de propriedades.

O histórico de conversa é carregado do Redis, a mensagem aumentada é enviada ao modelo de chat via `call_litellm()`, e o turno é persistido de volta ao Redis. Cada fase tem tratamento de erro isolado com fallback para um caminho Naive RAG mais simples (`retrieve_chunks()`), de forma que uma falha na decomposição ou no reranking nunca se manifesta como um HTTP 500.

### 2. Motor de Ingestão Code-to-Doc (ETL Multi-Canal)

O motor de ingestão (`app/services/ai/ingestion_service.py`) é disparado via `POST /v1/ingest/process-folder` e roda inteiramente como uma **`BackgroundTask` do FastAPI**, dentro do mesmo container e event loop da API de chat. Como uma varredura recursiva sobre uma árvore de código em escala de engine pode envolver dezenas de milhares de arquivos, todo trabalho ligado a disco (`os.walk`, leitura de arquivos) é despachado para threads de trabalho via `asyncio.to_thread()`, para nunca bloquear requisições concorrentes de `/v1/chat`.

Seu princípio arquitetural central: **código bruto nunca é embedado diretamente**. Cada canal transforma seu material de origem em Markdown semanticamente denso antes de ser dividido em chunks e embedado — é isso que permite que um modelo local pequeno responda com precisão em vez de se perder na sintaxe bruta.

| Canal | Origem | Processamento |
|---|---|---|
| **A — headers `.h`** | Headers C++ da UE5 | Limpa includes/boilerplate, filtra por relevância de gameplay (allowlist estrita por palavra-chave para código de Engine; desligada inteiramente em `sample_mode` ou para módulos estratégicos de gameplay moderno como Mover/Network Prediction), então envia o header já limpo a uma LLM atuando como "Documentador Técnico Sênior" para produzir um guia em Markdown orientado a gameplay. |
| **B — `.udn`** | Formato de documentação oficial da Epic | Sanitização baseada em regex puro (remove macros/metadata proprietários) — **sem chamada de LLM**. Menor custo, já que a fonte já é prosa escrita por humanos. |
| **C — `.cpp` (apenas Sample Mode)** | Lógica de implementação em projetos de exemplo (Lyra, City Sample, Vehicle Sample) | Divide o arquivo em blocos de nível de função via detecção de assinatura + balanceamento de chaves, agrupa funções pequenas para evitar chamadas de LLM desperdiçadoras, e documenta a lógica de implementação agrupada — capturando *como* uma mecânica funciona, não apenas sua API pública. |
| **D — `.bp.json` (apenas Sample Mode)** | Grafos de Blueprint, exportados via script Python companion no Unreal Editor (`BP_GraphExporter.py`) | Consome uma representação JSON já estruturada (`ParentClass`, `Variables`, `ExecutionFlow` linearizado com resolução de branch/loop) — sem parsing por regex de texto bruto — e documenta o comportamento de gameplay do Blueprint, suas variáveis-chave, principais fluxos de evento e pontos de integração com outros sistemas. |

Os quatro canais convergem para o mesmo pipeline posterior: **chunking estrutural de Markdown** (dividido pela hierarquia de headers, com um teto rígido de caracteres como rede de segurança), **enriquecimento de metadados** (`project_source`, `doc_type`, `ue_version`, `raw_content`), **geração de embeddings em lote**, e **upsert idempotente** no Qdrant.

### 3. Idempotência e Resiliência

O Redis sustenta todos os mecanismos de segurança do pipeline de ingestão:

- **Manifesto de Checkpoint** — um hash do Redis (`manifest:{collection_name}`) mapeia o caminho relativo de cada arquivo-fonte a um hash SHA-256 de seu conteúdo. Em reingestões, arquivos inalterados são completamente ignorados (sem releitura, sem chamada de LLM, sem reembedding), e arquivos modificados são detectados e reprocessados automaticamente. A entrada de um arquivo só é commitada no manifesto depois que *todos* os chunks que ele gerou forem **confirmados como persistidos** no Qdrant — nunca com base apenas em contagens de geração em memória — de forma que uma execução interrompida nunca deixa um checkpoint de "sucesso" falso.
- **Rastreamento de Falhas e Quarentena** — arquivos que falham no processamento (erros de leitura, de LLM, de embedding) são rastreados em um hash de falhas dedicado no Redis. Após `ingestion_max_consecutive_failures` falhas consecutivas, um arquivo é colocado em quarentena e pulado em execuções futuras até ser limpo manualmente, prevenindo chamadas desperdiçadoras de LLM/embedding em um arquivo permanentemente quebrado.
- **Locking Distribuído** — um lock distribuído baseado em Redis (`ingestion_lock_key`) garante que apenas uma execução de ingestão por coleção possa rodar por vez, prevenindo que duas execuções concorrentes corrompam o manifesto de checkpoint. O lock é renovado periodicamente (heartbeat) durante a duração de ingestões longas para que nunca expire no meio da execução por causa do TTL, enquanto ainda expira automaticamente de forma segura se o processo morrer inesperadamente.
- **Limpeza de Órfãos** — quando um arquivo previamente indexado muda, seus chunks antigos são deletados do Qdrant só *depois* que os novos chunks são confirmados como commitados, evitando qualquer janela em que dados válidos fiquem ausentes do índice.

### 4. Interface Web

Uma aplicação single-page moderna construída com:

- **React 19** — React mais recente com renderização concorrente
- **Vite** — Build tooling rápido com HMR
- **TypeScript** — Desenvolvimento type-safe
- **Tailwind CSS v4** — Styling utility-first com tema dark
- **Zustand** — Gerenciamento de estado leve
- **Lucide React** — Conjunto de ícones elegante
- **Motion** — Animações suaves

Funcionalidades:
- Streaming de respostas em tempo real
- Gerenciamento de sessões com histórico de conversa
- Design responsivo para desktop e mobile
- Tema dark otimizado para longas sessões de código

---

## Endpoints da API

| Método | Endpoint | Descrição |
|---|---|---|
| `POST` | `/v1/chat` | Executa o pipeline completo de Advanced RAG: embeda a pergunta, recupera e reordena chunks de documentação, monta o prompt aumentado, e gera resposta via proxy LiteLLM. O histórico de conversa é mantido por `session_id` no Redis. Retorna `503` em falhas operacionais conhecidas (ex: LiteLLM/Ollama indisponível) e `500` em erros inesperados. |
| `POST` | `/v1/ingest/process-folder` | Dispara o motor de ETL Code-to-Doc como tarefa em background sobre um caminho de pasta **dentro do container**. Aceita `content_type` (`source_code` ou `documentation`), `project_source` (rótulo de proveniência, ex: `UE5_Engine`, `Lyra`), `sample_mode` (habilita Canais C e D), e um `ue_version` opcional para sobrescrever detecção automática. Retorna imediatamente com `202 Accepted` — tarefa roda de forma assíncrona e progresso é acompanhado via logs da aplicação e manifesto no Redis. |
| `GET` | `/v1/health` | Health check ativo contra todas dependências (Qdrant, Redis, LiteLLM), executado de forma concorrente. Retorna `200` se todas dependências estiverem saudáveis ou degradadas, `503` se alguma dependência estiver em estado de erro — incluindo latência e detalhes por dependência. |
| `GET` | `/v1/sessions` | Lista todas as sessões de chat com suporte a paginação. |
| `GET` | `/v1/sessions/{session_id}` | Recupera uma sessão específica com seu histórico de mensagens. |
| `DELETE` | `/v1/sessions/{session_id}` | Deleta uma sessão e seu histórico. |

---

## Início Rápido

### Pré-requisitos

- Docker e Docker Compose
- Git
- (Opcional) Ollama para modelos locais
- (Opcional) Chave API do Gemini para modelos em cloud

### 1. Clonar e Configurar

```bash
git clone <repository-url>
cd ue5-ai-assistant
cp .env.example .env
```

Edite o `.env` e preencha os valores necessários:

```bash
# Obrigatório
AGENT_REDIS_PASSWORD=sua-senha-segura
AGENT_POSTGRES_PASSWORD=sua-senha-segura
LITELLM_MASTER_KEY=sua-master-key

# Opcional (para modelos Gemini)
GEMINI_API_KEY=sua-api-key-gemini
```

### 2. Iniciar o Stack

```bash
# Usando Docker Compose diretamente
docker compose up -d --build

# Ou usando o Makefile
make up
```

Isso sobe:
- `ue5-frontend` — Interface web React (porta 3001)
- `ue5-agent` — Backend FastAPI (porta 8000)
- `qdrant` — Banco vetorial (porta 6333)
- `agent-redis` — Cache de sessão e locks de ingestão
- `agent-postgres` — Armazenamento persistente de mensagens
- `litellm-proxy` — Gateway LLM (requer rede externa)

### 3. Verificar Health

```bash
curl http://localhost:8000/v1/health
```

Ou abra http://localhost:3001 no navegador.

### 4. Ingerir Documentação

Ingerir source da Engine UE5:

```bash
curl -X POST http://localhost:8000/v1/ingest/process-folder \
  -H "Content-Type: application/json" \
  -d '{
        "folder_path": "/data/ue5_engine_root/Engine/Source",
        "content_type": "source_code",
        "project_source": "UE5_Engine",
        "sample_mode": false
      }'
```

Ingerir um projeto de exemplo (Lyra, City Sample, etc.):

```bash
curl -X POST http://localhost:8000/v1/ingest/process-folder \
  -H "Content-Type: application/json" \
  -d '{
        "folder_path": "/data/sample_projects/Lyra",
        "content_type": "source_code",
        "project_source": "Lyra",
        "sample_mode": true
      }'
```

### 5. Conversar

Via Web UI:
- Abra http://localhost:3001
- Inicie uma nova conversa

Via API:

```bash
curl -X POST http://localhost:8000/v1/chat \
  -H "Content-Type: application/json" \
  -d '{
        "message": "Como implemento um double jump em C++ e conecto isso a um Animation Blueprint?",
        "session_id": "test-session-001"
      }'
```

---

## Configuração de Ambiente

A configuração é centralizada em `app/core/config.py` via Pydantic Settings, carregada de um arquivo `.env`.

### Aplicação

```env
APP_ENV=production               # production | development | test
LOG_LEVEL=INFO                   # DEBUG | INFO | WARNING | ERROR
```

### Bancos de Dados

```env
# Qdrant
QDRANT_HOST=qdrant
QDRANT_PORT=6333
QDRANT_COLLECTION=ue5_docs

# Redis
REDIS_HOST=agent-redis
REDIS_PORT=6379
REDIS_PASSWORD=                  # obrigatório
REDIS_DB=0
REDIS_SESSION_TTL=3600           # TTL de sessão em segundos

# PostgreSQL
POSTGRES_HOST=agent-postgres
POSTGRES_PORT=5432
POSTGRES_USER=ue5rag
POSTGRES_PASSWORD=               # obrigatório
POSTGRES_DB=ue5_rag_assistant
```

### Configuração LLM

```env
# LiteLLM Proxy (ponto de entrada único para todas chamadas de modelo)
LITELLM_BASE_URL=http://litellm-proxy:4000
LITELLM_MASTER_KEY=              # obrigatório
LITELLM_MAX_RETRIES=4

# Embeddings
EMBEDDING_MODEL=ollama-embed     # deve estar configurado no config.yml do LiteLLM
EMBEDDING_DIM=1024               # 384 | 768 | 1024 | 1536 | 3072
```

### Pipeline RAG

```env
# Geral
RAG_TOP_K=5
RAG_SCORE_THRESHOLD=0.6          # Apenas Naive RAG

# Decomposição de Query
RAG_DECOMPOSITION_ENABLED=true
RAG_DECOMPOSITION_MODEL=gemini/gemini-3.1-flash-lite
RAG_DECOMPOSITION_TIMEOUT=12.0

# Retrieval
RAG_RETRIEVAL_LIMIT_PER_QUERY=15

# Reranker
RAG_RERANKER_ENABLED=true
RAG_RERANKER_MODEL=ms-marco-MiniLM-L-12-v2
RAG_RERANKER_CACHE_DIR=/tmp/flashrank_cache
RAG_RERANK_MIN_SCORE=0.0
```

### Ingestão

```env
INGESTION_DOCUMENTER_MODEL=gemini/gemini-2.0-flash-lite
INGESTION_BATCH_SIZE=32
INGESTION_EMBEDDING_CONCURRENCY=8
INGESTION_MAX_CHUNK_CHARS=3000
INGESTION_DEFAULT_UE_VERSION=5.8
INGESTION_LOCK_TIMEOUT_SECONDS=3600
INGESTION_MAX_HEADER_CHARS=12000
INGESTION_MAX_CONSECUTIVE_FAILURES=3
```

---

## Exemplos de Uso

### Iniciando Ambiente de Desenvolvimento

```bash
# Iniciar com hot-reload
make dev

# Ou manualmente
docker compose --profile dev up -d
```

### Executando Jobs de Ingestão

```bash
# Usando o Makefile
make ingest-ue5

# Verificar status da ingestão
make ingest-status

# Ingestão customizada
make ingest ARGS="folder=/data/custom type=source_code project=MyProject"
```

### Operações de Banco de Dados

```bash
# Abrir shell do Postgres
make db-shell

# Criar um backup
make db-backup

# Abrir CLI do Redis
make redis-shell
```

### Visualizando Logs

```bash
# Todos os serviços
make logs

# Serviço específico
make logs-backend
make logs-frontend
make logs-qdrant
```

### Executando Testes

```bash
# Todos os testes
make test

# Testes do backend com coverage
make test-backend

# Testes do frontend
make test-frontend
```

---

## Desenvolvimento

### Targets Disponíveis no Makefile

Execute `make help` para ver todos os comandos disponíveis:

```
make dev           # Iniciar ambiente de desenvolvimento
make up            # Iniciar ambiente de produção
make down          # Parar todos os serviços
make logs          # Mostrar todos os logs
make health        # Verificar health dos serviços
make test          # Executar todos os testes
make db-shell      # Abrir shell do Postgres
make ingest-ue5    # Ingerir documentação da UE5
make shell-backend # Abrir shell do container backend
```

### Estrutura do Projeto

```
ue5-ai-assistant/
├── app/                      # Backend FastAPI
│   ├── api/v1/endpoints/     # Handlers de rotas da API
│   ├── core/                 # Configuração, auth, logging
│   ├── core/domains/         # Configs de domínio específicas por engine
│   ├── db/                   # Modelos de banco e conexões
│   ├── schemas/              # Schemas Pydantic de request/response
│   ├── services/             # Lógica de negócio
│   │   ├── ai/               # RAG, ingestão, embeddings
│   │   └── db/               # Serviços de banco
│   └── main.py               # Entry point da aplicação FastAPI
├── frontend/                 # Frontend React
│   ├── src/                  # Componentes e lógica React
│   ├── public/               # Assets estáticos
│   └── package.json          # Dependências npm
├── config/                   # Configurações de serviços
│   ├── qdrant/               # Config do Qdrant
│   └── redis/                # Config do Redis
├── db/                       # Inicialização do banco
│   └── init/                 # Scripts de migração SQL
├── scripts/                  # Scripts utilitários
├── docker-compose.yml        # Orquestração de serviços
├── Dockerfile                # Container do backend
├── Makefile                  # Comandos de desenvolvimento
└── requirements.txt          # Dependências Python
```

### Adicionando um Novo Domínio de Engine

O projeto suporta domínios de engine extensíveis. Para adicionar uma nova engine:

1. Crie uma config de domínio em `app/core/domains/`
2. Registre-a em `app/core/domains/registry.py`
3. Adicione estratégias de ingestão se necessário em `app/services/ai/ingestion/`

---

## Roadmap

### v0.3.0 (Planejado)

- [ ] Streaming de respostas de chat na API
- [ ] Suporte a WebSocket para updates em tempo real
- [ ] Highlighting de citações no frontend
- [ ] Mensagens de erro melhoradas e recuperação

### v0.4.0 (Planejado)

- [ ] Busca multi-coleção
- [ ] UI de filtragem avançada
- [ ] Exportar conversas
- [ ] Gerenciamento de API keys

### v0.5.0 (Planejado)

- [ ] Suporte completo a Godot 4
- [ ] Embeddings customizados fine-tuned
- [ ] Dashboard de performance
- [ ] Rate limiting

---

## Contribuindo

Contribuições são bem-vindas! Por favor leia nossas diretrizes de contribuição antes de submeter PRs.

1. Faça fork do repositório
2. Crie uma branch de feature (`git checkout -b feature/feature-incrivel`)
3. Commit suas mudanças (`git commit -m 'feat: adiciona feature incrível'`)
4. Push para a branch (`git push origin feature/feature-incrivel`)
5. Abra um Pull Request

---

## Licença

Este projeto está licenciado sob a Licença MIT - veja o arquivo [LICENSE](LICENSE) para detalhes.

---

## Agradecimentos

- [Epic Games](https://www.epicgames.com/) pelo Unreal Engine 5 e o formato de documentação `.udn`
- [Qdrant](https://qdrant.tech/) pelo excelente banco de dados vetorial
- [LiteLLM](https://github.com/BerriAI/litellm) por simplificar a abstração de provedores LLM
- [FlashRank](https://github.com/PrithivirajDamodaran/FlashRank) pelo reranking eficiente
