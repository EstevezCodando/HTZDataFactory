"""
Infraestrutura: Persistencia de auditoria em arquivos .jsonl.
Implementa ILogAuditoria usando um arquivo por job em /saidas/{job_id}/audit.jsonl.
"""

import json
import os
from pathlib import Path

from src.dominio.auditoria.entidades import Evento
from src.dominio.auditoria.repositorio import ILogAuditoria


class AuditJsonl:
    """Persiste eventos de auditoria em arquivo .jsonl por job."""

    def __init__(self, output_dir: str):
        self.output_dir = Path(output_dir)

    def _path(self, job_id: str) -> Path:
        job_dir = self.output_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        return job_dir / "audit.jsonl"

    def registrar(self, evento: Evento) -> None:
        path = self._path(evento.job_id)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(evento.to_dict(), ensure_ascii=False) + "\n")

    def eventos_do_job(self, job_id: str) -> list[Evento]:
        path = self._path(job_id)
        if not path.exists():
            return []
        eventos = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    ev = Evento(
                        job_id=d.pop("job_id"),
                        etapa=d.pop("etapa"),
                        ts=d.pop("ts"),
                        dados=d,
                    )
                    eventos.append(ev)
                except (json.JSONDecodeError, KeyError):
                    continue
        return eventos
