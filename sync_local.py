#!/usr/bin/env python3
"""
Goodvalley — Local Sync Script
Run this from your laptop to export ciruela bins from pWarehouse8
into data_dump/bins.json, then commit and push so Railway can import it.

Requirements (one-time):
    pip3 install requests

Usage:
    python3 sync_local.py
"""

import json
import os
import re
import sys
import getpass

PWAREHOUSE_URL = os.environ.get('PWAREHOUSE_URL', 'http://190.211.168.247:8077')
PWAREHOUSE_RUT = os.environ.get('PWAREHOUSE_RUT', '')
PWAREHOUSE_PASS = os.environ.get('PWAREHOUSE_PASS', '')

DRYING_MAP = {
    'cancha': 'cancha', 'cancha de sol': 'cancha', 'sol': 'cancha',
    'campo': 'cancha', 'field': 'cancha', 'field drying': 'cancha',
    'horno': 'horno', 'oven': 'horno', 'oven drying': 'horno',
    'termino secado': 'termino_secado', 'término secado': 'termino_secado',
    'termino_secado': 'termino_secado', 'term. secado': 'termino_secado',
    'term.secado': 'termino_secado',
}

CALIBER_RE = re.compile(r'(\d{2,3}/\d{2,3}|\d{2,3}\+)')


def _rv(row, i):
    return row.get(i) or row.get(str(i))


def _parse_js(text):
    text = text.strip()
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass
    fixed = re.sub(r'(?<!["\w])(\w+)\s*:', r'"\1":', text)
    return json.loads(fixed)


def _extract_sid(html):
    for pat in [
        r"['\"]?_S_ID['\"]?\s*[=:]\s*['\"]([A-Za-z0-9]+)['\"]",
        r"_S_ID=([A-Za-z0-9]+)",
    ]:
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            return m.group(1)
    return None


def _parse_producto(producto):
    p = (producto or '').strip().upper()
    if 'TERM' in p:
        drying = 'termino_secado'
    elif 'HORNO' in p:
        drying = 'horno'
    elif 'SOL' in p or 'CANCHA' in p or 'CAMPO' in p:
        drying = 'cancha'
    else:
        drying = None
    m = CALIBER_RE.search(p)
    caliber = m.group(1) if m else None
    return caliber, drying


def _temporada(tarja_str):
    if len(tarja_str) >= 8:
        try:
            p = int(tarja_str[:2])
            if 18 <= p <= 35:
                return str(2000 + p)
        except ValueError:
            pass
    return None


def login(url, rut, password):
    import requests
    sess = requests.Session()
    sess.headers['User-Agent'] = 'Mozilla/5.0 (compatible; Goodvalley-Sync/1.0)'

    print(f"  Conectando a {url} ...")
    r = sess.get(url + '/', timeout=30)
    r.raise_for_status()

    sid = _extract_sid(r.text)
    if not sid:
        raise RuntimeError("No se encontró _S_ID en la página de pWarehouse8.")
    print(f"  Session ID: {sid}")

    # _fp_ must be EMPTY — credentials go as separate O16/O17 fields
    lr = sess.post(url + '/HandleEvent', data={
        'Ajax': '1', 'IsEvent': '1', 'Obj': 'O23', 'Evt': 'click',
        'this': 'O23', '_S_ID': sid, '_fp_': '',
        'O16': ' \x02\x02' + rut,
        'O17': ' \x02\x02' + password,
        '_seq_': 'a', '_uo_': 'O0',
    }, timeout=30)
    lr.raise_for_status()

    try:
        result = _parse_js(lr.text)
        if result.get('success') is False:
            raise RuntimeError("pWarehouse8 rechazó el login — verificá RUT y contraseña.")
    except (json.JSONDecodeError, ValueError):
        pass  # non-JSON response after login is OK

    print("  Login OK.")
    return sess, sid


def fetch_bins(sess, sid, url, page_size=2000):
    handle = url + '/HandleEvent'
    all_rows = []
    start = 0
    total = None

    while True:
        page = start // page_size + 1
        r = sess.get(handle, params={
            'IsEvent': '1', 'Obj': 'O16B', 'Evt': 'data',
            'options': '1', 'page': str(page),
            'start': str(start), 'limit': str(page_size), '_S_ID': sid,
        }, timeout=90)
        r.raise_for_status()
        data = _parse_js(r.text)

        if not data.get('success'):
            raise RuntimeError(f"Falló la obtención de datos: {r.text[:200]}")

        rows = data.get('rows', [])
        if not rows:
            break

        all_rows.extend(rows)
        if total is None:
            total = int(data.get('results', len(rows)))

        fetched = min(start + page_size, total)
        print(f"  Descargando {fetched:,} / {total:,} filas ...", end='\r')

        if start + page_size >= total:
            break
        start += page_size

    print()

    bins = []
    for row in all_rows:
        producto = str(_rv(row, 8) or '').strip()
        if 'CIRUELA' not in producto.upper():
            continue

        tarja = _rv(row, 1)
        if tarja is None:
            continue
        tarja_str = str(int(float(tarja))) if isinstance(tarja, (int, float)) else str(tarja).strip()

        neto = _rv(row, 2)
        try:
            weight = float(neto) if neto is not None else 0.0
        except (ValueError, TypeError):
            weight = 0.0

        # Caliber
        caliber = None
        serie = _rv(row, 15)
        if serie:
            m = CALIBER_RE.search(str(serie).strip())
            if m:
                caliber = m.group(1)

        # Drying method
        secado = _rv(row, 16)
        drying = DRYING_MAP.get(str(secado).lower().strip()) if secado else None

        # Fallback: parse from PRODUCTO
        cal_p, dry_p = _parse_producto(producto)
        if not caliber:
            caliber = cal_p
        if not drying:
            drying = dry_p

        if not drying:
            continue

        productor = _rv(row, 12)
        bins.append({
            'bin_identifier': tarja_str,
            'producto':       producto,
            'caliber':        caliber or '',
            'drying':         drying,
            'weight_kg':      weight,
            'humedad':        None,
            'contenedor':     '',
            'producer_name':  str(productor).strip() if productor else '',
            'temporada':      _temporada(tarja_str),
        })

    return bins, total or 0


def main():
    rut      = PWAREHOUSE_RUT or input("RUT de pWarehouse8: ").strip()
    password = PWAREHOUSE_PASS or getpass.getpass("Contraseña de pWarehouse8: ")

    print("\n[1/2] Iniciando sesión en pWarehouse8 ...")
    sess, sid = login(PWAREHOUSE_URL, rut, password)

    print("\n[2/2] Descargando bins de ciruela ...")
    bins, total = fetch_bins(sess, sid, PWAREHOUSE_URL)
    print(f"  Encontrados {len(bins):,} bins de ciruela de {total:,} en total.")

    if not bins:
        print("  Nada para exportar.")
        return

    out_dir = os.path.join(os.path.dirname(__file__), 'data_dump')
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, 'bins.json')

    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(bins, f, ensure_ascii=False, indent=2)

    print(f"\n✓ Exportado → {out_path}")
    print("  Ahora hacé commit + push de data_dump/bins.json y luego")
    print("  presioná '↻ Sync (archivo)' en la app de Railway.")


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelado.")
    except Exception as e:
        print(f"\n✗ Error: {e}")
        sys.exit(1)
