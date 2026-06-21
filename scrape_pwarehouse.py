#!/usr/bin/env python3
"""
Goodvalley pWarehouse8 Scraper
Logs in, navigates to Bins en Bodega, extracts all ciruela data,
saves bins.json and uploads directly to Goodvalley.

Usage:  python3 scrape_pwarehouse.py
"""

import asyncio, json, os, sys
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

PWAREHOUSE_URL  = 'http://190.211.168.247:8077'
RUT             = '20664661-6'
PASSWORD        = 'estante991'
GOODVALLEY_URL  = 'https://web-production-2eea96.up.railway.app'
OUTPUT_FILE     = Path.home() / 'Desktop' / 'bins_scraped.json'
SCREENSHOT_DIR  = Path('/tmp/gv_scraper')

# Same extraction logic as the Chrome extension — returns the bins array
EXTRACT_JS = """
async () => {
  // 1. Find _S_ID
  let sid = null;
  if (typeof window._S_ID === 'string' && window._S_ID) sid = window._S_ID;
  if (!sid) { const el = document.querySelector('input[name="_S_ID"]'); if (el) sid = el.value; }
  if (!sid) {
    for (const s of document.querySelectorAll('script')) {
      const m = (s.textContent||'').match(/_S_ID['":\\s=]+['"]?([A-Za-z0-9]{8,})/i);
      if (m) { sid = m[1]; break; }
    }
  }
  if (!sid) {
    const m = document.documentElement.innerHTML.match(/_S_ID['":\\s=]+['"]?([A-Za-z0-9]{8,})/i);
    if (m) sid = m[1];
  }
  if (!sid) return { error: 'No se encontró _S_ID' };

  // 2. Find grid object ID
  let gridObj = null;

  // Method A: performance API
  try {
    for (const e of performance.getEntriesByType('resource')) {
      if (!e.name.includes('/HandleEvent')) continue;
      const u = new URL(e.name);
      const obj = u.searchParams.get('Obj');
      if (obj && u.searchParams.get('Evt') === 'data') { gridObj = obj; break; }
    }
  } catch(_) {}

  // Method B: ExtJS component registry
  if (!gridObj && window.Ext) {
    try {
      Ext.ComponentManager.each((id, comp) => {
        if (gridObj) return false;
        const store = comp.store || (comp.getStore && comp.getStore());
        if (!store) return;
        const total = (store.getTotalCount && store.getTotalCount()) || store.totalCount || 0;
        if (total < 5) return;
        const proxy = store.proxy || (store.getProxy && store.getProxy());
        if (proxy) {
          const ep = proxy.extraParams || {};
          if (ep.Obj) { gridObj = ep.Obj; return false; }
          const url = String(proxy.url || '');
          const m = url.match(/[?&]Obj=([^&]+)/i);
          if (m) { gridObj = m[1]; return false; }
        }
        if (id && /^O\\d/.test(id)) { gridObj = id; return false; }
      });
    } catch(_) {}
  }

  // Method C: DOM scan
  if (!gridObj && window.Ext && Ext.getCmp) {
    const seen = new Set();
    document.querySelectorAll('[id]').forEach(el => {
      const m = el.id.match(/^(O\\d{1,3}[A-Z]?)/);
      if (m) seen.add(m[1]);
    });
    for (const id of seen) {
      try {
        const comp = Ext.getCmp(id);
        if (!comp) continue;
        const store = comp.store || (comp.getStore && comp.getStore());
        if (!store) continue;
        if ((store.getTotalCount && store.getTotalCount()) > 5) { gridObj = id; break; }
      } catch(_) {}
    }
  }

  if (!gridObj) return { error: 'Grid no detectado — abrí Bins en Bodega y hacé clic en Actualizar' };

  // 3. Helpers
  const CALIBER_RE = /(\\d{2,3}\\/\\d{2,3}|\\d{2,3}\\+)/;
  const DRYING_MAP = {
    'cancha':'cancha','cancha de sol':'cancha','sol':'cancha','campo':'cancha',
    'horno':'horno','oven':'horno',
    'termino secado':'termino_secado','término secado':'termino_secado',
    'termino_secado':'termino_secado','term. secado':'termino_secado','term.secado':'termino_secado',
  };

  function parseDrying(val) {
    if (!val) return null;
    const v = String(val).toLowerCase().trim();
    if (DRYING_MAP[v]) return DRYING_MAP[v];
    if (v.startsWith('canch')||v.startsWith('sol')||v.startsWith('camp')) return 'cancha';
    if (v.startsWith('horn')||v.startsWith('oven'))                        return 'horno';
    if (v.startsWith('term'))                                               return 'termino_secado';
    return null;
  }

  function parseProducto(p) {
    const u = (p||'').toUpperCase();
    let drying = null;
    if (u.includes('TERM'))                                          drying = 'termino_secado';
    else if (u.includes('HORNO'))                                    drying = 'horno';
    else if (u.includes('SOL')||u.includes('CANCHA')||u.includes('CAMPO')) drying = 'cancha';
    const m = CALIBER_RE.exec(u);
    return { caliber: m ? m[1] : null, drying };
  }

  function rv(row, i) { return row[i] !== undefined ? row[i] : row[String(i)]; }

  // 4. Read rows — in-memory store first, then paginated XHR
  let allRows = null;

  if (window.Ext && Ext.ComponentManager) {
    try {
      Ext.ComponentManager.each((id, comp) => {
        if (allRows !== null) return false;
        const store = comp.store || (comp.getStore && comp.getStore());
        if (!store) return;
        const items = (store.data && store.data.items)
                   || (store.snapshot && store.snapshot.items) || [];
        if (items.length < 5) return;
        allRows = items.map(item => item.data || item.raw || item);
        return false;
      });
    } catch(_) {}
  }

  if (!allRows) {
    allRows = [];
    const PAGE_SIZE = 2000;
    let start = 0, total = null;

    function xhrGet(url) {
      return new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open('GET', url, true);
        xhr.withCredentials = true;
        xhr.setRequestHeader('X-Requested-With', 'XMLHttpRequest');
        xhr.onload = () => resolve(xhr.responseText);
        xhr.onerror = () => reject(new Error('XHR error'));
        xhr.send();
      });
    }

    while (true) {
      const page = Math.floor(start / PAGE_SIZE) + 1;
      const url = '/HandleEvent?' + new URLSearchParams({
        IsEvent:'1', Obj:gridObj, Evt:'data',
        options:'1', page:String(page), start:String(start), limit:String(PAGE_SIZE), _S_ID:sid,
      });
      let text;
      try { text = (await xhrGet(url)).trim(); } catch(e) { return { error: 'Error de red: '+e.message }; }
      if (!text || text==='{[]}'||text==='{}'||text==='[]') return { error: 'Sesión expirada' };
      if (text.startsWith('<')) return { error: 'Respuesta HTML — sesión expirada' };
      let data;
      try { data = JSON.parse(text); } catch(_) { return { error: 'JSON inválido: '+text.slice(0,80) }; }
      const rows = data.rows || [];
      if (!rows.length) break;
      allRows.push(...rows);
      if (total === null) total = parseInt(data.results||rows.length, 10);
      if (start + PAGE_SIZE >= total) break;
      start += PAGE_SIZE;
    }
  }

  if (!allRows || !allRows.length) return { error: 'Grid vacío' };

  // 5. Filter & transform
  const bins = [];
  for (const row of allRows) {
    const producto = String(rv(row,8)||'').trim();
    if (!producto.toUpperCase().includes('CIRUELA')) continue;
    const tarja = rv(row,1);
    if (tarja === null || tarja === undefined) continue;
    const tarjaStr = typeof tarja==='number' ? String(Math.round(tarja)) : String(tarja).trim();
    const weight  = parseFloat(rv(row,2)) || 0;
    const humedad = parseFloat(rv(row,5)) || null;
    const cont    = String(rv(row,7)||'').trim();
    let caliber = null;
    const serie = rv(row,15);
    if (serie) { const m = CALIBER_RE.exec(String(serie)); if (m) caliber = m[1]; }
    let drying = parseDrying(rv(row,16));
    const fb = parseProducto(producto);
    if (!caliber) caliber = fb.caliber;
    if (!drying)  drying  = fb.drying;
    if (!drying) continue;
    const tempCol = rv(row,0);
    const tempStr = tempCol ? String(tempCol).trim() : null;
    const tempVal = (tempStr && /^20\\d{2}$/.test(tempStr)) ? tempStr : (() => {
      if (tarjaStr.length >= 8) { const p = parseInt(tarjaStr.slice(0,2),10); if (p>=18&&p<=35) return String(2000+p); }
      return null;
    })();
    bins.push({
      bin_identifier: tarjaStr,
      producto, caliber: caliber||'', drying,
      weight_kg: weight,
      humedad: (humedad && humedad > 0) ? humedad : null,
      contenedor: cont,
      producer_name: rv(row,12) ? String(rv(row,12)).trim() : '',
      temporada: tempVal,
    });
  }

  return { bins, count: bins.length };
}
"""


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
        browser = await p.chromium.launch(headless=True)
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

        await page.wait_for_load_state('networkidle', timeout=20000)
        await page.screenshot(path=str(SCREENSHOT_DIR / '03_after_login.png'))

        # Detect failed login (still on login page)
        if await page.locator('input[name="O2F"]').count():
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

        # ── 4. Click Actualizar and poll until the store has data ────────────
        print('▶ Haciendo clic en Actualizar — esperando datos (~30s)...')
        act = page.locator('#O137_id')
        if await act.count():
            await act.click()
        else:
            if not await click_by_text(page, 'Actualizar'):
                raise RuntimeError('No se encontró el botón Actualizar.')

        for tick in range(25):   # up to ~75 seconds
            await page.wait_for_timeout(3000)
            count = await page.evaluate("""() => {
                let best = 0;
                if (!window.Ext) return 0;
                try {
                    Ext.ComponentManager.each((id, comp) => {
                        const store = comp.store || (comp.getStore && comp.getStore());
                        if (!store) return;
                        const n = (store.getTotalCount && store.getTotalCount())
                                  || store.totalCount || 0;
                        if (n > best) best = n;
                    });
                } catch(_) {}
                return best;
            }""")
            print(f'  {(tick+1)*3}s → {count} registros en store')
            if count > 100:
                break

        await page.screenshot(path=str(SCREENSHOT_DIR / '05_grid_loaded.png'))
        print('  📸 05_grid_loaded.png')

        # ── 5. Extract data via JS ─────────────────────────────────────────────
        print('▶ Extrayendo datos...')
        result = await page.evaluate(EXTRACT_JS)

        await page.screenshot(path=str(SCREENSHOT_DIR / '06_after_extract.png'))
        print('  📸 06_after_extract.png')

        await browser.close()

        if 'error' in result:
            print(f'\n✗ Error: {result["error"]}')
            sys.exit(1)

        bins = result['bins']
        print(f'\n✓ {len(bins):,} bins de ciruela extraídos')

        # ── 6. Save bins_scraped.json ──────────────────────────────────────────
        OUTPUT_FILE.write_text(json.dumps(bins, indent=2, ensure_ascii=False))
        print(f'✓ Guardado en {OUTPUT_FILE}')

        # ── 7. Upload to Goodvalley (3 retries) ───────────────────────────────
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
