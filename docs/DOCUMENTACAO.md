# HTZDataFactory — Documentação Técnica

> Gerador de arquivos ATDI binários (`.geo`, `.sol`, `.img`, `.pal`) para HTZ Warfare / ICS Telecom a partir de dados abertos — FABDEM, MapBiomas e imagens de satélite.

**Versão:** 1.3.0 · **Stack:** Python 3.11 · FastAPI · Celery · Redis · Rasterio · Docker

---

## Sumário

- [Visão Geral](#visão-geral)
- [Arquitetura — Contexto (C4 L1)](#arquitetura--contexto-c4-l1)
- [Arquitetura — Containers (C4 L2)](#arquitetura--containers-c4-l2)
- [Camadas DDD](#camadas-ddd)
- [Diagrama de Classes](#diagrama-de-classes)
- [Casos de Uso](#casos-de-uso)
- [Sequência — Gerar Pacote HTZ](#sequência--gerar-pacote-htz)
- [Sequência — Preview de Tiles](#sequência--preview-de-tiles)
- [Sequência — Download FABDEM](#sequência--download-fabdem)
- [Estrutura de Arquivos](#estrutura-de-arquivos)
- [Formato Binário ATDI](#formato-binário-atdi)
- [API Reference](#api-reference)
- [Configuração (.env)](#configuração-env)
- [Docker Compose](#docker-compose)
- [Tabela MapBiomas → ATDI](#tabela-mapbiomas--atdi)
- [Procedimentos](#procedimentos)
- [Troubleshooting](#troubleshooting)

---

## Visão Geral

| Item | Detalhe |
|------|---------|
| **DEM** | FABDEM v1.2 — University of Bristol (CC-BY-4.0) |
| **Clutter** | MapBiomas Collection 10 2023 (CC-BY-4.0) |
| **Imagem** | Sentinel-2 L2A · Google Satellite · Bing Satellite · OpenStreetMap |
| **Saídas** | `.geo` int16 30 m · `.sol` uint8 30 m · `.img` uint8 15 m · `.pal` 7200 bytes |
| **Arquitetura** | Docker Compose · DDD · Celery workers · Redis broker |
| **Frontend** | Leaflet.js — mapa interativo, preview de tiles, download |

---

## Arquitetura — Contexto (C4 L1)

```mermaid
graph TB
  User(["👤 Engenheiro RF"])

  subgraph HTZ["HTZDataFactory"]
    App["🖥 Sistema\nFastAPI + Celery\nDocker Compose"]
  end

  FABDEM["🌐 FABDEM\nUniversity of Bristol\ndownload por demanda"]
  MB["💾 MapBiomas\narquivo local ~5.5 GB"]
  S2["🛰 Planetary Computer\nSentinel-2 STAC"]
  Tiles["🗺 Google · Bing · OSM\ntiles de satélite"]
  HTZ_SW["📡 HTZ Warfare\nICS Telecom"]

  User -->|"seleciona área\nescolhe layers"| App
  App -->|"ZIP com .geo .sol .img .pal"| User
  App <-->|"download tiles por demanda"| FABDEM
  App <-->|"leitura janelada"| MB
  App <-->|"busca + download bandas"| S2
  App <-->|"download tiles para preview"| Tiles
  User -->|"importa arquivos ATDI"| HTZ_SW
```

---

## Arquitetura — Containers (C4 L2)

```mermaid
graph TB
  Browser["🌐 Browser :8080"]

  subgraph Docker["Docker Compose Network"]
    nginx["nginx:1.27\n:8080 → :80\nproxy + static files\nrate limiting"]
    api["API FastAPI\n:8000 interno\nREST + jobs + previews"]
    worker["Celery Worker\nprefork pool\npipeline ATDI"]
    redis["Redis 7\n:6379 interno\nbroker + result backend"]
    flower["Flower :5555\nlocalhost apenas\nmonitor workers"]
  end

  vFABDEM[("📁 /dados/fabdem\nbind-mount :ro")]
  vMB[("📁 /dados/mapbiomas\nbind-mount :ro")]
  vSaidas[("📁 /saidas\nbind-mount :rw\njobs + previews")]

  Browser --> nginx
  nginx --> api
  api --> redis
  worker --> redis
  flower --> redis
  api --- vSaidas
  worker --- vFABDEM
  worker --- vMB
  worker --- vSaidas
```

---

## Camadas DDD

```mermaid
graph TD
  subgraph Interface["Interface"]
    API["API FastAPI\nmain.py · schemas.py"]
    Worker["Celery Worker\ntasks.py"]
    FE["Frontend Leaflet\nindex.html"]
  end

  subgraph Aplicacao["Aplicação"]
    GP["gerar_pacote_htz.py"]
    CA["converter_arquivo.py"]
  end

  subgraph Dominio["Domínio"]
    Entidades["Entidades\nAreaInteresse · CelulaFABDEM\nTileMapBiomas · Evento"]
    Servicos["Serviços\nreclassifica_clutter()\naplicar_onion()\nPALETTE_FLAT"]
  end

  subgraph Infra["Infraestrutura"]
    FABDEM_A["fabdem_rasterio\ndownload + .geo"]
    MB_A["mapbiomas_windowed\njanela GDAL + .sol"]
    ATDI["atdi_writer\nbinário ATDI"]
    S2_A["sentinel_stac\nSTAC + .img"]
    OSM_A["osm_tiles · tile_downloader\nmbtiles_to_img"]
    AuditA["audit_jsonl\nlogging JSONL"]
  end

  Interface --> Aplicacao
  Aplicacao --> Dominio
  Aplicacao --> Infra
```

---

## Diagrama de Classes

```mermaid
classDiagram
  class AreaInteresse {
    +bbox: tuple
    +job_id: str
    +layers: list~str~
    +centro: tuple
    +area_graus2: float
  }

  class CelulaFABDEM {
    +codigo: str
    +lat_min: float
    +lat_max: float
    +lon_min: float
    +lon_max: float
    +bloco_zip: str
    +arquivo_zip: str
    +tif_local: Optional~str~
    +disponivel: bool
    +intersecta(w,s,e,n) bool
  }

  class Evento {
    +job_id: str
    +etapa: str
    +dados: dict
    +ts: str
    +to_dict() dict
  }

  class JobRequest {
    +bbox: list~float~
    +layers: list~str~
    +img_fonte: str
    +osm_zoom: int
    +img_preview_id: str
  }

  class PreviewRequest {
    +bbox: list~float~
    +source_id: str
    +zoom: int
    +accepted_terms: bool
  }

  class JobStatus {
    +job_id: str
    +status: str
    +zip_bytes: int
    +arquivos: list
    +progresso: list
    +cache_hit: bool
    +duracao_s: float
  }

  class AtdiWriter {
    +write_geo(path, data, ...)
    +write_sol(path, data, ...)
    +write_img(path, data, ...)
    +write_pal(path, palette)
    +write_pal_grayscale(path)
    -_build_header() bytes
    -_FALSE_NORTHING: 10000000
  }

  class ProcessamentoServicos {
    +PALETTE_FLAT: list
    +carregar_tabela_reclassificacao(csv) dict
    +reclassifica_clutter(arr, tabela) ndarray
    +aplica_onion(clutter, px, ring) ndarray
    +stats_urbanos(clutter) dict
  }

  JobRequest --> AreaInteresse
  JobRequest --> JobStatus
  PreviewRequest --> JobRequest
  AreaInteresse "1" --> "*" CelulaFABDEM
  AreaInteresse --> Evento
  AtdiWriter --> ProcessamentoServicos
```

---

## Casos de Uso

```mermaid
graph LR
  U(["👤 Engenheiro RF"])

  UC1["Gerar Pacote HTZ\ngeo + sol + img"]
  UC2["Pré-visualizar Imagem\nGoogle · Bing · OSM"]
  UC3["Converter GeoTIFF\ncustomizado"]
  UC4["Monitorar Job\npolling status"]
  UC5["Baixar Arquivos\nZIP ou individual"]
  UC6["Ver Grade FABDEM\nno mapa"]

  U --- UC1
  U --- UC2
  U --- UC3
  U --- UC4
  U --- UC5
  U --- UC6
  UC1 --> UC4
  UC1 --> UC5
  UC2 -->|confirma| UC1
  UC3 --> UC4
  UC3 --> UC5
```

| Caso de Uso | Pré-condição | Resultado |
|-------------|-------------|-----------|
| Gerar Pacote HTZ | Área selecionada, layers válidos, dados disponíveis | ZIP com `.geo .sol .img .pal manifesto.json audit.jsonl` |
| Pré-visualizar Imagem | Área selecionada, fonte escolhida, termos aceitos | Tiles no mapa para aprovação visual |
| Converter GeoTIFF | Arquivo `.tif` válido (DEM/clutter/imagem) | Arquivo ATDI correspondente |
| Monitorar Job | Job criado (pending/running) | Log de progresso em tempo real |
| Baixar Arquivos | Job concluído (done) | ZIP completo ou arquivo individual |

---

## Sequência — Gerar Pacote HTZ

```mermaid
sequenceDiagram
  actor User as Engenheiro RF
  participant FE as Frontend
  participant API as FastAPI
  participant Redis
  participant W as Worker Celery
  participant Ext as Fontes Externas

  User->>FE: seleciona área + layers + Gerar
  FE->>API: POST /jobs {bbox, layers, img_fonte}
  API->>API: valida + verifica cache LRU
  API->>Redis: enfileira gerar_pacote_task
  API-->>FE: 202 {job_id}

  loop polling 2s
    FE->>API: GET /jobs/{id}
    API-->>FE: {status, progresso[]}
  end

  Redis->>W: despacha task
  opt layer "geo"
    W->>Ext: download ZIP FABDEM (se ausente)
    W->>W: merge tiles + reprojeção UTM
    W->>W: write_geo() → .geo int16 LE
  end
  opt layer "sol"
    W->>W: janela GDAL MapBiomas
    W->>W: reclassifica + aplica_onion()
    W->>W: write_sol() → .sol uint8
  end
  opt layer "img"
    W->>Ext: Sentinel-2 / OSM / MBTiles preview
    W->>W: grayscale BT.601 + stretch p2-p98
    W->>W: write_img() + write_pal_grayscale()
  end
  W->>W: manifesto.json + ZIP
  W->>Redis: resultado done

  FE->>API: GET /jobs/{id}/files
  FE-->>User: painel de download
```

---

## Sequência — Preview de Tiles

```mermaid
sequenceDiagram
  actor User
  participant FE as Frontend
  participant API as FastAPI
  participant TD as TileDownloader
  participant Src as Google/Bing/OSM

  User->>FE: escolhe fonte + zoom + aceita termos
  User->>FE: clica Baixar e pré-visualizar
  FE->>API: POST /img/preview {bbox, source_id, zoom, accepted_terms:true}
  API->>TD: run_in_executor → download()
  API-->>FE: 202 {preview_id}

  loop polling 1s
    FE->>API: GET /img/preview/{id}/status
    API-->>FE: {done, total, percent}
    FE->>FE: atualiza barra de progresso
  end

  loop para cada tile
    TD->>Src: GET tile (XYZ ou Quadkey)
    Src-->>TD: PNG/JPEG bytes
    TD->>TD: SQLite INSERT (y_tms = 2^z-1-y)
    TD->>TD: sleep 50ms
  end

  TD->>API: complete: true

  FE->>FE: L.tileLayer(minNativeZoom=zoom, maxNativeZoom=zoom)
  FE->>FE: map.fitBounds(bbox)
  FE-->>User: imagem no mapa

  User->>FE: Confirmar e usar esta imagem
  User->>FE: Gerar com layers
  FE->>API: POST /jobs {img_fonte:mbtiles, img_preview_id}
```

---

## Sequência — Download FABDEM

```mermaid
sequenceDiagram
  participant W as Worker
  participant GJ as GeoJSON Catálogo
  participant FS as Filesystem
  participant Bristol as data.bris.ac.uk

  W->>GJ: _load_catalog(geojson_path)
  GJ-->>W: list[CelulaFABDEM]
  W->>W: filtra células que intersectam bbox

  loop para cada célula relevante
    W->>FS: _encontrar_tif_local() busca recursiva
    alt TIF existe em raiz ou subpasta
      FS-->>W: caminho .tif
    else TIF ausente
      W->>Bristol: GET ZIP bloco
      Bristol-->>W: ZIP bytes
      W->>FS: extrai só o TIF necessário
    end
  end

  W->>W: rasterio.merge(tiles, bounds=clip)
  W->>W: reproject WGS84 → UTM 30m
  W->>W: write_geo() → .geo
  W-->>W: (geo_path, grade{xmin,ymin,w,h,epsg})
```

---

## Estrutura de Arquivos

```
HTZDataFactory/
├── docker-compose.yml          # 5 serviços
├── Dockerfile                  # python:3.11-slim + GDAL
├── requirements.txt
├── .env.example                # template de configuração
├── config/
│   └── mapbiomas_atdi.csv      # tabela reclassificação (editável sem rebuild)
├── nginx/
│   └── nginx.conf              # proxy, rate limits, security headers
├── docs/
│   ├── index.html              # documentação interativa (local)
│   └── DOCUMENTACAO.md         # esta documentação (GitHub)
├── dados/
│   ├── fabdem/                 # GeoJSON + tiles .tif + blocos .zip
│   └── mapbiomas/              # TIF collection 10 ~5.5 GB
├── saidas/                     # jobs ZIP + previews MBTiles
├── frontend/
│   └── index.html              # SPA Leaflet
└── src/
    ├── dominio/
    │   ├── area_interesse/entidades.py
    │   ├── catalogo/entidades.py
    │   ├── processamento/servicos.py   # clutter, onion, PALETTE_FLAT
    │   └── auditoria/entidades.py
    ├── aplicacao/casos_uso/
    │   ├── gerar_pacote_htz.py         # orquestrador principal
    │   └── converter_arquivo.py        # converte GeoTIFF customizado
    ├── infraestrutura/adaptadores/
    │   ├── atdi_writer.py              # writer binário ATDI
    │   ├── fabdem_rasterio.py          # download + .geo
    │   ├── mapbiomas_windowed.py       # janela GDAL + .sol
    │   ├── sentinel_stac.py            # Sentinel-2 + .img
    │   ├── osm_tiles.py                # OSM tiles + .img
    │   ├── tile_downloader.py          # Google/Bing/OSM → MBTiles
    │   └── mbtiles_to_img.py           # MBTiles → .img ATDI
    └── interface/
        ├── api/main.py                 # FastAPI + endpoints
        ├── api/schemas.py              # Pydantic schemas
        └── worker/tasks.py             # Celery task
```

---

## Formato Binário ATDI

### Header (1010 bytes) — mesmo para .geo, .sol, .img

| Offset | Tamanho | Conteúdo |
|--------|---------|---------|
| 0–159 | 160 | Zeros (padding) |
| 160–~279 | variável | 4 cantos UTM em ASCII: `{x:.6f}\x00\x00{y:.6f}` (NW→NE→SW→SE) |
| ~280–~300 | variável | pixel_size_x`\x00` pixel_size_y`\x00` |
| 300 | 1 | `'M'` (metros) |
| 320–323 | 4 | largura em pixels (ASCII decimal) |
| 330–333 | 4 | altura em pixels (ASCII decimal) |
| 340–348 | 9 | `'4UTN{zona:02d} 00'` |
| 371–378 | 8 | `'UTN{zona:02d} 00'` |
| 415–417 | 3 | `'IMG'` |
| 515, 530… (8×) | 8×8 | `'0.000000'` (offsets geocêntricos) |
| 1009 | 1 | `0x1A` (EOF marker DOS) |

### Dados após o Header

| Arquivo | Tipo | Bytes/px | Conteúdo |
|---------|------|---------|---------|
| `.sol` | uint8 | 1 | código clutter ATDI 0–11 |
| `.geo` | int16 LE | 2 | elevação metros (Y negativo HS) |
| `.img` | uint8 | 1 | luminância grayscale 1–255 (0=nodata) |

> ⚠️ **False Northing:** EPSG:327xx adiciona 10.000.000 m ao Y. O ATDI espera Y negativo.  
> Correção: `y_atdi = y_epsg − 10.000.000`

### Formato .pal (7200 bytes)

```
240 entradas × 30 bytes = 7200 bytes
Por entrada: canal R (10 bytes) + canal G (10 bytes) + canal B (10 bytes)
Por campo: str(valor).encode('ascii') + b'\x00' * (10 - len(str(valor)))

Exemplos:
  valor=0   → b'0\x00\x00\x00\x00\x00\x00\x00\x00\x00'
  valor=31  → b'31\x00\x00\x00\x00\x00\x00\x00\x00'
  valor=240 → b'240\x00\x00\x00\x00\x00\x00\x00'
```

> ℹ️ O `.pal` acompanha **exclusivamente o `.img`**. O `.sol` não usa `.pal` — o HTZ gerencia as cores de clutter internamente.

---

## API Reference

### /jobs

| Método | Rota | Descrição |
|--------|------|---------|
| `POST` | `/jobs` | Cria job. Body: `JobRequest`. Retorna 202 `JobStatus` |
| `GET` | `/jobs/{job_id}` | Status + log de progresso |
| `GET` | `/jobs/{job_id}/download` | Download ZIP completo |
| `GET` | `/jobs/{job_id}/files` | Lista arquivos com URLs individuais |
| `GET` | `/jobs/{job_id}/download/{filename}` | Arquivo individual |
| `GET` | `/jobs/{job_id}/audit` | Histórico JSONL de auditoria |
| `GET` | `/jobs` | Lista todos os jobs |

**JobRequest:**
```json
{
  "bbox": [-47.5, -16.0, -47.0, -15.5],
  "layers": ["geo", "sol", "img"],
  "img_fonte": "sentinel2",
  "osm_zoom": 15,
  "img_preview_id": null
}
```

### /img/preview

| Método | Rota | Descrição |
|--------|------|---------|
| `GET` | `/img/sources` | Lista fontes de tiles (Google, Bing, OSM) com metadados |
| `POST` | `/img/preview` | Inicia download tiles → MBTiles. Retorna 202 `{preview_id}` |
| `GET` | `/img/preview/{id}/status` | Progresso: `{done, total, percent, status}` |
| `GET` | `/img/preview/{id}/tile/{z}/{x}/{y}` | Serve tile PNG/JPEG do MBTiles |
| `DELETE` | `/img/preview/{id}` | Remove preview e apaga MBTiles |

### Outros

| Método | Rota | Descrição |
|--------|------|---------|
| `GET` | `/health` | Health check: `{fabdem_ok, mapbiomas_ok, csv_ok}` |
| `GET` | `/status` | Status das camadas disponíveis |
| `POST` | `/convert` | Converte GeoTIFF customizado (form-data: file, tipo) |
| `GET` | `/tiles/fabdem` | GeoJSON 810 células FABDEM com status local |
| `GET` | `/tiles/mapbiomas` | GeoJSON bbox MapBiomas local |
| `DELETE` | `/cache` | Limpa cache LRU (requer X-API-Key) |

---

## Configuração (.env)

| Variável | Obrig. | Padrão | Descrição |
|----------|--------|--------|---------|
| `FABDEM_DIR` | ✅ | — | Pasta com GeoJSON catálogo + tiles |
| `MAPBIOMAS_DIR` | ✅ | — | Pasta com TIF MapBiomas |
| `MAPBIOMAS_FILENAME` | ✅ | `mapbiomas_10m_...tif` | Nome exato do arquivo TIF |
| `HTZ_API_KEY` | — | `""` | Chave para DELETE /cache |
| `FLOWER_USER` | — | `admin` | Usuário Flower |
| `FLOWER_PASSWORD` | ⚠️ | `troque_aqui` | Senha Flower |
| `HTTP_PORT` | — | `8080` | Porta nginx |
| `GDAL_CACHEMAX` | — | `4096` | Cache GDAL em MB (~25% RAM) |
| `MAX_CONCURRENT_JOBS` | — | `4` | Jobs simultâneos |
| `WORKER_CONCURRENCY` | — | `4` | Processos Celery por worker |
| `WORKER_MEMORY_LIMIT` | — | `8G` | Limite de memória do worker |

---

## Docker Compose

| Serviço | Imagem | Porta | Volumes |
|---------|--------|-------|---------|
| `api` | build local | 8000 (interno) | /dados/fabdem:ro · /dados/mapbiomas:ro · ./saidas · ./src:ro · ./frontend:ro |
| `worker` | build local | — | mesmos da api |
| `redis` | redis:7-alpine | 6379 (interno) | redis_data |
| `nginx` | nginx:1.27-alpine | **8080→80** | ./nginx/nginx.conf · ./frontend · ./docs |
| `flower` | mher/flower:2.0 | **localhost:5555** | — |

**Nginx — Rate Limits:**

| Zona | Rate | Burst | Endpoints |
|------|------|-------|---------|
| `api_general` | 20 r/s | 40 | padrão |
| `job_create` | 2 r/s | 5 | POST /jobs |
| `upload` | 1 r/s | 3 | POST /convert |
| `tile_serve` | 200 r/s | 500 | /img/preview/*/tile/* |

---

## Tabela MapBiomas → ATDI

Arquivo: `config/mapbiomas_atdi.csv` — editável sem rebuild.

| Código ATDI | Nome | RGB | MapBiomas mapeados |
|-------------|------|-----|-------------------|
| 0 | open | 240,240,240 | Campo, pastagem, sem dado |
| 1 | suburban | 91,90,238 | — |
| 2 | urban 8m | 90,167,242 | Área urbanizada (base efeito cebola) |
| 3 | urban 15m | 90,243,247 | Efeito cebola — anel 1 |
| 4 | urban 30m | 89,247,172 | Efeito cebola — anel 2 |
| 5 | forest | 96,226,97 | Floresta, mangue, silvicultura |
| 6 | hydro | 138,198,92 | Rios, lagos, campo alagado |
| 7 | urban 50m | 249,248,89 | Efeito cebola — núcleo |
| 8 | wood | 247,184,92 | Cerrado, savana |
| 9 | roof | 241,90,90 | Telhado/edificação |

**Efeito Cebola Urbano:**
```mermaid
graph LR
  A["urban 8m\ncódigo 2"] -->|erosão 300m| B["urban 15m\ncódigo 3"]
  B -->|erosão 300m| C["urban 30m\ncódigo 4"]
  C -->|erosão 300m| D["urban 50m\ncódigo 7\nnúcleo"]
```

---

## Procedimentos

### Primeiro Deploy

```bash
git clone https://github.com/seu-usuario/HTZDataFactory.git
cd HTZDataFactory
cp .env.example .env
# Editar .env: FABDEM_DIR, MAPBIOMAS_DIR, MAPBIOMAS_FILENAME
docker compose up --build -d
curl http://localhost:8080/health
```

### Reinicialização (sem rebuild)

```bash
# Código src/ ou frontend/ mudou:
docker compose restart api worker nginx

# Escalar workers:
docker compose up --scale worker=3 -d
```

### Diagnóstico

```bash
docker compose ps
docker compose logs api --tail=50
docker compose logs worker --tail=50
docker compose exec api ls /dados/fabdem/
docker compose exec redis redis-cli LLEN celery
```

---

## Troubleshooting

| Sintoma | Causa | Solução |
|---------|-------|---------|
| `offline — JSON.parse` | nginx 502 / containers parados | `docker compose restart api worker nginx` |
| `fabdem_ok: false` | GeoJSON catálogo não encontrado | Verificar `FABDEM_DIR` e existência do GeoJSON |
| `mapbiomas_ok: false` | TIF não encontrado | Verificar `MAPBIOMAS_DIR` + `MAPBIOMAS_FILENAME` |
| Job fica `pending` | Worker não consome | `docker compose logs worker` + restart |
| 422 em `POST /img/preview` | `accepted_terms: false` ou bbox inválido | Aceitar termos, verificar bbox ≤ 5°×5° |
| 429 em tiles | Rate limit antigo | Nginx já tem zona `tile_serve` 200r/s |
| Tiles não aparecem no mapa | Zoom Leaflet ≠ zoom dos tiles | `minNativeZoom`/`maxNativeZoom` já configurados |
| Sentinel-2 falha | Sem acesso ao Planetary Computer | Usar Google/Bing/OSM como alternativa |
| .sol com coordenadas erradas | False northing | Confirmar área no HS e bbox `[w,s,e,n]` |

---

*Documentação gerada em 2026 · HTZDataFactory v1.3.0*
