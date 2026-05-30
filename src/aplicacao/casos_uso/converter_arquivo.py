"""
Caso de Uso: Converter arquivo raster enviado pelo usuário para formato ATDI.

Suporta:
  tipo=dem    → GeoTIFF de elevação (qualquer projeção) → .geo
  tipo=clutter→ GeoTIFF de classes de uso do solo      → .sol + .pal
  tipo=img    → GeoTIFF RGB ou banda única              → .img

O arquivo é reprojetado automaticamente para UTM e gravado no formato
binário ATDI nativo (header 1010 bytes + dados brutos).
"""

import math
import os
import zipfile
from pathlib import Path

import numpy as np
import rasterio
from rasterio.crs import CRS
from rasterio.warp import reproject, Resampling, calculate_default_transform

from src.dominio.processamento.servicos import (
    carregar_tabela_reclassificacao,
    reclassifica_clutter,
    aplica_onion,
    PALETTE_FLAT,
)
from src.infraestrutura.adaptadores.atdi_writer import (
    write_geo, write_sol, write_pal, write_img, _utm_epsg,
)

WGS84 = CRS.from_epsg(4326)
PIXEL_GEO = 30.0   # metros — .geo e .sol
PIXEL_IMG = 15.0   # metros — .img


def _centro_bbox(src: rasterio.DatasetReader):
    """Extrai lon/lat do centro a partir do dataset (reprojetando se necessário)."""
    b = src.bounds
    cx = (b.left + b.right) / 2
    cy = (b.top  + b.bottom) / 2
    if src.crs and src.crs != WGS84:
        from rasterio.warp import transform as _tf
        xs, ys = _tf(src.crs, WGS84, [cx], [cy])
        cx, cy = xs[0], ys[0]
    return cx, cy   # lon, lat


def _ler_tfw(tif_path: str):
    """Tenta ler .tfw e retorna (affine, WGS84) ou (None, None)."""
    from rasterio.transform import from_origin
    tfw = Path(tif_path).with_suffix(".tfw")
    if not tfw.exists():
        return None, None
    vals = [float(l.strip()) for l in tfw.read_text().splitlines() if l.strip()]
    if len(vals) < 6:
        return None, None
    px_x, _, _, px_y, lon_ul, lat_ul = vals
    return from_origin(lon_ul, lat_ul, abs(px_x), abs(px_y)), WGS84


def _abrir(tif_path: str):
    """
    Abre o dataset. Se não tiver CRS, tenta .tfw (assume WGS84).
    Retorna (dataset_com_crs_correto, transform_a_usar).
    """
    ds = rasterio.open(tif_path)
    if ds.crs:
        return ds, ds.transform
    # Sem CRS → tenta .tfw
    tf, crs = _ler_tfw(tif_path)
    if tf and crs:
        return ds, tf   # retorna dataset + transform externo
    raise ValueError(
        "O arquivo não possui sistema de coordenadas (CRS). "
        "Forneça um GeoTIFF projetado (WGS84, UTM, SIRGAS2000 etc) "
        "ou um arquivo .tfw com as coordenadas no mesmo diretório."
    )


def _reproject_to_utm(src_data: np.ndarray, src_transform, src_crs: CRS,
                      bbox_wgs84: tuple, pixel_m: float,
                      resampling=Resampling.bilinear,
                      nodata_src=-9999.0, nodata_dst=-9999.0):
    """
    Reprojeta src_data de src_crs → UTM calculado a partir do centro do bbox.
    Retorna (dst_array, dst_transform, utm_epsg, lon_center, is_south).
    """
    west, south, east, north = bbox_wgs84
    lon_center = (west + east) / 2
    lat_center = (south + north) / 2
    is_south   = lat_center < 0
    utm_epsg   = _utm_epsg(lon_center, is_south)
    utm_crs    = CRS.from_epsg(utm_epsg)

    dst_transform, dst_w, dst_h = calculate_default_transform(
        src_crs=src_crs,
        dst_crs=utm_crs,
        width=src_data.shape[-1],
        height=src_data.shape[-2],
        left=west, bottom=south, right=east, top=north,
        resolution=pixel_m,
    )

    dst = np.full((dst_h, dst_w), nodata_dst, dtype=src_data.dtype)
    reproject(
        source=src_data,
        destination=dst,
        src_transform=src_transform,
        src_crs=src_crs,
        dst_transform=dst_transform,
        dst_crs=utm_crs,
        resampling=resampling,
        src_nodata=nodata_src,
        dst_nodata=nodata_dst,
    )
    return dst, dst_transform, utm_epsg, lon_center, is_south


# ──────────────────────────────────────────────────────────────────────────────
# Conversores por tipo
# ──────────────────────────────────────────────────────────────────────────────

def converter_dem(tif_path: str, output_dir: str,
                  reclassificacao_csv: str = "",
                  progress_cb=None) -> list[str]:
    """
    Converte GeoTIFF de elevação → .geo ATDI.
    Aceita qualquer unidade (metros ou pés — detecta automaticamente via metadados GDAL)
    e qualquer projeção.
    """
    if progress_cb: progress_cb("[DEM] Abrindo arquivo de elevação...")
    ds, src_tf = _abrir(tif_path)
    try:
        src_crs = ds.crs or WGS84
        data = ds.read(1).astype(np.float32)
        nodata_val = ds.nodata if ds.nodata is not None else -9999.0
        data[data == nodata_val] = -9999.0
        data[data < -500] = -9999.0   # valores absurdos → nodata

        # Bbox WGS84
        from rasterio.warp import transform_bounds
        if src_crs == WGS84:
            b = ds.bounds
            bbox = (b.left, b.bottom, b.right, b.top)
        else:
            bbox = transform_bounds(src_crs, WGS84, *ds.bounds)
    finally:
        ds.close()

    if progress_cb: progress_cb(f"[DEM] Bbox WGS84: {[round(v,4) for v in bbox]}")

    dst, dst_tf, utm_epsg, lon_c, is_s = _reproject_to_utm(
        data, src_tf, src_crs, bbox, PIXEL_GEO,
        resampling=Resampling.bilinear,
        nodata_src=-9999.0, nodata_dst=-9999.0,
    )
    if progress_cb: progress_cb(f"[DEM] Reprojetado para UTM EPSG:{utm_epsg} "
                                 f"({dst.shape[1]}×{dst.shape[0]}px @ {PIXEL_GEO:.0f}m)")

    dst[dst <= -500] = 0   # nodata → nível do mar
    elev = np.clip(dst, -500, 9000).astype(np.int16)

    xmin = dst_tf.c
    ymax = dst_tf.f
    ymin = ymax - elev.shape[0] * PIXEL_GEO

    lat_i = int(abs(math.floor((bbox[1]+bbox[3])/2)))
    lon_i = int(abs(math.floor(bbox[0])))
    ns = "S" if is_s else "N"
    ew = "W" if bbox[0] < 0 else "E"
    fname = f"{lat_i:02d}{ns}{lon_i:03d}{ew}.geo"
    out   = os.path.join(output_dir, fname)

    write_geo(out, elev, xmin, ymin, PIXEL_GEO, lon_c, is_s)
    sz = os.path.getsize(out)
    if progress_cb: progress_cb(f"[DEM] ✓ {fname} gerado — {sz/1024:.0f} KB "
                                 f"| elev {int(elev[elev!=0].min()) if (elev!=0).any() else 0}–"
                                 f"{int(elev.max())} m")
    return [out]


def converter_clutter(tif_path: str, output_dir: str,
                      reclassificacao_csv: str,
                      aplicar_onion: bool = True,
                      formato_clutter: str = "mapbiomas",
                      progress_cb=None) -> list[str]:
    """
    Converte GeoTIFF de uso/cobertura do solo → .sol + .pal ATDI.
    formato_clutter='mapbiomas' → aplica tabela CSV de reclassificação.
    formato_clutter='atdi'      → usa os valores do raster diretamente (0–9).
    """
    if progress_cb: progress_cb("[Clutter] Abrindo arquivo de clutter...")
    ds, src_tf = _abrir(tif_path)
    try:
        src_crs = ds.crs or WGS84
        data = ds.read(1).astype(np.uint8)
        from rasterio.warp import transform_bounds
        bbox = (transform_bounds(src_crs, WGS84, *ds.bounds)
                if src_crs != WGS84 else (*ds.bounds,))
    finally:
        ds.close()

    dst, dst_tf, utm_epsg, lon_c, is_s = _reproject_to_utm(
        data, src_tf, src_crs, bbox, PIXEL_GEO,
        resampling=Resampling.nearest,
        nodata_src=0, nodata_dst=0,
    )
    if progress_cb: progress_cb(f"[Clutter] Reprojetado para UTM EPSG:{utm_epsg} "
                                 f"({dst.shape[1]}×{dst.shape[0]}px @ {PIXEL_GEO:.0f}m)")

    if formato_clutter == "atdi":
        # Valores já são códigos ATDI — apenas clipa para 0–9
        if progress_cb: progress_cb("[Clutter] Formato ATDI direto — sem reclassificação")
        clutter = np.clip(dst, 0, 9).astype(np.uint8)
    else:
        if progress_cb: progress_cb("[Clutter] Carregando tabela de reclassificação MapBiomas...")
        tabela = carregar_tabela_reclassificacao(reclassificacao_csv)
        if progress_cb: progress_cb(f"[Clutter] {len(tabela)} mapeamentos carregados")
        clutter = reclassifica_clutter(dst, tabela)

    urban_px = int(np.sum(clutter == 2))
    if aplicar_onion and urban_px > 0:
        if progress_cb: progress_cb(f"[Clutter] Aplicando efeito cebola urbano ({urban_px}px)...")
        clutter = aplica_onion(clutter, PIXEL_GEO)

    xmin = dst_tf.c
    ymax = dst_tf.f
    ymin = ymax - clutter.shape[0] * PIXEL_GEO

    lat_i = int(abs(math.floor((bbox[1]+bbox[3])/2)))
    lon_i = int(abs(math.floor(bbox[0])))
    ns = "S" if is_s else "N"
    ew = "W" if bbox[0] < 0 else "E"
    base  = f"{lat_i:02d}{ns}{lon_i:03d}{ew}"
    sol   = os.path.join(output_dir, base + ".sol")
    pal   = os.path.join(output_dir, base + ".pal")

    write_sol(sol, clutter, xmin, ymin, PIXEL_GEO, lon_c, is_s)
    write_pal(pal, PALETTE_FLAT)
    sz = os.path.getsize(sol)
    unique = np.unique(clutter[clutter > 0])
    if progress_cb: progress_cb(f"[Clutter] ✓ {base}.sol gerado — {sz/1024:.0f} KB "
                                 f"| {len(unique)} classes ATDI")
    return [sol, pal]


def converter_img(tif_path: str, output_dir: str,
                  reclassificacao_csv: str = "",
                  progress_cb=None) -> list[str]:
    """
    Converte GeoTIFF de imagem (RGB ou banda única) → .img ATDI (8-bit, 15m UTM).
    """
    if progress_cb: progress_cb("[IMG] Abrindo arquivo de imagem...")
    ds, src_tf = _abrir(tif_path)
    try:
        src_crs = ds.crs or WGS84
        nb = ds.count
        if nb >= 3:
            r = ds.read(1).astype(np.float32)
            g = ds.read(2).astype(np.float32)
            b = ds.read(3).astype(np.float32)
            nodata = ds.nodata or 0
            mask = (r == nodata) | (g == nodata) | (b == nodata)
            gray = 0.299 * r + 0.587 * g + 0.114 * b
            gray[mask] = np.nan
        else:
            gray = ds.read(1).astype(np.float32)
            nodata = ds.nodata
            if nodata is not None:
                gray[gray == nodata] = np.nan

        from rasterio.warp import transform_bounds
        bbox = (transform_bounds(src_crs, WGS84, *ds.bounds)
                if src_crs != WGS84 else (ds.bounds.left, ds.bounds.bottom,
                                           ds.bounds.right, ds.bounds.top))
    finally:
        ds.close()

    # Normaliza para 8-bit ignorando NaN
    valid = gray[~np.isnan(gray)]
    if len(valid) == 0:
        raise ValueError("Imagem sem pixels válidos.")
    p2, p98 = np.nanpercentile(gray, [2, 98])
    gray = np.clip(gray, p2, p98)
    gray = ((gray - p2) / (p98 - p2 + 1e-6) * 254 + 1).astype(np.float32)
    gray[np.isnan(gray)] = 0
    data = gray.astype(np.uint8)

    dst, dst_tf, utm_epsg, lon_c, is_s = _reproject_to_utm(
        data, src_tf, src_crs, bbox, PIXEL_IMG,
        resampling=Resampling.bilinear,
        nodata_src=0, nodata_dst=0,
    )
    if progress_cb: progress_cb(f"[IMG] Reprojetado para UTM EPSG:{utm_epsg} "
                                 f"({dst.shape[1]}×{dst.shape[0]}px @ {PIXEL_IMG:.0f}m)")

    xmin = dst_tf.c
    ymax = dst_tf.f
    ymin = ymax - dst.shape[0] * PIXEL_IMG

    lat_i = int(abs(math.floor((bbox[1]+bbox[3])/2)))
    lon_i = int(abs(math.floor(bbox[0])))
    ns = "S" if is_s else "N"
    ew = "W" if bbox[0] < 0 else "E"
    fname = f"{lat_i:02d}{ns}{lon_i:03d}{ew}.img"
    out   = os.path.join(output_dir, fname)

    write_img(out, dst, xmin, ymin, PIXEL_IMG, lon_c, is_s)
    sz = os.path.getsize(out)
    if progress_cb: progress_cb(f"[IMG] ✓ {fname} gerado — {sz/1024:.0f} KB")
    return [out]


# ──────────────────────────────────────────────────────────────────────────────
# Dispatcher principal
# ──────────────────────────────────────────────────────────────────────────────

CONVERSORES = {
    "dem":     converter_dem,
    "clutter": converter_clutter,
    "img":     converter_img,
}


def executar(job_id: str, tif_path: str, tipo: str,
             output_dir: str, reclassificacao_csv: str,
             aplicar_onion: bool = True,
             formato_clutter: str = "mapbiomas") -> dict:
    """
    Ponto de entrada principal. Converte o arquivo enviado e empacota em ZIP.
    Retorna dict compatível com JobStatus.
    """
    import time, zipfile, json
    from datetime import datetime, timezone
    from src.infraestrutura.logging.setup import JobLogger

    t0   = time.monotonic()
    jlog = JobLogger(job_id)
    job_dir = Path(output_dir) / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    conversor = CONVERSORES.get(tipo)
    if not conversor:
        return {"status": "error", "job_id": job_id,
                "erro": f"Tipo '{tipo}' inválido. Use: dem, clutter, img",
                "progresso": jlog.get_lines()}

    try:
        extra = {}
        if tipo == "clutter":
            extra["aplicar_onion"]    = aplicar_onion
            extra["formato_clutter"]  = formato_clutter
        arquivos = conversor(
            tif_path=tif_path,
            output_dir=str(job_dir),
            reclassificacao_csv=reclassificacao_csv,
            progress_cb=jlog.info,
            **extra,
        )

        elapsed = round(time.monotonic() - t0, 1)
        manifesto = {
            "job_id": job_id,
            "gerado_em": datetime.now(timezone.utc).isoformat(),
            "duracao_s": elapsed,
            "tipo": tipo,
            "arquivo_origem": Path(tif_path).name,
            "disclaimer": (
                "Arquivo convertido automaticamente para formato ATDI. "
                "Valide o georeferenciamento antes do uso operacional."
            ),
        }
        mpath = job_dir / "manifesto.json"
        mpath.write_text(json.dumps(manifesto, ensure_ascii=False, indent=2))
        arquivos.append(str(mpath))

        zip_path = job_dir / f"htz_upload_{job_id}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in arquivos:
                if os.path.exists(f):
                    zf.write(f, Path(f).name)

        zip_bytes = os.path.getsize(zip_path)
        jlog.info(f"=== ✅ CONCLUÍDO em {elapsed}s | ZIP: {zip_bytes//1024}KB ===")
        return {
            "status":    "done",
            "job_id":    job_id,
            "zip_path":  str(zip_path),
            "zip_bytes": zip_bytes,
            "arquivos":  [Path(f).name for f in arquivos],
            "duracao_s": elapsed,
            "progresso": jlog.get_lines(),
        }

    except Exception as exc:
        elapsed = round(time.monotonic() - t0, 1)
        jlog.info(f"=== ✖ ERRO após {elapsed}s: {exc} ===")
        return {
            "status":    "error",
            "job_id":    job_id,
            "erro":      str(exc),
            "duracao_s": elapsed,
            "progresso": jlog.get_lines(),
        }
