"""Tests for cascade-delete rules, append-only price history, and unit-price math."""
from decimal import Decimal

import pytest

from app import create_app, latest_prices_for_product, latest_prices_for_store
from models import PriceEntry, Product, Store, db


@pytest.fixture
def client():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
    with app.app_context():
        db.create_all()
        with app.test_client() as c:
            yield c
        db.drop_all()


def _make_store(name="SM"):
    s = Store(name=name)
    db.session.add(s)
    db.session.commit()
    return s


def _make_product(name="Pancit Canton", brand="Lucky Me", size_amount=Decimal("60"), size_unit="g"):
    p = Product(name=name, brand=brand, size_amount=size_amount, size_unit=size_unit)
    if size_amount is not None and size_unit:
        p.size = f"{size_amount} {size_unit}"
    db.session.add(p)
    db.session.commit()
    return p


def test_deleting_store_removes_its_prices_but_keeps_product(client):
    store_a = _make_store("SM")
    store_b = _make_store("Puregold")
    product = _make_product()
    db.session.add_all([
        PriceEntry(store_id=store_a.id, product_id=product.id, price=Decimal("45")),
        PriceEntry(store_id=store_b.id, product_id=product.id, price=Decimal("42")),
    ])
    db.session.commit()

    db.session.delete(store_a)
    db.session.commit()

    assert PriceEntry.query.filter_by(store_id=store_a.id).count() == 0
    assert db.session.get(Product, product.id) is not None  # catalog survives
    assert PriceEntry.query.filter_by(store_id=store_b.id).count() == 1


def test_deleting_product_removes_prices_everywhere(client):
    store_a = _make_store("SM")
    store_b = _make_store("Puregold")
    product = _make_product()
    db.session.add_all([
        PriceEntry(store_id=store_a.id, product_id=product.id, price=Decimal("45")),
        PriceEntry(store_id=store_b.id, product_id=product.id, price=Decimal("42")),
    ])
    db.session.commit()

    db.session.delete(product)
    db.session.commit()

    assert PriceEntry.query.count() == 0
    assert db.session.get(Store, store_a.id) is not None  # stores survive


def test_deleting_single_price_leaves_others(client):
    store_a = _make_store("SM")
    store_b = _make_store("Puregold")
    product = _make_product()
    e1 = PriceEntry(store_id=store_a.id, product_id=product.id, price=Decimal("45"))
    e2 = PriceEntry(store_id=store_b.id, product_id=product.id, price=Decimal("42"))
    db.session.add_all([e1, e2])
    db.session.commit()

    db.session.delete(e1)
    db.session.commit()

    assert PriceEntry.query.count() == 1
    assert db.session.get(Store, store_a.id) is not None
    assert db.session.get(Product, product.id) is not None


def test_cheapest_store_ranked_first(client):
    cheap = _make_store("Cheap Mart")
    pricey = _make_store("Pricey Mart")
    product = _make_product()
    db.session.add_all([
        PriceEntry(store_id=pricey.id, product_id=product.id, price=Decimal("50")),
        PriceEntry(store_id=cheap.id, product_id=product.id, price=Decimal("38")),
    ])
    db.session.commit()

    ranked = latest_prices_for_product(product.id)
    assert ranked[0].store.name == "Cheap Mart"
    assert ranked[0].price == Decimal("38.00")


def test_record_price_route_appends_history(client):
    store = _make_store("SM")
    product = _make_product()

    client.post("/prices", data={"store_id": store.id, "product_id": product.id, "price": "45"})
    client.post("/prices", data={"store_id": store.id, "product_id": product.id, "price": "40"})

    entries = PriceEntry.query.filter_by(store_id=store.id, product_id=product.id).all()
    assert len(entries) == 2  # append-only, both observations kept

    latest = latest_prices_for_store(store.id)
    assert len(latest) == 1
    assert latest[0].price == Decimal("40.00")  # newest wins as "current"


def test_remove_product_from_store_deletes_all_observations(client):
    store = _make_store("SM")
    other = _make_store("Puregold")
    product = _make_product()
    db.session.add_all([
        PriceEntry(store_id=store.id, product_id=product.id, price=Decimal("45")),
        PriceEntry(store_id=store.id, product_id=product.id, price=Decimal("48")),
        PriceEntry(store_id=other.id, product_id=product.id, price=Decimal("42")),
    ])
    db.session.commit()

    client.post("/prices/remove", data={"store_id": store.id, "product_id": product.id})

    assert PriceEntry.query.filter_by(store_id=store.id).count() == 0
    assert PriceEntry.query.filter_by(store_id=other.id).count() == 1  # untouched


def test_unit_price_normalizes_across_sizes(client):
    small = _make_product("Pancit Canton small", brand="Lucky Me",
                          size_amount=Decimal("60"), size_unit="g")
    big = _make_product("Pancit Canton big", brand="Lucky Me",
                        size_amount=Decimal("1"), size_unit="kg")

    # 60g pack at ₱12 → ₱20 / 100g; 1kg pack at ₱180 → ₱18 / 100g
    assert small.unit_price(Decimal("12")) == Decimal("20")
    assert big.unit_price(Decimal("180")) == Decimal("18")
    assert small.unit_family == big.unit_family == "mass"


def test_gallon_unit_price_compares_with_litres(client):
    from models import parse_size_string

    gallon = _make_product("Distilled Water gal", brand="Wilkins",
                           size_amount=Decimal("1"), size_unit="gallon")
    litre = _make_product("Distilled Water 1L", brand="Wilkins",
                          size_amount=Decimal("1"), size_unit="L")

    # 1 US gallon = 3785.411784 mL → ₱100 / gallon ≈ ₱2.6417 / 100mL
    # 1 L = 1000 mL → ₱30 / L = ₱3.00 / 100mL
    assert gallon.unit_family == litre.unit_family == "volume"
    assert gallon.unit_label == "/100mL"
    assert round(gallon.unit_price(Decimal("100")), 4) == Decimal("2.6417")
    assert litre.unit_price(Decimal("30")) == Decimal("3.0000")
    # free-text "1 gallon" migrates to the structured unit
    assert parse_size_string("1 gallon") == (Decimal("1"), "gallon")
    assert parse_size_string("5 gal") == (Decimal("5"), "gallon")


def test_ounce_units_compare_within_family(client):
    from models import parse_size_string

    # mass: oz vs g
    oz = _make_product("Chips oz", size_amount=Decimal("8"), size_unit="oz")
    assert oz.unit_family == "mass"
    assert oz.unit_label == "/100g"
    # 8 oz = 226.796185 g → ₱100 / pack ≈ ₱44.0925 / 100g
    assert round(oz.unit_price(Decimal("100")), 4) == Decimal("44.0925")

    # volume: fl oz vs mL
    floz = _make_product("Juice floz", size_amount=Decimal("12"), size_unit="fl oz")
    assert floz.unit_family == "volume"
    assert floz.unit_label == "/100mL"
    # 12 fl oz = 354.882... mL → ₱50 ≈ ₱14.0892 / 100mL
    assert round(floz.unit_price(Decimal("50")), 4) == Decimal("14.0892")

    # free-text parsing, including the fluid/weight distinction
    assert parse_size_string("8 oz") == (Decimal("8"), "oz")
    assert parse_size_string("12 fl oz") == (Decimal("12"), "fl oz")
    assert parse_size_string("16 FL OZ") == (Decimal("16"), "fl oz")
    assert parse_size_string("500 ounces") == (Decimal("500"), "oz")


def test_unit_price_is_none_without_structured_size(client):
    p = Product(name="Mystery", size="big")
    db.session.add(p)
    db.session.commit()
    assert p.unit_price(Decimal("100")) is None
    assert p.unit_label is None


def test_product_history_route_lists_observations(client):
    store = _make_store("SM")
    product = _make_product()
    db.session.add_all([
        PriceEntry(store_id=store.id, product_id=product.id, price=Decimal("45")),
        PriceEntry(store_id=store.id, product_id=product.id, price=Decimal("48")),
    ])
    db.session.commit()

    resp = client.get(f"/products/{product.id}/history")
    assert resp.status_code == 200
    assert b"45.00" in resp.data
    assert b"48.00" in resp.data


def test_search_filters_stores(client):
    _make_store("Robinsons")
    _make_store("Puregold")

    resp = client.get("/stores?q=pure")
    assert b"Puregold" in resp.data
    assert b"Robinsons" not in resp.data


def test_create_product_with_existing_store(client):
    store = _make_store("Puregold")

    client.post("/products", data={
        "name": "Tuna", "brand": "Century",
        "size_amount": "180", "size_unit": "g",
        "store_id": store.id, "price": "32.50",
    })

    product = Product.query.filter_by(name="Tuna").one()
    entry = PriceEntry.query.filter_by(product_id=product.id, store_id=store.id).one()
    assert entry.price == Decimal("32.50")


def test_create_product_with_new_store(client):
    client.post("/products", data={
        "name": "Tuna", "brand": "Century",
        "size_amount": "180", "size_unit": "g",
        "store_id": "", "store_name": "Gaisano", "store_location": "Davao",
        "price": "30",
    })

    store = Store.query.filter_by(name="Gaisano").one()
    assert store.location == "Davao"
    product = Product.query.filter_by(name="Tuna").one()
    assert PriceEntry.query.filter_by(product_id=product.id, store_id=store.id).count() == 1


def test_create_product_without_store_makes_no_price(client):
    client.post("/products", data={
        "name": "Tuna", "brand": "Century",
        "size_amount": "180", "size_unit": "g",
    })

    assert Product.query.filter_by(name="Tuna").count() == 1
    assert PriceEntry.query.count() == 0


def test_create_product_with_store_but_no_price_is_rejected(client):
    store = _make_store("Puregold")

    client.post("/products", data={"name": "Tuna", "store_id": store.id, "price": ""})

    assert Product.query.count() == 0  # rolled back; nothing half-created
    assert PriceEntry.query.count() == 0


def test_compare_matches_by_category(client):
    store = _make_store("SM")
    juice = _make_product("Orange Juice", brand="Minute Maid",
                          size_amount=Decimal("1"), size_unit="L")
    juice.category = "Juice"
    cone = _make_product("Cookies & Cream", brand="Selecta",
                         size_amount=Decimal("1.5"), size_unit="L")
    cone.category = "Ice Cream"
    db.session.add(PriceEntry(store_id=store.id, product_id=juice.id, price=Decimal("85")))
    db.session.commit()

    resp = client.get("/compare?q=juice")
    assert b"Orange Juice" in resp.data
    assert b"Cookies &amp; Cream" not in resp.data


def test_compare_unit_ranking_picks_best_value(client):
    store = _make_store("SM")
    small = _make_product("Pancit Canton small", brand="Lucky Me",
                          size_amount=Decimal("60"), size_unit="g")
    small.category = "Noodles"
    big = _make_product("Pancit Canton big", brand="Lucky Me",
                        size_amount=Decimal("1"), size_unit="kg")
    big.category = "Noodles"
    db.session.add_all([
        PriceEntry(store_id=store.id, product_id=small.id, price=Decimal("12")),   # ₱20/100g
        PriceEntry(store_id=store.id, product_id=big.id, price=Decimal("180")),     # ₱18/100g
    ])
    db.session.commit()

    resp = client.get("/compare?q=pancit")
    assert resp.status_code == 200
    assert b"Cheapest by unit price" in resp.data
    assert b"Best value" in resp.data
