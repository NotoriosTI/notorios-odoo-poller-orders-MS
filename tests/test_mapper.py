from src.odoo.mapper import BatchOdooData, map_order_to_webhook_payload


def _make_batch():
    batch = BatchOdooData(
        partners={
            1: {
                "id": 1, "name": "Cliente Test", "email": "test@example.com",
                "phone": "+123", "street": "Calle 1", "street2": "",
                "city": "Santiago", "state_id": [5, "RM"],
                "zip": "1234", "country_id": [46, "Chile"], "vat": "12345678-9",
            },
            2: {
                "id": 2, "name": "Shipping Test", "email": "",
                "phone": "", "street": "Envío 1", "street2": "Depto 2",
                "city": "Viña", "state_id": [6, "Valparaíso"],
                "zip": "5678", "country_id": [46, "Chile"], "vat": "",
            },
        },
        products={
            100: {"id": 100, "name": "Producto A", "default_code": "SKU-A", "barcode": "123456", "product_tmpl_id": [50, "Tmpl A"]},
            101: {"id": 101, "name": "Producto B", "default_code": "", "barcode": "", "product_tmpl_id": [51, "Tmpl B"]},
        },
        variants={
            50: {"id": 50, "name": "Tmpl A", "default_code": "TMPL-A"},
            51: {"id": 51, "name": "Tmpl B", "default_code": ""},
        },
    )
    # Simular _lines_by_order
    batch._lines_by_order = {
        10: [
            {
                "order_id": [10, "SO001"],
                "product_id": [100, "Producto A"],
                "product_template_id": [50, "Tmpl A"],
                "product_uom_qty": 3,
                "price_unit": 15.50,
                "price_subtotal": 46.50,
                "price_total": 55.34,
                "discount": 0,
                "name": "Producto A x3",
            },
            {
                "order_id": [10, "SO001"],
                "product_id": [101, "Producto B"],
                "product_template_id": [51, "Tmpl B"],
                "product_uom_qty": 0,  # Debe ser filtrado
                "price_unit": 10,
                "price_subtotal": 0,
                "price_total": 0,
                "discount": 0,
                "name": "Producto B (qty 0)",
            },
            {
                "order_id": [10, "SO001"],
                "product_id": [101, "Producto B"],
                "product_template_id": [51, "Tmpl B"],
                "product_uom_qty": 2,
                "price_unit": 10,
                "price_subtotal": 20,
                "price_total": 23.80,
                "discount": 5,
                "name": "Producto B x2",
            },
        ],
    }
    return batch


def test_map_order_basic():
    order = {
        "id": 10,
        "name": "SO001",
        "state": "sale",
        "date_order": "2024-06-01 10:00:00",
        "write_date": "2024-06-01 12:00:00",
        "partner_id": [1, "Cliente Test"],
        "partner_shipping_id": [2, "Shipping Test"],
        "amount_untaxed": 66.50,
        "amount_tax": 12.64,
        "amount_total": 79.14,
        "currency_id": [3, "CLP"],
        "note": "Nota de prueba",
    }
    batch = _make_batch()
    payload = map_order_to_webhook_payload(order, batch, "testdb", 1)

    assert payload["source"] == "odoo"
    assert payload["connection_id"] == 1
    assert payload["odoo_db"] == "testdb"
    assert payload["order"]["name"] == "SO001"
    assert payload["order"]["amount_total"] == 79.14
    assert payload["order"]["currency"] == "CLP"

    assert payload["customer"]["name"] == "Cliente Test"
    assert payload["customer"]["email"] == "test@example.com"
    assert payload["customer"]["address"]["city"] == "Santiago"

    assert payload["shipping_address"]["name"] == "Shipping Test"
    assert payload["shipping_address"]["address"]["city"] == "Viña"


def test_items_filter_zero_qty():
    order = {
        "id": 10, "name": "SO001", "state": "sale",
        "date_order": "", "write_date": "",
        "partner_id": [1, "C"], "partner_shipping_id": False,
        "amount_untaxed": 0, "amount_tax": 0, "amount_total": 0,
        "currency_id": [3, "CLP"], "note": "",
    }
    batch = _make_batch()
    payload = map_order_to_webhook_payload(order, batch, "testdb", 1)

    # Qty 0 debe ser filtrado: quedan 2 items de 3 lines
    assert len(payload["items"]) == 2
    assert payload["items"][0]["sku"] == "SKU-A"
    assert payload["items"][0]["quantity"] == 3


def test_sku_fallback():
    order = {
        "id": 10, "name": "SO001", "state": "sale",
        "date_order": "", "write_date": "",
        "partner_id": False, "partner_shipping_id": False,
        "amount_untaxed": 0, "amount_tax": 0, "amount_total": 0,
        "currency_id": False, "note": "",
    }
    batch = _make_batch()
    payload = map_order_to_webhook_payload(order, batch, "mydb", 1)

    # Segundo item (product 101) no tiene default_code ni barcode, template tampoco
    assert payload["items"][1]["sku"] == "ODOO-mydb-101"


def test_shipping_falls_back_to_customer():
    order = {
        "id": 10, "name": "SO001", "state": "sale",
        "date_order": "", "write_date": "",
        "partner_id": [1, "C"], "partner_shipping_id": False,
        "amount_untaxed": 0, "amount_tax": 0, "amount_total": 0,
        "currency_id": False, "note": "",
    }
    batch = _make_batch()
    payload = map_order_to_webhook_payload(order, batch, "testdb", 1)

    assert payload["shipping_address"]["name"] == "Cliente Test"
