"""DataFrame normalization helpers for Streamlit/Odoo interoperability.

Odoo XML-RPC commonly returns relational values as mixed-type sequences such as
``[42, "[PART-001] Part Name"]``. Pandas can hold those objects, but Streamlit
serializes dataframes through PyArrow, which cannot infer a homogeneous Arrow
list type from an integer ID followed by a string display name.

Normalize nested Odoo values into display-safe scalar strings before handing a
DataFrame to Streamlit.
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Mapping

import pandas as pd


def normalize_odoo_value(value: Any) -> Any:
    """Convert nested Odoo RPC values into Arrow-safe display scalars."""
    if isinstance(value, tuple):
        value = list(value)

    if isinstance(value, list):
        if not value:
            return ""

        # Standard Odoo many2one representation: [record_id, display_name].
        if (
            len(value) == 2
            and isinstance(value[0], (int, float))
            and isinstance(value[1], str)
        ):
            record_id = int(value[0]) if isinstance(value[0], float) and value[0].is_integer() else value[0]
            return f"{record_id} | {value[1]}"

        # Human-readable representation for lists of scalar values, including
        # AWIA's reasons arrays.
        if all(item is None or isinstance(item, (str, int, float, bool)) for item in value):
            return " • ".join("" if item is None else str(item) for item in value)

        return json.dumps(value, default=str, ensure_ascii=False)

    if isinstance(value, Mapping):
        return json.dumps(dict(value), default=str, ensure_ascii=False, sort_keys=True)

    if isinstance(value, set):
        return " • ".join(sorted(str(item) for item in value))

    return value


def make_frame(records: Iterable[Mapping[str, Any]]) -> pd.DataFrame:
    """Build a Streamlit/PyArrow-safe DataFrame from Odoo/API records."""
    normalized_records = [
        {key: normalize_odoo_value(value) for key, value in record.items()}
        for record in records
    ]
    if not normalized_records:
        return pd.DataFrame()

    frame = pd.DataFrame(normalized_records)

    # A column can still be heterogeneous across records (for example False in
    # one row and a string in another). Arrow object columns are safest when
    # heterogeneous display values are normalized to pandas' nullable string
    # dtype. Homogeneous numeric/bool columns remain numeric/bool for sorting.
    for column in frame.columns:
        if frame[column].dtype != object:
            continue
        non_null = frame[column].dropna()
        scalar_types = {type(value) for value in non_null}
        if len(scalar_types) > 1:
            frame[column] = frame[column].map(
                lambda value: None if value is None else str(value)
            ).astype("string")

    return frame
