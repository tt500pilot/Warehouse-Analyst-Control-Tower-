from datetime import datetime, timedelta, timezone

import pytest

from app.services.kitting_instrumentation import (
    KittingEventStore,
    SessionIdentity,
    SessionStateError,
)


def _store(tmp_path):
    base = datetime(2026, 8, 28, 13, 40, tzinfo=timezone.utc)
    moments = iter(
        [
            base,
            base + timedelta(minutes=2),
            base + timedelta(minutes=5),
            base + timedelta(minutes=9),
        ]
    )
    return KittingEventStore(
        tmp_path / "events.sqlite3",
        now_provider=lambda: next(moments),
    )


def test_session_records_observed_kitting_timing(tmp_path):
    store = _store(tmp_path)
    session = store.start_session(
        SessionIdentity(
            picking_id=5,
            picking_name="WH/PC/00005",
            manufacturing_order="WH/MO/00005",
            awia_origin="AWIA-MOCK-MO-005",
        ),
        operator="picker-a",
        layout_version="mock-v1",
        route_algorithm_version="manual-observed-v1",
    )

    session_id = session["session_id"]
    store.append_event(session_id, "location_arrival", location_code="A-01-L1-BA")
    store.append_event(session_id, "item_scan", product_code="SEAL-218", quantity=2)
    store.append_event(session_id, "stage_complete")

    report = store.session_report(session_id)
    assert report["summary"]["event_count"] == 3
    assert report["summary"]["item_scans"] == 1
    assert report["summary"]["location_arrivals"] == 1
    assert report["summary"]["observed_start_to_stage_minutes"] == 9.0
    assert report["summary"]["time_to_first_scan_minutes"] == 5.0


def test_session_identity_does_not_require_session_id(tmp_path):
    store = KittingEventStore(tmp_path / "events.sqlite3")
    session = store.start_session(
        SessionIdentity(
            picking_id=5,
            picking_name="WH/PC/00005",
            manufacturing_order="WH/MO/00005",
            awia_origin="AWIA-MOCK-MO-005",
        )
    )

    assert session["picking_id"] == 5
    assert isinstance(session["session_id"], str)
    assert session["session_id"]


def test_duplicate_active_session_for_picking_is_blocked(tmp_path):
    store = KittingEventStore(tmp_path / "events.sqlite3")
    identity = SessionIdentity(picking_id=5)
    store.start_session(identity)

    with pytest.raises(SessionStateError, match="already has an active/staged"):
        store.start_session(identity)


def test_normal_close_requires_stage_complete(tmp_path):
    store = KittingEventStore(tmp_path / "events.sqlite3")
    session = store.start_session(SessionIdentity(picking_id=5))

    with pytest.raises(SessionStateError, match="stage_complete"):
        store.close_session(session["session_id"])


def test_closed_session_rejects_new_events(tmp_path):
    store = KittingEventStore(tmp_path / "events.sqlite3")
    session = store.start_session(SessionIdentity(picking_id=5))
    session_id = session["session_id"]
    store.append_event(session_id, "stage_complete")
    store.close_session(session_id)

    with pytest.raises(SessionStateError, match="events can only be appended"):
        store.append_event(session_id, "note", metadata={"message": "late"})


def test_staged_session_metadata_can_be_retagged_without_changing_events(tmp_path):
    store = KittingEventStore(tmp_path / "events.sqlite3")
    session = store.start_session(
        SessionIdentity(picking_id=5),
        operator="virtual-picker-v1",
        layout_version="mock-v1",
        route_algorithm_version="manual-observed-v1",
    )
    session_id = session["session_id"]
    store.append_event(session_id, "item_scan", product_code="A", quantity=1)
    store.append_event(session_id, "stage_complete")
    events_before = store.events_for_session(session_id)

    updated = store.update_session_metadata(
        session_id,
        route_algorithm_version="virtual-picker-nearest-neighbor-v1",
    )

    assert updated["status"] == "staged"
    assert updated["route_algorithm_version"] == "virtual-picker-nearest-neighbor-v1"
    assert store.events_for_session(session_id) == events_before


def test_closed_session_metadata_retagging_is_blocked(tmp_path):
    store = KittingEventStore(tmp_path / "events.sqlite3")
    session = store.start_session(SessionIdentity(picking_id=5))
    session_id = session["session_id"]
    store.append_event(session_id, "stage_complete")
    store.close_session(session_id)

    with pytest.raises(SessionStateError, match="metadata can only be changed"):
        store.update_session_metadata(
            session_id,
            route_algorithm_version="virtual-picker-nearest-neighbor-v1",
        )
