"""
scripts/validate_stack.py
==========================
Script de validação end-to-end para confirmar que toda a stack está operacional.

Executa na máquina HOST (fora do Docker), batendo nos endpoints expostos.

Uso:
    python scripts/validate_stack.py
    python scripts/validate_stack.py --api-url http://localhost:8000 --verbose
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from typing import Any

try:
    import httpx
except ImportError:
    print("❌ Instale httpx: pip install httpx")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Configuração padrão (pode ser sobrescrita via args)
# ---------------------------------------------------------------------------
DEFAULT_API_URL = "http://localhost:8000"
DEFAULT_LITELLM_URL = "http://localhost:4000"
DEFAULT_QDRANT_URL = "http://localhost:6333"


def print_section(title: str) -> None:
    print(f"\n{'=' * 55}")
    print(f"  {title}")
    print(f"{'=' * 55}")


def check(label: str, ok: bool, detail: str = "") -> bool:
    status = "✅" if ok else "❌"
    msg = f"  {status} {label}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    return ok


# ---------------------------------------------------------------------------
# Checks individuais
# ---------------------------------------------------------------------------

def check_qdrant(base_url: str, client: httpx.Client) -> bool:
    print_section("1. Qdrant (Vector DB)")
    try:
        r = client.get(f"{base_url}/collections", timeout=5)
        collections = r.json().get("result", {}).get("collections", [])
        names = [c["name"] for c in collections]
        has_collection = "ue5_docs" in names

        check("Qdrant acessível", r.status_code == 200, f"HTTP {r.status_code}")
        check(
            "Coleção 'ue5_docs' existe",
            has_collection,
            "Execute ue5_rag_ingestor.py se não existir" if not has_collection else f"Coleções: {names}",
        )
        return r.status_code == 200
    except Exception as exc:
        check("Qdrant acessível", False, str(exc))
        return False


def check_litellm(base_url: str, client: httpx.Client, master_key: str) -> bool:
    print_section("2. LiteLLM Proxy")
    try:
        r = client.get(
            f"{base_url}/health/readiness",
            headers={"Authorization": f"Bearer {master_key}"},
            timeout=10,
        )
        check("LiteLLM /health/readiness", r.status_code == 200, f"HTTP {r.status_code}")

        # Lista modelos disponíveis
        r_models = client.get(
            f"{base_url}/models",
            headers={"Authorization": f"Bearer {master_key}"},
            timeout=10,
        )
        if r_models.status_code == 200:
            models = [m["id"] for m in r_models.json().get("data", [])]
            print(f"     Modelos disponíveis: {models[:5]}{'...' if len(models) > 5 else ''}")

        return r.status_code == 200
    except Exception as exc:
        check("LiteLLM acessível", False, str(exc))
        return False


def check_fastapi_health(base_url: str, client: httpx.Client) -> bool:
    print_section("3. FastAPI Agent (Health Check)")
    try:
        r = client.get(f"{base_url}/v1/health", timeout=10)
        data = r.json()
        overall_ok = data.get("status") in ("ok", "degraded")

        check("FastAPI respondendo", r.status_code in (200, 503), f"HTTP {r.status_code}")
        check("Status geral", data.get("status") == "ok", data.get("status", "unknown"))

        deps = data.get("dependencies", {})
        for dep_name, dep_info in deps.items():
            dep_ok = dep_info.get("status") == "ok"
            latency = dep_info.get("latency_ms")
            detail = f"{dep_info.get('status')} | {latency}ms" if latency else dep_info.get("status", "")
            check(f"  Dep: {dep_name}", dep_ok, detail)

        return overall_ok
    except Exception as exc:
        check("FastAPI /v1/health", False, str(exc))
        return False


def check_fastapi_chat(base_url: str, client: httpx.Client) -> bool:
    print_section("4. FastAPI Chat Endpoint (Stub)")
    session_id = str(uuid.uuid4())
    payload = {
        "message": "O que é o Lumen no Unreal Engine 5?",
        "session_id": session_id,
        "model": "ollama/qwen2.5-coder:7b",
        "top_k": 3,
    }
    try:
        start = time.perf_counter()
        r = client.post(f"{base_url}/v1/chat", json=payload, timeout=30)
        elapsed = round((time.perf_counter() - start) * 1000)

        data = r.json()
        has_answer = bool(data.get("answer"))
        has_session = data.get("session_id") == session_id

        check("POST /v1/chat respondeu", r.status_code == 200, f"HTTP {r.status_code} | {elapsed}ms")
        check("session_id preservado", has_session)
        check("Campo 'answer' presente", has_answer)

        if has_answer:
            print(f"\n     Resposta (stub): {data['answer'][:120]}...")

        return r.status_code == 200
    except Exception as exc:
        check("POST /v1/chat", False, str(exc))
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Valida a stack UE5 AI Assistant")
    parser.add_argument("--api-url", default=DEFAULT_API_URL)
    parser.add_argument("--litellm-url", default=DEFAULT_LITELLM_URL)
    parser.add_argument("--qdrant-url", default=DEFAULT_QDRANT_URL)
    parser.add_argument("--litellm-key", default="sk-litellm-master-key-here",
                        help="LITELLM_MASTER_KEY configurado no .env")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    print("\n🚀 UE5 AI Assistant — Validação da Stack")
    print(f"   API:     {args.api_url}")
    print(f"   LiteLLM: {args.litellm_url}")
    print(f"   Qdrant:  {args.qdrant_url}")

    results: dict[str, bool] = {}

    with httpx.Client() as client:
        results["qdrant"] = check_qdrant(args.qdrant_url, client)
        results["litellm"] = check_litellm(args.litellm_url, client, args.litellm_key)
        results["health"] = check_fastapi_health(args.api_url, client)
        results["chat"] = check_fastapi_chat(args.api_url, client)

    print_section("RESUMO FINAL")
    all_ok = True
    for name, ok in results.items():
        if not check(name.capitalize(), ok):
            all_ok = False

    if all_ok:
        print("\n🎉 Stack 100% operacional! Pronto para a Fase 2 (lógica RAG).\n")
        sys.exit(0)
    else:
        print("\n⚠️  Um ou mais checks falharam. Verifique os logs dos containers.\n")
        print("   docker compose logs --tail=50 ue5-agent")
        sys.exit(1)


if __name__ == "__main__":
    main()