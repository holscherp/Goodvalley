import os
from datetime import datetime

from flask import Flask, render_template, request, redirect, url_for, flash
from sqlalchemy.exc import IntegrityError

from db import db

PWAREHOUSE_URL  = os.environ.get('PWAREHOUSE_URL',  'http://190.211.168.247:8077')
PWAREHOUSE_RUT  = os.environ.get('PWAREHOUSE_RUT',  '')
PWAREHOUSE_PASS = os.environ.get('PWAREHOUSE_PASS', '')

DRYING_METHODS = {
    'oven': 'Oven Drying',
    'field': 'Field Drying',
    'other': 'Other',
}

CALIBER_OPTIONS = [
    (40, 50), (50, 60), (60, 70), (70, 80), (80, 90), (90, 100),
]

STATUS_BADGE = {
    'draft':     'secondary',
    'confirmed': 'primary',
    'shipped':   'info',
    'closed':    'success',
    'cancelled': 'danger',
}

# Which transitions are allowed from each status
STATUS_TRANSITIONS = {
    'draft':     ['confirmed', 'cancelled'],
    'confirmed': ['shipped',   'cancelled'],
    'shipped':   ['closed',    'cancelled'],
    'closed':    [],
    'cancelled': [],
}


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
        from models import Bin, Order, OrderBin  # noqa: F401
        db.create_all()
        # Migration: allow caliber columns to be null (handles pre-existing tables)
        try:
            with db.engine.connect() as conn:
                conn.execute(db.text('ALTER TABLE bins ALTER COLUMN caliber_low DROP NOT NULL'))
                conn.execute(db.text('ALTER TABLE bins ALTER COLUMN caliber_high DROP NOT NULL'))
                conn.commit()
        except Exception:
            pass

    @app.context_processor
    def inject_constants():
        return dict(
            DRYING_METHODS=DRYING_METHODS,
            CALIBER_OPTIONS=CALIBER_OPTIONS,
            STATUS_BADGE=STATUS_BADGE,
            STATUS_TRANSITIONS=STATUS_TRANSITIONS,
        )

    # ── ORDERS ────────────────────────────────────────────────────────────────

    @app.route('/')
    def index():
        return redirect(url_for('list_orders'))

    @app.route('/orders')
    def list_orders():
        from models import Order
        orders = Order.query.order_by(Order.created_at.desc()).all()
        return render_template('orders/list.html', orders=orders)

    @app.route('/orders/new', methods=['GET', 'POST'])
    def new_order():
        if request.method == 'POST':
            from models import Order
            buyer = request.form.get('buyer_name', '').strip()
            if not buyer:
                flash('Buyer name is required.', 'danger')
                return render_template('orders/new.html')

            cal_low  = request.form.get('req_caliber_low')  or None
            cal_high = request.form.get('req_caliber_high') or None
            drying   = request.form.get('req_drying_method') or None

            order = Order(
                buyer_name=buyer,
                req_caliber_low=int(cal_low)   if cal_low  else None,
                req_caliber_high=int(cal_high) if cal_high else None,
                req_drying_method=drying,
                notes=request.form.get('notes', '').strip() or None,
            )
            db.session.add(order)
            db.session.commit()
            flash('Order created.', 'success')
            return redirect(url_for('order_detail', order_id=order.id))

        return render_template('orders/new.html')

    @app.route('/orders/<int:order_id>')
    def order_detail(order_id):
        from models import Order, Bin, OrderBin

        order = Order.query.get_or_404(order_id)

        # Search params — fall back to order's saved requirements
        cal_low  = request.args.get('caliber_low',    order.req_caliber_low)
        cal_high = request.args.get('caliber_high',   order.req_caliber_high)
        drying   = request.args.get('drying_method',  order.req_drying_method)
        searched = 'search' in request.args

        search_results = []
        if searched:
            # IDs of every bin currently locked to any order
            locked_ids = {row[0] for row in db.session.query(OrderBin.bin_id).all()}

            query = Bin.query
            if locked_ids:
                query = query.filter(Bin.id.notin_(locked_ids))

            try:
                if cal_low and cal_high:
                    query = query.filter(
                        Bin.caliber_low  >= int(cal_low),
                        Bin.caliber_high <= int(cal_high),
                    )
                if drying:
                    query = query.filter(Bin.drying_method == drying)
            except (ValueError, TypeError):
                flash('Invalid search parameters.', 'danger')

            search_results = query.order_by(Bin.bin_identifier).all()

        return render_template(
            'orders/detail.html',
            order=order,
            search_results=search_results,
            searched=searched,
            cal_low=cal_low,
            cal_high=cal_high,
            drying=drying,
        )

    @app.route('/orders/<int:order_id>/allocate', methods=['POST'])
    def allocate_bins(order_id):
        from models import Order, OrderBin

        order = Order.query.get_or_404(order_id)

        if order.status in ('shipped', 'closed', 'cancelled'):
            flash(f'Cannot allocate bins to an order with status "{order.status}".', 'danger')
            return redirect(url_for('order_detail', order_id=order_id))

        bin_ids = request.form.getlist('bin_ids')
        if not bin_ids:
            flash('No bins selected.', 'warning')
            return redirect(url_for('order_detail', order_id=order_id))

        try:
            for bid in bin_ids:
                db.session.add(OrderBin(order_id=order_id, bin_id=int(bid)))
            db.session.commit()
            flash(f'{len(bin_ids)} bin(s) allocated to this order.', 'success')
        except IntegrityError:
            db.session.rollback()
            flash(
                'One or more selected bins were just allocated by another user. '
                'Refresh the page and try again.',
                'danger',
            )

        return redirect(url_for('order_detail', order_id=order_id))

    @app.route('/orders/<int:order_id>/deallocate/<int:bin_id>', methods=['POST'])
    def deallocate_bin(order_id, bin_id):
        from models import Order, OrderBin

        order = Order.query.get_or_404(order_id)

        if order.status in ('shipped', 'closed'):
            flash('Cannot remove bins from a shipped or closed order.', 'danger')
            return redirect(url_for('order_detail', order_id=order_id))

        ob = OrderBin.query.filter_by(order_id=order_id, bin_id=bin_id).first_or_404()
        db.session.delete(ob)
        db.session.commit()
        flash('Bin removed from this order and returned to available inventory.', 'success')
        return redirect(url_for('order_detail', order_id=order_id))

    @app.route('/orders/<int:order_id>/status', methods=['POST'])
    def update_order_status(order_id):
        from models import Order, OrderBin

        order = Order.query.get_or_404(order_id)
        new_status = request.form.get('status')

        if new_status not in STATUS_TRANSITIONS.get(order.status, []):
            flash(f'Cannot change status from "{order.status}" to "{new_status}".', 'danger')
            return redirect(url_for('order_detail', order_id=order_id))

        if new_status == 'cancelled':
            count = OrderBin.query.filter_by(order_id=order_id).delete()
            flash(
                f'Order cancelled. {count} bin(s) released back to available inventory.',
                'warning',
            )

        order.status = new_status
        order.updated_at = datetime.utcnow()
        db.session.commit()

        if new_status != 'cancelled':
            flash(f'Order status updated to "{new_status}".', 'success')

        return redirect(url_for('order_detail', order_id=order_id))

    @app.route('/orders/<int:order_id>/delete', methods=['POST'])
    def delete_order(order_id):
        from models import Order

        order = Order.query.get_or_404(order_id)

        if order.status not in ('cancelled', 'closed'):
            flash('Only cancelled or closed orders can be deleted.', 'danger')
            return redirect(url_for('list_orders'))

        db.session.delete(order)
        db.session.commit()
        flash(f'Order #{order_id} deleted.', 'success')
        return redirect(url_for('list_orders'))

    # ── BINS ──────────────────────────────────────────────────────────────────

    @app.route('/bins')
    def list_bins():
        from models import Bin
        bins = Bin.query.order_by(Bin.bin_identifier).all()
        return render_template('bins/list.html', bins=bins)

    @app.route('/bins/new', methods=['GET', 'POST'])
    def new_bin():
        if request.method == 'POST':
            from models import Bin
            try:
                b = Bin(
                    bin_identifier=request.form['bin_identifier'].strip(),
                    producer_name=request.form['producer_name'].strip(),
                    weight_kg=float(request.form['weight_kg']),
                    drying_method=request.form['drying_method'],
                    caliber_low=int(request.form['caliber_low']),
                    caliber_high=int(request.form['caliber_high']),
                    notes=request.form.get('notes', '').strip() or None,
                )
                db.session.add(b)
                db.session.commit()
                flash(f'Bin {b.bin_identifier} added.', 'success')
                return redirect(url_for('list_bins'))
            except IntegrityError:
                db.session.rollback()
                flash('A bin with that ID already exists.', 'danger')
            except ValueError as e:
                flash(f'Invalid value: {e}', 'danger')

        return render_template('bins/new.html')

    @app.route('/bins/<int:bin_id>/edit', methods=['GET', 'POST'])
    def edit_bin(bin_id):
        from models import Bin
        b = Bin.query.get_or_404(bin_id)

        if request.method == 'POST':
            try:
                b.producer_name = request.form['producer_name'].strip()
                b.weight_kg     = float(request.form['weight_kg'])
                b.drying_method = request.form['drying_method']
                b.caliber_low   = int(request.form['caliber_low'])
                b.caliber_high  = int(request.form['caliber_high'])
                b.notes         = request.form.get('notes', '').strip() or None
                db.session.commit()
                flash('Bin updated.', 'success')
                return redirect(url_for('list_bins'))
            except ValueError as e:
                db.session.rollback()
                flash(f'Invalid value: {e}', 'danger')

        return render_template('bins/edit.html', bin=b)

    @app.route('/sync', methods=['POST'])
    def sync():
        import json as _json
        import os as _os
        import re as _re
        from models import Bin

        _DRYING_MAP = {
            'cancha': 'field', 'field drying': 'field', 'field': 'field',
            'horno': 'oven',   'oven drying':  'oven',  'oven':  'oven',
            'otro':  'other',  'other':         'other',
        }

        def _parse_js(text):
            try:
                return _json.loads(text)
            except Exception:
                pass
            return _json.loads(_re.sub(r'(?<!["\w])(\w+)\s*:', r'"\1":', text))

        def _rv(row, i):
            return row.get(i) or row.get(str(i))

        def _do_import(bins_data):
            added = skipped = 0
            for b in bins_data:
                bid = str(b.get('bin_identifier', '')).strip()
                if not bid or Bin.query.filter_by(bin_identifier=bid).first():
                    skipped += 1
                    continue
                drying = b.get('drying_method', '')
                if drying not in ('field', 'oven', 'other'):
                    skipped += 1
                    continue
                db.session.add(Bin(
                    bin_identifier=bid,
                    producer_name=b.get('producer_name', ''),
                    weight_kg=float(b.get('weight_kg') or 0),
                    drying_method=drying,
                    caliber_low=b.get('caliber_low'),
                    caliber_high=b.get('caliber_high'),
                ))
                added += 1
            db.session.commit()
            return added, skipped

        live = request.form.get('live') == '1'

        try:
            if not live:
                dump_path = _os.path.join(_os.path.dirname(__file__), 'data_dump', 'bins.json')
                if not _os.path.exists(dump_path):
                    flash('data_dump/bins.json not found. Place the export file there and redeploy.', 'danger')
                    return redirect(url_for('list_bins'))
                with open(dump_path) as f:
                    bins_data = _json.load(f)
                added, skipped = _do_import(bins_data)
                flash(f'File sync complete: {added} imported, {skipped} already existed.', 'success')
                return redirect(url_for('list_bins'))

            # — Live sync from pWarehouse8 —
            import requests as _req
            url = PWAREHOUSE_URL.rstrip('/')
            rut = PWAREHOUSE_RUT
            pw  = PWAREHOUSE_PASS
            if not rut or not pw:
                flash('Set PWAREHOUSE_RUT and PWAREHOUSE_PASS in Railway Variables for live sync.', 'danger')
                return redirect(url_for('list_bins'))

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
                raise ValueError("Could not find _S_ID in pWarehouse8 login page.")

            fp = '&O16= \x02\x02' + rut + '&O17= \x02\x02' + pw
            sess.post(url + '/HandleEvent', data={
                'Ajax': '1', 'IsEvent': '1', 'Obj': 'O23', 'Evt': 'click',
                'this': 'O23', '_S_ID': sid, '_fp_': fp, '_seq_': 'a', '_uo_': 'O0',
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
                data = _parse_js(dr.text)
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
                if 'CIRUELA' not in str(_rv(row, 8) or '').upper():
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
                cal_low = cal_high = None
                serie = _rv(row, 15)
                if serie and '/' in str(serie):
                    try:
                        lo, hi = str(serie).strip().upper().split('/', 1)
                        cal_low, cal_high = int(lo.strip()), int(hi.strip())
                    except Exception:
                        pass
                secado = _rv(row, 16)
                drying = _DRYING_MAP.get(str(secado).lower().strip()) if secado else None
                if not drying:
                    continue
                productor = _rv(row, 12)
                bins_data.append({
                    'bin_identifier': tarja_str,
                    'producer_name':  str(productor).strip() if productor else '',
                    'weight_kg':      weight,
                    'drying_method':  drying,
                    'caliber_low':    cal_low,
                    'caliber_high':   cal_high,
                })

            added, skipped = _do_import(bins_data)
            flash(f'Live sync complete: {added} imported, {skipped} already existed.', 'success')

        except Exception as e:
            db.session.rollback()
            flash(f'Sync failed: {e}', 'danger')

        return redirect(url_for('list_bins'))

    return app


app = create_app()

if __name__ == '__main__':
    app.run(debug=True)
