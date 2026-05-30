"""
Dominio: Area de Interesse
Entidade central — representa o pedido geografico do usuario.
"""

from dataclasses import dataclass, field
from typing import Optional
import uuid


@dataclass
class AreaInteresse:
    """
    Representa a area geografica para a qual o pacote HTZ sera gerado.

    bbox: (west, south, east, north) em graus WGS84.
    job_id: identificador unico gerado automaticamente.
    layers: conjunto de camadas solicitadas (geo, sol, img, blg).
    """
    bbox: tuple[float, float, float, float]
    job_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    layers: list[str] = field(default_factory=lambda: ["geo", "sol", "img"])

    def __post_init__(self):
        west, south, east, north = self.bbox
        if west >= east:
            raise ValueError(f"bbox invalido: west ({west}) >= east ({east})")
        if south >= north:
            raise ValueError(f"bbox invalido: south ({south}) >= north ({north})")
        if not (-180 <= west <= 180 and -180 <= east <= 180):
            raise ValueError("Longitudes fora do intervalo [-180, 180]")
        if not (-90 <= south <= 90 and -90 <= north <= 90):
            raise ValueError("Latitudes fora do intervalo [-90, 90]")
        # Limita bbox a 5 graus de lado para evitar processamentos excessivos
        if (east - west) > 5 or (north - south) > 5:
            raise ValueError(
                f"bbox muito grande ({east-west:.1f}°×{north-south:.1f}°). "
                "Limite: 5°×5°"
            )

    @property
    def centro(self) -> tuple[float, float]:
        west, south, east, north = self.bbox
        return (west + east) / 2, (south + north) / 2

    @property
    def area_graus2(self) -> float:
        west, south, east, north = self.bbox
        return (east - west) * (north - south)

    def __repr__(self):
        w, s, e, n = self.bbox
        return f"AreaInteresse(job={self.job_id}, bbox=[{w},{s},{e},{n}])"
