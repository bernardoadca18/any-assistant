# =============================================================================
# Dockerfile — UE5 AI Assistant Frontend (React + Vite + TypeScript)
# Multi-stage build: "builder" → "production" | "development"
# =============================================================================

# --- Stage 1: Builder (instala dependências e compila) -----------------------
FROM node:22-alpine AS builder

WORKDIR /build

# Instala dependências do sistema necessárias
RUN apk add --no-cache curl

# Copia arquivos de dependências primeiro para aproveitar cache de layer
COPY frontend/package.json frontend/package-lock.json* ./

# Instala dependências (npm ci para builds reproduzíveis)
RUN npm ci --prefer-offline --no-audit

# Copia código fonte e configurações
COPY frontend/ .

# Argumentos de build para variáveis de ambiente
ARG VITE_API_BASE_URL=http://localhost:8000/v1
ARG VITE_QDRANT_DASHBOARD_URL=http://localhost:6333/dashboard
ARG VITE_LITELLM_PROXY_URL=http://localhost:4000

# Define variáveis de ambiente para o build
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL
ENV VITE_QDRANT_DASHBOARD_URL=$VITE_QDRANT_DASHBOARD_URL
ENV VITE_LITELLM_PROXY_URL=$VITE_LITELLM_PROXY_URL

# Build de produção
RUN npm run build


# --- Stage 2: Production (NGINX) --------------------------------------------
FROM nginx:1.27-alpine AS production

LABEL maintainer="UE5 AI Assistant"
LABEL description="React Frontend for UE5 AI Assistant RAG System"

# Usuário não-root por segurança
RUN addgroup -g 1001 -S appgroup && \
    adduser -u 1001 -S appuser -G appgroup

# Copia configuração customizada do NGINX
COPY --chown=appuser:appgroup frontend/nginx.conf /etc/nginx/nginx.conf

# Copia arquivos de build do estágio anterior
COPY --from=builder --chown=appuser:appgroup /build/dist /usr/share/nginx/html

# Cria diretório de logs e ajusta permissões
RUN mkdir -p /var/log/nginx && \
    chown -R appuser:appgroup /var/log/nginx && \
    chown -R appuser:appgroup /var/cache/nginx

EXPOSE 80

# Healthcheck via curl
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -sf http://localhost:80/health || exit 1

# Inicia NGINX em foreground
CMD ["nginx", "-g", "daemon off;"]


# --- Stage 3: Development (hot-reload) ---------------------------------------
FROM node:22-alpine AS development

LABEL maintainer="UE5 AI Assistant"
LABEL description="React Frontend for UE5 AI Assistant RAG System (Development)"

WORKDIR /app

# Instala dependências do sistema
RUN apk add --no-cache curl

# Copia arquivos de dependências
COPY package.json package-lock.json* ./

# Instala todas as dependências (incluindo devDependencies)
RUN npm ci --prefer-offline --no-audit

# Copia código fonte
COPY . .

# Variáveis de ambiente para desenvolvimento (serão sobrescritas pelo compose)
ENV VITE_API_BASE_URL=http://localhost:8000/v1
ENV VITE_QDRANT_DASHBOARD_URL=http://localhost:6333/dashboard
ENV VITE_LITELLM_PROXY_URL=http://localhost:4000

EXPOSE 5173

# Healthcheck via curl
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -sf http://localhost:5173/ || exit 1

# Servidor de desenvolvimento com hot-reload
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0", "--port", "5173"]
