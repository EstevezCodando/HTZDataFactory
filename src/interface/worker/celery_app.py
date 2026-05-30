"""
Celery app — workers de processamento HTZ.
"""

import os
import importlib.util
import pathlib

# Fix PROJ e logging antes de qualquer import rasterio
_spec = importlib.util.find_spec("rasterio")
if _spec:
    _proj = pathlib.Path(_spec.origin).parent / "proj_data"
    if _proj.exists():
        os.environ["PROJ_DATA"] = str(_proj)
        os.environ["PROJ_LIB"]  = str(_proj)

from src.infraestrutura.logging.setup import setup_logging
setup_logging()

import logging
_log = logging.getLogger("htz.celery")

from celery import Celery
from celery.signals import worker_ready, task_prerun, task_postrun, task_failure

BROKER  = os.environ.get("CELERY_BROKER_URL",    "redis://redis:6379/0")
BACKEND = os.environ.get("CELERY_RESULT_BACKEND", "redis://redis:6379/0")

celery_app = Celery(
    "htz_factory",
    broker=BROKER,
    backend=BACKEND,
    include=["src.interface.worker.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    result_expires=86400,
    worker_max_tasks_per_child=10,
    worker_log_format="%(message)s",           # nosso formatter já cuida do formato
    worker_task_log_format="%(message)s",
)


# ── Sinais de ciclo de vida ────────────────────────────────────────────────────

@worker_ready.connect
def on_worker_ready(sender=None, **kwargs):
    _log.info("Celery worker pronto", extra={
        "broker": BROKER,
        "concurrency": sender.app.conf.worker_concurrency,
    })


@task_prerun.connect
def on_task_start(task_id=None, task=None, args=None, kwargs=None, **_):
    job_id = (args or [None])[0] if args else (kwargs or {}).get("job_id")
    _log.info("Task iniciada", extra={"task_id": task_id, "job_id": job_id})


@task_postrun.connect
def on_task_done(task_id=None, task=None, retval=None, state=None, **_):
    status = (retval or {}).get("status", state) if isinstance(retval, dict) else state
    _log.info("Task concluida", extra={"task_id": task_id, "status": status})


@task_failure.connect
def on_task_fail(task_id=None, exception=None, traceback=None, **_):
    _log.error("Task falhou", extra={
        "task_id": task_id,
        "exception": str(exception),
    }, exc_info=exception)
