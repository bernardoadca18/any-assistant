"""
app/core/logging.py
===================
Configuração de logging estruturado.
- Produção: JSON (fácil de parsear por ferramentas como Grafana Loki)
- Desenvolvimento: saída colorida e legível por humanos
"""

import logging
import sys

from app.core.config import settings


def setup_logging() -> None:
    """
    Configura o logging da aplicação.
    Chamado uma única vez no lifespan do FastAPI.
    """
    log_level = getattr(logging, settings.log_level.upper(), logging.INFO)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)

    if settings.is_development:
        formatter = logging.Formatter(
            fmt="%(asctime)s | %(levelname)-8s | %(name)s:%(lineno)d | %(message)s",
            datefmt="%H:%M:%S",
        )
    else:
        import json as _json

        class JsonFormatter(logging.Formatter):
            def format(self, record: logging.LogRecord) -> str:
                log_entry = {
                    "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
                    "level": record.levelname,
                    "logger": record.name,
                    "message": record.getMessage(),
                    "module": record.module,
                    "line": record.lineno,
                }
                if record.exc_info:
                    log_entry["exception"] = self.formatException(record.exc_info)
                return _json.dumps(log_entry, ensure_ascii=False)

        formatter = JsonFormatter()

    handler.setFormatter(formatter)
    root_logger.addHandler(handler)
    root_logger.setLevel(log_level)

    for noisy_logger in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)

    logging.getLogger(__name__).info(
        "Logging configurado | env=%s level=%s",
        settings.app_env,
        settings.log_level,
    )