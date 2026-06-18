import os
from datetime import datetime
from io import BytesIO

from flask import Flask, render_template, request, redirect, url_for, flash
from sqlalchemy.exc import IntegrityError
import openpyxl

from db import db

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

    @app.route('/bins/import', methods=['GET', 'POST'])
    def import_bins():
        if request.method == 'POST':
            from models import Bin

            file = request.files.get('file')
            if not file or not file.filename.lower().endswith('.xlsx'):
                flash('Please upload a .xlsx file.', 'danger')
                return redirect(url_for('import_bins'))

            wb = openpyxl.load_workbook(BytesIO(file.read()))
            ws = wb.active

            # Spanish and English drying method names
            drying_map = {
                'oven drying': 'oven', 'oven': 'oven', 'horno': 'oven',
                'field drying': 'field', 'field': 'field', 'cancha': 'field',
                'other': 'other', 'otro': 'other',
            }

            # Auto-detect the header row by finding the row containing "TARJA"
            header_row_num = None
            col = {}
            for idx, row in enumerate(ws.iter_rows(max_row=15, values_only=True), start=1):
                vals = [str(v).upper().strip() if v is not None else '' for v in row]
                if 'TARJA' in vals:
                    header_row_num = idx
                    col = {name: i for i, name in enumerate(vals)}
                    break

            if header_row_num is None:
                flash('Could not find a header row with a TARJA column. Check the file format.', 'danger')
                return redirect(url_for('import_bins'))

            errors = []
            added = 0
            skipped = 0

            for row_num, row in enumerate(
                ws.iter_rows(min_row=header_row_num + 1, values_only=True),
                start=header_row_num + 1,
            ):
                if not any(cell is not None for cell in row):
                    continue

                def cell(name):
                    i = col.get(name)
                    return row[i] if i is not None and i < len(row) else None

                tarja    = cell('TARJA')
                neto     = cell('NETO')
                productor = cell('PRODUCTOR')
                serie    = cell('SERIE')
                secado   = cell('SECADO')

                if tarja is None:
                    errors.append(f'Row {row_num}: Missing TARJA — skipped.')
                    continue

                # TARJA may come as a float (e.g. 2601304118.0) — strip decimal
                tarja_str = str(int(tarja)) if isinstance(tarja, float) else str(tarja).strip()

                # Caliber (SERIE) — may be None or "N/A"
                cal_low = cal_high = None
                if serie is not None:
                    serie_str = str(serie).strip().upper()
                    if serie_str not in ('', 'N/A', 'NONE', '-'):
                        if '/' in serie_str:
                            try:
                                lo, hi = serie_str.split('/', 1)
                                cal_low, cal_high = int(lo), int(hi)
                            except ValueError:
                                errors.append(f'Row {row_num}: Invalid SERIE "{serie}" — stored as N/A.')
                        else:
                            errors.append(f'Row {row_num}: SERIE "{serie}" not in XX/YY format — stored as N/A.')

                # Drying method (SECADO)
                drying_key = drying_map.get(str(secado).lower().strip()) if secado else None
                if not drying_key:
                    errors.append(f'Row {row_num}: Unknown SECADO "{secado}" — skipped.')
                    continue

                if Bin.query.filter_by(bin_identifier=tarja_str).first():
                    skipped += 1
                    continue

                try:
                    db.session.add(Bin(
                        bin_identifier=tarja_str,
                        producer_name=str(productor).strip() if productor else '',
                        weight_kg=float(neto) if neto is not None else 0.0,
                        drying_method=drying_key,
                        caliber_low=cal_low,
                        caliber_high=cal_high,
                    ))
                    added += 1
                except Exception as e:
                    db.session.rollback()
                    errors.append(f'Row {row_num}: {e}')
                    continue

            db.session.commit()
            flash(f'Import complete: {added} added, {skipped} skipped (duplicate IDs).', 'success')
            for msg in errors[:15]:
                flash(msg, 'warning')

            return redirect(url_for('list_bins'))

        return render_template('bins/import.html')

    return app


app = create_app()

if __name__ == '__main__':
    app.run(debug=True)
