#!/usr/bin/env python3
"""
Goodvalley — Local Sync Script
Run this from any computer with internet access to import ciruela bins
from pWarehouse8 into the Goodvalley Railway database.

Requirements (one-time install):
    pip install requests psycopg2-binary

Usage:
    python3 sync_local.py
"""

import json
import os
import re
import sys

# ── Configuration ──────────────────────────────────────────────────────────────
# Fill these in, or set them as environment variables.

PWAREHOUSE_URL  = os.environ.get('PWAREHOUSE_URL',  'http://190.211.168.247:8077')
PWAREHOUSE_RUT  = os.environ.get('PWAREHOUSE_RUT',  '')   # your pWarehouse8 RUT
PWAREHOUSE_PASS = os.environ.get('PWAREHOUSE_PASS', '')   # your pWarehouse8 password

# Copy from Railway → your project → Postgres service → Connect tab → DATABASE_URL
DATABASE_URL    = os.environ.get('DATABASE_URL',    '')

# ── Column indices in the O16B grid ───────────────────────────────────────────
COL_TARJA     = 1
COL_NETO      = 2
COL_PRODUCTO  = 8
COL_PRODUCTOR = 12
COL_SERIE     = 15
COL_SECADO    = 16

DRYING_MAP = {
    'cancha': 'field', 'field drying': 'field', 'field': 'field',
    'horno':  'oven',  'oven drying':  'oven',  'oven':  'oven',
    'otro':   'other', 'other':        'other',
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _row_val(row, idx):
    return row.get(idx) or row.get(str(idx))


def _parse_js(text):
    """Parse pWarehouse8's JavaScript-style JSON (unquoted keys)."""
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


def _fp_encode(value):
    """whCLI field-value encoding (space + STX + STX prefix)."""
    return ' \x02\x02' + value


# ── pWarehouse8 ───────────────────────────────────────────────────────────────

def pw_login(url, rut, password):
    import requests
    sess = requests.Session()
    sess.headers['User-Agent'] = 'Mozilla/5.0 (compatible; Goodvalley-Sync/1.0)'

    print(f"  Connecting to {url} ...")
    r = sess.get(url + '/', timeout=30)
    r.raise_for_status()

    sid = _extract_sid(r.text)
    if not sid:
        raise RuntimeError("Could not find _S_ID in pWarehouse8 login page.")
    print(f"  Session ID: {sid}")

    fp = '&O16=' + _fp_encode(rut) + '&O17=' + _fp_encode(password)
    lr = sess.post(url + '/HandleEvent', data={
        'Ajax': '1', 'IsEvent': '1', 'Obj': 'O23', 'Evt': 'click',
        'this': 'O23', '_S_ID': sid, '_fp_': fp, '_seq_': 'a', '_uo_': 'O0',
    }, timeout=30)
    lr.raise_for_status()

    try:
        result = _parse_js(lr.text)
        if result.get('success') is False:
            raise RuntimeError("pWarehouse8 rejected the login — check your RUT and password.")
    except (json.JSONDecodeError, ValueError):
        pass  # non-JSON response after login is fine

    print("  Login OK.")
    return sess, sid


def pw_fetch_ciruelas(sess, sid, url, page_size=2000):
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
            raise RuntimeError(f"Data fetch failed: {r.text[:200]}")

        rows = data.get('rows', [])
        if not rows:
            break

        all_rows.extend(rows)
        if total is None:
            total = int(data.get('results', len(rows)))

        fetched = min(start + page_size, total)
        print(f"  Fetched {fetched:,} / {total:,} rows ...", end='\r')

        if start + page_size >= total:
            break
        start += page_size

    print()

    bins = []
    for row in all_rows:
        producto = str(_row_val(row, COL_PRODUCTO) or '').upper()
        if 'CIRUELA' not in producto:
            continue

        tarja = _row_val(row, COL_TARJA)
        if tarja is None:
            continue
        tarja_str = str(int(float(tarja))) if isinstance(tarja, (int, float)) else str(tarja).strip()

        neto = _row_val(row, COL_NETO)
        try:
            weight = float(neto) if neto is not None else 0.0
        except (ValueError, TypeError):
            weight = 0.0

        cal_low = cal_high = None
        serie = _row_val(row, COL_SERIE)
        if serie:
            s = str(serie).strip().upper()
            if '/' in s:
                try:
                    lo, hi = s.split('/', 1)
                    cal_low, cal_high = int(lo.strip()), int(hi.strip())
                except (ValueError, TypeError):
                    pass

        secado = _row_val(row, COL_SECADO)
        drying = DRYING_MAP.get(str(secado).lower().strip()) if secado else None
        if not drying:
            continue

        productor = _row_val(row, COL_PRODUCTOR)
        bins.append({
            'bin_identifier': tarja_str,
            'producer_name':  str(productor).strip() if productor else '',
            'weight_kg':      weight,
            'drying_method':  drying,
            'caliber_low':    cal_low,
            'caliber_high':   cal_high,
        })

    return bins, total or 0


# ── Database ──────────────────────────────────────────────────────────────────

def db_import(bins, db_url):
    import psycopg2
    db_url = db_url.replace('postgres://', 'postgresql://', 1)
    print(f"  Connecting to Railway database ...")
    conn = psycopg2.connect(db_url)
    cur  = conn.cursor()

    added = skipped = 0
    for b in bins:
        cur.execute("SELECT 1 FROM bins WHERE bin_identifier = %s", (b['bin_identifier'],))
        if cur.fetchone():
            skipped += 1
            continue
        cur.execute("""
            INSERT INTO bins
              (bin_identifier, producer_name, weight_kg,
               drying_method, caliber_low, caliber_high, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, NOW())
        """, (
            b['bin_identifier'], b['producer_name'], b['weight_kg'],
            b['drying_method'], b['caliber_low'], b['caliber_high'],
        ))
        added += 1

    conn.commit()
    cur.close()
    conn.close()
    return added, skipped


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    rut      = PWAREHOUSE_RUT
    password = PWAREHOUSE_PASS
    db_url   = DATABASE_URL

    # Prompt for any missing values
    if not rut:
        rut = input("pWarehouse8 RUT: ").strip()
    if not password:
        import getpass
        password = getpass.getpass("pWarehouse8 password: ")
    if not db_url:
        print("\nPaste your Railway DATABASE_URL (Railway → Postgres → Connect tab):")
        db_url = input("> ").strip()

    print("\n[1/3] Logging into pWarehouse8 ...")
    sess, sid = pw_login(PWAREHOUSE_URL, rut, password)

    print("\n[2/3] Fetching ciruela bins ...")
    bins, total = pw_fetch_ciruelas(sess, sid, PWAREHOUSE_URL)
    print(f"  Found {len(bins):,} ciruela bins out of {total:,} total.")

    if not bins:
        print("  Nothing to import.")
        return

    print("\n[3/3] Importing into Railway database ...")
    added, skipped = db_import(bins, db_url)

    print(f"\n✓ Done — {added} bins imported, {skipped} already existed.")


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled.")
    except Exception as e:
        print(f"\n✗ Error: {e}")
        sys.exit(1)
