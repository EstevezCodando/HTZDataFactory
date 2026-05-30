"""Dominio: Auditoria — entidade Evento."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class Evento:
    """Registro de uma etapa do processamento para auditoria."""
    job_id: str
    etapa: str
    dados: dict[str, Any] = field(default_factory=dict)
    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        return {
            "ts": self.ts,
            "job_id": self.job_id,
            "etapa": self.etapa,
            **self.dados,
        }
