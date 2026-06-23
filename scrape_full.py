#!/usr/bin/env python3
"""
Full pWarehouse sync — 3 parallel browser sessions in one Chromium process.
  Session 1 → Bins en Bodega
  Session 2 → Pallets en Bodega
  Session 3 → Informe Procesos

Usage:
    python3 scrape_full.py                   # scrape + upload
    GV_NO_UPLOAD=1 python3 scrape_full.py   # scrape only, save to /tmp
"""
import asyncio, json, os, re, sys, datetime
from pathlib import Path
from playwright.async_api import async_playwright

PWAREHOUSE_URL = os.environ.get('PWAREHOUSE_URL', 'http://190.211.168.247:8077')
RUT            = os.environ.get('PWAREHOUSE_RUT',  '20664661-6')
PASSWORD       = os.environ.get('PWAREHOUSE_PASS', 'estante991')
GOODVALLEY_URL = os.environ.get('GOODVALLEY_URL',  'https://web-production-2eea96.up.railway.app')

# Output paths (can be overridden by env vars set by sync_start in app.py)
OUTPUT_DIR      = Path(os.environ.get('GV_OUTPUT_DIR', '/tmp'))
BINS_OUT        = Path(os.environ.get('GV_BINS_OUT',     str(OUTPUT_DIR / 'bins_scraped.json')))
PALLETS_OUT     = Path(os.environ.get('GV_PALLETS_OUT',  str(OUTPUT_DIR / 'pallets_scraped.json')))
PROCESOS_OUT    = Path(os.environ.get('GV_PROCESOS_OUT', str(OUTPUT_DIR / 'procesos_scraped.json')))
SCREENSHOT_DIR  = Path('/tmp/gv_scraper')

_CALIBER_RE = re.compile(r'(\d{2,3}/\d{2,3}|\d{2,3}\+)')
_DRYING_MAP = {
    'cancha': 'cancha', 'cancha de sol': 'cancha', 'sol': 'cancha', 'campo': 'cancha',
    'horno': 'horno',   'oven': 'horno',
    'termino secado': 'termino_secado', 'término secado': 'termino_secado',
    'termino_secado': 'termino_secado', 'term. secado': 'termino_secado',
    'term.secado': 'termino_secado',
}
_TIPO_MAP = {
    'tsc': 'tsc', 'tcc': 'tcc', 'tss': 'tss', 'elliot': 'elliot',
    'condicion natural': 'natural', 'condición natural': 'natural',
}


# ── Shared helpers ────────────────────────────────────────────────────────────

def _rv(row, *keys):
    """Get a value from a row that is either a dict or a list."""
    if isinstance(row, dict):
        for k in keys:
            v = row.get(k)
            if v is not None:
                return v
            v = row.get(str(k))
            if v is not None:
                return v
        return None
    for k in keys:
        try:
            v = row[k]
            if v is not None:
                return v
        except (IndexError, KeyError, TypeError):
            pass
    return None


def _parse_drying(val):
    if not val:
        return None
    v = str(val).lower().strip()
    if v in _DRYING_MAP:
        return _DRYING_MAP[v]
    if v.startswith(('canch', 'sol', 'camp')): return 'cancha'
    if v.startswith(('horn', 'oven')):          return 'horno'
    if v.startswith('term'):                    return 'termino_secado'
    return None


async def _login(page, label):
    """Navigate to pWarehouse and log in."""
    for attempt in range(1, 4):
        try:
            await page.goto(PWAREHOUSE_URL, timeout=30000)
            await page.wait_for_load_state('networkidle')
            break
        except Exception as e:
            if attempt == 3:
                raise RuntimeError(f'[{label}] Sin conexión tras 3 intentos: {e}')
            print(f'  [{label}] red inestable, reintentando...')
            await asyncio.sleep(5)

    await page.locator('input[name="O2F"]').fill(RUT)
    await page.locator('input[name="O17"]').fill(PASSWORD)

    btn = page.locator('#O23_id')
    if await btn.count():
        await btn.click()
    else:
        for t in ['Aceptar', 'Login', 'Iniciar sesión']:
            loc = page.locator(f'a:has-text("{t}"), button:has-text("{t}")')
            if await loc.count():
                await loc.first.click()
                break
        else:
            raise RuntimeError(f'[{label}] Botón Aceptar no encontrado')

    await page.wait_for_load_state('networkidle', timeout=20000)
    await page.wait_for_timeout(3000)

    for _ in range(8):
        if not await page.locator('input[name="O2F"]').count():
            break
        await page.wait_for_timeout(1000)
    else:
        raise RuntimeError(f'[{label}] Login falló — credenciales o servidor')

    print(f'[{label}] ✓ Login OK')


async def _click_text(page, *texts):
    for text in texts:
        loc = page.locator(f'a:has-text("{text}"), button:has-text("{text}")')
        if await loc.count():
            await loc.first.click()
            return True
    return False


async def _capture_rows(page, label, btn_id=None, btn_texts=(), max_secs=120):
    """Intercept /HandleEvent JSON responses and collect all data rows."""
    all_rows = []
    total    = [None]
    cap_url  = [None]

    async def on_response(resp):
        if '/HandleEvent' not in resp.url:
            return
        try:
            text = await resp.text()
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
        if cap_url[0] is None:
            cap_url[0] = resp.url
        if total[0] is None:
            total[0] = int(obj.get('results', len(rows)))
        all_rows.extend(rows)
        print(f'  [{label}] +{len(rows)} → {len(all_rows)}/{total[0] or "?"}')

    page.on('response', on_response)

    # Click the refresh/search button
    clicked = False
    if btn_id:
        b = page.locator(btn_id)
        if await b.count():
            await b.click()
            clicked = True
    if not clicked:
        clicked = await _click_text(page, *btn_texts, 'Actualizar', 'Buscar', 'Refresh')
    if not clicked:
        print(f'  [{label}] WARN: no se encontró botón Actualizar')

    for tick in range(max_secs // 3):
        await page.wait_for_timeout(3000)
        t = total[0] or 0
        print(f'  [{label}] {(tick+1)*3}s → {len(all_rows)}/{t}')
        if all_rows and (total[0] is None or len(all_rows) >= total[0]):
            break

    return all_rows, cap_url[0]


# ── Transform: Bins en Bodega ─────────────────────────────────────────────────

def _transform_bins(raw_rows):
    bins = []
    for row in raw_rows:
        producto = str(_rv(row, 'PRODUCTO', 8) or '').strip()
        if 'CIRUELA' not in producto.upper():
            continue
        tarja = _rv(row, 'TARJA', 1)
        if tarja is None:
            continue
        tarja_str = str(round(float(tarja))) if isinstance(tarja, (int, float)) else str(tarja).strip()
        weight    = float(_rv(row, 'NETO', 2) or 0)
        hum_raw   = _rv(row, 'HUMEDAD', 5)
        humedad   = float(hum_raw) if hum_raw else None

        caliber = None
        serie   = _rv(row, 'SERIE', 15)
        if serie:
            m = _CALIBER_RE.search(str(serie))
            if m: caliber = m.group(1)

        drying = _parse_drying(_rv(row, 'SECADO', 16))

        if not caliber or not drying:
            p = producto.upper()
            if not caliber:
                m = _CALIBER_RE.search(p)
                caliber = m.group(1) if m else None
            if not drying:
                if 'TERM' in p:   drying = 'termino_secado'
                elif 'HORNO' in p: drying = 'horno'
                elif any(k in p for k in ('SOL', 'CANCHA', 'CAMPO')): drying = 'cancha'

        if not drying:
            continue

        temp_col = _rv(row, 'TEMPORADA', 0)
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
            'producto':       producto,
            'caliber':        caliber or '',
            'drying':         drying,
            'weight_kg':      weight,
            'humedad':        humedad if humedad and humedad > 0 else None,
            'contenedor':     str(_rv(row, 'CONTENEDOR', 7) or '').strip(),
            'producer_name':  str(_rv(row, 'PRODUCTOR', 12) or '').strip(),
            'temporada':      temporada,
        })
    return bins


# ── Transform: Pallets en Bodega ──────────────────────────────────────────────
# xlsx column order: TARJA(0) S_PALLET_CLASE(1) PALLET_ESTADO_OT(2)
#   HORAPRODUCCION(3) FECHAPRODUCCION(4) OT(5) TIPOPROCESO(6) CONTENEDOR(7)
#   PRODUCTO(8) RUTEXPORTADOR(9) EXPORTADOR(10) SERIE(11) UNIDADES(12)
#   NETO(kg)(13) CLIENTE(14) ESTADO(15)

def _transform_pallets(raw_rows):
    pallets = []
    for row in raw_rows:
        producto = str(_rv(row, 'PRODUCTO', 8) or '').strip()
        if 'CIRUELA' not in producto.upper():
            continue

        tarja   = str(_rv(row, 'TARJA', 0) or '').strip()
        ot      = str(_rv(row, 'OT', 5) or '').strip()
        tipo    = str(_rv(row, 'TIPOPROCESO', 6) or '').strip()
        serie   = str(_rv(row, 'SERIE', 11) or '').strip()
        neto    = _rv(row, 'NETO(kg)', 'NETO', 13)
        cliente = str(_rv(row, 'CLIENTE', 14) or '').strip()
        estado  = str(_rv(row, 'ESTADO', 15) or '').strip()

        if not tarja or not ot or not cliente:
            continue
        if estado and 'DISPONIBLE' not in estado.upper() and estado:
            continue  # skip non-available pallets

        kg      = float(neto) if neto is not None else 0.0
        caliber = serie if _CALIBER_RE.search(serie) else None
        pt      = _TIPO_MAP.get(tipo.lower().strip())

        # Infer drying from product name (enriched with procesos later)
        p = producto.upper()
        if 'HORNO' in p:   drying = 'horno'
        elif any(k in p for k in ('SOL','CANCHA','CAMPO')): drying = 'cancha'
        else:              drying = 'termino_secado'

        pallets.append({
            'tarja':        tarja,
            'ot':           ot,
            'customer':     cliente,
            'caliber':      caliber,
            'drying':       drying,
            'product_type': pt,
            'weight_kg':    kg,
            'producto':     producto,
            'temporada':    None,  # filled from procesos below
        })
    return pallets


# ── Transform: Informe Procesos ───────────────────────────────────────────────
# xlsx column order: FECHAPRODUCCION(0) TIPOPROCESO(1) TIPO(2) OT(3) IDOT(4)
#   NETOEGRESO(5) PRODUCTOR(6) SERIEINGRESO(7) EXPORTADOR(8) SECADO(9)
#   TEMPORADA(10) then caliber columns 20/30, 30/40, …

def _transform_procesos(raw_rows):
    procesos = []
    for row in raw_rows:
        ot     = str(_rv(row, 'OT', 3) or '').strip()
        if not ot:
            continue
        tipo   = str(_rv(row, 'TIPOPROCESO', 1) or '').strip()
        secado = _rv(row, 'SECADO', 9)
        temp   = _rv(row, 'TEMPORADA', 10)
        neto   = _rv(row, 'NETOEGRESO', 5)
        serie  = str(_rv(row, 'SERIEINGRESO', 7) or '').strip()

        drying    = _parse_drying(secado)
        try:
            temporada = str(int(float(temp))) if temp else None
        except Exception:
            temporada = None

        procesos.append({
            'ot':          ot,
            'tipoproceso': tipo,
            'drying':      drying,
            'temporada':   temporada,
            'neto_egreso': float(neto) if neto else None,
            'serie':       serie,
        })
    return procesos


# ── Section scrapers ──────────────────────────────────────────────────────────

async def scrape_bins_section(ctx):
    page = await ctx.new_page()
    try:
        await _login(page, 'BINS')
        await page.wait_for_timeout(3000)

        nav = page.locator('#O57_id')
        if await nav.count():
            await nav.click()
        else:
            if not await _click_text(page, 'Bins en bodega', 'Bins'):
                raise RuntimeError('[BINS] Menú Bins en Bodega no encontrado')

        await page.wait_for_timeout(3000)
        await page.wait_for_load_state('networkidle', timeout=15000)

        rows, _ = await _capture_rows(page, 'BINS', btn_id='#O137_id')
        return _transform_bins(rows)
    finally:
        await page.close()


async def scrape_pallets_section(ctx):
    page = await ctx.new_page()
    try:
        await _login(page, 'PALLETS')
        await page.wait_for_timeout(3000)

        if not await _click_text(page,
                'Pallets en bodega', 'Pallets en Bodega',
                'Pallets bodega', 'Pallets'):
            raise RuntimeError('[PALLETS] Menú Pallets en Bodega no encontrado')

        await page.wait_for_timeout(3000)
        await page.wait_for_load_state('networkidle', timeout=15000)

        rows, _ = await _capture_rows(page, 'PALLETS')
        # Save a sample for column-mapping debugging
        SCREENSHOT_DIR.mkdir(exist_ok=True)
        if rows:
            (SCREENSHOT_DIR / 'pallets_sample.json').write_text(
                json.dumps(rows[:3], ensure_ascii=False, indent=2))
        return _transform_pallets(rows)
    finally:
        await page.close()


async def scrape_procesos_section(ctx):
    page = await ctx.new_page()
    try:
        await _login(page, 'PROC')
        await page.wait_for_timeout(3000)

        if not await _click_text(page,
                'Informe procesos', 'Informe Procesos',
                'Procesos', 'Proceso'):
            raise RuntimeError('[PROC] Menú Procesos no encontrado')

        await page.wait_for_timeout(3000)
        await page.wait_for_load_state('networkidle', timeout=15000)

        rows, _ = await _capture_rows(page, 'PROC', btn_texts=['Actualizar', 'Buscar'])
        if rows:
            (SCREENSHOT_DIR / 'procesos_sample.json').write_text(
                json.dumps(rows[:3], ensure_ascii=False, indent=2))
        return _transform_procesos(rows)
    finally:
        await page.close()


# ── Upload helpers ────────────────────────────────────────────────────────────

def _post_json(path, payload, timeout=120, label=''):
    import urllib.request
    body = json.dumps(payload, ensure_ascii=False).encode()
    req  = urllib.request.Request(
        f'{GOODVALLEY_URL}{path}', data=body,
        headers={'Content-Type': 'application/json'}, method='POST',
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        result = json.loads(resp.read())
    return result


def _upload_bins(bins_data):
    import urllib.request, time
    boundary = 'GVscraper1234'
    body = (
        f'--{boundary}\r\n'
        f'Content-Disposition: form-data; name="bins_file"; filename="bins_scraped.json"\r\n'
        f'Content-Type: application/json\r\n\r\n'
        + json.dumps(bins_data, ensure_ascii=False)
        + f'\r\n--{boundary}--\r\n'
    ).encode()
    for attempt in range(1, 4):
        req = urllib.request.Request(
            f'{GOODVALLEY_URL}/sync/upload', data=body,
            headers={'Content-Type': f'multipart/form-data; boundary={boundary}'},
            method='POST',
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                print(f'✓ Bins upload OK (HTTP {resp.status})')
                return True
        except Exception as e:
            print(f'  bins upload intento {attempt}/3: {e}')
            if attempt < 3:
                time.sleep(10)
    return False


def _upload_pallets(pallets_data):
    import time
    for attempt in range(1, 4):
        try:
            r = _post_json('/admin/import-pallets', {
                'passcode': '001083748', 'pallets': pallets_data,
            }, timeout=180)
            print(f'✓ Pallets upload: {r.get("added",0)} añadidos, '
                  f'{r.get("allocated",0)} asignados, '
                  f'{r.get("skipped_duplicate",0)} duplicados')
            return True
        except Exception as e:
            print(f'  pallets upload intento {attempt}/3: {e}')
            if attempt < 3:
                time.sleep(10)
    return False


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    SCREENSHOT_DIR.mkdir(exist_ok=True)
    print(f'[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] '
          f'Full sync — 3 sesiones paralelas')

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage'],
        )
        ctx_bins, ctx_pallets, ctx_proc = [
            await browser.new_context() for _ in range(3)
        ]
        try:
            print('▶ Scraping Bins + Pallets + Procesos en paralelo...')
            results = await asyncio.gather(
                scrape_bins_section(ctx_bins),
                scrape_pallets_section(ctx_pallets),
                scrape_procesos_section(ctx_proc),
                return_exceptions=True,
            )
        finally:
            await browser.close()

    bins_data, pallets_data, proc_data = results

    for name, data in [('BINS', bins_data), ('PALLETS', pallets_data), ('PROCESOS', proc_data)]:
        if isinstance(data, Exception):
            print(f'✗ {name}: {data}', file=sys.stderr)

    bins_ok    = not isinstance(bins_data,    Exception)
    pallets_ok = not isinstance(pallets_data, Exception)
    proc_ok    = not isinstance(proc_data,    Exception)

    # Enrich pallets with secado/temporada from procesos
    if pallets_ok and proc_ok and pallets_data and proc_data:
        by_ot = {p['ot']: p for p in reversed(proc_data)}  # last OT wins
        for pallet in pallets_data:
            info = by_ot.get(pallet['ot'], {})
            if info.get('drying') and not pallet.get('drying'):
                pallet['drying'] = info['drying']
            if info.get('temporada') and not pallet.get('temporada'):
                pallet['temporada'] = info['temporada']

    if bins_ok:
        BINS_OUT.write_text(json.dumps(bins_data, indent=2, ensure_ascii=False))
        print(f'✓ Bins: {len(bins_data)} ciruelas → {BINS_OUT}')

    if pallets_ok:
        PALLETS_OUT.write_text(json.dumps(pallets_data, indent=2, ensure_ascii=False))
        print(f'✓ Pallets: {len(pallets_data)} ciruelas → {PALLETS_OUT}')

    if proc_ok:
        PROCESOS_OUT.write_text(json.dumps(proc_data, indent=2, ensure_ascii=False))
        print(f'✓ Procesos: {len(proc_data)} OTs → {PROCESOS_OUT}')

    if os.environ.get('GV_NO_UPLOAD'):
        print('GV_NO_UPLOAD activo — sin upload.')
        return

    if bins_ok and bins_data:
        _upload_bins(bins_data)

    if pallets_ok and pallets_data:
        _upload_pallets(pallets_data)

    print('\n✓ Full sync completado.')


if __name__ == '__main__':
    asyncio.run(main())
