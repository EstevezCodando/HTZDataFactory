# HTZDataFactory — Guia do Usuário

> Gerador e conversor automático de dados para HTZ / ICS Telecom

---

## O que é isso?

O **HTZDataFactory** é um sistema web que transforma dados geoespaciais abertos em arquivos no
formato **ATDI binário nativo** — o formato que o HTZ e o ICS Telecom usam para
carregar terreno, clutter e imagem de satélite.

O operador desenha uma área no mapa (ou envia seu próprio arquivo) que deseja atualizar a base de analise do HTZ, o sistema processa tudo
automaticamente e entrega um ZIP pronto para abrir no HTZ.

---

## O que o sistema produz?

| Arquivo          | O que é                                        | Resolução  | Fonte Base     |
| ---------------- | ---------------------------------------------- | ---------- | -------------- |
| `.geo`           | Modelo digital de terreno (elevação em metros) | 30 m/pixel | FABDEM v1.2    |
| `.sol`           | Clutter — tipo de superfície em cada pixel     | 30 m/pixel | MapBiomas      |
| `.pal`           | Paleta de cores do clutter para o HTZ          | —          | Gerado         |
| `.img`           | Imagem de satélite grayscale                   | 15 m/pixel | Sentinel-2 L2A |
| `manifesto.json` | Metadados, fontes e disclaimer                 | —          | Gerado         |

---

## Como usar

### Modo 1 — Gerar por Área (dados automáticos)

1. Acesse o sistema em `http://localhost:8080/app`
2. Escolha o mapa base (OSM, Google Satélite, etc.) no canto superior direito
3. Clique no ícone de retângulo na barra do mapa e desenhe sua área
4. Marque quais camadas quer gerar: `.geo`, `.sol`, `.img`
5. Clique em **Gerar pacote HTZ**
6. Acompanhe o log em tempo real
7. Quando concluído, clique em **Baixar ZIP**

**Dica:** O mapa mostra os tiles FABDEM disponíveis:

- 🟢 **Verde** = tile já salvo localmente → conversão instantânea
- 🔴 **Vermelho** = tile será baixado automaticamente do servidor da Universidade de Bristol ( Evitar esse caso devido a lentidão)

---

### Modo 2 — Converter seu próprio arquivo

Use quando já tem um raster (DEM, uso do solo ou imagem) e quer convertê-lo para ATDI.

1. Vá para a aba **Converter Arquivo**
2. Selecione o tipo de conversão: **DEM**, **Clutter** ou **Imagem**
3. Arraste ou selecione seu GeoTIFF
4. Clique em **Converter para ATDI**
5. Baixe o ZIP com o resultado

---

## Dados utilizados

### FABDEM v1.2 — Terreno

- **O que é:** Modelo digital de terreno com vegetação e edificações removidas (_bare earth_)
- **Fonte:** Universidade de Bristol — [fabdem.space](https://fabdem.space)
- **Data:** 2022
- **Como funciona:** O sistema consulta um catálogo GeoJSON de 810 tiles 1°×1°. Quando sua
  área intersecta um tile ainda não baixado, ele é obtido automaticamente do servidor da Bristol.
  Tiles baixados ficam salvos localmente para uso futuro.
- **Licença:** CC-BY-4.0

### MapBiomas Collection 10 — Clutter

- **O que é:** Mapa anual de uso e cobertura da terra do Brasil (ano 2023)
- **Fonte:** [mapbiomas.org](https://mapbiomas.org) — parceria de institutos brasileiros
- **Como funciona:** O arquivo TIF é lido apenas na janela da sua área de interesse
  (sem carregar o arquivo inteiro na memória). Os códigos MapBiomas são convertidos para
  os 10 tipos de clutter do ATDI usando a tabela `config/mapbiomas_atdi.csv`.
- **Efeito cebola urbano:** Áreas urbanas recebem automaticamente camadas concêntricas
  de altura crescente (8m → 15m → 30m → 50m) em anéis de 300m — simulando o gradiente
  de densidade das cidades.
- **Licença:** CC-BY-4.0

### Sentinel-2 L2A — Imagem

- **O que é:** Imagem óptica multibandas de 10m de resolução
- **Fonte:** ESA/Copernicus via Microsoft Planetary Computer (STAC, sem autenticação)
- **Como funciona:** O sistema busca automaticamente a cena com menos nuvens
  (<20%) dos últimos 3 anos para sua área. Baixa as bandas RGB e converte para
  grayscale 8-bit alinhado ao grid do `.geo`.
- **Licença:** Open Access

---

## Como carregar seus próprios dados

### Substituir o MapBiomas

Para usar seu próprio mapa de uso do solo:

1. Exporte sua área do [mapbiomas.org](https://mapbiomas.org) como GeoTIFF
2. Vá para **Converter Arquivo → Clutter**
3. Envie o TIF — a tabela de reclassificação padrão será aplicada

Ou, para carregar como dado de base permanente do servidor:

1. Coloque o TIF na pasta mapeada como `/dados/mapbiomas/` no Docker
2. Atualize a variável `MAPBIOMAS_TIF` no `docker-compose.yml`
3. Execute `docker compose restart api worker`

### Substituir o MapBiomas com outra fonte de clutter

Se seu raster de clutter já usa os códigos ATDI (0–9), crie um CSV de identidade:

```csv
codigo_mapbiomas,nome_mapbiomas,codigo_atdi,nome_atdi,justificativa
0,open,0,open,passthrough
1,suburban,1,suburban,passthrough
2,urban_8m,2,urban_8m,passthrough
...
```

Salve como `config/mapbiomas_atdi.csv` — não é necessário rebuild do Docker.

### Carregar seu próprio DEM ( MODELO DO TERRENO PARA GERAR O GEO)

Qualquer GeoTIFF de elevação funciona (SRTM, ALOS AW3D30, Copernicus DEM 30m, etc.):

1. Vá para **Converter Arquivo → DEM**
2. Envie o GeoTIFF — qualquer projeção é aceita (WGS84, UTM, SIRGAS2000...)
3. O sistema reprojeta automaticamente para 30m UTM

Para usar como dado permanente do servidor:

1. Coloque os TIFs na pasta FABDEM com o padrão de nome: `XX_FABDEM_V1-2.tif`
2. Ou adapte o catálogo `fabdem_v1_2_brasil_celulas_1x1.geojson` para incluir seus tiles

### Carregar imagem de satélite ou ortofoto

1. Exporte sua imagem como GeoTIFF (RGB ou grayscale)
2. Vá para **Converter Arquivo → Imagem**
3. O sistema normaliza automaticamente para 8-bit e reprojeta para 15m UTM

---

## Especificações dos arquivos de entrada

### Para conversão de DEM → `.geo`

| Campo        | Requisito                                 |
| ------------ | ----------------------------------------- |
| Formato      | GeoTIFF (`.tif` / `.tiff`)                |
| Tipo de dado | Float32, Int16 ou Int32                   |
| Valores      | Elevação em **metros**                    |
| Bandas       | 1 banda                                   |
| Projeção     | Qualquer (WGS84, UTM, SIRGAS2000...)      |
| Nodata       | Qualquer valor definido no TIF (ou -9999) |
| Resolução    | Qualquer (reamostrado para 30m UTM)       |

### Para conversão de Clutter → `.sol` + `.pal`

| Campo        | Requisito                                      |
| ------------ | ---------------------------------------------- |
| Formato      | GeoTIFF (`.tif` / `.tiff`)                     |
| Tipo de dado | UInt8 ou UInt16                                |
| Valores      | Inteiros representando classes de uso do solo  |
| Bandas       | 1 banda                                        |
| Sistema      | MapBiomas (0–48) **ou** diretamente ATDI (0–9) |
| Projeção     | Qualquer                                       |

#### Tabela de clutter ATDI (resultado no `.sol`)

| Código | Nome ATDI | Descrição                                       |
| ------ | --------- | ----------------------------------------------- |
| 0      | open      | Área aberta, pastagem, sem dado                 |
| 1      | suburban  | Suburbano, área de transição                    |
| 2      | urban_8m  | Urbano base — efeito cebola começa aqui         |
| 3      | urban_15m | Urbano 15m (2ª camada — gerado automaticamente) |
| 4      | urban_30m | Urbano 30m (3ª camada — gerado automaticamente) |
| 5      | forest    | Floresta densa, dossel fechado                  |
| 6      | hydro     | Rios, lagos, reservatórios                      |
| 7      | urban_50m | Centro urbano 50m (gerado automaticamente)      |
| 8      | wood      | Cerrado, caatinga, vegetação arbustiva          |
| 9      | roof      | Telhado, superfície impermeável                 |

### Para conversão de Imagem → `.img`

| Campo        | Requisito                                            |
| ------------ | ---------------------------------------------------- |
| Formato      | GeoTIFF (`.tif` / `.tiff`)                           |
| Bandas       | 1 (grayscale) ou 3 (RGB) — convertido para grayscale |
| Tipo de dado | UInt8, UInt16 ou Float32 (normalizado para 8-bit)    |
| Projeção     | Qualquer                                             |

> **Nota sobre `.tfw`:** Se o GeoTIFF não tiver CRS embutido, coloque um arquivo
> `.tfw` com o mesmo nome no mesmo diretório. O sistema assume WGS84 para arquivos com `.tfw`.

---

## Limites recomendados de área

| Área (graus) | Dimensão aproximada | Tempo estimado  |
| ------------ | ------------------- | --------------- |
| 0,5° × 0,5°  | ~55 × 55 km         | 30–60 s         |
| 1° × 1°      | ~111 × 111 km       | 60–120 s        |
| 2° × 2°      | ~222 × 222 km       | 2–5 min         |
| > 3° × 3°    | —                   | Não recomendado |

---

## Tabela de reclassificação (`config/mapbiomas_atdi.csv`)

Este arquivo controla como cada classe do MapBiomas é convertida para clutter ATDI.
Pode ser editado sem rebuild do Docker — a mudança entra em vigor no próximo job.

```csv
codigo_mapbiomas,nome_mapbiomas,codigo_atdi,nome_atdi,justificativa
3,Formação Florestal,5,forest,dossel fechado
4,Formação Savânica,8,wood,dossel aberto
24,Área Urbanizada,2,urban_8m,base do efeito cebola
33,Rio/lago/oceano,6,hydro,superfície aquática
```

---

## Como abrir no HTZ

1. Descompacte o ZIP baixado
2. No HTZ : `File → Import → ATDI Files`
3. Selecione o `.sol` ou `.geo` — o software carrega automaticamente os demais arquivos do mesmo diretório com o mesmo nome base

---

## Monitoramento

- **Interface web:** `http://localhost:8080/app`
- **API (Swagger):** `http://localhost:8080/docs`
- **Flower (workers):** `http://localhost:5555` — usuário `admin`, senha `htz2024`
- **Logs:** `docker compose logs -f api`

---

## Escalando workers

Para processar múltiplos jobs em paralelo:

```bash
docker compose up --scale worker=4 -d
```

Cada worker ocupa ~2 GB de RAM no pico. Em uma máquina de 32 GB, use até 8 workers.

---

_Dados gerados automaticamente para uso em simulações de RF. Validação em campo é recomendada antes do uso operacional._
