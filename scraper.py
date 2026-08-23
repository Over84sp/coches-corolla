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
MIN_HP = 140           # potencia mínima (CV)
MAX_KM = None          # p. ej. 120000, o None para sin límite
MAX_PRICE = None       # p. ej. 25000, o None para sin límite
MAX_PAGES = 6          # tope de páginas (35 anuncios/página) — robots.txt desaconseja pg≥7
DELAY_S = 2.5          # segundos entre peticiones (cortesía)
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


def fetch_html(url: str, retries: int = 3) -> str:
    """Descarga la página. Usa curl si está disponible (su fingerprint TLS pasa
    los filtros anti-bot de coches.net; urllib recibe página de bloqueo en algunos
    datacenters). Fallback a urllib con reintentos."""
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            html, status, headers = _fetch_with_curl(url) if shutil.which("curl") \
                else _fetch_with_urllib(url)
            STATE_DIR.mkdir(exist_ok=True)
            (STATE_DIR / "ultima_respuesta.html").write_text(
                f"URL: {url}\nHTTP: {status}\n{headers}\n\n{html[:3000]}", encoding="utf-8")
            return html
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            log(f"  [!] intento {attempt}/{retries} fallido: {exc}")
            time.sleep(5 * attempt)
    raise RuntimeError(f"No se pudo descargar {url}: {last_exc}")


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
    """Descarga hasta MAX_PAGES del listado y devuelve los anuncios crudos."""
    ads, page = [], 1
    while page <= MAX_PAGES:
        url = BASE_URL + (f"?pg={page}" if page > 1 else "")
        log(f"→ Descargando página {page}: {url}")
        results = parse_listings(fetch_html(url))
        items = results.get("items", [])
        ads.extend(items)
        total_pages = results.get("totalPages", 1)
        log(f"   {len(items)} anuncios (total listado: {results.get('totalResults', '?')} · páginas: {total_pages})")
        if page >= total_pages or not items:
            break
        page += 1
        time.sleep(DELAY_S)
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

    seen = load_seen()
    new_ads, price_drops, updated_seen = [], [], {}
    for a in matched:
        prev = seen.get(a["id"])
        if prev is None:
            new_ads.append(a)
        elif prev.get("price") is not None and a["price"] is not None and a["price"] < prev["price"]:
            price_drops.append((a, prev["price"]))
        a2 = dict(a)
        a2.pop("first_seen", None)
        updated_seen[a["id"]] = a2

    save_state(updated_seen, new_ads)

    log("")
    if new_ads:
        log(f"🆕 ANUNCIOS NUEVOS ({len(new_ads)}):")
        for a in sorted(new_ads, key=lambda x: x["price"]):
            log(fmt_row(a))
    else:
        log("🆕 Sin anuncios nuevos desde la última ejecución.")

    if price_drops:
        log(f"\n📉 REBAJAS ({len(price_drops)}):")
        for a, old_price in sorted(price_drops, key=lambda x: x[0]["price"]):
            log(f"  {a['title']}\n     💶 {old_price:,} € → {a['price']:,} € "
                f"({a['year']} · {a['km']:,} km · {a['province']})\n     🔗 {a['url']}")

    # Resumen para GitHub Actions (job summary)
    summary = Path(__file__).resolve().parent / "resumen.md"
    lines = [f"# Corolla TS ≥{MIN_YEAR} · ≥{MIN_HP} CV — {now_iso}",
             f"Descargados: **{len(raw_ads)}** · Tras filtros: **{len(matched)}** · "
             f"Nuevos: **{len(new_ads)}** · Rebajas: **{len(price_drops)}**", ""]
    if new_ads:
        lines += [f"## 🆕 Nuevos ({len(new_ads)})", "",
                  "| Precio | Año | km | CV | Lugar | Tipo | Título |",
                  "|---:|---:|---:|---:|---|---|---|"]
        for a in sorted(new_ads, key=lambda x: x["price"]):
            lines.append(f"| {a['price']:,} € | {a['year']} | {a['km']:,} | {a['hp']} | "
                         f"{a['city']} ({a['province']}) | {a['tipo']} | "
                         f"[{a['title']}]({a['url']}) |")
    if price_drops:
        lines += ["", f"## 📉 Rebajas ({len(price_drops)})", "",
                  "| Antes | Ahora | Título |", "|---:|---:|---|"]
        for a, old in price_drops:
            lines.append(f"| {old:,} € | {a['price']:,} € | [{a['title']}]({a['url']}) |")
    summary.write_text("\n".join(lines), encoding="utf-8")

    log(f"\n✔ Estado guardado en {STATE_DIR}/ · Resumen en resumen.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
