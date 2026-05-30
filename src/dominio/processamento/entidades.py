"""
Dominio: Processamento Geoespacial
Entidade CamadaRaster — wrapper sobre arrays numpy com metadados geoespaciais.
"""

from dataclasses import dataclass
from typing import Optional
import numpy as np


@dataclass
class CamadaRaster:
    """
    Representa uma camada raster processada pronta para conversao ATDI.

    nome: identificador da camada (geo, sol, img, blg)
    dados: array numpy 2D (height x width)
    transform: affine transform rasterio
    crs: CRS do raster
    pixel_m: resolucao em metros
    nodata: valor de nodata (None = sem nodata)
    """
    nome: str
    dados: np.ndarray
    transform: object   # rasterio.transform.Affine
    crs: object         # rasterio.crs.CRS
    pixel_m: float
    nodata: Optional[float] = None

    @property
    def shape(self) -> tuple[int, int]:
        return self.dados.shape  # (height, width)

    @property
    def width(self) -> int:
        return self.dados.shape[1]

    @property
    def height(self) -> int:
        return self.dados.shape[0]

    @property
    def xmin(self) -> float:
        """Coordenada X (easting) do canto superior esquerdo."""
        return self.transform.c

    @property
    def ymax(self) -> float:
        """Coordenada Y (northing) do canto superior esquerdo."""
        return self.transform.f

    @property
    def ymin(self) -> float:
        """Coordenada Y (northing) do canto inferior esquerdo."""
        return self.ymax - self.height * self.pixel_m
