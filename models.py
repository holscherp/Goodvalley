from datetime import datetime
from db import db
import re as _re

_CALIBER_RE = _re.compile(r'(\d{2,3}/\d{2,3}|\d{2,3}\+)')

CALIBER_OPTIONS = [
    '20/30','30/40','40/50','50/60','60/70','70/80',
    '80/90','90/100','100/120','120/144','144/170','170+',
]

# ── Yield / rendimiento tables (from Disponible Master.xlsx → Rendimientos) ──

# Caliber text range → numeric midpoint (AJ:AK table in MP Comprometida)
CALIBER_TO_NUM = {
    '20/30': 25, '30/40': 35, '40/50': 45, '50/60': 55,
    '60/70': 65, '70/80': 75, '80/90': 85, '80/100': 90,
    '90/100': 95, '100/120': 110, '120/144': 132,
    '144/170': 157, '170+': 185,
}

# Rend. Usado (= Rend. Teórico by default) per (tipo, caliber_num)
# tipo keys: 'tsc', 'tcc', 'ss' (= cancha/tss), 'elliot', 'natural'
_YIELD_TABLE = {
    ('tsc',    35): 0.83, ('tsc',    45): 0.83, ('tsc',    55): 0.85,
    ('tsc',    65): 0.83, ('tsc',    75): 0.82, ('tsc',    85): 0.79,
    ('tsc',    95): 0.78, ('tsc',   110): 0.75, ('tsc',   132): 0.72,
    ('tcc',    35): 1.10, ('tcc',    45): 1.10, ('tcc',    55): 1.10,
    ('tcc',    65): 1.10, ('tcc',    75): 1.10, ('tcc',    85): 1.10,
    ('tcc',    95): 1.10, ('tcc',   110): 1.10, ('tcc',   132): 1.10,
    ('ss',     35): 0.75, ('ss',     45): 0.75, ('ss',     55): 0.75,
    ('ss',     65): 0.75, ('ss',     75): 0.75, ('ss',     85): 0.75,
    ('ss',     95): 0.75,
    ('elliot', 95): 0.75, ('elliot',110): 0.75, ('elliot',132): 0.75,
    ('natural',35): 0.98, ('natural',45): 0.98, ('natural',55): 0.98,
    ('natural',65): 0.98, ('natural',75): 0.98, ('natural',85): 0.98,
    ('natural',90): 0.98, ('natural',95): 0.98, ('natural',110): 0.98,
    ('natural',132): 0.98, ('natural',157): 0.98,
}

# Flat fallback yields when caliber is unknown
_FLAT_YIELD = {
    'tsc': 0.80, 'tcc': 1.10, 'ss': 0.75, 'elliot': 0.75, 'natural': 0.98,
}


def _tipo_key(product_type, drying):
    """Map web-app product_type + drying to the Rendimientos tipo key."""
    if product_type == 'tsc':     return 'tsc'
    if product_type == 'tcc':     return 'tcc'
    if product_type == 'tss':     return 'ss'
    if product_type == 'ss':      return 'ss'
    if product_type == 'elliot':  return 'elliot'
    if product_type == 'natural': return 'natural'
    if product_type == 'cn':      return 'natural'
    if drying == 'cancha':        return 'ss'
    if drying == 'horno':         return 'tsc'
    return None


_yield_overrides_cache = {}  # (tipo, caliber_num) → float, populated from DB at startup


def load_yield_overrides():
    global _yield_overrides_cache
    try:
        _yield_overrides_cache = {
            (o.tipo, o.caliber_num): o.rend_teorico
            for o in YieldOverride.query.all()
        }
    except Exception:
        _yield_overrides_cache = {}


def get_yield(product_type, drying, caliber):
    """Return the Rend. Usado for the given line spec, or None if unknown."""
    tipo = _tipo_key(product_type, drying)
    if tipo is None:
        return None
    cal_num = CALIBER_TO_NUM.get(caliber or '')
    if cal_num is not None:
        override = _yield_overrides_cache.get((tipo, cal_num))
        if override is not None:
            return override
        return _YIELD_TABLE.get((tipo, cal_num), _FLAT_YIELD.get(tipo))
    return _FLAT_YIELD.get(tipo)

DRYING_LABELS = {
    'cancha':         'Cancha / Sol',
    'horno':          'Horno',
    'termino_secado': 'Término secado',
}

PRODUCT_TYPE_LABELS = {
    'tsc':     'TSC',
    'tcc':     'TCC',
    'tss':     'SS',
    'ss':      'SS',
    'elliot':  'Elliot',
    'natural': 'CN',
    'cn':      'CN',
}

FRUIT_QUALITY_LABELS = {
    'deluxe':   'Deluxe',
    'premium':  'Premium',
    'estandar': 'Estándar',
    'base':     'Base',
}

BIN_STATUS_LABELS = {
    'available': 'Disponible',
    'allocated': 'Asignado',
    'shipped':   'Despachado',
    'gone':      'Retirado',
}

ORDER_STATUS_LABELS = {
    'open':      'Abierta',
    'confirmed': 'Confirmada',
    'fulfilled': 'Cumplida',
    'cancelled': 'Cancelada',
}


def _parse_producto(producto):
    """Return (caliber_str, drying_key) from the PRODUCTO field."""
    p = (producto or '').strip().upper()
    if 'TERM' in p:
        drying = 'termino_secado'
    elif 'HORNO' in p:
        drying = 'horno'
    elif 'SOL' in p or 'CANCHA' in p or 'CAMPO' in p:
        drying = 'cancha'
    else:
        drying = None
    m = _CALIBER_RE.search(p)
    caliber = m.group(1) if m else None
    return caliber, drying


class YieldOverride(db.Model):
    __tablename__ = 'yield_overrides'

    id           = db.Column(db.Integer, primary_key=True)
    tipo         = db.Column(db.String(20),  nullable=False)
    caliber_num  = db.Column(db.Integer,     nullable=False)
    rend_teorico = db.Column(db.Float,       nullable=False)
    comentario   = db.Column(db.String(200), nullable=True)

    __table_args__ = (db.UniqueConstraint('tipo', 'caliber_num', name='uq_yield_tipo_cal'),)


class Bin(db.Model):
    __tablename__ = 'bins'

    id            = db.Column(db.Integer, primary_key=True)
    bin_identifier = db.Column(db.String(50), unique=True, nullable=False)
    producto      = db.Column(db.String(200), nullable=True)
    caliber       = db.Column(db.String(20),  nullable=True)
    drying        = db.Column(db.String(30),  nullable=True)
    weight_kg     = db.Column(db.Float, default=0.0)
    humedad       = db.Column(db.Float, nullable=True)
    contenedor    = db.Column(db.String(100), nullable=True)
    producer_name = db.Column(db.String(200), default='')
    temporada     = db.Column(db.String(10),  nullable=True)
    status        = db.Column(db.String(20),  default='available')
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)

    allocation = db.relationship('Allocation', backref='bin', uselist=False,
                                  foreign_keys='Allocation.bin_id')

    @property
    def is_available(self):
        return self.status == 'available'

    @property
    def caliber_label(self):
        return self.caliber or 'N/A'

    @property
    def drying_label(self):
        return DRYING_LABELS.get(self.drying, self.drying or '—')

    @property
    def status_label(self):
        return BIN_STATUS_LABELS.get(self.status, self.status)


class Order(db.Model):
    __tablename__ = 'orders'

    id         = db.Column(db.Integer, primary_key=True)
    customer   = db.Column(db.String(200), nullable=False)
    reference  = db.Column(db.String(100), nullable=True)
    status     = db.Column(db.String(20),  default='open')
    notes      = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    lines = db.relationship(
        'OrderLine', backref='order',
        cascade='all, delete-orphan', lazy='select',
    )

    @property
    def allocated_kg(self):
        return sum(line.allocated_kg for line in self.lines)

    @property
    def target_kg(self):
        return sum(line.target_kg for line in self.lines)

    @property
    def status_label(self):
        return ORDER_STATUS_LABELS.get(self.status, self.status)


class OrderLine(db.Model):
    __tablename__ = 'order_lines'

    id          = db.Column(db.Integer, primary_key=True)
    order_id    = db.Column(db.Integer, db.ForeignKey('orders.id'), nullable=False)
    caliber     = db.Column(db.String(20),  nullable=True)
    drying      = db.Column(db.String(30),  nullable=True)
    target_kg   = db.Column(db.Float, nullable=False)
    max_humedad = db.Column(db.Float, nullable=True)
    temporada    = db.Column(db.String(10),  nullable=True)
    product_type  = db.Column(db.String(20),  nullable=True)
    fruit_quality = db.Column(db.String(20),  nullable=True)
    notes         = db.Column(db.String(200), nullable=True)

    allocations = db.relationship(
        'Allocation', backref='line',
        cascade='all, delete-orphan',
    )

    @property
    def allocated_kg(self):
        total = 0
        for a in self.allocations:
            if a.bin:
                total += a.bin.weight_kg or 0
            elif a.surplus:
                total += a.surplus.weight_kg or 0
        return total

    @property
    def yield_rate(self):
        """Rend. Usado for this line's product_type + drying + caliber."""
        return get_yield(self.product_type, self.drying, self.caliber)

    @property
    def mp_kg_needed(self):
        """Raw-material kg needed = target_kg (finished PT) / yield.
        Falls back to target_kg when yield is unknown."""
        y = self.yield_rate
        if y and y > 0:
            return self.target_kg / y
        return self.target_kg

    @property
    def pct(self):
        needed = self.mp_kg_needed
        if needed:
            return min(100, round(self.allocated_kg / needed * 100))
        return 0

    @property
    def satisfied(self):
        return self.allocated_kg >= self.mp_kg_needed

    @property
    def product_type_label(self):
        return PRODUCT_TYPE_LABELS.get(self.product_type, self.product_type or '')

    @property
    def fruit_quality_label(self):
        return FRUIT_QUALITY_LABELS.get(self.fruit_quality, self.fruit_quality or '')

    @property
    def spec_label(self):
        parts = []
        if self.caliber:
            parts.append(self.caliber)
        if self.drying:
            parts.append(DRYING_LABELS.get(self.drying, self.drying))
        if self.product_type:
            parts.append(PRODUCT_TYPE_LABELS.get(self.product_type, self.product_type))
        return ' · '.join(parts) if parts else 'Cualquier calibre/secado'


class Excedente(db.Model):
    __tablename__ = 'excedentes'

    id              = db.Column(db.Integer, primary_key=True)
    source_order_id  = db.Column(db.Integer, db.ForeignKey('orders.id'), nullable=True)
    source_line_id   = db.Column(db.Integer, db.ForeignKey('order_lines.id'), nullable=True)
    source_bin_tarja = db.Column(db.String(50), nullable=True)
    caliber          = db.Column(db.String(20),  nullable=True)
    drying          = db.Column(db.String(30),  nullable=True)
    temporada       = db.Column(db.String(10),  nullable=True)
    producto        = db.Column(db.String(200), nullable=True)
    weight_kg       = db.Column(db.Float, nullable=False)
    boxes           = db.Column(db.Integer, nullable=True)
    status          = db.Column(db.String(20),  default='available')
    created_at      = db.Column(db.DateTime, default=datetime.utcnow)

    allocation = db.relationship('Allocation', backref='surplus', uselist=False,
                                  foreign_keys='Allocation.surplus_id')

    @property
    def drying_label(self):
        return DRYING_LABELS.get(self.drying, self.drying or '—')

    @property
    def status_label(self):
        return BIN_STATUS_LABELS.get(self.status, self.status)


class Allocation(db.Model):
    __tablename__ = 'allocations'

    id         = db.Column(db.Integer, primary_key=True)
    order_id   = db.Column(db.Integer, db.ForeignKey('orders.id'), nullable=False)
    line_id    = db.Column(db.Integer, db.ForeignKey('order_lines.id'), nullable=False)
    bin_id     = db.Column(db.Integer, db.ForeignKey('bins.id'),   nullable=True)
    surplus_id = db.Column(db.Integer, db.ForeignKey('excedentes.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (db.UniqueConstraint('bin_id', name='uq_alloc_bin_id'),)


class Proceso(db.Model):
    __tablename__ = 'procesos'

    id          = db.Column(db.Integer, primary_key=True)
    ot          = db.Column(db.String(50),  nullable=False)
    idot        = db.Column(db.String(50),  nullable=True)
    tipoproceso = db.Column(db.String(50),  nullable=True)
    drying      = db.Column(db.String(30),  nullable=True)
    temporada   = db.Column(db.String(10),  nullable=True)
    neto_egreso = db.Column(db.Float,       nullable=True)
    serie       = db.Column(db.String(50),  nullable=True)
    synced_at   = db.Column(db.DateTime,    default=datetime.utcnow)

    lineas = db.relationship('ProcesoLinea', backref='proceso',
                             cascade='all, delete-orphan', lazy='select')

    @property
    def drying_label(self):
        return DRYING_LABELS.get(self.drying, self.drying or '—')


class ProcesoLinea(db.Model):
    __tablename__ = 'proceso_lineas'

    id          = db.Column(db.Integer, primary_key=True)
    proceso_id  = db.Column(db.Integer, db.ForeignKey('procesos.id'), nullable=False)
    ot          = db.Column(db.String(50),  nullable=False)
    tipo_fila   = db.Column(db.String(5),   nullable=True)   # 'D' or 'R'
    idot        = db.Column(db.String(50),  nullable=True)
    fecha       = db.Column(db.String(50),  nullable=True)
    tipoproceso = db.Column(db.String(50),  nullable=True)
    productor   = db.Column(db.String(200), nullable=True)
    serie       = db.Column(db.String(50),  nullable=True)
    drying      = db.Column(db.String(30),  nullable=True)
    temporada   = db.Column(db.String(20),  nullable=True)
    neto_egreso = db.Column(db.Float,       nullable=True)

    @property
    def drying_label(self):
        return DRYING_LABELS.get(self.drying, self.drying or '—')


class Pallet(db.Model):
    __tablename__ = 'pallets'

    id           = db.Column(db.Integer, primary_key=True)
    tarja        = db.Column(db.String(50),  unique=True, nullable=False)
    ot           = db.Column(db.String(50),  nullable=True)
    customer     = db.Column(db.String(200), nullable=True)
    caliber      = db.Column(db.String(20),  nullable=True)
    drying       = db.Column(db.String(30),  nullable=True)
    product_type = db.Column(db.String(20),  nullable=True)
    weight_kg    = db.Column(db.Float,       nullable=True)
    producto     = db.Column(db.String(200), nullable=True)
    temporada    = db.Column(db.String(10),  nullable=True)
    bin_ids_json = db.Column(db.Text,        nullable=True)
    synced_at    = db.Column(db.DateTime,    default=datetime.utcnow)

    @property
    def bin_identifiers(self):
        import json as _j
        try:
            return _j.loads(self.bin_ids_json or '[]')
        except Exception:
            return []

    @property
    def bin_count(self):
        return len(self.bin_identifiers)

    @property
    def drying_label(self):
        return DRYING_LABELS.get(self.drying, self.drying or '—')

    @property
    def product_type_label(self):
        return PRODUCT_TYPE_LABELS.get(self.product_type, self.product_type or '—')
