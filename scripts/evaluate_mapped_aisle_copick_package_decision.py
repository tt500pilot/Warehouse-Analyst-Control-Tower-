from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.services.copick_package_decision import evaluate_copick_package_decision


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate REJECT/DEFER/READY_FOR_CONTROLLED_PILOT for mapped-aisle co-pick packages. "
            "Package readiness remains advisory and never authorizes inventory movement."
        )
    )
    parser.add_argument("--package-economics", required=True)
    parser.add_argument("--setup-minutes", type=float, default=15.0)
    parser.add_argument("--max-payback-pickings", type=float, default=50.0)
    parser.add_argument("--lookback-days", type=int, default=90)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    source_path = Path(args.package_economics)
    if not source_path.exists():
        raise FileNotFoundError(source_path)
    payload = json.loads(source_path.read_text(encoding="utf-8"))

    result = evaluate_copick_package_decision(
        payload,
        setup_minutes=args.setup_minutes,
        max_payback_pickings=args.max_payback_pickings,
        lookback_days=args.lookback_days,
    )
    result["package_economics_file"] = str(source_path)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
