"""
Infraestrutura: Converte MBTiles → .img + .pal ATDI.

Lê os tiles do arquivo MBTiles gerado por tile_downloader.py,
costura em mosaico PIL, converte para grayscale, reprojeta para UTM
e escreve .img + .pal usando a mesma lógica de osm_tiles.py.
"""

import math
import os
import sqlite3
from pathlib import Path

import numpy as np
from PIL import Image

from src.infraestrutura.adaptadores.atdi_writer import write_img, write_pal_grayscale, _utm_epsg
from src.infraestrutura.adaptadores.osm_tiles import (
    _rgb_to_gray_uint8,
    _reproject_to_utm,
    _tile2deg_nw,
    PIXEL_SIZE_M,
)

TILE_SIZE = 256


def _read_tiles_from_mbtiles(
    mbtiles_path: str,
    x0: int, y0: int, x1: int, y1: int,
    zoom: int,
) -> tuple[Image.Image, tuple]:
    """
    Lê tiles do MBTiles e costura em mosaico PIL.
    Retorna (imagem_RGB, (west, south, east, north)) em WGS84.
    """
    cols = x1 - x0 + 1
    rows = y1 - y0 + 1
    canvas = Image.new("RGB", (cols * TILE_SIZE, rows * TILE_SIZE), (200, 200, 200))

    with sqlite3.connect(f"file:{mbtiles_path}?mode=ro", uri=True) as conn:
        for iy, ty in enumerate(range(y0, y1 + 1)):
            y_tms = (1 << zoom) - 1 - ty   # XYZ → TMS
            for ix, tx in enumerate(range(x0, x1 + 1)):
                row = conn.execute(
                    "SELECT tile_data FROM tiles "
                    "WHERE zoom_level=? AND tile_column=? AND tile_row=?",
                    (zoom, tx, y_tms),
                ).fetchone()
                if row:
                    try:
                        tile = Image.open(__import__("io").BytesIO(row[0])).convert("RGB")
                        canvas.paste(tile, (ix * TILE_SIZE, iy * TILE_SIZE))
                    except Exception:
                        pass   # tile corrompido → mantém cinza

    # Bounding box WGS84 do mosaico
    lat_n, lon_w = _tile2deg_nw(x0, y0, zoom)
    lat_s, lon_e = _tile2deg_nw(x1 + 1, y1 + 1, zoom)
    return canvas, (lon_w, lat_s, lon_e, lat_n)


def _tiles_for_bbox(bbox: tuple, zoom: int) -> tuple[int, int, int, int]:
    """
    Calcula o range de tiles (x0, y0, x1, y1) que cobre o bbox.
    Usa a mesma margem pequena do tile_downloader.
    """
    import math as _m
    west, south, east, north = bbox
    margin = 360.0 / (1 << zoom) * 0.3

    def _deg2xy(lat, lon):
        n = 1 << zoom
        x = int((lon + 180.0) / 360.0 * n)
        lat_r = _m.radians(lat)
        y = int((1.0 - _m.asinh(_m.tan(lat_r)) / _m.pi) / 2.0 * n)
        return max(0, min(n - 1, x)), max(0, min(n - 1, y))

    x0, y0 = _deg2xy(north + margin, west - margin)
    x1, y1 = _deg2xy(south - margin, east + margin)
    return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)


def process(
    mbtiles_path: str,
    bbox: tuple,
    output_dir: str,
    grade: dict = None,
    pixel_size_m: float = PIXEL_SIZE_M,
    progress_cb=None,
) -> tuple[str, str]:
    """
    Converte MBTiles → .img + .pal ATDI.

    Args:
        mbtiles_path: caminho do .mbtiles com os tiles baixados
        bbox:         (west, south, east, north) WGS84 — usado para nome do arquivo
        output_dir:   pasta de saída dos arquivos ATDI
        grade:        dict {xmin, ymin, width, height, utm_epsg} do .geo para alinhamento 2×
        pixel_size_m: tamanho do pixel UTM de saída (padrão 15 m)
        progress_cb:  callback(str) para log

    Returns:
        (img_path, pal_path)
    """
    if not Path(mbtiles_path).exists():
        raise FileNotFoundError(f"MBTiles não encontrado: {mbtiles_path}")

    west, south, east, north = bbox
    os.makedirs(output_dir, exist_ok=True)

    lon_center = (west + east) / 2
    lat_center = (south + north) / 2
    is_south   = lat_center < 0
    utm_epsg   = grade["utm_epsg"] if grade else _utm_epsg(lon_center, is_south)

    # Descobre o zoom do MBTiles
    with sqlite3.connect(f"file:{mbtiles_path}?mode=ro", uri=True) as conn:
        row = conn.execute(
            "SELECT zoom_level FROM tiles ORDER BY zoom_level DESC LIMIT 1"
        ).fetchone()
    if not row:
        raise ValueError("MBTiles vazio — nenhum tile encontrado.")
    zoom = row[0]

    if progress_cb:
        progress_cb(f"[MBTiles→IMG] Lendo tiles do MBTiles (zoom={zoom})...")

    # Determina range de tiles e costura mosaico
    x0, y0, x1, y1 = _tiles_for_bbox(bbox, zoom)
    mosaic, mosaic_bounds = _read_tiles_from_mbtiles(
        mbtiles_path, x0, y0, x1, y1, zoom
    )

    if progress_cb:
        progress_cb(f"[MBTiles→IMG] Mosaico: {mosaic.width}×{mosaic.height}px "
                    f"({(x1-x0+1)}×{(y1-y0+1)} tiles)")

    # Grayscale
    gray = _rgb_to_gray_uint8(mosaic)
    del mosaic

    if progress_cb:
        progress_cb(f"[MBTiles→IMG] Reprojetando para UTM EPSG:{utm_epsg}...")

    # Reprojeção UTM
    dst_arr, xmin_utm, ymin_utm = _reproject_to_utm(
        gray, mosaic_bounds, utm_epsg, grade, pixel_size_m
    )

    # Nome do arquivo baseado no bbox
    lat_i = int(abs(math.floor(lat_center)))
    lon_i = int(abs(math.floor(west)))
    ns = "S" if is_south else "N"
    ew = "W" if west < 0 else "E"
    base_name = f"{lat_i:02d}{ns}{lon_i:03d}{ew}"

    img_path = os.path.join(output_dir, base_name + ".img")
    pal_path = os.path.join(output_dir, base_name + ".pal")

    if progress_cb:
        progress_cb(f"[MBTiles→IMG] Escrevendo {base_name}.img "
                    f"({dst_arr.shape[1]}×{dst_arr.shape[0]}px @ {pixel_size_m:.0f}m)...")

    write_img(
        path=img_path,
        data=dst_arr,
        xmin=xmin_utm,
        ymin=ymin_utm,
        pixel_size=pixel_size_m,
        lon_center=lon_center,
        south=is_south,
    )
    write_pal_grayscale(pal_path)

    if progress_cb:
        sz = os.path.getsize(img_path)
        progress_cb(f"[MBTiles→IMG] IMG gerado: {sz/1024:.0f} KB | {base_name}.pal gerado")

    return img_path, pal_path
