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
        "url": "https://www.coches.net" + (ad.get("url") or ""),
    }


CSV_FIELDS = ["id", "first_seen", "published", "title", "price", "year", "km", "hp",
              "fuel", "label", "province", "city", "seller_type", "seller_name",
              "seller_rating", "tipo", "url"]


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
    "gsk_":  ("https://api.groq.com/openai/v1", ["llama-3.1-8b-instant", "llama-3.1-8b"]),
    "sk_or_": ("https://openrouter.ai/api/v1",  ["meta-llama/llama-3.1-8b-instruct:free"]),
    "sk-":   ("https://api.openai.com/v1", ["gpt-4o-mini", "gpt-4.1-mini"]),
}


def _resolver_ia():
    """Devuelve (url_chat, modelo, key) según la key y los overrides por env."""
    key = (os.environ.get("IA_API_KEY") or "").strip()
    if not key:
        return None
    base, preferidas = "https://api.groq.com/openai/v1", ["llama-3.1-8b-instant"]
    for prefijo, (b, modelos) in PROVEEDORES.items():
        if key.lower().startswith(prefijo):
            base, preferidas = b, modelos
            break
    if os.environ.get("IA_URL"):
        base = os.environ["IA_URL"].rstrip("/")
    url = base + "/chat/completions"
    modelo = os.environ.get("IA_MODEL") or preferidas[0]
    # si no hay override de modelo, intenta elegir uno realmente disponible
    if not os.environ.get("IA_MODEL"):
        try:
            estado, cuerpo = _curl_json(base + "/models", token=key, timeout=20)
            ids = [m.get("id", "") for m in (cuerpo or {}).get("data", [])]
            for pref in preferidas + ids:
                if any(pref in i for i in ids):
                    modelo = next(i for i in ids if pref in i)
                    break
        except Exception:  # noqa: BLE001 — si falla, usamos el preferido por defecto
            pass
    return url, modelo, key


def _curl_json(url, payload=None, token=None, timeout=90):
    """POST/GET JSON vía curl (python-urllib se come el 403/1010 de Cloudflare)."""
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


def ai_ranking(coches: list, now_iso: str):
    """Ranking de oportunidades con LLM (si hay IA_API_KEY). Devuelve None si no
    hay key o falla, y entonces el dashboard usa su heurístico integrado."""
    ia = _resolver_ia()
    if not ia or not coches:
        return None
    url_ia, MODELO_IA, tok = ia
    lineas = ["id|precio|año|km|CV|versión|lugar|valoración"]
    for c in coches[:45]:
        lineas.append(f"{c['id']}|{c['precio']}€|{c['anyo']}|{c['km']}km|{c['cv']}CV|"
                      f"{c['titulo'][:34]}|{c['lugares'][:18]}|{c.get('rating') or '-'}")
    sysmsg = ("Eres un experto en coches de ocasión Toyota Corolla Touring Sports híbridos. "
              "Respondes SOLO con JSON válido, sin texto adicional.")
    usrmsg = ("Analiza este inventario y haz un ranking de las 12 MEJORES oportunidades "
              "calidad/precio (ten en cuenta precio vs km y año, potencia, acabado, "
              "reputación del vendedor y coherencia del anuncio). Formato exacto:\n"
              '{"ranking":[{"id":"<id>","score":<0-100>,"comentario":"máx 85 caracteres, concreto"}]}\n'
              "Inventario:\n" + "\n".join(lineas))
    payload = {
        "model": MODELO_IA, "temperature": 0.2, "max_tokens": 900,
        "messages": [{"role": "system", "content": sysmsg},
                     {"role": "user", "content": usrmsg}]}
    try:
        estado, out = _curl_json(url_ia, payload=payload, token=tok, timeout=90)
        if estado != "200" or not isinstance(out, dict):
            raise RuntimeError(f"[{MODELO_IA}] HTTP {estado}: {str(out)[:150]}")
        contenido = out["choices"][0]["message"]["content"].strip()
        contenido = re.sub(r"^```(json)?|```$", "", contenido.strip(), flags=re.M).strip()
        ranking = json.loads(contenido).get("ranking", [])
        ids_validos = {c["id"] for c in coches}
        ranking = [{"id": str(r.get("id", "")), "score": int(r.get("score", 50)),
                    "comentario": str(r.get("comentario", ""))[:100]}
                   for r in ranking if str(r.get("id")) in ids_validos][:12]
        if not ranking:
            return None
        log(f"✔ Ranking IA generado ({MODELO_IA}, {len(ranking)} coches)")
        return {"fecha": now_iso, "ia": MODELO_IA, "ranking": ranking}
    except urllib.error.HTTPError as exc:
        cuerpo = exc.read()[:180].decode("utf-8", "ignore")
        log(f"⚠ Ranking IA no disponible (HTTP {exc.code}: {cuerpo}) — dashboard heurístico")
        return None
    except Exception as exc:  # noqa: BLE001 — la IA nunca debe romper el scraper
        log(f"⚠ Ranking IA no disponible ({str(exc)[:90]}) — el dashboard usará el heurístico")
        return None


def write_site(inv_groups, inv_ads_count, new_group_ids, drop_group_ids, now_iso) -> None:
    """Genera docs/index.html (dashboard) inyectando los datos en docs/plantilla.html."""
    import statistics
    site_dir = Path(__file__).resolve().parent / "docs"
    plantilla = site_dir / "plantilla.html"
    if not plantilla.exists():
        log("⚠ Sin docs/plantilla.html — no se genera dashboard")
        return
    site_dir.mkdir(exist_ok=True)

    # histórico del precio medio (una línea por tirada)
    hist_file = STATE_DIR / "historico_precios.csv"
    cars = [g[0] for g in inv_groups]
    if cars:
        nuevo_hist = not hist_file.exists()
        with hist_file.open("a", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            if nuevo_hist:
                w.writerow(["fecha", "coches", "precio_medio"])
            w.writerow([now_iso[:16], len(cars), round(statistics.mean(c["price"] for c in cars))])
    historico = []
    if hist_file.exists():
        for row in csv.DictReader(hist_file.open(encoding="utf-8")):
            try:
                historico.append({"fecha": row["fecha"], "coches": int(row["coches"]),
                                  "precio": int(row["precio_medio"])})
            except (KeyError, ValueError):
                pass

    inventario = [{
        "id": g[0].get("id", ""), "precio": g[0]["price"], "anyo": g[0].get("year"), "km": g[0].get("km"),
        "cv": g[0].get("hp"), "titulo": g[0].get("title", ""), "url": g[0].get("url", ""),
        "urls": [a.get("url", "") for a in g], "n": len(g), "lugares": places_cell(g),
        "tipo": g[0].get("tipo", ""), "visto": max(x.get("last_seen", "") for x in g),
        "publicado": g[0].get("published", ""),
        "vendedor": g[0].get("seller_name") or g[0].get("seller_type", ""),
        "rating": g[0].get("seller_rating", ""),
        "nuevo": any(x["id"] in new_group_ids for x in g),
        "rebajado": any(x["id"] in drop_group_ids for x in g),
    } for g in inv_groups]

    ranking = ai_ranking(inventario, now_iso)

    datos = {"actualizado": now_iso, "config": f"≥{MIN_YEAR} · ≥{MIN_HP} CV",
             "anuncios": inv_ads_count, "rebajas": len(drop_group_ids),
             "inventario": inventario, "historico": historico, "rankingIA": ranking}
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
        write_site(inv_groups, len(inv_ads), new_group_ids, drop_group_ids, now_iso)
    except Exception as exc:  # noqa: BLE001 — el dashboard nunca debe romper el scraper
        log(f"⚠ No se pudo generar el dashboard: {exc}")

    log(f"\n✔ Estado guardado en {STATE_DIR}/ · Resumen en resumen.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
