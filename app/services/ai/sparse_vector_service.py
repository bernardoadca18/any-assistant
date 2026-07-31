"""
app/services/ai/sparse_vector_service.py
=========================================
Gerador de vetores esparsos (estilo BM25) para a busca híbrida do Qdrant.

Este módulo é INTENCIONALMENTE desacoplado de qualquer outro serviço da
aplicação — não importa `rag_service` nem `ingestion_service` — para evitar
qualquer risco de import circular. Ambos importam só daqui.

CORREÇÃO (multi-domain — ver app/core/domains/): a regex de sub-split
CamelCase/snake_case ERA uma constante módulo-level fixa, hardcoded para as
convenções de nomenclatura da UE5 (prefixos A/U/F/E). Isso quebra
silenciosamente para outros domínios que usam convenções diferentes (ou,
no caso de Godot 4, uma convenção suficientemente parecida que a mesma
regex ainda funciona, mas por coincidência, não por design). A função
principal agora aceita `camel_split_regex` — o chamador (rag_service.py na
query, ingestion_service.py na ingestão) resolve essa regex a partir do
`BaseDomainProfile.sparse_camel_split_regex` do domínio ativo e a repassa
explicitamente. Nenhum default é assumido aqui para evitar reintroduzir o
acoplamento oculto a um domínio específico.

ESTRATÉGIA DE TOKENIZAÇÃO E HASHING (inalterada):
    Hashing trick via zlib.crc32 num espaço esparso fixo de 2**24 buckets,
    sem vocabulário fixo versionado. Tokenização em duas passadas: (1) split
    por caractere não-alfanumérico, capturando o token bruto; (2) sub-split
    do token bruto via `camel_split_regex`, capturando componentes
    semânticos individuais. União das duas passadas.

TF vs IDF (inalterado):
    Apenas term-frequency bruto é retornado; o IDF é responsabilidade do
    Qdrant via `modifier=Modifier.IDF` na configuração do espaço esparso
    nomeado (ver ingestion_service.ensure_sparse_vectors_enabled).
"""

from __future__ import annotations

import re
import zlib
from functools import lru_cache

from qdrant_client.http import models as qdrant_models

# ---------------------------------------------------------------------------
# Nome do espaço de vetor esparso nomeado na coleção do Qdrant. Compartilhado
# entre todos os domínios — cada domínio tem sua PRÓPRIA coleção Qdrant
# (ver BaseDomainProfile.default_collection_name), então não há colisão em
# usar o mesmo nome de espaço esparso dentro de cada coleção isolada.
# ---------------------------------------------------------------------------
SPARSE_VECTOR_NAME: str = "keywords_sparse"

# Tamanho do espaço esparso — 2**24 (~16.7M) buckets.
_SPARSE_VECTOR_DIM: int = 2**24

# Passada 1: quebra por qualquer sequência de caracteres não-alfanuméricos
# (espaços, pontuação, `::`, `->`, `()`, `@`, etc). Domain-agnostic — não
# há necessidade de parametrizar esta passada por domínio.
_NON_ALNUM_RE = re.compile(r"[^A-Za-z0-9]+")

# Regex de fallback (idêntica à antiga constante fixa) — usada apenas se um
# chamador legado não passar `camel_split_regex` explicitamente, para não
# quebrar em produção durante o rollout da migração multi-domain. Chamadores
# NOVOS (rag_service.py, ingestion_service.py pós-refactor) devem sempre
# passar a regex resolvida do domain_profile ativo.
_LEGACY_DEFAULT_CAMEL_SPLIT_RE = r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+"


@lru_cache(maxsize=32)
def _compiled_camel_split(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern)


def _tokenize(text: str, camel_split_regex: str) -> list[str]:
    """
    Tokeniza texto livre em termos minúsculos, prontos para hashing.

    Une o token bruto (Passada 1) com seus componentes CamelCase/snake_case
    (Passada 2, via `camel_split_regex` — específica do domínio ativo)
    sempre que a sub-divisão produzir mais de uma parte.

    Termos de 1 caractere são descartados, exceto quando são o único
    conteúdo do token bruto original.
    """
    tokens: list[str] = []
    camel_re = _compiled_camel_split(camel_split_regex)

    for raw_word in _NON_ALNUM_RE.split(text):
        if not raw_word:
            continue

        lowered = raw_word.lower()
        if len(lowered) > 1:
            tokens.append(lowered)

        sub_parts = camel_re.findall(raw_word)
        if len(sub_parts) > 1:
            for part in sub_parts:
                part_lower = part.lower()
                if len(part_lower) > 1:
                    tokens.append(part_lower)

    return tokens


def _token_to_index(token: str) -> int:
    """Mapeia um token para um índice determinístico no espaço esparso."""
    return zlib.crc32(token.encode("utf-8")) % _SPARSE_VECTOR_DIM


def build_sparse_vector(
    text: str,
    camel_split_regex: str | None = None,
) -> qdrant_models.SparseVector:
    """
    Constrói um SparseVector (estilo BM25/TF) a partir de texto livre.

    Args:
        text: Texto de origem (title + raw_content + summary_markdown na
            ingestão, ou keywords_exact do decompositor no retrieval).
        camel_split_regex: Regex de sub-split específica do domínio ativo
            (ver `BaseDomainProfile.sparse_camel_split_regex`). Se None,
            usa a regex legada (equivalente à UE5) por compatibilidade —
            todo chamador novo deve passar este valor explicitamente.

    Returns:
        qdrant_models.SparseVector com `indices` e `values` (TF bruto, sem
        normalização — IDF aplicado pelo Qdrant). Vazio se `text` não
        produzir nenhum token utilizável.
    """
    if not text:
        return qdrant_models.SparseVector(indices=[], values=[])

    tokens = _tokenize(text, camel_split_regex or _LEGACY_DEFAULT_CAMEL_SPLIT_RE)
    if not tokens:
        return qdrant_models.SparseVector(indices=[], values=[])

    term_frequency: dict[int, float] = {}
    for token in tokens:
        idx = _token_to_index(token)
        term_frequency[idx] = term_frequency.get(idx, 0.0) + 1.0

    indices = list(term_frequency.keys())
    values = [term_frequency[i] for i in indices]
    return qdrant_models.SparseVector(indices=indices, values=values)
