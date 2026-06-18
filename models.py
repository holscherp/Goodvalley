from datetime import datetime
from db import db


class Bin(db.Model):
    __tablename__ = 'bins'

    id = db.Column(db.Integer, primary_key=True)
    bin_identifier = db.Column(db.String, unique=True, nullable=False)
    producer_name = db.Column(db.String, nullable=False)
    weight_kg = db.Column(db.Numeric(10, 2), nullable=False)
    drying_method = db.Column(db.String, nullable=False)  # 'oven', 'field', 'other'
    caliber_low = db.Column(db.Integer, nullable=False)
    caliber_high = db.Column(db.Integer, nullable=False)
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    allocation = db.relationship('OrderBin', back_populates='bin', uselist=False)

    @property
    def is_available(self):
        return self.allocation is None

    @property
    def caliber_label(self):
        return f'{self.caliber_low}/{self.caliber_high}'


class Order(db.Model):
    __tablename__ = 'orders'

    id = db.Column(db.Integer, primary_key=True)
    buyer_name = db.Column(db.String, nullable=False)
    status = db.Column(db.String, nullable=False, default='draft')
    req_caliber_low = db.Column(db.Integer)
    req_caliber_high = db.Column(db.Integer)
    req_drying_method = db.Column(db.String)
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)

    order_bins = db.relationship('OrderBin', back_populates='order', cascade='all, delete-orphan')

    @property
    def allocated_bins(self):
        return [ob.bin for ob in self.order_bins]

    @property
    def total_weight(self):
        return sum(float(ob.bin.weight_kg) for ob in self.order_bins)


class OrderBin(db.Model):
    __tablename__ = 'order_bins'

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('orders.id'), nullable=False)
    bin_id = db.Column(db.Integer, db.ForeignKey('bins.id'), nullable=False)
    allocated_at = db.Column(db.DateTime, default=datetime.utcnow)

    # This UNIQUE constraint is the bin lock — enforced by the database itself.
    # Any attempt to insert a bin_id that already exists here raises IntegrityError.
    __table_args__ = (
        db.UniqueConstraint('bin_id', name='uq_order_bins_bin_id'),
    )

    order = db.relationship('Order', back_populates='order_bins')
    bin = db.relationship('Bin', back_populates='allocation')
