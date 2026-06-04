"""Schemas Pydantic para a API REST."""

from pydantic import BaseModel, field_validator, model_validator
from typing import Optional, Literal

# Área máxima permitida por job: 5° × 5° = 25 graus quadrados
_MAX_AREA_DEG2 = 25.0


class JobRequest(BaseModel):
    bbox: list[float]
    layers: list[str] = ["geo", "sol", "img"]
    img_fonte: str = "sentinel2"       # "sentinel2" | "osm" | "mbtiles"
    osm_zoom:  int = 15                # zoom OSM (12-17; só usado quando img_fonte="osm")
    img_preview_id: Optional[str] = None  # obrigatório quando img_fonte="mbtiles"
    aplicar_onion: bool = True         # aplica efeito cebola urbano no .sol

    @field_validator("bbox")
    @classmethod
    def validar_bbox(cls, v):
        if len(v) != 4:
            raise ValueError("bbox deve ter 4 valores: [west, south, east, north]")
        west, south, east, north = v
        if not (-180 <= west < east <= 180):
            raise ValueError(f"longitude inválida: west={west}, east={east}")
        if not (-90 <= south < north <= 90):
            raise ValueError(f"latitude inválida: south={south}, north={north}")
        area = (east - west) * (north - south)
        if area > _MAX_AREA_DEG2:
            raise ValueError(
                f"Área do bbox ({area:.1f}°²) excede o máximo permitido "
                f"({_MAX_AREA_DEG2}°² = 5°×5°). Reduza a área de interesse."
            )
        return v

    @field_validator("img_fonte")
    @classmethod
    def validar_img_fonte(cls, v):
        if v not in {"sentinel2", "osm", "mbtiles"}:
            raise ValueError("img_fonte deve ser 'sentinel2', 'osm' ou 'mbtiles'")
        return v

    @field_validator("osm_zoom")
    @classmethod
    def validar_osm_zoom(cls, v):
        if not (12 <= v <= 17):
            raise ValueError("osm_zoom deve estar entre 12 e 17")
        return v

    @field_validator("layers")
    @classmethod
    def validar_layers(cls, v):
        allowed = {"geo", "sol", "img"}   # "blg" removido — não implementado
        invalid = set(v) - allowed
        if invalid:
            raise ValueError(f"Layers inválidas: {invalid}. Permitidas: {allowed}")
        if not v:
            raise ValueError("Selecione ao menos uma camada")
        return v

    @model_validator(mode="after")
    def validar_mbtiles_preview_id(self):
        if self.img_fonte == "mbtiles" and not self.img_preview_id:
            raise ValueError(
                "img_preview_id é obrigatório quando img_fonte='mbtiles'"
            )
        return self


class PreviewRequest(BaseModel):
    """Requisição para iniciar download de tiles para pré-visualização."""
    bbox: list[float]
    source_id: str       # chave em tile_downloader.SOURCES
    zoom: int            # nível de zoom para download
    accepted_terms: bool # obrigatório para qualquer fonte externa

    @field_validator("bbox")
    @classmethod
    def validar_bbox(cls, v):
        if len(v) != 4:
            raise ValueError("bbox deve ter 4 valores: [west, south, east, north]")
        west, south, east, north = v
        if not (-180 <= west < east <= 180):
            raise ValueError(f"longitude inválida: west={west}, east={east}")
        if not (-90 <= south < north <= 90):
            raise ValueError(f"latitude inválida: south={south}, north={north}")
        area = (east - west) * (north - south)
        if area > _MAX_AREA_DEG2:
            raise ValueError(
                f"Área ({area:.1f}°²) excede {_MAX_AREA_DEG2}°²"
            )
        return v

    @field_validator("accepted_terms")
    @classmethod
    def validar_terms(cls, v):
        if not v:
            raise ValueError(
                "É necessário aceitar os termos de responsabilidade para baixar tiles externos."
            )
        return v


class JobStatus(BaseModel):
    job_id: str
    status: Literal["pending", "running", "done", "error"]
    bbox: Optional[list[float]] = None
    layers: Optional[list[str]] = None
    zip_bytes: Optional[int] = None
    arquivos: Optional[list[str]] = None
    erro: Optional[str] = None
    progresso: Optional[list[str]] = None
    cache_hit: Optional[bool] = None
    duracao_s: Optional[float] = None


class JobListItem(BaseModel):
    job_id: str
    status: str
    zip_bytes: Optional[int] = None
    duracao_s: Optional[float] = None
