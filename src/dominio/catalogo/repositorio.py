"""Interfaces (protocolos) para os catalogos de dados."""

from typing import Protocol, runtime_checkable
from .entidades import CelulaFABDEM, TileMapBiomas


@runtime_checkable
class ICatalogoDEM(Protocol):
    def celulas_para_bbox(self, west: float, south: float,
                          east: float, north: float) -> list[CelulaFABDEM]:
        """Retorna celulas FABDEM que intersectam o bbox."""
        ...

    def garantir_tif(self, celula: CelulaFABDEM) -> str:
        """Baixa o TIF se necessario e retorna o caminho local."""
        ...


@runtime_checkable
class ICatalogoClutter(Protocol):
    def tile_para_bbox(self, west: float, south: float,
                       east: float, north: float) -> TileMapBiomas:
        """Retorna metadados do tile MapBiomas para o bbox."""
        ...
