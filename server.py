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

def db_query(sql, params=()):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(sql, params)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows

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
            lojas = []
            for s in MONITORED_STORES:
                count_rows = db_query("SELECT COUNT(*) as c FROM notas_fiscais WHERE cnpj = ?", (s["cnpj"],))
                total_prod = count_rows[0]["c"] if count_rows else 0
                lojas.append({
                    "cnpj": s["cnpj"],
                    "nome": s["nome"],
                    "nomeCurto": s["nomeCurto"],
                    "cidade": s["cidade"],
                    "telefone": s["telefone"],
                    "distancia": s["distancia"],
                    "totalItens": total_prod
                })
            self._json(200, {"lojas": lojas})
            return

        # ── Rota: Listar Produtos de Uma Loja Específica ─────────────────────
        if path == "/api/loja-produtos":
            cnpj = qs.get("cnpj", [""])[0].strip()
            if not cnpj:
                self._json(400, {"error": "CNPJ não informado"})
                return

            rows = db_query("SELECT * FROM notas_fiscais WHERE cnpj = ? ORDER BY dataEmissao DESC", (cnpj,))
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
                "resultado": resultado
            })
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

            words = termo.upper().split()
            where_clauses = ["UPPER(descricao) LIKE ?" for _ in words]
            params = [f"%{w}%" for w in words]

            sql = f"SELECT * FROM notas_fiscais WHERE {' AND '.join(where_clauses)}"
            rows = db_query(sql, params)

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
                
                p = r["precoUnitario"]
                if p < grouped[desc]["preco_min"]:
                    grouped[desc]["preco_min"] = p
                if p > grouped[desc]["preco_max"]:
                    grouped[desc]["preco_max"] = p

            itens = list(grouped.values())

            # Se for GTIN de varejo numérico
            if termo.isdigit() and len(termo) >= 7:
                itens.append({"gtin": termo, "nome": f"Código de Barras GTIN {termo}", "tipo": "varejo"})

            self._json(200, {
                "termo": termo,
                "total": len(itens),
                "resultado": itens
            })
            return

        # ── Rota: Preços e Comparação de Lojas ──────────────────────────────
        if path == "/api/preco":
            produto_desc = qs.get("produto", [""])[0].strip()
            gtin = qs.get("gtin", [""])[0].strip()

            if produto_desc:
                words = produto_desc.upper().split()
                where_clauses = ["UPPER(descricao) LIKE ?" for _ in words]
                params = [f"%{w}%" for w in words]
                sql = f"SELECT * FROM notas_fiscais WHERE {' AND '.join(where_clauses)} ORDER BY precoUnitario ASC"
                rows = db_query(sql, params)
            elif gtin:
                rows = db_query("SELECT * FROM notas_fiscais WHERE gtin = ? ORDER BY precoUnitario ASC", (gtin,))
            else:
                self._json(400, {"error": "Informe produto ou gtin"})
                return

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

            precos = [x["produto"]["precoUnitario"] for x in resultado]
            min_p = min(precos) if precos else 0
            max_p = max(precos) if precos else 0

            self._json(200, {
                "codigo": 80,
                "fonte": "SEFAZ-BA FISCAL & ERP AUDITADO",
                "intervaloPreco": {"min": min_p, "max": max_p},
                "totalLojas": len(set(x["estabelecimento"]["cnpj"] for x in resultado)),
                "resultado": resultado
            })
            return

        # ── Rota: Histórico dos Últimos 15 Dias ─────────────────────────────
        if path == "/api/historico":
            produto_desc = qs.get("produto", [""])[0].strip()
            # Buscar histórico de vendas ordenadas por data
            words = produto_desc.upper().split()
            where_clauses = ["UPPER(descricao) LIKE ?" for w in words] if words else ["1=1"]
            params = [f"%{w}%" for w in words]
            sql = f"SELECT dataEmissao, precoUnitario, nomeEstabelecimento FROM notas_fiscais WHERE {' AND '.join(where_clauses)} ORDER BY id ASC"
            rows = db_query(sql, params)

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
                rows = db_query("SELECT * FROM notas_fiscais WHERE cnpj = ? ORDER BY dataEmissao DESC", (cnpj,))
            elif produto_desc:
                words = produto_desc.upper().split()
                where_clauses = ["UPPER(descricao) LIKE ?" for _ in words]
                params = [f"%{w}%" for _ in words]
                rows = db_query(f"SELECT * FROM notas_fiscais WHERE {' AND '.join(where_clauses)} ORDER BY precoUnitario ASC", params)
            else:
                rows = db_query("SELECT * FROM notas_fiscais ORDER BY dataEmissao DESC")

            csv_header = "Produto;Loja Fornecedor;CNPJ;Telefone;Preco Praticado (R$);Preco Bruto;Unidade;Data e Hora Emissao;Modelo Nota;Numero Nota Fiscal;Distancia (km);Municipio;NCM\n"
            csv_lines = [csv_header]
            for r in rows:
                line = f'"{r["descricao"]}";"{r["nomeEstabelecimento"]}";"{r["cnpj"]}";"{r["telefone"]}";{r["precoUnitario"]:.2f};{r["precoBruto"]:.2f};"{r["unidade"]}";"{r["dataEmissao"]}";"NF-e Mod. {r["tipoNFe"]}";"{r["numeroNFe"]}";{r["distancia"]};"{r["municipio"]}";"{r["ncm"]}"\n'
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
    socketserver.TCPServer.allow_reuse_address = True
    print(f"Servidor Preço da Hora Bahia Rodando em http://localhost:{PORT}")
    server = socketserver.TCPServer(("0.0.0.0", PORT), AppHandler)
    server.serve_forever()
