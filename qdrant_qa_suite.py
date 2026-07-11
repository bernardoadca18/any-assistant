"""
qdrant_qa_suite.py
==================

Suíte de QA assíncrona para validar integridade, filtros, recuperação
semântica e performance da coleção Qdrant "ue5_docs" (pipeline de
ingestão UE5 -> LiteLLM/Ollama embeddings -> Qdrant).

Estágios de validação:
    1. Sanidade de metadados (payload obrigatório + densidade textual)
    2. Filtros estritos de metadados (doc_type / source_file)
    3. Recuperação semântica (gold-standard queries + limiar de score)
    4. Benchmark de latência (sequencial + estresse concorrente)
    5. Relatório final (stdout + JSON opcional + Markdown opcional)

Uso rápido:
    python qdrant_qa_suite.py --host localhost --port 6333

Uso com embeddings reais via LiteLLM (recomendado para o estágio 3
ser realmente significativo — com o stub mock, os testes de filtro,
latência e sanidade continuam válidos, mas o teste semântico apenas
valida a plumbing, não a qualidade de recuperação):
    python qdrant_qa_suite.py --embedding-mode litellm \\
        --litellm-base-url http://localhost:4000 \\
        --litellm-master-key SEU_TOKEN \\
        --embedding-model ollama-embed

Dependências:
    pip install qdrant-client httpx

Integração com pytest (opcional):
    Todas as funções de teste (`test_*`) são `async def` puras que
    recebem (client, cfg, report) e podem ser chamadas diretamente
    de dentro de testes pytest-asyncio, ex.:

        @pytest.mark.asyncio
        async def test_metadata():
            cfg = SuiteConfig()
            async with make_client(cfg) as client:
                report = SuiteReport(started_at=now_iso())
                await test_metadata_sanity(client, cfg, report)
                assert report.failed == 0
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import random
import statistics
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

try:
    from qdrant_client import AsyncQdrantClient, models
except ImportError as exc:  # pragma: no cover
    print("ERRO: 'qdrant-client' não instalado. Rode: pip install qdrant-client", file=sys.stderr)
    raise

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None  # só é necessário no modo --embedding-mode litellm


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #

log = logging.getLogger("qdrant_qa_suite")


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%H:%M:%S",
    )


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Configuração
# --------------------------------------------------------------------------- #

REQUIRED_PAYLOAD_KEYS: tuple[str, ...] = ("text", "raw_content", "source_file", "title")


@dataclass
class SuiteConfig:
    """Configuração centralizada da suíte. Todos os campos têm defaults
    sensatos e podem ser sobrescritos via CLI ou variáveis de ambiente."""

    qdrant_host: str = os.getenv("QDRANT_HOST", "localhost")
    qdrant_port: int = int(os.getenv("QDRANT_PORT", "6333"))
    qdrant_grpc_port: int = int(os.getenv("QDRANT_GRPC_PORT", "6334"))
    prefer_grpc: bool = False
    collection: str = os.getenv("QDRANT_COLLECTION", "ue5_docs")

    embedding_dim: int = 1024

    sample_size: int = 50
    min_text_len: int = 50

    top_k: int = 5
    score_threshold: float = 0.70

    concurrency: int = 20
    stress_queries: int = 50
    latency_warn_ms: float = 500.0

    request_timeout_s: float = 15.0

    embedding_mode: str = "mock"  # "mock" | "litellm"
    litellm_base_url: str = os.getenv("LITELLM_BASE_URL", "http://localhost:4000")
    litellm_master_key: str = os.getenv("LITELLM_MASTER_KEY", "")
    embedding_model: str = os.getenv("EMBEDDING_MODEL", "ollama-embed")

    output_json: Optional[str] = None
    output_md: Optional[str] = None
    verbose: bool = False
    seed: int = 1337


@dataclass
class GoldQuery:
    """Um cenário de busca 'gold standard' simulando uma pergunta real
    de um desenvolvedor de jogos, com palavras-chave esperadas no
    título/texto do(s) chunk(s) relevantes."""

    query: str
    expected_keywords: tuple[str, ...]
    description: str = ""


GOLD_QUERIES: list[GoldQuery] = [
    GoldQuery(
        query="How to update character sprite color dynamically during runtime",
        expected_keywords=("PaperGroupedSpriteComponent", "Sprite", "Paper2D"),
        description="Tint/cor dinâmica de sprite em runtime",
    ),
    GoldQuery(
        query="How to focus the camera viewport onto an object using a shortcut",
        expected_keywords=("CameraTarget", "AnimationEditContext", "Focus", "Viewport"),
        description="Atalho de foco de câmera no viewport do editor",
    ),
    GoldQuery(
        query="How to detect overlap between two actors using collision components",
        expected_keywords=("Overlap", "Collision", "PrimitiveComponent"),
        description="Detecção de overlap/colisão entre atores",
    ),
    GoldQuery(
        query="How to replicate a variable over the network for multiplayer",
        expected_keywords=("Replicated", "Network", "RPC", "Replication"),
        description="Replicação de variáveis em rede/multiplayer",
    ),
]


# --------------------------------------------------------------------------- #
# Embeddings — stub mock (substituível) e cliente real via LiteLLM
# --------------------------------------------------------------------------- #

async def embed_query_mock(text: str, dim: int = 1024) -> list[float]:
    """STUB determinístico — SUBSTITUA por um client de embeddings real
    (LiteLLM/OpenAI/Ollama) para que o estágio 3 (recuperação semântica)
    seja significativo. Gera um vetor pseudo-aleatório normalizado a
    partir do hash do texto, apenas para exercitar a integração/plumbing
    sem depender de uma API externa.
    """
    seed = int(hashlib.sha256(text.encode("utf-8")).hexdigest(), 16) % (2**32)
    rng = random.Random(seed)
    vec = [rng.uniform(-1.0, 1.0) for _ in range(dim)]
    norm = sum(v * v for v in vec) ** 0.5 or 1.0
    return [v / norm for v in vec]


async def embed_query_litellm(text: str, cfg: SuiteConfig) -> list[float]:
    """Gera um embedding real chamando o proxy LiteLLM (endpoint
    OpenAI-compatible /embeddings), no mesmo formato usado pelo
    `embedding_service.py` do pipeline de ingestão."""
    if httpx is None:
        raise RuntimeError("httpx não instalado. Rode: pip install httpx")

    headers = {"Authorization": f"Bearer {cfg.litellm_master_key}"} if cfg.litellm_master_key else {}
    payload = {"model": cfg.embedding_model, "input": text}

    async with httpx.AsyncClient(
        base_url=cfg.litellm_base_url,
        headers=headers,
        timeout=httpx.Timeout(connect=5.0, read=60.0, write=10.0, pool=5.0),
    ) as client:
        resp = await client.post("/embeddings", json=payload)
        resp.raise_for_status()
        data = resp.json()

    try:
        vector = data["data"][0]["embedding"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"Resposta de embedding em formato inesperado: {data}") from exc

    if not vector or len(vector) != cfg.embedding_dim:
        raise RuntimeError(
            f"Dimensão de embedding inesperada: esperado {cfg.embedding_dim}, "
            f"recebido {len(vector) if vector else 0}."
        )
    return vector


def get_embedder(cfg: SuiteConfig) -> Callable[[str], Awaitable[list[float]]]:
    """Factory que retorna a função de embedding ativa conforme
    `cfg.embedding_mode`. Ponto único de substituição para plugar um
    client de embeddings real."""
    if cfg.embedding_mode == "litellm":
        return lambda text: embed_query_litellm(text, cfg)
    log.warning(
        "embedding_mode='mock': o estágio de recuperação semântica valida apenas "
        "a integração (plumbing), não a qualidade real de recall. Use "
        "--embedding-mode litellm para um teste semântico significativo."
    )
    return lambda text: embed_query_mock(text, cfg.embedding_dim)


# --------------------------------------------------------------------------- #
# Estruturas de resultado / relatório
# --------------------------------------------------------------------------- #

@dataclass
class TestResult:
    name: str
    passed: bool
    duration_ms: float
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class SuiteReport:
    started_at: str
    finished_at: str = ""
    results: list[TestResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    latency_stats: dict[str, Any] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def failed(self) -> int:
        return self.total - self.passed

    def add(self, result: TestResult) -> None:
        self.results.append(result)
        status = "PASS" if result.passed else "FAIL"
        log.info("[%s] %s (%.1fms) %s", status, result.name, result.duration_ms, result.message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)
        log.warning(message)


class Timer:
    """Context manager simples para medir duração em milissegundos."""

    def __enter__(self) -> "Timer":
        self._start = asyncio.get_event_loop().time()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.ms = (asyncio.get_event_loop().time() - self._start) * 1000.0


# --------------------------------------------------------------------------- #
# Cliente Qdrant
# --------------------------------------------------------------------------- #

@asynccontextmanager
async def make_client(cfg: SuiteConfig):
    client = AsyncQdrantClient(
        host=cfg.qdrant_host,
        port=cfg.qdrant_port,
        grpc_port=cfg.qdrant_grpc_port,
        prefer_grpc=cfg.prefer_grpc,
        timeout=cfg.request_timeout_s,
    )
    try:
        yield client
    finally:
        await client.close()


async def assert_collection_ready(client: AsyncQdrantClient, cfg: SuiteConfig) -> None:
    """Verifica que a coleção existe e reporta metadados básicos antes
    de iniciar os testes, falhando cedo com uma mensagem clara."""
    try:
        info = await client.get_collection(cfg.collection)
    except Exception as exc:
        raise RuntimeError(
            f"Não foi possível acessar a coleção '{cfg.collection}' em "
            f"{cfg.qdrant_host}:{cfg.qdrant_port}. Verifique se o Qdrant está no "
            f"ar e se o nome da coleção está correto. Erro original: {exc}"
        ) from exc

    points_count = getattr(info, "points_count", None)
    log.info(
        "Coleção '%s' OK | pontos=%s | status=%s",
        cfg.collection,
        points_count,
        getattr(info, "status", "?"),
    )


# --------------------------------------------------------------------------- #
# Estágio 1 — Sanidade de metadados
# --------------------------------------------------------------------------- #

async def _fetch_random_sample_ids(
    client: AsyncQdrantClient, cfg: SuiteConfig
) -> list[Any]:
    """Coleta todos os IDs da coleção (sem payload/vetores, portanto
    barato) e sorteia uma amostra verdadeiramente aleatória, em vez de
    depender da ordem de inserção do `scroll`."""
    ids: list[Any] = []
    next_offset = None
    batch_size = 2000

    while True:
        points, next_offset = await client.scroll(
            collection_name=cfg.collection,
            limit=batch_size,
            offset=next_offset,
            with_payload=False,
            with_vectors=False,
        )
        ids.extend(p.id for p in points)
        if next_offset is None:
            break

    if not ids:
        raise RuntimeError(f"A coleção '{cfg.collection}' está vazia.")

    rng = random.Random(cfg.seed)
    sample_size = min(cfg.sample_size, len(ids))
    return rng.sample(ids, sample_size)


async def test_metadata_sanity(
    client: AsyncQdrantClient, cfg: SuiteConfig, report: SuiteReport
) -> None:
    """Estágio 1: amostra aleatória de pontos e valida presença/densidade
    das chaves obrigatórias do payload."""
    name = "metadata_sanity"
    with Timer() as t:
        try:
            sample_ids = await _fetch_random_sample_ids(client, cfg)
            points = await client.retrieve(
                collection_name=cfg.collection,
                ids=sample_ids,
                with_payload=True,
                with_vectors=False,
            )
        except Exception as exc:
            report.add(TestResult(name, False, 0.0, f"Erro ao amostrar pontos: {exc}"))
            return

    missing_keys: dict[str, list[int]] = {}
    empty_keys: dict[str, list[int]] = {}
    low_density: list[int] = []

    for p in points:
        payload = p.payload or {}
        for key in REQUIRED_PAYLOAD_KEYS:
            if key not in payload:
                missing_keys.setdefault(key, []).append(p.id)
            elif payload[key] is None or (isinstance(payload[key], str) and not payload[key].strip()):
                empty_keys.setdefault(key, []).append(p.id)

        text_val = (payload.get("text") or "")
        if isinstance(text_val, str) and 0 < len(text_val.strip()) < cfg.min_text_len:
            low_density.append(p.id)

    problems = []
    for key, ids in missing_keys.items():
        problems.append(f"chave '{key}' ausente em {len(ids)}/{len(points)} pontos")
        report.warn(f"Payload malformado: chave obrigatória '{key}' ausente em pontos {ids[:5]}...")
    for key, ids in empty_keys.items():
        problems.append(f"chave '{key}' vazia em {len(ids)}/{len(points)} pontos")
        report.warn(f"Payload malformado: chave '{key}' vazia em pontos {ids[:5]}...")
    if low_density:
        problems.append(
            f"{len(low_density)}/{len(points)} pontos com 'text' abaixo de {cfg.min_text_len} chars"
        )
        report.warn(f"Baixa densidade textual em pontos: {low_density[:5]}...")

    passed = not problems
    message = "OK" if passed else "; ".join(problems)
    report.add(TestResult(
        name, passed, t.ms, message,
        details={"sample_size": len(points), "problems": problems},
    ))


# --------------------------------------------------------------------------- #
# Estágio 2 — Filtros estritos de metadados
# --------------------------------------------------------------------------- #

async def test_filter_doc_type(
    client: AsyncQdrantClient, cfg: SuiteConfig, report: SuiteReport
) -> None:
    """Garante que um filtro estrutural `doc_type == "C++ Source API"`
    não vaza resultados de outro `doc_type`."""
    name = "filter_doc_type"
    target = "C++ Source API"
    with Timer() as t:
        try:
            points, _ = await client.scroll(
                collection_name=cfg.collection,
                scroll_filter=models.Filter(
                    must=[models.FieldCondition(key="doc_type", match=models.MatchValue(value=target))]
                ),
                limit=200,
                with_payload=True,
                with_vectors=False,
            )
        except Exception as exc:
            report.add(TestResult(name, False, 0.0, f"Erro na busca filtrada: {exc}"))
            return

    leaked = [p.id for p in points if (p.payload or {}).get("doc_type") != target]
    passed = len(points) > 0 and not leaked
    message = (
        "OK" if passed
        else ("Nenhum resultado retornado para o filtro" if not points
              else f"{len(leaked)} pontos vazaram de outro doc_type")
    )
    report.add(TestResult(
        name, passed, t.ms, message,
        details={"returned": len(points), "leaked_ids": leaked[:10]},
    ))


async def test_filter_source_file(
    client: AsyncQdrantClient, cfg: SuiteConfig, report: SuiteReport
) -> None:
    """Garante que um filtro exato por `source_file` retorna apenas
    pontos daquele arquivo específico."""
    name = "filter_source_file"
    with Timer() as t:
        try:
            probe_points, _ = await client.scroll(
                collection_name=cfg.collection, limit=1, with_payload=True, with_vectors=False,
            )
            if not probe_points:
                report.add(TestResult(name, False, 0.0, "Coleção vazia, impossível testar filtro"))
                return
            target_path = probe_points[0].payload.get("source_file")

            points, _ = await client.scroll(
                collection_name=cfg.collection,
                scroll_filter=models.Filter(
                    must=[models.FieldCondition(key="source_file", match=models.MatchValue(value=target_path))]
                ),
                limit=500,
                with_payload=True,
                with_vectors=False,
            )
        except Exception as exc:
            report.add(TestResult(name, False, 0.0, f"Erro na busca filtrada: {exc}"))
            return

    leaked = [p.id for p in points if (p.payload or {}).get("source_file") != target_path]
    passed = len(points) > 0 and not leaked
    message = "OK" if passed else f"{len(leaked)} pontos vazaram de outro source_file"
    report.add(TestResult(
        name, passed, t.ms, message,
        details={"target_source_file": target_path, "returned": len(points), "leaked_ids": leaked[:10]},
    ))


# --------------------------------------------------------------------------- #
# Wrapper de busca — compatível com qdrant-client novo (query_points) e
# antigo (search, removido a partir da v1.10+ do client em favor da
# Query API unificada).
# --------------------------------------------------------------------------- #

async def vector_search(
    client: AsyncQdrantClient,
    cfg: SuiteConfig,
    vector: list[float],
    limit: int,
    with_payload: bool = True,
) -> list[Any]:
    """Executa uma busca por similaridade e retorna uma lista uniforme
    de `ScoredPoint`, abstraindo a diferença entre `query_points` (API
    atual, >=1.10) e `search` (API legada, mantida por compatibilidade
    com deployments antigos do qdrant-client)."""
    if hasattr(client, "query_points"):
        response = await client.query_points(
            collection_name=cfg.collection,
            query=vector,
            limit=limit,
            with_payload=with_payload,
        )
        return response.points
    return await client.search(  # type: ignore[attr-defined]
        collection_name=cfg.collection,
        query_vector=vector,
        limit=limit,
        with_payload=with_payload,
    )


# --------------------------------------------------------------------------- #
# Estágio 3 — Recuperação semântica (gold standard)
# --------------------------------------------------------------------------- #

def _keyword_hit(payload: dict[str, Any], keywords: tuple[str, ...]) -> bool:
    haystack = f"{payload.get('title', '')} {payload.get('text', '')}".lower()
    return any(kw.lower() in haystack for kw in keywords)


async def test_semantic_retrieval(
    client: AsyncQdrantClient,
    cfg: SuiteConfig,
    report: SuiteReport,
    embedder: Callable[[str], Awaitable[list[float]]],
) -> None:
    """Estágio 3: para cada gold-query, embeda a consulta, busca top_k
    no Qdrant e valida (a) score acima do limiar e (b) presença de
    alguma palavra-chave esperada no título/texto do(s) hit(s)."""
    for gq in GOLD_QUERIES:
        name = f"semantic::{gq.description or gq.query[:40]}"
        with Timer() as t:
            try:
                vector = await embedder(gq.query)
                hits = await vector_search(client, cfg, vector, limit=cfg.top_k, with_payload=True)
            except Exception as exc:
                report.add(TestResult(name, False, 0.0, f"Erro na busca semântica: {exc}"))
                continue

        if not hits:
            report.add(TestResult(name, False, t.ms, "Nenhum resultado retornado"))
            continue

        top_score = hits[0].score
        keyword_found = any(_keyword_hit(h.payload or {}, gq.expected_keywords) for h in hits)
        score_ok = top_score >= cfg.score_threshold

        passed = score_ok and keyword_found
        message = (
            f"top_score={top_score:.3f} (limiar={cfg.score_threshold}) | "
            f"keyword_match={'sim' if keyword_found else 'não'} | "
            f"top_title='{(hits[0].payload or {}).get('title', '?')}'"
        )
        report.add(TestResult(
            name, passed, t.ms, message,
            details={
                "query": gq.query,
                "top_score": top_score,
                "expected_keywords": gq.expected_keywords,
                "top_hits": [
                    {"id": h.id, "score": h.score, "title": (h.payload or {}).get("title")}
                    for h in hits
                ],
            },
        ))


# --------------------------------------------------------------------------- #
# Estágio 4 — Latência e estresse concorrente
# --------------------------------------------------------------------------- #

async def _timed_search(client: AsyncQdrantClient, cfg: SuiteConfig, vector: list[float]) -> float:
    """Executa uma busca única e retorna a latência em ms, com timeout
    explícito para detectar travamentos do container."""
    start = asyncio.get_event_loop().time()
    await asyncio.wait_for(
        vector_search(client, cfg, vector, limit=cfg.top_k, with_payload=False),
        timeout=cfg.request_timeout_s,
    )
    return (asyncio.get_event_loop().time() - start) * 1000.0


async def test_latency_sequential(
    client: AsyncQdrantClient,
    cfg: SuiteConfig,
    report: SuiteReport,
    embedder: Callable[[str], Awaitable[list[float]]],
    n_requests: int = 20,
) -> list[float]:
    """Estágio 4a: mede latência média de buscas simples, sequenciais."""
    name = "latency_sequential"
    vector = await embedder(GOLD_QUERIES[0].query)
    latencies: list[float] = []
    errors = 0

    with Timer() as t:
        for _ in range(n_requests):
            try:
                latencies.append(await _timed_search(client, cfg, vector))
            except asyncio.TimeoutError:
                errors += 1
                report.warn("Timeout em busca sequencial")
            except Exception as exc:
                errors += 1
                report.warn(f"Erro em busca sequencial: {exc}")

    avg = statistics.mean(latencies) if latencies else float("inf")
    passed = errors == 0 and avg <= cfg.latency_warn_ms
    message = (
        f"n={n_requests} avg={avg:.1f}ms erros={errors} "
        f"(limiar de alerta={cfg.latency_warn_ms}ms)"
    )
    report.add(TestResult(
        name, passed, t.ms, message,
        details={"latencies_ms": latencies, "errors": errors, "avg_ms": avg},
    ))
    return latencies


async def test_latency_concurrent_stress(
    client: AsyncQdrantClient,
    cfg: SuiteConfig,
    report: SuiteReport,
    embedder: Callable[[str], Awaitable[list[float]]],
) -> list[float]:
    """Estágio 4b: dispara `cfg.stress_queries` buscas concorrentes
    (limitadas por um semáforo de `cfg.concurrency`) para simular carga
    e detectar timeouts/erros sob concorrência."""
    name = "latency_concurrent_stress"
    vectors = [await embedder(gq.query) for gq in GOLD_QUERIES]
    semaphore = asyncio.Semaphore(cfg.concurrency)

    async def _bounded_search(i: int) -> tuple[Optional[float], Optional[str]]:
        vector = vectors[i % len(vectors)]
        async with semaphore:
            try:
                latency = await _timed_search(client, cfg, vector)
                return latency, None
            except asyncio.TimeoutError:
                return None, "timeout"
            except Exception as exc:
                return None, str(exc)

    with Timer() as t:
        outcomes = await asyncio.gather(
            *(_bounded_search(i) for i in range(cfg.stress_queries)),
            return_exceptions=False,
        )

    latencies = [lat for lat, err in outcomes if err is None]
    errors = [err for _, err in outcomes if err is not None]
    timeouts = sum(1 for e in errors if e == "timeout")

    for err in errors:
        report.warn(f"Falha sob concorrência: {err}")

    avg = statistics.mean(latencies) if latencies else float("inf")
    p95 = (
        statistics.quantiles(latencies, n=20)[18] if len(latencies) >= 20 else max(latencies, default=0.0)
    )
    passed = timeouts == 0 and len(errors) == 0

    message = (
        f"n={cfg.stress_queries} concorrência={cfg.concurrency} "
        f"avg={avg:.1f}ms p95={p95:.1f}ms erros={len(errors)} timeouts={timeouts} "
        f"wall_clock={t.ms:.1f}ms"
    )
    report.add(TestResult(
        name, passed, t.ms, message,
        details={
            "avg_ms": avg, "p95_ms": p95, "errors": len(errors),
            "timeouts": timeouts, "n": cfg.stress_queries, "concurrency": cfg.concurrency,
        },
    ))
    report.latency_stats.update({
        "sequential_avg_ms": report.latency_stats.get("sequential_avg_ms"),
        "concurrent_avg_ms": avg,
        "concurrent_p95_ms": p95,
        "concurrent_errors": len(errors),
        "concurrent_timeouts": timeouts,
    })
    return latencies


# --------------------------------------------------------------------------- #
# Estágio 5 — Relatório
# --------------------------------------------------------------------------- #

def render_terminal_summary(report: SuiteReport, cfg: SuiteConfig) -> str:
    lines = []
    lines.append("=" * 78)
    lines.append(" RELATÓRIO DE QA — QDRANT COLLECTION: %s" % cfg.collection)
    lines.append("=" * 78)
    lines.append(f" Início:   {report.started_at}")
    lines.append(f" Fim:      {report.finished_at}")
    lines.append(f" Total de testes: {report.total}")
    lines.append(f" Sucessos:        {report.passed}")
    lines.append(f" Falhas:          {report.failed}")
    if report.latency_stats:
        lines.append(f" Latência seq. média: {report.latency_stats.get('sequential_avg_ms', 'n/a')}")
        lines.append(f" Latência conc. média (p95): "
                      f"{report.latency_stats.get('concurrent_avg_ms', 'n/a')} "
                      f"({report.latency_stats.get('concurrent_p95_ms', 'n/a')})")
    lines.append("-" * 78)
    for r in report.results:
        icon = "✅" if r.passed else "❌"
        lines.append(f" {icon} {r.name:<45} {r.duration_ms:>7.1f}ms  {r.message}")
    if report.warnings:
        lines.append("-" * 78)
        lines.append(f" ⚠ {len(report.warnings)} alerta(s) de payload/latência:")
        for w in report.warnings[:20]:
            lines.append(f"   - {w}")
        if len(report.warnings) > 20:
            lines.append(f"   ... e mais {len(report.warnings) - 20} alerta(s)")
    lines.append("=" * 78)
    overall = "SUÍTE APROVADA ✅" if report.failed == 0 else "SUÍTE COM FALHAS ❌"
    lines.append(f" {overall}")
    lines.append("=" * 78)
    return "\n".join(lines)


def render_markdown_report(report: SuiteReport, cfg: SuiteConfig) -> str:
    lines = [
        f"# Relatório de QA — Qdrant `{cfg.collection}`",
        "",
        f"- **Início:** {report.started_at}",
        f"- **Fim:** {report.finished_at}",
        f"- **Total:** {report.total} | **Sucessos:** {report.passed} | **Falhas:** {report.failed}",
        "",
        "## Resultados",
        "",
        "| Status | Teste | Duração (ms) | Mensagem |",
        "|---|---|---|---|",
    ]
    for r in report.results:
        icon = "✅" if r.passed else "❌"
        msg = r.message.replace("|", "\\|")
        lines.append(f"| {icon} | {r.name} | {r.duration_ms:.1f} | {msg} |")

    if report.warnings:
        lines += ["", "## Alertas", ""]
        lines += [f"- {w}" for w in report.warnings]

    if report.latency_stats:
        lines += ["", "## Latência", ""]
        for k, v in report.latency_stats.items():
            lines.append(f"- **{k}:** {v}")

    return "\n".join(lines)


def write_reports(report: SuiteReport, cfg: SuiteConfig) -> None:
    if cfg.output_json:
        payload = {
            "collection": cfg.collection,
            "started_at": report.started_at,
            "finished_at": report.finished_at,
            "total": report.total,
            "passed": report.passed,
            "failed": report.failed,
            "latency_stats": report.latency_stats,
            "warnings": report.warnings,
            "results": [asdict(r) for r in report.results],
        }
        with open(cfg.output_json, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
        log.info("Relatório JSON salvo em: %s", cfg.output_json)

    if cfg.output_md:
        with open(cfg.output_md, "w", encoding="utf-8") as f:
            f.write(render_markdown_report(report, cfg))
        log.info("Relatório Markdown salvo em: %s", cfg.output_md)


# --------------------------------------------------------------------------- #
# Orquestração
# --------------------------------------------------------------------------- #

async def run_suite(cfg: SuiteConfig) -> SuiteReport:
    report = SuiteReport(started_at=now_iso())
    embedder = get_embedder(cfg)

    async with make_client(cfg) as client:
        await assert_collection_ready(client, cfg)

        # Estágio 1 — sanidade de metadados
        await test_metadata_sanity(client, cfg, report)

        # Estágio 2 — filtros estritos
        await test_filter_doc_type(client, cfg, report)
        await test_filter_source_file(client, cfg, report)

        # Estágio 3 — recuperação semântica
        await test_semantic_retrieval(client, cfg, report, embedder)

        # Estágio 4 — latência
        seq_latencies = await test_latency_sequential(client, cfg, report, embedder, n_requests=20)
        if seq_latencies:
            report.latency_stats["sequential_avg_ms"] = statistics.mean(seq_latencies)
        await test_latency_concurrent_stress(client, cfg, report, embedder)

    report.finished_at = now_iso()
    return report


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Suíte de QA assíncrona para a coleção Qdrant 'ue5_docs'.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--host", dest="qdrant_host", default=SuiteConfig.qdrant_host)
    p.add_argument("--port", dest="qdrant_port", type=int, default=SuiteConfig.qdrant_port)
    p.add_argument("--grpc-port", dest="qdrant_grpc_port", type=int, default=SuiteConfig.qdrant_grpc_port)
    p.add_argument("--prefer-grpc", action="store_true")
    p.add_argument("--collection", default=SuiteConfig.collection)

    p.add_argument("--embedding-dim", type=int, default=SuiteConfig.embedding_dim)
    p.add_argument("--sample-size", type=int, default=SuiteConfig.sample_size)
    p.add_argument("--min-text-len", type=int, default=SuiteConfig.min_text_len)

    p.add_argument("--top-k", type=int, default=SuiteConfig.top_k)
    p.add_argument("--score-threshold", type=float, default=SuiteConfig.score_threshold)

    p.add_argument("--concurrency", type=int, default=SuiteConfig.concurrency)
    p.add_argument("--stress-queries", type=int, default=SuiteConfig.stress_queries)
    p.add_argument("--latency-warn-ms", type=float, default=SuiteConfig.latency_warn_ms)
    p.add_argument("--request-timeout-s", type=float, default=SuiteConfig.request_timeout_s)

    p.add_argument("--embedding-mode", choices=["mock", "litellm"], default=SuiteConfig.embedding_mode)
    p.add_argument("--litellm-base-url", default=SuiteConfig.litellm_base_url)
    p.add_argument("--litellm-master-key", default=SuiteConfig.litellm_master_key)
    p.add_argument("--embedding-model", default=SuiteConfig.embedding_model)

    p.add_argument("--output-json", default=None, help="Caminho para salvar relatório JSON")
    p.add_argument("--output-md", default=None, help="Caminho para salvar relatório Markdown")
    p.add_argument("--seed", type=int, default=SuiteConfig.seed)
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def cfg_from_args(args: argparse.Namespace) -> SuiteConfig:
    return SuiteConfig(
        qdrant_host=args.qdrant_host,
        qdrant_port=args.qdrant_port,
        qdrant_grpc_port=args.qdrant_grpc_port,
        prefer_grpc=args.prefer_grpc,
        collection=args.collection,
        embedding_dim=args.embedding_dim,
        sample_size=args.sample_size,
        min_text_len=args.min_text_len,
        top_k=args.top_k,
        score_threshold=args.score_threshold,
        concurrency=args.concurrency,
        stress_queries=args.stress_queries,
        latency_warn_ms=args.latency_warn_ms,
        request_timeout_s=args.request_timeout_s,
        embedding_mode=args.embedding_mode,
        litellm_base_url=args.litellm_base_url,
        litellm_master_key=args.litellm_master_key,
        embedding_model=args.embedding_model,
        output_json=args.output_json,
        output_md=args.output_md,
        verbose=args.verbose,
        seed=args.seed,
    )


async def main_async() -> int:
    args = build_arg_parser().parse_args()
    cfg = cfg_from_args(args)
    setup_logging(cfg.verbose)

    try:
        report = await run_suite(cfg)
    except Exception as exc:
        log.error("Falha fatal ao rodar a suíte: %s", exc, exc_info=cfg.verbose)
        return 2

    print(render_terminal_summary(report, cfg))
    write_reports(report, cfg)

    return 0 if report.failed == 0 else 1


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    sys.exit(main())