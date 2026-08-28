from app.services.kitting_baseline import analyze_kitting_baseline


def test_kitting_baseline_distinguishes_gross_cycle_from_move_span():
    pickings = [
        {
            "id": 1,
            "name": "PICK/MOCK/1",
            "origin": "MO/MOCK/1",
            "picking_type_id": [10, "Pick Components"],
            "create_date": "2026-08-01 08:00:00",
            "scheduled_date": "2026-08-01 08:05:00",
            "date_done": "2026-08-01 08:30:00",
        },
        {
            "id": 2,
            "name": "PICK/MOCK/2",
            "origin": "MO/MOCK/2",
            "picking_type_id": [10, "Pick Components"],
            "create_date": "2026-08-01 09:00:00",
            "scheduled_date": "2026-08-01 09:00:00",
            "date_done": "2026-08-01 09:20:00",
        },
    ]
    moves = [
        {"id": 11, "picking_id": [1, "PICK/MOCK/1"], "product_id": [100, "A"], "location_id": [501, "A-01"], "date": "2026-08-01 08:10:00", "write_uid": [7, "Picker A"]},
        {"id": 12, "picking_id": [1, "PICK/MOCK/1"], "product_id": [101, "B"], "location_id": [502, "B-02"], "date": "2026-08-01 08:25:00", "write_uid": [7, "Picker A"]},
        {"id": 13, "picking_id": [2, "PICK/MOCK/2"], "product_id": [100, "A"], "location_id": [501, "A-01"], "date": "2026-08-01 09:05:00", "write_uid": [8, "Picker B"]},
        {"id": 14, "picking_id": [2, "PICK/MOCK/2"], "product_id": [102, "C"], "location_id": [503, "C-03"], "date": "2026-08-01 09:15:00", "write_uid": [8, "Picker B"]},
    ]

    report = analyze_kitting_baseline(pickings, moves)

    assert report["summary"]["kits_analyzed"] == 2
    assert report["summary"]["gross_cycle_minutes"]["average"] == 25.0
    assert report["summary"]["gross_cycle_minutes"]["median"] == 25.0
    assert report["summary"]["move_event_span_minutes_proxy"]["average"] == 12.5
    assert report["summary"]["move_lines_per_kit"] == 2.0
    assert report["summary"]["source_locations_per_kit"] == 2.0
    assert "must not be interpreted as picker labor time" in report["methodology"]["gross_cycle_time"]


def test_kitting_baseline_filters_other_picking_types():
    report = analyze_kitting_baseline(
        [
            {"id": 1, "name": "P1", "picking_type_id": [10, "Pick Components"], "create_date": "2026-08-01 08:00:00", "date_done": "2026-08-01 08:30:00"},
            {"id": 2, "name": "P2", "picking_type_id": [11, "Delivery Orders"], "create_date": "2026-08-01 08:00:00", "date_done": "2026-08-01 08:10:00"},
        ],
        [],
    )
    assert report["summary"]["kits_analyzed"] == 1
    assert report["kits"][0]["picking_name"] == "P1"
