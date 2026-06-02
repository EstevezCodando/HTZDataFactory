"""
Gerador de arquivos binarios nativos ATDI: .sol, .geo, .img

Formato do header (1010 bytes), mapeado a partir de arquivo de referencia
22S043W.sol da pasta 'Camadas HTZ':

  [0-159]   zeros (padding)
  [160...]  4 cantos UTM, cada par escrito como: x_str\x00\x00y_str
            Ordem: (xmin,ymax) (xmax,ymax) (xmin,ymin) (xmax,ymin)
  [pos...]  pixel_size_x_str\x00 pixel_size_y_str\x00
  [300]     'M' (unidade metros) — POSICAO FIXA
  [320-323] largura em pixels     — POSICAO FIXA
  [330-333] altura em pixels      — POSICAO FIXA
  [340-348] '4UTNxx 00' (projecao) — POSICAO FIXA
  [371-380] 'UTNxx 00'             — POSICAO FIXA
  [380]     '0'
  [390]     '1'
  [415-417] 'IMG'
  [515-627] 8x '0.000000' (offset 15 bytes cada)
  [1009]    '\x1a' (EOF marker)

Dados apos header (offset 1010):
  .sol -> uint8  (1 byte/pixel) codigo de clutter ATDI
  .geo -> int16  (2 bytes/pixel, little-endian) elevacao em metros
  .img -> uint8  (1 byte/pixel) imagem 8-bit na metade do pixel size
"""

import numpy as np


def _utm_zone_for_lon(lon: float) -> int:
    return int((lon + 180) / 6) + 1


def _utm_epsg(lon: float, south: bool = True) -> int:
    zone = _utm_zone_for_lon(lon)
    return 32700 + zone if south else 32600 + zone


def _utm_code(lon: float) -> str:
    zone = _utm_zone_for_lon(lon)
    return f"UTN{zone:02d}"   # ex: 'UTN23'


_FALSE_NORTHING = 10_000_000.0   # metros adicionados pelo EPSG:327xx no hemisferio sul


def _build_header(xmin: float, ymin: float, xmax: float, ymax: float,
                  pixel_size: float, width: int, height: int,
                  lon_center: float, south: bool = True) -> bytes:
    """
    Constroi o header binario ATDI de 1010 bytes.

    IMPORTANTE: O ATDI usa UTM *sem* false northing — Y negativo no hemisferio sul.
    O rasterio com EPSG:327xx retorna Y positivo (com false northing de 10.000.000m).
    Este metodo subtrai automaticamente o false northing quando south=True.
    """
    buf = bytearray(1010)
    utm_code = _utm_code(lon_center)   # ex: 'UTN23'

    # Converte Y do padrao EPSG (Y positivo com false northing) para ATDI (Y negativo)
    if south:
        ymin = ymin - _FALSE_NORTHING
        ymax = ymax - _FALSE_NORTHING

    def _w(pos: int, s: str):
        enc = s.encode('ascii')
        buf[pos: pos + len(enc)] = enc

    # ── Bloco de coordenadas a partir do byte 160 ─────────────────────────
    # Formato: para cada canto, escreve x_str + \x00\x00 + y_str
    # Cantos na ordem: NW, NE, SW, SE
    corners = [
        (xmin, ymax),   # NW (top-left)
        (xmax, ymax),   # NE (top-right)
        (xmin, ymin),   # SW (bottom-left)
        (xmax, ymin),   # SE (bottom-right)
    ]
    pos = 160
    for (cx, cy) in corners:
        xs = f"{cx:.6f}"
        ys = f"{cy:.6f}"
        _w(pos, xs)
        pos += len(xs) + 2       # 2 nulos separadores entre X e Y
        _w(pos, ys)
        pos += len(ys)           # Y imediatamente seguido pelo proximo canto

    # Pixel sizes
    px_str = f"{pixel_size:.6f}"
    _w(pos, px_str)
    pos += len(px_str) + 1       # 1 nulo separador
    _w(pos, px_str)
    pos += len(px_str) + 1

    # ── Campos em posicoes fixas (verificadas no arquivo de referencia) ──
    _w(300, "M")
    _w(320, str(width))
    _w(330, str(height))
    _w(340, f"4{utm_code}")
    _w(347, "00")
    _w(371, utm_code)
    _w(377, "00")
    buf[380] = ord('0')
    buf[390] = ord('1')
    _w(415, "IMG")

    # 8 parametros de offset geocentrico (todos zero)
    for offset in [515, 530, 545, 560, 575, 590, 605, 620]:
        _w(offset, "0.000000")

    # EOF marker (Ctrl-Z)
    buf[1009] = 0x1A

    return bytes(buf)


# ─── Writers publicos ─────────────────────────────────────────────────────────

def write_sol(path: str, data: np.ndarray,
              xmin: float, ymin: float, pixel_size: float,
              lon_center: float, south: bool = True):
    """
    Escreve .sol (clutter uint8, 1 byte/pixel).
    data: array 2D uint8 com codigos ATDI, shape=(height, width).
    xmin, ymin: canto inferior esquerdo em metros UTM.
    """
    height, width = data.shape
    xmax = xmin + width  * pixel_size
    ymax = ymin + height * pixel_size
    header = _build_header(xmin, ymin, xmax, ymax, pixel_size,
                           width, height, lon_center, south)
    with open(path, 'wb') as f:
        f.write(header)
        f.write(data.astype(np.uint8).tobytes())


def write_geo(path: str, data: np.ndarray,
              xmin: float, ymin: float, pixel_size: float,
              lon_center: float, south: bool = True):
    """
    Escreve .geo (terreno int16, 2 bytes/pixel little-endian).
    data: array 2D int16 com elevacao em metros.
    """
    height, width = data.shape
    xmax = xmin + width  * pixel_size
    ymax = ymin + height * pixel_size
    header = _build_header(xmin, ymin, xmax, ymax, pixel_size,
                           width, height, lon_center, south)
    with open(path, 'wb') as f:
        f.write(header)
        f.write(data.astype('<i2').tobytes())   # little-endian int16


def write_blg(path: str, data: np.ndarray,
              xmin: float, ymin: float, pixel_size: float,
              lon_center: float, south: bool = True):
    """
    Escreve .blg (building DHM uint8, 1 byte/pixel).
    data: array 2D uint8 com altura AGL em metros (0 = sem edificacao).
    Formato identico ao .sol mas com semantica de altura de edificacao.
    """
    height, width = data.shape
    xmax = xmin + width  * pixel_size
    ymax = ymin + height * pixel_size
    header = _build_header(xmin, ymin, xmax, ymax, pixel_size,
                           width, height, lon_center, south)
    with open(path, 'wb') as f:
        f.write(header)
        f.write(data.astype(np.uint8).tobytes())


def _pal_field(value: int) -> bytes:
    """
    10 bytes por canal no formato ATDI (referencia: 06S035W.pal — area de Natal).
    Formato: str(valor).encode('ascii') + b'\\x00' * (10 - len(str(valor)))
    Exemplos:
      valor 0   → b'0\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x00'
      valor 31  → b'31\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x00'
      valor 240 → b'240\\x00\\x00\\x00\\x00\\x00\\x00\\x00'
    """
    s = str(max(0, min(255, value)))
    return s.encode('ascii') + b'\x00' * (10 - len(s))


def write_pal(path: str, palette_flat: list):
    """
    Escreve .pal ATDI (7200 bytes = 240 entradas x 30 bytes).
    palette_flat: lista de 768 ints (256 entradas RGB, como PIL putpalette).
    Usa apenas entradas 0-239.
    """
    buf = bytearray()
    for i in range(240):
        r = palette_flat[i * 3]
        g = palette_flat[i * 3 + 1]
        b = palette_flat[i * 3 + 2]
        buf += _pal_field(r)
        buf += _pal_field(g)
        buf += _pal_field(b)

    assert len(buf) == 7200, f"PAL size={len(buf)}, expected 7200"
    with open(path, 'wb') as f:
        f.write(buf)


def write_pal_grayscale(path: str):
    """
    Escreve .pal ATDI com rampa de cinza linear para uso com .img.
    Entradas 0-239: R=G=B=i  (0 = nodata/preto, 239 = quase branco).
    Gerado quando .img e solicitado sem .sol no mesmo job.
    """
    buf = bytearray()
    for i in range(240):
        field = _pal_field(i)
        buf += field + field + field   # R, G, B identicos → cinza
    assert len(buf) == 7200, f"PAL grayscale size={len(buf)}, expected 7200"
    with open(path, 'wb') as f:
        f.write(buf)


def write_img(path: str, data: np.ndarray,
              xmin: float, ymin: float, pixel_size: float,
              lon_center: float, south: bool = True):
    """
    Escreve .img (imagem 8-bit uint8, 1 byte/pixel).
    pixel_size tipicamente metade do .sol/.geo (resolucao dupla).
    """
    height, width = data.shape
    xmax = xmin + width  * pixel_size
    ymax = ymin + height * pixel_size
    header = _build_header(xmin, ymin, xmax, ymax, pixel_size,
                           width, height, lon_center, south)
    with open(path, 'wb') as f:
        f.write(header)
        f.write(data.astype(np.uint8).tobytes())
