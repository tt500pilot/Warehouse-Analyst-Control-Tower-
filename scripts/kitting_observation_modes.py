"""Manage and summarize AWIA kitting observation modes."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.services.kitting_instrumentation import KittingEventStore
from app.services.kitting_observation_modes import (
    backfill_observation_modes,
    ensure_observation_mode_column,
    set_observation_mode,
    summarize_closed_sessions_by_mode,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage AWIA kitting observation modes.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("migrate")

    set_mode = sub.add_parser("set")
    set_mode.add_argument("--session-id", required=True)
    set_mode.add_argument("--mode", required=True)

    sub.add_parser("summary")

    args = parser.parse_args()
    store = KittingEventStore()
    ensure_observation_mode_column(store)

    if args.command == "migrate":
        payload = backfill_observation_modes(store)
    elif args.command == "set":
        payload = set_observation_mode(store, args.session_id, args.mode)
    elif args.command == "summary":
        payload = summarize_closed_sessions_by_mode(store)
    else:
        raise RuntimeError(f"Unhandled command {args.command}")

    print(json.dumps(payload, indent=2, default=str))


if __name__ == "__main__":
    main()
