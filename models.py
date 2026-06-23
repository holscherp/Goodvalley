from datetime import datetime
from db import db
import re as _re

_CALIBER_RE = _re.compile(r'(\d{2,3}/\d{2,3}|\d{2,3}\+)')

CALIBER_OPTIONS = [
    '20/30','30/40','40/50','50/60','60/70','70/80',
    '80/90','90/100','100/120','120/144','144/170','170+',
]

DRYING_LABELS = {
    'cancha':         'Cancha / Sol',
    'horno':          'Horno',
    'termino_secado': 'Término secado',
}

PRODUCT_TYPE_LABELS = {
    'tsc':     'TSC — Tiernizado sin carozo',
    'tcc':     'TCC — Tiernizado con carozo',
    'tss':     'TSS — Tiernizado sin semilla',
    'elliot':  'Elliot',
    'natural': 'Condición Natural',
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
    product_type = db.Column(db.String(20), nullable=True)
    notes        = db.Column(db.String(200), nullable=True)

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
    def pct(self):
        if self.target_kg:
            return min(100, round(self.allocated_kg / self.target_kg * 100))
        return 0

    @property
    def satisfied(self):
        return self.allocated_kg >= self.target_kg

    @property
    def product_type_label(self):
        return PRODUCT_TYPE_LABELS.get(self.product_type, self.product_type or '')

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
