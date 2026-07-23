"""
Construção de sparse vectors estilo BM25 para busca híbrida no Qdrant.
Sem dependência nova: usa zlib.crc32 (stdlib) para hashear tokens em
índices esparsos — evita manter um vocabulário fixo e funciona bem para
identificadores de código (CamelCase/snake_case), que um tokenizador de
linguagem natural (ex: fastembed Qdrant/bm25) tende a partir mal.
"""
from __future__ import annotations

import re
import zlib
from collections import Counter

from qdrant_client import models as qdrant_models

_CAMEL_SPLIT_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+")
_STOPWORDS = {
    "the", "a", "an", "is", "are", "of", "to", "in", "on", "for", "and",
    "or", "with", "this", "that", "como", "para", "com", "que", "uma", "um",
}
_SPARSE_INDEX_SPACE = 2**24  # espaço amplo — colisão improvável no seu corpus


def _tokenize(text: str) -> list[str]:
    """Separa por não-alfanumérico, depois quebra identificadores por
    CamelCase/snake_case/dígitos (ex: 'GetActorLocation' -> ['get','actor','location'])."""
    raw_tokens = re.split(r"[^A-Za-z0-9_]+", text)
    tokens: list[str] = []
    for raw in raw_tokens:
        if not raw:
            continue
        parts = _CAMEL_SPLIT_RE.findall(raw) or [raw]
        tokens.extend(p.lower() for p in parts if len(p) > 1)
    return [t for t in tokens if t not in _STOPWORDS]


def build_sparse_vector(text: str) -> qdrant_models.SparseVector:
    """
    Gera um SparseVector de term-frequency bruto. O IDF é aplicado pelo
    Qdrant em tempo de busca (ver Modifier.IDF no Passo 1) — aqui só
    contamos ocorrências por token.
    """
    tokens = _tokenize(text)
    if not tokens:
        return qdrant_models.SparseVector(indices=[], values=[])

    counts = Counter(tokens)
    indices = [zlib.crc32(tok.encode("utf-8")) % _SPARSE_INDEX_SPACE for tok in counts]
    values = [float(freq) for freq in counts.values()]
    return qdrant_models.SparseVector(indices=indices, values=values)