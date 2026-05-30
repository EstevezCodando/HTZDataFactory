"""
Dominio: Processamento Geoespacial — Servicos de dominio.

Logica de negocio pura, sem dependencias de infraestrutura.
Migrado e refatorado de webapp/processing/mapbiomas_sol.py.
"""

import csv
import numpy as np
from typing import Optional


# ─── Paleta de cores ATDI (HTZ) ───────────────────────────────────────
# Cores extraidas diretamente do painel "Clutter parameters" do HTZ
# 256 entradas RGB, lista plana (768 bytes)
PALETTE_FLAT: list[int] = [0] * 768

def _sp(i, r, g, b):
    PALETTE_FLAT[i*3:i*3+3] = [r, g, b]

_sp(0,  240, 240, 240)   # open       - cinza claro (fundo)
_sp(1,   91,  90, 238)   # suburban   - azul-roxo
_sp(2,   90, 167, 242)   # urban 8m   - azul claro
_sp(3,   90, 243, 247)   # urban 15m  - ciano
_sp(4,   89, 247, 172)   # urban 30m  - verde-ciano/menta
_sp(5,   96, 226,  97)   # forest     - verde vivo
_sp(6,  138, 198,  92)   # hydro      - verde-oliva
_sp(7,  249, 248,  89)   # urban 50m  - amarelo
_sp(8,  247, 184,  92)   # wood       - laranja
_sp(9,  241,  90,  90)   # roof/build - vermelho
_sp(10, 160, 160, 160)   # rail       - cinza medio
_sp(11, 210, 210, 210)   # road       - cinza claro
del _sp


def carregar_tabela_reclassificacao(csv_path: str) -> dict[int, int]:
    """
    Carrega mapeamento MapBiomas->ATDI do CSV externo.
    Retorna dict {codigo_mapbiomas: codigo_atdi}.
    Permite versionar a tabela sem rebuild da imagem Docker.
    """
    tabela = {}
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                mb = int(row["codigo_mapbiomas"])
                atdi = int(row["codigo_atdi"])
                tabela[mb] = atdi
            except (ValueError, KeyError):
                continue  # ignora linhas malformadas ou cabecalho
    return tabela


def reclassifica_clutter(arr: np.ndarray,
                         tabela: dict[int, int]) -> np.ndarray:
    """
    Traduz codigos MapBiomas para codigos clutter ATDI.
    Pixels nao mapeados recebem codigo 0 (open).
    """
    out = np.zeros(arr.shape, dtype=np.uint8)
    for mb, atdi in tabela.items():
        out[arr == mb] = atdi
    return out


def _erode_mask(mask: np.ndarray, n_pixels: int) -> np.ndarray:
    """
    Erosao binaria 3x3 por n_pixels iteracoes (pure numpy, sem scipy).
    Um pixel sobrevive somente se todos os 8 vizinhos tambem sao True.
    """
    result = mask.copy()
    for _ in range(n_pixels):
        padded = np.pad(result, 1, constant_values=False)
        result = (
            padded[:-2, :-2] & padded[:-2, 1:-1] & padded[:-2, 2:] &
            padded[1:-1, :-2] & padded[1:-1, 1:-1] & padded[1:-1, 2:] &
            padded[2:, :-2]   & padded[2:, 1:-1]   & padded[2:, 2:]
        )
    return result


def aplica_onion(clutter: np.ndarray, pixel_size_m: float,
                 ring_width_m: float = 300.0) -> np.ndarray:
    """
    Efeito cebola urbano: eleva densidade ATDI do exterior para o interior
    das manchas urbanas por erosao iterativa.

    Partindo de todos os pixels urbanos em codigo 2 (urban 8m):
      exterior  -> 2 (urban  8m)  [mantido da reclassificacao]
      1 erosao  -> 3 (urban 15m)
      2 erosoes -> 4 (urban 30m)
      3 erosoes -> 7 (urban 50m)

    Cidades pequenas (<ring_width_m de raio) ficam apenas com codigo 2.
    Metropoles ganham os 4 niveis automaticamente.

    ring_width_m: largura de cada anel em metros (default 300m).
    """
    ring_px = max(1, round(ring_width_m / pixel_size_m))
    urban = (clutter == 2)

    if not urban.any():
        return clutter

    m15 = _erode_mask(urban, ring_px)
    clutter[m15] = 3

    m30 = _erode_mask(m15, ring_px)
    clutter[m30] = 4

    m50 = _erode_mask(m30, ring_px)
    clutter[m50] = 7

    return clutter


def stats_urbanos(clutter: np.ndarray) -> dict[str, int]:
    """Retorna contagem de pixels por nivel urbano para log de auditoria."""
    return {
        "urban_8m":  int(np.sum(clutter == 2)),
        "urban_15m": int(np.sum(clutter == 3)),
        "urban_30m": int(np.sum(clutter == 4)),
        "urban_50m": int(np.sum(clutter == 7)),
    }
