#!/usr/bin/env python3
"""
scripts/validate_rag.py
========================
Teste end-to-end do pipeline RAG real (Fase 2).
Executa no HOST (fora do Docker), bate no endpoint exposto na porta 8000.

Diferente do validate_stack.py (que testava o stub), este script:
1. Envia perguntas reais sobre UE5
2. Verifica se chunks foram recuperados do Qdrant
3. Verifica se a resposta veio do LLM (não é um stub)
4. Testa a persistência de sessão (histórico no Redis)
5. Mede latências de cada etapa

Uso:
    python scripts/validate_rag.py
    python scripts/validate_rag.py --model ollama/llama3.2:3b --top-k 3
"""

from __future__ import annotations

import argparse
import sys
import time
import uuid
from typing import Any

try:
    import httpx
except ImportError:
    print("❌ Instale httpx: pip install httpx")
    sys.exit(1)

DEFAULT_API_URL = "http://localhost:8000"

TEST_QUESTIONS = [
    "O que é o sistema Lumen e como ele funciona no Unreal Engine 5?",
    "Como o Nanite funciona para renderização de geometria?",
    "Quais são as diferenças entre Blueprint e C++ no UE5?",
]


def print_section(title: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")


def check(label: str, ok: bool, detail: str = "") -> bool:
    icon = "✅" if ok else "❌"
    msg = f"  {icon} {label}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    return ok


def test_single_query(
    client: httpx.Client,
    base_url: str,
    question: str,
    session_id: str,
    model: str,
    top_k: int,
    verbose: bool,
) -> dict[str, Any]:
    """Envia uma pergunta e valida a resposta RAG."""
    payload = {
        "message": question,
        "session_id": session_id,
        "model": model,
        "top_k": top_k,
    }

    start = time.perf_counter()
    try:
        r = client.post(f"{base_url}/v1/chat", json=payload, timeout=180)
        elapsed = round((time.perf_counter() - start) * 1000)
        r.raise_for_status()
    except httpx.HTTPStatusError as exc:
        print(f"  ❌ HTTP {exc.response.status_code}: {exc.response.text[:200]}")
        return {"ok": False}
    except httpx.TimeoutException:
        print(f"  ❌ Timeout após {elapsed}ms — o modelo pode estar carregando")
        return {"ok": False}

    data = r.json()
    answer = data.get("answer", "")
    chunks = data.get("retrieved_chunks", [])
    prompt_tokens = data.get("prompt_tokens")
    completion_tokens = data.get("completion_tokens")

    # Validações
    is_not_stub = "[STUB" not in answer
    has_answer = len(answer) > 20
    has_chunks = len(chunks) > 0

    check(f"Respondeu em {elapsed}ms", r.status_code == 200, f"HTTP {r.status_code}")
    check("Resposta não é stub", is_not_stub, "Pipeline RAG real ativo")
    check("Resposta tem conteúdo", has_answer, f"{len(answer)} chars")
    check(
        f"Chunks recuperados do Qdrant",
        has_chunks,
        f"{len(chunks)} chunks | scores: {[round(c['score'], 3) for c in chunks[:3]]}",
    )

    if prompt_tokens:
        print(f"     Tokens: {prompt_tokens} in / {completion_tokens} out")

    if verbose and answer:
        print(f"\n  💬 Resposta:\n     {answer[:400].replace(chr(10), chr(10) + '     ')}...")
        if chunks:
            top_chunk = chunks[0]
            print(f"\n  📄 Top chunk: [{top_chunk['title']}] score={top_chunk['score']:.3f}")

    return {
        "ok": r.status_code == 200 and is_not_stub and has_chunks,
        "elapsed_ms": elapsed,
        "chunks": len(chunks),
        "answer_len": len(answer),
    }


def test_session_memory(
    client: httpx.Client,
    base_url: str,
    model: str,
) -> bool:
    """Verifica se o histórico de sessão funciona (Redis)."""
    print_section("TESTE DE MEMÓRIA DE SESSÃO (Redis)")
    session_id = str(uuid.uuid4())

    # Turno 1: apresentação
    r1 = client.post(
        f"{base_url}/v1/chat",
        json={
            "message": "Meu nome é Dev e estou trabalhando com Lumen no UE5.",
            "session_id": session_id,
            "model": model,
            "top_k": 2,
        },
        timeout=120,
    )
    check("Turno 1 enviado", r1.status_code == 200, f"HTTP {r1.status_code}")

    # Turno 2: pergunta que requer memória do turno anterior
    r2 = client.post(
        f"{base_url}/v1/chat",
        json={
            "message": "Diga meu nome e o que eu mencionei estar trabalhando.",
            "session_id": session_id,
            "model": model,
            "top_k": 2,
        },
        timeout=120,
    )
    check("Turno 2 enviado", r2.status_code == 200, f"HTTP {r2.status_code}")

    answer2 = r2.json().get("answer", "").lower()
    has_memory = "dev" in answer2 or "lumen" in answer2

    check(
        "Histórico preservado no Redis",
        has_memory,
        "Modelo reconhece contexto anterior" if has_memory else
        "⚠️ Modelo pode não ter referenciado o histórico — revise o session_service",
    )
    return has_memory


def main() -> None:
    parser = argparse.ArgumentParser(description="Valida o pipeline RAG do UE5 Assistant")
    parser.add_argument("--api-url", default=DEFAULT_API_URL)
    parser.add_argument("--model", default="ollama/qwen2.5-coder:7b")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    print(f"\n🚀 UE5 AI Assistant — Validação do Pipeline RAG (Fase 2)")
    print(f"   API:   {args.api_url}")
    print(f"   Model: {args.model}")
    print(f"   Top-K: {args.top_k}")

    results: list[bool] = []
    session_id = str(uuid.uuid4())   # mesma sessão para todos os testes

    with httpx.Client() as client:

        # Teste de saúde rápido antes de testar o RAG
        try:
            r = client.get(f"{args.api_url}/v1/health", timeout=10)
            if r.status_code != 200:
                print(f"\n❌ API não está saudável (HTTP {r.status_code}). Suba o stack primeiro.")
                sys.exit(1)
        except Exception as exc:
            print(f"\n❌ API inacessível em {args.api_url}: {exc}")
            sys.exit(1)

        for i, question in enumerate(TEST_QUESTIONS, start=1):
            print_section(f"QUERY {i}/{len(TEST_QUESTIONS)}")
            print(f"  ❓ {question}\n")
            result = test_single_query(
                client=client,
                base_url=args.api_url,
                question=question,
                session_id=session_id,
                model=args.model,
                top_k=args.top_k,
                verbose=args.verbose,
            )
            results.append(result.get("ok", False))

        mem_ok = test_session_memory(client, args.api_url, args.model)
        results.append(mem_ok)

    print_section("RESUMO FINAL")
    all_ok = all(results)
    passed = sum(results)
    total = len(results)

    icon = "🎉" if all_ok else "⚠️"
    print(f"\n  {icon} {passed}/{total} testes passaram\n")

    if all_ok:
        print("  Pipeline RAG 100% operacional! Pronto para a Fase 3 (streaming + auth).\n")
        sys.exit(0)
    else:
        print("  Alguns testes falharam. Verifique os logs: docker compose logs -f ue5-agent\n")
        sys.exit(1)


if __name__ == "__main__":
    main()