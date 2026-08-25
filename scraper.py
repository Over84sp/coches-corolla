#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Scraper ligero de coches.net — Toyota Corolla Touring Sports 2022+ · 140 CV+

- 1 listado por ejecución (hasta MAX_PAGES peticiones de 35 anuncios, con retardo).
- Filtros en servidor por URL (marca/modelo/carrocería); año y CV se filtran en local.
- Dedupeo por ID: solo muestra anuncios NUEVOS (y rebajas de precio de conocidos).
- Estado en .data/seen.json · historial acumulativo en .data/anuncios.csv
- Python 3.9+ estándar (sin dependencias externas).
"""

import csv
import datetime as dt
import gzip
import io
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# ─────────────────────────── CONFIGURACIÓN ───────────────────────────
BASE_URL = "https://www.coches.net/toyota/corolla/familiar/segunda-mano/"
# ¿Provincia? p. ej. ".../segunda-mano/barcelona/" · precio máx: ".../20000_euros/"
MIN_YEAR = 2022        # año mínimo del vehículo
MIN_HP = 140           # potencia mínima (CV): incluye 140H/180H/196H/200H,
                       # descarta los 125H (122 CV)
MAX_KM = None          # p. ej. 120000, o None para sin límite
MAX_PRICE = None       # p. ej. 25000, o None para sin límite
MAX_PAGES = 6          # páginas por tirada (35 anuncios/página) — robots.txt de
                       # coches.net desaconseja pg≥7; si el rate-limit corta antes,
                       # la tirada sigue con lo recogido (falla-rápido en pg>1)
DELAY_S = 6.0          # segundos entre peticiones + jitter (cortesía / anti rate-limit)
INV_DAYS = 14          # días que un anuncio visto permanece en el "inventario" del resumen
PRUNE_DAYS = 30        # días antes de olvidar un anuncio del estado (seen.json)
OBJETIVOS = {"140H": None, "180H": None, "200H": None}  # € objetivo de compra por
                       # modelo; None = automático (percentil 10 del mercado actual)
TIMEOUT_S = 30
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

STATE_DIR = Path(__file__).resolve().parent / ".data"
SEEN_FILE = STATE_DIR / "seen.json"
CSV_FILE = STATE_DIR / "anuncios.csv"
# ─────────────────────────────────────────────────────────────────────

RE_PROPS = re.compile(r'window\.__INITIAL_PROPS__\s*=\s*JSON\.parse\("(.*?)"\);', re.S)


def log(msg: str) -> None:
    print(msg, flush=True)


def fetch_html(url: str, retries: int = 4):
    """Descarga la página y valida que trae el JSON de datos.

    Usa curl si está disponible (su fingerprint TLS pasa los filtros anti-bot de
    coches.net; urllib recibe página de bloqueo en algunos datacenters). Si llega
    la página de bloqueo ("Ups!...") espera cada vez más y reintenta.
    Devuelve el HTML o None si no se consigue."""
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            html, status, headers = _fetch_with_curl(url) if shutil.which("curl") \
                else _fetch_with_urllib(url)
            STATE_DIR.mkdir(exist_ok=True)
            (STATE_DIR / "ultima_respuesta.html").write_text(
                f"URL: {url}\nHTTP: {status}\n{headers}\n\n{html[:3000]}", encoding="utf-8")
            if not RE_PROPS.search(html):
                raise RuntimeError("página de bloqueo anti-bot o maquetación cambiada "
                                   f"(inicio: {re.sub(r'[^ -~áéíóúñÁÉÍÓÚÑ ]', ' ', html[:150])})")
            return html
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            log(f"  [!] intento {attempt}/{retries}: {str(exc)[:120]}")
            time.sleep(15 * attempt)   # 15s, 30s, 45s, 60s — dejar pasar el rate-limit
    return None


def _fetch_with_curl(url: str):
    cmd = [shutil.which("curl"), "-sS", "--compressed", "--max-time", str(TIMEOUT_S),
           "-w", "\n@@HTTP:%{http_code}", "-A", USER_AGENT,
           "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
           "-H", "Accept-Language: es-ES,es;q=0.9,en;q=0.8",
           "-H", "Sec-Fetch-Dest: document", "-H", "Sec-Fetch-Mode: navigate",
           "-H", "Sec-Fetch-Site: none", "-H", "Sec-Fetch-User: ?1",
           "-H", "Upgrade-Insecure-Requests: 1", url]
    out = subprocess.run(cmd, capture_output=True, timeout=TIMEOUT_S + 15).stdout
    marker = out.rfind(b"\n@@HTTP:")
    status = out[marker + 8:].decode("ascii", "ignore").strip() if marker >= 0 else "?"
    html = (out[:marker] if marker >= 0 else out).decode("utf-8", "ignore")
    return html, status, "vía curl"


def _fetch_with_urllib(url: str):
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip",
        "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none", "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    })
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
        return raw.decode("utf-8", "ignore"), resp.status, ""


def parse_listings(html: str) -> dict:
    """Extrae initialResults del JSON embebido en la página."""
    m = RE_PROPS.search(html)
    if not m:
        snippet = re.sub(r"\s+", " ", html[:400])
        raise RuntimeError(
            "No se encontró window.__INITIAL_PROPS__ (¿cambio de maquetación o bloqueo anti-bot?). "
            f"Inicio del HTML recibido: {snippet}")
    data = json.loads(json.loads('"' + m.group(1) + '"'))
    return data["initialResults"]


def collect_ads() -> list:
    """Descarga hasta MAX_PAGES del listado y devuelve los anuncios crudos.
    Si una página queda bloqueada tras los reintentos, termina la tirada con lo
    recogido hasta ese momento (el estado se conserva y la próxima tirada recupera
    las novedades por dedupeo de IDs)."""
    ads, page = [], 1
    while page <= MAX_PAGES:
        url = BASE_URL + (f"?pg={page}" if page > 1 else "")
        log(f"→ Descargando página {page}: {url}")
        html = fetch_html(url, retries=4 if page == 1 else 1)
        if html is None:
            if page == 1:
                log("  [!] La primera página no está accesible; tirada abortada (sin cambios de estado).")
            else:
                log(f"  [!] Página {page} bloqueada; sigo con {len(ads)} anuncios ya descargados.")
            break
        results = parse_listings(html)
        items = results.get("items", [])
        ads.extend(items)
        total_pages = results.get("totalPages", 1)
        log(f"   {len(items)} anuncios (total listado: {results.get('totalResults', '?')} · páginas: {total_pages})")
        if page >= total_pages or not items or page >= MAX_PAGES:
            break
        page += 1
        time.sleep(DELAY_S + random.uniform(0, 2))
    return ads


def passes_filters(ad: dict) -> bool:
    if ad.get("year") is None or ad["year"] < MIN_YEAR:
        return False
    if ad.get("hp") is None or ad["hp"] < MIN_HP:
        return False
    if MAX_KM is not None and (ad.get("km") or 0) > MAX_KM:
        return False
    if MAX_PRICE is not None and (ad.get("price") or 0) > MAX_PRICE:
        return False
    return True


def normalize(ad: dict, now_iso: str) -> dict:
    seller = ad.get("seller") or {}
    ratings = (seller.get("ratings") or {})
    loc = ad.get("location") or {}
    return {
        "id": str(ad.get("id", "")),
        "first_seen": now_iso,
        "published": (ad.get("publicationDate") or ad.get("creationDate") or "")[:10],
        "title": (ad.get("title") or f"{ad.get('make','')} {ad.get('model','')}").strip(),
        "price": ad.get("price"),
        "year": ad.get("year"),
        "km": ad.get("km"),
        "hp": ad.get("hp"),
        "fuel": ad.get("fuelType", ""),
        "label": ad.get("environmentalLabel", ""),
        "province": loc.get("mainProvince", ""),
        "city": loc.get("cityLiteral", ""),
        "seller_type": "Profesional" if seller.get("isProfessional") else "Particular",
        "seller_name": (seller.get("name") or "").strip(),
        "seller_rating": ratings.get("average", ""),
        "tipo": "Km0/Demo" if "/km-0/" in (ad.get("url") or "") else "Ocasión",
        "foto": (ad.get("imgUrl") or "").split("/359x269cut")[0],
        "url": "https://www.coches.net" + (ad.get("url") or ""),
    }


CSV_FIELDS = ["id", "first_seen", "published", "title", "price", "year", "km", "hp",
              "fuel", "label", "province", "city", "seller_type", "seller_name",
              "seller_rating", "tipo", "foto", "url"]


def state_passes_filters(v: dict) -> bool:
    """Aplica los filtros vigentes a un anuncio guardado en el estado
    (para que el inventario refleje cambios de configuración)."""
    if v.get("year") is None or v["year"] < MIN_YEAR:
        return False
    if v.get("hp") is None or v["hp"] < MIN_HP:
        return False
    if MAX_KM is not None and (v.get("km") or 0) > MAX_KM:
        return False
    if MAX_PRICE is not None and (v.get("price") or 0) > MAX_PRICE:
        return False
    return True


def load_seen() -> dict:
    if SEEN_FILE.exists():
        return json.loads(SEEN_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(seen: dict, new_ads: list) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    SEEN_FILE.write_text(json.dumps(seen, ensure_ascii=False, indent=1), encoding="utf-8")
    # Historial acumulativo (una fila por anuncio la primera vez que se ve)
    is_new_file = not CSV_FILE.exists()
    with CSV_FILE.open("a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        if is_new_file:
            w.writeheader()
        for ad in new_ads:
            w.writerow(ad)


def fmt_row(a: dict) -> str:
    stars = f" · ⭐{a['seller_rating']}" if a["seller_rating"] != "" else ""
    tipo = f" · {a['tipo']}" if a["tipo"] != "Ocasión" else ""
    return (f"  💶 {a['price']:>6,} € · {a['year']} · {a['km']:>7,} km · {a['hp']} CV"
            f" · {a['fuel']}{stars}{tipo}\n"
            f"     📍 {a['city']} ({a['province']}) · {a['seller_type']}"
            f"{' — ' + a['seller_name'] if a['seller_name'] else ''} · publicado {a['published']}\n"
            f"     {a['title']}\n"
            f"     🔗 {a['url']}")


# Auto-detección de proveedor por prefijo de la key:
#   xai-  → Grok (x.ai) · gsk_ → Groq · sk- → OpenAI · sk_or_ → OpenRouter
PROVEEDORES = {
    "xai-":  ("https://api.x.ai/v1",       ["grok-4-fast-non-reasoning", "grok-4-fast",
                                             "grok-3-mini", "grok-3"]),
    "gsk_":  ("https://api.groq.com/openai/v1",
              ["qwen/qwen3.6-27b", "groq/compound-mini", "llama-3.3-70b-versatile",
               "llama-3.1-8b-instant"]),
    "sk_or_": ("https://openrouter.ai/api/v1",
               ["x-ai/grok-4-fast:free", "x-ai/grok-4-fast", "x-ai/grok-3-mini",
                "x-ai/grok-4-fast-non-reasoning", "deepseek/deepseek-chat-v3.1:free",
                "meta-llama/llama-3.3-70b-instruct:free"]),
    "sk-":   ("https://api.openai.com/v1", ["gpt-4o-mini", "gpt-4.1-mini"]),
}


def _resolver_ia():
    """Devuelve (base_url, key, candidatos) según prefijo de la key y el catálogo."""
    key = (os.environ.get("IA_API_KEY") or "").strip()
    if not key:
        return None
    base, preferidas = "https://api.groq.com/openai/v1", [
        "llama-3.3-70b-versatile", "llama-3.1-8b-instant", "qwen/qwen3.6-27b"]
    for prefijo, (b, modelos) in PROVEEDORES.items():
        if key.lower().startswith(prefijo):
            base, preferidas = b, modelos
            break
    if os.environ.get("IA_URL"):
        base = os.environ["IA_URL"].rstrip("/")
    candidatos = ([os.environ["IA_MODEL"]] if os.environ.get("IA_MODEL")
                  else list(preferidas))
    no_chat = ("guard", "embed", "whisper", "tts", "vision", "flux", "sdxl",
               "orpheus", "safeguard")
    try:
        estado, cuerpo = _curl_json(base + "/models", token=key, timeout=20)
        ids = [m.get("id", "") for m in (cuerpo or {}).get("data", [])]
        log(f"  [IA] catálogo ({len(ids)}): {', '.join(ids[:14])}")
        # añade como reserva los primeros modelos "chat" del catálogo
        if not os.environ.get("IA_MODEL"):
            candidatos += [i for i in ids
                           if i not in candidatos
                           and not any(x in i.lower() for x in no_chat)][:2]
        # descarta candidatos que no están en el catálogo (si este se pudo leer)
        presentes = [c for c in candidatos if c in ids] if ids else candidatos
        candidatos = presentes or candidatos
    except Exception:  # noqa: BLE001
        pass
    return base, key, candidatos[:4]


def _curl_json(url, payload=None, token=None, timeout=90):
    """POST/GET JSON vía curl (python-urllib recibe 403/1010 de Cloudflare)."""
    exe = shutil.which("curl")
    if not exe:
        raise RuntimeError("curl no disponible")
    cmd = [exe, "-sS", "--compressed", "--max-time", str(timeout),
           "-w", "\n@@HTTP:%{http_code}", "-H", "Content-Type: application/json"]
    if token:
        cmd += ["-H", f"Authorization: Bearer {token}"]
    if payload is not None:
        cmd += ["-X", "POST", "-d", json.dumps(payload)]
    cmd.append(url)
    out = subprocess.run(cmd, capture_output=True, timeout=timeout + 15).stdout
    marker = out.rfind(b"\n@@HTTP:")
    estado = out[marker + 8:].decode("ascii", "ignore").strip() if marker >= 0 else "0"
    cuerpo = (out[:marker] if marker >= 0 else out).decode("utf-8", "ignore")
    try:
        return estado, json.loads(cuerpo)
    except json.JSONDecodeError:
        return estado, cuerpo


def ai_comentarios(nuevos: list, now_iso: str):
    """Comentario breve de la IA para cada coche NUEVO (payload pequeño = fiable).
    Devuelve {id: comentario} (vacío si no hay IA o falla)."""
    if not nuevos:
        return {}
    ia = _resolver_ia()
    if not ia:
        return {}
    base, key, candidatos = ia
    lineas = [f"{c.get('id')}|{c.get('modelo')}|{c['precio']}€|{c['anyo']}|{c['km']}km|"
              f"{c['titulo'][:38]}|{c['lugares'][:20]}|⭐{c.get('rating') or '-'}"
              for c in nuevos[:6]]
    sysmsg = ("Eres asesor experto en coches de ocasión Toyota Corolla híbridos. "
              "Respondes SOLO con JSON válido.")
    usrmsg = ("Escribe una frase útil (máx 75 caracteres) sobre cada anuncio nuevo: qué "
              "destaca (precio/km/equipamiento) o qué conviene vigilar. Formato exacto:\n"
              '{"comentarios":[{"id":"<id>","comentario":"..."}]}\nAnuncios:\n'
              + "\n".join(lineas))
    for modelo in candidatos:
        payload = {
            "model": modelo, "temperature": 0.4, "max_tokens": 700,
            "messages": [{"role": "system", "content": sysmsg},
                         {"role": "user", "content": usrmsg}]}
        try:
            estado, out = _curl_json(base + "/chat/completions",
                                     payload=payload, token=key, timeout=90)
            if estado != "200" or not isinstance(out, dict):
                continue
            msg = (out.get("choices") or [{}])[0].get("message", {}) or {}
            contenido = (msg.get("content") or msg.get("reasoning_content")
                         or msg.get("reasoning") or "").strip()
            contenido = re.sub(r"^```(json)?|```$", "", contenido.strip(), flags=re.M).strip()
            if not contenido.startswith("{"):
                m = re.search(r"\{[\s\S]*\}", contenido)
                contenido = m.group(0) if m else ""
            try:
                items = json.loads(contenido).get("comentarios", [])
            except json.JSONDecodeError:
                items = []
            ids_validos = {c.get("id") for c in nuevos[:6]}
            resultado = {str(it.get("id")): str(it.get("comentario", ""))[:110]
                         for it in items if str(it.get("id")) in ids_validos
                         and it.get("comentario")}
            if resultado:
                log(f"✔ Comentarios IA para {len(resultado)} novedad(es) ({modelo})")
                return resultado
        except Exception as exc:  # noqa: BLE001
            log(f"  [IA] [{modelo}] {str(exc)[:100]}")
    return {}


def version_de(titulo: str, hp) -> str:
    """Modelo del Corolla TS: 140H / 180H / 200H (por título, fallback por CV)."""
    t = (titulo or "").upper()
    for tag in ("200H", "180H", "140H"):
        if tag in t:
            return tag
    try:
        hp = int(hp)
    except (TypeError, ValueError):
        return "Otro"
    return {140: "140H", 178: "200H", 180: "180H", 184: "180H", 196: "200H"}.get(hp, f"{hp} CV")


def registrar_estado(fecha, anuncios, nuevos, ia_ok, aviso=""):
    """estado.json (última ejecución) + estado_log.csv (historial de tiradas)."""
    STATE_DIR.mkdir(exist_ok=True)
    ahora = dt.datetime.now(dt.timezone.utc)
    prox = ahora.replace(hour=6, minute=30, second=0, microsecond=0)
    if ahora >= prox:
        prox = (ahora.replace(hour=18, minute=30) if ahora.hour < 18
                else (ahora + dt.timedelta(days=1)).replace(hour=6, minute=30))
    (STATE_DIR / "estado.json").write_text(json.dumps({
        "fecha": fecha, "anuncios": anuncios, "nuevos": nuevos, "ia_ok": ia_ok,
        "aviso": aviso, "proxima": prox.strftime("%Y-%m-%d %H:%M UTC")}, ensure_ascii=False),
        encoding="utf-8")
    log_f = STATE_DIR / "estado_log.csv"
    nuevo_f = not log_f.exists()
    with log_f.open("a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if nuevo_f:
            w.writerow(["fecha", "anuncios", "nuevos", "ia_ok", "aviso"])
        w.writerow([fecha[:16], anuncios, nuevos, int(ia_ok), aviso[:60]])


def write_site(inv_groups, inv_ads_count, new_group_ids, drop_group_ids, now_iso,
               merged) -> None:
    """Genera docs/index.html (dashboard) inyectando los datos en docs/plantilla.html."""
    import statistics
    site_dir = Path(__file__).resolve().parent / "docs"
    plantilla = site_dir / "plantilla.html"
    if not plantilla.exists():
        log("⚠ Sin docs/plantilla.html — no se genera dashboard")
        return
    site_dir.mkdir(exist_ok=True)

    # histórico de precios (una línea por tirada): mínimo, mediano y medio.
    # migra el formato antiguo (fecha,coches,precio_medio) al nuevo de 5 columnas.
    hist_file = STATE_DIR / "historico_precios.csv"
    cars = [g[0] for g in inv_groups]
    filas = []
    if hist_file.exists():
        with hist_file.open(newline="", encoding="utf-8") as fh:
            lector = csv.reader(fh)
            next(lector, None)                      # cabecera (antigua o nueva)
            for r in lector:
                if len(r) >= 5:
                    filas.append(r[:5])
                elif len(r) == 3:
                    filas.append([r[0], r[1], "", "", r[2]])   # vieja → nueva
    if cars:
        precios = sorted(c["price"] for c in cars)
        filas.append([now_iso[:16], len(cars), precios[0],
                      precios[len(precios) // 2], round(statistics.mean(precios))])
        with hist_file.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["fecha", "coches", "minimo", "mediano", "medio"])
            w.writerows(filas)
    historico = []
    if hist_file.exists():
        for row in csv.DictReader(hist_file.open(encoding="utf-8")):
            try:
                historico.append({
                    "fecha": row["fecha"], "coches": int(row["coches"]),
                    "minimo": int(row["minimo"]) if row.get("minimo") else None,
                    "mediano": int(row["mediano"]) if row.get("mediano") else None,
                    "medio": int(row.get("medio") or row.get("precio_medio") or 0) or None,
                    "precio": int(row.get("medio") or row.get("precio_medio") or 0) or None})
            except (KeyError, ValueError):
                pass

    inventario = [{
        "id": g[0].get("id", ""), "modelo": version_de(g[0].get("title", ""), g[0].get("hp")),
        "precio": g[0]["price"], "anyo": g[0].get("year"), "km": g[0].get("km"),
        "cv": g[0].get("hp"), "titulo": g[0].get("title", ""), "url": g[0].get("url", ""),
        "urls": [a.get("url", "") for a in g], "n": len(g), "lugares": places_cell(g),
        "tipo": g[0].get("tipo", ""), "visto": max(x.get("last_seen", "") for x in g),
        "publicado": g[0].get("published", ""),
        "vendedor": g[0].get("seller_name") or g[0].get("seller_type", ""),
        "rating": g[0].get("seller_rating", ""),
        "nuevo": any(x["id"] in new_group_ids for x in g),
        "rebajado": any(x["id"] in drop_group_ids for x in g),
    } for g in inv_groups]

    # ── referencias cortas estables (T001, T002…) ──
    ref_file = STATE_DIR / "referencias.csv"
    refs = {}
    if ref_file.exists():
        for row in csv.DictReader(ref_file.open(encoding="utf-8")):
            if row.get("id") and row.get("ref"):
                refs[row["id"]] = row["ref"]
    sig = max((int(r[1:]) for r in refs.values() if r.startswith("T")), default=0) + 1
    for c in sorted(inventario, key=lambda x: x["precio"]):
        if c["id"] not in refs:
            refs[c["id"]] = f"T{sig:03d}"
            sig += 1
    with ref_file.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["ref", "id"])
        for rid, ref in sorted(refs.items(), key=lambda kv: kv[1]):
            w.writerow([ref, rid])
    for c in inventario:
        c["ref"] = refs.get(c["id"], "")

    # ── novedades del DÍA (todas las tiradas de hoy) + comentarios IA ──
    primer_dia = {}
    if CSV_FILE.exists():
        for row in csv.DictReader(CSV_FILE.open(encoding="utf-8")):
            primer_dia[row.get("id")] = (row.get("first_seen") or "")[:10]
    hoy = now_iso[:10]
    nuevos = [c for c in inventario
              if c["nuevo"] or primer_dia.get(c["id"]) == hoy]
    nuevos.sort(key=lambda c: (c.get("publicado") or ""), reverse=True)
    nuevos = nuevos[:12]
    com_file = STATE_DIR / "comentarios.csv"
    comentarios = {}
    if com_file.exists():
        for row in csv.DictReader(com_file.open(encoding="utf-8")):
            if row.get("comentario"):
                comentarios[row.get("id")] = row.get("comentario")
    frescos = ai_comentarios([c for c in nuevos if c["id"] not in comentarios], now_iso)
    if frescos:
        comentarios.update(frescos)
        sin_cab = not com_file.exists()
        with com_file.open("a", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            if sin_cab:
                w.writerow(["id", "fecha", "comentario"])
            for cid, com in frescos.items():
                w.writerow([cid, now_iso[:16], com])
    for c in nuevos:
        c["comentarioIA"] = comentarios.get(c["id"], "")

    # ── estado del sistema ──
    registrar_estado(now_iso, len(inventario), len(nuevos), bool(frescos) or not nuevos)
    ejecuciones = []
    log_f = STATE_DIR / "estado_log.csv"
    if log_f.exists():
        for row in csv.DictReader(log_f.open(encoding="utf-8")):
            try:
                ejecuciones.append({"fecha": row["fecha"], "anuncios": int(row["anuncios"]),
                                    "nuevos": int(row["nuevos"]), "ia": bool(int(row["ia_ok"])),
                                    "aviso": row.get("aviso", "")})
            except (KeyError, ValueError):
                pass
    estado = json.loads((STATE_DIR / "estado.json").read_text(encoding="utf-8"))

    # ── histórico por modelo (una fila por tirada y modelo) ──
    por_modelo = {}
    for c in inventario:
        por_modelo.setdefault(c["modelo"], []).append(c["precio"])
    mod_file = STATE_DIR / "historico_modelos.csv"
    filas_mod = []
    if mod_file.exists():
        for row in csv.DictReader(mod_file.open(encoding="utf-8")):
            try:
                filas_mod.append([row["fecha"], row["modelo"], int(row["n"]),
                                  int(row["minimo"]), int(row["mediano"]), int(row["medio"])])
            except (KeyError, ValueError):
                pass
    hoy_fila = now_iso[:16]
    filas_mod = [f for f in filas_mod if f[0] != hoy_fila]
    for mod, ps in sorted(por_modelo.items()):
        ps2 = sorted(ps)
        filas_mod.append([hoy_fila, mod, len(ps2), ps2[0],
                          ps2[len(ps2) // 2], round(statistics.mean(ps2))])
    with mod_file.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["fecha", "modelo", "n", "minimo", "mediano", "medio"])
        w.writerows(filas_mod)
    historico_modelos = [{"fecha": f[0], "modelo": f[1], "n": f[2],
                          "minimo": f[3], "mediano": f[4], "medio": f[5]} for f in filas_mod]

    # ── salidas estimadas: anuncios no vistos en 4+ días (vendidos o retirados) ──
    salidas_file = STATE_DIR / "salidas.csv"
    ya_salidas, salidas_todas = set(), []
    if salidas_file.exists():
        for row in csv.DictReader(salidas_file.open(encoding="utf-8")):
            ya_salidas.add(row.get("id"))
            try:
                salidas_todas.append({"fecha": row["fecha_salida"], "modelo": row["modelo"],
                                      "precio": int(row["precio"] or 0),
                                      "dias": int(row["dias_en_venta"] or 0)})
            except (KeyError, ValueError):
                pass
    primer_dia = {}
    if CSV_FILE.exists():
        for row in csv.DictReader(CSV_FILE.open(encoding="utf-8")):
            primer_dia[row.get("id")] = (row.get("first_seen") or "")[:10]
    ids_hoy = {c["id"] for c in inventario}
    corte = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=4)).strftime("%Y-%m-%d")
    nuevas_salidas = []
    for v in merged.values():
        fid = str(v.get("id") or "")
        if fid in ids_hoy or fid in ya_salidas:
            continue
        ultimo = (v.get("last_seen") or "")[:10]
        if not (ultimo and ultimo <= corte):
            continue
        dias = ""
        f0 = primer_dia.get(fid, "")
        if f0:
            try:
                dias = (dt.datetime.strptime(ultimo, "%Y-%m-%d")
                        - dt.datetime.strptime(f0, "%Y-%m-%d")).days
            except ValueError:
                pass
        nuevas_salidas.append([ultimo, fid, version_de(v.get("title", ""), v.get("hp")),
                               v.get("price") or "", dias])
    if nuevas_salidas:
        sin_cabecera = not salidas_file.exists()
        with salidas_file.open("a", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            if sin_cabecera:
                w.writerow(["fecha_salida", "id", "modelo", "precio", "dias_en_venta"])
            w.writerows(nuevas_salidas)
        for f in nuevas_salidas:
            try:
                salidas_todas.append({"fecha": f[0], "modelo": f[2],
                                      "precio": int(f[3] or 0), "dias": int(f[4] or 0)})
            except ValueError:
                pass

    # ── objetivos de compra (percentil 10 automático si no hay valor fijo) ──
    def _pct(precios, p):
        s = sorted(precios)
        return s[max(0, min(len(s) - 1, int(round(p * (len(s) - 1)))))]

    objetivos = []
    for mod, ps in sorted(por_modelo.items()):
        obj = OBJETIVOS.get(mod) or _pct(ps, 0.10)
        objetivos.append({"modelo": mod, "objetivo": obj, "minimo": min(ps),
                          "debajo": sum(1 for x in ps if x <= obj)})

    # ── rebajas recientes (14 días) para el dashboard ──
    hace14 = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=14)).strftime("%Y-%m-%d")
    rebajas_recientes = []
    rebajas_file = STATE_DIR / "rebajas.csv"
    if rebajas_file.exists():
        for row in csv.DictReader(rebajas_file.open(encoding="utf-8")):
            try:
                if (row.get("fecha") or "")[:10] < hace14:
                    continue
                nuevo, anterior = int(row["precio_nuevo"]), int(row["precio_anterior"])
                url_ad = (merged.get(row["id"]) or {}).get("url", "")
                rebajas_recientes.append({
                    "fecha": row["fecha"][:10], "id": row["id"],
                    "ref": refs.get(row["id"], ""), "titulo": row["titulo"],
                    "modelo": version_de(row["titulo"], None),
                    "url": url_ad,
                    "precio_anterior": anterior, "precio_nuevo": nuevo,
                    "dif": anterior - nuevo})
            except (KeyError, ValueError):
                pass
    rebajas_recientes.sort(key=lambda r: r["fecha"], reverse=True)
    hace7 = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)).strftime("%Y-%m-%d")
    ult7 = {r["id"]: r for r in rebajas_recientes if r["fecha"] >= hace7}
    for c in inventario:
        if c["id"] in ult7:
            c["rebajado"] = True
            c["rebaja_info"] = {"de": ult7[c["id"]]["precio_anterior"],
                                "a": ult7[c["id"]]["precio_nuevo"],
                                "fecha": ult7[c["id"]]["fecha"]}

    datos = {"actualizado": now_iso, "config": f"≥{MIN_YEAR} · ≥{MIN_HP} CV",
             "anuncios": inv_ads_count, "rebajas": len(drop_group_ids),
             "inventario": inventario, "historico": historico,
             "comentariosIA": comentarios, "novedades": nuevos,
             "estado": estado, "ejecuciones": ejecuciones[-30:],
             "historicoModelos": historico_modelos, "salidas": salidas_todas,
             "objetivos": objetivos, "rebajas": rebajas_recientes}
    html = plantilla.read_text(encoding="utf-8").replace(
        "/*DATOS*/null", json.dumps(datos, ensure_ascii=False))
    (site_dir / "index.html").write_text(html, encoding="utf-8")
    log(f"✔ Dashboard generado en docs/index.html ({len(inventario)} coches)")


def car_key(a: dict):
    """Clave de 'mismo coche físico': los concesionarios publican el mismo vehículo
    en varias sucursales (multilisting) con IDs y hasta precios distintos."""
    return ((a.get("title") or "").strip().lower(), a.get("year"), a.get("km"), a.get("hp"))


def group_cars(ads: list) -> list:
    """Agrupa anuncios del mismo coche. Devuelve grupos ordenados por precio."""
    groups = {}
    for a in ads:
        groups.setdefault(car_key(a), []).append(a)
    return [sorted(g, key=lambda x: x["price"]) for g in groups.values()]


def links_cell(g: list) -> str:
    """Celda de título de un grupo: [título](url1) · [2ª](url2) · [3ª](url3)..."""
    cell = f"[{g[0]['title']}]({g[0]['url']})"
    for i, a in enumerate(g[1:], 2):
        cell += f" · [{i}ª]({a['url']})"
    return cell


def places_cell(g: list) -> str:
    seen, out = set(), []
    for a in g:
        p = f"{a.get('city', '')} ({a.get('province', '')})".strip()
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    if not out:
        return ""
    return out[0] + (f" +{len(out) - 1} más" if len(out) > 2 else (f" / {out[1]}" if len(out) > 1 else ""))


def main() -> int:
    now_iso = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    log(f"═══ coches.net · Corolla Touring Sports ≥{MIN_YEAR} · ≥{MIN_HP} CV · {now_iso} ═══")

    raw_ads = collect_ads()
    log(f"→ Anuncios descargados: {len(raw_ads)} (antes de filtrar)")

    matched, seen_ids_run = [], set()
    for ad in raw_ads:
        if not passes_filters(ad):
            continue
        a = normalize(ad, now_iso)
        if a["id"] in seen_ids_run:      # el listado repite anuncios promocionados
            continue
        seen_ids_run.add(a["id"])
        matched.append(a)
    log(f"→ Que cumplen año ≥{MIN_YEAR} y ≥{MIN_HP} CV: {len(matched)}")

    if not matched:
        # Tirada vacía (bloqueo o sin resultados): NO tocar el estado
        log("⚠ Sin anuncios esta tirada — el estado y el CSV quedan como estaban.")
        try:
            registrar_estado(now_iso, 0, 0, False, aviso="tirada sin datos (bloqueo)")
        except Exception:  # noqa: BLE001
            pass
        Path(__file__).resolve().parent.joinpath("resumen.md").write_text(
            f"# Corolla TS ≥{MIN_YEAR} · ≥{MIN_HP} CV — {now_iso}\n\n"
            "⚠ Ejecución sin datos (posible bloqueo temporal de coches.net). "
            "El estado se conserva; la próxima ejecución detectará las novedades igualmente.\n",
            encoding="utf-8")
        return 0

    # ── Estado: fusionar lo visto antes (memoria) con lo de esta tirada ──
    today = now_iso[:10]
    prune_cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=PRUNE_DAYS)).strftime("%Y-%m-%d")

    merged = {}
    for k, v in load_seen().items():                # migrar/recuperar lo anterior
        e = dict(v)
        e.setdefault("last_seen", today)
        merged[k] = e
    merged = {k: v for k, v in merged.items() if v["last_seen"] >= prune_cutoff}

    prior_prices = {k: v.get("price") for k, v in merged.items()}
    prior_ids = set(merged.keys())

    new_ads = []          # por ID (para CSV/estado)
    for a in matched:
        if a["id"] not in prior_ids:
            new_ads.append(a)
        a2 = {k: v for k, v in a.items() if k != "first_seen"}
        a2["last_seen"] = today
        merged[a["id"]] = a2

    # Grupos de "mismo coche físico" (multilistings de concesionario)
    cur_groups = group_cars(matched)
    new_groups = [g for g in cur_groups if not any(a["id"] in prior_ids for a in g)]
    drop_groups = []      # (grupo, precio_mínimo_anterior)
    for g in cur_groups:
        olds = [prior_prices[a["id"]] for a in g
                if a["id"] in prior_prices and prior_prices[a["id"]] is not None]
        if olds and g[0]["price"] is not None and g[0]["price"] < min(olds):
            drop_groups.append((g, min(olds)))

    save_state(merged, new_ads)

    # ── registro persistente de rebajas (nivel anuncio) ──
    # (tras guardar el estado: si esto fallara, la tirada no se pierde)
    try:
        nuevas_rebajas = []
        for a in matched:
            prev_p = prior_prices.get(a["id"])
            if prev_p is not None and a["price"] is not None and a["price"] < prev_p:
                nuevas_rebajas.append([now_iso[:16], a["id"],
                                       (a.get("title") or "")[:44],
                                       a["price"], prev_p])
        rebajas_file = STATE_DIR / "rebajas.csv"
        if nuevas_rebajas:
            sin_cab = not rebajas_file.exists()
            with rebajas_file.open("a", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                if sin_cab:
                    w.writerow(["fecha", "id", "titulo", "precio_nuevo", "precio_anterior"])
                w.writerows(nuevas_rebajas)
    except Exception as exc:  # noqa: BLE001
        log(f"⚠ No se pudo registrar la rebaja en el log: {str(exc)[:90]}")

    log("")
    if new_groups:
        tot = sum(len(g) for g in new_groups)
        log(f"🆕 COCHES NUEVOS ({len(new_groups)} coches · {tot} anuncios):")
        for g in sorted(new_groups, key=lambda g: g[0]["price"]):
            log(f"  💶 {g[0]['price']:,} € · {g[0]['year']} · {g[0]['km']:,} km · "
                f"{g[0]['hp']} CV · {g[0]['province']}")
            for a in g:
                log(f"     {'   └ ' if len(g) > 1 else ''}{a['price']:,} € · {a['city']} — "
                    f"{a['seller_name'] or a['seller_type']} · {a['url']}")
    else:
        log("🆕 Sin coches nuevos desde la última ejecución.")

    if drop_groups:
        log(f"\n📉 REBAJAS ({len(drop_groups)} coches):")
        for g, old_min in sorted(drop_groups, key=lambda x: x[0][0]["price"]):
            log(f"  {g[0]['title']}\n     💶 {old_min:,} € → {g[0]['price']:,} € "
                f"({g[0]['year']} · {g[0]['km']:,} km · {places_cell(g)})\n     🔗 {g[0]['url']}")

    # Resumen para GitHub Actions (job summary) — novedades + rebajas + inventario completo
    summary = Path(__file__).resolve().parent / "resumen.md"
    n_new_anuncios = sum(len(g) for g in new_groups)
    palabra = "coche" if len(new_groups) == 1 else "coches"
    lines = [f"# Corolla TS ≥{MIN_YEAR} · ≥{MIN_HP} CV — {now_iso}",
             f"Descargados: **{len(raw_ads)}** · Tras filtros: **{len(matched)}** anuncios · "
             f"Nuevos: **{len(new_groups)}** {palabra} ({n_new_anuncios} anuncios) · "
             f"Rebajas: **{len(drop_groups)}**", ""]
    if new_groups:
        lines += [f"## 🆕 Nuevos ({len(new_groups)} {palabra})", "",
                  "| Precio | Año | km | CV | Lugar | Tipo | Título |",
                  "|---:|---:|---:|---:|---|---|---|"]
        for g in sorted(new_groups, key=lambda g: g[0]["price"]):
            a = g[0]
            lines.append(f"| {a['price']:,} € | {a['year']} | {a['km']:,} | {a['hp']} | "
                         f"{places_cell(g)} | {a['tipo']} | {links_cell(g)} |")
    if drop_groups:
        lines += ["", f"## 📉 Rebajas ({len(drop_groups)})", "",
                  "| Antes | Ahora | Título |", "|---:|---:|---|"]
        for g, old in sorted(drop_groups, key=lambda x: x[0][0]["price"]):
            lines.append(f"| {old:,} € | {g[0]['price']:,} € | {links_cell(g)} |")
    # Inventario: todo lo visto en los últimos INV_DAYS días, AGRUPADO por coche
    new_group_ids = {a["id"] for g in new_groups for a in g}
    drop_group_ids = {a["id"] for g, _ in drop_groups for a in g}
    inv_cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=INV_DAYS)).strftime("%Y-%m-%d")
    inv_ads = [v for v in merged.values()
               if v.get("last_seen", today) >= inv_cutoff
               and isinstance(v.get("price"), (int, float))
               and state_passes_filters(v)]
    inv_groups = sorted(group_cars(inv_ads), key=lambda g: g[0]["price"])
    lines += ["", f"## 🚗 Inventario completo ({len(inv_groups)} coches · {len(inv_ads)} anuncios)", "",
              "| | Precio | Año | km | CV | Lugar | Tipo | Visto | Título |",
              "|---|---:|---:|---:|---:|---|---|---|---|"]
    for g in inv_groups:
        a = g[0]
        mark = "🆕" if any(x["id"] in new_group_ids for x in g) else \
               ("📉" if any(x["id"] in drop_group_ids for x in g) else "")
        if len(g) > 1:
            mark += f" ×{len(g)}"
        km = a.get("km"); km = f"{km:,}" if isinstance(km, int) else ""
        visto = max(x.get("last_seen", today) for x in g)
        lines.append(f"| {mark} | {a['price']:,} € | {a.get('year','')} | {km} | "
                     f"{a.get('hp','')} | {places_cell(g)} | "
                     f"{a.get('tipo','')} | {visto} | {links_cell(g)} |")
    lines += ["", f"*Inventario = todo lo visto en los últimos {INV_DAYS} días "
              f"(cada tirada cubre las {MAX_PAGES} primeras páginas del listado). "
              "Los concesionarios publican el mismo coche en varias sucursales: los "
              "anuncios repetidos se agrupan en una sola fila (×N, con un enlace por "
              "anuncio) mostrando el precio más bajo. La columna \"Visto\" indica la "
              "última tirada en la que apareció.*"]
    summary.write_text("\n".join(lines), encoding="utf-8")

    try:
        write_site(inv_groups, len(inv_ads), new_group_ids, drop_group_ids, now_iso, merged)
    except Exception as exc:  # noqa: BLE001 — el dashboard nunca debe romper el scraper
        log(f"⚠ No se pudo generar el dashboard: {exc}")

    log(f"\n✔ Estado guardado en {STATE_DIR}/ · Resumen en resumen.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
