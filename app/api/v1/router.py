"""
app/api/v1/router.py
====================
Agrega todos os routers da versão 1 da API.
"""

from fastapi import APIRouter

from app.api.v1.endpoints import admin, chat, health, ingest, sessions

api_router = APIRouter()

api_router.include_router(health.router, tags=["infra"])
api_router.include_router(chat.router, prefix="/chat", tags=["agent"])
api_router.include_router(ingest.router, prefix="/ingest", tags=["ingestion"])
api_router.include_router(sessions.router, tags=["sessions"])
api_router.include_router(admin.router, tags=["admin"])