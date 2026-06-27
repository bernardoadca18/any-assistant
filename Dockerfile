# =============================================================================
# Dockerfile — UE5 AI Agent (FastAPI)
# Multi-stage build: "builder" → "production" | "development"
# =============================================================================

# --- Stage 1: Builder (instala dependências e compila) -----------------------
FROM python:3.12-slim AS builder

WORKDIR /build

# Instala dependências do sistema necessárias para compilação
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copia requirements primeiro para aproveitar cache de layer
COPY requirements.txt .

# Instala em diretório isolado para copiar no stage final
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# --- Stage 2: Production -----------------------------------------------------
FROM python:3.12-slim AS production

LABEL maintainer="UE5 AI Assistant"
LABEL description="FastAPI RAG Agent for Unreal Engine 5 documentation"

# Usuário não-root por segurança
RUN groupadd -r appuser && useradd -r -g appuser appuser

WORKDIR /app

# Dependências de runtime mínimas
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copia dependências instaladas do builder
COPY --from=builder /install /usr/local

# Copia código da aplicação
COPY --chown=appuser:appuser app/ ./app/

# Muda para usuário não-root
USER appuser

EXPOSE 8000

# Uvicorn com configurações de produção
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "2", \
     "--loop", "uvloop", \
     "--log-level", "info", \
     "--no-access-log"]


# --- Stage 3: Development (hot-reload) ---------------------------------------
FROM production AS development

USER root

# Instala dependências de desenvolvimento
RUN pip install --no-cache-dir watchfiles pytest pytest-asyncio httpx

USER appuser

# Hot-reload via watchfiles
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--reload", \
     "--reload-dir", "/app/app", \
     "--log-level", "debug"]