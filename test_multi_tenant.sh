#!/bin/bash
# =============================================================================
# Script de Teste - Multi-Tenancy Godot 4
# =============================================================================
# Este script testa a autenticação e configuração de tenant Godot 4
# =============================================================================

set -e

# Cores para output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

API_BASE="http://localhost:8000/v1"
API_KEY=""

echo -e "${YELLOW}========================================${NC}"
echo -e "${YELLOW}Teste Multi-Tenancy - Godot 4${NC}"
echo -e "${YELLOW}========================================${NC}\n"

# =============================================================================
# PASSO 1: Verificar se a API está saudável
# =============================================================================
echo -e "${YELLOW}[1/6] Verificando saúde da API...${NC}"
curl -s -f "$API_BASE/health" | jq . || {
    echo -e "${RED}✗ API não está respondendo${NC}"
    echo "  Execute: docker compose up -d"
    exit 1
}
echo -e "${GREEN}✓ API está saudável${NC}\n"

# =============================================================================
# PASSO 2: Criar tenant Godot 4 (requer auth de admin inicial)
# =============================================================================
echo -e "${YELLOW}[2/6] Criando tenant Godot 4...${NC}"
echo "  Nota: Em desenvolvimento, use X-Tenant-ID header para bypass de auth"

# Em desenvolvimento, podemos usar X-Tenant-ID para criar o primeiro tenant
# Ou usar o default tenant que já existe

# Verificar se tenant Godot 4 já existe
TENANT_CHECK=$(curl -s "$API_BASE/admin/tenants" \
  -H "X-Tenant-ID: 00000000-0000-0000-0000-000000000001" \
  -H "Content-Type: application/json" | jq -r '.items[] | select(.slug == "godot_4") | .id')

if [ -n "$TENANT_CHECK" ] && [ "$TENANT_CHECK" != "null" ]; then
    echo -e "${GREEN}✓ Tenant 'godot_4' já existe: $TENANT_CHECK${NC}\n"
    TENANT_ID=$TENANT_CHECK
else
    # Criar novo tenant
    TENANT_RESPONSE=$(curl -s -X POST "$API_BASE/admin/tenants" \
      -H "X-Tenant-ID: 00000000-0000-0000-0000-000000000001" \
      -H "Content-Type: application/json" \
      -d '{
        "slug": "godot_4",
        "name": "Godot 4 Documentation",
        "description": "Godot 4 engine documentation assistant"
      }')

    TENANT_ID=$(echo $TENANT_RESPONSE | jq -r '.id')

    if [ "$TENANT_ID" == "null" ] || [ -z "$TENANT_ID" ]; then
        echo -e "${RED}✗ Falha ao criar tenant${NC}"
        echo "$TENANT_RESPONSE" | jq .
        exit 1
    fi

    echo -e "${GREEN}✓ Tenant criado: $TENANT_ID${NC}\n"
fi

# =============================================================================
# PASSO 3: Criar API Key para o tenant Godot 4
# =============================================================================
echo -e "${YELLOW}[3/6] Criando API Key para tenant...${NC}"

KEY_RESPONSE=$(curl -s -X POST "$API_BASE/admin/tenants/$TENANT_ID/api-keys" \
  -H "X-Tenant-ID: 00000000-0000-0000-0000-000000000001" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "godot_4_main_key",
    "is_admin": false,
    "is_read_only": false
  }')

API_KEY=$(echo $KEY_RESPONSE | jq -r '.key')

if [ "$API_KEY" == "null" ] || [ -z "$API_KEY" ]; then
    echo -e "${RED}✗ Falha ao criar API key${NC}"
    echo "$KEY_RESPONSE" | jq .
    exit 1
fi

echo -e "${GREEN}✓ API Key criada: ${API_KEY:0:20}...${NC}"
echo -e "${YELLOW}  ⚠️  Salve esta chave! Ela não será mostrada novamente.${NC}\n"

# =============================================================================
# PASSO 4: Criar collection Godot 4 no Qdrant
# =============================================================================
echo -e "${YELLOW}[4/6] Criando collection Godot 4...${NC}"

COLLECTION_RESPONSE=$(curl -s -X POST "$API_BASE/admin/collections" \
  -H "X-Tenant-ID: 00000000-0000-0000-0000-000000000001" \
  -H "Content-Type: application/json" \
  -d '{
    "tenant_id": "'$TENANT_ID'",
    "name": "godot_4_docs",
    "qdrant_collection_name": "godot_4_docs_hybrid",
    "domain_id": "godot_4",
    "display_name": "Godot 4 Documentation",
    "description": "Godot 4 engine documentation and tutorials"
  }')

COLLECTION_ID=$(echo $COLLECTION_RESPONSE | jq -r '.id')

if [ "$COLLECTION_ID" == "null" ] || [ -z "$COLLECTION_ID" ]; then
    echo -e "${RED}✗ Falha ao criar collection${NC}"
    echo "$COLLECTION_RESPONSE" | jq .
    exit 1
fi

echo -e "${GREEN}✓ Collection criada: $COLLECTION_ID${NC}\n"

# =============================================================================
# PASSO 5: Testar autenticação com a API Key
# =============================================================================
echo -e "${YELLOW}[5/6] Testando autenticação com API Key...${NC}"

# Testar endpoint que requer autenticação
AUTH_TEST=$(curl -s -w "\n%{http_code}" "$API_BASE/chat/sessions" \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json")

HTTP_CODE=$(echo "$AUTH_TEST" | tail -n1)
RESPONSE_BODY=$(echo "$AUTH_TEST" | head -n -1)

if [ "$HTTP_CODE" == "200" ]; then
    echo -e "${GREEN}✓ Autenticação funcionando!${NC}"
    echo "  Sessions: $(echo $RESPONSE_BODY | jq -r '.total // 0')"
else
    echo -e "${RED}✗ Falha na autenticação (HTTP $HTTP_CODE)${NC}"
    echo "$RESPONSE_BODY" | jq .
    exit 1
fi
echo ""

# =============================================================================
# PASSO 6: Validar tenant via endpoint de health/info
# =============================================================================
echo -e "${YELLOW}[6/6] Validando configuração do tenant...${NC}"

# Listar tenants para confirmar
TENANTS=$(curl -s "$API_BASE/admin/tenants" \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json")

echo "$TENANTS" | jq '.items[] | select(.slug == "godot_4") | {id, slug, name, collections_count, api_keys_count}'

echo -e "\n${GREEN}========================================${NC}"
echo -e "${GREEN}✓ Setup completo!${NC}"
echo -e "${GREEN}========================================${NC}\n"

echo -e "${YELLOW}Tenant ID: ${NC}$TENANT_ID"
echo -e "${YELLOW}API Key:   ${NC}${API_KEY:0:20}... (salve completo)"
echo -e "${YELLOW}Collection: ${NC}$COLLECTION_ID"
echo ""
echo "Próximos passos:"
echo "  1. Use a API Key para fazer requisições:"
echo "     curl -H 'X-API-Key: $API_KEY' http://localhost:8000/api/v1/chat/sessions"
echo ""
echo "  2. Configure ingestão de documentos Godot 4"
echo ""
echo "  3. Teste chat com contexto do tenant:"
echo "     curl -X POST http://localhost:8000/api/v1/chat/message \\"
echo "       -H 'X-API-Key: $API_KEY' \\"
echo "       -H 'Content-Type: application/json' \\"
echo "       -d '{\"message\": \"How do I create a node in Godot 4?\"}'"
