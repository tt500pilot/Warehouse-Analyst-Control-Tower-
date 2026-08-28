"""Observation-mode metadata for AWIA kitting instrumentation.

This module deliberately keeps simulation-vs-human classification outside Odoo.
It adds a small SQLite schema migration beside the existing kitting event store
and prevents synthetic virtual-picker timings from being pooled with future
human-observed timings.
"""

from __future__ import annotations

import json
import sqlite3
from statistics import mean, median
from typing import Any

from app.services.kitting_instrumentation import KittingEventStore

OBSERVATION_MODES = {
    "simulated_virtual_picker",
    "human_observed",
    "manual_test",
    "unknown_legacy",
}


def _connect(store: KittingEventStore) -> sqlite3.Connection:
    connection = sqlite3.connect(store.db_path, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def ensure_observation_mode_column(store: KittingEventStore) -> None:
    """Idempotently add the session-level observation_mode column."""
    with _connect(store) as connection:
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(kitting_sessions)").fetchall()
        }
        if "observation_mode" not in columns:
            connection.execute(
                "ALTER TABLE kitting_sessions "
                "ADD COLUMN observation_mode TEXT NOT NULL DEFAULT 'unknown_legacy'"
            )


def set_observation_mode(
    store: KittingEventStore,
    session_id: str,
    observation_mode: str,
) -> dict[str, Any]:
    if observation_mode not in OBSERVATION_MODES:
        raise ValueError(
            f"Unsupported observation_mode {observation_mode!r}; "
            f"allowed={sorted(OBSERVATION_MODES)}"
        )
    ensure_observation_mode_column(store)
    store.get_session(session_id)
    with _connect(store) as connection:
        connection.execute(
            "UPDATE kitting_sessions SET observation_mode = ? WHERE session_id = ?",
            (observation_mode, session_id),
        )
    return store.get_session(session_id)


def backfill_observation_modes(store: KittingEventStore) -> dict[str, Any]:
    """Classify legacy sessions conservatively from their existing evidence.

    Sessions carrying virtual-picker event metadata or a virtual-picker operator
    are classified as simulated. Everything else remains unknown_legacy rather
    than being falsely promoted to human-observed evidence.
    """
    ensure_observation_mode_column(store)
    updated: list[dict[str, Any]] = []
    with _connect(store) as connection:
        sessions = connection.execute(
            "SELECT session_id, operator, observation_mode FROM kitting_sessions ORDER BY started_at"
        ).fetchall()
        for row in sessions:
            if row["observation_mode"] != "unknown_legacy":
                continue
            session_id = str(row["session_id"])
            operator = str(row["operator"] or "")
            event_rows = connection.execute(
                "SELECT metadata_json FROM kitting_events WHERE session_id = ?",
                (session_id,),
            ).fetchall()
            simulated = operator.startswith("virtual-picker")
            if not simulated:
                for event_row in event_rows:
                    try:
                        metadata = json.loads(event_row["metadata_json"] or "{}")
                    except json.JSONDecodeError:
                        continue
                    if (
                        metadata.get("classification") == "simulated_human_like"
                        or metadata.get("simulator_version")
                    ):
                        simulated = True
                        break
            if simulated:
                connection.execute(
                    "UPDATE kitting_sessions SET observation_mode = 'simulated_virtual_picker' "
                    "WHERE session_id = ?",
                    (session_id,),
                )
                updated.append(
                    {
                        "session_id": session_id,
                        "observation_mode": "simulated_virtual_picker",
                    }
                )
    return {
        "updated_sessions": len(updated),
        "updates": updated,
    }


def summarize_closed_sessions_by_mode(store: KittingEventStore) -> dict[str, Any]:
    ensure_observation_mode_column(store)
    backfill_observation_modes(store)
    with _connect(store) as connection:
        rows = connection.execute(
            "SELECT session_id, observation_mode FROM kitting_sessions "
            "WHERE status = 'closed' ORDER BY started_at"
        ).fetchall()

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        report = store.session_report(str(row["session_id"]))
        grouped.setdefault(str(row["observation_mode"]), []).append(report)

    result: dict[str, Any] = {
        "closed_sessions": len(rows),
        "by_observation_mode": {},
    }
    for mode in sorted(grouped):
        reports = grouped[mode]
        timings = [
            report["summary"]["observed_start_to_stage_minutes"]
            for report in reports
            if report["summary"]["observed_start_to_stage_minutes"] is not None
        ]
        result["by_observation_mode"][mode] = {
            "closed_sessions": len(reports),
            "start_to_stage_minutes": {
                "average": round(mean(timings), 2) if timings else None,
                "median": round(median(timings), 2) if timings else None,
                "measured_sessions": len(timings),
            },
            "sessions": [report["session"] for report in reports],
        }
    return result
