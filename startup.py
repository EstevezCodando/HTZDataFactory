"""
Ponto de entrada do HTZDataFactory.
Aplica fix PROJ e configura logging antes de qualquer outro import.
"""

import os
import importlib.util
import pathlib

# 1. Fix PROJ — antes do rasterio/pyproj
_spec = importlib.util.find_spec("rasterio")
if _spec:
    _proj = pathlib.Path(_spec.origin).parent / "proj_data"
    if _proj.exists():
        os.environ["PROJ_DATA"] = str(_proj)
        os.environ["PROJ_LIB"]  = str(_proj)

# 2. Logging estruturado — antes de qualquer import do projeto
from src.infraestrutura.logging.setup import setup_logging
_json_output = os.environ.get("LOG_FORMAT", "json").lower() != "human"
setup_logging(json_output=_json_output)

import logging
_log = logging.getLogger("htz.startup")
_log.info("HTZDataFactory iniciando", extra={
    "proj_data": os.environ.get("PROJ_DATA", "nao definido"),
    "log_format": "json" if _json_output else "human",
})

# 3. Importa app FastAPI
from src.interface.api.main import app  # noqa: F401, E402

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "startup:app",
        host="0.0.0.0",
        port=8000,
        reload=os.environ.get("DEV_RELOAD", "0") == "1",
        log_config=None,  # desabilita log config do uvicorn — usamos o nosso
        access_log=False, # nginx já loga os acessos
    )
