"""
Infraestrutura: Adaptador Sentinel-2 via Planetary Computer (STAC).
Migrado de webapp/processing/sentinel_img.py com suporte a alinhamento de grade.
Gera .img ATDI (uint8, 15m, alinhado 2x ao .geo).
"""

import math
import os
import numpy as np
import rasterio
from rasterio.crs import CRS
from rasterio.warp import reproject, Resampling, calculate_default_transform, transform_bounds
from rasterio.transform import from_bounds as tfrom_bounds
from rasterio.io import MemoryFile

from src.infraestrutura.adaptadores.atdi_writer import write_img, write_pal_grayscale, _utm_epsg

WGS84_CRS    = CRS.from_epsg(4326)
PIXEL_SIZE_M = 15.0   # .img ATDI usa metade do pixel do .sol/.geo


def _find_best_scene(bbox: tuple):
    """Busca a cena Sentinel-2 L2A com menor cobertura de nuvens."""
    try:
        import pystac_client
        import planetary_computer
    except ImportError:
        raise RuntimeError(
            "Instale: pip install pystac-client planetary-computer"
        )
    catalog = pystac_client.Client.open(
        "https://planetarycomputer.microsoft.com/api/stac/v1",
        modifier=planetary_computer.sign_inplace,
    )
    search = catalog.search(
        collections=["sentinel-2-l2a"],
        bbox=bbox,
        datetime="2022-01-01/2024-12-31",
        query={"eo:cloud_cover": {"lt": 20}},
        sortby=["+properties.eo:cloud_cover"],
        max_items=20,
    )
    items = list(search.items())
    if not items:
        raise RuntimeError("Nenhuma cena Sentinel-2 encontrada para esta area.")
    return items[0]


def _download_band(url: str, bbox_wgs: tuple) -> tuple:
    """Le uma banda Sentinel-2 via VSICURL."""
    west, south, east, north = bbox_wgs
    margin = 0.05
    with rasterio.open(url) as ds:
        src_crs = ds.crs
        if src_crs.to_epsg() != 4326:
            w, s, e, n = transform_bounds(
                WGS84_CRS, src_crs,
                west - margin, south - margin, east + margin, north + margin,
            )
        else:
            w, s, e, n = west - margin, south - margin, east + margin, north + margin
        window  = ds.window(w, s, e, n)
        full    = rasterio.windows.Window(0, 0, ds.width, ds.height)
        window  = window.intersection(full)
        data    = ds.read(1, window=window).astype(np.float32)
        transform = ds.window_transform(window)
    return data, transform, src_crs


def _to_8bit(arr: np.ndarray, nodata_val: float = -9999.0) -> np.ndarray:
    """Stretch percentil 2-98 -> uint8 (1-255). Nodata fica 0."""
    valid_mask = arr > nodata_val
    valid = arr[valid_mask]
    if len(valid) == 0:
        return np.zeros(arr.shape, dtype=np.uint8)
    p2, p98 = np.percentile(valid, [2, 98])
    if p98 <= p2:
        p2, p98 = valid.min(), valid.max()
    out = np.zeros(arr.shape, dtype=np.float32)
    out[valid_mask] = np.clip(
        (arr[valid_mask] - p2) / max(p98 - p2, 1.0) * 254 + 1,
        1, 255
    )
    return out.astype(np.uint8)


def process(bbox: tuple, output_dir: str,
            grade: dict = None,
            progress_cb=None) -> str:
    """
    Baixa Sentinel-2 e gera .img ATDI.

    grade: dict com {xmin, ymin, width, height, utm_epsg} do .geo/.sol
           para alinhamento exato (2x a grade).
    Returns: caminho do .img gerado.
    """
    west, south, east, north = bbox
    os.makedirs(output_dir, exist_ok=True)

    lon_center = (west + east) / 2
    lat_center = (south + north) / 2
    is_south   = lat_center < 0
    utm_epsg   = grade["utm_epsg"] if grade else _utm_epsg(lon_center, is_south)
    utm_crs    = CRS.from_epsg(utm_epsg)

    if progress_cb:
        progress_cb("[Sentinel-2] Buscando cena com menos nuvens...")

    item = _find_best_scene(bbox)
    dt   = item.datetime.strftime('%Y-%m-%d')
    cc   = item.properties.get('eo:cloud_cover', '?')
    if progress_cb:
        progress_cb(f"[Sentinel-2] Cena: {dt}  nuvens={cc}%  ({item.id})")

    # Baixa bandas RGB (10m)
    bands = []
    for band_name, label in [('B04', 'Red'), ('B03', 'Green'), ('B02', 'Blue')]:
        if progress_cb:
            progress_cb(f"[Sentinel-2] Baixando {band_name} ({label}, 10m)...")
        url = item.assets[band_name].href
        data, src_transform, src_crs = _download_band(url, bbox)
        bands.append(data)

    r, g, b = bands
    if progress_cb:
        progress_cb(f"[Sentinel-2] Bandas: R={int(r.min())}-{int(r.max())}  "
                    f"G={int(g.min())}-{int(g.max())}  B={int(b.min())}-{int(b.max())}")

    # Salva TIFF RGB original para auditoria
    try:
        tif_path = os.path.join(output_dir, "original_sentinel2.tif")
        profile = {
            "driver": "GTiff", "dtype": "uint16",
            "width": r.shape[1], "height": r.shape[0],
            "count": 3, "crs": src_crs, "transform": src_transform,
            "compress": "deflate", "nodata": 0,
        }
        with rasterio.open(tif_path, "w", **profile) as dst:
            dst.write(r.astype("uint16"), 1)
            dst.write(g.astype("uint16"), 2)
            dst.write(b.astype("uint16"), 3)
        if progress_cb:
            sz = os.path.getsize(tif_path)
            progress_cb(f"[Sentinel-2] TIFF original salvo: {sz/1024/1024:.1f} MB")
    except Exception as ex:
        if progress_cb:
            progress_cb(f"[Sentinel-2] AVISO: nao foi possivel salvar TIFF original: {ex}")

    # Mascaramento de nodata (Sentinel usa 0 como nodata em cada banda)
    nodata_mask = (r == 0) | (g == 0) | (b == 0)

    # Luminosidade ITU-R BT.601
    gray = 0.299 * r + 0.587 * g + 0.114 * b
    gray[nodata_mask] = np.nan

    if progress_cb:
        valid_pct = 100 * np.sum(~nodata_mask) / nodata_mask.size
        progress_cb(f"[Sentinel-2] Pixels validos: {valid_pct:.1f}%  "
                    f"gray={int(np.nanmin(gray))}-{int(np.nanmax(gray))}")

    if progress_cb:
        progress_cb("[Sentinel-2] Reprojetando para UTM 15m e normalizando 8-bit...")

    # Grade de saida
    if grade is not None:
        w_utm      = grade["xmin"]
        s_utm      = grade["ymin"]
        dst_width  = grade["width"] * 2
        dst_height = grade["height"] * 2
        e_utm      = w_utm + grade["width"]  * 30.0
        n_utm      = s_utm + grade["height"] * 30.0
        dst_transform = tfrom_bounds(w_utm, s_utm, e_utm, n_utm, dst_width, dst_height)
    else:
        w_utm, s_utm, e_utm, n_utm = transform_bounds(
            WGS84_CRS, utm_crs, west, south, east, north)
        dst_width  = max(1, round((e_utm - w_utm) / PIXEL_SIZE_M))
        dst_height = max(1, round((n_utm - s_utm) / PIXEL_SIZE_M))
        dst_transform = tfrom_bounds(w_utm, s_utm, e_utm, n_utm, dst_width, dst_height)

    # NaN -> -9999 para reproject
    gray_reproj = np.where(np.isnan(gray), -9999.0, gray).astype(np.float32)
    dst_arr = np.full((dst_height, dst_width), -9999.0, dtype=np.float32)

    reproject(
        source=gray_reproj,
        destination=dst_arr,
        src_transform=src_transform,
        src_crs=src_crs,
        dst_transform=dst_transform,
        dst_crs=utm_crs,
        resampling=Resampling.bilinear,
        src_nodata=-9999.0,
        dst_nodata=-9999.0,
    )

    img_8bit = _to_8bit(dst_arr)

    lat_i = int(abs(math.floor(lat_center)))
    lon_i = int(abs(math.floor(west)))
    ns = "S" if is_south else "N"
    ew = "W" if west < 0 else "E"
    filename = f"{lat_i:02d}{ns}{lon_i:03d}{ew}.img"
    out_path  = os.path.join(output_dir, filename)

    if progress_cb:
        progress_cb(f"[Sentinel-2] Escrevendo {filename} "
                    f"({dst_width}x{dst_height}px, {PIXEL_SIZE_M:.0f}m UTM)...")

    write_img(
        path=out_path,
        data=img_8bit,
        xmin=w_utm,
        ymin=s_utm,
        pixel_size=PIXEL_SIZE_M,
        lon_center=lon_center,
        south=is_south,
    )

    pal_path = out_path.replace(".img", ".pal")
    write_pal_grayscale(pal_path)

    if progress_cb:
        sz = os.path.getsize(out_path)
        valid = img_8bit[img_8bit > 0]
        rng = f"{valid.min()}-{valid.max()}" if len(valid) else "sem pixels"
        progress_cb(f"[Sentinel-2] IMG gerado: {sz/1024:.0f} KB | range {rng}")
        progress_cb(f"[Sentinel-2] PAL grayscale gerado: {filename.replace('.img', '.pal')}")

    return out_path, pal_path, {"cena_id": item.id, "data": dt, "nuvens_pct": cc}
