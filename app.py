import os
from datetime import datetime

from flask import Flask, render_template, request, redirect, url_for, flash
from sqlalchemy.exc import IntegrityError

from db import db

PWAREHOUSE_URL  = os.environ.get('PWAREHOUSE_URL',  'http://190.211.168.247:8077')
PWAREHOUSE_RUT  = os.environ.get('PWAREHOUSE_RUT',  '')
PWAREHOUSE_PASS = os.environ.get('PWAREHOUSE_PASS', '')

DRYING_MAP = {
    'cancha': 'cancha', 'cancha de sol': 'cancha', 'sol': 'cancha',
    'campo': 'cancha', 'field': 'cancha', 'field drying': 'cancha',
    'horno': 'horno', 'oven': 'horno', 'oven drying': 'horno',
    'termino secado': 'termino_secado', 'término secado': 'termino_secado',
    'termino_secado': 'termino_secado', 'term. secado': 'termino_secado',
    'term.secado': 'termino_secado',
}


_SYNC_POPUP = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<title>Sync pWarehouse</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0 }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    background: #0f0f1a; color: #e0d4f7;
    display: flex; flex-direction: column; height: 100vh; overflow: hidden;
  }
  #header {
    background: #3b0764; padding: 12px 16px;
    font-size: 14px; font-weight: 600; flex-shrink: 0;
    display: flex; align-items: center; gap: 10px;
  }
  #spinner {
    width: 14px; height: 14px; border: 2px solid rgba(255,255,255,.3);
    border-top-color: #fff; border-radius: 50%;
    animation: spin .7s linear infinite; flex-shrink: 0;
  }
  @keyframes spin { to { transform: rotate(360deg) } }
  #log {
    flex: 1; overflow-y: auto; padding: 12px 16px;
    font-family: 'SF Mono', monospace; font-size: 12px;
    line-height: 1.65; white-space: pre-wrap; color: #d4f0c0;
  }
  #footer {
    padding: 10px 16px; background: #1a1a2e; flex-shrink: 0;
    font-size: 13px; font-weight: 600; min-height: 38px;
  }
  .ok  { color: #6fcf97 }
  .err { color: #eb5757 }
</style>
</head>
<body>
<div id="header">
  <div id="spinner"></div>
  <span id="title">Sincronizando pWarehouse…</span>
</div>
<div id="log"></div>
<div id="footer"></div>
<script>
(function() {
  const log    = document.getElementById('log');
  const footer = document.getElementById('footer');
  const title  = document.getElementById('title');
  const spinner = document.getElementById('spinner');

  const es = new EventSource('/sync/live');

  es.onmessage = function(e) {
    if (e.data.startsWith('__DONE__')) {
      es.close();
      spinner.style.display = 'none';
      const ok = parseInt(e.data.replace('__DONE__', '')) === 0;
      if (ok) {
        title.textContent = '✓ Sincronización completada';
        footer.innerHTML = '<span class="ok">✓ Listo — cerrando en 2 segundos…</span>';
        setTimeout(function() {
          try { if (window.opener) window.opener.postMessage('gv_sync_done', '*'); } catch(_) {}
          window.close();
        }, 2000);
      } else {
        title.textContent = 'Error al sincronizar';
        footer.innerHTML = '<span class="err">✗ Error — revisá el log arriba.</span>';
      }
    } else {
      log.textContent += e.data + '\\n';
      log.scrollTop = log.scrollHeight;
    }
  };

  es.onerror = function() {
    es.close();
    spinner.style.display = 'none';
    title.textContent = 'Error de conexión';
    footer.innerHTML = '<span class="err">✗ No se pudo conectar al servidor.</span>';
  };
})();
</script>
</body>
</html>"""


def create_app():
    app = Flask(__name__)

    db_url = os.environ.get('DATABASE_URL', 'postgresql://localhost/goodvalley')
    if db_url.startswith('postgres://'):
        db_url = db_url.replace('postgres://', 'postgresql://', 1)

    app.config['SQLALCHEMY_DATABASE_URI'] = db_url
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-change-me')

    db.init_app(app)

    with app.app_context():
        from models import Bin, Order, OrderLine, Allocation, Excedente  # noqa: F401
        db.create_all()

        _migrate(db)

    # ── Routes ────────────────────────────────────────────────────────────────

    @app.route('/')
    def index():
        from models import Bin, Order, Allocation
        from sqlalchemy import func

        total   = Bin.query.count()
        avail   = Bin.query.filter_by(status='available').count()
        kg      = db.session.query(func.sum(Bin.weight_kg)).filter_by(status='available').scalar() or 0
        n_open  = Order.query.filter(Order.status.in_(['open', 'confirmed'])).count()
        alloc_n = Allocation.query.count()

        # Summary by caliber + drying (available bins only)
        rows = (
            db.session.query(
                Bin.caliber, Bin.drying,
                func.count(Bin.id).label('cnt'),
                func.sum(Bin.weight_kg).label('kg'),
            )
            .filter_by(status='available')
            .filter(Bin.caliber.isnot(None))
            .group_by(Bin.caliber, Bin.drying)
            .order_by(Bin.caliber, Bin.drying)
            .all()
        )

        from models import DRYING_LABELS
        return render_template('index.html',
            total=total, avail=avail, kg=round(kg, 1),
            n_open=n_open, alloc_n=alloc_n, summary_rows=rows,
            DRYING_LABELS=DRYING_LABELS,
        )

    # ── Sync ──────────────────────────────────────────────────────────────────

    @app.route('/sync-popup')
    def sync_popup():
        from flask import make_response
        resp = make_response(_SYNC_POPUP)
        resp.headers['Content-Type'] = 'text/html; charset=utf-8'
        return resp

    @app.route('/sync/live')
    def sync_live():
        import subprocess, sys, json as _json
        from pathlib import Path as _Path
        from flask import Response, stream_with_context
        from models import Bin

        scraper     = _Path(__file__).parent / 'scrape_pwarehouse.py'
        output_file = _Path('/tmp/gv_bins_scraped.json')

        def _temporada(t):
            if len(t) >= 8:
                try:
                    p = int(t[:2])
                    if 18 <= p <= 35:
                        return str(2000 + p)
                except ValueError:
                    pass
            return None

        def generate():
            env = {**os.environ, 'GV_NO_UPLOAD': '1', 'GV_OUTPUT': str(output_file)}
            proc = subprocess.Popen(
                [sys.executable, str(scraper)],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, env=env,
            )
            for line in proc.stdout:
                yield f'data: {line.rstrip()}\n\n'
            proc.wait()

            if proc.returncode != 0:
                yield f'data: __DONE__{proc.returncode}\n\n'
                return

            if not output_file.exists():
                yield 'data: ✗ No se encontró el archivo de bins.\n\n'
                yield 'data: __DONE__1\n\n'
                return

            yield 'data: ▶ Importando bins a la base de datos...\n\n'
            try:
                bins_data = _json.loads(output_file.read_text())

                existing_map = {
                    row[0]: row[1] for row in
                    db.session.query(Bin.bin_identifier, Bin.id)
                    .filter(Bin.status == 'available').all()
                }
                all_ids = {row[0] for row in db.session.query(Bin.bin_identifier).all()}

                added = updated = skipped = 0
                new_batch = []

                for b in bins_data:
                    bid = str(b.get('bin_identifier', '')).strip()
                    if not bid:
                        skipped += 1; continue
                    drying = b.get('drying') or ''
                    if drying not in ('cancha', 'horno', 'termino_secado'):
                        skipped += 1; continue

                    weight = float(b.get('weight_kg') or 0)

                    if bid in existing_map:
                        db.session.query(Bin).filter_by(id=existing_map[bid]).update({
                            'weight_kg': weight,
                            'humedad': b.get('humedad'),
                            'caliber': b.get('caliber') or '',
                            'drying': drying,
                            'producto': b.get('producto') or '',
                            'contenedor': b.get('contenedor') or '',
                            'producer_name': b.get('producer_name') or '',
                            'temporada': b.get('temporada') or _temporada(bid),
                        }, synchronize_session=False)
                        updated += 1
                    elif bid not in all_ids:
                        new_batch.append(Bin(
                            bin_identifier=bid,
                            producto=b.get('producto') or '',
                            caliber=b.get('caliber') or '',
                            drying=drying,
                            weight_kg=weight,
                            humedad=b.get('humedad'),
                            contenedor=b.get('contenedor') or '',
                            producer_name=b.get('producer_name') or '',
                            temporada=b.get('temporada') or _temporada(bid),
                            status='available',
                        ))
                        all_ids.add(bid)
                        added += 1
                        if len(new_batch) >= 500:
                            db.session.bulk_save_objects(new_batch)
                            db.session.commit()
                            new_batch = []
                    else:
                        skipped += 1

                if new_batch:
                    db.session.bulk_save_objects(new_batch)
                db.session.commit()

                yield f'data: ✓ {added} nuevos, {updated} actualizados, {skipped} omitidos.\n\n'
                yield 'data: __DONE__0\n\n'

            except Exception as e:
                db.session.rollback()
                yield f'data: ✗ Error importando: {e}\n\n'
                yield 'data: __DONE__1\n\n'

        return Response(
            stream_with_context(generate()),
            content_type='text/event-stream',
            headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
        )

    @app.route('/sync', methods=['POST'])
    def sync():
        import json as _json, os as _os, re as _re
        from models import Bin, _parse_producto

        _DRYING_MAP = dict(DRYING_MAP)

        def _parse_js(text):
            try:
                return _json.loads(text)
            except Exception:
                pass
            return _json.loads(_re.sub(r'(?<!["\w])(\w+)\s*:', r'"\1":', text))

        def _rv(row, i):
            return row.get(i) or row.get(str(i))

        def _temporada(t):
            if len(t) >= 8:
                try:
                    p = int(t[:2])
                    if 18 <= p <= 35:
                        return str(2000 + p)
                except ValueError:
                    pass
            return None

        def _do_import(bins_data):
            added = skipped = 0
            for b in bins_data:
                bid = str(b.get('bin_identifier', '')).strip()
                if not bid:
                    skipped += 1
                    continue
                exists = Bin.query.filter_by(bin_identifier=bid).first()
                if exists:
                    skipped += 1
                    continue
                drying = b.get('drying') or b.get('drying_method', '')
                if drying not in ('cancha', 'horno', 'termino_secado'):
                    skipped += 1
                    continue
                db.session.add(Bin(
                    bin_identifier=bid,
                    producto=b.get('producto') or b.get('product') or '',
                    caliber=b.get('caliber') or b.get('caliber_str') or '',
                    drying=drying,
                    weight_kg=float(b.get('weight_kg') or 0),
                    humedad=b.get('humedad') or b.get('humidity'),
                    contenedor=b.get('contenedor') or b.get('container', ''),
                    producer_name=b.get('producer_name', ''),
                    temporada=b.get('temporada') or _temporada(bid),
                    status='available',
                ))
                added += 1
            db.session.commit()
            return added, skipped

        live = request.form.get('live') == '1'

        try:
            if not live:
                dump_path = _os.path.join(_os.path.dirname(__file__), 'data_dump', 'bins.json')
                if not _os.path.exists(dump_path):
                    flash('data_dump/bins.json no encontrado. Coloca el archivo y vuelve a hacer deploy.', 'err')
                    return redirect(url_for('index'))
                with open(dump_path) as f:
                    bins_data = _json.load(f)
                added, skipped = _do_import(bins_data)
                flash(f'Sync (archivo) completo: {added} importados, {skipped} ya existían.', 'ok')
                return redirect(url_for('index'))

            # — Live sync from pWarehouse8 —
            import requests as _req
            url = PWAREHOUSE_URL.rstrip('/')
            rut = PWAREHOUSE_RUT
            pw  = PWAREHOUSE_PASS
            if not rut or not pw:
                flash('Configurá PWAREHOUSE_RUT y PWAREHOUSE_PASS en las variables de Railway.', 'err')
                return redirect(url_for('index'))

            sess = _req.Session()
            sess.headers['User-Agent'] = 'Mozilla/5.0 (compatible; Goodvalley-Sync/1.0)'
            r = sess.get(url + '/', timeout=30)
            r.raise_for_status()

            sid = None
            for pat in [
                r"['\"]?_S_ID['\"]?\s*[=:]\s*['\"]([A-Za-z0-9]+)['\"]",
                r"_S_ID=([A-Za-z0-9]+)",
            ]:
                m = _re.search(pat, r.text, _re.IGNORECASE)
                if m:
                    sid = m.group(1)
                    break
            if not sid:
                raise ValueError("No se encontró _S_ID en la página de pWarehouse8.")

            # _fp_ must be EMPTY; O16/O17 are sent as separate POST fields
            sess.post(url + '/HandleEvent', data={
                'Ajax': '1', 'IsEvent': '1', 'Obj': 'O23', 'Evt': 'click',
                'this': 'O23', '_S_ID': sid, '_fp_': '',
                'O16': ' \x02\x02' + rut,
                'O17': ' \x02\x02' + pw,
                '_seq_': 'a', '_uo_': 'O0',
            }, timeout=30)

            all_rows, start, total, page_size = [], 0, None, 2000
            while True:
                page = start // page_size + 1
                dr = sess.get(url + '/HandleEvent', params={
                    'IsEvent': '1', 'Obj': 'O16B', 'Evt': 'data',
                    'options': '1', 'page': str(page),
                    'start': str(start), 'limit': str(page_size), '_S_ID': sid,
                }, timeout=90)
                dr.raise_for_status()
                raw = dr.text.strip()
                if raw in ('{[]}', '{}', '[]', ''):
                    raise ValueError(
                        "pWarehouse8 bloqueó la conexión desde Railway (respondió vacío). "
                        "Ejecutá sync_local.py desde tu Mac y después subí el bins.json "
                        "con el botón '📤 Subir JSON'."
                    )
                data = _parse_js(raw)
                rows = data.get('rows', [])
                if not rows:
                    break
                all_rows.extend(rows)
                if total is None:
                    total = int(data.get('results', len(rows)))
                if start + page_size >= total:
                    break
                start += page_size

            bins_data = []
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
                except Exception:
                    weight = 0.0

                # Caliber from SERIE col or PRODUCTO
                caliber = None
                serie = _rv(row, 15)
                if serie:
                    s = str(serie).strip()
                    import re as _reb
                    m = _reb.search(r'(\d{2,3}/\d{2,3}|\d{2,3}\+)', s)
                    if m:
                        caliber = m.group(1)

                # Drying from SECADO col or PRODUCTO
                secado = _rv(row, 16)
                drying = _DRYING_MAP.get(str(secado).lower().strip()) if secado else None

                # Fallback: parse both from PRODUCTO
                cal_p, dry_p = _parse_producto(producto)
                if not caliber:
                    caliber = cal_p
                if not drying:
                    drying = dry_p

                if not drying:
                    continue

                productor = _rv(row, 12)
                bins_data.append({
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

            added, skipped = _do_import(bins_data)
            flash(f'Sync en vivo completo: {added} importados, {skipped} ya existían.', 'ok')

        except Exception as e:
            db.session.rollback()
            flash(f'Sync falló: {e}', 'err')

        return redirect(url_for('index'))

    @app.route('/sync/upload', methods=['POST'])
    def sync_upload():
        import json as _json
        from models import Bin

        def _temporada(t):
            if len(t) >= 8:
                try:
                    p = int(t[:2])
                    if 18 <= p <= 35:
                        return str(2000 + p)
                except ValueError:
                    pass
            return None

        f = request.files.get('bins_file')
        if not f or not f.filename:
            flash('No se seleccionó ningún archivo.', 'err')
            return redirect(url_for('index'))

        try:
            bins_data = _json.load(f)
        except Exception as e:
            flash(f'El archivo no es JSON válido: {e}', 'err')
            return redirect(url_for('index'))

        try:
            # Load existing available bins into a dict keyed by identifier
            existing_map = {
                row[0]: row[1] for row in
                db.session.query(Bin.bin_identifier, Bin.id)
                .filter(Bin.status == 'available').all()
            }
            # Also track all identifiers (including allocated/shipped) to avoid re-adding
            all_ids = {
                row[0] for row in db.session.query(Bin.bin_identifier).all()
            }

            added = updated = skipped = 0
            new_batch = []
            incoming_ids = set()

            for b in bins_data:
                bid = str(b.get('bin_identifier', '')).strip()
                if not bid:
                    skipped += 1
                    continue
                drying = b.get('drying') or ''
                if drying not in ('cancha', 'horno', 'termino_secado'):
                    skipped += 1
                    continue

                incoming_ids.add(bid)
                weight = float(b.get('weight_kg') or 0)

                if bid in existing_map:
                    # Update available bin with fresh data from pWarehouse
                    db.session.query(Bin).filter_by(id=existing_map[bid]).update({
                        'weight_kg': weight,
                        'humedad': b.get('humedad'),
                        'caliber': b.get('caliber') or '',
                        'drying': drying,
                        'producto': b.get('producto') or '',
                        'contenedor': b.get('contenedor') or '',
                        'producer_name': b.get('producer_name') or '',
                        'temporada': b.get('temporada') or _temporada(bid),
                    }, synchronize_session=False)
                    updated += 1
                elif bid not in all_ids:
                    new_batch.append(Bin(
                        bin_identifier=bid,
                        producto=b.get('producto') or '',
                        caliber=b.get('caliber') or '',
                        drying=drying,
                        weight_kg=weight,
                        humedad=b.get('humedad'),
                        contenedor=b.get('contenedor') or '',
                        producer_name=b.get('producer_name') or '',
                        temporada=b.get('temporada') or _temporada(bid),
                        status='available',
                    ))
                    all_ids.add(bid)
                    added += 1
                    if len(new_batch) >= 500:
                        db.session.bulk_save_objects(new_batch)
                        db.session.commit()
                        new_batch = []
                else:
                    skipped += 1

            if new_batch:
                db.session.bulk_save_objects(new_batch)
            db.session.commit()

            flash(f'Sync completo: {added} nuevos, {updated} actualizados, {skipped} omitidos.', 'ok')
        except Exception as e:
            db.session.rollback()
            flash(f'Error al importar: {e}', 'err')

        return redirect(url_for('index'))

    # ── Bins ──────────────────────────────────────────────────────────────────

    @app.route('/bins')
    def list_bins():
        from models import Bin, CALIBER_OPTIONS, DRYING_LABELS
        from sqlalchemy import func

        q_caliber  = request.args.get('caliber', '')
        q_drying   = request.args.get('drying', '')
        q_status   = request.args.get('status', '')
        q_temporada = request.args.get('temporada', '')
        q_grower   = request.args.get('grower', '')
        q_text     = request.args.get('q', '').strip()

        query = Bin.query

        if q_caliber:
            query = query.filter(Bin.caliber == q_caliber)
        if q_drying:
            query = query.filter(Bin.drying == q_drying)
        if q_status:
            query = query.filter(Bin.status == q_status)
        if q_temporada:
            query = query.filter(Bin.temporada == q_temporada)
        if q_grower:
            query = query.filter(Bin.producer_name == q_grower)
        if q_text:
            like = f'%{q_text}%'
            query = query.filter(
                db.or_(Bin.bin_identifier.ilike(like), Bin.producto.ilike(like))
            )

        total_count = query.count()
        total_kg    = db.session.query(func.sum(Bin.weight_kg)).filter(
            *([Bin.caliber == q_caliber]  if q_caliber  else []),
            *([Bin.drying == q_drying]    if q_drying   else []),
            *([Bin.status == q_status]    if q_status   else []),
            *([Bin.temporada == q_temporada] if q_temporada else []),
            *([Bin.producer_name == q_grower] if q_grower else []),
        ).scalar() or 0

        bins = query.order_by(Bin.bin_identifier).limit(500).all()

        # Filter options
        growers = [
            r[0] for r in
            db.session.query(Bin.producer_name)
            .filter(Bin.producer_name != '')
            .distinct()
            .order_by(Bin.producer_name)
            .all()
        ]
        temporadas = [
            r[0] for r in
            db.session.query(Bin.temporada)
            .filter(Bin.temporada.isnot(None))
            .distinct()
            .order_by(Bin.temporada)
            .all()
        ]

        return render_template('bins/list.html',
            bins=bins,
            total_count=total_count,
            total_kg=round(total_kg, 1),
            CALIBER_OPTIONS=CALIBER_OPTIONS,
            DRYING_LABELS=DRYING_LABELS,
            growers=growers,
            temporadas=temporadas,
            q_caliber=q_caliber, q_drying=q_drying, q_status=q_status,
            q_temporada=q_temporada, q_grower=q_grower, q_text=q_text,
        )

    # ── Orders ────────────────────────────────────────────────────────────────

    @app.route('/orders')
    def list_orders():
        from models import Order
        orders = Order.query.order_by(Order.created_at.desc()).all()
        return render_template('orders/list.html', orders=orders)

    @app.route('/orders', methods=['POST'])
    def create_order():
        from models import Order, OrderLine

        customer  = request.form.get('customer', '').strip()
        reference = request.form.get('reference', '').strip() or None
        notes     = request.form.get('notes', '').strip() or None

        if not customer:
            flash('El nombre del cliente es obligatorio.', 'err')
            return redirect(url_for('list_orders'))

        caliber     = request.form.get('caliber')   or None
        drying      = request.form.get('drying')    or None
        target_kg   = request.form.get('target_kg', '0')
        max_humedad = request.form.get('max_humedad') or None
        temporada   = request.form.get('temporada')  or None
        pitted      = bool(request.form.get('pitted'))
        line_notes  = request.form.get('line_notes', '').strip() or None

        try:
            tkg = float(target_kg)
        except (ValueError, TypeError):
            tkg = 0.0

        order = Order(customer=customer, reference=reference, notes=notes)
        db.session.add(order)
        db.session.flush()

        line = OrderLine(
            order_id=order.id, caliber=caliber, drying=drying,
            target_kg=tkg,
            max_humedad=float(max_humedad) if max_humedad else None,
            temporada=temporada, pitted=pitted, notes=line_notes,
        )
        db.session.add(line)
        db.session.commit()
        flash(f'Orden #{order.id} creada.', 'ok')
        return redirect(url_for('order_detail', order_id=order.id))

    @app.route('/orders/new')
    def new_order():
        from models import CALIBER_OPTIONS, DRYING_LABELS
        return render_template('orders/new.html',
            CALIBER_OPTIONS=CALIBER_OPTIONS, DRYING_LABELS=DRYING_LABELS)

    @app.route('/orders/<int:order_id>')
    def order_detail(order_id):
        from models import Order, Bin, CALIBER_OPTIONS, DRYING_LABELS, Allocation, Excedente

        order = Order.query.get_or_404(order_id)

        search_line_id = request.args.get('search_line', type=int)
        search_bins = []
        search_excedentes = []
        if search_line_id and order.status in ('open', 'confirmed'):
            line = next((l for l in order.lines if l.id == search_line_id), None)
            if line:
                allocated_bin_ids = {
                    a.bin_id for a in
                    Allocation.query.filter(Allocation.bin_id.isnot(None)).all()
                }
                q = Bin.query.filter_by(status='available')
                caliber_f = request.args.get('caliber_f') or line.caliber
                if caliber_f:
                    q = q.filter(Bin.caliber == caliber_f)
                if line.drying:
                    q = q.filter(Bin.drying == line.drying)
                if line.temporada:
                    q = q.filter(Bin.temporada == line.temporada)
                if line.max_humedad:
                    q = q.filter(
                        db.or_(Bin.humedad.is_(None), Bin.humedad <= line.max_humedad)
                    )
                if allocated_bin_ids:
                    q = q.filter(Bin.id.notin_(allocated_bin_ids))
                search_bins = q.order_by(Bin.bin_identifier).limit(200).all()

                allocated_surplus_ids = {
                    a.surplus_id for a in
                    Allocation.query.filter(Allocation.surplus_id.isnot(None)).all()
                }
                eq = Excedente.query.filter_by(status='available')
                if caliber_f:
                    eq = eq.filter(Excedente.caliber == caliber_f)
                if line.drying:
                    eq = eq.filter(Excedente.drying == line.drying)
                if line.temporada:
                    eq = eq.filter(Excedente.temporada == line.temporada)
                if allocated_surplus_ids:
                    eq = eq.filter(Excedente.id.notin_(allocated_surplus_ids))
                search_excedentes = eq.order_by(Excedente.created_at.desc()).all()

        return render_template('orders/detail.html',
            order=order,
            search_bins=search_bins,
            search_excedentes=search_excedentes,
            search_line_id=search_line_id,
            CALIBER_OPTIONS=CALIBER_OPTIONS,
            DRYING_LABELS=DRYING_LABELS,
        )

    @app.route('/orders/<int:order_id>/lines', methods=['POST'])
    def add_line(order_id):
        from models import Order, OrderLine

        order = Order.query.get_or_404(order_id)
        if order.status in ('fulfilled', 'cancelled'):
            flash('No se pueden agregar líneas a una orden cerrada.', 'err')
            return redirect(url_for('order_detail', order_id=order_id))

        caliber     = request.form.get('caliber')    or None
        drying      = request.form.get('drying')     or None
        target_kg   = request.form.get('target_kg',  '0')
        max_humedad = request.form.get('max_humedad') or None
        temporada   = request.form.get('temporada')  or None
        pitted      = bool(request.form.get('pitted'))
        notes       = request.form.get('notes', '').strip() or None

        try:
            tkg = float(target_kg)
        except (ValueError, TypeError):
            tkg = 0.0

        line = OrderLine(
            order_id=order_id, caliber=caliber, drying=drying,
            target_kg=tkg,
            max_humedad=float(max_humedad) if max_humedad else None,
            temporada=temporada, pitted=pitted, notes=notes,
        )
        db.session.add(line)
        db.session.commit()
        flash('Línea agregada.', 'ok')
        return redirect(url_for('order_detail', order_id=order_id))

    @app.route('/orders/<int:order_id>/lines/<int:line_id>/allocate', methods=['POST'])
    def allocate_bins(order_id, line_id):
        from models import Order, OrderLine, Allocation, Bin, Excedente

        order = Order.query.get_or_404(order_id)
        line  = OrderLine.query.get_or_404(line_id)

        if order.status in ('fulfilled', 'cancelled'):
            flash('No se pueden asignar bins a una orden cerrada.', 'err')
            return redirect(url_for('order_detail', order_id=order_id))

        bin_ids     = request.form.getlist('bin_ids')
        surplus_ids = request.form.getlist('surplus_ids')

        if not bin_ids and not surplus_ids:
            flash('Seleccioná al menos un bin o excedente.', 'err')
            return redirect(url_for('order_detail', order_id=order_id,
                                    search_line=line_id))

        count = 0
        for bid in bin_ids:
            try:
                b = Bin.query.get(int(bid))
                if not b or b.status != 'available':
                    continue
                db.session.add(Allocation(order_id=order_id, line_id=line_id, bin_id=b.id))
                b.status = 'allocated'
                count += 1
            except IntegrityError:
                db.session.rollback()

        for sid in surplus_ids:
            s = Excedente.query.get(int(sid))
            if not s or s.status != 'available':
                continue
            db.session.add(Allocation(order_id=order_id, line_id=line_id, surplus_id=s.id))
            s.status = 'allocated'
            count += 1

        db.session.commit()
        flash(f'{count} elemento(s) asignado(s).', 'ok')
        return redirect(url_for('order_detail', order_id=order_id))

    @app.route('/allocations/<int:alloc_id>/release', methods=['POST'])
    def release_bin(alloc_id):
        from models import Allocation, Bin, Excedente

        alloc = Allocation.query.get_or_404(alloc_id)
        order_id = alloc.order_id
        if alloc.bin_id:
            b = Bin.query.get(alloc.bin_id)
            if b:
                b.status = 'available'
        elif alloc.surplus_id:
            s = Excedente.query.get(alloc.surplus_id)
            if s:
                s.status = 'available'
        db.session.delete(alloc)
        db.session.commit()
        flash('Liberado.', 'ok')
        return redirect(url_for('order_detail', order_id=order_id))

    @app.route('/reset', methods=['POST'])
    def reset_inventory():
        from models import Bin, Allocation

        if request.form.get('passcode') != '001083748':
            flash('Código incorrecto.', 'err')
            return redirect(url_for('index'))

        Allocation.query.filter(Allocation.bin_id.isnot(None)).delete(synchronize_session=False)
        Bin.query.delete(synchronize_session=False)
        db.session.commit()
        flash('Inventario reseteado — todos los bins han sido eliminados.', 'ok')
        return redirect(url_for('index'))

    @app.route('/orders/<int:order_id>/delete', methods=['POST'])
    def delete_order(order_id):
        from models import Order, Excedente

        order = Order.query.get_or_404(order_id)
        if order.status not in ('fulfilled', 'cancelled'):
            flash('Solo se pueden eliminar órdenes cumplidas o canceladas.', 'err')
            return redirect(url_for('order_detail', order_id=order_id))

        # Null out FK references from excedentes → order lines before cascade delete
        line_ids = [l.id for l in order.lines]
        if line_ids:
            Excedente.query.filter(
                Excedente.source_line_id.in_(line_ids)
            ).update({'source_line_id': None, 'source_order_id': None},
                     synchronize_session=False)
        Excedente.query.filter_by(source_order_id=order_id).update(
            {'source_order_id': None}, synchronize_session=False)

        db.session.delete(order)
        db.session.commit()
        flash(f'Orden #{order_id} eliminada.', 'ok')
        return redirect(url_for('list_orders'))

    @app.route('/orders/<int:order_id>/dispatch', methods=['GET', 'POST'])
    def dispatch_order(order_id):
        from models import Order, Bin, Allocation, Excedente, DRYING_LABELS

        order = Order.query.get_or_404(order_id)
        if order.status != 'confirmed':
            flash('Solo se pueden despachar órdenes confirmadas.', 'err')
            return redirect(url_for('order_detail', order_id=order_id))

        if request.method == 'POST':
            created = 0
            for line in order.lines:
                exc_kg_str = request.form.get(f'exc_kg_{line.id}', '').strip()
                exc_boxes_str = request.form.get(f'exc_boxes_{line.id}', '').strip()
                try:
                    exc_kg = float(exc_kg_str) if exc_kg_str else 0.0
                except ValueError:
                    exc_kg = 0.0
                try:
                    exc_boxes = int(exc_boxes_str) if exc_boxes_str else None
                except ValueError:
                    exc_boxes = None

                if exc_kg > 0:
                    s = Excedente(
                        source_order_id=order_id,
                        source_line_id=line.id,
                        caliber=line.caliber,
                        drying=line.drying,
                        temporada=line.temporada,
                        producto=line.spec_label,
                        weight_kg=exc_kg,
                        boxes=exc_boxes,
                        status='available',
                    )
                    db.session.add(s)
                    created += 1

            # Mark allocated bins/surplus as shipped
            allocs = Allocation.query.filter_by(order_id=order_id).all()
            for a in allocs:
                if a.bin_id:
                    b = Bin.query.get(a.bin_id)
                    if b:
                        b.status = 'shipped'
                elif a.surplus_id:
                    s = Excedente.query.get(a.surplus_id)
                    if s:
                        s.status = 'shipped'

            order.status = 'fulfilled'
            db.session.commit()

            msg = 'Orden cumplida — items despachados.'
            if created:
                msg += f' {created} excedente(s) registrado(s) como disponibles.'
            flash(msg, 'ok')
            return redirect(url_for('order_detail', order_id=order_id))

        return render_template('orders/dispatch.html',
            order=order, DRYING_LABELS=DRYING_LABELS)

    @app.route('/orders/<int:order_id>/status', methods=['POST'])
    def update_order_status(order_id):
        from models import Order, Bin, Allocation

        order      = Order.query.get_or_404(order_id)
        new_status = request.form.get('status')

        allowed = {
            'open':      ['confirmed', 'cancelled'],
            'confirmed': ['fulfilled', 'cancelled'],
            'fulfilled': [],
            'cancelled': [],
        }
        if new_status not in allowed.get(order.status, []):
            flash(f'Transición de estado no permitida.', 'err')
            return redirect(url_for('order_detail', order_id=order_id))

        if new_status == 'cancelled':
            allocs = Allocation.query.filter_by(order_id=order_id).all()
            for a in allocs:
                if a.bin_id:
                    b = Bin.query.get(a.bin_id)
                    if b:
                        b.status = 'available'
                elif a.surplus_id:
                    from models import Excedente
                    s = Excedente.query.get(a.surplus_id)
                    if s:
                        s.status = 'available'
                db.session.delete(a)
            flash('Orden cancelada — items liberados.', 'ok')
        elif new_status == 'fulfilled':
            allocs = Allocation.query.filter_by(order_id=order_id).all()
            for a in allocs:
                if a.bin_id:
                    b = Bin.query.get(a.bin_id)
                    if b:
                        b.status = 'shipped'
                elif a.surplus_id:
                    from models import Excedente
                    s = Excedente.query.get(a.surplus_id)
                    if s:
                        s.status = 'shipped'
            flash('Orden cumplida — items marcados como despachados.', 'ok')
        else:
            from models import ORDER_STATUS_LABELS
            flash(f'Estado actualizado a "{ORDER_STATUS_LABELS.get(new_status, new_status)}".', 'ok')

        order.status = new_status
        db.session.commit()
        return redirect(url_for('order_detail', order_id=order_id))

    return app


def _migrate(db_obj):
    """Additive schema migrations — safe to run on every startup."""
    stmts = [
        # bins — new columns
        'ALTER TABLE bins ADD COLUMN IF NOT EXISTS producto VARCHAR(200)',
        'ALTER TABLE bins ADD COLUMN IF NOT EXISTS caliber VARCHAR(20)',
        'ALTER TABLE bins ADD COLUMN IF NOT EXISTS drying VARCHAR(30)',
        'ALTER TABLE bins ADD COLUMN IF NOT EXISTS humedad FLOAT',
        'ALTER TABLE bins ADD COLUMN IF NOT EXISTS contenedor VARCHAR(100)',
        'ALTER TABLE bins ADD COLUMN IF NOT EXISTS temporada VARCHAR(10)',
        "ALTER TABLE bins ADD COLUMN IF NOT EXISTS status VARCHAR(20) DEFAULT 'available'",
        # Old drying_method column has NOT NULL — make it nullable so new inserts work
        'ALTER TABLE bins ALTER COLUMN drying_method DROP NOT NULL',
        # orders — new columns
        'ALTER TABLE orders ADD COLUMN IF NOT EXISTS customer VARCHAR(200)',
        'ALTER TABLE orders ADD COLUMN IF NOT EXISTS reference VARCHAR(100)',
        # copy buyer_name → customer for old rows
        'UPDATE orders SET customer = buyer_name WHERE customer IS NULL AND buyer_name IS NOT NULL',
        # Drop NOT NULL on legacy order columns so new inserts don't fail
        'ALTER TABLE orders ALTER COLUMN buyer_name DROP NOT NULL',
        # status rename (draft→open, shipped/closed→fulfilled)
        "UPDATE orders SET status = 'open'      WHERE status = 'draft'",
        "UPDATE orders SET status = 'fulfilled' WHERE status IN ('shipped','closed')",
        # excedentes support
        'ALTER TABLE allocations ALTER COLUMN bin_id DROP NOT NULL',
        'ALTER TABLE allocations ADD COLUMN IF NOT EXISTS surplus_id INTEGER REFERENCES excedentes(id)',
    ]
    with db_obj.engine.connect() as conn:
        for sql in stmts:
            try:
                conn.execute(db_obj.text(sql))
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass


app = create_app()

if __name__ == '__main__':
    app.run(debug=True)
