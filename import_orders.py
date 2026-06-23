#!/usr/bin/env python3
"""
Phase 1: Create orders + lines from Pallets en bodega + historico.
Phase 2: Create finished-pallet bins and allocate them to those orders.

Usage:
    python3 import_orders.py             # run both phases
    python3 import_orders.py --phase1    # orders only (already done)
    python3 import_orders.py --phase2    # pallets/bins only
    python3 import_orders.py --dry-run   # print without uploading
"""
import json, sys, re, urllib.request
import pandas as pd
from pathlib import Path

GOODVALLEY_URL = 'https://web-production-2eea96.up.railway.app'
PASSCODE       = '001083748'
DESKTOP        = Path.home() / 'Desktop'

TIPO_MAP = {
    'tsc':              'tsc',
    'tcc':              'tcc',
    'tss':              'tss',
    'elliot':           'elliot',
    'condicion natural':'natural',
}
SECADO_MAP = {
    'cancha': 'cancha', 'sol': 'cancha', 'campo': 'cancha',
    'horno':  'horno',  'oven': 'horno',
}
CALIBER_RE = re.compile(r'(\d{2,3}/\d{2,3}|\d{2,3}\+)')


def map_tipo(val):
    return TIPO_MAP.get(str(val or '').lower().strip())


def map_secado(val):
    return SECADO_MAP.get(str(val or '').lower().strip())


def infer_drying(producto):
    p = (producto or '').upper()
    if 'HORNO' in p:   return 'horno'
    if any(k in p for k in ('SOL', 'CANCHA', 'CAMPO')): return 'cancha'
    return 'termino_secado'   # "TERMINADO/TERMINADA" = finished drying


def post_json(path, payload, timeout=120):
    body = json.dumps(payload, ensure_ascii=False).encode()
    req  = urllib.request.Request(
        f'{GOODVALLEY_URL}{path}', data=body,
        headers={'Content-Type': 'application/json'}, method='POST',
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def load_excel():
    print('Leyendo archivos Excel...')
    hist   = pd.read_excel(DESKTOP / 'historico para Camilo.xlsx')
    pallet = pd.read_excel(DESKTOP / 'Pallets en bodega.xlsx', header=1)

    # OT → {secado, temporada, idbins} from EGRESO A PROCESO
    egreso = hist[hist['MOVIMIENTO'] == 'EGRESO A PROCESO'].copy()
    egreso['IDBINS2'] = egreso['IDBINS2'].astype(str).str.strip().str.split('.').str[0]
    egreso = egreso[egreso['IDBINS2'].notna() & (egreso['IDBINS2'] != 'nan')]
    egreso['OT_s'] = egreso['OT'].astype(str).str.strip()

    ot_info = {}
    for ot, grp in egreso.groupby('OT_s'):
        sv = grp['SECADO'].dropna()
        tv = grp['TEMPORADA'].dropna()
        ot_info[ot] = {
            'secado':    sv.mode()[0] if not sv.empty else None,
            'temporada': int(tv.mode()[0]) if not tv.empty else None,
            'idbins':    sorted(grp['IDBINS2'].unique().tolist()),
        }

    circ = pallet[pallet['PRODUCTO'].str.contains('CIRUELA', na=False)].copy()
    circ['OT'] = circ['OT'].astype(str).str.strip()
    return circ, ot_info


# ── Phase 1: Create orders ────────────────────────────────────────────────────

def phase1(circ, ot_info, dry_run):
    groups = {}
    for _, row in circ.iterrows():
        cliente = str(row['CLIENTE']).strip()
        ot      = str(row['OT']).strip()
        tipo    = str(row['TIPOPROCESO']).strip() if pd.notna(row['TIPOPROCESO']) else ''
        serie   = str(row['SERIE']).strip()       if pd.notna(row['SERIE'])       else ''
        kg      = float(row['NETO(kg)'])          if pd.notna(row['NETO(kg)'])    else 0.0
        produto = str(row['PRODUTO'] if 'PRODUTO' in row else row.get('PRODUCTO', '')).strip()

        groups.setdefault(cliente, {})
        key = (ot, tipo, serie)
        if key not in groups[cliente]:
            info   = ot_info.get(ot, {})
            drying = map_secado(info.get('secado')) or infer_drying(str(row.get('PRODUCTO','')))
            groups[cliente][key] = {
                'ot': ot, 'product_type': map_tipo(tipo),
                'caliber': serie if CALIBER_RE.search(serie) else None,
                'drying': drying, 'temporada': info.get('temporada'),
                'kg': 0.0, 'idbins': info.get('idbins', []),
            }
        groups[cliente][key]['kg'] += kg

    payload_orders = []
    for cliente, line_map in sorted(groups.items()):
        lines = []
        for (ot, tipo, serie), d in line_map.items():
            note = f"OT {ot}" + (f" · {serie}" if serie and not CALIBER_RE.search(serie) else '')
            lines.append({
                'caliber': d['caliber'], 'drying': d['drying'],
                'product_type': d['product_type'],
                'target_kg': round(d['kg'], 1),
                'temporada': d['temporada'],
                'notes': note,
                'bin_identifiers': d['idbins'],
            })
        payload_orders.append({'customer': cliente, 'lines': lines})

    print(f'Phase 1: {len(payload_orders)} orders · '
          f'{sum(len(o["lines"]) for o in payload_orders)} lines')

    if dry_run:
        print(json.dumps(payload_orders[:1], indent=2, ensure_ascii=False))
        return

    result = post_json('/admin/import-orders',
                       {'passcode': PASSCODE, 'orders': payload_orders})
    print(f'  ✓ Creadas: {result["created"]}  Omitidas: {result["skipped"]}')
    if result.get('errors'):
        print(f'  Errores: {result["errors"][:5]}')


# ── Phase 2: Create pallet-bins and allocate ──────────────────────────────────

def phase2(circ, ot_info, dry_run):
    records = []
    for _, row in circ.iterrows():
        ot      = str(row['OT']).strip()
        tarja   = str(row['TARJA']).strip()
        cliente = str(row['CLIENTE']).strip()
        tipo    = str(row['TIPOPROCESO']).strip() if pd.notna(row['TIPOPROCESO']) else ''
        serie   = str(row['SERIE']).strip()       if pd.notna(row['SERIE'])       else ''
        kg      = float(row['NETO(kg)'])          if pd.notna(row['NETO(kg)'])    else 0.0
        produto = str(row.get('PRODUCTO', '')).strip()

        info   = ot_info.get(ot, {})
        drying = map_secado(info.get('secado')) or infer_drying(produto)

        records.append({
            'tarja':        tarja,
            'ot':           ot,
            'customer':     cliente,
            'caliber':      serie if CALIBER_RE.search(serie) else None,
            'drying':       drying,
            'product_type': map_tipo(tipo),
            'weight_kg':    kg,
            'producto':     produto,
            'temporada':    info.get('temporada'),
        })

    print(f'Phase 2: {len(records)} pallets to import as bins')

    if dry_run:
        print(json.dumps(records[:3], indent=2, ensure_ascii=False))
        return

    result = post_json('/admin/import-pallets',
                       {'passcode': PASSCODE, 'pallets': records},
                       timeout=180)
    print(f'  ✓ Bins añadidos: {result["added"]}  Asignados: {result["allocated"]}')
    print(f'  Duplicados omitidos: {result["skipped_duplicate"]}')
    print(f'  Sin orden: {result["skipped_no_order"]}  Sin línea: {result["skipped_no_line"]}')
    if result.get('errors'):
        print(f'  Primeros errores: {result["errors"][:5]}')


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    dry_run = '--dry-run' in sys.argv
    do_p1   = '--phase2' not in sys.argv
    do_p2   = '--phase1' not in sys.argv

    circ, ot_info = load_excel()

    if do_p1:
        phase1(circ, ot_info, dry_run)
        print()
    if do_p2:
        phase2(circ, ot_info, dry_run)


if __name__ == '__main__':
    main()
