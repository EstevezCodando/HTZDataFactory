"""
Infraestrutura: Adaptador FABDEM.
Busca celulas no GeoJSON de catalogo, baixa ZIPs por demanda, gera .geo ATDI.
"""

import io
import json
import math
import os
import zipfile
from pathlib import Path
from typing import Optional

import numpy as np
import requests
import rasterio
from rasterio.crs import CRS
from rasterio.io import MemoryFile
from rasterio.merge import merge
from rasterio.transform import from_bounds
from rasterio.warp import reproject, Resampling, calculate_default_transform

from src.dominio.catalogo.entidades import CelulaFABDEM
from src.infraestrutura.adaptadores.atdi_writer import write_geo, _utm_epsg

WGS84_CRS  = CRS.from_epsg(4326)
PIXEL_SIZE_M = 30.0   # metros — resolucao padrao do .geo

# URL base para download dos ZIPs FABDEM
FABDEM_BASE_URL = "https://data.bris.ac.uk/datasets/s5hqmjcdj8yo2ibzi9b4ew3sn"


def _load_catalog(geojson_path: str) -> list[CelulaFABDEM]:
    """Carrega o catalogo GeoJSON de celulas FABDEM."""
    with open(geojson_path, encoding="utf-8") as f:
        fc = json.load(f)
    celulas = []
    for feat in fc.get("features", []):
        p = feat.get("properties", {})
        c = CelulaFABDEM(
            codigo=p.get("codigo_fabdem", ""),
            lat_min=float(p.get("lat_min", 0)),
            lat_max=float(p.get("lat_max", 0)),
            lon_min=float(p.get("lon_min", 0)),
            lon_max=float(p.get("lon_max", 0)),
            bloco_zip=p.get("bloco_zip_10x10", ""),
            arquivo_zip=p.get("arquivo_zip_estimado", ""),
        )
        celulas.append(c)
    return celulas


def _encontrar_tif_local(tif_name: str, fabdem_dir: str) -> Optional[str]:
    """
    Procura o TIF recursivamente em fabdem_dir e seus subdiretórios.
    Os TIFs podem estar em subpastas de blocos extraídos
    (ex: S20W050-S10W040_FABDEM_V1-2/S14W047_FABDEM_V1-2.tif).
    """
    # Primeiro: raiz do diretório
    direct = os.path.join(fabdem_dir, tif_name)
    if os.path.exists(direct):
        return direct
    # Segundo: qualquer subdiretório (busca recursiva)
    for root, dirs, files in os.walk(fabdem_dir):
        if tif_name in files:
            return os.path.join(root, tif_name)
    return None


def _garantir_tif(celula: CelulaFABDEM, fabdem_dir: str,
                  progress_cb=None) -> str:
    """
    Verifica se o TIF ja existe localmente (incluindo subdiretórios de blocos).
    Se nao, baixa o ZIP e extrai apenas o TIF necessario.
    Retorna o caminho local do TIF.
    """
    # Nome do TIF dentro do ZIP (ex: N01W002_FABDEM_V1-2.tif)
    tif_name = f"{celula.codigo}_FABDEM_V1-2.tif"

    # Busca recursiva — TIFs podem estar em subpastas de blocos
    found = _encontrar_tif_local(tif_name, fabdem_dir)
    if found:
        return found

    # Caminho padrão para download novo (na raiz)
    tif_path = os.path.join(fabdem_dir, tif_name)

    if os.path.exists(tif_path):
        return tif_path

    # Precisa baixar o ZIP do bloco
    if not celula.arquivo_zip:
        raise FileNotFoundError(
            f"Celula {celula.codigo} sem URL de download no catalogo. "
            f"Baixe manualmente em: {FABDEM_BASE_URL}"
        )

    zip_url = f"{FABDEM_BASE_URL}/{celula.arquivo_zip}"
    if progress_cb:
        progress_cb(f"Baixando FABDEM {celula.codigo} de {zip_url} ...")

    resp = requests.get(zip_url, stream=True, timeout=120)
    resp.raise_for_status()

    # Extrai apenas o TIF necessario (evita baixar blocos 10x10 inteiros)
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        # Busca o TIF pelo codigo da celula
        matching = [n for n in zf.namelist() if celula.codigo in n and n.endswith(".tif")]
        if not matching:
            # Fallback: lista todos os TIFs disponíveis
            available = [n for n in zf.namelist() if n.endswith(".tif")]
            raise FileNotFoundError(
                f"TIF {tif_name} nao encontrado no ZIP. "
                f"Disponiveis: {available[:5]}"
            )
        zf.extract(matching[0], fabdem_dir)
        # Move para o nivel correto se necessario
        extracted = os.path.join(fabdem_dir, matching[0])
        if extracted != tif_path:
            os.makedirs(os.path.dirname(tif_path), exist_ok=True)
            os.rename(extracted, tif_path)

    if progress_cb:
        sz = os.path.getsize(tif_path)
        progress_cb(f"FABDEM {celula.codigo} baixado: {sz/1024/1024:.1f} MB")

    return tif_path


def process(bbox: tuple, output_dir: str,
            geojson_path: str, fabdem_dir: str,
            progress_cb=None) -> tuple[str, dict]:
    """
    Gera .geo ATDI a partir de tiles FABDEM para o bbox.

    Returns:
        (caminho_do_geo, grade) onde grade = {xmin, ymin, width, height}
        para alinhamento do .img Sentinel-2.
    """
    west, south, east, north = bbox
    os.makedirs(output_dir, exist_ok=True)

    lon_center = (west + east) / 2
    lat_center = (south + north) / 2
    is_south   = lat_center < 0
    utm_epsg   = _utm_epsg(lon_center, is_south)
    utm_crs    = CRS.from_epsg(utm_epsg)

    if progress_cb:
        progress_cb(f"[FABDEM] Consultando catalogo GeoJSON...")

    celulas = _load_catalog(geojson_path)
    relevantes = [c for c in celulas if c.intersecta(west, south, east, north)]

    if not relevantes:
        raise RuntimeError(
            f"Nenhuma celula FABDEM encontrada para bbox {bbox}. "
            "Verifique o catalogo ou a area de interesse."
        )

    if progress_cb:
        progress_cb(f"[FABDEM] {len(relevantes)} celula(s): "
                    f"{[c.codigo for c in relevantes]}")

    # Garante TIFs locais
    tif_paths = []
    for c in relevantes:
        tif = _garantir_tif(c, fabdem_dir, progress_cb)
        tif_paths.append(tif)

    # Merge com clip no bbox + margem
    margin = 0.05
    clip_bounds = (west - margin, south - margin, east + margin, north + margin)

    datasets = [rasterio.open(p) for p in tif_paths]
    mosaic, wgs_transform = merge(datasets, bounds=clip_bounds)
    for ds in datasets:
        ds.close()

    src_data = mosaic[0].astype(np.float32)

    if progress_cb:
        progress_cb(f"[FABDEM] Reprojetando para UTM EPSG:{utm_epsg} @ {PIXEL_SIZE_M}m...")

    # Calcula transform UTM
    dst_transform, dst_width, dst_height = calculate_default_transform(
        src_crs=WGS84_CRS,
        dst_crs=utm_crs,
        width=mosaic.shape[2],
        height=mosaic.shape[1],
        left=west, bottom=south, right=east, top=north,
        resolution=PIXEL_SIZE_M,
    )

    dst_arr = np.zeros((dst_height, dst_width), dtype=np.float32)
    reproject(
        source=src_data,
        destination=dst_arr,
        src_transform=wgs_transform,
        src_crs=WGS84_CRS,
        dst_transform=dst_transform,
        dst_crs=utm_crs,
        resampling=Resampling.bilinear,
        src_nodata=-9999.0,
        dst_nodata=-9999.0,
    )

    # Substitui nodata por 0 (nivel do mar) para compatibilidade HTZ
    dst_arr[dst_arr <= -9000] = 0
    elev_int16 = np.clip(dst_arr, -500, 9000).astype(np.int16)

    xmin_utm = dst_transform.c
    ymax_utm = dst_transform.f
    ymin_utm = ymax_utm - dst_height * PIXEL_SIZE_M

    # Nome do arquivo
    lat_i = int(abs(math.floor(lat_center)))
    lon_i = int(abs(math.floor(west)))
    ns = "S" if is_south else "N"
    ew = "W" if west < 0 else "E"
    filename = f"{lat_i:02d}{ns}{lon_i:03d}{ew}.geo"
    out_path  = os.path.join(output_dir, filename)

    if progress_cb:
        progress_cb(f"[FABDEM] Escrevendo {filename} "
                    f"({dst_width}x{dst_height}px, {PIXEL_SIZE_M:.0f}m UTM)...")

    write_geo(
        path=out_path,
        data=elev_int16,
        xmin=xmin_utm,
        ymin=ymin_utm,
        pixel_size=PIXEL_SIZE_M,
        lon_center=lon_center,
        south=is_south,
    )

    if progress_cb:
        sz = os.path.getsize(out_path)
        elev_vals = elev_int16[elev_int16 != 0]
        elev_range = (int(elev_vals.min()), int(elev_vals.max())) if len(elev_vals) else (0, 0)
        progress_cb(f"[FABDEM] GEO gerado: {sz/1024:.0f} KB, "
                    f"elevacao {elev_range[0]}-{elev_range[1]}m")

    grade = {
        "xmin": xmin_utm,
        "ymin": ymin_utm,
        "width": dst_width,
        "height": dst_height,
        "pixel_m": PIXEL_SIZE_M,
        "utm_epsg": utm_epsg,
    }
    return out_path, grade
