# Guia de Execução — UE5 AI Assistant

## Pré-requisitos

- Stack LiteLLM rodando (`docker ps` mostra `litellm-proxy` na rede `litellm_public`)
- Qdrant com a coleção `ue5_docs` já ingerida (ou vazia para testes iniciais)
- Docker Engine ≥ 24 + Docker Compose v2

---

## Passo 1 — Variáveis de Ambiente

```bash
cd ue5-assistant/
cp .env.example .env
```

Edite o `.env` com valores reais:

```env
AGENT_REDIS_PASSWORD=uma_senha_forte_aqui
LITELLM_MASTER_KEY=sk-seu-master-key-do-litellm
OLLAMA_BASE_URL=http://host.docker.internal:11434
APP_ENV=development   # troca para production quando estiver satisfeito
LOG_LEVEL=DEBUG
```

> **Por que `host.docker.internal`?**  
> O Ollama roda nativamente na sua máquina. Dentro do container, `host.docker.internal`
> resolve para o IP do host via a diretiva `extra_hosts` no compose.

---

## Passo 2 — Verificar a Rede do LiteLLM

Confirme que a rede `litellm_public` existe antes de subir:

```bash
docker network ls | grep litellm_public
```

Saída esperada:
```
<id>   litellm_public   bridge   local
```

Se não aparecer, o stack do LiteLLM ainda não foi iniciado. Suba-o primeiro.

---

## Passo 3 — Subir a Stack do Assistente

```bash
# Produção (2 workers Uvicorn, sem hot-reload)
docker compose up -d --build

# Desenvolvimento (hot-reload ativo, logs coloridos)
docker compose -f docker-compose.yml up --build \
  --no-deps ue5-agent          # rebuild apenas do FastAPI se Qdrant/Redis já estão rodando
```

Aguardar os health checks passarem (~15s):

```bash
docker compose ps
# STATUS esperado: healthy para qdrant e agent-redis, running para ue5-agent
```

---

## Passo 4 — Validar a Stack

Execute o script de validação a partir do host (não dentro do container):

```bash
pip install httpx   # se não tiver
python scripts/validate_stack.py \
  --litellm-key sk-seu-master-key-do-litellm \
  --verbose
```

Saída esperada:
```
🚀 UE5 AI Assistant — Validação da Stack

=======================================================
  1. Qdrant (Vector DB)
=======================================================
  ✅ Qdrant acessível — HTTP 200
  ✅ Coleção 'ue5_docs' existe

=======================================================
  2. LiteLLM Proxy
=======================================================
  ✅ LiteLLM /health/readiness — HTTP 200
     Modelos disponíveis: ['ollama/qwen2.5-coder:7b', ...]

=======================================================
  3. FastAPI Agent (Health Check)
=======================================================
  ✅ FastAPI respondendo — HTTP 200
  ✅ Status geral — ok
  ✅   Dep: qdrant — ok | 2.3ms
  ✅   Dep: redis — ok | 0.8ms
  ✅   Dep: litellm — ok | 45.1ms

=======================================================
  4. FastAPI Chat Endpoint (Stub)
=======================================================
  ✅ POST /v1/chat respondeu — HTTP 200 | 12ms
  ✅ session_id preservado
  ✅ Campo 'answer' presente

🎉 Stack 100% operacional! Pronto para a Fase 2 (lógica RAG).
```

---

## Passo 5 — Validação Manual via cURL

### Health Check

```bash
curl -s http://localhost:8000/v1/health | python3 -m json.tool
```

### Chat (Stub — confirma roteamento)

```bash
curl -s -X POST http://localhost:8000/v1/chat \
  -H "Content-Type: application/json" \
  -d '{
    "message": "Como configurar o Lumen no UE5?",
    "session_id": "test-session-001",
    "model": "ollama/qwen2.5-coder:7b",
    "top_k": 5
  }' | python3 -m json.tool
```

### Confirmar que FastAPI alcança o LiteLLM (teste de rede interno)

```bash
# Entra no container do agente e testa DNS
docker exec ue5-agent-api curl -s http://litellm-proxy:4000/health/readiness
```

Se retornar JSON do LiteLLM, a bridge de rede está funcionando corretamente.

---

## Passo 6 — Documentação Interativa (apenas em development)

Com `APP_ENV=development`, acesse:
- **Swagger UI**: http://localhost:8000/docs
- **ReDoc**: http://localhost:8000/redoc
- **Qdrant Dashboard**: http://localhost:6333/dashboard

---

## Logs

```bash
# Todos os serviços
docker compose logs -f

# Apenas o agente FastAPI
docker compose logs -f ue5-agent

# Últimas 100 linhas do Qdrant
docker compose logs --tail=100 qdrant
```

---

## Reiniciar apenas o agente (após edições no código)

```bash
docker compose up -d --build ue5-agent
```

---

## Próximo Passo — Fase 2

Com a infraestrutura validada, os arquivos a implementar na Fase 2 são:

```
app/services/ai/embedding_service.py   # gera embedding via Ollama
app/services/ai/rag_service.py         # busca no Qdrant + monta prompt
app/services/cache/session_service.py  # lê/escreve histórico no Redis
app/api/v1/endpoints/chat.py           # substitui o stub pela lógica real
```