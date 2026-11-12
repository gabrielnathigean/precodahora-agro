import urllib.request, ssl, urllib.parse, re, json, time, threading

# Cache do token CSRF e cookies da web
_web_session = {
    "cookie": None,
    "csrf_token": None,
    "expires_at": 0
}
_web_lock = threading.Lock()

def get_web_session():
    now = time.time()
    with _web_lock:
        if _web_session["cookie"] and _web_session["csrf_token"] and now < _web_session["expires_at"]:
            return _web_session["cookie"], _web_session["csrf_token"]
        
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        
        url = "https://precodahora.ba.gov.br/produtos/"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "pt-BR,pt;q=0.9"
        }
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, context=ctx, timeout=12) as resp:
            set_cookies = resp.headers.get_all("Set-Cookie") or []
            cookie_header = "; ".join([c.split(";")[0] for c in set_cookies])
            html = resp.read().decode("utf-8", errors="ignore")
            m = re.search(r'name=["\']csrf_token["\']\s+type=["\']hidden["\']\s+value=["\']([^"\']+)["\']', html)
            if not m:
                raise ValueError("CSRF token nao encontrado no HTML do portal web da SEFAZ")
            csrf_token = m.group(1)
            
            _web_session["cookie"] = cookie_header
            _web_session["csrf_token"] = csrf_token
            _web_session["expires_at"] = now + 1800 # 30 min
            return cookie_header, csrf_token

def sefaz_web_search(termo=None, gtin=None, cnpj=None, geo=None):
    """
    Consulta o portal web oficial (https://precodahora.ba.gov.br/produtos/)
    que possui busca por termo aberta sem limite 429 restritivo da API v2!
    """
    geo = geo or {}
    lat = geo.get("lat", -9.4137)
    lon = geo.get("lon", -40.5057)
    raio = geo.get("raio", 50)
    horas = geo.get("horas", 72)
    
    cookie, csrf_token = get_web_session()
    
    post_url = "https://precodahora.ba.gov.br/produtos/"
    post_data = {
        "pagina": 1,
        "ordenar": "preco.asc",
        "raio": raio,
        "horas": horas,
        "latitude": lat,
        "longitude": lon
    }
    if termo:
        post_data["termo"] = termo
    if gtin:
        post_data["gtin"] = gtin
    if cnpj:
        post_data["cnpj"] = cnpj
        
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "X-CSRFToken": csrf_token,
        "Referer": "https://precodahora.ba.gov.br/produtos/",
        "Cookie": cookie
    }
    
    data_bytes = urllib.parse.urlencode(post_data).encode("utf-8")
    req = urllib.request.Request(post_url, data=data_bytes, headers=headers)
    
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data
    except urllib.error.HTTPError as e:
        if e.code in (400, 403):
            # Token expirado, limpa cache e tenta 1x mais
            with _web_lock:
                _web_session["expires_at"] = 0
            cookie, csrf_token = get_web_session()
            headers["X-CSRFToken"] = csrf_token
            headers["Cookie"] = cookie
            req = urllib.request.Request(post_url, data=data_bytes, headers=headers)
            with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))
        raise
