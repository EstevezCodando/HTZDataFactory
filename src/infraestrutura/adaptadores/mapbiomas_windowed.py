"""
Infraestrutura: Adaptador MapBiomas.
Le o TIF (ou mosaico de TIFs) via janela GDAL sem carregar tudo na memoria.
Suporta um unico arquivo ou varios TIFs na mesma pasta (montagem via VRT em memoria).
Aplica reclassificacao via CSV externo e efeito cebola urbano.
Gera .sol + .pal ATDI.
"""

import math
import os
import tempfile
from pathlib import Path
import numpy as np
import rasterio
from rasterio.crs import CRS
from rasterio.warp import reproject, Resampling, calculate_default_transform
import rasterio.windows

from src.dominio.processamento.servicos import (
    carregar_tabela_reclassificacao,
    reclassifica_clutter,
    aplica_onion,
    stats_urbanos,
)
from src.infraestrutura.adaptadores.atdi_writer import write_sol, _utm_epsg

WGS84_CRS    = CRS.from_epsg(4326)
PIXEL_SIZE_M = 30.0   # metros


def _ler_tfw(tif_path: str):
    """
    Le o arquivo .tfw (world file) associado ao TIF.
    Retorna (transform_affine, crs_wgs84) ou None se nao existir.
    Formato TFW: 6 linhas — pixel_x, rot1, rot2, pixel_y (negativo), lon_ul, lat_ul
    """
    from rasterio.transform import from_origin
    tfw = Path(tif_path).with_suffix(".tfw")
    if not tfw.exists():
        return None, None
    vals = [float(l.strip()) for l in tfw.read_text().splitlines() if l.strip()]
    if len(vals) < 6:
        return None, None
    px_x, _, _, px_y, lon_ul, lat_ul = vals
    # from_origin(west, north, xsize, ysize) — px_y é negativo no TFW
    transform = from_origin(lon_ul, lat_ul, abs(px_x), abs(px_y))
    return transform, WGS84_CRS


def _abrir_mapbiomas(mapbiomas_tif: str):
    """
    Abre o MapBiomas como dataset rasterio.
    - Se o TIF nao tiver CRS/geotransform, aplica o .tfw correspondente.
    - Se `mapbiomas_tif` nao existir mas o diretório tiver varios TIFs,
      cria um VRT em disco temporário para ler o mosaico como arquivo único.
    Retorna (dataset, vrt_path_ou_None).
    """
    pasta = os.path.dirname(mapbiomas_tif)

    # Resolve lista de TIFs disponíveis
    if os.path.exists(mapbiomas_tif):
        tifs = [Path(mapbiomas_tif)]
    else:
        tifs = sorted(Path(pasta).glob("*.tif"))
        if not tifs:
            raise FileNotFoundError(
                f"MapBiomas TIF nao encontrado: {mapbiomas_tif}\n"
                f"Conteudo da pasta {pasta}: {list(Path(pasta).iterdir())}"
            )

    if len(tifs) == 1:
        ds = rasterio.open(str(tifs[0]))
        # Se sem georeferencia, aplica .tfw
        if ds.crs is None:
            transform, crs = _ler_tfw(str(tifs[0]))
            if transform:
                from rasterio.io import MemoryFile
                profile = ds.profile.copy()
                profile.update(crs=crs, transform=transform)
                data = ds.read()
                ds.close()
                mem = MemoryFile()
                with mem.open(**profile) as dst:
                    dst.write(data)
                return mem.open(), None  # retorna dataset da MemoryFile
        return ds, None

    # Múltiplos TIFs → VRT via GDAL, injetando TFW se necessário
    from osgeo import gdal, osr

    # Verifica se os TIFs têm georeferência; se não, adiciona via VRT manual
    first = rasterio.open(str(tifs[0]))
    sem_crs = first.crs is None
    first_transform, first_crs = _ler_tfw(str(tifs[0]))
    first.close()

    if sem_crs and first_transform:
        # Constrói VRT XML manual para montar os TIFs com georeferência do TFW
        vrt_xml_parts = []
        total_width = total_height = 0
        rasters = []
        for t in tifs:
            ds = rasterio.open(str(t))
            tf, crs = _ler_tfw(str(t))
            rasters.append((t, ds.width, ds.height, tf or ds.transform))
            total_width = max(total_width, ds.width)
            total_height += ds.height
            ds.close()

        # BuildVRT com InputFileList — usa GDAL que lê .tfw automaticamente
        vrt_tmp = tempfile.NamedTemporaryFile(suffix=".vrt", delete=False)
        vrt_tmp.close()
        gdal.BuildVRT(vrt_tmp.name, [str(t) for t in tifs])
        return rasterio.open(vrt_tmp.name), vrt_tmp.name
    else:
        vrt_tmp = tempfile.NamedTemporaryFile(suffix=".vrt", delete=False)
        vrt_tmp.close()
        gdal.BuildVRT(vrt_tmp.name, [str(t) for t in tifs])
        return rasterio.open(vrt_tmp.name), vrt_tmp.name


def process(bbox: tuple, output_dir: str,
            mapbiomas_tif: str, reclassificacao_csv: str,
            aplicar_onion: bool = True,
            progress_cb=None) -> tuple[str, str]:
    """
    Gera .sol + .pal a partir do MapBiomas para o bbox.
    Leitura via janela GDAL — nao carrega o TIF inteiro na memoria.

    Returns:
        (caminho_sol, caminho_pal)
    """
    west, south, east, north = bbox
    os.makedirs(output_dir, exist_ok=True)

    lon_center = (west + east) / 2
    lat_center = (south + north) / 2
    is_south   = lat_center < 0
    utm_epsg   = _utm_epsg(lon_center, is_south)
    utm_crs    = CRS.from_epsg(utm_epsg)

    # Carrega tabela de reclassificacao do CSV externo
    if progress_cb:
        progress_cb("[MapBiomas] Carregando tabela de reclassificacao...")
    tabela = carregar_tabela_reclassificacao(reclassificacao_csv)
    if progress_cb:
        progress_cb(f"[MapBiomas] {len(tabela)} mapeamentos carregados do CSV")

    # Leitura via janela GDAL (evita carregar o arquivo inteiro na memoria)
    margin = 0.1
    clip_bounds = (west - margin, south - margin, east + margin, north + margin)

    if progress_cb:
        progress_cb(f"[MapBiomas] Lendo janela do TIF ({west},{south} -> {east},{north})...")

    # ── Leitura da janela com tolerância a bbox fora da cobertura ────────────
    fora_da_cobertura = False
    src_ds, vrt_tmp = _abrir_mapbiomas(mapbiomas_tif)
    try:
        src_crs = src_ds.crs or WGS84_CRS
        src_bounds = src_ds.bounds

        # Verifica sobreposição geográfica antes de tentar a intersecção
        sem_sobreposicao = (
            clip_bounds[2] <= src_bounds.left   or   # east do bbox <= west do raster
            clip_bounds[0] >= src_bounds.right  or   # west do bbox >= east do raster
            clip_bounds[3] <= src_bounds.bottom or   # north do bbox <= south do raster
            clip_bounds[1] >= src_bounds.top         # south do bbox >= north do raster
        )

        if sem_sobreposicao:
            fora_da_cobertura = True
            if progress_cb:
                progress_cb(
                    f"[MapBiomas] AVISO: bbox fora da cobertura do arquivo local "
                    f"({src_bounds.left:.2f},{src_bounds.bottom:.2f} -> "
                    f"{src_bounds.right:.2f},{src_bounds.top:.2f}). "
                    f"SOL gerado com cobertura open (codigo 0)."
                )
        else:
            window = src_ds.window(*clip_bounds)
            raster_window = rasterio.windows.Window(0, 0, src_ds.width, src_ds.height)
            try:
                window = window.intersection(raster_window)
            except Exception:
                fora_da_cobertura = True
                if progress_cb:
                    progress_cb("[MapBiomas] AVISO: intersecao vazia — SOL gerado como open.")

        if not fora_da_cobertura:
            data = src_ds.read(1, window=window)
            wgs_transform = src_ds.window_transform(window)
    finally:
        src_ds.close()
        if vrt_tmp and os.path.exists(vrt_tmp):
            os.unlink(vrt_tmp)

    # ── Calcula dimensões UTM de destino ──────────────────────────────────────
    dst_transform, dst_width, dst_height = calculate_default_transform(
        src_crs=WGS84_CRS,
        dst_crs=utm_crs,
        width=max(1, int((east - west) / (PIXEL_SIZE_M / 111320))),
        height=max(1, int((north - south) / (PIXEL_SIZE_M / 111320))),
        left=west, bottom=south, right=east, top=north,
        resolution=PIXEL_SIZE_M,
    )

    if fora_da_cobertura:
        # Área fora da cobertura: .sol todo open (0)
        dst_arr = np.zeros((dst_height, dst_width), dtype=np.uint8)
    else:
        if progress_cb:
            progress_cb(f"[MapBiomas] Janela lida: {data.shape[1]}x{data.shape[0]}px")
        if progress_cb:
            progress_cb(f"[MapBiomas] Reprojetando para UTM EPSG:{utm_epsg} "
                        f"({dst_width}x{dst_height}px @ {PIXEL_SIZE_M:.0f}m)...")

        dst_arr = np.zeros((dst_height, dst_width), dtype=np.uint8)
        reproject(
            source=data,
            destination=dst_arr,
            src_transform=wgs_transform,
            src_crs=src_crs,
            dst_transform=dst_transform,
            dst_crs=utm_crs,
            resampling=Resampling.nearest,
            src_nodata=0,
            dst_nodata=0,
        )

    if progress_cb:
        progress_cb("[MapBiomas] Reclassificando codigos MapBiomas -> ATDI...")

    clutter = reclassifica_clutter(dst_arr, tabela)

    # Efeito cebola urbano (opcional)
    urban_antes = int(np.sum(clutter == 2))
    if aplicar_onion and urban_antes > 0:
        if progress_cb:
            progress_cb(f"[MapBiomas] Aplicando efeito cebola urbano "
                        f"({urban_antes}px urban 8m, aneis de 300m = 10px)...")
        clutter = aplica_onion(clutter, pixel_size_m=PIXEL_SIZE_M, ring_width_m=300.0)
        if progress_cb:
            st = stats_urbanos(clutter)
            progress_cb(f"[MapBiomas] Urban: 8m={st['urban_8m']}px  "
                        f"15m={st['urban_15m']}px  30m={st['urban_30m']}px  "
                        f"50m={st['urban_50m']}px")
    elif not aplicar_onion:
        if progress_cb:
            progress_cb(f"[MapBiomas] Efeito cebola desativado "
                        f"({urban_antes}px urban 8m mantidos como urban_8m)")
    else:
        if progress_cb:
            progress_cb("[MapBiomas] Nenhuma area urbana encontrada no bbox")

    xmin_utm = dst_transform.c
    ymax_utm = dst_transform.f
    ymin_utm = ymax_utm - dst_height * PIXEL_SIZE_M

    lat_i = int(abs(math.floor(lat_center)))
    lon_i = int(abs(math.floor(west)))
    ns = "S" if is_south else "N"
    ew = "W" if west < 0 else "E"
    base_name = f"{lat_i:02d}{ns}{lon_i:03d}{ew}"

    sol_path = os.path.join(output_dir, base_name + ".sol")

    if progress_cb:
        progress_cb(f"[MapBiomas] Escrevendo {base_name}.sol ...")

    write_sol(
        path=sol_path,
        data=clutter,
        xmin=xmin_utm,
        ymin=ymin_utm,
        pixel_size=PIXEL_SIZE_M,
        lon_center=lon_center,
        south=is_south,
    )

    if progress_cb:
        sz_sol = os.path.getsize(sol_path)
        unique = np.unique(clutter[clutter > 0])
        progress_cb(f"[MapBiomas] SOL gerado: {sz_sol/1024:.0f} KB, "
                    f"{len(unique)} classes ATDI")

    return sol_path, stats_urbanos(clutter)
