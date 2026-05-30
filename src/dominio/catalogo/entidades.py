"""
Dominio: Catalogo de Dados
Entidades que representam tiles/celulas de dados disponiveis.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class CelulaFABDEM:
    """Uma celula 1x1 grau do catalogo FABDEM."""
    codigo: str                    # ex: "S17W048"
    lat_min: float
    lat_max: float
    lon_min: float
    lon_max: float
    bloco_zip: str                 # ex: "S20W050_S10W040.zip"
    arquivo_zip: str               # nome do ZIP de download
    tif_local: Optional[str] = None  # caminho local se ja baixado

    @property
    def disponivel(self) -> bool:
        return self.tif_local is not None

    def intersecta(self, west, south, east, north) -> bool:
        return (self.lon_min < east and self.lon_max > west and
                self.lat_min < north and self.lat_max > south)


@dataclass
class TileMapBiomas:
    """Representa a janela do raster MapBiomas para um bbox."""
    tif_path: str
    west: float
    south: float
    east: float
    north: float
    resolucao_m: float = 10.0    # MapBiomas Collection 10 a 10m

    @property
    def bbox(self):
        return (self.west, self.south, self.east, self.north)
