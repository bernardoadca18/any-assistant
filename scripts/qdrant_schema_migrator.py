#!/usr/bin/env python3
"""
qdrant_schema_migrator.py
==========================
Database Fixer & Schema Migrator — coleção `ue5_docs` (Qdrant).

ESCOPO REAL DESTE SCRIPT (confirmado lendo ingestion_service.py e
rag_service.py do próprio projeto):

    O `raw_content == None` em chunks com chunk_id != 0 NÃO é um bug.
    É dedup intencional (ingestion_service.py::_build_chunks_with_metadata)
    já compensado em runtime por rag_service.py::_backfill_raw_content,
    que reconstrói o valor a partir do chunk_id==0 do mesmo `source_file`
    no momento do retrieval. Este script NÃO toca em `raw_content` e NÃO
    usa tree-sitter — seria resolver, com escrita em produção, algo que já
    funciona.

    O que este script corrige é o drift de schema real, observável no
    payload atual:
        1. `doc_type` como string livre (ex: "C++ Source API")
           -> slug estável (ex: "cpp_header_api").
        2. Campo morto `description` (sempre "") -> removido.
        3. Campo `text` (sumário gerado por LLM) -> renomeado para
           `summary_markdown`, para não colidir semanticamente com
           `raw_content`.
        4. `project_source` / `source_file_root` ausentes em pontos
           legados -> preenchidos com um fallback explícito e sinalizado
           no log de auditoria (NUNCA inventados silenciosamente).
        5. `schema_version` adicionado (2) para permitir migrações
           futuras incrementais sem precisar re-varrer pontos já OK.

SEGURANÇA:
    - Usa exclusivamente `set_payload()` / `delete_payload()` do
      qdrant-client oficial. NUNCA `upsert()` — o vetor de nenhum ponto é
      lido, transmitido ou reescrito por este script.
    - Roda em modo --dry-run por padrão. Só grava no banco com --apply.
    - Todo ponto alterado (ou que falhou) é registrado em um log JSONL de
      auditoria, com valores antigos e novos, antes/depois da chamada.
    - Idempotente: rodar duas vezes não duplica trabalho — pontos já no
      schema v2 são pulados (comparação campo a campo, não é um filtro
      fixo no Qdrant, porque o drift observado é justamente
      inconsistência de quais campos existem).

IMPORTANTE — ação de código necessária além deste script:
    Este script só migra os DADOS. O código da aplicação ainda escreve e
    compara o schema v1. Sem atualizar o código, a próxima ingestão
    reintroduz o drift na hora e uma checagem existente quebra:

    - ingestion_service.py, linhas ~756/825/865/1061: literais
          doc_type="C++ Implementation (mechanic)"
          doc_type="C++ Source API"
          doc_type="Official Documentation (.udn)"
          doc_type="Blueprint Logic (.bp.json)"
      -> trocar pelos slugs de DOC_TYPE_SLUG_MAP abaixo.
    - ingestion_service.py, linha ~1229/1242: chunks ainda geram
          "text": section_text  e  "description": ""
      -> renomear "text" para "summary_markdown" e remover "description".
    - rag_service.py, linha ~1003:
          fence_lang = "cpp" if doc_type == "C++ Source API" else "markdown"
      -> comparar contra o slug novo ("cpp_header_api"), ou a formatação
         de bloco de código quebra silenciosamente (tudo passa a renderizar
         como markdown, mesmo C++).

    Recomendação: faça o deploy do código atualizado ANTES ou junto com
    --apply deste script, para não haver uma janela em que ingestões novas
    escrevam schema v1 sobre uma coleção já migrada para v2.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from qdrant_client import QdrantClient
from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
)
log = logging.getLogger("qdrant_schema_migrator")


# =============================================================================
# Schema v2 — mapeamento definitivo
# =============================================================================

# String livre atual (payload["doc_type"]) -> slug estável do schema v2.
# Mantenha em espelho com o que ingestion_service.py passa a escrever.
DOC_TYPE_SLUG_MAP: dict[str, str] = {
    "C++ Source API": "cpp_header_api",
    "C++ Implementation (mechanic)": "cpp_implementation",
    "Official Documentation (.udn)": "udn_documentation",
    "Blueprint Logic (.bp.json)": "blueprint_logic",
}

# Valores já aceitos como slug v2 (idempotência: se o ponto já tem um
# destes, doc_type não precisa de correção).
VALID_V2_DOC_TYPES: frozenset[str] = frozenset(DOC_TYPE_SLUG_MAP.values())

SCHEMA_VERSION = 2

# Fallback explícito para project_source ausente em pontos legados.
# Só é aplicado quando o campo está ausente/vazio — nunca sobrescreve um
# valor já presente, mesmo que "errado".
DEFAULT_PROJECT_SOURCE_FALLBACK = "UE5_Engine"


# =============================================================================
# Log de auditoria (JSONL) — um registro por ponto processado
# =============================================================================

@dataclass
class AuditRecord:
    point_id: str
    status: str  # "migrated" | "skipped_ok" | "failed" | "dry_run"
    fields_changed: list[str] = field(default_factory=list)
    old_values: dict[str, Any] = field(default_factory=dict)
    new_values: dict[str, Any] = field(default_factory=dict)
    keys_deleted: list[str] = field(default_factory=list)
    error: str | None = None
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class AuditLog:
    """Escreve um JSONL append-only. Nunca sobrescreve execuções anteriores."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a", encoding="utf-8")
        log.info("Log de auditoria: %s", self.path.resolve())

    def write(self, record: AuditRecord) -> None:
        self._fh.write(json.dumps(record.__dict__, ensure_ascii=False, default=str) + "\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


# =============================================================================
# Cálculo do diff de migração (schema v1 -> v2), por ponto
# =============================================================================

@dataclass
class MigrationPlan:
    """O que precisa ser gravado/removido para levar um ponto ao schema v2."""

    to_set: dict[str, Any] = field(default_factory=dict)
    to_delete: list[str] = field(default_factory=list)
    changed_fields: list[str] = field(default_factory=list)
    old_values: dict[str, Any] = field(default_factory=dict)

    @property
    def is_noop(self) -> bool:
        return not self.to_set and not self.to_delete


def plan_migration(payload: dict[str, Any]) -> MigrationPlan:
    """
    Compara o payload atual contra o schema v2 e retorna só as mudanças
    necessárias. Não modifica `payload` — é side-effect free, o que
    permite testar isoladamente (ver seção de testes na documentação).

    Regras de idempotência: um ponto já migrado (schema_version == 2 e
    nenhum campo v1 remanescente) resulta em MigrationPlan vazio.
    """
    plan = MigrationPlan()

    # --- 1. doc_type -> slug -------------------------------------------------
    doc_type = payload.get("doc_type")
    if isinstance(doc_type, str) and doc_type not in VALID_V2_DOC_TYPES:
        slug = DOC_TYPE_SLUG_MAP.get(doc_type)
        if slug is not None:
            plan.to_set["doc_type"] = slug
            plan.changed_fields.append("doc_type")
            plan.old_values["doc_type"] = doc_type
        else:
            # doc_type desconhecido — não inventamos um slug. Fica registrado
            # para revisão manual (ver AuditRecord.error no relatório final).
            log.warning(
                "doc_type desconhecido, sem mapeamento em DOC_TYPE_SLUG_MAP: %r "
                "(point_id será marcado como failed para revisão manual)",
                doc_type,
            )
            raise ValueError(f"doc_type sem mapeamento conhecido: {doc_type!r}")

    # --- 2. description morto -> remover ------------------------------------
    if "description" in payload:
        plan.to_delete.append("description")
        plan.changed_fields.append("description (removido)")
        plan.old_values["description"] = payload.get("description")

    # --- 3. text -> summary_markdown ----------------------------------------
    if "text" in payload and "summary_markdown" not in payload:
        plan.to_set["summary_markdown"] = payload["text"]
        plan.to_delete.append("text")
        plan.changed_fields.append("text -> summary_markdown")
        plan.old_values["text"] = payload["text"]
    elif "text" in payload and "summary_markdown" in payload:
        # Já existe summary_markdown (execução anterior parcial?) — só
        # garante que o campo antigo "text" seja removido, sem sobrescrever
        # summary_markdown (que pode já ter sido editado manualmente).
        plan.to_delete.append("text")
        plan.changed_fields.append("text (removido, summary_markdown já existia)")
        plan.old_values["text"] = payload["text"]

    # --- 4. project_source obrigatório ---------------------------------------
    project_source = payload.get("project_source")
    if not project_source:
        plan.to_set["project_source"] = DEFAULT_PROJECT_SOURCE_FALLBACK
        plan.changed_fields.append("project_source (fallback aplicado)")
        plan.old_values["project_source"] = project_source

    # --- 5. source_file_root obrigatório -------------------------------------
    source_file_root = payload.get("source_file_root")
    if not source_file_root:
        # Fallback para source_file, igual ao default já usado em
        # ingestion_service.py::_build_chunks_with_metadata para Canais A/B.
        source_file = payload.get("source_file")
        if source_file:
            plan.to_set["source_file_root"] = source_file
            plan.changed_fields.append("source_file_root (derivado de source_file)")
            plan.old_values["source_file_root"] = source_file_root
        else:
            raise ValueError(
                "Ponto sem source_file_root E sem source_file — "
                "impossível derivar um fallback seguro."
            )

    # --- 6. schema_version ----------------------------------------------------
    if payload.get("schema_version") != SCHEMA_VERSION:
        plan.to_set["schema_version"] = SCHEMA_VERSION
        plan.changed_fields.append("schema_version")
        plan.old_values["schema_version"] = payload.get("schema_version")

    return plan


# =============================================================================
# Migrador — orquestra scroll + set_payload/delete_payload + auditoria
# =============================================================================

def _is_retryable_qdrant_error(exc: BaseException) -> bool:
    if isinstance(exc, ResponseHandlingException):
        return True
    if isinstance(exc, UnexpectedResponse):
        # 429/5xx são transitórios; 4xx de payload malformado não é.
        return exc.status_code in (429, 500, 502, 503, 504)
    return False


class SchemaMigratorV2:
    """
    Orquestra a migração completa de uma coleção.

    Plugável para outras stacks (Godot/C#, backends diversos): a lógica
    específica de UE5 mora inteiramente em DOC_TYPE_SLUG_MAP e em
    plan_migration(). Para reusar este orquestrador com outra base, basta
    trocar essas duas peças — scroll, retry, dry-run e auditoria são
    genéricos.
    """

    def __init__(
        self,
        client: QdrantClient,
        collection_name: str,
        audit_log: AuditLog,
        dry_run: bool = True,
    ) -> None:
        self.client = client
        self.collection_name = collection_name
        self.audit_log = audit_log
        self.dry_run = dry_run
        self.stats = {"migrated": 0, "skipped_ok": 0, "failed": 0, "total": 0}

    # -- scroll -----------------------------------------------------------

    def iter_points(self, batch_size: int, limit: int | None = None) -> Iterator[Any]:
        """
        Varre a coleção inteira via scroll(). Não pede vetores
        (with_vectors=False) — este script nunca lê nem escreve vetores,
        então não há motivo para trafegar esses bytes.
        """
        offset = None
        yielded = 0
        while True:
            records, next_offset = self.client.scroll(
                collection_name=self.collection_name,
                limit=batch_size,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            if not records:
                return
            for record in records:
                yield record
                yielded += 1
                if limit is not None and yielded >= limit:
                    return
            if next_offset is None:
                return
            offset = next_offset

    # -- apply --------------------------------------------------------------

    @retry(
        retry=retry_if_exception_type((ResponseHandlingException, UnexpectedResponse)),
        wait=wait_exponential_jitter(initial=1, max=20),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    def _apply_plan(self, point_id: str, plan: MigrationPlan) -> None:
        """
        Grava a mudança no Qdrant. set_payload() e delete_payload() fazem
        merge parcial — NUNCA tocam no vetor nem em campos não listados.
        A ordem (set antes de delete) garante que, se o processo morrer
        entre as duas chamadas, o ponto fica com campo novo E antigo
        presentes (estado detectável e re-migrável), nunca sem nenhum dos
        dois.
        """
        if plan.to_set:
            self.client.set_payload(
                collection_name=self.collection_name,
                payload=plan.to_set,
                points=[point_id],
                wait=True,
            )
        if plan.to_delete:
            self.client.delete_payload(
                collection_name=self.collection_name,
                keys=plan.to_delete,
                points=[point_id],
                wait=True,
            )

    # -- loop principal -------------------------------------------------------

    def run(self, batch_size: int = 64, limit: int | None = None, sleep_between: float = 0.0) -> dict:
        mode = "DRY-RUN (nada será gravado)" if self.dry_run else "APPLY (gravando no Qdrant)"
        log.info("Iniciando migração da coleção '%s' — modo: %s", self.collection_name, mode)

        for record in self.iter_points(batch_size=batch_size, limit=limit):
            self.stats["total"] += 1
            point_id = str(record.id)
            payload = record.payload or {}

            try:
                plan = plan_migration(payload)
            except ValueError as exc:
                self.stats["failed"] += 1
                self.audit_log.write(
                    AuditRecord(point_id=point_id, status="failed", error=str(exc))
                )
                log.error("point_id=%s FALHOU no planejamento: %s", point_id, exc)
                continue

            if plan.is_noop:
                self.stats["skipped_ok"] += 1
                self.audit_log.write(AuditRecord(point_id=point_id, status="skipped_ok"))
                continue

            if self.dry_run:
                self.stats["migrated"] += 1
                self.audit_log.write(
                    AuditRecord(
                        point_id=point_id,
                        status="dry_run",
                        fields_changed=plan.changed_fields,
                        old_values=plan.old_values,
                        new_values=plan.to_set,
                        keys_deleted=plan.to_delete,
                    )
                )
                log.info("point_id=%s [DRY-RUN] mudanças: %s", point_id, plan.changed_fields)
                continue

            try:
                self._apply_plan(point_id, plan)
            except Exception as exc:  # noqa: BLE001 — precisa capturar tudo para não travar o lote
                self.stats["failed"] += 1
                self.audit_log.write(
                    AuditRecord(point_id=point_id, status="failed", error=repr(exc))
                )
                log.error("point_id=%s FALHOU ao aplicar: %r", point_id, exc)
                continue

            self.stats["migrated"] += 1
            self.audit_log.write(
                AuditRecord(
                    point_id=point_id,
                    status="migrated",
                    fields_changed=plan.changed_fields,
                    old_values=plan.old_values,
                    new_values=plan.to_set,
                    keys_deleted=plan.to_delete,
                )
            )
            log.info("point_id=%s migrado: %s", point_id, plan.changed_fields)

            if sleep_between:
                time.sleep(sleep_between)

        return self.stats


# =============================================================================
# CLI
# =============================================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Migra o payload da coleção Qdrant do schema v1 para v2.",
    )
    parser.add_argument("--host", default="localhost", help="Host do Qdrant (padrão: localhost)")
    parser.add_argument("--port", type=int, default=6333, help="Porta REST do Qdrant (padrão: 6333)")
    parser.add_argument("--collection", default="ue5_docs", help="Nome da coleção (padrão: ue5_docs)")
    parser.add_argument("--batch-size", type=int, default=64, help="Tamanho do batch do scroll()")
    parser.add_argument("--limit", type=int, default=None, help="Limita a N pontos (útil para teste)")
    parser.add_argument(
        "--sleep-between", type=float, default=0.0,
        help="Segundos de espera entre updates (throttle manual, se necessário)",
    )
    parser.add_argument(
        "--log-path", default="./migration_audit.jsonl",
        help="Caminho do log de auditoria JSONL (append-only)",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Grava as mudanças de fato no Qdrant. Sem esta flag, roda em dry-run.",
    )
    args = parser.parse_args()

    client = QdrantClient(host=args.host, port=args.port, timeout=30.0)

    try:
        client.get_collection(args.collection)
    except Exception as exc:
        log.error("Não foi possível acessar a coleção '%s' em %s:%d — %s",
                   args.collection, args.host, args.port, exc)
        return 1

    if args.apply:
        confirm = input(
            f"\n>>> Isso vai GRAVAR mudanças de payload na coleção '{args.collection}' "
            f"em {args.host}:{args.port}. Vetores não são tocados, mas esta ação não é "
            f"reversível automaticamente (embora fique tudo no log de auditoria). "
            f"Digite 'sim' para confirmar: "
        )
        if confirm.strip().lower() != "sim":
            log.info("Cancelado pelo usuário.")
            return 0

    audit_log = AuditLog(Path(args.log_path))
    migrator = SchemaMigratorV2(
        client=client,
        collection_name=args.collection,
        audit_log=audit_log,
        dry_run=not args.apply,
    )

    try:
        stats = migrator.run(
            batch_size=args.batch_size,
            limit=args.limit,
            sleep_between=args.sleep_between,
        )
    finally:
        audit_log.close()

    log.info(
        "Concluído | total=%d migrados=%d ok_sem_alteracao=%d falharam=%d",
        stats["total"], stats["migrated"], stats["skipped_ok"], stats["failed"],
    )
    if stats["failed"]:
        log.warning(
            "%d ponto(s) falharam — revise %s (status='failed') antes de considerar "
            "a migração completa.", stats["failed"], args.log_path,
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())