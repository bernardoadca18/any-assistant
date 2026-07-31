"""
app/db/models.py
=================
Modelos SQLAlchemy ORM — mapeiam 1:1 o schema criado em
db/init/001_init_schema.sql. Não geram migrations automaticamente;
o schema é a fonte de verdade (script SQL), estes modelos são para
leitura/escrita via ORM no dia a dia da aplicação.

Se no futuro adotarem Alembic, usem `alembic revision --autogenerate`
tendo esse schema já aplicado como baseline (alembic stamp head).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ENUM, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# ENUMs — devem casar exatamente com os tipos criados no schema SQL
# (create_type=False: o tipo já existe, criado pelo script de init)
# ---------------------------------------------------------------------------

message_role_enum = ENUM(
    "user", "assistant", "system", name="message_role", create_type=False
)
message_status_enum = ENUM(
    "pending", "streaming", "success", "error", name="message_status", create_type=False
)


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str | None] = mapped_column(String(255), unique=True)
    display_name: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())

    sessions: Mapped[list["ChatSession"]] = relationship(back_populates="user")


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    # NOVO (multi-domain) — troca o UNIQUE simples de `code` por composto
    # (code, domain_id): permite o mesmo code existir em domínios distintos
    # sem colisão (ver db/init/002_domain_multi_tenant_schema.sql).
    domain_id: Mapped[str] = mapped_column(String(64), nullable=False, default="godot_4")

    __table_args__ = (UniqueConstraint("code", "domain_id", name="uq_projects_code_domain"),)


class DocType(Base):
    __tablename__ = "doc_types"

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    domain_id: Mapped[str] = mapped_column(String(64), nullable=False, default="godot_4")

    __table_args__ = (UniqueConstraint("code", "domain_id", name="uq_doc_types_code_domain"),)


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    # NOTA: este default só se aplica se ChatSession for instanciado sem
    # `title` explícito — get_or_create_session() (session_service.py)
    # SEMPRE passa o título resolvido de BaseDomainProfile.default_session_title,
    # então este valor é apenas uma rede de segurança neutra, não mais o
    # texto UE5 fixo de antes.
    title: Mapped[str] = mapped_column(String(160), nullable=False, default="Nova Conversa")
    # NOVO (multi-domain — ver db/init/002_domain_multi_tenant_schema.sql e
    # app/core/domains/registry.py). `default` do ORM só se aplica a INSERTs
    # feitos via SQLAlchemy sem valor explícito; a migração SQL já cobre o
    # backfill retroativo das sessões pré-existentes (todas 'ue5').
    domain_id: Mapped[str] = mapped_column(String(64), nullable=False, default="godot_4")
    default_model: Mapped[str] = mapped_column(String(120), nullable=False, default="ollama/qwen2.5-coder:7b")
    default_top_k: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=5)
    project_source_filter: Mapped[str | None] = mapped_column(String(64))
    is_archived: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())

    user: Mapped[User | None] = relationship(back_populates="sessions")
    messages: Mapped[list["ChatMessage"]] = relationship(
        back_populates="session", cascade="all, delete-orphan", order_by="ChatMessage.sequence_number"
    )

    __table_args__ = (CheckConstraint("default_top_k BETWEEN 1 AND 20"),)


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False
    )
    # NOVO (multi-domain) — denormalizado da sessão no momento da escrita
    # (ver docstring da migração 002 para o motivo de não depender de JOIN).
    domain_id: Mapped[str] = mapped_column(String(64), nullable=False, default="godot_4")
    role: Mapped[str] = mapped_column(message_role_enum, nullable=False)
    status: Mapped[str] = mapped_column(message_status_enum, nullable=False, default="success")
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    error_detail: Mapped[str | None] = mapped_column(Text)
    model_used: Mapped[str | None] = mapped_column(String(120))
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)
    request_payload: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    session: Mapped[ChatSession] = relationship(back_populates="messages")
    retrieved_chunks: Mapped[list["RetrievedChunk"]] = relationship(
        back_populates="message", cascade="all, delete-orphan", order_by="RetrievedChunk.rank_position"
    )

    __table_args__ = (UniqueConstraint("session_id", "sequence_number"),)


class RetrievedChunk(Base):
    __tablename__ = "retrieved_chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_messages.id", ondelete="CASCADE"), nullable=False
    )
    # NOVO (multi-domain) — denormalizado da mensagem, mesmo racional do
    # ChatMessage.domain_id acima.
    domain_id: Mapped[str] = mapped_column(String(64), nullable=False, default="godot_4")
    qdrant_chunk_id: Mapped[int] = mapped_column(Integer, nullable=False)
    project_id: Mapped[int | None] = mapped_column(SmallInteger, ForeignKey("projects.id"))
    doc_type_id: Mapped[int | None] = mapped_column(SmallInteger, ForeignKey("doc_types.id"))
    score: Mapped[float] = mapped_column(nullable=False)
    title: Mapped[str] = mapped_column(String(255), default="")
    source_file: Mapped[str] = mapped_column(Text, default="")
    ue_version: Mapped[str] = mapped_column(String(16), default="")
    summary_markdown: Mapped[str] = mapped_column(Text, default="")
    raw_content: Mapped[str] = mapped_column(Text, default="")
    rank_position: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    message: Mapped[ChatMessage] = relationship(back_populates="retrieved_chunks")
    project: Mapped[Project | None] = relationship()
    doc_type: Mapped[DocType | None] = relationship()

    __table_args__ = (
        UniqueConstraint("message_id", "rank_position"),
        CheckConstraint("score >= 0 AND score <= 1"),
    )