"""
Infraestrutura: Download de tiles de fontes externas → MBTiles (SQLite).

Suporta:
  - XYZ (Google Satellite, OpenStreetMap, custom)
  - Quadkey (Bing Satellite)

O arquivo MBTiles gerado segue o padrão v1.1 e pode ser servido diretamente
pela API via endpoint /img/preview/{id}/tile/{z}/{x}/{y}.

Referência de formato: https://github.com/mapbox/mbtiles-spec/blob/master/1.1/spec.md

AVISO: O uso de Google Satellite e Bing Satellite está sujeito aos
Termos de Serviço de cada provedor. Este módulo é fornecido apenas como
mecanismo técnico — a responsabilidade pelo uso é inteiramente do usuário.
"""

import io
import math
import sqlite3
import time
import urllib.request
import urllib.error
from pathlib import Path

# ── Fontes disponíveis ────────────────────────────────────────────────────────

SOURCES: dict[str, dict] = {
    "google_satellite": {
        "name": "Google Satellite",
        "url_template": "https://mt1.google.com/vt/lyrs=s&x={x}&y={y}&z={z}",
        "kind": "xyz",
        "zoom_min": 12,
        "zoom_max": 18,
        "requires_consent": True,
        "attribution": "© Google",
        "tile_format": "jpg",
        "delay_s": 0.05,
    },
    "bing_satellite": {
        "name": "Bing Satellite",
        "url_template": "https://ecn.t{s}.tiles.virtualearth.net/tiles/a{q}.jpeg?g=1&n=z",
        "kind": "quadkey",
        "zoom_min": 12,
        "zoom_max": 18,
        "requires_consent": True,
        "attribution": "© Microsoft Bing",
        "tile_format": "jpg",
        "delay_s": 0.05,
    },
    "osm": {
        "name": "OpenStreetMap",
        "url_template": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        "kind": "xyz",
        "zoom_min": 12,
        "zoom_max": 17,
        "requires_consent": True,
        "attribution": "© OpenStreetMap contributors (ODbL)",
        "tile_format": "png",
        "delay_s": 0.05,
    },
}

DISCLAIMER = (
    "O usuário é o único responsável por garantir autorização para acessar, "
    "baixar, armazenar, transformar e utilizar tiles de qualquer fonte externa. "
    "O uso de Google Satellite e Bing Satellite está sujeito aos Termos de Serviço "
    "de cada provedor. Esta ferramenta é fornecida apenas para uso interno em "
    "planejamento de redes de radiofrequência."
)

_USER_AGENT = "HTZDataFactory/1.3 (+RF-planning; internal use)"


# ── Funções auxiliares ────────────────────────────────────────────────────────

def list_sources() -> list[dict]:
    """Retorna metadados das fontes (sem url_template)."""
    return [
        {
            "id": k,
            "name": v["name"],
            "zoom_min": v["zoom_min"],
            "zoom_max": v["zoom_max"],
            "requires_consent": v["requires_consent"],
            "attribution": v["attribution"],
        }
        for k, v in SOURCES.items()
    ]


def _xy_to_quadkey(x: int, y: int, z: int) -> str:
    """Converte coordenadas XYZ para Bing Quadkey."""
    quadkey = []
    for i in range(z, 0, -1):
        digit = 0
        mask = 1 << (i - 1)
        if x & mask:
            digit += 1
        if y & mask:
            digit += 2
        quadkey.append(str(digit))
    return "".join(quadkey)


def _build_url(source: dict, x: int, y: int, z: int) -> str:
    """Constrói a URL do tile para a fonte e coordenada dadas."""
    tpl = source["url_template"]
    if source["kind"] == "quadkey":
        q = _xy_to_quadkey(x, y, z)
        s = (x + y) % 4  # servidor (0-3)
        return tpl.format(s=s, q=q)
    # XYZ simples
    return tpl.format(x=x, y=y, z=z)


def _fetch_tile(url: str, retries: int = 3) -> bytes | None:
    """Baixa um tile com retry exponencial. Retorna None em caso de 404."""
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                ct = resp.headers.get("Content-Type", "")
                if not ct.startswith("image/"):
                    return None
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if attempt < retries - 1:
                time.sleep(0.1 * (2 ** attempt))
        except Exception:
            if attempt < retries - 1:
                time.sleep(0.1 * (2 ** attempt))
    return None


def _deg2tile(lat: float, lon: float, zoom: int) -> tuple[int, int]:
    """Retorna (x, y) do tile que contém (lat, lon) no zoom dado."""
    n = 1 << zoom
    x = int((lon + 180.0) / 360.0 * n)
    lat_r = math.radians(lat)
    y = int((1.0 - math.asinh(math.tan(lat_r)) / math.pi) / 2.0 * n)
    return max(0, min(n - 1, x)), max(0, min(n - 1, y))


def _count_tiles(bbox: tuple, zoom: int) -> int:
    """Conta tiles que cobrem o bbox no zoom dado."""
    west, south, east, north = bbox
    margin = 360.0 / (1 << zoom) * 0.3
    x0, y0 = _deg2tile(north + margin, west - margin, zoom)
    x1, y1 = _deg2tile(south - margin, east + margin, zoom)
    x0, x1 = min(x0, x1), max(x0, x1)
    y0, y1 = min(y0, y1), max(y0, y1)
    return (x1 - x0 + 1) * (y1 - y0 + 1)


# ── MBTiles helpers ───────────────────────────────────────────────────────────

def _init_mbtiles(conn: sqlite3.Connection, source: dict, bbox: tuple,
                  zoom: int):
    """Cria as tabelas e metadados no arquivo MBTiles."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS metadata (name TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS tiles (
            zoom_level INTEGER NOT NULL,
            tile_column INTEGER NOT NULL,
            tile_row INTEGER NOT NULL,
            tile_data BLOB NOT NULL,
            PRIMARY KEY (zoom_level, tile_column, tile_row)
        );
    """)
    west, south, east, north = bbox
    meta = [
        ("name",        source["name"]),
        ("type",        "baselayer"),
        ("version",     "1.1"),
        ("format",      source["tile_format"]),
        ("bounds",      f"{west},{south},{east},{north}"),
        ("minzoom",     str(zoom)),
        ("maxzoom",     str(zoom)),
        ("attribution", source["attribution"]),
        ("htz:source_id", next(k for k, v in SOURCES.items() if v is source)),
        ("htz:accepted_terms", "true"),
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO metadata (name, value) VALUES (?, ?)", meta
    )
    conn.commit()


def _write_tile(conn: sqlite3.Connection, z: int, x: int, y: int,
                data: bytes):
    """Escreve um tile no MBTiles com y em convenção TMS (invertido)."""
    y_tms = (1 << z) - 1 - y
    conn.execute(
        "INSERT OR REPLACE INTO tiles (zoom_level, tile_column, tile_row, tile_data)"
        " VALUES (?, ?, ?, ?)",
        (z, x, y_tms, data),
    )


# ── Download principal ────────────────────────────────────────────────────────

def download(
    bbox: tuple,
    source_id: str,
    zoom: int,
    output_path: str,
    max_tiles: int = 4000,
    progress_cb=None,
) -> dict:
    """
    Baixa tiles do bbox para um arquivo MBTiles.

    Args:
        bbox:         (west, south, east, north) em graus WGS84
        source_id:    chave em SOURCES
        zoom:         nível de zoom
        output_path:  caminho do .mbtiles de saída
        max_tiles:    limite de tiles (segurança)
        progress_cb:  callback(str) para log de progresso

    Returns:
        dict com {total, downloaded, failed, path, complete}
    """
    if source_id not in SOURCES:
        raise ValueError(f"Fonte desconhecida: '{source_id}'. Disponíveis: {list(SOURCES)}")

    source  = SOURCES[source_id]
    z_min   = source["zoom_min"]
    z_max   = source["zoom_max"]
    if not (z_min <= zoom <= z_max):
        raise ValueError(f"Zoom {zoom} fora do range permitido [{z_min}, {z_max}] para {source_id}")

    west, south, east, north = bbox

    # Calcula range de tiles com margem pequena
    margin = 360.0 / (1 << zoom) * 0.3
    x0, y0 = _deg2tile(north + margin, west  - margin, zoom)   # NW
    x1, y1 = _deg2tile(south - margin, east  + margin, zoom)   # SE
    x0, x1 = min(x0, x1), max(x0, x1)
    y0, y1 = min(y0, y1), max(y0, y1)

    total = (x1 - x0 + 1) * (y1 - y0 + 1)
    if total > max_tiles:
        raise ValueError(
            f"Área solicita {total} tiles (máximo: {max_tiles}). "
            f"Reduza a área ou o zoom."
        )

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    if progress_cb:
        progress_cb(f"[Tiles] Fonte: {source['name']} | zoom={zoom} | "
                    f"{total} tiles ({x1-x0+1}×{y1-y0+1})")

    downloaded = 0
    failed     = 0
    delay      = source.get("delay_s", 0.05)

    with sqlite3.connect(output_path) as conn:
        _init_mbtiles(conn, source, bbox, zoom)

        for ty in range(y0, y1 + 1):
            for tx in range(x0, x1 + 1):
                url  = _build_url(source, tx, ty, zoom)
                data = _fetch_tile(url)
                if data:
                    _write_tile(conn, zoom, tx, ty, data)
                    downloaded += 1
                else:
                    failed += 1

                done = downloaded + failed
                if progress_cb and done % max(1, total // 20) == 0:
                    progress_cb(
                        f"[Tiles] {done}/{total} "
                        f"({downloaded} OK, {failed} falhas)"
                    )
                time.sleep(delay)

        conn.commit()

    if progress_cb:
        progress_cb(
            f"[Tiles] Concluído: {downloaded}/{total} tiles baixados"
            + (f" | {failed} falhas" if failed else "")
        )

    return {
        "total":      total,
        "downloaded": downloaded,
        "failed":     failed,
        "path":       output_path,
        "complete":   True,
    }


# ── Serving ───────────────────────────────────────────────────────────────────

def serve_tile(mbtiles_path: str, z: int, x: int, y: int) -> bytes | None:
    """
    Lê e retorna um tile do MBTiles.
    Converte y de XYZ para TMS internamente.
    """
    y_tms = (1 << z) - 1 - y
    try:
        with sqlite3.connect(f"file:{mbtiles_path}?mode=ro", uri=True) as conn:
            row = conn.execute(
                "SELECT tile_data FROM tiles "
                "WHERE zoom_level=? AND tile_column=? AND tile_row=?",
                (z, x, y_tms),
            ).fetchone()
        return row[0] if row else None
    except Exception:
        return None


def get_metadata(mbtiles_path: str) -> dict:
    """Retorna metadados do MBTiles como dict."""
    try:
        with sqlite3.connect(f"file:{mbtiles_path}?mode=ro", uri=True) as conn:
            rows = conn.execute("SELECT name, value FROM metadata").fetchall()
        return dict(rows)
    except Exception:
        return {}
