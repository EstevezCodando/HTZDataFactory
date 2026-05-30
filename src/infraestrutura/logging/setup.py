"""
Logging estruturado para o HTZDataFactory.

Emite JSON por linha (JSON Lines) — compatível com:
  - Docker logs (docker compose logs)
  - Loki / Grafana
  - Elasticsearch / OpenSearch
  - qualquer parser de JSON Lines

Níveis usados:
  DEBUG   — detalhe interno de algoritmos (desabilitado em produção)
  INFO    — progresso normal de cada etapa
  WARNING — situações inesperadas mas recuperáveis
  ERROR   — falhas que interrompem o job
  CRITICAL — falhas que derrubam o serviço
"""

import json
import logging
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from typing import Any


# ── Formatter JSON Lines ──────────────────────────────────────────────────────

class JsonLinesFormatter(logging.Formatter):
    """Formata cada log record como uma linha JSON."""

    # Campos do LogRecord que não precisam ir para o JSON
    _SKIP = {
        "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno",
        "funcName", "created", "msecs", "relativeCreated", "thread",
        "threadName", "processName", "process", "taskName",
    }

    def format(self, record: logging.LogRecord) -> str:
        # Campos base
        doc: dict[str, Any] = {
            "ts":      datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level":   record.levelname,
            "logger":  record.name,
            "msg":     record.getMessage(),
            "file":    f"{record.filename}:{record.lineno}",
        }

        # Exceção, se houver
        if record.exc_info:
            doc["exception"] = {
                "type":    record.exc_info[0].__name__ if record.exc_info[0] else None,
                "message": str(record.exc_info[1]),
                "trace":   traceback.format_exception(*record.exc_info),
            }

        # Campos extras passados com extra={...}
        for key, val in record.__dict__.items():
            if key not in self._SKIP and not key.startswith("_"):
                doc[key] = val

        return json.dumps(doc, ensure_ascii=False, default=str)


# ── Configuração global ───────────────────────────────────────────────────────

def setup_logging(
    level: str = None,
    json_output: bool = True,
) -> logging.Logger:
    """
    Configura o logging global do processo.

    level: "DEBUG" | "INFO" | "WARNING" | "ERROR"
           default: lê de LOG_LEVEL env var, fallback "INFO"
    json_output: True = JSON Lines (produção), False = human-readable (dev)

    Retorna o logger raiz configurado.
    """
    level = level or os.environ.get("LOG_LEVEL", "INFO").upper()
    numeric_level = getattr(logging, level, logging.INFO)

    root = logging.getLogger()
    root.setLevel(numeric_level)

    # Remove handlers existentes (evita duplicação em hot-reload)
    root.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(numeric_level)

    if json_output:
        handler.setFormatter(JsonLinesFormatter())
    else:
        fmt = "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s"
        handler.setFormatter(logging.Formatter(fmt, datefmt="%H:%M:%S"))

    root.addHandler(handler)

    # Silencia loggers muito verbosos de bibliotecas externas
    for noisy in ("urllib3", "botocore", "s3transfer", "rasterio",
                  "fiona", "shapely", "pystac_client", "planetary_computer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return root


def get_logger(name: str) -> logging.Logger:
    """Retorna logger nomeado. Usar no topo de cada módulo:
        log = get_logger(__name__)
    """
    return logging.getLogger(name)


# ── Logger de job — adiciona job_id em todos os registros ────────────────────

class JobLogger:
    """
    Wrapper de logger que injeta job_id em todos os registros.
    Acumula linhas para exibição em tempo real no frontend.
    """

    def __init__(self, job_id: str, max_lines: int = 500):
        self._log = get_logger("htz.job")
        self.job_id = job_id
        self._lines: list[str] = []
        self._max_lines = max_lines
        self._t0 = time.monotonic()

    def _extra(self, **kwargs) -> dict:
        return {"job_id": self.job_id, "elapsed_s": round(time.monotonic() - self._t0, 2), **kwargs}

    def info(self, msg: str, **kwargs):
        self._log.info(msg, extra=self._extra(**kwargs))
        self._append(msg)

    def warning(self, msg: str, **kwargs):
        self._log.warning(msg, extra=self._extra(**kwargs))
        self._append(f"⚠ {msg}")

    def error(self, msg: str, **kwargs):
        self._log.error(msg, extra=self._extra(**kwargs))
        self._append(f"✖ {msg}")

    def debug(self, msg: str, **kwargs):
        self._log.debug(msg, extra=self._extra(**kwargs))

    def _append(self, msg: str):
        self._lines.append(msg)
        if len(self._lines) > self._max_lines:
            self._lines.pop(0)

    def get_lines(self) -> list[str]:
        return list(self._lines)

    def progress_cb(self, msg: str):
        """Compatível com a interface progress_cb dos adaptadores."""
        self.info(msg)
