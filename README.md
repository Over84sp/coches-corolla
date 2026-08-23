# 🔎 coches.net — Toyota Corolla Touring Sports (2022+ · 140 CV+)

Scraper **ultraligero** de coches.net que se ejecuta 2 veces al día en
**GitHub Actions** (gratis, sin ordenador encendido) y avisa solo de
**anuncios nuevos** y **bajadas de precio**.

- Cada ejecución hace como máximo **6 peticiones** (35 anuncios/página, ~2,5 s entre ellas).
- Filtrado en servidor por URL: `toyota/corolla/familiar/segunda-mano` (el familiar **es** el Touring Sports).
- Filtrado en local: año ≥ 2022 y potencia ≥ 160 CV (versiones 180H/196H/200H, como la búsqueda manual; descarta 125H de 122 CV y 140H de 140 CV).
- Solo se consultan listados permitidos por `robots.txt` (respeta su tope de paginación `pg≥7`).

## Puesta en marcha (2 minutos)

1. Crea un repositorio **privado** vacío en GitHub.
2. Sube estos ficheros conservando la estructura:
   ```
   scraper.py
   README.md
   .github/workflows/scrape.yml
   ```
   (Desde línea de comandos, con el contenido de esta carpella en el directorio actual:)
   ```bash
   git init -b main
   git add .
   git commit -m "scraper coches.net corolla ts"
   git remote add origin git@github.com:TU_USUARIO/TU_REPO.git
   git push -u origin main
   ```
3. Listo. El workflow corre a las **08:30 y 20:30** (hora española de verano).
   En la pestaña **Actions** puedes ver cada ejecución: en su resumen aparece la
   tabla de anuncios nuevos y rebajas.

## Ver las novedades

- **Actions → última ejecución → Summary**: tabla de 🆕 nuevos y 📉 rebajas.
- **`.data/anuncios.csv`**: historial acumulativo (una fila por anuncio visto).
- **`.data/seen.json`**: estado de dedupeo (no tocar a mano).
- **`resumen.md`**: último resumen en la raíz del repo.

> La primera ejecución marcará como "nuevos" todos los anuncios vigentes que
> cumplan los filtros (normal). A partir de la segunda solo verás lo nuevo.

## Ajustar filtros

Todo está en el bloque `CONFIGURACIÓN` de `scraper.py`:

| Variable | Significado | Ejemplo |
|---|---|---|
| `BASE_URL` | Listado de coches.net | `.../segunda-mano/barcelona/` · `.../20000_euros/` |
| `MIN_YEAR` | Año mínimo | `2022` |
| `MIN_HP` | CV mínimos | `160` |
| `MAX_KM` / `MAX_PRICE` | Límites locales opcionales | `120000` / `25000` o `None` |
| `MAX_PAGES` | Tope de páginas por ejecución | `6` (máx. recomendado) |

Otros listados útiles: `.../hibrido/`, `.../automatico/`, por provincia
(`/barcelona/`, `/girona/`...) o precio máximo (`/25000_euros/`).

## Notas

- GitHub **desactiva los cron de repos sin actividad a los 60 días**; con hacer
  cualquier push o ejecutar el workflow a mano se reactiva.
- La hora de los cron es UTC (`06:30` y `18:30`); en invierno serán 07:30/19:30.
- **coches.net aplica rate-limit/anti-bot**: la descarga se hace con `curl`
  (urllib recibe página de bloqueo desde los datacenters de GitHub). Si aun así
  se corta (pasa sobre la 6ª petición rápida), la tirada termina con lo recogido
  y el estado queda intacto — la siguiente tirada recupera novedades por dedupeo.
  Con `MAX_PAGES = 5` (~175 anuncios cubiertos) no suele pasar.
- Uso respetuoso: ~10 peticiones/día máximo, con retardo entre peticiones.
  Si la web cambiara de maquetación, el script fallará con error claro en el
  log de Actions en vez de corromper el estado (mira `.data/ultima_respuesta.html`).
