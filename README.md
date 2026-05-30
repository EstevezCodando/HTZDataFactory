# HTZDataFactory

Gera arquivos ATDI binários (`.geo`, `.sol`, `.pal`, `.img`) para HTZ / ICS Telecom a partir de dados abertos — FABDEM, MapBiomas e Sentinel-2.

---

## Requisitos

- Docker Desktop 4.0+ (ou Docker Engine 24+)
- 16 GB RAM, SSD recomendado
- Arquivos de dados (ver seção abaixo)

---

## Estrutura de dados necessária

O sistema precisa de dois conjuntos de dados externos que você mantém **fora** da pasta do projeto, em qualquer local da sua máquina. Você informa os caminhos no `.env`.

### FABDEM — Modelo Digital de Terreno

Uma pasta contendo:

```
<sua_pasta_fabdem>/
├── fabdem_v1_2_brasil_celulas_1x1.geojson   ← catálogo obrigatório
├── S17W048_FABDEM_V1-2.tif                  ← tiles já baixados (opcional)
├── S18W048_FABDEM_V1-2.tif
├── S20W060-S10W050_FABDEM_V1-2/             ← ou pasta de bloco extraída
│   ├── S11W050_FABDEM_V1-2.tif
│   └── ...
└── S20W060-S10W050_FABDEM_V1-2.zip          ← ou o ZIP ainda comprimido
```

- O catálogo `.geojson` é obrigatório — sem ele o sistema não sabe quais tiles existem.
- Os tiles `.tif` são opcionais na largada: o sistema **baixa automaticamente** o tile necessário na primeira vez que uma área é solicitada.
- Tiles já presentes (como arquivo `.tif`, pasta extraída ou `.zip` do bloco) são detectados automaticamente e exibidos no mapa como disponíveis.

O catálogo pode ser obtido em: https://github.com/openterrain/fabdem  
Os ZIPs de blocos: https://data.bris.ac.uk/data/dataset/s5hqmjcdj8yo2ibzi9b4ew3sn (University of Bristol)

### MapBiomas — Mapa de Cobertura da Terra

Uma pasta contendo o arquivo TIF (~5.5 GB):

```
<sua_pasta_mapbiomas>/
└── mapbiomas_10m_collection2_integration_v1-classification_2023.tif
```

- O nome do arquivo pode ser diferente — você informa o nome exato no `.env`.
- O arquivo precisa estar completo antes de subir o sistema (não é baixado automaticamente).
- Download em: https://mapbiomas.org/download → Coleção 10 → Brasil → Integração

---

## Instalação

### 1. Clonar o repositório

```bash
git clone https://github.com/seu-usuario/HTZDataFactory.git
cd HTZDataFactory
```

### 2. Criar o arquivo de configuração

```bash
# Windows
copy .env.example .env

# Linux / Mac
cp .env.example .env
```

Abra o `.env` e preencha **obrigatoriamente** os caminhos dos dados:

```env
# Pasta onde está o fabdem_v1_2_brasil_celulas_1x1.geojson e os tiles
FABDEM_DIR=C:/Dados/FABDEM

# Pasta onde está o arquivo TIF do MapBiomas
MAPBIOMAS_DIR=C:/Dados/MapBiomas

# Nome exato do arquivo TIF dentro de MAPBIOMAS_DIR
MAPBIOMAS_FILENAME=mapbiomas_10m_collection2_integration_v1-classification_2023.tif
```

As demais variáveis têm valores padrão razoáveis — ajuste conforme o seu hardware (ver seção [Performance](#performance)).

### 3. Subir o sistema

```bash
# Primeira vez — faz o build (~5–10 min dependendo da conexão)
docker compose up --build -d

# A partir da segunda vez
docker compose up -d
```

### 4. Verificar

```bash
docker compose ps
```

Todos os serviços devem estar `running`:

```
NAME                        STATUS
htzdatafactory-api-1        running
htzdatafactory-worker-1     running
htzdatafactory-redis-1      running
htzdatafactory-nginx-1      running
htzdatafactory-flower-1     running
```

```bash
curl http://localhost:8080/health
```

Resposta esperada:

```json
{ "status": "ok", "fabdem_ok": true, "mapbiomas_ok": true, "csv_ok": true }
```

Se `fabdem_ok` ou `mapbiomas_ok` vier `false`, os volumes não estão montados corretamente — confira os caminhos no `.env` e reinicie:

```bash
docker compose exec api ls /dados/fabdem/
docker compose exec api ls /dados/mapbiomas/
# Se vazio, o caminho no .env está errado
docker compose down && docker compose up -d
```

---

## Acessar

| Interface   | URL padrão                     | Descrição                       |
|-------------|--------------------------------|---------------------------------|
| Frontend    | http://localhost:8080/app      | Interface web com mapa          |
| API Docs    | http://localhost:8080/docs     | Swagger interativo              |
| Flower      | http://localhost:5555          | Monitor de workers Celery       |
| Health      | http://localhost:8080/health   | Status dos dados e serviços     |

Credenciais do Flower: `FLOWER_USER` / `FLOWER_PASSWORD` definidos no `.env`.

---

## Usar via frontend

1. Acesse http://localhost:8080/app
2. Segure **Shift** e arraste no mapa para definir a área
3. Marque as camadas desejadas: **FABDEM / .geo**, **MapBiomas / .sol**, **Imagem / .img**
4. Escolha a fonte da imagem: **Sentinel-2** (sem nuvens, online) ou **OpenStreetMap** (sempre disponível, zoom 14–16)
5. Clique **Gerar**
6. Acompanhe o log; ao concluir, baixe cada arquivo individualmente ou o ZIP completo

---

## Usar via API

```bash
# Criar job (Sentinel-2)
curl -X POST http://localhost:8080/jobs \
  -H "Content-Type: application/json" \
  -d '{"bbox": [-47.5, -16.0, -47.0, -15.5], "layers": ["geo", "sol", "img"]}'

# Criar job (OpenStreetMap zoom 15)
curl -X POST http://localhost:8080/jobs \
  -H "Content-Type: application/json" \
  -d '{"bbox": [-47.5, -16.0, -47.0, -15.5], "layers": ["geo", "sol", "img"], "img_fonte": "osm", "osm_zoom": 15}'

# Acompanhar (polling até status=done)
curl http://localhost:8080/jobs/{job_id}

# Baixar ZIP completo
curl -OJ http://localhost:8080/jobs/{job_id}/download

# Listar arquivos individuais
curl http://localhost:8080/jobs/{job_id}/files
```

Limite de bbox: **5° × 5°** por job.

---

## Converter TIFF próprio

Envie um GeoTIFF já preparado para conversão direta (sem buscar FABDEM/MapBiomas):

```bash
curl -X POST http://localhost:8080/convert \
  -F "file=@meu_terreno.tif" \
  -F "tipo=dem"
  # tipo: dem | clutter | img

# Para clutter, parâmetros extras:
# -F "formato_clutter=mapbiomas"  → reclassifica via config/mapbiomas_atdi.csv (padrão)
# -F "formato_clutter=atdi"       → usa valores 0–9 diretamente
# -F "aplicar_onion=false"        → desativa efeito cebola urbano
```

---

## Parar / reiniciar

```bash
docker compose down           # para, preserva dados em ./saidas/
docker compose down -v        # para e apaga volume Redis (fila limpa)
docker compose restart api    # reinicia só a API (ex: após editar src/)
docker compose restart worker # reinicia só o worker
```

---

## Atualizar código sem rebuild

`src/` e `frontend/` são montados como volume — edições têm efeito imediato após recarregar a página ou reiniciar o serviço. Rebuild é necessário apenas quando `Dockerfile` ou `requirements.txt` mudam:

```bash
docker compose up --build -d
```

---

## Performance

Variáveis ajustáveis no `.env`:

| Variável              | Padrão | Recomendação                              |
|-----------------------|--------|-------------------------------------------|
| `GDAL_CACHEMAX`       | 4096   | ~25% da RAM (ex: 32 GB → 8192)            |
| `MAX_CONCURRENT_JOBS` | 4      | ≤ número de cores                         |
| `WORKER_CONCURRENCY`  | 4      | ≤ número de cores                         |
| `WORKER_MEMORY_LIMIT` | 8G     | ≥ 4 GB por worker                         |

Escalar workers horizontalmente:

```bash
docker compose up --scale worker=3 -d   # 3 × WORKER_CONCURRENCY jobs em paralelo
```

---

## Tabela de reclassificação MapBiomas → ATDI

Arquivo: `config/mapbiomas_atdi.csv` — editável sem reiniciar o sistema.

| Código | Nome      | Altura típica |
|--------|-----------|---------------|
| 0      | open      | 0 m           |
| 1      | suburban  | 4 m           |
| 2      | urban 8m  | 8 m           |
| 3      | urban 15m | 15 m          |
| 4      | urban 30m | 30 m          |
| 5      | forest    | 20 m          |
| 6      | hydro     | 0 m           |
| 7      | urban 50m | 50 m          |
| 8      | wood      | 8 m           |
| 9      | roof      | 10 m          |

---

## Troubleshooting

**`fabdem_ok: false` ou `mapbiomas_ok: false`**
```bash
# Verifica o que o container enxerga
docker compose exec api ls /dados/fabdem/
docker compose exec api ls /dados/mapbiomas/
# Se vazio → FABDEM_DIR ou MAPBIOMAS_DIR no .env está errado
```

**Job fica em `pending` para sempre**
```bash
docker compose logs worker | tail -30
docker compose restart worker
```

**Sentinel-2 falha**
- Verifique conectividade com `planetarycomputer.microsoft.com`
- Use OpenStreetMap como fonte alternativa de imagem

**`.sol` abre no HTZ mas as coordenadas estão erradas**
- Confira se a área está no hemisfério sul — o false northing é subtraído automaticamente
- Revise se o bbox está em [west, south, east, north] (não invertido)

**`.sol` abre mas sem paleta de cores**
- O arquivo `.pal` deve estar na mesma pasta que o `.sol`

**Erro PROJ no log da API**
```bash
docker compose restart api
# O startup.py corrige o PROJ_DATA automaticamente — reiniciar resolve na maioria dos casos
```
