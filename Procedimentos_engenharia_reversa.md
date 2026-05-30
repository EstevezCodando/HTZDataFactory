# Engenharia Reversa do Formato Binário ATDI

**Objetivo:** descrever os passos para entender a estrutura interna dos arquivos `.sol`, `.geo`, `.img` e `.pal` do HTZ / ICS Telecom e reproduzir a escrita byte a byte em Python.

**Arquivo de referência usado:** `22S043W.sol` e '06S035W.sol' Natal

---

## Passo 1 — Abrir o arquivo .sol em um editor de texto

A primeira coisa foi abrir o `.sol` diretamente no Notepad++ (ou qualquer editor que mostre bytes não-ASCII como `•` ou `?`).

**O que foi observado:**

- O arquivo não começa imediatamente com dados binários. Há um bloco inicial de texto legível antes.
- Aparecem números com casas decimais — coordenadas UTM provavelmente.
- Após esse bloco de texto há claramente uma região de zeros, depois mais texto, depois zeros novamente.
- A partir de determinado ponto o conteúdo é completamente binário — os dados de pixels.

**Conclusão inicial:** existe um cabeçalho ASCII antes dos dados brutos. O formato é `[header][dados]`, não comprimido.

---

## Passo 2 — Abrir em editor hexadecimal

Com o HxD (editor hexadecimal), foi possível ver byte a byte.

**Observações:**

```
Offset 0x000 (0):    00 00 00 00 ... (zeros)
Offset 0x0A0 (160):  começa conteúdo ASCII
...
Offset 0x3F2 (1010): início dos dados de pixel
```

- Os primeiros 160 bytes são zeros — padding inicial.
- A partir do byte 160 começa o bloco de coordenadas.
- O offset 1010 (0x3F2) marca o início dos dados — **o header tem exatamente 1010 bytes**.
- O último byte do header (offset 1009) é `0x1A` — o marcador de fim de arquivo ASCII (Ctrl+Z), herdado do DOS.

---

## Passo 3 — Isolar e decodificar o bloco de coordenadas

Copiando os bytes 160 em diante como ASCII, encontrou-se:

```
515337.000000  9565430.000000  543367.000000  9565430.000000
515337.000000  9537400.000000  543367.000000  9537400.000000
30.000000  30.000000
```

Quatro pares de coordenadas seguidos do tamanho do pixel. Separados por `\x00\x00` entre X e Y de cada par, e sem separador entre pares consecutivos.

**Mapeamento dos cantos:**

| Par | X (Easting) | Y (Northing) | Posição           |
| --- | ----------- | ------------ | ----------------- |
| 1   | 515337      | 9565430      | NW (top-left)     |
| 2   | 543367      | 9565430      | NE (top-right)    |
| 3   | 515337      | 9537400      | SW (bottom-left)  |
| 4   | 543367      | 9537400      | SE (bottom-right) |

**Detalhe crítico — separadores:**

```python
# Entre X e Y de cada canto: dois nulos (\x00\x00)
xs = "515337.000000"
# [bytes do xs] + \x00 \x00 + [bytes do ys]

# Entre um canto e o próximo: nenhum separador
# ys do canto N + imediatamente xs do canto N+1
```

Seguido dos dois valores de pixel size (X e Y, sempre iguais), cada um terminado com `\x00`.

---

## Passo 4 — Identificar os campos em posições fixas

Após isolar o bloco de coordenadas, foi preciso entender os campos restantes. Usando o HxD para navegar por offsets específicos:

**Byte 300:** caractere `M` → unidade metros.

**Bytes 320–323:** número inteiro em ASCII → **largura em pixels** (número de colunas).

**Bytes 330–333:** número inteiro em ASCII → **altura em pixels** (número de linhas).

**Bytes 340–348:** string `4UTN23 00`

- O `4` é um prefixo fixo
- `UTN23` é o código da projeção UTM zona 23
- `00` é sufixo fixo
- Ou seja: `4 + UTN + {zona:02d} + (espaço) + 00`

**Bytes 371–380:** string `UTN23 00` (sem o `4`, repetição do código de projeção)

**Byte 380:** `0`

**Byte 390:** `1`

**Bytes 415–417:** string `IMG`

**Bytes 515–627:** oito valores `0.000000` espaçados de 15 bytes cada — parâmetros de offset geocêntrico (datum shift), todos zero para WGS84.

**Byte 1009:** `0x1A` (Ctrl+Z, marcador de EOF no estilo DOS)

---

## Passo 5 — Verificar a projeção UTM: zona vs. arquivo

O arquivo de referência era da região do Rio de Janeiro (~43°W), zona UTM 23.

Para confirmar a lógica de cálculo da zona:

```
zona = int((longitude + 180) / 6) + 1
zona(-43°W) = int((-43 + 180) / 6) + 1 = int(137 / 6) + 1 = 22 + 1 = 23  ✓
```

O código `UTN23` no header confirmou essa fórmula.

---

## Passo 6 — Descobrir o problema do false northing

Os valores Y no header estavam em torno de **9.5 milhões** de metros — isso é coordenada UTM com false northing (o padrão EPSG:327xx adiciona 10.000.000 m para evitar Y negativo no hemisfério sul).

Mas ao abrir o `.sol` no HTZ, as coordenadas apareciam como **negativas** (ex: -434.570 km). Isso revelou que o ATDI espera Y **sem** false northing — ou seja, Y negativo no hemisfério sul.

**Teste de verificação:**

```
Y do header ATDI:  -434570.000000
Y rasterio UTM:   9565430.000000
Diferença:        10.000.000 m  ← exatamente o false northing
```

**Correção obrigatória no writer:**

```python
_FALSE_NORTHING = 10_000_000.0

if south:
    ymin = ymin - _FALSE_NORTHING
    ymax = ymax - _FALSE_NORTHING
```

Sem essa correção o arquivo abre no HTZ com a área posicionada 10.000 km acima do correto — erro invisível até testar no software.

---

## Passo 7 — Determinar o tipo de dado dos pixels

**Para o .sol:**

Após o byte 1010, cada byte é um pixel. Calculando:

```
tamanho_total  = tamanho_arquivo - 1010  (header)
pixels_esperados = largura × altura
bytes_por_pixel  = (tamanho_total) / (pixels_esperados)
               = 1  → uint8
```

Confirmado: `.sol` = **uint8, 1 byte por pixel**, sem compressão.

**Para o .geo:**

Mesmo cálculo, resultado = 2 bytes por pixel. Valores de elevação em torno de 0–2000 (metros) → **int16 little-endian**.

Verificação: abriu no HxD, leu os primeiros 2 bytes após o offset 1010:

```
bytes: C0 03
int16 LE: 0x03C0 = 960 metros  (plausível para Serra dos Órgãos/RJ)
```

**Para o .img:**

1 byte por pixel, uint8. Valores de 0 a 255 (luminância grayscale). O zero é reservado para nodata (bordas sem cobertura).

---

## Passo 8 — Engenharia reversa do formato .pal

O arquivo `.pal` não tem header — começa direto com os dados.

**Tamanho:** 7200 bytes exatos.

```
7200 / 240 = 30 bytes por entrada de cor
30 / 3 canais (R, G, B) = 10 bytes por canal
```

Abrindo no HxD, cada entrada de 10 bytes segue o padrão:

```
Para valor 90 (exemplo — canal R da cor "urban 8m"):
39 30 00 00 65 6C 20 31 00 00
'9''0' \x00 \x00 'e' 'l' ' ' '1' \x00 \x00
```

Ou seja: valor em ASCII + padding de nulos até 4 bytes + sufixo literal `el 1\x00\x00`.

**Exceção — entrada 0 (background/nodata):**

```
30 00 73 73 65 6C 20 31 00 00
'0' \x00 's''s''e''l'' ''1' \x00 \x00
```

O sufixo é `ssel 1\x00\x00` (com `ss` extra) em vez de `el 1\x00\x00`. Parece ser um campo de flag indicando "background" ou "transparência".

**Número de entradas:**

O HTZ lê 240 entradas de cor (0–239), não 256. As entradas 240–255 existiriam em um `.pal` completo de 7680 bytes, mas o ATDI usa 7200. Confirmado empiricamente — arquivos `.pal` de referência tinham exatamente 7200 bytes.

---

## Passo 9 — Verificação cruzada: escrever e comparar

Com o writer implementado, foi gerado um `.sol` sintético para a mesma área do arquivo de referência e comparados byte a byte no HxD.

**Critérios de validação:**

1. Tamanho do header: exatamente 1010 bytes ✓
2. Coordenadas nos bytes 160+: mesmos valores com 6 casas decimais ✓
3. Largura/altura nos bytes 320/330: correto ✓
4. Código UTM nos bytes 340/371: `4UTN23 00` / `UTN23 00` ✓
5. False northing subtraído: Y negativo igual ao arquivo original ✓
6. Byte 1009 = `0x1A` ✓
7. Dados de pixel após offset 1010: valores uint8 iguais ✓

O arquivo gerado abriu corretamente no HTZ, com a área posicionada exatamente sobre o Rio de Janeiro.

---

## Passo 10 — Teste do .pal: efeito cebola visível no HTZ

Para confirmar que a paleta estava correta, foi gerado um `.sol` com o efeito cebola urbano (anéis concêntricos de código 2, 3, 4, 7) e um `.pal` com as cores ATDI padrão.

No HTZ, ao importar:

- Código 0 → cinza claro (open)
- Código 2 → azul claro (urban 8m)
- Código 3 → ciano (urban 15m)
- Código 4 → verde-ciano (urban 30m)
- Código 7 → amarelo (urban 50m)
- Código 5 → verde (forest)

Os anéis concêntricos apareceram visualmente corretos ao redor das manchas urbanas — confirmando tanto a lógica do writer quanto o mapeamento da paleta.

---

## Resumo da estrutura final descoberta

### Header ATDI (1010 bytes)

```
[0   – 159]  zeros (padding)
[160 – ~280] 4 cantos UTM em ASCII
             formato por canto: {x:.6f} \x00\x00 {y:.6f}
             ordem: NW → NE → SW → SE
             (sem separador entre cantos)
[~280 – ~310] pixel_size_x \x00 pixel_size_y \x00
[300]        'M'  (unidade metros)
[320 – 323]  largura em pixels (ASCII decimal)
[330 – 333]  altura em pixels  (ASCII decimal)
[340 – 348]  '4UTN{zona:02d} 00'
[371 – 378]  'UTN{zona:02d} 00'
[380]        '0'
[390]        '1'
[415 – 417]  'IMG'
[515, 530, 545, 560, 575, 590, 605, 620]  '0.000000' (offsets geocêntricos)
[1009]       0x1A (EOF marker)
```

### Dados após o header (offset 1010)

| Arquivo | Tipo     | Bytes/pixel | Conteúdo                              |
| ------- | -------- | ----------- | ------------------------------------- |
| `.sol`  | uint8    | 1           | código de clutter ATDI (0–11)         |
| `.geo`  | int16 LE | 2           | elevação em metros (Y negativo no HS) |
| `.img`  | uint8    | 1           | luminância grayscale 1–255 (0=nodata) |
| `.blg`  | uint8    | 1           | altura de edificação em metros        |

### Paleta .pal (7200 bytes, sem header)

```
240 entradas × 3 canais × 10 bytes = 7200 bytes

Por canal (10 bytes):
  entrada 0:  '0' \x00 'ssel 1' \x00\x00
  demais:     {valor_ascii} + padding_nulos_ate_4 + 'el 1' \x00\x00

Ex: valor 90 → '90\x00\x00el 1\x00\x00'
    valor 242 → '242\x00el 1\x00\x00'
```

### Convenção de coordenadas Y (armadilha principal)

```
rasterio EPSG:327xx  →  Y positivo  (ex: 9.565.430 m)
ATDI espera          →  Y negativo  (ex: -434.570 m)
Diferença            →  10.000.000 m  (false northing)

Correção: ymin_atdi = ymin_epsg - 10_000_000
          ymax_atdi = ymax_epsg - 10_000_000
```

---

_Procedimento documentado em 2026-05-30 | HTZDataFactory v1.3 | Alvarez _
