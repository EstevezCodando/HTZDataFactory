"""
Celery tasks — execucao assíncrona dos jobs HTZ.
"""

import os
from celery import Task
from .celery_app import celery_app
from src.aplicacao.casos_uso.gerar_pacote_htz import executar

OUTPUT_DIR          = os.environ.get("OUTPUT_DIR", "/saidas")
FABDEM_GEOJSON      = os.environ.get("FABDEM_GEOJSON", "")
MAPBIOMAS_TIF       = os.environ.get("MAPBIOMAS_TIF", "")
RECLASSIFICACAO_CSV = os.environ.get("RECLASSIFICACAO_CSV", "")
FABDEM_DIR          = os.path.dirname(FABDEM_GEOJSON)


class JobTask(Task):
    """Task base com log centralizado no Redis (via Celery state)."""

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        self.update_state(state="FAILURE", meta={"erro": str(exc)})


@celery_app.task(
    bind=True,
    base=JobTask,
    name="htz.gerar_pacote",
    queue="htz_jobs",
    time_limit=900,      # 15 min hard limit
    soft_time_limit=780, # 13 min soft limit (permite cleanup)
    max_retries=0,
)
def gerar_pacote_task(self, job_id: str, bbox: list, layers: list,
                      img_fonte: str = "sentinel2", osm_zoom: int = 15,
                      img_preview_id: str = None) -> dict:
    """
    Executa o pipeline HTZ em worker Celery dedicado.
    Progresso enviado via update_state para consulta via GET /jobs/{id}.
    """
    log_lines: list[str] = []

    def progress_cb(msg: str):
        log_lines.append(msg)
        # Atualiza estado Celery com os ultimos 100 logs
        self.update_state(
            state="PROGRESS",
            meta={
                "job_id": job_id,
                "status": "running",
                "progresso": log_lines[-100:],
            },
        )

    resultado = executar(
        job_id=job_id,
        bbox=tuple(bbox),
        layers=layers,
        output_dir=OUTPUT_DIR,
        fabdem_geojson=FABDEM_GEOJSON,
        fabdem_dir=FABDEM_DIR,
        mapbiomas_tif=MAPBIOMAS_TIF,
        reclassificacao_csv=RECLASSIFICACAO_CSV,
        img_fonte=img_fonte,
        osm_zoom=osm_zoom,
        img_preview_id=img_preview_id,
        progress_cb=progress_cb,
    )

    resultado["progresso"] = log_lines
    return resultado
