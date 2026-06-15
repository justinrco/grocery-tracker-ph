"""Database models for the grocery price tracker.

Products live in a shared catalog. Each ``PriceEntry`` is an immutable observation
of a (store, product) price at a point in time — recording a new price *appends*
a row rather than overwriting, so price history is preserved. The "current price"
at a store is simply the most recent observation for that (store, product) pair.

Products carry a structured size (amount + unit) so the app can compute and
compare *unit prices* — e.g. ₱/100g, ₱/100mL, ₱/piece — across packages of
different sizes within the same unit family.
"""
import re
from datetime import datetime
from decimal import Decimal

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event
from sqlalchemy.engine import Engine

db = SQLAlchemy()


@event.listens_for(Engine, "connect")
def _enable_sqlite_fk(dbapi_connection, connection_record):
    """Enforce foreign keys (and thus cascade deletes) on SQLite connections."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


# (family, multiplier_to_base, base_unit_label, per_n_of_base_for_display)
# e.g. kg → mass family, 1000g per kg, display "₱X / 100g".
UNIT_INFO = {
    "g":     ("mass",   Decimal(1),             "g",     Decimal(100)),
    "kg":    ("mass",   Decimal(1000),          "g",     Decimal(100)),
    "mL":    ("volume", Decimal(1),             "mL",    Decimal(100)),
    "L":     ("volume", Decimal(1000),          "mL",    Decimal(100)),
    "gallon":("volume", Decimal("3785.411784"), "mL",    Decimal(100)),  # US gallon
    "piece": ("count",  Decimal(1),             "piece", Decimal(1)),
    "pack":  ("count",  Decimal(1),             "pack",  Decimal(1)),
}
UNIT_CHOICES = list(UNIT_INFO.keys())


def parse_size_string(text):
    """Best-effort parse of free-text like '60g', '1.5L', '10 kg' → (Decimal, unit).

    Returns (None, None) when the format isn't recognized. Used to migrate
    pre-existing free-text sizes into the structured columns.
    """
    if not text:
        return None, None
    m = re.match(
        r"^\s*(\d+(?:\.\d+)?)\s*(kg|gallons?|gals?|g|ml|mL|ML|L|l|pcs?|piece|pack)\s*$",
        text,
    )
    if not m:
        return None, None
    amount = Decimal(m.group(1))
    raw = m.group(2)
    normalize = {
        "g": "g", "kg": "kg",
        "ml": "mL", "mL": "mL", "ML": "mL",
        "l": "L", "L": "L",
        "gal": "gallon", "gals": "gallon", "gallon": "gallon", "gallons": "gallon",
        "pc": "piece", "pcs": "piece", "piece": "piece",
        "pack": "pack",
    }
    return amount, normalize.get(raw)


def _format_amount(amount):
    """Render a Decimal amount without trailing zeros: 1 → '1', 1.5 → '1.5'."""
    d = Decimal(amount)
    if d == d.to_integral_value():
        return str(int(d))
    return format(d.normalize(), "f")


class Store(db.Model):
    __tablename__ = "store"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, unique=True)
    location = db.Column(db.String(160))
    notes = db.Column(db.Text)

    prices = db.relationship(
        "PriceEntry",
        back_populates="store",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self):
        return f"<Store {self.name!r}>"


class Product(db.Model):
    __tablename__ = "product"
    __table_args__ = (
        db.UniqueConstraint("name", "brand", "size", name="uq_product_identity"),
    )

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False)
    brand = db.Column(db.String(120))
    # Legacy free-text size kept as display fallback for pre-migration rows.
    size = db.Column(db.String(60))
    # Structured size for unit-price comparison.
    size_amount = db.Column(db.Numeric(10, 3))
    size_unit = db.Column(db.String(8))
    category = db.Column(db.String(80))

    prices = db.relationship(
        "PriceEntry",
        back_populates="product",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    @property
    def display_size(self):
        if self.size_amount is not None and self.size_unit:
            return f"{_format_amount(self.size_amount)} {self.size_unit}"
        return self.size or ""

    @property
    def display_name(self):
        parts = [self.name]
        if self.brand:
            parts.insert(0, self.brand)
        label = " ".join(parts)
        size = self.display_size
        if size:
            label = f"{label} ({size})"
        return label

    @property
    def unit_family(self):
        info = UNIT_INFO.get(self.size_unit)
        return info[0] if info else None

    @property
    def unit_label(self):
        """Human label for the unit-price display, e.g. '/100g', '/piece'."""
        info = UNIT_INFO.get(self.size_unit)
        if not info:
            return None
        _, _, base_unit, per = info
        return f"/{_format_amount(per)}{base_unit}" if per != 1 else f"/{base_unit}"

    def unit_price(self, absolute_price):
        """Compute the per-unit price for ``absolute_price`` at this product's size.

        Returns a Decimal (₱ per 100g / 100mL / piece) or None if the product
        has no structured size set.
        """
        info = UNIT_INFO.get(self.size_unit)
        if not info or self.size_amount is None:
            return None
        _, multiplier, _, per = info
        total_in_base = Decimal(self.size_amount) * multiplier
        if total_in_base == 0:
            return None
        return (Decimal(absolute_price) / total_in_base) * per

    def __repr__(self):
        return f"<Product {self.display_name!r}>"


class PriceEntry(db.Model):
    """One immutable observation of a price at a store. Append-only — recording
    a new price for the same (store, product) inserts a new row, preserving
    history. The "current" price at a store is the row with the latest
    ``recorded_at`` for that (store, product) pair.
    """
    __tablename__ = "price_entry"

    id = db.Column(db.Integer, primary_key=True)
    store_id = db.Column(
        db.Integer, db.ForeignKey("store.id", ondelete="CASCADE"), nullable=False
    )
    product_id = db.Column(
        db.Integer, db.ForeignKey("product.id", ondelete="CASCADE"), nullable=False
    )
    price = db.Column(db.Numeric(10, 2), nullable=False)
    # Kept the column name ``updated_at`` for backward compatibility with the
    # pre-history schema; treat it as "recorded_at" — the observation timestamp.
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)

    store = db.relationship("Store", back_populates="prices")
    product = db.relationship("Product", back_populates="prices")

    @property
    def recorded_at(self):
        return self.updated_at

    @property
    def unit_price(self):
        return self.product.unit_price(self.price) if self.product else None

    def __repr__(self):
        return f"<PriceEntry store={self.store_id} product={self.product_id} ₱{self.price} @ {self.updated_at}>"
