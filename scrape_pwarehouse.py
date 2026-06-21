#!/usr/bin/env python3
"""
Goodvalley pWarehouse8 Scraper
Logs in, navigates to Bins en Bodega, extracts all ciruela data,
saves bins.json and uploads directly to Goodvalley.

Usage:  python3 scrape_pwarehouse.py
"""

import asyncio, json, os, re, sys
from pathlib import Path
from urllib.parse import urlparse, parse_qs, urlencode
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

PWAREHOUSE_URL  = os.environ.get('PWAREHOUSE_URL', 'http://190.211.168.247:8077')
RUT             = os.environ.get('PWAREHOUSE_RUT',  '20664661-6')
PASSWORD        = os.environ.get('PWAREHOUSE_PASS', 'estante991')
GOODVALLEY_URL  = os.environ.get('GOODVALLEY_URL',  'https://web-production-2eea96.up.railway.app')
OUTPUT_FILE     = Path(os.environ.get('GV_OUTPUT', str(Path.home() / 'Desktop' / 'bins_scraped.json')))
SCREENSHOT_DIR  = Path('/tmp/gv_scraper')

_CALIBER_RE = re.compile(r'(\d{2,3}/\d{2,3}|\d{2,3}\+)')
_DRYING_MAP = {
    'cancha': 'cancha', 'cancha de sol': 'cancha', 'sol': 'cancha', 'campo': 'cancha',
    'horno': 'horno', 'oven': 'horno',
    'termino secado': 'termino_secado', 'término secado': 'termino_secado',
    'termino_secado': 'termino_secado', 'term. secado': 'termino_secado',
    'term.secado': 'termino_secado',
}


def _parse_drying(val):
    if not val:
        return None
    v = str(val).lower().strip()
    if v in _DRYING_MAP:
        return _DRYING_MAP[v]
    if v.startswith(('canch', 'sol', 'camp')):
        return 'cancha'
    if v.startswith(('horn', 'oven')):
        return 'horno'
    if v.startswith('term'):
        return 'termino_secado'
    return None


def _parse_producto(p):
    u = (p or '').upper()
    drying = None
    if 'TERM' in u:
        drying = 'termino_secado'
    elif 'HORNO' in u:
        drying = 'horno'
    elif 'SOL' in u or 'CANCHA' in u or 'CAMPO' in u:
        drying = 'cancha'
    m = _CALIBER_RE.search(u)
    return (m.group(1) if m else None), drying


def _rv(row, i):
    if isinstance(row, dict):
        return row.get(i, row.get(str(i)))
    try:
        return row[i]
    except (IndexError, KeyError):
        return None


def _transform_rows(raw_rows):
    bins = []
    for row in raw_rows:
        producto = str(_rv(row, 8) or '').strip()
        if 'CIRUELA' not in producto.upper():
            continue
        tarja = _rv(row, 1)
        if tarja is None:
            continue
        tarja_str = str(round(float(tarja))) if isinstance(tarja, (int, float)) else str(tarja).strip()
        weight = float(_rv(row, 2) or 0)
        hum_raw = _rv(row, 5)
        humedad = float(hum_raw) if hum_raw else None
        cont = str(_rv(row, 7) or '').strip()
        caliber = None
        serie = _rv(row, 15)
        if serie:
            m = _CALIBER_RE.search(str(serie))
            if m:
                caliber = m.group(1)
        drying = _parse_drying(_rv(row, 16))
        cal_p, dry_p = _parse_producto(producto)
        if not caliber:
            caliber = cal_p
        if not drying:
            drying = dry_p
        if not drying:
            continue
        temp_col = _rv(row, 0)
        temp_str = str(temp_col).strip() if temp_col else None
        if temp_str and re.match(r'^20\d{2}$', temp_str):
            temporada = temp_str
        elif len(tarja_str) >= 8:
            try:
                p_num = int(tarja_str[:2])
                temporada = str(2000 + p_num) if 18 <= p_num <= 35 else None
            except Exception:
                temporada = None
        else:
            temporada = None
        bins.append({
            'bin_identifier': tarja_str,
            'producto': producto,
            'caliber': caliber or '',
            'drying': drying,
            'weight_kg': weight,
            'humedad': humedad if humedad and humedad > 0 else None,
            'contenedor': cont,
            'producer_name': str(_rv(row, 12) or '').strip(),
            'temporada': temporada,
        })
    return bins


async def click_by_text(page, text):
    """Click a UniGUI link/button by visible text — fallback when IDs shift."""
    loc = page.locator(f'a:has-text("{text}"), button:has-text("{text}")')
    if await loc.count():
        await loc.first.click()
        return True
    return False


async def main():
    import datetime
    SCREENSHOT_DIR.mkdir(exist_ok=True)
    print(f'[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] Iniciando scraper...')

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage'],
        )
        page    = await browser.new_page()

        # ── 1. Open pWarehouse8 (retry on transient network errors) ───────────
        print('▶ Abriendo pWarehouse8...')
        for attempt in range(1, 4):
            try:
                await page.goto(PWAREHOUSE_URL, timeout=30000)
                await page.wait_for_load_state('networkidle')
                break
            except Exception as e:
                if attempt == 3:
                    raise RuntimeError(f'No se pudo conectar a pWarehouse tras 3 intentos: {e}')
                print(f'  red inestable (intento {attempt}/3), reintentando en 5s...')
                await asyncio.sleep(5)
        await page.screenshot(path=str(SCREENSHOT_DIR / '01_login_page.png'))

        # ── 2. Log in ──────────────────────────────────────────────────────────
        print('▶ Iniciando sesión...')
        await page.locator('input[name="O2F"]').fill(RUT)
        await page.locator('input[name="O17"]').fill(PASSWORD)
        await page.screenshot(path=str(SCREENSHOT_DIR / '02_filled_login.png'))

        # Try confirmed ID first, fall back to text search
        btn = page.locator('#O23_id')
        if await btn.count():
            await btn.click()
        else:
            if not await click_by_text(page, 'Aceptar'):
                raise RuntimeError('No se encontró el botón Aceptar en el login.')

        # UniGUI can take a few seconds to tear down the login form after networkidle
        await page.wait_for_load_state('networkidle', timeout=20000)
        await page.wait_for_timeout(3000)
        await page.screenshot(path=str(SCREENSHOT_DIR / '03_after_login.png'))

        # Detect failed login: wait up to 8s for the login form to disappear
        for _ in range(8):
            if not await page.locator('input[name="O2F"]').count():
                break
            await page.wait_for_timeout(1000)
        else:
            raise RuntimeError('Login falló — credenciales incorrectas o servidor no responde.')

        # ── 3. Navigate to Bins en Bodega ──────────────────────────────────────
        print('▶ Navegando a Bins en Bodega...')
        await page.wait_for_timeout(3000)

        nav = page.locator('#O57_id')
        if await nav.count():
            await nav.click()
        else:
            if not await click_by_text(page, 'Bins en bodega'):
                if not await click_by_text(page, 'Bins'):
                    raise RuntimeError('No se encontró el menú Bins en Bodega.')

        await page.wait_for_timeout(3000)
        await page.wait_for_load_state('networkidle', timeout=15000)
        await page.screenshot(path=str(SCREENSHOT_DIR / '04_bins_en_bodega.png'))

        # ── 4. Intercept network responses, then click Actualizar ─────────────
        print('▶ Haciendo clic en Actualizar — esperando datos...')

        all_rows = []
        data_total = [None]
        captured_url = [None]

        async def on_response(response):
            url = response.url
            if '/HandleEvent' not in url:
                return
            try:
                text = await response.text()
            except Exception:
                return
            if not text or not text.startswith('{'):
                return
            try:
                obj = json.loads(text)
            except Exception:
                return
            rows = obj.get('rows')
            if not isinstance(rows, list) or not rows:
                return
            if captured_url[0] is None:
                captured_url[0] = url
            if data_total[0] is None:
                data_total[0] = int(obj.get('results', len(rows)))
            all_rows.extend(rows)
            print(f'  → {len(rows)} filas capturadas (acumulado: {len(all_rows)} / {data_total[0]})')

        page.on('response', on_response)

        act = page.locator('#O137_id')
        if await act.count():
            await act.click()
        else:
            if not await click_by_text(page, 'Actualizar'):
                raise RuntimeError('No se encontró el botón Actualizar.')

        for tick in range(40):  # up to 120 seconds
            await page.wait_for_timeout(3000)
            total_so_far = data_total[0] or 0
            print(f'  {(tick+1)*3}s → {len(all_rows)} / {total_so_far} filas')
            if all_rows and (data_total[0] is None or len(all_rows) >= data_total[0]):
                break

        await page.screenshot(path=str(SCREENSHOT_DIR / '05_grid_loaded.png'))
        print('  📸 05_grid_loaded.png')

        # Fetch additional pages if needed (pWarehouse may paginate)
        if all_rows and data_total[0] and len(all_rows) < data_total[0] and captured_url[0]:
            qs   = parse_qs(urlparse(captured_url[0]).query, keep_blank_values=True)
            flat = {k: v[0] for k, v in qs.items()}
            limit = int(flat.get('limit', 2000))
            print(f'  Obteniendo páginas adicionales...')
            while len(all_rows) < data_total[0]:
                start = len(all_rows)
                flat.update({'start': str(start), 'page': str(start // limit + 1)})
                extra_url = '/HandleEvent?' + urlencode(flat)
                extra_text = await page.evaluate(
                    f'async () => {{ const r = await fetch({json.dumps(extra_url)}, '
                    f'{{credentials:"include",headers:{{"X-Requested-With":"XMLHttpRequest"}}}}); '
                    f'return r.text(); }}'
                )
                try:
                    extra_rows = json.loads(extra_text).get('rows', [])
                except Exception:
                    break
                if not extra_rows:
                    break
                all_rows.extend(extra_rows)
                print(f'  → {len(extra_rows)} filas adicionales (total: {len(all_rows)})')

        await browser.close()

        if not all_rows:
            print('\n✗ pWarehouse no envió datos en 120 segundos.')
            print('  Esto puede ocurrir cuando el servidor responde lentamente desde Railway.')
            sys.exit(1)

        # ── 5. Transform to bins format ────────────────────────────────────────
        print('▶ Procesando datos...')
        bins = _transform_rows(all_rows)
        print(f'\n✓ {len(bins):,} bins de ciruela extraídos de {len(all_rows):,} filas')

        # ── 6. Save bins_scraped.json ──────────────────────────────────────────
        OUTPUT_FILE.write_text(json.dumps(bins, indent=2, ensure_ascii=False))
        print(f'✓ Guardado en {OUTPUT_FILE}')

        # ── 7. Upload to Goodvalley (3 retries) ───────────────────────────────
        if os.environ.get('GV_NO_UPLOAD'):
            print(f'✓ GV_NO_UPLOAD activo — omitiendo upload (archivo en {OUTPUT_FILE})')
            print('\n✓ Listo.')
            return

        print(f'▶ Subiendo a Goodvalley ({GOODVALLEY_URL})...')
        import urllib.request
        boundary = 'GVscraper1234'
        body = (
            f'--{boundary}\r\n'
            f'Content-Disposition: form-data; name="bins_file"; filename="bins_scraped.json"\r\n'
            f'Content-Type: application/json\r\n\r\n'
            + json.dumps(bins, ensure_ascii=False)
            + f'\r\n--{boundary}--\r\n'
        ).encode()

        uploaded = False
        for attempt in range(1, 4):
            req = urllib.request.Request(
                f'{GOODVALLEY_URL}/sync/upload',
                data=body,
                headers={'Content-Type': f'multipart/form-data; boundary={boundary}'},
                method='POST',
            )
            try:
                with urllib.request.urlopen(req, timeout=90) as resp:
                    print(f'✓ Goodvalley respondió: HTTP {resp.status}')
                    uploaded = True
                    break
            except Exception as e:
                print(f'  intento {attempt}/3 falló: {e}')
                if attempt < 3:
                    await asyncio.sleep(10)

        if not uploaded:
            print(f'  ⚠ Upload falló tras 3 intentos — {OUTPUT_FILE} guardado localmente.')
            sys.exit(1)

        print('\n🎉 Listo.')


if __name__ == '__main__':
    asyncio.run(main())
