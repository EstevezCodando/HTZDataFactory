"""Interface para o repositorio de auditoria."""

from typing import Protocol, runtime_checkable
from .entidades import Evento


@runtime_checkable
class ILogAuditoria(Protocol):
    def registrar(self, evento: Evento) -> None:
        """Persiste um evento de auditoria."""
        ...

    def eventos_do_job(self, job_id: str) -> list[Evento]:
        """Retorna todos os eventos de um job."""
        ...
