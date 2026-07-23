"""
app/services/ai/sparse_vector_service.py
=========================================
Gerador de vetores esparsos (estilo BM25) para a busca híbrida do Qdrant.

Este módulo é INTENCIONALMENTE desacoplado de qualquer outro serviço da
aplicação — não importa `rag_service` nem `ingestion_service` — para evitar
qualquer risco de import circular (ingestion_service já importa
`call_litellm` de rag_service; rag_service agora também precisa deste
módulo para o retrieval híbrido). Ambos importam só daqui.

ESTRATÉGIA DE TOKENIZAÇÃO E HASHING:
    Em vez de manter um vocabulário fixo (dicionário token → índice, que
    precisaria ser versionado e sincronizado entre ingestão e retrieval),
    usamos "hashing trick": cada token é mapeado para um índice em um
    espaço esparso fixo de tamanho `2**24` via `zlib.crc32` (biblioteca
    padrão do Python — sem dependências externas pesadas como scikit-learn
    ou sentence-transformers só para isso).

    A tokenização é feita em duas passadas complementares, pensando
    especificamente em identificadores de código C++/UE5:
    1. Split por qualquer caractere não-alfanumérico (pontuação, espaços,
       `::`, `->`, etc.) — captura o token "bruto" completo (ex:
       "AddMovementInput", "UPROPERTY").
    2. Sub-split desse token bruto por fronteiras de CamelCase, snake_case
       e dígitos (ex: "AddMovementInput" → "add", "movement", "input";
       "max_health_points" → "max", "health", "points") — captura também
       as partes semânticas individuais, para que uma busca por "movement"
       ou "health" ainda encontre o identificador composto.

    O resultado é a UNIÃO dos tokens das duas passadas (com o token bruto
    também incluído quando tiver mais de 1 caractere), permitindo tanto
    match exato do identificador quanto match parcial por componente.

TF vs IDF:
    `build_sparse_vector` retorna apenas term-frequency BRUTO (contagem de
    ocorrências por índice, sem normalização). O IDF é responsabilidade do
    próprio Qdrant em tempo de busca — a coleção deve ter o espaço esparso
    configurado com `modifier=Modifier.IDF` (ver
    `ingestion_service.ensure_sparse_vectors_enabled`), que calcula e
    aplica a estatística de IDF em nível de coleção automaticamente,
    inclusive conforme novos documentos entram.
"""

from __future__ import annotations

import re
import zlib

from qdrant_client.http import models as qdrant_models

# ---------------------------------------------------------------------------
# Nome do espaço de vetor esparso nomeado na coleção do Qdrant.
# Compartilhado entre ingestion_service.py (migração de schema + upsert +
# backfill) e rag_service.py (retrieval híbrido) — definido aqui, num módulo
# sem dependências internas, para que nenhum dos dois precise importar do
# outro só por causa desta constante.
# ---------------------------------------------------------------------------
SPARSE_VECTOR_NAME: str = "keywords_sparse"

# Tamanho do espaço esparso — 2**24 (~16.7M) buckets. Grande o suficiente
# para tornar colisões de hash raras em corpora do porte de uma base de
# documentação técnica, sem precisar de um vocabulário fixo versionado.
_SPARSE_VECTOR_DIM: int = 2**24

# Passada 1: quebra por qualquer sequência de caracteres não-alfanuméricos
# (espaços, pontuação, `::`, `->`, `()`, etc).
_NON_ALNUM_RE = re.compile(r"[^A-Za-z0-9]+")

# Passada 2: sub-divide um token bruto em partes CamelCase / snake_case /
# dígitos. Cobre os padrões mais comuns de identificadores C++/UE5:
#   - "AddMovementInput"   -> ["Add", "Movement", "Input"]
#   - "UPROPERTY"          -> ["UPROPERTY"] (sigla toda maiúscula, mantida)
#   - "bIsJumping"         -> ["b", "Is", "Jumping"]
#   - "max_health_points"  -> já quebrado pela Passada 1 (underscore não é
#     alfanumérico), então cada parte chega aqui sozinha.
#   - "UE5"                -> ["UE", "5"]
_CAMEL_SPLIT_RE = re.compile(
    r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+"
)


def _tokenize(text: str) -> list[str]:
    """
    Tokeniza texto livre (título, resumo, conteúdo bruto) em uma lista de
    termos em minúsculas, prontos para hashing.

    Une o token bruto (Passada 1) com seus componentes CamelCase/snake_case
    (Passada 2) sempre que a sub-divisão produzir mais de uma parte — um
    token de uma palavra só (ex: "jump") não precisa de sub-split.

    Termos de 1 caractere são descartados (ruído — normalmente artigos
    partidos, iniciais de prefixo UE como o "A" de "ACharacter" sozinho,
    etc.) EXCETO quando são o único conteúdo do token bruto original.
    """
    tokens: list[str] = []

    for raw_word in _NON_ALNUM_RE.split(text):
        if not raw_word:
            continue

        lowered = raw_word.lower()
        if len(lowered) > 1:
            tokens.append(lowered)

        sub_parts = _CAMEL_SPLIT_RE.findall(raw_word)
        if len(sub_parts) > 1:
            for part in sub_parts:
                part_lower = part.lower()
                if len(part_lower) > 1:
                    tokens.append(part_lower)

    return tokens


def _token_to_index(token: str) -> int:
    """Mapeia um token para um índice determinístico no espaço esparso."""
    return zlib.crc32(token.encode("utf-8")) % _SPARSE_VECTOR_DIM


def build_sparse_vector(text: str) -> qdrant_models.SparseVector:
    """
    Constrói um SparseVector (estilo BM25/TF) a partir de texto livre.

    Args:
        text: Texto de origem (tipicamente a concatenação de title +
            raw_content + summary_markdown na ingestão, ou os
            `keywords_exact` extraídos pelo decompositor de query no
            retrieval — ver rag_service._hybrid_search).

    Returns:
        qdrant_models.SparseVector com `indices` (posições no espaço
        esparso) e `values` (term-frequency bruto, sem normalização — o
        IDF é aplicado pelo próprio Qdrant via modifier=Modifier.IDF).
        Vazio (indices=[], values=[]) se `text` não produzir nenhum token
        utilizável.
    """
    if not text:
        return qdrant_models.SparseVector(indices=[], values=[])

    tokens = _tokenize(text)
    if not tokens:
        return qdrant_models.SparseVector(indices=[], values=[])

    term_frequency: dict[int, float] = {}
    for token in tokens:
        idx = _token_to_index(token)
        term_frequency[idx] = term_frequency.get(idx, 0.0) + 1.0

    indices = list(term_frequency.keys())
    values = [term_frequency[i] for i in indices]
    return qdrant_models.SparseVector(indices=indices, values=values)