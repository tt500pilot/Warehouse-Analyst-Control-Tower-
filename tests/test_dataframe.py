from app.ui.dataframe import make_frame, normalize_odoo_value


def test_many2one_value_is_rendered_as_scalar_string() -> None:
    value = [42, "[ORBITAL-TRANSFER-STAGE] Orbital Transfer Stage"]
    assert normalize_odoo_value(value) == "42 | [ORBITAL-TRANSFER-STAGE] Orbital Transfer Stage"


def test_make_frame_handles_mixed_many2one_column() -> None:
    frame = make_frame(
        [
            {"id": 1, "product_tmpl_id": [42, "[ORBITAL-TRANSFER-STAGE] Orbital Transfer Stage"]},
            {"id": 2, "product_tmpl_id": False},
            {"id": 3, "product_tmpl_id": [43, "Another Product"]},
        ]
    )

    assert frame.loc[0, "product_tmpl_id"] == "42 | [ORBITAL-TRANSFER-STAGE] Orbital Transfer Stage"
    assert str(frame["product_tmpl_id"].dtype) in {"object", "string"}


def test_nested_values_are_serialized_for_arrow() -> None:
    frame = make_frame(
        [
            {"reasons": ["A", "B"], "metadata": {"x": 1}},
        ]
    )

    assert frame.loc[0, "reasons"] == "A • B"
    assert frame.loc[0, "metadata"] == '{"x": 1}'
