"""
Caso de Uso: Gerar Pacote HTZ.
Orquestra os 5 domínios para produzir um ZIP com todos os arquivos ATDI.
"""

import hashlib
import json
import logging
import os
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from src.dominio.area_interesse.entidades import AreaInteresse
from src.dominio.auditoria.entidades import Evento
from src.infraestrutura.logging.setup import JobLogger
from src.infraestrutura.persistencia.audit_jsonl import AuditJsonl
from src.infraestrutura.adaptadores import fabdem_rasterio, mapbiomas_windowed, sentinel_stac, osm_tiles

_log = logging.getLogger("htz.caso_uso.gerar_pacote")


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:12]


def executar(
    job_id: str,
    bbox: tuple,
    layers: list[str],
    output_dir: str,
    fabdem_geojson: str,
    fabdem_dir: str,
    mapbiomas_tif: str,
    reclassificacao_csv: str,
    img_fonte: str = "sentinel2",   # "sentinel2" | "osm"
    osm_zoom: int  = 15,
    progress_cb=None,
) -> dict:
    t0 = time.monotonic()
    area = AreaInteresse(bbox=bbox, job_id=job_id, layers=layers)
    job_dir = Path(output_dir) / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    audit  = AuditJsonl(output_dir)
    jlog   = JobLogger(job_id)

    # Compatibilidade: se vier progress_cb externo, encadeia com o JobLogger
    def _cb(msg: str):
        jlog.info(msg)
        if progress_cb:
            progress_cb(msg)

    def log_evento(etapa: str, **dados):
        ev = Evento(job_id=job_id, etapa=etapa, dados=dados)
        audit.registrar(ev)

    _log.info("Job iniciado", extra={"job_id": job_id, "bbox": list(bbox), "layers": layers})
    log_evento("inicio", bbox=list(bbox), layers=layers)

    arquivos_gerados = []
    grade        = None
    meta_sentinel = {}
    meta_sol     = {}

    try:
        # ── 1. FABDEM → .geo ──────────────────────────────────────────────
        if "geo" in layers:
            _cb("=== [1/3] Gerando terreno .geo (FABDEM) ===")
            geo_path, grade = fabdem_rasterio.process(
                bbox=bbox, output_dir=str(job_dir),
                geojson_path=fabdem_geojson, fabdem_dir=fabdem_dir,
                progress_cb=_cb,
            )
            arquivos_gerados.append(geo_path)
            sz = os.path.getsize(geo_path)
            log_evento("fabdem", arquivo=Path(geo_path).name, bytes=sz,
                       largura=grade["width"], altura=grade["height"])
            _log.info("FABDEM gerado", extra={"job_id": job_id, "bytes": sz,
                       "width": grade["width"], "height": grade["height"]})

        # ── 2. MapBiomas → .sol ───────────────────────────────────────────
        # Nota: .sol não usa .pal — paleta de cores é exclusiva do .img
        if "sol" in layers:
            _cb("=== [2/3] Gerando clutter .sol (MapBiomas) ===")
            sol_path, urban_stats = mapbiomas_windowed.process(
                bbox=bbox, output_dir=str(job_dir),
                mapbiomas_tif=mapbiomas_tif,
                reclassificacao_csv=reclassificacao_csv,
                progress_cb=_cb,
            )
            arquivos_gerados.append(sol_path)
            meta_sol = urban_stats
            sz = os.path.getsize(sol_path)
            log_evento("mapbiomas", arquivo=Path(sol_path).name, bytes=sz, **urban_stats)
            _log.info("MapBiomas gerado", extra={"job_id": job_id, "bytes": sz, **urban_stats})

        # ── 3. Imagem → .img + .pal (Sentinel-2 ou OSM) ─────────────────
        # .pal é exclusivo do .img — gerado sempre que .img for solicitado
        if "img" in layers:
            if img_fonte == "osm":
                _cb(f"=== [3/3] Gerando imagem .img (OpenStreetMap zoom {osm_zoom}) ===")
                img_path, img_pal_path = osm_tiles.process(
                    bbox=bbox, output_dir=str(job_dir),
                    zoom=osm_zoom, grade=grade, progress_cb=_cb,
                )
                arquivos_gerados += [img_path, img_pal_path]
                sz = os.path.getsize(img_path)
                log_evento("osm_img", arquivo=Path(img_path).name, bytes=sz, zoom=osm_zoom)
                _log.info("OSM IMG gerado", extra={"job_id": job_id, "bytes": sz, "zoom": osm_zoom})
                meta_sentinel = {"fonte": "osm", "zoom": osm_zoom}
            else:
                _cb("=== [3/3] Gerando imagem .img (Sentinel-2) ===")
                img_path, img_pal_path, meta_sentinel = sentinel_stac.process(
                    bbox=bbox, output_dir=str(job_dir),
                    grade=grade, progress_cb=_cb,
                )
                arquivos_gerados += [img_path, img_pal_path]
                sz = os.path.getsize(img_path)
                log_evento("sentinel2", arquivo=Path(img_path).name, bytes=sz, **meta_sentinel)
                _log.info("Sentinel-2 gerado", extra={"job_id": job_id, "bytes": sz, **meta_sentinel})

        # ── 4. Manifesto ──────────────────────────────────────────────────
        csv_hash = _sha256(reclassificacao_csv)
        elapsed  = round(time.monotonic() - t0, 1)
        manifesto = {
            "job_id": job_id,
            "gerado_em": datetime.now(timezone.utc).isoformat(),
            "duracao_s": elapsed,
            "bbox": list(bbox),
            "layers": layers,
            "fontes": {
                "dem":     "FABDEM v1.2 2022 (University of Bristol, CC-BY-4.0)",
                "clutter": "MapBiomas Collection 10 2023 (CC-BY-4.0)",
                "imagem":  (
                    f"OpenStreetMap zoom {osm_zoom} (ODbL)"
                    if img_fonte == "osm"
                    else "Sentinel-2 L2A (ESA Copernicus, Open Access)"
                ),
            },
            "versao_tabela_reclassificacao": f"mapbiomas_atdi.csv @ SHA256:{csv_hash}",
            "meta_sentinel": meta_sentinel,
            "meta_urbano":   meta_sol,
            "disclaimer": (
                "Dados gerados automaticamente para uso em simulacoes de RF. "
                "Os dados são gerados com base nas fontes acima, analise dos dados é necessaria antes de uso operacional."
            ),
        }
        manifesto_path = job_dir / "manifesto.json"
        manifesto_path.write_text(
            json.dumps(manifesto, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        arquivos_gerados.append(str(manifesto_path))

        # ── 5. ZIP ────────────────────────────────────────────────────────
        zip_path = job_dir / f"htz_pacote_{job_id}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for f_path in arquivos_gerados:
                if os.path.exists(f_path):
                    zf.write(f_path, Path(f_path).name)
            audit_path = job_dir / "audit.jsonl"
            if audit_path.exists():
                zf.write(str(audit_path), "audit.jsonl")

        zip_bytes = os.path.getsize(zip_path)
        log_evento("concluido", zip=zip_path.name, zip_bytes=zip_bytes,
                   n_arquivos=len(arquivos_gerados), duracao_s=elapsed)
        _log.info("Job concluido", extra={
            "job_id": job_id, "zip_bytes": zip_bytes,
            "n_arquivos": len(arquivos_gerados), "duracao_s": elapsed,
        })
        _cb(f"=== ✅ CONCLUIDO em {elapsed}s | ZIP: {zip_bytes//1024}KB ===")

        return {
            "status":   "done",
            "job_id":   job_id,
            "zip_path": str(zip_path),
            "zip_bytes": zip_bytes,
            "arquivos": [Path(p).name for p in arquivos_gerados],
            "duracao_s": elapsed,
            "progresso": jlog.get_lines(),
        }

    except Exception as exc:
        elapsed = round(time.monotonic() - t0, 1)
        log_evento("erro", msg=str(exc), duracao_s=elapsed)
        _log.error("Job falhou", extra={"job_id": job_id, "erro": str(exc),
                                         "duracao_s": elapsed}, exc_info=True)
        _cb(f"=== ✖ ERRO após {elapsed}s: {exc} ===")
        return {
            "status":    "error",
            "job_id":    job_id,
            "erro":      str(exc),
            "duracao_s": elapsed,
            "progresso": jlog.get_lines(),
        }
