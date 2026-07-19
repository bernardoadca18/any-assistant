[English](./README.md) | Português (BR)

---

# UE5 AI Assistant

![Version](https://img.shields.io/badge/version-0.2.0-blue)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-async-009688)
![Status](https://img.shields.io/badge/status-active--development-yellow)

> Um assistente de IA técnico para **Unreal Engine 5**, construído sobre um pipeline avançado de Retrieval-Augmented Generation (RAG). Ele responde perguntas de gameplay e arquitetura de engine ancorando as respostas da LLM em uma base de conhecimento semanticamente curada, derivada diretamente do código-fonte C++ da UE5, da documentação oficial da Epic e de lógica de Blueprint exportada.

Diferente de um sistema RAG ingênuo que simplesmente "embeda o código-fonte bruto", o princípio central de design deste projeto é que **C++ e grafos de Blueprint brutos não têm densidade semântica suficiente para um retrieval confiável**. Um motor de ingestão dedicado transforma código e scripting visual em documentação técnica gerada por LLM, legível por humanos, *antes* que qualquer coisa chegue ao banco vetorial — preservando o conteúdo original bruto como uma camada secundária de referência, soberana para sintaxe exata.

---

## Sumário

- [Arquitetura e Tech Stack](#arquitetura-e-tech-stack)
- [Core Features](#core-features)
  - [Pipeline de Advanced RAG](#1-pipeline-de-advanced-rag)
  - [Motor de Ingestão Code-to-Doc](#2-motor-de-ingestão-code-to-doc-etl-multi-canal)
  - [Idempotência e Resiliência](#3-idempotência-e-resiliência)
- [Endpoints da API](#endpoints-da-api)
- [Configuração de Ambiente](#configuração-de-ambiente)
- [Instruções de Uso](#instruções-de-uso)

---

## Arquitetura e Tech Stack

O sistema é composto por uma camada de API FastAPI stateless, cercada por infraestrutura especializada. **Nenhum componente conversa diretamente com um provedor de LLM** — toda chamada de modelo (chat, embeddings, decomposição de query, geração de documentação na ingestão) é roteada através de um único **LiteLLM Proxy**, que abstrai o provedor subjacente (modelos locais via Ollama ou modelos em nuvem como o Gemini).

| Componente | Papel |
|---|---|
| **FastAPI** | Camada de API assíncrona que expõe `/v1/chat`, `/v1/ingest/process-folder` e `/v1/health`. Executa a ingestão como `BackgroundTasks` no mesmo event loop, usando `asyncio.to_thread()` para descarregar I/O de disco bloqueante. |
| **LiteLLM Proxy** | Ponto único de entrada para *todo* o tráfego de modelos — chat completions, embeddings, decomposição de query e geração de documentação em tempo de ingestão. Roteia internamente para Ollama ou Gemini conforme o alias do modelo. Autenticado via master key. |
| **Qdrant** | Banco de dados vetorial que armazena os chunks de documentação com payloads enriquecidos (`project_source`, `doc_type`, `ue_version`, `raw_content`). Suporta busca filtrada por payload (ex: restringir o retrieval a um projeto específico). |
| **Redis** | Armazenamento de múltiplos propósitos: histórico de sessão de conversa (com TTL), o **manifesto de checkpoint** da ingestão (hashes SHA-256 de conteúdo por arquivo), um **rastreador de falhas/quarentena**, e **locks distribuídos** que serializam execuções de ingestão por coleção. |
| **FlashRank** | Reranker Cross-Encoder leve, rodando apenas em CPU (runtime ONNX, sem necessidade de GPU), usado para reordenar os chunks recuperados por relevância semântica real antes de chegarem ao prompt final. |
| **Gemini (via LiteLLM)** | Usado em duas tarefas leves, rápidas e baratas: decomposição/tradução de query (`gemini-3.1-flash-lite`) e geração de documentação em tempo de ingestão (`gemini-2.0-flash-lite`). |
| **Ollama** | Backend de modelo local (ex: `qwen2.5-coder`), acessado exclusivamente através do proxy LiteLLM — usado para o modelo principal de chat/completion e para embeddings. |

---

## Core Features

### 1. Pipeline de Advanced RAG

O pipeline (`app/services/ai/rag_service.py`) roda em três fases desenhadas para maximizar tanto o **recall** (encontrar toda a documentação relevante para perguntas multi-tópico) quanto a **precisão** (filtrar ruído antes que chegue ao prompt final):

**Fase 1 — Decomposição e Tradução de Query**
O corpus vetorial é indexado inteiramente em **inglês técnico** (tanto a documentação gerada por LLM quanto os docs oficiais `.udn` da Epic). Como uma query em português sofreria um mismatch cross-lingual no embedding, `decompose_query()` chama uma LLM rápida (`gemini-3.1-flash-lite`, com um modelo de fallback distinto em caso de falha) para:
1. Produzir uma **tradução** literal em inglês da pergunta do usuário (`translated_query`) — esta é sempre usada como âncora de busca, nunca a query original no idioma de origem.
2. Decompor a pergunta em até **3 sub-queries técnicas adicionais**, cada uma focada em um único conceito da UE5 (ex: separar "como programo o pulo em C++ e configuro a animação de aterrissagem?" em uma query de input binding em C++ e outra de Animation Blueprint).

O parser da resposta é deliberadamente defensivo, com estratégias de fallback em camadas (JSON direto → JSON em bloco markdown → JSON embutido → extração de strings entre aspas → fallback para a pergunta original) para garantir que o pipeline nunca quebre diante de uma resposta malformada da LLM.

**Fase 2 — Retrieval Expandido + Reranking via Cross-Encoder**
1. Cada query (tradução + sub-queries) é embedada e buscada no Qdrant de forma independente, cada uma trazendo até `rag_retrieval_limit_per_query` candidatos (padrão: 15) — priorizando recall em detrimento de precisão nesta etapa.
2. Os resultados de todas as queries são **deduplicados** em memória por `(source_file, chunk_id)`.
3. O pool deduplicado é reordenado por `rerank_with_flashrank()`, um Cross-Encoder (`ms-marco-MiniLM-L-12-v2` por padrão) que pontua cada chunk diretamente contra a query traduzida em inglês — nunca contra as sub-queries individualmente — para julgar a relevância em relação à intenção completa da pergunta. O reranking roda em CPU via ONNX e é despachado para uma thread para não bloquear o event loop.
4. Um piso de qualidade opcional (`rag_rerank_min_score`) pode descartar chunks com score baixo após o reranking, sempre preservando ao menos o resultado de maior score, para que o prompt nunca fique vazio.
5. O retrieval pode, opcionalmente, ser restrito a um único `project_source` (ex: apenas Lyra, ou apenas código base da Engine) via filtro de payload no Qdrant.

**Fase 3 — Geração**
O contexto final é formatado em um prompt de duas camadas por chunk:
- **Análise Semântica** — o resumo conceitual gerado pela LLM (o que o código *faz* e *por quê*).
- **Referência Direta** — o código-fonte bruto e não modificado (`raw_content`: o header C++ já limpo, o `.udn` sanitizado, ou o JSON de Blueprint formatado), que o system prompt instrui explicitamente o modelo a tratar como a **fonte soberana de verdade** para assinaturas exatas, tipos e nomes de propriedades.

O histórico de conversa é carregado do Redis, a mensagem aumentada é enviada ao modelo de chat via `call_litellm()`, e o turno é persistido de volta no Redis. Cada fase tem tratamento de erro isolado com fallback para um caminho de RAG mais simples (`retrieve_chunks()`, Naive RAG), de forma que uma falha na decomposição ou no reranking nunca se manifesta como um HTTP 500.

### 2. Motor de Ingestão Code-to-Doc (ETL Multi-Canal)

O motor de ingestão (`app/services/ai/ingestion_service.py`) é disparado via `POST /v1/ingest/process-folder` e roda inteiramente como uma **`BackgroundTask` do FastAPI**, dentro do mesmo container e event loop da API de chat. Como uma varredura recursiva sobre uma árvore de código em escala de engine pode envolver dezenas de milhares de arquivos, todo trabalho ligado a disco (`os.walk`, leitura de arquivos) é despachado para threads de trabalho via `asyncio.to_thread()`, para nunca bloquear requisições concorrentes a `/v1/chat`.

Seu princípio arquitetural central: **código bruto nunca é embedado diretamente**. Cada canal transforma seu material de origem em Markdown semanticamente denso antes de ser dividido em chunks e embedado — é isso que permite que um modelo local pequeno responda com precisão em vez de se perder na sintaxe bruta.

| Canal | Origem | Processamento |
|---|---|---|
| **A — headers `.h`** | Headers C++ da UE5 | Limpa includes/boilerplate, filtra por relevância de gameplay (allowlist estrita por palavra-chave para código de Engine; desligada inteiramente em `sample_mode` ou para módulos estratégicos de gameplay moderno como Mover/Network Prediction), então envia o header já limpo a uma LLM atuando como "Documentador Técnico Sênior" para produzir um guia em Markdown orientado a gameplay. |
| **B — `.udn`** | Formato oficial de documentação da Epic | Sanitização puramente via regex (remove macros/metadados proprietários) — **sem chamada de LLM**. Custo menor, já que a origem já é prosa escrita por humanos. |
| **C — `.cpp` (somente Sample Mode)** | Lógica de implementação em projetos de amostra (Lyra, City Sample, Vehicle Sample) | Divide o arquivo em blocos por função via detecção de assinatura + balanceamento de chaves, agrupa funções pequenas para evitar chamadas de LLM desperdiçadas, e documenta a lógica de implementação agrupada — capturando *como* uma mecânica funciona, não apenas sua API pública. |
| **D — `.bp.json` (somente Sample Mode)** | Grafos de Blueprint, exportados via um script Python complementar do Editor Unreal (`BP_GraphExporter.py`) | Consome uma representação JSON já estruturada (`ParentClass`, `Variables`, `ExecutionFlow` linearizado com resolução de branch/loop) — sem parsing por regex de texto bruto — e documenta o comportamento de gameplay do Blueprint, suas variáveis-chave, principais fluxos de evento e pontos de integração com outros sistemas. |

Os quatro canais convergem para o mesmo pipeline posterior: **chunking estrutural de Markdown** (dividido pela hierarquia de headers, com um teto rígido de caracteres como rede de segurança), **enriquecimento de metadados** (`project_source`, `doc_type`, `ue_version`, `raw_content`), **geração de embeddings em lote**, e **upsert idempotente** no Qdrant.

### 3. Idempotência e Resiliência

O Redis sustenta todos os mecanismos de segurança do pipeline de ingestão:

- **Manifesto de Checkpoint** — um hash do Redis (`manifest:{collection_name}`) mapeia o caminho relativo de cada arquivo-fonte a um hash SHA-256 de seu conteúdo. Em reingestões, arquivos inalterados são completamente ignorados (sem releitura, sem chamada de LLM, sem reembedding), e arquivos modificados são detectados e reprocessados automaticamente. A entrada de um arquivo só é commitada no manifesto depois que *todos* os chunks que ele gerou forem **confirmados como persistidos** no Qdrant — nunca com base apenas em contagens de geração em memória — de forma que uma execução interrompida nunca deixa um checkpoint de "sucesso" falso.
- **Rastreamento de Falhas e Quarentena** — arquivos que falham no processamento (erros de leitura, de LLM, de embedding) são rastreados em um hash dedicado de falhas no Redis. Após `ingestion_max_consecutive_failures` falhas consecutivas, um arquivo é colocado em quarentena e ignorado em execuções futuras até intervenção manual, evitando desperdiçar chamadas de LLM/embedding em um arquivo permanentemente quebrado.
- **Lock Distribuído** — um lock distribuído baseado em Redis (`ingestion_lock_key`) garante que apenas uma execução de ingestão por coleção rode por vez, evitando que duas execuções concorrentes corrompam o manifesto de checkpoint. O lock é renovado periodicamente (heartbeat) durante toda a duração de ingestões longas, para que nunca expire no meio da execução por TTL, mantendo ainda assim uma expiração segura caso o processo morra inesperadamente.
- **Limpeza de Órfãos** — quando um arquivo já indexado é alterado, seus chunks antigos só são removidos do Qdrant *depois* que os novos chunks são confirmados como commitados, evitando qualquer janela em que dados válidos fiquem ausentes do índice.

---

## Endpoints da API

| Método | Endpoint | Descrição |
|---|---|---|
| `POST` | `/v1/chat` | Executa o pipeline completo de Advanced RAG: embeda a pergunta, recupera e reordena os chunks de documentação, monta o prompt aumentado, e gera a resposta via o proxy LiteLLM. O histórico de conversa é mantido por `session_id` no Redis. Retorna `503` em falhas operacionais conhecidas (ex: LiteLLM/Ollama indisponível) e `500` em erros inesperados. |
| `POST` | `/v1/ingest/process-folder` | Dispara o motor de ETL Code-to-Doc como uma tarefa em background sobre um caminho de pasta **dentro do container**. Aceita `content_type` (`source_code` ou `documentation`), `project_source` (rótulo de proveniência, ex: `UE5_Engine`, `Lyra`), `sample_mode` (habilita os Canais C e D), e um `ue_version` opcional para sobrescrever a detecção automática. Retorna imediatamente com `202 Accepted` — a tarefa roda de forma assíncrona e o progresso é acompanhado pelos logs da aplicação e pelo manifesto no Redis. |
| `GET` | `/v1/health` | Health check ativo contra todas as dependências (Qdrant, Redis, LiteLLM), executado de forma concorrente. Retorna `200` se todas as dependências estiverem saudáveis ou degradadas, `503` se alguma dependência estiver em estado de erro — incluindo latência e detalhe de status por dependência. |

---

## Configuração de Ambiente

A configuração é centralizada em `app/core/config.py` via Pydantic Settings, carregada a partir de um arquivo `.env`. Principais variáveis:

```env
# --- Aplicação ---
APP_ENV=production               # production | development | test
LOG_LEVEL=INFO

# --- Qdrant ---
QDRANT_HOST=qdrant
QDRANT_PORT=6333
QDRANT_COLLECTION=ue5_docs

# --- Redis (sessões, manifesto, locks, quarentena) ---
REDIS_HOST=agent-redis
REDIS_PORT=6379
REDIS_PASSWORD=                  # obrigatória
REDIS_DB=0
REDIS_SESSION_TTL=3600           # TTL da sessão de conversa, em segundos

# --- LiteLLM Proxy (ponto único de entrada para todas as chamadas de modelo) ---
LITELLM_BASE_URL=http://litellm-proxy:4000
LITELLM_MASTER_KEY=              # obrigatória
LITELLM_MAX_RETRIES=4            # tentativas em caso de 429/500/502/503/504 ou timeout

# --- Embeddings ---
EMBEDDING_MODEL=ollama-embed     # deve estar configurado como modelo de embedding no config.yml do LiteLLM
EMBEDDING_DIM=1024               # um de: 384, 768, 1024, 1536, 3072

# --- RAG (geral) ---
RAG_TOP_K=5
RAG_SCORE_THRESHOLD=0.6          # usado apenas no Naive RAG — substituído pelo reranker no Advanced RAG

# --- Advanced RAG — Decomposição de Query ---
RAG_DECOMPOSITION_ENABLED=true
RAG_DECOMPOSITION_MODEL=gemini/gemini-3.1-flash-lite
RAG_DECOMPOSITION_TIMEOUT=12.0

# --- Advanced RAG — Expansão de Retrieval ---
RAG_RETRIEVAL_LIMIT_PER_QUERY=15

# --- Advanced RAG — Reranker FlashRank ---
RAG_RERANKER_ENABLED=true
RAG_RERANKER_MODEL=ms-marco-MiniLM-L-12-v2   # ou ms-marco-MultiBERT-L-12, rank-T5-flan
RAG_RERANKER_CACHE_DIR=/tmp/flashrank_cache
RAG_RERANK_MIN_SCORE=0.0

# --- Ingestão — ETL Code-to-Doc ---
INGESTION_DOCUMENTER_MODEL=gemini/gemini-2.0-flash-lite
INGESTION_BATCH_SIZE=32
INGESTION_EMBEDDING_CONCURRENCY=8
INGESTION_MAX_CHUNK_CHARS=3000
INGESTION_DEFAULT_UE_VERSION=5.8
INGESTION_LOCK_TIMEOUT_SECONDS=3600
INGESTION_MAX_HEADER_CHARS=12000
INGESTION_MAX_CONSECUTIVE_FAILURES=3
```

> **Nota:** `ollama_base_url` está deliberadamente ausente — o serviço FastAPI nunca chama o Ollama diretamente. Todo o tráfego de modelo, incluindo embeddings, é roteado através do proxy LiteLLM, que já tem o Ollama pré-configurado como um de seus backends.

---

## Instruções de Uso

> O projeto foi desenhado para rodar como parte de uma stack multi-container (serviço FastAPI + Qdrant + Redis + proxy LiteLLM), coordenada via Docker Compose.

### 1. Clone e configure

```bash
git clone <repository-url>
cd ue5-ai-assistant
cp .env.example .env
# Preencha REDIS_PASSWORD, LITELLM_MASTER_KEY e quaisquer chaves de API de
# provedor exigidas pelo seu config.yml do LiteLLM (ex: chave de API do Gemini).
```

### 2. Suba a stack

```bash
docker compose up -d --build
```

Isso deve subir, no mínimo: o serviço FastAPI `ue5-agent`, o `qdrant`, o `agent-redis`, e o `litellm-proxy` (com o Ollama acessível a partir da rede do proxy).

### 3. Verifique a saúde do sistema

```bash
curl http://localhost:8000/v1/health
```

### 4. Ingira documentação e código-fonte

```bash
curl -X POST http://localhost:8000/v1/ingest/process-folder \
  -H "Content-Type: application/json" \
  -d '{
        "folder_path": "/data/UnrealEngine/Engine/Source",
        "content_type": "source_code",
        "project_source": "UE5_Engine",
        "sample_mode": false
      }'
```

Para um projeto-sample de gameplay (habilita os Canais C e D):

```bash
curl -X POST http://localhost:8000/v1/ingest/process-folder \
  -H "Content-Type: application/json" \
  -d '{
        "folder_path": "/data/Lyra",
        "content_type": "source_code",
        "project_source": "Lyra",
        "sample_mode": true
      }'
```

A ingestão roda em background; acompanhe o progresso pelos logs da aplicação (nível `INFO`) e pelo manifesto de checkpoint no Redis.

### 5. Faça uma pergunta

```bash
curl -X POST http://localhost:8000/v1/chat \
  -H "Content-Type: application/json" \
  -d '{
        "message": "Como implemento um double jump em C++ e conecto isso a um Animation Blueprint?",
        "session_id": "5c1f2e3a-...-uuid-v4",
        "top_k": 5
      }'
```
