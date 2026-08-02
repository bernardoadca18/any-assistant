# Guia Completo de Execução - UE5 AI Assistant

## Visão Geral

Este guia descreve como configurar e executar o UE5 AI Assistant, um sistema multi-tenant de assistência técnica para Unreal Engine 5 e Godot 4, construído sobre um pipeline avançado de RAG (Retrieval-Augmented Generation).

---

## Arquitetura do Sistema

O sistema é composto por:

- **Backend FastAPI** - API assíncrona com multi-tenancy
- **Frontend React/Vite** - Interface web para chat e administração
- **LiteLLM Proxy** - Gateway unificado para todos os modelos LLM
- **Qdrant** - Banco de dados vetorial
- **PostgreSQL** - Persistência de dados multi-tenant
- **Redis** - Cache e gerenciamento de sessões

---

## Pré-requisitos

### Software Necessário

- Docker e Docker Compose
- Node.js 18+ (para desenvolvimento do frontend)
- Python 3.11+ (para desenvolvimento do backend)
- Git

### Portas Requeridas

- `8000` - Backend API
- `5173` - Frontend (desenvolvimento)
- `4000` - LiteLLM Proxy
- `6333` - Qdrant
- `5432` - PostgreSQL
- `6379` - Redis

---

## Configuração do Ambiente

### 1. Clone os Repositórios

```bash
# Repositório principal
git clone <url-do-repositorio>
cd unreal-engine-pocket-assistant

# LiteLLM Proxy (em diretório separado)
git clone <url-do-litellm-proxy> ../litellm-proxy
```

### 2. Configure o LiteLLM Proxy

```bash
cd ../litellm-proxy

# Copie o arquivo de exemplo
cp .env.example .env

# Edite o .env com suas configurações:
# - LITELLM_MASTER_KEY: Chave mestre do proxy
# - GEMINI_API_KEY_1-11: Chaves da API do Gemini
# - OPENROUTER_API_KEY_1-11: Chaves da API do OpenRouter
# - POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_DB: Credenciais do banco
# - REDIS_PASSWORD: Senha do Redis
```

### 3. Configure o Backend

```bash
cd ../unreal-engine-pocket-assistant

# Copie o arquivo de exemplo
cp .env.example .env

# Configure as variáveis essenciais:
cat > .env << 'EOF'
# Aplicação
APP_ENV=development
LOG_LEVEL=INFO

# LiteLLM Proxy
LITELLM_BASE_URL=http://litellm-proxy:4000
LITELLM_MASTER_KEY=sk-seu-master-key-aqui

# PostgreSQL
POSTGRES_HOST=agent-postgres
POSTGRES_PORT=5432
POSTGRES_USER=ue5rag
POSTGRES_PASSWORD=sua-senha-segura
POSTGRES_DB=ue5_rag_assistant

# Redis
REDIS_HOST=agent-redis
REDIS_PORT=6379
REDIS_PASSWORD=sua-senha-redis

# Qdrant
QDRANT_HOST=qdrant
QDRANT_PORT=6333

# Domínio ativo (godot_4 ou ue5)
ACTIVE_DOMAIN=godot_4
EOF
```

### 4. Configure o Frontend

```bash
cd frontend

# Copie o arquivo de exemplo
cp .env.example .env

# Configure:
cat > .env << 'EOF'
VITE_API_BASE_URL=http://localhost:8000/v1
VITE_QDRANT_DASHBOARD_URL=http://localhost:6333/dashboard
VITE_LITELLM_PROXY_URL=http://localhost:4000
EOF
```

---

## Execução

### Opção 1: Docker Compose (Recomendado para Produção)

```bash
# Na raiz do projeto
docker-compose up -d

# Verifique os logs
docker-compose logs -f backend
```

### Opção 2: Desenvolvimento Local

#### Backend

```bash
# Crie o ambiente virtual
python -m venv .venv
source .venv/bin/activate  # Linux/Mac
# ou
.\.venv\Scripts\activate  # Windows

# Instale as dependências
pip install -r requirements.txt

# Execute as migrações do banco
# (se necessário, configure Alembic)

# Execute o servidor
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

#### Frontend

```bash
cd frontend

# Instale as dependências
npm install
# ou
bun install

# Execute o servidor de desenvolvimento
npm run dev
# ou
bun run dev
```

#### LiteLLM Proxy

```bash
cd ../litellm-proxy

# Execute via Docker Compose
docker-compose up -d litellm postgres redis

# Verifique se está rodando
curl http://localhost:4000/health
```

---

## Inicialização do Banco de Dados

### Criar Tenant Padrão

O sistema requer um tenant padrão para funcionar. Execute o script SQL:

```sql
-- Conecte ao PostgreSQL
psql -U ue5rag -d ue5_rag_assistant

-- Crie o tenant padrão
INSERT INTO tenants (id, slug, name, description, is_active)
VALUES (
    gen_random_uuid(),
    'default',
    'Tenant Padrão',
    'Tenant padrão para desenvolvimento',
    true
) ON CONFLICT (slug) DO NOTHING;

-- Verifique se foi criado
SELECT * FROM tenants WHERE slug = 'default';
```

### Criar API Key Admin

```sql
-- Gere uma API key (use bcrypt para hash)
-- Exemplo: sk-test-admin-key-123456

INSERT INTO api_keys (id, tenant_id, name, key_prefix, key_hash, is_admin, is_active)
SELECT 
    gen_random_uuid(),
    id,
    'Admin Key',
    'sk-test',
    '$2b$12$hash-bcrypt-da-chave-aqui',
    true,
    true
FROM tenants WHERE slug = 'default';
```

---

## Uso do Sistema

### 1. Acesse o Frontend

Abra o navegador em: `http://localhost:5173`

### 2. Faça Login

Use a API Key criada anteriormente:
- **API Key**: `sk-test-admin-key-123456`
- **Tenant**: `default`

### 3. Configure o Tenant

Após o login, acesse o menu de configurações para:
- Selecionar o domínio ativo (Godot 4 ou UE5)
- Criar coleções Qdrant
- Gerenciar configurações de RAG

### 4. Crie uma Coleção

1. Acesse **Collection Manager**
2. Clique em **Nova Coleção**
3. Preencha os campos:
   - **Nome (interno)**: `godot4_docs`
   - **Qdrant Collection Name**: `godot4_docs_v1`
   - **Domain ID**: `godot_4`
   - **Display Name**: `Godot 4.x Documentação`
   - **Descrição**: Documentação oficial do Godot 4

### 5. Inicie o Chat

1. Acesse **Chat Workspace**
2. Selecione o modelo no botão **Parâmetros**
3. Configure `top_k` (padrão: 5)
4. Digite sua pergunta sobre Godot 4 ou UE5

---

## Endpoints da API

### Autenticação

```bash
# Health check
GET /v1/health

# Login com API Key
POST /v1/auth/login
{
  "apiKey": "sk-test-admin-key-123456",
  "tenant_slug": "default"
}
```

### Chat

```bash
# Listar modelos disponíveis
GET /v1/chat/models

# Enviar mensagem (streaming)
POST /v1/chat
{
  "session_id": "uuid-da-sessao",
  "message": "Como criar um personagem em Godot 4?",
  "model": "gemini-3.5-flash-lite",
  "top_k": 5,
  "stream": true,
  "domain_id": "godot_4"
}
```

### Administração

```bash
# Listar tenants
GET /v1/admin/tenants

# Criar tenant
POST /v1/admin/tenants
{
  "slug": "empresa-x",
  "name": "Empresa X",
  "description": "Tenant da Empresa X"
}

# Listar coleções
GET /v1/admin/collections?tenant_id={uuid}

# Criar coleção
POST /v1/admin/collections
{
  "tenant_id": "uuid-do-tenant",
  "name": "docs_collection",
  "qdrant_collection_name": "docs_v1",
  "domain_id": "godot_4",
  "display_name": "Documentação"
}
```

---

## Modelos Disponíveis

O sistema acessa modelos através do LiteLLM Proxy. Modelos configurados:

### Gemini (via Google AI)
- `gemini-3.6-flash` - Respostas rápidas
- `gemini-3.1-pro` - Maior capacidade
- `gemini-3.5-flash-lite` - Decomposição de queries

### OpenRouter
- `openrouter-gemma4-26b` - Google Gemma 4
- `openrouter-nemotron-super` - NVIDIA Nemotron
- `openrouter-laguna-m1` - Poolside Laguna

### Ollama (local)
- Configure no `litellm-proxy/config/litellm/config.yml`

---

## Solução de Problemas

### Erro: "Tenant não encontrado"

**Causa**: O tenant não foi criado no banco de dados.

**Solução**:
```sql
INSERT INTO tenants (slug, name, is_active)
VALUES ('default', 'Tenant Padrão', true);
```

### Erro: "body.tenant_id: Input should be a valid UUID"

**Causa**: O frontend está enviando o slug do tenant em vez do UUID.

**Solução**: O código já foi corrigido para resolver o UUID a partir do slug. Certifique-se de usar a versão mais recente.

### Erro: "LiteLLM não responde"

**Causa**: O proxy não está rodando ou a chave está incorreta.

**Solução**:
```bash
# Verifique se o LiteLLM está rodando
docker ps | grep litellm

# Verifique os logs
docker logs litellm-proxy

# Teste a conexão
curl -H "Authorization: Bearer sua-master-key" http://localhost:4000/health
```

### Erro: "Modelo não disponível"

**Causa**: O modelo não está configurado no LiteLLM.

**Solução**:
1. Edite `/litellm-proxy/config/litellm/config.yml`
2. Adicione o modelo desejado
3. Reinicie o container: `docker restart litellm-proxy`

### Frontend não carrega modelos

**Causa**: Problema de CORS ou autenticação.

**Solução**:
1. Verifique se o backend está rodando
2. Confirme se a API Key está correta
3. Verifique os headers de autenticação no navegador (DevTools → Network)

---

## Estrutura do Projeto

```
unreal-engine-pocket-assistant/
├── app/                      # Backend FastAPI
│   ├── api/v1/endpoints/     # Endpoints da API
│   │   ├── admin.py          # Administração (tenants, coleções)
│   │   ├── chat.py           # Chat e modelos
│   │   ├── ingest.py         # Ingestão de documentos
│   │   └── sessions.py       # Gerenciamento de sessões
│   ├── core/                 # Configurações e domínios
│   │   ├── config.py         # Settings
│   │   └── domains/          # Perfis de domínio (Godot/UE5)
│   ├── db/                   # Modelos ORM
│   ├── schemas/              # Schemas Pydantic
│   └── services/             # Lógica de negócio
│       └── ai/               # RAG, embeddings, ingestão
├── frontend/                 # Frontend React/Vite
│   ├── src/
│   │   ├── components/       # Componentes React
│   │   ├── services/         # Cliente API
│   │   ├── store/            # Estado global (Zustand)
│   │   ├── types/            # Tipos TypeScript
│   │   └── views/            # Páginas
│   └── package.json
├── docker-compose.yml        # Orquestração Docker
├── .env                      # Variáveis de ambiente
└── requirements.txt          # Dependências Python
```

---

## Configuração Multi-Tenant

O sistema suporta múltiplos tenants com isolamento completo:

### Criar Novo Tenant

```bash
curl -X POST http://localhost:8000/v1/admin/tenants \
  -H "Authorization: Bearer sua-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "slug": "empresa-y",
    "name": "Empresa Y",
    "description": "Tenant isolado para Empresa Y"
  }'
```

### Criar API Key para Tenant

```bash
curl -X POST http://localhost:8000/v1/admin/tenants/{tenant_id}/api-keys \
  -H "Authorization: Bearer sua-api-key-admin" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Chave de Produção",
    "is_admin": false,
    "is_read_only": false
  }'
```

---

## Monitoramento

### Verificar Saúde do Sistema

```bash
curl http://localhost:8000/v1/health
```

Resposta esperada:
```json
{
  "status": "ok",
  "version": "0.2.0",
  "environment": "development",
  "dependencies": {
    "qdrant": {"status": "ok", "latency_ms": 5.2},
    "redis": {"status": "ok", "latency_ms": 1.1},
    "postgres": {"status": "ok", "latency_ms": 2.3},
    "litellm": {"status": "ok", "latency_ms": 45.7}
  }
}
```

### Logs

```bash
# Backend
docker-compose logs -f backend

# LiteLLM
docker logs -f litellm-proxy

# Todos os serviços
docker-compose logs -f
```

---

## Segurança

### Boas Práticas

1. **Nunca commite arquivos `.env`** no repositório
2. **Use HTTPS** em produção
3. **Rotacione API keys** periodicamente
4. **Limite permissões** de cada API key
5. **Configure rate limiting** no tenant

### Configurar Rate Limiting

```sql
UPDATE tenants
SET rate_limit_rpm = 60, rate_limit_tpm = 100000
WHERE slug = 'empresa-x';
```

---

## Backup e Restore

### Backup do PostgreSQL

```bash
docker exec litellm-postgres pg_dump -U ue5rag ue5_rag_assistant > backup.sql
```

### Restore

```bash
cat backup.sql | docker exec -i litellm-postgres psql -U ue5rag ue5_rag_assistant
```

---

## Atualização

```bash
# Pare os containers
docker-compose down

# Atualize o código
git pull origin main

# Reconstrua as imagens
docker-compose build

# Inicie os containers
docker-compose up -d

# Execute migrações (se houver)
docker-compose exec backend alembic upgrade head
```

---

## Suporte

Para problemas e sugestões:
1. Consulte este guia
2. Verifique os logs do sistema
3. Abra uma issue no repositório

---

## Licença

Apache 2.0 - Veja o arquivo LICENSE para detalhes.
