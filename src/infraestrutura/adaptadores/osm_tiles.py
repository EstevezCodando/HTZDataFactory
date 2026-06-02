"""
Infraestrutura: Adaptador OpenStreetMap Tiles → .img ATDI.

Baixa tiles OSM no zoom informado (padrão: 15), costura em mosaico,
reprojeta para UTM e escreve .img alinhado à grade do .geo/.sol.

Zoom 15 → ~4.8 m/pixel na linha do Equador (melhor que Sentinel-2 10m).
Zoom 14 → ~9.6 m/pixel  |  Zoom 16 → ~2.4 m/pixel

Política de uso OSM: https://operations.osmfoundation.org/policies/tiles/
- User-Agent identificando a aplicação (obrigatório)
- Não usar em produção com centenas de requisições simultâneas
- Para uso intensivo: Overpass + mbtiles local, ou tile mirror próprio
"""

import io
import math
import os
import time
import urllib.request
from pathlib import Path

import numpy as np
from PIL import Image
import rasterio
from rasterio.crs import CRS
from rasterio.transform import from_bounds
from rasterio.warp import reproject, Resampling, calculate_default_transform

from src.infraestrutura.adaptadores.atdi_writer import write_img, write_pal_grayscale, _utm_epsg

# ── Constantes ────────────────────────────────────────────────────────────────
TILE_SIZE   = 256          # pixels por tile OSM
DEFAULT_ZOOM = 15
PIXEL_SIZE_M = 15.0        # .img ATDI: metade do .geo/.sol (30m)
WGS84_CRS    = CRS.from_epsg(4326)

_USER_AGENT = "HTZDataFactory/1.3 (+https://github.com/htzdatafactory; RF-planning)"
_DELAY_S    = 0.05         # delay entre tiles (gentileza com o servidor OSM)


# ── Conversão coordenadas ↔ tile number ──────────────────────────────────────

def _deg2tile(lat: float, lon: float, zoom: int) -> tuple[int, int]:
    """Retorna (x, y) do tile OSM que contém (lat, lon) no zoom dado."""
    n = 1 << zoom
    x = int((lon + 180.0) / 360.0 * n)
    lat_r = math.radians(lat)
    y = int((1.0 - math.asinh(math.tan(lat_r)) / math.pi) / 2.0 * n)
    # Clamp para evitar overflow nos extremos
    x = max(0, min(n - 1, x))
    y = max(0, min(n - 1, y))
    return x, y


def _tile2deg_nw(x: int, y: int, zoom: int) -> tuple[float, float]:
    """Retorna (lat, lon) do canto noroeste do tile (x, y)."""
    n = 1 << zoom
    lon = x / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    return lat, lon


def _tile_bbox_wgs84(x: int, y: int, zoom: int) -> tuple[float, float, float, float]:
    """Retorna (west, south, east, north) em WGS84 do tile (x, y)."""
    lat_n, lon_w = _tile2deg_nw(x, y, zoom)
    lat_s, lon_e = _tile2deg_nw(x + 1, y + 1, zoom)
    return lon_w, lat_s, lon_e, lat_n


# ── Download ──────────────────────────────────────────────────────────────────

def _download_tile(x: int, y: int, zoom: int, retries: int = 3) -> Image.Image | None:
    url = f"https://tile.openstreetmap.org/{zoom}/{x}/{y}.png"
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return Image.open(io.BytesIO(resp.read())).convert("RGB")
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(1.0 * (attempt + 1))
            else:
                return None   # tile não disponível — será preenchido com cinza


def _stitch_tiles(x0: int, y0: int, x1: int, y1: int, zoom: int,
                  progress_cb=None) -> tuple[Image.Image, tuple]:
    """
    Baixa e costura tiles de (x0,y0) até (x1,y1) inclusive.
    Retorna (imagem_PIL, (west, south, east, north)) em WGS84.
    """
    cols = x1 - x0 + 1
    rows = y1 - y0 + 1
    total = cols * rows

    canvas = Image.new("RGB", (cols * TILE_SIZE, rows * TILE_SIZE), (200, 200, 200))

    n_ok = 0
    for iy, ty in enumerate(range(y0, y1 + 1)):
        for ix, tx in enumerate(range(x0, x1 + 1)):
            tile = _download_tile(tx, ty, zoom)
            if tile:
                canvas.paste(tile, (ix * TILE_SIZE, iy * TILE_SIZE))
                n_ok += 1
            if progress_cb and (ix + iy * cols) % max(1, total // 10) == 0:
                progress_cb(f"[OSM] {n_ok}/{total} tiles baixados...")
            time.sleep(_DELAY_S)

    # Bounding box WGS84 do mosaico completo
    lat_n, lon_w = _tile2deg_nw(x0, y0, zoom)
    lat_s, lon_e = _tile2deg_nw(x1 + 1, y1 + 1, zoom)
    return canvas, (lon_w, lat_s, lon_e, lat_n)


# ── Conversão para grayscale 8-bit ────────────────────────────────────────────

def _rgb_to_gray_uint8(img: Image.Image) -> np.ndarray:
    """ITU-R BT.601: Y = 0.299R + 0.587G + 0.114B, saída 1-255 (0 = nodata)."""
    arr = np.array(img, dtype=np.float32)
    gray = 0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1] + 0.114 * arr[:, :, 2]
    # Normaliza para 1-255 usando percentil 2-98 (ignora extremos)
    p2, p98 = np.percentile(gray, [2, 98])
    if p98 > p2:
        gray = (gray - p2) / (p98 - p2) * 254.0 + 1.0
    gray = np.clip(gray, 1, 255).astype(np.uint8)
    return gray


# ── Reprojeção WGS84 → UTM ────────────────────────────────────────────────────

def _reproject_to_utm(arr: np.ndarray, src_bounds: tuple,
                      utm_epsg: int, grade: dict | None,
                      pixel_size_m: float) -> tuple[np.ndarray, float, float]:
    """
    Reprojeta array WGS84 para UTM.
    Se grade fornecida, alinha exatamente 2× a grade do .geo/.sol.
    Retorna (dst_arr, xmin_utm, ymin_utm).
    """
    west, south, east, north = src_bounds
    utm_crs = CRS.from_epsg(utm_epsg)

    if grade:
        dst_width  = grade["width"]  * 2
        dst_height = grade["height"] * 2
        xmin_utm   = grade["xmin"]
        ymin_utm   = grade["ymin"]
        xmax_utm   = xmin_utm + grade["width"]  * 30.0
        ymax_utm   = ymin_utm + grade["height"] * 30.0
        dst_transform = from_bounds(xmin_utm, ymin_utm, xmax_utm, ymax_utm,
                                    dst_width, dst_height)
    else:
        src_h, src_w = arr.shape
        dst_transform, dst_width, dst_height = calculate_default_transform(
            src_crs=WGS84_CRS, dst_crs=utm_crs,
            width=src_w, height=src_h,
            left=west, bottom=south, right=east, top=north,
            resolution=pixel_size_m,
        )
        xmin_utm = dst_transform.c
        ymin_utm = dst_transform.f - dst_height * pixel_size_m

    src_transform = from_bounds(west, south, east, north, arr.shape[1], arr.shape[0])

    dst_arr = np.zeros((dst_height, dst_width), dtype=np.uint8)
    reproject(
        source=arr,
        destination=dst_arr,
        src_transform=src_transform,
        src_crs=WGS84_CRS,
        dst_transform=dst_transform,
        dst_crs=utm_crs,
        resampling=Resampling.bilinear,
        src_nodata=0,
        dst_nodata=0,
    )
    return dst_arr, xmin_utm, ymin_utm


# ── Ponto de entrada público ──────────────────────────────────────────────────

def process(bbox: tuple, output_dir: str,
            zoom: int = DEFAULT_ZOOM,
            grade: dict = None,
            pixel_size_m: float = PIXEL_SIZE_M,
            base_name: str = None,
            progress_cb=None) -> str:
    """
    Baixa tiles OSM para o bbox, costura, reprojeta para UTM e escreve .img ATDI.

    Args:
        bbox:         (west, south, east, north) em graus WGS84
        output_dir:   pasta de saída
        zoom:         zoom OSM (14-16 recomendado; padrão 15 ≈ 4.8 m/px)
        grade:        dict {xmin, ymin, width, height, utm_epsg} do .geo para alinhamento
        pixel_size_m: tamanho do pixel UTM de saída (padrão 15 m)
        base_name:    prefixo do arquivo de saída (ex: "16S048W")
        progress_cb:  callback(str) para log de progresso

    Returns:
        Caminho do arquivo .img gerado
    """
    west, south, east, north = bbox
    os.makedirs(output_dir, exist_ok=True)

    lon_center = (west + east) / 2
    lat_center = (south + north) / 2
    is_south   = lat_center < 0
    utm_epsg   = grade["utm_epsg"] if grade else _utm_epsg(lon_center, is_south)

    # ── Calcula quais tiles cobrem o bbox ────────────────────────────────────
    # Margem de 0.5 tile para garantir cobertura completa após reprojeção
    margin = 360.0 / (1 << zoom) * 0.5
    x0, y0 = _deg2tile(north + margin, west  - margin, zoom)   # NW → menor y
    x1, y1 = _deg2tile(south - margin, east  + margin, zoom)   # SE → maior y
    # y cresce para sul no sistema OSM
    y0, y1 = min(y0, y1), max(y0, y1)
    x0, x1 = min(x0, x1), max(x0, x1)

    n_tiles = (x1 - x0 + 1) * (y1 - y0 + 1)
    if progress_cb:
        progress_cb(f"[OSM] Zoom {zoom} → {n_tiles} tiles "
                    f"({x1-x0+1} colunas × {y1-y0+1} linhas)...")

    # ── Baixa e costura ──────────────────────────────────────────────────────
    mosaic, mosaic_bounds = _stitch_tiles(x0, y0, x1, y1, zoom, progress_cb)

    if progress_cb:
        progress_cb(f"[OSM] Mosaico: {mosaic.width}×{mosaic.height}px "
                    f"({mosaic.width*mosaic.height/1e6:.1f} Mpx)")

    # ── Grayscale ────────────────────────────────────────────────────────────
    gray = _rgb_to_gray_uint8(mosaic)
    del mosaic   # libera RAM

    if progress_cb:
        progress_cb(f"[OSM] Reprojetando para UTM EPSG:{utm_epsg}...")

    # ── Reprojeção UTM ───────────────────────────────────────────────────────
    dst_arr, xmin_utm, ymin_utm = _reproject_to_utm(
        gray, mosaic_bounds, utm_epsg, grade, pixel_size_m
    )

    # ── Nome do arquivo ──────────────────────────────────────────────────────
    if not base_name:
        import math as _m
        lat_i = int(abs(_m.floor(lat_center)))
        lon_i = int(abs(_m.floor(west)))
        ns = "S" if is_south else "N"
        ew = "W" if west < 0 else "E"
        base_name = f"{lat_i:02d}{ns}{lon_i:03d}{ew}"

    img_path = os.path.join(output_dir, base_name + ".img")

    if progress_cb:
        progress_cb(f"[OSM] Escrevendo {base_name}.img "
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

    pal_path = img_path.replace(".img", ".pal")
    write_pal_grayscale(pal_path)

    if progress_cb:
        sz = os.path.getsize(img_path)
        progress_cb(f"[OSM] IMG gerado: {sz/1024:.0f} KB")
        progress_cb(f"[OSM] PAL grayscale gerado: {base_name}.pal")

    return img_path, pal_path
