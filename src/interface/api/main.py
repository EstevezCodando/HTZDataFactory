"""
HTZDataFactory — API FastAPI.
Jobs via Celery workers (prefork). Cache de bbox. Logging estruturado JSON.
"""

# Fix PROJ ANTES de qualquer import rasterio/pyproj
import os as _os, importlib.util as _ilu, pathlib as _pl
_spec = _ilu.find_spec("rasterio")
if _spec:
    _pd = _pl.Path(_spec.origin).parent / "proj_data"
    if _pd.exists():
        _os.environ.setdefault("PROJ_DATA", str(_pd))
        _os.environ.setdefault("PROJ_LIB",  str(_pd))

import asyncio
import hashlib
import logging
import os
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, BackgroundTasks, Request, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from .schemas import JobRequest, JobStatus, JobListItem
from src.infraestrutura.logging.setup import setup_logging, get_logger
from src.infraestrutura.persistencia.audit_jsonl import AuditJsonl

setup_logging()
_log = get_logger("htz.api")

# ── Config ────────────────────────────────────────────────────────────────────
OUTPUT_DIR          = os.environ.get("OUTPUT_DIR", "/saidas")
FABDEM_GEOJSON      = os.environ.get("FABDEM_GEOJSON", "")
MAPBIOMAS_TIF       = os.environ.get("MAPBIOMAS_TIF", "")
RECLASSIFICACAO_CSV = os.environ.get("RECLASSIFICACAO_CSV", "")
MAX_CONCURRENT      = int(os.environ.get("MAX_CONCURRENT_JOBS", "4"))
USE_CELERY          = bool(os.environ.get("CELERY_BROKER_URL"))

# API Key opcional para endpoints destrutivos (DELETE /cache)
_API_KEY = os.environ.get("HTZ_API_KEY", "")

# Tamanho máximo de upload: 500 MB
_MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(500 * 1024 * 1024)))

# CORS: apenas origens locais (não expõe para internet)
_ALLOWED_ORIGINS = [
    o.strip() for o in
    os.environ.get("CORS_ORIGINS", "http://localhost:8080,http://localhost:8000").split(",")
    if o.strip()
]

_celery_task = None
if USE_CELERY:
    try:
        from src.interface.worker.tasks import gerar_pacote_task
        _celery_task = gerar_pacote_task
    except Exception as e:
        _log.warning("Celery nao disponivel, usando asyncio", extra={"reason": str(e)})
        USE_CELERY = False

# ── Estado em memória — protegido por lock ────────────────────────────────────
_lock:       threading.Lock         = threading.Lock()
_jobs:       dict[str, dict]        = {}
_bbox_cache: OrderedDict[str, str]  = OrderedDict()   # LRU com tamanho máximo
_BBOX_CACHE_MAX = 500
_semaforo:   asyncio.Semaphore      = None


def _bbox_hash(bbox: list, layers: list) -> str:
    key = f"{[round(v, 2) for v in bbox]}|{sorted(layers)}"
    return hashlib.sha256(key.encode()).hexdigest()[:24]


def _bbox_cache_set(key: str, job_id: str):
    """Insere no cache LRU com limite de tamanho."""
    with _lock:
        if key in _bbox_cache:
            _bbox_cache.move_to_end(key)
        _bbox_cache[key] = job_id
        while len(_bbox_cache) > _BBOX_CACHE_MAX:
            _bbox_cache.popitem(last=False)


def _jobs_set(job_id: str, data: dict):
    with _lock:
        _jobs[job_id] = data


def _jobs_update(job_id: str, data: dict):
    with _lock:
        if job_id in _jobs:
            _jobs[job_id].update(data)


def _jobs_get(job_id: str) -> Optional[dict]:
    with _lock:
        return _jobs.get(job_id)


def _jobs_snapshot() -> list[tuple[str, dict]]:
    with _lock:
        return list(_jobs.items())


# ── Validação TIFF (magic bytes) ──────────────────────────────────────────────
_TIFF_MAGIC = {
    b"\x49\x49\x2A\x00",  # TIFF little-endian
    b"\x4D\x4D\x00\x2A",  # TIFF big-endian
    b"\x49\x49\x2B\x00",  # BigTIFF little-endian
    b"\x4D\x4D\x00\x2B",  # BigTIFF big-endian
}

def _validar_tiff_magic(data: bytes) -> bool:
    return data[:4] in _TIFF_MAGIC


# ── Validação de API Key ───────────────────────────────────────────────────────
def _check_api_key(request: Request):
    """Verifica API Key em header X-API-Key ou query ?api_key=. Ignora se não configurada."""
    if not _API_KEY:
        return  # sem chave configurada, endpoint não protegido por chave
    provided = request.headers.get("X-API-Key") or request.query_params.get("api_key", "")
    # Comparação segura contra timing attack
    import hmac
    if not hmac.compare_digest(provided, _API_KEY):
        raise HTTPException(403, "Acesso negado")


# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="HTZDataFactory",
    description="Gerador automático de dados ATDI .SOL .GEO .IMG .PAL",
    version="1.3.0",
    docs_url="/docs",
    redoc_url="/redoc",
    # Oculta detalhes de erro internos no schema OpenAPI
    openapi_url="/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Content-Type", "X-API-Key"],
    allow_credentials=False,
)

_frontend = Path(__file__).parent.parent.parent.parent / "frontend"
_index_html = _frontend / "index.html"


@app.get("/app", include_in_schema=False)
@app.get("/app/", include_in_schema=False)
def serve_frontend():
    if not _index_html.exists():
        raise HTTPException(404, "Frontend não encontrado")
    return FileResponse(
        str(_index_html),
        media_type="text/html",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
        },
    )


@app.on_event("startup")
async def startup():
    global _semaforo
    _semaforo = asyncio.Semaphore(MAX_CONCURRENT)
    _log.info("API iniciada", extra={
        "backend":        "celery+redis" if USE_CELERY else "asyncio",
        "max_concurrent": MAX_CONCURRENT,
        "mapbiomas_ok":   Path(MAPBIOMAS_TIF).exists() if MAPBIOMAS_TIF else False,
        "fabdem_ok":      Path(FABDEM_GEOJSON).exists() if FABDEM_GEOJSON else False,
        "cors_origins":   _ALLOWED_ORIGINS,
    })


# ── Middleware de request logging ─────────────────────────────────────────────
@app.middleware("http")
async def log_requests(request: Request, call_next):
    t0  = time.monotonic()
    resp = await call_next(request)
    ms  = round((time.monotonic() - t0) * 1000)
    if request.url.path not in ("/health", "/status", "/nginx-health"):
        # IP confiável vem do nginx via X-Real-IP (interno Docker)
        _log.info("HTTP", extra={
            "method": request.method,
            "path":   request.url.path,
            "status": resp.status_code,
            "ms":     ms,
            "ip":     request.headers.get("x-real-ip", "internal"),
        })
    return resp


# ── Background worker asyncio (fallback sem Celery) ──────────────────────────
def _run_job_sync(job_id: str, bbox: tuple, layers: list[str],
                  img_fonte: str = "sentinel2", osm_zoom: int = 15):
    from src.aplicacao.casos_uso.gerar_pacote_htz import executar
    _jobs_update(job_id, {"status": "running"})
    try:
        resultado = executar(
            job_id=job_id, bbox=bbox, layers=layers,
            output_dir=OUTPUT_DIR,
            fabdem_geojson=FABDEM_GEOJSON,
            fabdem_dir=os.path.dirname(FABDEM_GEOJSON),
            mapbiomas_tif=MAPBIOMAS_TIF,
            reclassificacao_csv=RECLASSIFICACAO_CSV,
            img_fonte=img_fonte,
            osm_zoom=osm_zoom,
            progress_cb=None,
        )
        _jobs_update(job_id, resultado)
    except Exception as exc:
        _log.error("Job falhou", extra={"job_id": job_id, "erro": str(exc)})
        _jobs_update(job_id, {"status": "error", "erro": "Erro interno no processamento"})


async def _run_job_async(job_id: str, bbox: tuple, layers: list[str],
                         img_fonte: str = "sentinel2", osm_zoom: int = 15):
    async with _semaforo:
        loop = asyncio.get_event_loop()
        try:
            await asyncio.wait_for(
                loop.run_in_executor(None, _run_job_sync, job_id, bbox, layers,
                                     img_fonte, osm_zoom),
                timeout=3600,
            )
        except asyncio.TimeoutError:
            _log.error("Job timeout", extra={"job_id": job_id})
            _jobs_update(job_id, {"status": "error", "erro": "Timeout: job excedeu 1 hora"})


# ── Rotas ─────────────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
def root():
    return {"servico": "HTZDataFactory", "versao": "1.3.0", "docs": "/docs"}


@app.get("/status")
def status_dados():
    """
    Retorna quais fontes de dados estão disponíveis e ativas no sistema.
    Paths internos NÃO são expostos na resposta.
    """
    import json
    import glob as _glob

    fabdem_ok    = Path(FABDEM_GEOJSON).exists() if FABDEM_GEOJSON else False
    csv_ok       = Path(RECLASSIFICACAO_CSV).exists() if RECLASSIFICACAO_CSV else False
    if MAPBIOMAS_TIF:
        if Path(MAPBIOMAS_TIF).exists():
            mapbiomas_ok = True
        else:
            pasta = str(Path(MAPBIOMAS_TIF).parent)
            mapbiomas_ok = len(_glob.glob(f"{pasta}/*.tif")) > 0
    else:
        mapbiomas_ok = False

    fabdem_tiles_local = 0
    if fabdem_ok:
        fabdem_dir = os.path.dirname(FABDEM_GEOJSON)
        fabdem_tiles_local = sum(
            1 for root, dirs, files in os.walk(fabdem_dir)
            for f in files if f.endswith("_FABDEM_V1-2.tif")
        )

    mapbiomas_gb = 0.0
    if mapbiomas_ok:
        pasta = str(Path(MAPBIOMAS_TIF).parent)
        tifs_mb = [Path(t).stat().st_size for t in _glob.glob(f"{pasta}/*.tif")]
        mapbiomas_gb = round(sum(tifs_mb) / 1e9, 2) if tifs_mb else 0.0

    csv_classes = 0
    if csv_ok:
        import csv
        with open(RECLASSIFICACAO_CSV) as f:
            csv_classes = sum(1 for _ in csv.DictReader(f))

    with _lock:
        jobs_ativos = sum(1 for j in _jobs.values() if j.get("status") == "running")
        jobs_fila   = sum(1 for j in _jobs.values() if j.get("status") == "pending")

    return {
        "sistema": {
            "backend":        "celery+redis" if USE_CELERY else "asyncio",
            "max_concurrent": MAX_CONCURRENT,
            "jobs_ativos":    jobs_ativos,
            "jobs_fila":      jobs_fila,
        },
        "dados": {
            "mapbiomas": {
                "ativo":      mapbiomas_ok,
                "descricao":  "MapBiomas Collection 10 — Uso e Cobertura da Terra 2023",
                "resolucao":  "10m original → 30m UTM no .sol",
                "tamanho_gb": mapbiomas_gb if mapbiomas_ok else None,
                "gera":       [".sol", ".pal"],
                "licenca":    "CC-BY-4.0",
            },
            "fabdem": {
                "ativo":        fabdem_ok,
                "descricao":    "FABDEM v1.2 — Modelo Digital de Terreno (bare earth)",
                "resolucao":    "30m UTM no .geo",
                "tiles_locais": fabdem_tiles_local,
                "tiles_total":  810,
                "gera":         [".geo"],
                "licenca":      "CC-BY-4.0 (University of Bristol)",
                "nota":         "Tiles baixados por demanda na primeira requisição de cada área",
            },
            "sentinel2": {
                "ativo":     True,
                "descricao": "Sentinel-2 L2A — Imagem óptica 10m (ESA Copernicus)",
                "resolucao": "10m original → 15m UTM no .img",
                "fonte":     "Microsoft Planetary Computer (STAC)",
                "gera":      [".img"],
                "licenca":   "Open Access (ESA Copernicus)",
                "nota":      "Seleciona automaticamente a cena com menos nuvens (<20%) dos últimos 3 anos",
            },
            "reclassificacao": {
                "ativo":   csv_ok,
                "arquivo": "mapbiomas_atdi.csv",
                "classes": csv_classes,
                "nota":    "Editável sem reiniciar o sistema",
            },
        },
        "camadas_disponiveis": {
            "geo": fabdem_ok,
            "sol": mapbiomas_ok and csv_ok,
            "img": True,
        },
    }


@app.post("/jobs", response_model=JobStatus, status_code=202)
async def criar_job(request: JobRequest, background_tasks: BackgroundTasks):
    """Cria um job de geração de pacote HTZ."""
    bbox      = list(request.bbox)
    layers    = request.layers
    img_fonte = request.img_fonte
    osm_zoom  = request.osm_zoom

    status = status_dados()
    for layer in layers:
        if not status["camadas_disponiveis"].get(layer, False):
            raise HTTPException(
                422,
                f"Camada '{layer}' não disponível — dado de origem ausente. "
                f"Consulte GET /status."
            )

    cache_key = _bbox_hash(bbox, layers)
    with _lock:
        cached_id = _bbox_cache.get(cache_key)
        cached    = _jobs.get(cached_id, {}) if cached_id else {}

    if cached_id and cached.get("status") == "done":
        _log.info("Cache hit", extra={"cache_key": cache_key, "job_id": cached_id})
        return JobStatus(
            job_id=cached_id, status="done",
            bbox=cached.get("bbox"), layers=cached.get("layers"),
            zip_bytes=cached.get("zip_bytes"), arquivos=cached.get("arquivos"),
            cache_hit=True,
        )

    job_id = uuid.uuid4().hex[:16]
    _jobs_set(job_id, {"status": "pending", "job_id": job_id, "bbox": bbox,
                       "layers": layers, "img_fonte": img_fonte, "osm_zoom": osm_zoom})
    _bbox_cache_set(cache_key, job_id)
    _log.info("Job criado", extra={"job_id": job_id, "layers": layers,
                                   "img_fonte": img_fonte, "osm_zoom": osm_zoom})

    if USE_CELERY and _celery_task:
        _celery_task.apply_async(args=[job_id, bbox, layers, img_fonte, osm_zoom], task_id=job_id)
    else:
        background_tasks.add_task(_run_job_async, job_id, tuple(bbox), layers, img_fonte, osm_zoom)

    return JobStatus(job_id=job_id, status="pending", bbox=bbox, layers=layers)


@app.get("/jobs/{job_id}", response_model=JobStatus)
def status_job(job_id: str):
    # Valida job_id: apenas hex
    if not job_id.isalnum():
        raise HTTPException(400, "job_id inválido")

    if USE_CELERY and _celery_task:
        result = _celery_task.AsyncResult(job_id)
        state  = result.state

        if state == "PROGRESS":
            meta = result.info or {}
            _jobs_update(job_id, {"status": "running"})
            return JobStatus(job_id=job_id, status="running",
                             progresso=(meta.get("progresso") or [])[-50:])

        if state == "SUCCESS":
            res = result.result or {}
            _jobs_update(job_id, res)
            return JobStatus(
                job_id=job_id, status=res.get("status", "done"),
                zip_bytes=res.get("zip_bytes"), arquivos=res.get("arquivos"),
                duracao_s=res.get("duracao_s"),
                progresso=(res.get("progresso") or [])[-50:],
            )

        if state == "FAILURE":
            return JobStatus(job_id=job_id, status="error", erro="Erro no processamento")

    job = _jobs_get(job_id)
    if not job:
        raise HTTPException(404, "Job não encontrado")

    return JobStatus(
        job_id=job_id,
        status=job.get("status", "pending"),
        bbox=job.get("bbox"), layers=job.get("layers"),
        zip_bytes=job.get("zip_bytes"), arquivos=job.get("arquivos"),
        erro=job.get("erro"), duracao_s=job.get("duracao_s"),
        progresso=(job.get("progresso") or [])[-50:],
    )


@app.get("/jobs/{job_id}/download")
def download_job(job_id: str):
    if not job_id.isalnum():
        raise HTTPException(400, "job_id inválido")

    if USE_CELERY and _celery_task and not _jobs_get(job_id):
        result = _celery_task.AsyncResult(job_id)
        if result.state == "SUCCESS":
            _jobs_set(job_id, result.result or {})

    job = _jobs_get(job_id)
    if not job:
        raise HTTPException(404, "Job não encontrado")
    if job.get("status") != "done":
        raise HTTPException(409, f"Job não concluído (status={job.get('status')})")

    zip_path = job.get("zip_path")
    if not zip_path:
        candidate = Path(OUTPUT_DIR) / job_id / f"htz_pacote_{job_id}.zip"
        if candidate.exists():
            zip_path = str(candidate)

    if not zip_path:
        raise HTTPException(500, "Arquivo não disponível")

    # Resolve e verifica que o path está dentro do OUTPUT_DIR
    resolved = Path(zip_path).resolve()
    base     = Path(OUTPUT_DIR).resolve()
    try:
        resolved.relative_to(base)
    except ValueError:
        _log.error("Path traversal detectado", extra={"zip_path": str(zip_path)})
        raise HTTPException(400, "Caminho inválido")

    if not resolved.exists() or not resolved.is_file():
        raise HTTPException(500, "Arquivo não disponível")

    _log.info("Download iniciado", extra={"job_id": job_id,
              "zip_bytes": resolved.stat().st_size})
    return FileResponse(
        str(resolved),
        media_type="application/zip",
        filename=resolved.name,
        headers={"Content-Disposition": f'attachment; filename="{resolved.name}"'},
    )


@app.get("/jobs/{job_id}/files")
def listar_arquivos_job(job_id: str):
    """Lista os arquivos gerados por um job, com tamanho e URL de download."""
    if not job_id.isalnum():
        raise HTTPException(400, "job_id inválido")

    if USE_CELERY and _celery_task and not _jobs_get(job_id):
        result = _celery_task.AsyncResult(job_id)
        if result.state == "SUCCESS":
            _jobs_set(job_id, result.result or {})

    job = _jobs_get(job_id)
    if not job:
        raise HTTPException(404, "Job não encontrado")
    if job.get("status") != "done":
        raise HTTPException(409, "Job não concluído")

    job_dir = (Path(OUTPUT_DIR) / job_id).resolve()
    base    = Path(OUTPUT_DIR).resolve()
    try:
        job_dir.relative_to(base)
    except ValueError:
        raise HTTPException(400, "Caminho inválido")

    if not job_dir.exists():
        raise HTTPException(404, "Diretório do job não encontrado")

    EXTS_PRODUTO = {".geo", ".sol", ".pal", ".img"}
    arquivos = []
    for f in sorted(job_dir.iterdir()):
        if f.suffix.lower() in EXTS_PRODUTO and f.is_file() and not f.is_symlink():
            arquivos.append({
                "nome":     f.name,
                "extensao": f.suffix.lower(),
                "bytes":    f.stat().st_size,
                "url":      f"/jobs/{job_id}/download/{f.name}",
            })
    zip_path = job.get("zip_path") or str(job_dir / f"htz_pacote_{job_id}.zip")
    zp = Path(zip_path).resolve()
    if zp.exists() and not zp.is_symlink():
        arquivos.append({
            "nome":     zp.name,
            "extensao": ".zip",
            "bytes":    zp.stat().st_size,
            "url":      f"/jobs/{job_id}/download",
        })
    return {"job_id": job_id, "arquivos": arquivos}


@app.get("/jobs/{job_id}/download/{filename}")
def download_arquivo(job_id: str, filename: str):
    """Download de um arquivo individual gerado pelo job."""
    if not job_id.isalnum():
        raise HTTPException(400, "job_id inválido")

    if USE_CELERY and _celery_task and not _jobs_get(job_id):
        result = _celery_task.AsyncResult(job_id)
        if result.state == "SUCCESS":
            _jobs_set(job_id, result.result or {})

    job = _jobs_get(job_id)
    if not job:
        raise HTTPException(404, "Job não encontrado")
    if job.get("status") != "done":
        raise HTTPException(409, "Job não concluído")

    # Rejeita qualquer separador de path no nome do arquivo
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(400, "Nome de arquivo inválido")

    # Resolve e verifica boundary
    file_path = (Path(OUTPUT_DIR) / job_id / filename).resolve()
    base      = (Path(OUTPUT_DIR) / job_id).resolve()
    try:
        file_path.relative_to(base)
    except ValueError:
        _log.warning("Path traversal tentado", extra={"job_id": job_id, "filename": filename})
        raise HTTPException(400, "Nome de arquivo inválido")

    if not file_path.exists() or not file_path.is_file() or file_path.is_symlink():
        raise HTTPException(404, f"Arquivo não encontrado")

    # Whitelist de extensões permitidas
    ext = file_path.suffix.lower()
    EXTS_PERMITIDAS = {".geo", ".sol", ".pal", ".img", ".zip", ".json", ".jsonl"}
    if ext not in EXTS_PERMITIDAS:
        raise HTTPException(400, "Tipo de arquivo não permitido")

    media_types = {
        ".geo":   "application/octet-stream",
        ".sol":   "application/octet-stream",
        ".pal":   "application/octet-stream",
        ".img":   "application/octet-stream",
        ".zip":   "application/zip",
        ".json":  "application/json",
        ".jsonl": "application/json",
    }
    return FileResponse(
        str(file_path),
        media_type=media_types.get(ext, "application/octet-stream"),
        filename=filename,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/jobs/{job_id}/audit")
def audit_job(job_id: str):
    if not job_id.isalnum():
        raise HTTPException(400, "job_id inválido")
    audit   = AuditJsonl(OUTPUT_DIR)
    eventos = audit.eventos_do_job(job_id)
    return {"job_id": job_id, "eventos": [e.to_dict() for e in eventos]}


@app.get("/jobs", response_model=list[JobListItem])
def listar_jobs():
    return [
        JobListItem(job_id=jid, status=j.get("status", "?"),
                    zip_bytes=j.get("zip_bytes"), duracao_s=j.get("duracao_s"))
        for jid, j in _jobs_snapshot()
    ]


@app.get("/health")
def health():
    return {
        "status":  "ok",
        "backend": "celery+redis" if USE_CELERY else "asyncio",
        "max_concurrent_jobs": MAX_CONCURRENT,
        # Apenas booleanos — sem paths internos
        "fabdem_ok":      Path(FABDEM_GEOJSON).exists() if FABDEM_GEOJSON else False,
        "mapbiomas_ok":   Path(MAPBIOMAS_TIF).exists() if MAPBIOMAS_TIF else False,
        "csv_ok":         Path(RECLASSIFICACAO_CSV).exists() if RECLASSIFICACAO_CSV else False,
    }


@app.get("/tiles/fabdem")
def tiles_fabdem():
    """
    GeoJSON com todas as células FABDEM indicando disponibilidade local.
    Considera disponível tanto o TIF já extraído quanto o ZIP do bloco presente na pasta.
    """
    import json as _json

    if not FABDEM_GEOJSON or not Path(FABDEM_GEOJSON).exists():
        raise HTTPException(404, "Catálogo FABDEM não encontrado")

    fabdem_dir = os.path.dirname(FABDEM_GEOJSON)

    # ── Indexa TIFs já extraídos (busca recursiva) ────────────────────────────
    local_tifs: set[str] = set()
    for root, dirs, files in os.walk(fabdem_dir):
        for f in files:
            if f.endswith("_FABDEM_V1-2.tif"):
                local_tifs.add(f.replace("_FABDEM_V1-2.tif", ""))

    # ── Indexa ZIPs de blocos presentes na pasta raiz ─────────────────────────
    # Ex: "S20W060-S10W050_FABDEM_V1-2.zip" ou pasta "S20W060-S10W050_FABDEM_V1-2"
    # O nome do bloco é o prefixo antes de "_FABDEM_V1-2"
    local_blocos: set[str] = set()
    for entry in Path(fabdem_dir).iterdir():
        nome = entry.name
        if "_FABDEM_V1-2" in nome:
            bloco = nome.split("_FABDEM_V1-2")[0]   # ex: "S20W060-S10W050"
            local_blocos.add(bloco)

    with open(FABDEM_GEOJSON, encoding="utf-8") as f:
        fc = _json.load(f)

    disponiveis = 0
    for feat in fc.get("features", []):
        p      = feat.get("properties", {})
        codigo = p.get("codigo_fabdem", "")
        bloco  = p.get("bloco_zip_10x10", "")

        tif_ok   = codigo in local_tifs
        bloco_ok = bloco in local_blocos

        p["downloaded"]     = tif_ok or bloco_ok
        p["tif_extraido"]   = tif_ok
        p["bloco_presente"] = bloco_ok
        if p["downloaded"]:
            disponiveis += 1

    return {
        "type":       "FeatureCollection",
        "total":      len(fc.get("features", [])),
        "downloaded": disponiveis,
        "features":   fc.get("features", []),
    }


@app.get("/tiles/mapbiomas")
def tiles_mapbiomas():
    """Retorna o bbox do arquivo MapBiomas como GeoJSON."""
    import glob as _glob

    tif_path = None
    if MAPBIOMAS_TIF and Path(MAPBIOMAS_TIF).exists():
        tif_path = MAPBIOMAS_TIF
    elif MAPBIOMAS_TIF:
        pasta = str(Path(MAPBIOMAS_TIF).parent)
        tifs  = _glob.glob(f"{pasta}/*.tif")
        if tifs:
            tif_path = tifs[0]

    if not tif_path:
        raise HTTPException(404, "MapBiomas TIF não encontrado")

    try:
        import rasterio
        from rasterio.warp import transform_bounds
        from rasterio.crs import CRS as _CRS
        from rasterio.transform import from_origin as _from_origin
        wgs84 = _CRS.from_epsg(4326)

        def _read_tfw(tif):
            tfw = Path(tif).with_suffix(".tfw")
            if not tfw.exists():
                return None, None
            vals = [float(l.strip()) for l in tfw.read_text().splitlines() if l.strip()]
            if len(vals) < 6:
                return None, None
            px_x, _, _, px_y, lon_ul, lat_ul = vals
            return _from_origin(lon_ul, lat_ul, abs(px_x), abs(px_y)), wgs84

        with rasterio.open(tif_path) as src:
            src_crs = src.crs
            if src_crs is None:
                tfw_transform, src_crs = _read_tfw(tif_path)
                if tfw_transform is None:
                    raise HTTPException(500, "TIF sem CRS e sem arquivo .tfw")
                from rasterio.transform import array_bounds as _ab
                b = _ab(src.height, src.width, tfw_transform)
                west, south, east, north = b
            elif src_crs == wgs84:
                b = src.bounds
                west, south, east, north = b.left, b.bottom, b.right, b.top
            else:
                bounds = transform_bounds(src_crs, wgs84, *src.bounds)
                west, south, east, north = bounds

        return {
            "type": "Feature",
            "properties": {"nome": "MapBiomas Collection 10", "resolucao": "10m", "ano": 2023},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[
                    [west, south], [east, south],
                    [east, north], [west, north],
                    [west, south],
                ]],
            },
        }
    except HTTPException:
        raise
    except Exception:
        _log.exception("Erro ao ler bounds MapBiomas")
        raise HTTPException(500, "Erro ao ler metadados do arquivo")


@app.post("/convert", response_model=JobStatus, status_code=202)
async def converter_arquivo(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(..., description="GeoTIFF para converter"),
    tipo: str = Form(..., description="Tipo: dem | clutter | img"),
    aplicar_onion: bool = Form(True),
    formato_clutter: str = Form("mapbiomas"),
):
    """Converte GeoTIFF enviado pelo usuário para formato ATDI binário."""
    TIPOS_VALIDOS = {"dem", "clutter", "img"}
    if tipo not in TIPOS_VALIDOS:
        raise HTTPException(422, f"tipo inválido. Use: dem, clutter ou img")

    FORMATOS_VALIDOS = {"mapbiomas", "atdi"}
    if formato_clutter not in FORMATOS_VALIDOS:
        raise HTTPException(422, "formato_clutter inválido. Use: mapbiomas ou atdi")

    # Valida extensão pelo nome declarado
    nome = (file.filename or "upload.tif").strip()
    if not nome.lower().endswith((".tif", ".tiff")):
        raise HTTPException(422, "Apenas arquivos GeoTIFF (.tif / .tiff) são aceitos")

    # Sanitiza o nome para não ter path separators
    nome = Path(nome).name
    if not nome:
        nome = "upload.tif"

    # Lê até MAX+1 bytes para verificar tamanho
    conteudo = await file.read(_MAX_UPLOAD_BYTES + 1)
    if len(conteudo) > _MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"Arquivo excede o limite de {_MAX_UPLOAD_BYTES // (1024*1024)} MB")

    # Valida magic bytes TIFF
    if not _validar_tiff_magic(conteudo):
        raise HTTPException(422, "Arquivo não é um GeoTIFF válido")

    job_id  = uuid.uuid4().hex[:16]
    job_dir = Path(OUTPUT_DIR) / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    tif_path = str(job_dir / nome)
    try:
        with open(tif_path, "wb") as f:
            f.write(conteudo)
    except Exception:
        # Limpa em caso de erro
        import shutil
        shutil.rmtree(str(job_dir), ignore_errors=True)
        _log.exception("Erro ao salvar upload")
        raise HTTPException(500, "Erro ao processar arquivo")

    _jobs_set(job_id, {"status": "pending", "job_id": job_id, "tipo": tipo, "arquivo": nome})
    _log.info("Upload recebido", extra={
        "job_id": job_id, "tipo": tipo,
        "arquivo": nome, "bytes": len(conteudo),
    })

    async def _run():
        async with _semaforo:
            from src.aplicacao.casos_uso.converter_arquivo import executar
            loop = asyncio.get_event_loop()
            _jobs_update(job_id, {"status": "running"})
            try:
                resultado = await asyncio.wait_for(
                    loop.run_in_executor(
                        None, executar,
                        job_id, tif_path, tipo,
                        OUTPUT_DIR, RECLASSIFICACAO_CSV, aplicar_onion,
                        formato_clutter,
                    ),
                    timeout=3600,
                )
                _jobs_update(job_id, resultado)
            except asyncio.TimeoutError:
                _jobs_update(job_id, {"status": "error", "erro": "Timeout no processamento"})
            except Exception:
                _log.exception("Erro no converter_arquivo", extra={"job_id": job_id})
                _jobs_update(job_id, {"status": "error", "erro": "Erro interno no processamento"})

    background_tasks.add_task(_run)
    return JobStatus(job_id=job_id, status="pending")


@app.delete("/cache")
def limpar_cache(request: Request):
    """Remove cache de bbox. Requer X-API-Key se HTZ_API_KEY estiver configurado."""
    _check_api_key(request)
    with _lock:
        n = len(_bbox_cache)
        _bbox_cache.clear()
    _log.info("Cache limpo", extra={"entries": n})
    return {"cache_entries_removidas": n}
