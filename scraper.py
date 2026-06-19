"""
pWarehouse8 (whCLI V8) integration.

Logs in, paginates through all bins, and returns those where
the PRODUCTO column contains "CIRUELA" (case-insensitive).

Environment variables (set in Railway):
  PWAREHOUSE_URL   — e.g. http://190.211.168.247:8077
  PWAREHOUSE_USER  — your pWarehouse8 username
  PWAREHOUSE_PASS  — your pWarehouse8 password
"""

import json
import os
import re

import requests

# ── Column indices in the O16B "Bins en Bodega" grid ──────────────────────────
COL_TARJA    = 1
COL_NETO     = 2
COL_PRODUCTO = 8
COL_PRODUCTOR = 12
COL_SERIE    = 15
COL_SECADO   = 16

DRYING_MAP = {
    'cancha':        'field',
    'field drying':  'field',
    'field':         'field',
    'horno':         'oven',
    'oven drying':   'oven',
    'oven':          'oven',
    'otro':          'other',
    'other':         'other',
}


def _parse_response(r):
    """
    Parse a pWarehouse8 HTTP response.
    pWarehouse8 returns JavaScript object notation with unquoted keys
    (e.g. {success:true,results:10805,rows:[...]}) which is not valid JSON.
    We quote bare word keys before handing to json.loads.
    """
    text = r.text.strip()
    if not text:
        raise ValueError("pWarehouse8 returned an empty response.")
    if '<html' in text[:100].lower() or '<!doctype' in text[:100].lower():
        raise ValueError(
            "pWarehouse8 returned the login page — session ID may be expired. "
            "Get a fresh _S_ID from your browser and try again."
        )
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass
    # Quote bare keys (including numeric):  success: → "success":  0: → "0":
    fixed = re.sub(r'(?<!["\w])(\w+)\s*:', r'"\1":', text)
    try:
        return json.loads(fixed)
    except (json.JSONDecodeError, ValueError):
        raise ValueError(f"Cannot parse pWarehouse8 response: {text[:120]}")


def _row_val(row, idx):
    """Row dict keys may be int or str depending on JSON serialisation."""
    v = row.get(idx)
    if v is None:
        v = row.get(str(idx))
    return v


def _extract_sid(html):
    """Try several patterns to pull _S_ID out of the initial page HTML."""
    patterns = [
        r"['\"]?_S_ID['\"]?\s*[=:]\s*['\"]([A-Za-z0-9]+)['\"]",
        r"_S_ID=([A-Za-z0-9]+)",
        r"Unisessionid['\"\s]*[:=]\s*['\"]?([A-Za-z0-9]+)",
    ]
    for pat in patterns:
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            return m.group(1)
    return None


def _fp_encode(value):
    """
    whCLI field-value encoding: values in _fp_ are prefixed with space+STX+STX.
    Captured from browser: field value " \x02\x02actualvalue" sent in _fp_.
    """
    return ' \x02\x02' + value


def pwarehouse_login(url=None, rut=None, password=None):
    """
    Open an authenticated session to pWarehouse8.
    Returns (requests.Session, session_id, base_url).

    Login format reverse-engineered from browser Network capture:
      POST /HandleEvent
      Ajax=1&IsEvent=1&Obj=O23&Evt=click&this=O23&_S_ID={sid}
      &_fp_=&O16={enc_rut}&O17={enc_pass}&_seq_=a&_uo_=O0

    Field encoding: each value is prefixed with " \\x02\\x02" before URL-encoding.
    """
    base_url = (url or os.environ.get('PWAREHOUSE_URL', '')).rstrip('/')
    rut      = rut      or os.environ.get('PWAREHOUSE_RUT',  '')
    password = password or os.environ.get('PWAREHOUSE_PASS', '')

    if not base_url:
        raise ValueError("PWAREHOUSE_URL is not configured.")

    sess = requests.Session()
    sess.headers.update({'User-Agent': 'Mozilla/5.0 (compatible; Goodvalley-Sync/1.0)'})

    # 1) GET initial page — _S_ID is embedded in the HTML
    try:
        r = sess.get(base_url + '/', timeout=30)
        r.raise_for_status()
    except requests.exceptions.ConnectionError:
        raise ValueError(
            f"Cannot reach {base_url}. "
            "Check that the pWarehouse8 server is online and this Railway service can access it."
        )
    except requests.exceptions.Timeout:
        raise ValueError(f"Connection to {base_url} timed out.")

    sid = _extract_sid(r.text)
    if not sid:
        raise ValueError(
            "Could not find _S_ID in the pWarehouse8 login page. "
            "The page structure may have changed — contact support."
        )

    # 2) POST login button click (Obj=O23, Evt=click).
    #    _S_ID goes in the POST body (not header).
    #    _fp_ contains RUT (O16) and password (O17), each prefixed with space+STX+STX.
    fp = '&O16=' + _fp_encode(rut) + '&O17=' + _fp_encode(password)

    login_r = sess.post(
        base_url + '/HandleEvent',
        data={
            'Ajax':    '1',
            'IsEvent': '1',
            'Obj':     'O23',
            'Evt':     'click',
            'this':    'O23',
            '_S_ID':   sid,
            '_fp_':    fp,
            '_seq_':   'a',
            '_uo_':    'O0',
        },
        timeout=30,
    )
    login_r.raise_for_status()

    # pWarehouse8 may return {success:false} or an HTML redirect on bad credentials
    try:
        result = _parse_response(login_r)
        if result.get('success') is False:
            raise ValueError(
                "pWarehouse8 rejected the login — check your RUT and password."
            )
    except ValueError:
        raise
    except Exception:
        pass  # Non-JSON / HTML response after login is treated as success

    return sess, sid, base_url


def fetch_ciruela_bins(sess, sid, base_url, page_size=2000):
    """
    Fetch all bins from pWarehouse8 and return those where
    PRODUCTO contains 'CIRUELA'.

    Returns a list of dicts:
      bin_identifier, producer_name, weight_kg,
      drying_method, caliber_low, caliber_high, producto
    """
    handle_url = base_url + '/HandleEvent'
    all_rows   = []
    start      = 0
    total      = None

    while True:
        page = start // page_size + 1
        params = {
            'IsEvent': '1',
            'Obj':     'O16B',
            'Evt':     'data',
            'options': '1',
            'page':    str(page),
            'start':   str(start),
            'limit':   str(page_size),
            '_S_ID':   sid,
        }
        r = sess.get(handle_url, params=params, timeout=90)
        r.raise_for_status()
        data = _parse_response(r)

        if not data.get('success'):
            raise ValueError(f"pWarehouse8 data endpoint returned failure: {data}")

        rows = data.get('rows', [])
        if not rows:
            break

        all_rows.extend(rows)

        if total is None:
            total = int(data.get('results', len(rows)))

        if start + page_size >= total:
            break
        start += page_size

    # Filter to ciruelas and map to Bin fields
    bins = []
    for row in all_rows:
        producto = str(_row_val(row, COL_PRODUCTO) or '').strip().upper()
        if 'CIRUELA' not in producto:
            continue

        tarja = _row_val(row, COL_TARJA)
        if tarja is None:
            continue
        # Excel often delivers integers as floats (e.g. 23013023260.0)
        if isinstance(tarja, float):
            tarja_str = str(int(tarja))
        else:
            tarja_str = str(tarja).strip()

        neto = _row_val(row, COL_NETO)
        try:
            weight = float(neto) if neto is not None else 0.0
        except (ValueError, TypeError):
            weight = 0.0

        productor = _row_val(row, COL_PRODUCTOR)

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

        secado   = _row_val(row, COL_SECADO)
        drying   = DRYING_MAP.get(str(secado).lower().strip()) if secado else None
        if not drying:
            continue  # Skip rows whose drying method we can't map

        bins.append({
            'bin_identifier': tarja_str,
            'producer_name':  str(productor).strip() if productor else '',
            'weight_kg':      weight,
            'drying_method':  drying,
            'caliber_low':    cal_low,
            'caliber_high':   cal_high,
            'producto':       producto,
        })

    return bins, total or 0
