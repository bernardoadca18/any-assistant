"""
app/db/postgres.py
===================
Gerenciamento do ciclo de vida do engine Postgres (SQLAlchemy async) —
fonte de verdade durável do histórico de chat/mensagens/chunks citados.

Segue o mesmo padrão de app/db/redis.py: singleton inicializado no
lifespan do FastAPI, dependency para uso nos endpoints.

Divisão de responsabilidades Postgres x Redis:
- Postgres: fonte de verdade. Toda sessão e mensagem é gravada aqui,
  sempre. É o que o frontend carrega ao abrir a aplicação.
- Redis (agent-redis, ver app/db/redis.py): cache de curto prazo do
  histórico recente de uma sessão, usado para montar o prompt do LLM
  rapidamente sem bater no Postgres a cada turno. Tem TTL e pode ser
  reconstruído a partir do Postgres se expirar (ver
  app/services/session_service.py::warm_redis_from_postgres).
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import settings

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------
_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


async def init_postgres() -> AsyncEngine:
    """
    Cria o engine async do Postgres e valida a conectividade com um
    SELECT 1. Deve ser chamado no lifespan startup do FastAPI, depois
    do Redis e antes dos clientes LiteLLM (ordem não é crítica aqui,
    mas mantemos consistente com o restante do startup).

    Raises:
        ConnectionError: se o Postgres não responder
    """
    global _engine, _session_factory

    log.info(
        "Conectando ao Postgres em %s:%d/%s...",
        settings.postgres_host,
        settings.postgres_port,
        settings.postgres_db,
    )

    engine = create_async_engine(
        settings.database_url,
        echo=settings.is_development,
        pool_size=10,
        max_overflow=5,
        pool_pre_ping=True,   # evita erros com conexões mortas em pool ocioso
        pool_recycle=1800,
    )

    try:
        async with engine.connect() as conn:
            await conn.exec_driver_sql("SELECT 1")
        log.info("Postgres conectado com sucesso.")
    except SQLAlchemyError as exc:
        raise ConnectionError(
            f"Não foi possível conectar ao Postgres em "
            f"{settings.postgres_host}:{settings.postgres_port}: {exc}"
        ) from exc

    _engine = engine
    _session_factory = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    return engine


async def close_postgres() -> None:
    """Fecha o engine Postgres (dispose do pool). Chamado no lifespan shutdown."""
    global _engine, _session_factory
    if _engine:
        await _engine.dispose()
        _engine = None
        _session_factory = None
        log.info("Conexão com Postgres encerrada.")


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        raise RuntimeError(
            "Postgres session factory não inicializada. "
            "Verifique se o lifespan do FastAPI está configurado corretamente."
        )
    return _session_factory


async def get_db_session() -> AsyncSession:  # pragma: no cover - dependency generator
    """
    FastAPI Dependency — entrega uma AsyncSession por request e garante
    commit/rollback/close corretos.

    Uso:
        async def my_endpoint(db: AsyncSession = Depends(get_db_session)):
            ...
    """
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise