#!/usr/bin/env python3
"""
Read the 3 Excel files on Desktop and create orders in Goodvalley.

Usage:
    python3 import_orders.py
    python3 import_orders.py --dry-run     # print JSON without uploading
"""
import json, sys, re, urllib.request
import pandas as pd
from pathlib import Path

GOODVALLEY_URL = 'https://web-production-2eea96.up.railway.app'
PASSCODE = '001083748'
DESKTOP = Path.home() / 'Desktop'

TIPO_TO_PRODUCT_TYPE = {
    'tsc':              'tsc',
    'tcc':              'tcc',
    'tss':              'tss',
    'elliot':           'elliot',
    'condicion natural':'natural',
}

SECADO_TO_DRYING = {
    'cancha': 'cancha',
    'sol':    'cancha',
    'campo':  'cancha',
    'horno':  'horno',
    'oven':   'horno',
}

CALIBER_RE = re.compile(r'(\d{2,3}/\d{2,3}|\d{2,3}\+)')


def map_product_type(val):
    return TIPO_TO_PRODUCT_TYPE.get(str(val or '').lower().strip())


def map_drying(val):
    return SECADO_TO_DRYING.get(str(val or '').lower().strip())


def infer_drying_from_producto(producto):
    p = (producto or '').upper()
    if 'HORNO' in p:
        return 'horno'
    if 'SOL' in p or 'CANCHA' in p or 'CAMPO' in p:
        return 'cancha'
    if 'TERMINO' in p or 'TERMINADO' in p or 'TERMINADA' in p:
        return 'termino_secado'
    return None


def main():
    dry_run = '--dry-run' in sys.argv

    print('Leyendo archivos Excel...')
    hist   = pd.read_excel(DESKTOP / 'historico para Camilo.xlsx')
    pallet = pd.read_excel(DESKTOP / 'Pallets en bodega.xlsx', header=1)

    # ── Build OT → {bins, secado, temporada} from historico EGRESO A PROCESO ──
    egreso = hist[hist['MOVIMIENTO'] == 'EGRESO A PROCESO'].copy()
    egreso['IDBINS2'] = (
        egreso['IDBINS2'].astype(str).str.strip().str.split('.').str[0]
    )
    egreso = egreso[egreso['IDBINS2'].notna() & (egreso['IDBINS2'] != 'nan')]
    egreso['OT'] = egreso['OT'].astype(str).str.strip()

    ot_info = {}
    for ot, grp in egreso.groupby('OT'):
        secado_vals = grp['SECADO'].dropna()
        temp_vals   = grp['TEMPORADA'].dropna()
        ot_info[ot] = {
            'bins':      sorted(grp['IDBINS2'].unique().tolist()),
            'secado':    secado_vals.mode()[0] if not secado_vals.empty else None,
            'temporada': temp_vals.mode()[0]   if not temp_vals.empty   else None,
        }

    # ── Filter ciruela pallets only ──
    circ = pallet[pallet['PRODUCTO'].str.contains('CIRUELA', na=False)].copy()
    circ['OT'] = circ['OT'].astype(str).str.strip()

    # ── Group into 1 order per CLIENTE ──
    orders = {}
    for _, row in circ.iterrows():
        cliente = str(row['CLIENTE']).strip()
        ot      = str(row['OT']).strip()
        tipo    = str(row['TIPOPROCESO']).strip() if pd.notna(row['TIPOPROCESO']) else ''
        serie   = str(row['SERIE']).strip()       if pd.notna(row['SERIE'])       else ''
        kg      = float(row['NETO(kg)'])          if pd.notna(row['NETO(kg)'])    else 0.0
        produto = str(row['PRODUCTO']).strip()

        if cliente not in orders:
            orders[cliente] = {}
        key = (ot, tipo, serie)
        if key not in orders[cliente]:
            info = ot_info.get(ot, {})
            drying = map_drying(info.get('secado')) or infer_drying_from_producto(produto)
            orders[cliente][key] = {
                'ot':          ot,
                'product_type': map_product_type(tipo),
                'caliber':     serie if CALIBER_RE.search(serie) else None,
                'drying':      drying,
                'temporada':   info.get('temporada'),
                'kg':          0.0,
                'pallets':     0,
                'bins':        info.get('bins', []),
            }
        orders[cliente][key]['kg']      += kg
        orders[cliente][key]['pallets'] += 1

    # ── Build the JSON payload ──
    payload_orders = []
    for cliente, line_map in sorted(orders.items()):
        lines = []
        for (ot, tipo, serie), d in line_map.items():
            lines.append({
                'caliber':         d['caliber'],
                'drying':          d['drying'],
                'product_type':    d['product_type'],
                'target_kg':       round(d['kg'], 1),
                'temporada':       d['temporada'],
                'notes':           f"OT {ot}" + (f" · {serie}" if serie and not CALIBER_RE.search(serie) else ''),
                'bin_identifiers': d['bins'],
            })
        payload_orders.append({
            'customer': cliente,
            'lines':    lines,
        })

    print(f'\n{len(payload_orders)} orders · {sum(len(o["lines"]) for o in payload_orders)} lines total\n')

    if dry_run:
        print(json.dumps(payload_orders[:2], indent=2, ensure_ascii=False))
        print('... (dry-run, not uploading)')
        return

    # ── POST to Goodvalley ──
    body = json.dumps({
        'passcode': PASSCODE,
        'orders':   payload_orders,
    }, ensure_ascii=False).encode()

    print(f'Enviando a {GOODVALLEY_URL}/admin/import-orders ...')
    req = urllib.request.Request(
        f'{GOODVALLEY_URL}/admin/import-orders',
        data=body,
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        result = json.loads(resp.read())

    print(f'\n✓ Creadas: {result["created"]}  Omitidas (ya existían): {result["skipped"]}')
    if result.get('errors'):
        print(f'✗ Errores: {result["errors"]}')

    print('\nDetalle por orden:')
    for o in result.get('orders', []):
        if o.get('skipped'):
            print(f'  SKIP  {o["customer"]}')
            continue
        total_alloc = sum(l['allocated'] for l in o.get('lines', []))
        total_miss  = sum(l['not_found'] for l in o.get('lines', []))
        flag = '⚠' if total_miss else '✓'
        print(f'  {flag} #{o["order_id"]:4d}  {o["customer"]:<35}  '
              f'{len(o["lines"])} líneas  '
              f'{total_alloc} bins asignados  {total_miss} no encontrados')


if __name__ == '__main__':
    main()
