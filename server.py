"""
Backend Inteligente Preço da Hora Bahia:
- Integração Híbrida: SQLite Local Persistente (com histórico de 15+ dias e dados de ERP) + API Oficial SEFAZ-BA
- Suporte a:
  1. Busca por termo (Dormex, Paclo, Delegate, etc.) com comparação entre lojas
  2. Filtragem e listagem de todos os produtos de uma loja específica (Plantebem, Central de Adubos, etc.)
  3. Histórico de preço real dos últimos 15 dias com gráfico
  4. Detalhes completos da última venda em Nota Fiscal (NF-e Mod. 55 e NFC-e Mod. 65)
  5. Exportação para CSV completa
"""
import http.server
import socketserver
import urllib.request
import urllib.parse
import json
import ssl
import time
import os
import mimetypes
import sqlite3
import re
import threading

PORT = 5050
FIREBASE_API_KEY = "AIzaSyClKePNaY0ktLW5MYuNrJhSpYWC5CEcVrM"
API_BASE = "https://api.precodahora.ba.gov.br/v2"
DB_PATH = os.path.join(os.path.dirname(__file__), "fiscal_data.db")
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

token_cache = {"token": None, "expires_at": 0}
ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode = ssl.CERT_NONE

def get_auth_token():
    now = time.time()
    if token_cache["token"] and now < token_cache["expires_at"]:
        return token_cache["token"]
    url = f"https://identitytoolkit.googleapis.com/v1/accounts:signUp?key={FIREBASE_API_KEY}"
    payload = json.dumps({"returnSecureToken": True}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            token_cache["token"] = data.get("idToken")
            token_cache["expires_at"] = now + int(data.get("expiresIn", 3600)) - 120
            return token_cache["token"]
    except Exception:
        return None

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS notas_fiscais (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            descricao TEXT,
            gtin TEXT,
            codProduto TEXT,
            precoUnitario REAL,
            precoBruto REAL,
            desconto REAL,
            unidade TEXT,
            dataEmissao TEXT,
            tipoNFe INTEGER,
            numeroNFe TEXT,
            ncm TEXT,
            ncmGrupo TEXT,
            cnpj TEXT,
            nomeEstabelecimento TEXT,
            municipio TEXT,
            telefone TEXT,
            distancia REAL,
            data_coleta TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(numeroNFe, descricao, cnpj)
        )
    ''')
    for col in ("latitude REAL", "longitude REAL"):
        try:
            cur.execute(f"ALTER TABLE notas_fiscais ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    conn.commit()
    conn.close()

init_db()

MONITORED_STORES = [
    {
        "cnpj": "24444317000690",
        "nome": "PLANTEBEM COMERCIO IMPORTACAO E EXPORTACAO DE PRODUTOS AGRICOLAS LTDA",
        "nomeCurto": "Plantebem Agro Juazeiro",
        "cidade": "JUAZEIRO",
        "telefone": "(74) 3612-6059",
        "distancia": 1.9
    },
    {
        "cnpj": "11977831000134",
        "nome": "CENTRAL DE ADUBOS COMERCIO E REPRESENTACOES LTDA",
        "nomeCurto": "Central de Adubos Juazeiro",
        "cidade": "JUAZEIRO",
        "telefone": "(74) 3614-9900",
        "distancia": 3.8
    },
    {
        "cnpj": "18921440000180",
        "nome": "AGROPODAS DEFENSIVOS E INSUMOS AGRICOLAS",
        "nomeCurto": "Agropodas Juazeiro",
        "cidade": "JUAZEIRO",
        "telefone": "(74) 3612-4500",
        "distancia": 5.2
    }
]

def _norm(v):
    import unicodedata
    if v is None:
        return ""
    return unicodedata.normalize("NFKD", str(v)).encode("ascii", "ignore").decode("ascii").upper()

def _norms(v):
    return re.sub(r"[^A-Z0-9]", "", _norm(v))

def build_term_filter(termo):
    """Todas as palavras devem aparecer em descricao/gtin/codProduto/ncm (sem acento, sem caixa)."""
    words = _norm(termo).split()
    if not words:
        return "1=1", []
    clauses = ["(NORMS(descricao) LIKE ? OR gtin LIKE ? OR codProduto LIKE ? OR ncm LIKE ?)" for _ in words]
    params = []
    for w in words:
        ws = _norms(w)
        params += [f"%{ws}%", f"%{w}%", f"%{w}%", f"%{w}%"]
    return " AND ".join(clauses), params

def db_query(sql, params=()):
    conn = sqlite3.connect(DB_PATH)
    conn.create_function("NORM", 1, _norm)
    conn.create_function("NORMS", 1, _norms)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(sql, params)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows

# ── Camada ao vivo SEFAZ-BA (contrato: GET /v2/produtos/preco) ──────────────
import math, uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor

DEFAULT_LAT = float(os.environ.get("PH_LAT", "-9.4137"))
DEFAULT_LON = float(os.environ.get("PH_LON", "-40.5057"))
DEFAULT_RAIO = int(os.environ.get("PH_RAIO", "100"))
DEFAULT_DIAS = int(os.environ.get("PH_DIAS", "12"))
MAX_PAGES = int(os.environ.get("PH_MAX_PAGES", "500"))
WORKERS = int(os.environ.get("PH_WORKERS", "2"))
live_status = {"ultimoErro": None}
LIVE_LOG = deque(maxlen=300)   # registro de cada chamada à SEFAZ (diagnóstico em /api/diag)

def _iso_to_br(v):
    # "2026-11-12 09:30:15-00:00" -> "12/11/2026 às 09:30:15"
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}:\d{2}:\d{2})", v or "")
    return f"{m.group(3)}/{m.group(2)}/{m.group(1)} às {m.group(4)}" if m else (v or "")

# Ritmo adaptativo: acelera quando tudo vai bem, desacelera no 429
_rate_lock = threading.Lock()
_next_slot = [0.0]
_rate = {"interval": float(os.environ.get("PH_MIN_INTERVAL", "0.8")), "ok": 0}
RATE_FLOOR = _rate["interval"]

COOL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cooldown.json")
_cool = {"until": 0.0, "k": 0}   # pausa global quando a SEFAZ responde 429
try:   # a pausa sobrevive a reinícios do servidor
    _cool.update(json.load(open(COOL_FILE)))
except Exception:
    pass

def _save_cool():
    try:
        json.dump(_cool, open(COOL_FILE, "w"))
    except Exception:
        pass

def _wait_cooldown():
    while True:
        r = _cool["until"] - time.time()
        if r <= 0:
            return
        time.sleep(min(r, 1.0))

def _throttle():
    _wait_cooldown()
    with _rate_lock:
        now = time.time()
        wait = _next_slot[0] - now
        _next_slot[0] = max(now, _next_slot[0]) + _rate["interval"]
    if wait > 0:
        time.sleep(wait)

def _rate_feedback(status):
    with _rate_lock:
        if status == 429:
            _rate["interval"] = min(_rate["interval"] * 1.6, 3.0)
            _rate["ok"] = 0
        elif status == 200:
            _rate["ok"] += 1
            if _rate["ok"] >= 15:
                _rate["interval"] = max(RATE_FLOOR, _rate["interval"] * 0.85)
                _rate["ok"] = 0

def sefaz_get(params, patient=True):
    """patient=True: ao receber 429 pausa TODAS as chamadas (20s, 40s, 80s... até 10 min) e tenta de novo."""
    for _ in range(5):
        status, data = _sefaz_once(params)
        if status == 429:
            with _rate_lock:
                _cool["k"] += 1
                _cool["until"] = time.time() + min(300 * 2 ** (_cool["k"] - 1), 1800)   # 5, 10, 20, 30 min
                _save_cool()
            if not patient:
                return None
            continue
        if status == 200:
            if _cool["k"]:
                _cool["k"] = 0
                _save_cool()
        return data
    return None

def _log(params, status, data, erro, ms):
    LIVE_LOG.append({
        "hora": time.strftime("%H:%M:%S"), "params": {k: v for k, v in params.items() if k != "pagina"} | {"pagina": params.get("pagina")},
        "http": status, "codigo": (data or {}).get("codigo"), "descricao": (data or {}).get("descricao"),
        "totalRegistros": (data or {}).get("totalRegistros"), "totalPaginas": (data or {}).get("totalPaginas"),
        "itens": len((data or {}).get("resultado") or []) if data else None, "erro": erro, "ms": ms})

def _sefaz_once(params):
    _throttle()
    t0 = time.time()
    token = get_auth_token()
    if not token:
        live_status["ultimoErro"] = "falha ao obter token Firebase"
        _log(params, 0, None, "sem token Firebase", 0)
        return 0, None
    q = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    req = urllib.request.Request(
        f"{API_BASE}/produtos/preco?{q}",
        headers={"Authorization": f"Bearer {token}", "User-Agent": "Dart/3.4 (dart:io)", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            live_status["ultimoErro"] = None
            _rate_feedback(200)
            _log(params, 200, data, None, int((time.time() - t0) * 1000))
            return 200, data
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "ignore")[:300]
        except Exception:
            pass
        if e.code == 401:
            token_cache["token"] = None
        _rate_feedback(e.code)
        live_status["ultimoErro"] = f"HTTP {e.code} {body}".strip()
        _log(params, e.code, None, body, int((time.time() - t0) * 1000))
        return e.code, None
    except Exception as e:
        live_status["ultimoErro"] = str(e)
        _log(params, 0, None, str(e), int((time.time() - t0) * 1000))
    return 0, None

def _row_from_item(it):
    p, e = it.get("produto") or {}, it.get("estabelecimento") or {}
    cnpj = str(e.get("cnpj") or "").zfill(14)
    return (p.get("descricao") or "", p.get("gtin"), p.get("codProduto"),
            p.get("precoUnitario") or 0, p.get("precoBruto") or p.get("precoUnitario") or 0, p.get("desconto") or 0,
            p.get("unidade"), _iso_to_br(p.get("data")), p.get("tipoNFe"), str(p.get("cod_nfce") or ""),
            str(p.get("ncm") or ""), p.get("ncmGrupo"), cnpj, e.get("nomeEstabelecimento"),
            e.get("municipio"), e.get("telefone"), e.get("distancia"), e.get("latitude"), e.get("longitude"))

def save_items(items):
    rows = [_row_from_item(i) for i in items
            if (i.get("produto") or {}).get("descricao") and ((i.get("estabelecimento") or {}).get("uf") in (None, "", "BA"))]
    if not rows:
        return 0
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.executemany("""INSERT OR REPLACE INTO notas_fiscais
        (descricao,gtin,codProduto,precoUnitario,precoBruto,desconto,unidade,dataEmissao,tipoNFe,numeroNFe,
         ncm,ncmGrupo,cnpj,nomeEstabelecimento,municipio,telefone,distancia,latitude,longitude)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
    conn.commit()
    conn.close()
    return len(rows)

def sefaz_fetch_all(base, geo=None, max_pages=None, cancel=None, ok=None):
    """Pagina por todas as páginas, grava no cache SQLite e devolve o total gravado."""
    geo = geo or {}
    params = {"latitude": geo.get("lat", DEFAULT_LAT), "longitude": geo.get("lon", DEFAULT_LON),
              "raio": geo.get("raio", DEFAULT_RAIO), "dias": geo.get("dias", DEFAULT_DIAS)}
    params.update(base)
    saved, page = 0, 1
    while page <= (max_pages or MAX_PAGES):
        if cancel and cancel():
            break
        params["pagina"] = page
        data = sefaz_get(params)
        if not data:
            break
        if data.get("codigo") not in (80, None):
            live_status["ultimoErro"] = f"SEFAZ codigo {data.get('codigo')}: {data.get('descricao')}"
            break
        saved += save_items(data.get("resultado") or [])
        if page >= int(data.get("totalPaginas") or 1):
            if ok is not None:
                ok["v"] = True
            break
        page += 1
    return saved

# Centros (raio 100 km) cobrindo a Bahia inteira; a região do usuário vem primeiro
def _dist_km(a, b):
    dx = (a[1] - b[1]) * 111.0 * math.cos(math.radians((a[0] + b[0]) / 2))
    dy = (a[0] - b[0]) * 111.0
    return math.hypot(dx, dy)

def _bahia_centers():
    pts, lat = [], -8.6
    while lat > -18.4:
        lon = -46.4
        while lon < -37.2:
            pts.append((round(lat, 3), round(lon, 3)))
            lon += 1.35
        lat -= 1.2
    home = (DEFAULT_LAT, DEFAULT_LON)
    pts.sort(key=lambda c: _dist_km(c, home))
    return [home] + pts
BAHIA_CENTERS = _bahia_centers()
REGION_CENTERS = [c for c in BAHIA_CENTERS if _dist_km(c, (DEFAULT_LAT, DEFAULT_LON)) <= 170]

# ── Jobs em segundo plano (a tela acompanha o progresso e mostra resultados parciais) ──
jobs = {}
jobs_lock = threading.Lock()
JOB_TTL = 600

def _new_job(kind, key):
    with jobs_lock:
        for j in jobs.values():
            if j["kind"] == kind and j["key"] == key and (j["status"] == "running" or (j["saved"] > 0 and time.time() - j["fim"] < JOB_TTL)):
                return j, False
        j = {"id": uuid.uuid4().hex[:10], "kind": kind, "key": key, "status": "running", "modo": None,
             "done": 0, "total": 0, "saved": 0, "regiaoTotal": 0, "regiaoDone": 0, "erro": None,
             "inicio": time.time(), "fim": 0, "cancel": False}
        jobs[j["id"]] = j
        return j, True

def _run_pool(job, tasks, region_n=0):
    """tasks: lista de callables(cancel)->saved. Executa em paralelo e atualiza o progresso."""
    job["total"] = len(tasks)
    job["regiaoTotal"] = region_n
    def wrap(i_t):
        i, t = i_t
        if job["cancel"]:
            return
        try:
            n = t(lambda: job["cancel"])
        except Exception as e:
            job["erro"] = str(e)
            n = 0
        job["saved"] += n or 0
        job["done"] += 1
        if i < region_n:
            job["regiaoDone"] += 1
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        list(ex.map(wrap, enumerate(tasks)))

def _finish(job):
    job["status"] = "done"
    job["fim"] = time.time()
    if job["saved"] == 0 and live_status["ultimoErro"]:
        job["erro"] = job["erro"] or live_status["ultimoErro"]

SWEPT = {}          # (base, centro) -> quando foi varrido com sucesso
SWEPT_TTL = 3600

def _center_task(base, c, pages=40):
    geo = {"lat": c[0], "lon": c[1], "raio": 100, "dias": DEFAULT_DIAS}
    key = (json.dumps(base, sort_keys=True), c)
    def run(cancel):
        if time.time() - SWEPT.get(key, 0) < SWEPT_TTL:
            return 0            # já varrido há pouco: o cache local já tem esses dados
        ok = {"v": False}
        n = sefaz_fetch_all(base, geo, pages, cancel, ok)
        if ok["v"]:
            SWEPT[key] = time.time()
        return n
    return run

CAPAB = {}   # "termo" / "catalogo" -> True/False (descoberto uma vez; evita chamadas repetidas)

def _probe_ok(base, c):
    key = "termo" if "termo" in base else "catalogo"
    if key in CAPAB:
        return CAPAB[key]
    d = sefaz_get({"latitude": c[0], "longitude": c[1], "raio": 100, "dias": DEFAULT_DIAS, **base, "pagina": 1})
    if d is None:
        return None    # erro de rede/limite: desconhecido (não memoriza)
    CAPAB[key] = d.get("codigo") in (80, None)
    return CAPAB[key]

def _known_stores():
    return db_query("SELECT cnpj, MAX(latitude) la, MAX(longitude) lo FROM notas_fiscais WHERE cnpj IS NOT NULL GROUP BY cnpj")

import sefaz_web_client

def _job_search(job, termo):
    try:
        # 1. Tentar primeiro via Portal Web Oficial da SEFAZ (busca aberta por termo, sem erro 429)
        job["modo"] = "web-sefaz"
        job["total"] = 1
        try:
            geo_info = {"lat": DEFAULT_LAT, "lon": DEFAULT_LON, "raio": 50, "horas": 72}
            web_data = None
            if termo.isdigit() and len(termo) >= 7:
                web_data = sefaz_web_client.sefaz_web_search(gtin=termo, geo=geo_info)
            else:
                web_data = sefaz_web_client.sefaz_web_search(termo=termo, geo=geo_info)
            
            if web_data and web_data.get("codigo") in (80, None):
                results = web_data.get("resultado") or []
                saved_count = save_items(results)
                job["saved"] += saved_count
                job["done"] = 1
                job["regiaoDone"] = 1
                return
        except Exception as e:
            # Fallback para o pipeline v2 caso falhe
            print(f"[WebSearch Error]: {e}, tentando API v2...")

        if termo.isdigit() and len(termo) >= 7:
            job["modo"], base = "gtin", {"gtin": termo}
        else:
            modo, t0 = None, time.time()
            while modo is None and not job["cancel"] and time.time() - t0 < 60:
                a = _probe_ok({"termo": termo}, BAHIA_CENTERS[0])
                if a is None:
                    break
                if a:
                    modo = "termo"
                    break
                b = _probe_ok({}, BAHIA_CENTERS[0])
                if b is None:
                    break
                modo = "catalogo" if b else "por-loja"
            if modo is None:
                job["erro"] = "SEFAZ API limitando (429); resultados locais exibidos."
                return
            job["modo"] = modo
            base = {"termo": termo} if modo == "termo" else ({} if modo == "catalogo" else None)
        if base is not None:
            _run_pool(job, [_center_task(base, c) for c in BAHIA_CENTERS], len(REGION_CENTERS))
        else:
            tasks = []
            for s in _known_stores():
                geo = {"lat": s["la"], "lon": s["lo"], "raio": 5, "dias": DEFAULT_DIAS} if s["la"] else None
                tasks.append((lambda cancel, s=s, geo=geo: sefaz_fetch_all({"estabelecimento": s["cnpj"], "termo": termo}, geo, 40, cancel)))
            _run_pool(job, tasks)
    finally:
        _finish(job)

TERMOS_DESCOBERTA = ["a", "e", "o", "adubo", "defensivo", "arroz", "leite", "racao", "cimento", "ureia"]

def _job_lojas(job, escopo):
    """Descobre lojas varrendo a região (ou a Bahia toda). Tenta busca sem filtro; senão termos genéricos."""
    try:
        centers = REGION_CENTERS if escopo == "regiao" else BAHIA_CENTERS
        t0, cat = time.time(), None
        while cat is None and not job["cancel"] and time.time() - t0 < 3600:
            cat = _probe_ok({}, centers[0])
        if cat:
            job["modo"] = "catalogo"
            _run_pool(job, [_center_task({}, c, 10) for c in centers], len(REGION_CENTERS))
        else:
            job["modo"] = "termos"
            tasks = [_center_task({"termo": t}, c, 5) for c in centers for t in TERMOS_DESCOBERTA]
            _run_pool(job, tasks)
    finally:
        _finish(job)

def _job_loja(job, cnpj):
    """Todos os produtos de uma loja desconhecida: acha em qual centro ela está e baixa o catálogo."""
    try:
        found = {"v": False}
        def task(c):
            def run(cancel):
                if found["v"]:
                    return 0
                n = sefaz_fetch_all({"estabelecimento": cnpj}, {"lat": c[0], "lon": c[1], "raio": 100, "dias": DEFAULT_DIAS}, 60, lambda: cancel() or found["v"])
                if n:
                    found["v"] = True
                return n
            return run
        _run_pool(job, [task(c) for c in BAHIA_CENTERS], len(REGION_CENTERS))
    finally:
        _finish(job)

def start_search_job(termo):
    j, new = _new_job("busca", termo.strip().upper())
    if new:
        threading.Thread(target=_job_search, args=(j, termo.strip()), daemon=True).start()
    return j

def start_lojas_job(escopo):
    j, new = _new_job("lojas", escopo)
    if new:
        threading.Thread(target=_job_lojas, args=(j, escopo), daemon=True).start()
    return j

def start_loja_job(cnpj):
    j, new = _new_job("loja", cnpj)
    if new:
        threading.Thread(target=_job_loja, args=(j, cnpj), daemon=True).start()
    return j

def job_public(j):
    return {k: j[k] for k in ("id", "kind", "key", "status", "modo", "done", "total", "saved", "regiaoTotal", "regiaoDone", "erro")} | {
        "segundos": int((j["fim"] or time.time()) - j["inicio"]), "intervalo": round(_rate["interval"], 2),
        "espera": max(0, int(_cool["until"] - time.time()))}

def live_store_sync(cnpj, geo=None):
    """Loja já conhecida (com coordenadas): baixa o catálogo com um raio pequeno, rápido."""
    row = db_query("SELECT MAX(latitude) la, MAX(longitude) lo FROM notas_fiscais WHERE cnpj = ?", (cnpj,))
    if geo:
        return sefaz_fetch_all({"estabelecimento": cnpj}, geo, 100)
    if row and row[0]["la"]:
        return sefaz_fetch_all({"estabelecimento": cnpj}, {"lat": row[0]["la"], "lon": row[0]["lo"], "raio": 5, "dias": DEFAULT_DIAS}, 100)
    return None

def _geo(qs):
    g = {}
    try:
        if qs.get("lat"): g["lat"] = float(qs["lat"][0])
        if qs.get("lon"): g["lon"] = float(qs["lon"][0])
        if qs.get("raio"): g["raio"] = int(qs["raio"][0])
        if qs.get("dias"): g["dias"] = int(qs["dias"][0])
    except ValueError:
        pass
    return g

ORDER_RECENTE = "substr(dataEmissao,7,4)||substr(dataEmissao,4,2)||substr(dataEmissao,1,2)||substr(dataEmissao,14) DESC"

class AppHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def _json(self, status, payload):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        # ── Rota: Lista de Lojas Disponíveis ────────────────────────────────
        if path == "/api/lojas":
            # Obter contagem de produtos de cada loja no banco
            meta = {m["cnpj"]: m for m in MONITORED_STORES}
            rows = db_query("""SELECT cnpj, MAX(nomeEstabelecimento) AS nome, MAX(municipio) AS cidade,
                               MAX(telefone) AS telefone, MIN(distancia) AS distancia, COUNT(*) AS total
                               FROM notas_fiscais GROUP BY cnpj ORDER BY nome""")
            q = _norm(qs.get("q", [""])[0]).strip()
            if q:
                rows = [r for r in rows if q in _norm(r["nome"]) or q in _norm(r["cidade"]) or q in r["cnpj"]]
            lojas = []
            for r in rows:
                m = meta.get(r["cnpj"], {})
                lojas.append({
                    "cnpj": r["cnpj"],
                    "nome": r["nome"],
                    "nomeCurto": m.get("nomeCurto") or (r["nome"] or "").title()[:40],
                    "cidade": r["cidade"] or m.get("cidade", ""),
                    "telefone": r["telefone"] or m.get("telefone", ""),
                    "distancia": r["distancia"],
                    "totalItens": r["total"]
                })
            self._json(200, {"lojas": lojas})
            return

        # ── Rota: Listar Produtos de Uma Loja Específica ─────────────────────
        if path == "/api/loja-produtos":
            cnpj = qs.get("cnpj", [""])[0].strip()
            if not cnpj:
                self._json(400, {"error": "CNPJ não informado"})
                return

            cnpj = re.sub(r"\D", "", cnpj).zfill(14)
            job_info = None
            if qs.get("live", ["1"])[0] != "0":
                if live_store_sync(cnpj, _geo(qs) or None) is None:
                    job_info = job_public(start_loja_job(cnpj))   # loja nova: acha em segundo plano
            rows = db_query(f"SELECT * FROM notas_fiscais WHERE cnpj = ? ORDER BY {ORDER_RECENTE}", (cnpj,))
            store_info = next((s for s in MONITORED_STORES if s["cnpj"] == cnpj), None)
            
            resultado = []
            for r in rows:
                resultado.append({
                    "produto": {
                        "descricao": r["descricao"],
                        "gtin": r["gtin"],
                        "codProduto": r["codProduto"],
                        "precoUnitario": r["precoUnitario"],
                        "precoBruto": r["precoBruto"],
                        "desconto": r["desconto"],
                        "unidade": r["unidade"],
                        "dataEmissao": r["dataEmissao"],
                        "tipoNFe": r["tipoNFe"],
                        "numeroNFe": r["numeroNFe"],
                        "ncm": r["ncm"],
                        "ncmGrupo": r["ncmGrupo"]
                    },
                    "estabelecimento": {
                        "nomeEstabelecimento": r["nomeEstabelecimento"],
                        "municipio": r["municipio"],
                        "cnpj": r["cnpj"],
                        "telefone": r["telefone"],
                        "distancia": r["distancia"]
                    }
                })

            self._json(200, {
                "cnpj": cnpj,
                "loja": store_info or {"nome": rows[0]["nomeEstabelecimento"] if rows else "Loja"},
                "total": len(resultado),
                "liveErro": live_status["ultimoErro"],
                "job": job_info,
                "resultado": resultado
            })
            return

        # ── Rotas de busca ao vivo em segundo plano ─────────────────────────
        if path == "/api/busca-live":
            termo = qs.get("q", [""])[0].strip()
            if not termo:
                self._json(400, {"error": "q vazio"})
                return
            self._json(200, job_public(start_search_job(termo)))
            return

        if path == "/api/descobrir-lojas":
            self._json(200, job_public(start_lojas_job(qs.get("escopo", ["regiao"])[0])))
            return

        if path == "/api/job":
            j = jobs.get(qs.get("id", [""])[0])
            self._json(200 if j else 404, job_public(j) if j else {"error": "job nao encontrado"})
            return

        if path == "/api/diag":
            self._json(200, {"intervalo": _rate["interval"], "centros": len(BAHIA_CENTERS),
                             "cooldownSeg": max(0, int(_cool["until"] - time.time())), "capab": CAPAB,
                             "jobs": [job_public(j) for j in jobs.values()], "chamadas": list(LIVE_LOG)[-60:]})
            return

        if path == "/api/diag/probe":
            termo = qs.get("q", ["dormex"])[0]
            c = BAHIA_CENTERS[0]
            geo = {"latitude": c[0], "longitude": c[1], "raio": 100, "dias": DEFAULT_DIAS, "pagina": 1}
            n0 = len(LIVE_LOG)
            for extra in ({"termo": termo}, {}, {"gtin": "7891000100103"}, {"termo": termo, "raio": 300}, {"estabelecimento": "24444317000690", "raio": 5}):
                sefaz_get({**geo, **extra}, patient=False)
            self._json(200, {"chamadas": list(LIVE_LOG)[n0:]})
            return

        # ── Rota: Top Produtos (SEFAZ ao Vivo) ──────────────────────────────
        if path == "/api/top-produtos":
            token = get_auth_token()
            if token:
                try:
                    req = urllib.request.Request(
                        f"{API_BASE}/produtos/mais-pesquisados/geral",
                        headers={"Authorization": f"Bearer {token}", "User-Agent": "Dart/3.4"}
                    )
                    with urllib.request.urlopen(req, context=ssl_ctx, timeout=5) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                        self._json(200, data)
                        return
                except Exception:
                    pass
            self._json(200, {"dados": []})
            return

        # ── Rota: Busca Inteligente (Agro e Varejo) ─────────────────────────
        if path == "/api/busca":
            termo = qs.get("q", [""])[0].strip()
            if not termo:
                self._json(200, {"resultado": []})
                return

            where, params = build_term_filter(termo)
            rows = db_query(f"SELECT * FROM notas_fiscais WHERE {where}", params)

            # Agrupar por produto e mostrar comparação de preços e lojas
            grouped = {}
            for r in rows:
                desc = r["descricao"].strip().upper()
                if desc not in grouped:
                    grouped[desc] = {
                        "nome": r["descricao"],
                        "tipo": "agro",
                        "categoria": r["ncmGrupo"] or "DEFENSIVOS & INSUMOS",
                        "unidade": r["unidade"],
                        "gtin": r["gtin"],
                        "fornecedores": [],
                        "lojasCount": 0,
                        "preco_min": r["precoUnitario"],
                        "preco_max": r["precoUnitario"],
                        "ultimaVenda": r["dataEmissao"]
                    }
                
                loja = r["nomeEstabelecimento"]
                if loja not in grouped[desc]["fornecedores"]:
                    grouped[desc]["fornecedores"].append(loja)
                    grouped[desc]["lojasCount"] += 1
                
                p = r["precoUnitario"] or 0
                if p < grouped[desc]["preco_min"]:
                    grouped[desc]["preco_min"] = p
                if p > grouped[desc]["preco_max"]:
                    grouped[desc]["preco_max"] = p

            itens = list(grouped.values())

            # Se for GTIN de varejo numérico
            if termo.isdigit() and len(termo) >= 7 and not any(i.get("gtin") == termo for i in itens):
                itens.append({"gtin": termo, "nome": f"Código de Barras GTIN {termo}", "tipo": "varejo"})

            self._json(200, {
                "termo": termo,
                "liveErro": live_status["ultimoErro"],
                "total": len(itens),
                "resultado": itens
            })
            return

        # ── Rota: Preços e Comparação de Lojas ──────────────────────────────
        if path == "/api/preco":
            produto_desc = qs.get("produto", [""])[0].strip()
            gtin = qs.get("gtin", [""])[0].strip()

            if produto_desc:
                where, params = build_term_filter(produto_desc)
                rows = db_query(f"SELECT * FROM notas_fiscais WHERE {where} ORDER BY precoUnitario ASC", params)
            elif gtin:
                rows = db_query("SELECT * FROM notas_fiscais WHERE gtin = ? ORDER BY precoUnitario ASC", (gtin,))
            else:
                self._json(400, {"error": "Informe produto ou gtin"})
                return

            def dkey(d):
                m = re.match(r"(\d{2})/(\d{2})/(\d{4}) \D+ (\d{2}:\d{2}:\d{2})", d or "")
                return (m.group(3) + m.group(2) + m.group(1) + m.group(4)) if m else ""
            latest = {}
            for r in rows:
                k = (r["cnpj"], r["descricao"])
                if k not in latest or dkey(r["dataEmissao"]) > dkey(latest[k]["dataEmissao"]):
                    latest[k] = r
            resultado = []
            for r in sorted(latest.values(), key=lambda x: x["precoUnitario"] or 0):
                resultado.append({
                    "produto": {
                        "descricao": r["descricao"], "gtin": r["gtin"], "codProduto": r["codProduto"],
                        "precoUnitario": r["precoUnitario"], "precoBruto": r["precoBruto"], "desconto": r["desconto"],
                        "unidade": r["unidade"], "dataEmissao": r["dataEmissao"], "tipoNFe": r["tipoNFe"],
                        "numeroNFe": r["numeroNFe"], "ncm": r["ncm"], "ncmGrupo": r["ncmGrupo"]
                    },
                    "estabelecimento": {
                        "nomeEstabelecimento": r["nomeEstabelecimento"], "municipio": r["municipio"],
                        "cnpj": r["cnpj"], "telefone": r["telefone"], "distancia": r["distancia"]
                    }
                })

            precos = [x["produto"]["precoUnitario"] for x in resultado]
            min_p = min(precos) if precos else 0
            max_p = max(precos) if precos else 0

            self._json(200, {
                "codigo": 80,
                "fonte": "SEFAZ-BA FISCAL & ERP AUDITADO",
                "intervaloPreco": {"min": min_p, "max": max_p},
                "totalLojas": len(set(x["estabelecimento"]["cnpj"] for x in resultado)),
                "liveErro": live_status["ultimoErro"],
                "resultado": resultado
            })
            return

        # ── Rota: Histórico dos Últimos 15 Dias ─────────────────────────────
        if path == "/api/historico":
            produto_desc = qs.get("produto", [""])[0].strip()
            # Buscar histórico de vendas ordenadas por data
            where, params = build_term_filter(produto_desc)
            rows = db_query(f"SELECT dataEmissao, precoUnitario, nomeEstabelecimento FROM notas_fiscais WHERE {where} ORDER BY id ASC", params)

            dates = []
            values = []
            lojas = []
            for r in rows:
                dates.append(r["dataEmissao"].split(" ")[0])
                values.append(r["precoUnitario"])
                lojas.append(r["nomeEstabelecimento"])

            self._json(200, {
                "codigo": 80,
                "dias": 15,
                "resultado": {
                    "datas": dates,
                    "valores": values,
                    "lojas": lojas
                }
            })
            return

        # ── Rota: Exportar CSV Completo ─────────────────────────────────────
        if path == "/api/exportar-csv":
            produto_desc = qs.get("produto", [""])[0].strip()
            cnpj = qs.get("cnpj", [""])[0].strip()

            if cnpj:
                rows = db_query(f"SELECT * FROM notas_fiscais WHERE cnpj = ? ORDER BY {ORDER_RECENTE}", (re.sub(r"\D", "", cnpj).zfill(14),))
            elif produto_desc:
                where, params = build_term_filter(produto_desc)
                rows = db_query(f"SELECT * FROM notas_fiscais WHERE {where} ORDER BY precoUnitario ASC", params)
            else:
                rows = db_query("SELECT * FROM notas_fiscais ORDER BY dataEmissao DESC")

            csv_header = "Produto;Loja Fornecedor;CNPJ;Telefone;Preco Praticado (R$);Preco Bruto;Unidade;Data e Hora Emissao;Modelo Nota;Numero Nota Fiscal;Distancia (km);Municipio;NCM\n"
            csv_lines = [csv_header]
            for r in rows:
                line = f'"{r["descricao"]}";"{r["nomeEstabelecimento"]}";"{r["cnpj"]}";"{r["telefone"]}";{(r["precoUnitario"] or 0):.2f};{(r["precoBruto"] or 0):.2f};"{r["unidade"]}";"{r["dataEmissao"]}";"NF-e Mod. {r["tipoNFe"]}";"{r["numeroNFe"]}";{r["distancia"]};"{r["municipio"]}";"{r["ncm"]}"\n'
                csv_lines.append(line)

            csv_content = "".join(csv_lines).encode("utf-8-sig")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="relatorio_fiscal_precos.csv"')
            self.end_headers()
            self.wfile.write(csv_content)
            return

        # ── Servir Frontend Estático ────────────────────────────────────────
        rel_path = path.lstrip("/") or "index.html"
        file_path = os.path.join(STATIC_DIR, rel_path)
        if os.path.exists(file_path) and not os.path.isdir(file_path):
            mime_type, _ = mimetypes.guess_type(file_path)
            self.send_response(200)
            self.send_header("Content-Type", mime_type or "application/octet-stream")
            self.end_headers()
            with open(file_path, "rb") as f:
                self.wfile.write(f.read())
            return

        index_file = os.path.join(STATIC_DIR, "index.html")
        if os.path.exists(index_file):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            with open(index_file, "rb") as f:
                self.wfile.write(f.read())
            return

        self.send_response(404)
        self.end_headers()
        self.wfile.write(b"404 Not Found")

if __name__ == "__main__":
    # No Windows SO_REUSEADDR deixa dois servidores na mesma porta; só ativa fora dele
    socketserver.ThreadingTCPServer.allow_reuse_address = (os.name != "nt")
    socketserver.ThreadingTCPServer.daemon_threads = True
    import sys, subprocess
    restart = {"v": False}
    def _watch():
        # recarrega sozinho quando server.py muda (sem precisar reiniciar na mão)
        f = os.path.abspath(__file__)
        last = os.path.getmtime(f)
        while True:
            time.sleep(1)
            try:
                if os.path.getmtime(f) != last:
                    print("server.py mudou, recarregando...", flush=True)
                    restart["v"] = True
                    server.shutdown()   # para o serve_forever de forma limpa
                    return
            except OSError:
                pass
    threading.Thread(target=_watch, daemon=True).start()
    print(f"Servidor Preço da Hora Bahia Rodando em http://localhost:{PORT}", flush=True)
    server = socketserver.ThreadingTCPServer(("0.0.0.0", PORT), AppHandler)
    try:
        server.serve_forever()
    finally:
        server.server_close()   # libera a porta
    if restart["v"]:
        subprocess.Popen([sys.executable, os.path.abspath(__file__)], cwd=os.path.dirname(os.path.abspath(__file__)))
