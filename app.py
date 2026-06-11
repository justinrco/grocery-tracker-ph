"""Grocery price tracker — Flask application.

Track and compare grocery prices (Philippine Peso) across stores/markets.
Prices are append-only (each save records a new observation, preserving
history); the "current" price for a (store, product) pair is the most-recent
observation.
"""
import os
from datetime import datetime
from decimal import Decimal, InvalidOperation

from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from sqlalchemy import and_, func, or_, inspect as sql_inspect

from models import (
    PriceEntry,
    Product,
    Store,
    UNIT_CHOICES,
    db,
    parse_size_string,
)


def create_app(config=None):
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-secret-change-me"),
        SQLALCHEMY_DATABASE_URI=os.environ.get(
            "DATABASE_URL", "sqlite:///grocery.db"
        ),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
    )
    if config:
        app.config.update(config)

    db.init_app(app)

    @app.template_filter("peso")
    def peso(value):
        """Format a number as Philippine Peso, e.g. ₱1,234.50."""
        if value is None:
            return ""
        try:
            return f"₱{Decimal(value):,.2f}"
        except (InvalidOperation, TypeError, ValueError):
            return str(value)

    @app.template_filter("humanize")
    def humanize(value):
        """Render a datetime as a relative phrase like 'today' or '3 weeks ago'."""
        if value is None:
            return ""
        try:
            delta = datetime.utcnow() - value
        except TypeError:
            return ""
        seconds = int(delta.total_seconds())
        if seconds < 60:
            return "just now"
        minutes = seconds // 60
        if minutes < 60:
            return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
        hours = minutes // 60
        if hours < 24:
            return f"{hours} hour{'s' if hours != 1 else ''} ago"
        days = hours // 24
        if days == 0:
            return "today"
        if days == 1:
            return "yesterday"
        if days < 30:
            return f"{days} days ago"
        months = days // 30
        if months < 12:
            return f"{months} month{'s' if months != 1 else ''} ago"
        years = days // 365
        return f"{years} year{'s' if years != 1 else ''} ago"

    @app.context_processor
    def inject_categories():
        rows = (
            db.session.query(Product.category)
            .filter(Product.category.isnot(None), Product.category != "")
            .distinct()
            .order_by(db.func.lower(Product.category))
            .all()
        )
        return {"known_categories": [c for (c,) in rows]}

    register_routes(app)

    with app.app_context():
        db.create_all()
        _migrate_schema()

    return app


def _migrate_schema():
    """One-shot, idempotent schema bump for unit-price + price-history.

    - Adds ``product.size_amount`` and ``product.size_unit`` columns if missing.
    - Drops the old unique index on ``(store_id, product_id)`` to allow multiple
      observations per pair (price history).
    - Backfills ``size_amount``/``size_unit`` from any parseable free-text
      ``size`` values left over from the pre-unit-price schema.
    """
    engine = db.engine
    inspector = sql_inspect(engine)
    if "product" not in inspector.get_table_names():
        return  # fresh DB — db.create_all already added the new columns

    product_cols = {c["name"] for c in inspector.get_columns("product")}
    with engine.begin() as conn:
        if "size_amount" not in product_cols:
            conn.exec_driver_sql(
                "ALTER TABLE product ADD COLUMN size_amount NUMERIC(10,3)"
            )
        if "size_unit" not in product_cols:
            conn.exec_driver_sql(
                "ALTER TABLE product ADD COLUMN size_unit VARCHAR(8)"
            )
        conn.exec_driver_sql("DROP INDEX IF EXISTS uq_store_product")

        # Backfill structured size from any legacy free-text sizes we can parse.
        rows = conn.exec_driver_sql(
            "SELECT id, size FROM product "
            "WHERE size IS NOT NULL AND size != '' "
            "AND (size_amount IS NULL OR size_unit IS NULL)"
        ).fetchall()
        for pid, sizestr in rows:
            amount, unit = parse_size_string(sizestr)
            if amount is not None and unit is not None:
                conn.exec_driver_sql(
                    "UPDATE product SET size_amount = ?, size_unit = ? WHERE id = ?",
                    (str(amount), unit, pid),
                )


def _parse_price(raw):
    """Return a non-negative Decimal price, or None if invalid."""
    if raw is None:
        return None
    raw = raw.strip().replace(",", "").replace("₱", "")
    if not raw:
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None
    if value < 0:
        return None
    return value.quantize(Decimal("0.01"))


def _parse_size_amount(raw):
    """Return a positive Decimal size amount, or None if blank/invalid."""
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None
    if value <= 0:
        return None
    return value


def _latest_price_subquery():
    """Subquery: latest observation time per (store_id, product_id)."""
    return (
        db.session.query(
            PriceEntry.store_id.label("store_id"),
            PriceEntry.product_id.label("product_id"),
            func.max(PriceEntry.updated_at).label("latest_at"),
        )
        .group_by(PriceEntry.store_id, PriceEntry.product_id)
        .subquery()
    )


def latest_prices_for_store(store_id):
    """Current price entry per product at one store, ordered by product name."""
    sub = _latest_price_subquery()
    return (
        PriceEntry.query.join(
            sub,
            and_(
                PriceEntry.store_id == sub.c.store_id,
                PriceEntry.product_id == sub.c.product_id,
                PriceEntry.updated_at == sub.c.latest_at,
            ),
        )
        .join(Product)
        .filter(PriceEntry.store_id == store_id)
        .order_by(Product.name)
        .all()
    )


def latest_prices_for_product(product_id):
    """Current price entry per store for one product, cheapest first."""
    sub = _latest_price_subquery()
    return (
        PriceEntry.query.join(
            sub,
            and_(
                PriceEntry.store_id == sub.c.store_id,
                PriceEntry.product_id == sub.c.product_id,
                PriceEntry.updated_at == sub.c.latest_at,
            ),
        )
        .filter(PriceEntry.product_id == product_id)
        .order_by(PriceEntry.price.asc())
        .all()
    )


def register_routes(app):
    # ---------- Home ----------
    @app.route("/")
    def index():
        return redirect(url_for("list_stores"))

    # ---------- Stores ----------
    @app.route("/stores")
    def list_stores():
        q = request.args.get("q", "").strip()
        query = Store.query
        if q:
            like = f"%{q}%"
            query = query.filter(
                or_(Store.name.ilike(like), Store.location.ilike(like))
            )
        stores = query.order_by(Store.name).all()
        # Pre-compute product counts (distinct products with at least one price).
        counts = dict(
            db.session.query(
                PriceEntry.store_id, func.count(func.distinct(PriceEntry.product_id))
            )
            .group_by(PriceEntry.store_id)
            .all()
        )
        return render_template("stores.html", stores=stores, q=q, counts=counts)

    @app.route("/stores", methods=["POST"])
    def create_store():
        name = request.form.get("name", "").strip()
        if not name:
            flash("Store name is required.", "danger")
            return redirect(url_for("list_stores"))
        if Store.query.filter(db.func.lower(Store.name) == name.lower()).first():
            flash(f"A store named “{name}” already exists.", "warning")
            return redirect(url_for("list_stores"))
        store = Store(
            name=name,
            location=request.form.get("location", "").strip() or None,
            notes=request.form.get("notes", "").strip() or None,
        )
        db.session.add(store)
        db.session.commit()
        flash(f"Added store “{store.name}”.", "success")
        return redirect(url_for("store_detail", store_id=store.id))

    @app.route("/stores/<int:store_id>")
    def store_detail(store_id):
        store = db.session.get(Store, store_id) or abort(404)
        prices = latest_prices_for_store(store.id)
        products = Product.query.order_by(Product.name).all()
        return render_template(
            "store_detail.html",
            store=store,
            prices=prices,
            products=products,
            unit_choices=UNIT_CHOICES,
        )

    @app.route("/stores/<int:store_id>/edit", methods=["POST"])
    def edit_store(store_id):
        store = db.session.get(Store, store_id) or abort(404)
        name = request.form.get("name", "").strip()
        if not name:
            flash("Store name is required.", "danger")
            return redirect(url_for("store_detail", store_id=store.id))
        existing = Store.query.filter(
            db.func.lower(Store.name) == name.lower(), Store.id != store.id
        ).first()
        if existing:
            flash(f"Another store named “{name}” already exists.", "warning")
            return redirect(url_for("store_detail", store_id=store.id))
        store.name = name
        store.location = request.form.get("location", "").strip() or None
        store.notes = request.form.get("notes", "").strip() or None
        db.session.commit()
        flash("Store updated.", "success")
        return redirect(url_for("store_detail", store_id=store.id))

    @app.route("/stores/<int:store_id>/delete", methods=["POST"])
    def delete_store(store_id):
        store = db.session.get(Store, store_id) or abort(404)
        name = store.name
        db.session.delete(store)  # cascade removes its price entries
        db.session.commit()
        flash(f"Deleted store “{name}” and all its prices.", "success")
        return redirect(url_for("list_stores"))

    # ---------- Products (catalog) ----------
    @app.route("/products")
    def list_products():
        q = request.args.get("q", "").strip()
        query = Product.query
        if q:
            like = f"%{q}%"
            query = query.filter(
                or_(
                    Product.name.ilike(like),
                    Product.brand.ilike(like),
                    Product.category.ilike(like),
                )
            )
        products = query.order_by(Product.name).all()
        # Per-product store counts (distinct stores with at least one price).
        counts = dict(
            db.session.query(
                PriceEntry.product_id, func.count(func.distinct(PriceEntry.store_id))
            )
            .group_by(PriceEntry.product_id)
            .all()
        )
        stores = Store.query.order_by(Store.name).all()
        return render_template(
            "products.html",
            products=products,
            stores=stores,
            counts=counts,
            unit_choices=UNIT_CHOICES,
            q=q,
        )

    @app.route("/products", methods=["POST"])
    def create_product():
        product, error = _create_product_from_form(request.form)
        if error:
            flash(error, "warning")
            return redirect(url_for("list_products"))

        store, store_error = _resolve_store(request.form)
        if store_error:
            db.session.rollback()
            flash(store_error, "warning")
            return redirect(url_for("list_products"))

        if store is not None:
            price = _parse_price(request.form.get("price"))
            if price is None:
                db.session.rollback()
                flash(
                    f"Enter a valid price to add “{product.display_name}” at {store.name}.",
                    "danger",
                )
                return redirect(url_for("list_products"))
            db.session.add(
                PriceEntry(store_id=store.id, product_id=product.id, price=price)
            )
            db.session.commit()
            flash(
                f"Added “{product.display_name}” at {store.name} for {peso_str(price)}.",
                "success",
            )
            return redirect(url_for("product_detail", product_id=product.id))

        db.session.commit()
        flash(f"Added product “{product.display_name}”.", "success")
        return redirect(url_for("product_detail", product_id=product.id))

    @app.route("/products/<int:product_id>")
    def product_detail(product_id):
        product = db.session.get(Product, product_id) or abort(404)
        current = latest_prices_for_product(product.id)
        return render_template(
            "product_detail.html",
            product=product,
            prices=current,
            unit_choices=UNIT_CHOICES,
        )

    @app.route("/products/<int:product_id>/history")
    def product_history(product_id):
        product = db.session.get(Product, product_id) or abort(404)
        # Optionally scope to a single store via ?store=<id>.
        store_id = _to_int(request.args.get("store"))
        store = db.session.get(Store, store_id) if store_id else None
        query = PriceEntry.query.filter_by(product_id=product.id)
        if store is not None:
            query = query.filter_by(store_id=store.id)
        observations = query.order_by(PriceEntry.updated_at.desc()).all()
        stores = (
            Store.query.join(PriceEntry)
            .filter(PriceEntry.product_id == product.id)
            .distinct()
            .order_by(Store.name)
            .all()
        )
        return render_template(
            "product_history.html",
            product=product,
            store=store,
            stores=stores,
            observations=observations,
        )

    @app.route("/products/<int:product_id>/edit", methods=["POST"])
    def edit_product(product_id):
        product = db.session.get(Product, product_id) or abort(404)
        name = request.form.get("name", "").strip()
        if not name:
            flash("Product name is required.", "danger")
            return redirect(url_for("product_detail", product_id=product.id))
        size_amount = _parse_size_amount(request.form.get("size_amount"))
        size_unit = request.form.get("size_unit", "").strip() or None
        if (size_amount is None) != (size_unit is None):
            flash("Enter both a size amount and a unit, or leave both blank.", "warning")
            return redirect(url_for("product_detail", product_id=product.id))
        if size_unit and size_unit not in UNIT_CHOICES:
            flash("Pick a valid unit from the list.", "warning")
            return redirect(url_for("product_detail", product_id=product.id))

        product.name = name
        product.brand = request.form.get("brand", "").strip() or None
        product.size_amount = size_amount
        product.size_unit = size_unit
        # Keep the legacy free-text ``size`` in sync so old display fallbacks work.
        product.size = product.display_size or None
        product.category = request.form.get("category", "").strip() or None
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            flash("A product with that name, brand and size already exists.", "warning")
            return redirect(url_for("product_detail", product_id=product.id))
        flash("Product updated.", "success")
        return redirect(url_for("product_detail", product_id=product.id))

    @app.route("/products/<int:product_id>/delete", methods=["POST"])
    def delete_product(product_id):
        product = db.session.get(Product, product_id) or abort(404)
        name = product.display_name
        db.session.delete(product)  # cascade removes its prices everywhere
        db.session.commit()
        flash(f"Deleted product “{name}” from all stores.", "success")
        return redirect(url_for("list_products"))

    # ---------- Price entries (append-only) ----------
    @app.route("/prices", methods=["POST"])
    def record_price():
        """Record a new price observation. Always appends — never mutates."""
        store = db.session.get(Store, _to_int(request.form.get("store_id"))) or abort(404)

        product_id = _to_int(request.form.get("product_id"))
        if product_id:
            product = db.session.get(Product, product_id) or abort(404)
        else:
            product, error = _create_product_from_form(request.form, find_existing=True)
            if error:
                flash(error, "warning")
                return redirect(url_for("store_detail", store_id=store.id))

        price = _parse_price(request.form.get("price"))
        if price is None:
            flash("Enter a valid non-negative price.", "danger")
            return redirect(url_for("store_detail", store_id=store.id))

        db.session.add(
            PriceEntry(store_id=store.id, product_id=product.id, price=price)
        )
        db.session.commit()
        flash(
            f"Recorded {peso_str(price)} for “{product.display_name}” at {store.name}.",
            "success",
        )
        return redirect(url_for("store_detail", store_id=store.id))

    @app.route("/prices/<int:price_id>/delete", methods=["POST"])
    def delete_price(price_id):
        """Delete a single observation. The next-most-recent observation (if any)
        becomes the current price for that (store, product)."""
        entry = db.session.get(PriceEntry, price_id) or abort(404)
        store_id = entry.store_id
        db.session.delete(entry)
        db.session.commit()
        flash("Removed that observation.", "success")
        return redirect(request.referrer or url_for("store_detail", store_id=store_id))

    @app.route("/prices/remove", methods=["POST"])
    def remove_product_from_store():
        """Remove a product from a store entirely (deletes all its observations)."""
        store_id = _to_int(request.form.get("store_id"))
        product_id = _to_int(request.form.get("product_id"))
        if not (store_id and product_id):
            abort(400)
        deleted = (
            PriceEntry.query.filter_by(store_id=store_id, product_id=product_id)
            .delete(synchronize_session=False)
        )
        db.session.commit()
        flash(f"Removed all {deleted} price record(s) for that product.", "success")
        return redirect(request.referrer or url_for("store_detail", store_id=store_id))

    # ---------- Compare ----------
    @app.route("/compare")
    def compare():
        q = request.args.get("q", "").strip()
        results = []
        unit_ranking = []
        if q:
            like = f"%{q}%"
            products = (
                Product.query.filter(
                    or_(
                        Product.name.ilike(like),
                        Product.brand.ilike(like),
                        Product.category.ilike(like),
                    )
                )
                .order_by(Product.category, Product.name)
                .all()
            )
            for product in products:
                ranked = latest_prices_for_product(product.id)
                results.append((product, ranked))

            # Cross-product unit-price ranking (per unit family) — the killer
            # feature when comparing different-sized packages in one category.
            by_family = {}
            for product, ranked in results:
                if not ranked or not product.unit_family:
                    continue
                cheapest = ranked[0]  # already sorted by absolute price
                up = product.unit_price(cheapest.price)
                if up is None:
                    continue
                by_family.setdefault(product.unit_family, []).append(
                    (up, product, cheapest)
                )
            for family, items in by_family.items():
                items.sort(key=lambda row: row[0])
                unit_ranking.append((family, items))
            unit_ranking.sort(key=lambda fam: fam[0])
        return render_template(
            "compare.html", q=q, results=results, unit_ranking=unit_ranking
        )

    # ---------- helpers bound to app context ----------
    def _create_product_from_form(form, find_existing=False):
        """Build (and persist) a Product from form fields.

        Returns (product, error_message). On success error_message is None.
        """
        name = form.get("name", "").strip()
        if not name:
            return None, "Product name is required."
        brand = form.get("brand", "").strip() or None
        size_amount = _parse_size_amount(form.get("size_amount"))
        size_unit = form.get("size_unit", "").strip() or None
        if (size_amount is None) != (size_unit is None):
            return None, "Enter both a size amount and a unit, or leave both blank."
        if size_unit and size_unit not in UNIT_CHOICES:
            return None, "Pick a valid unit from the list."

        category = form.get("category", "").strip() or None

        # Derive the legacy free-text ``size`` (used for unique-identity match
        # and as a display fallback) from the structured size, when present.
        size_text = None
        if size_amount is not None and size_unit:
            size_text = _format_size(size_amount, size_unit)

        existing = Product.query.filter(
            db.func.lower(Product.name) == name.lower(),
            _ci_eq(Product.brand, brand),
            _ci_eq(Product.size, size_text),
        ).first()
        if existing:
            if find_existing:
                return existing, None
            return None, f"Product “{existing.display_name}” already exists."

        product = Product(
            name=name,
            brand=brand,
            size=size_text,
            size_amount=size_amount,
            size_unit=size_unit,
            category=category,
        )
        db.session.add(product)
        db.session.flush()
        return product, None


def _resolve_store(form):
    """Resolve an optional store to tie to a product."""
    store_id = _to_int(form.get("store_id"))
    if store_id:
        store = db.session.get(Store, store_id)
        if store is None:
            return None, "The selected store no longer exists."
        return store, None

    name = form.get("store_name", "").strip()
    if not name:
        return None, None

    existing = Store.query.filter(db.func.lower(Store.name) == name.lower()).first()
    if existing:
        return existing, None

    store = Store(
        name=name, location=form.get("store_location", "").strip() or None
    )
    db.session.add(store)
    db.session.flush()
    return store, None


def _ci_eq(column, value):
    if value is None:
        return column.is_(None)
    return db.func.lower(column) == value.lower()


def _to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _format_size(amount, unit):
    d = Decimal(amount)
    if d == d.to_integral_value():
        amt = str(int(d))
    else:
        amt = format(d.normalize(), "f")
    return f"{amt} {unit}"


def peso_str(value):
    return f"₱{Decimal(value):,.2f}"


app = create_app()


if __name__ == "__main__":
    app.run(debug=True)
