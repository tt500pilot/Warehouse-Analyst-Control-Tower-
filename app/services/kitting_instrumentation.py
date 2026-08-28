"""Prospective AWIA kitting instrumentation stored outside Odoo.

Odoo remains the transactional system of record for stock moves.  This module
records observational workflow events that Odoo does not natively separate,
such as picker start, location arrival, item scan, shortage/resume, and staging
completion.  The data is append-oriented and intentionally does not mutate
Odoo.

SQLite is used for the local sandbox/runtime slice so instrumentation survives
API/CLI restarts without adding another service dependency.  A production
backend can later replace this store behind the same service boundary.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any, Callable, Iterable

ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = ROOT_DIR / "data" / "runtime" / "awia_kitting_events.sqlite3"

ALLOWED_EVENT_TYPES = {
    "location_arrival",
    "item_scan",
    "shortage_detected",
    "resume",
    "stage_complete",
    "note",
}


class KittingInstrumentationError(RuntimeError):
    """Base error for instrumentation workflow failures."""


class SessionNotFoundError(KittingInstrumentationError):
    pass


class SessionStateError(KittingInstrumentationError):
    pass


class InvalidEventError(KittingInstrumentationError):
    pass


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _round_or_none(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(value, digits)


@dataclass(frozen=True)
class SessionIdentity:
    session_id: str
    picking_id: int
    picking_name: str | None = None
    manufacturing_order: str | None = None
    awia_origin: str | None = None


class KittingEventStore:
    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        now_provider: Callable[[], datetime] = _utc_now,
    ) -> None:
        configured = db_path or os.getenv("AWIA_KITTING_EVENT_DB") or DEFAULT_DB_PATH
        self.db_path = Path(configured)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._now = now_provider
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS kitting_sessions (
                    session_id TEXT PRIMARY KEY,
                    picking_id INTEGER NOT NULL,
                    picking_name TEXT,
                    manufacturing_order TEXT,
                    awia_origin TEXT,
                    operator TEXT,
                    layout_version TEXT,
                    route_algorithm_version TEXT,
                    started_at TEXT NOT NULL,
                    stage_completed_at TEXT,
                    closed_at TEXT,
                    status TEXT NOT NULL CHECK(status IN ('active', 'staged', 'closed', 'cancelled')),
                    notes TEXT
                );

                CREATE UNIQUE INDEX IF NOT EXISTS uq_active_kitting_session_per_picking
                ON kitting_sessions(picking_id)
                WHERE status IN ('active', 'staged');

                CREATE TABLE IF NOT EXISTS kitting_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    move_line_id INTEGER,
                    product_id INTEGER,
                    product_code TEXT,
                    location_id INTEGER,
                    location_code TEXT,
                    quantity REAL,
                    metadata_json TEXT,
                    FOREIGN KEY(session_id) REFERENCES kitting_sessions(session_id)
                );

                CREATE INDEX IF NOT EXISTS idx_kitting_events_session_time
                ON kitting_events(session_id, occurred_at, event_id);
                """
            )

    def start_session(
        self,
        identity: SessionIdentity,
        *,
        operator: str | None = None,
        layout_version: str | None = None,
        route_algorithm_version: str | None = None,
        notes: str | None = None,
        occurred_at: datetime | None = None,
    ) -> dict[str, Any]:
        if identity.picking_id <= 0:
            raise ValueError("picking_id must be positive")
        timestamp = _iso(occurred_at or self._now())
        session_id = str(uuid.uuid4())
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO kitting_sessions (
                        session_id, picking_id, picking_name, manufacturing_order,
                        awia_origin, operator, layout_version, route_algorithm_version,
                        started_at, status, notes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)
                    """,
                    (
                        session_id,
                        identity.picking_id,
                        identity.picking_name,
                        identity.manufacturing_order,
                        identity.awia_origin,
                        operator,
                        layout_version,
                        route_algorithm_version,
                        timestamp,
                        notes,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise SessionStateError(
                f"Picking {identity.picking_id} already has an active/staged instrumentation session."
            ) from exc
        return self.get_session(session_id)

    def append_event(
        self,
        session_id: str,
        event_type: str,
        *,
        occurred_at: datetime | None = None,
        move_line_id: int | None = None,
        product_id: int | None = None,
        product_code: str | None = None,
        location_id: int | None = None,
        location_code: str | None = None,
        quantity: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized = event_type.strip().lower()
        if normalized not in ALLOWED_EVENT_TYPES:
            raise InvalidEventError(
                f"Unsupported event_type {event_type!r}; allowed={sorted(ALLOWED_EVENT_TYPES)}"
            )
        session = self.get_session(session_id)
        if session["status"] not in {"active", "staged"}:
            raise SessionStateError(
                f"Session {session_id} is {session['status']!r}; events can only be appended while active/staged."
            )
        if normalized == "stage_complete" and session["status"] == "staged":
            raise SessionStateError(f"Session {session_id} is already staged.")
        if quantity is not None and quantity < 0:
            raise ValueError("quantity cannot be negative")

        timestamp = _iso(occurred_at or self._now())
        payload = json.dumps(metadata or {}, sort_keys=True)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO kitting_events (
                    session_id, event_type, occurred_at, move_line_id, product_id,
                    product_code, location_id, location_code, quantity, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    normalized,
                    timestamp,
                    move_line_id,
                    product_id,
                    product_code,
                    location_id,
                    location_code,
                    quantity,
                    payload,
                ),
            )
            event_id = int(cursor.lastrowid)
            if normalized == "stage_complete":
                connection.execute(
                    """
                    UPDATE kitting_sessions
                    SET stage_completed_at = ?, status = 'staged'
                    WHERE session_id = ?
                    """,
                    (timestamp, session_id),
                )
        return self.get_event(event_id)

    def close_session(
        self,
        session_id: str,
        *,
        occurred_at: datetime | None = None,
        cancelled: bool = False,
    ) -> dict[str, Any]:
        session = self.get_session(session_id)
        if session["status"] in {"closed", "cancelled"}:
            return session
        if not cancelled and session["status"] != "staged":
            raise SessionStateError(
                "A normal session can only close after stage_complete has been recorded."
            )
        timestamp = _iso(occurred_at or self._now())
        status = "cancelled" if cancelled else "closed"
        with self._connect() as connection:
            connection.execute(
                "UPDATE kitting_sessions SET closed_at = ?, status = ? WHERE session_id = ?",
                (timestamp, status, session_id),
            )
        return self.get_session(session_id)

    def get_event(self, event_id: int) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM kitting_events WHERE event_id = ?", (event_id,)
            ).fetchone()
        if row is None:
            raise KittingInstrumentationError(f"Event {event_id} was not found.")
        return self._event_row(row)

    def get_session(self, session_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM kitting_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        if row is None:
            raise SessionNotFoundError(f"Session {session_id!r} was not found.")
        return dict(row)

    def active_session_for_picking(self, picking_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM kitting_sessions
                WHERE picking_id = ? AND status IN ('active', 'staged')
                ORDER BY started_at DESC LIMIT 1
                """,
                (picking_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def events_for_session(self, session_id: str) -> list[dict[str, Any]]:
        self.get_session(session_id)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM kitting_events
                WHERE session_id = ?
                ORDER BY occurred_at ASC, event_id ASC
                """,
                (session_id,),
            ).fetchall()
        return [self._event_row(row) for row in rows]

    def session_report(self, session_id: str) -> dict[str, Any]:
        session = self.get_session(session_id)
        events = self.events_for_session(session_id)
        started = _parse(session["started_at"])
        staged = _parse(session["stage_completed_at"])
        closed = _parse(session["closed_at"])
        scan_events = [event for event in events if event["event_type"] == "item_scan"]
        location_events = [event for event in events if event["event_type"] == "location_arrival"]
        shortage_events = [event for event in events if event["event_type"] == "shortage_detected"]
        resume_events = [event for event in events if event["event_type"] == "resume"]

        first_scan = _parse(scan_events[0]["occurred_at"]) if scan_events else None
        last_scan = _parse(scan_events[-1]["occurred_at"]) if scan_events else None

        def minutes_between(a: datetime | None, b: datetime | None) -> float | None:
            if not a or not b or b < a:
                return None
            return (b - a).total_seconds() / 60.0

        return {
            "session": session,
            "summary": {
                "event_count": len(events),
                "item_scans": len(scan_events),
                "location_arrivals": len(location_events),
                "shortage_events": len(shortage_events),
                "resume_events": len(resume_events),
                "observed_start_to_stage_minutes": _round_or_none(minutes_between(started, staged)),
                "time_to_first_scan_minutes": _round_or_none(minutes_between(started, first_scan)),
                "first_to_last_scan_minutes": _round_or_none(minutes_between(first_scan, last_scan)),
                "stage_to_close_minutes": _round_or_none(minutes_between(staged, closed)),
            },
            "events": events,
            "methodology": {
                "observed_start_to_stage": "AWIA session start to explicit stage_complete; intended as picker-observed kit cycle for prospective instrumentation.",
                "time_to_first_scan": "AWIA session start to first item_scan event.",
                "first_to_last_scan": "first item_scan to last item_scan; excludes pre-first-scan and post-last-scan work.",
                "odoo_completion": "Join separately to native stock.picking.date_done; AWIA does not fabricate or overwrite Odoo timestamps.",
            },
        }

    def summarize_closed_sessions(self) -> dict[str, Any]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT session_id FROM kitting_sessions WHERE status = 'closed' ORDER BY started_at"
            ).fetchall()
        reports = [self.session_report(str(row["session_id"])) for row in rows]
        observed = [
            report["summary"]["observed_start_to_stage_minutes"]
            for report in reports
            if report["summary"]["observed_start_to_stage_minutes"] is not None
        ]
        return {
            "closed_sessions": len(reports),
            "observed_start_to_stage_minutes": {
                "average": _round_or_none(mean(observed) if observed else None),
                "median": _round_or_none(median(observed) if observed else None),
                "measured_sessions": len(observed),
            },
            "sessions": [report["session"] for report in reports],
        }

    @staticmethod
    def _event_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        try:
            result["metadata"] = json.loads(result.pop("metadata_json") or "{}")
        except json.JSONDecodeError:
            result["metadata"] = {}
            result.pop("metadata_json", None)
        return result
