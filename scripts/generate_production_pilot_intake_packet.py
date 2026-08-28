from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.services.production_pilot_intake import build_production_pilot_intake_packet


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Generate a manager/IT/warehouse production-pilot intake packet from an AWIA production readiness artifact. "
            "The packet is advisory and does not authorize production access or inventory movement."
        )
    )
    parser.add_argument("--readiness", required=True)
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--output-markdown", required=True)
    args = parser.parse_args()

    readiness_path = Path(args.readiness)
    if not readiness_path.exists():
        raise FileNotFoundError(readiness_path)
    readiness = json.loads(readiness_path.read_text(encoding="utf-8"))
    packet = build_production_pilot_intake_packet(readiness)
    packet["source_readiness_file"] = str(readiness_path)

    json_path = Path(args.output_json)
    md_path = Path(args.output_markdown)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(packet, indent=2), encoding="utf-8")

    lines: list[str] = []
    lines.append("# AWIA Production Pilot Intake Packet")
    lines.append("")
    lines.append(
        "> **Approval preparation only.** This packet does not grant production access, authorize Odoo writes, or authorize inventory movement."
    )
    lines.append("")
    lines.append("## Current readiness")
    lines.append("")
    lines.append(f"- Source readiness status: **{packet.get('source_readiness_status')}**")
    summary = packet.get("summary") or {}
    lines.append(f"- Checks complete: {summary.get('complete')} / {summary.get('checks')}")
    lines.append(f"- Organizational actions remaining: {summary.get('organizational_actions_remaining')}")
    lines.append(
        f"- Ready to request read-only pilot approval review: **{'YES' if packet.get('ready_to_request_read_only_pilot_approval') else 'NO'}**"
    )
    lines.append("- Odoo mutated: **No**")
    lines.append("- Inventory execution authorized: **No**")
    lines.append("")

    phase_order = [row.get("phase") for row in packet.get("phase_summary") or []]
    items = packet.get("items") or []
    for phase in phase_order:
        phase_rows = [row for row in items if row.get("phase") == phase]
        if not phase_rows:
            continue
        lines.append(f"## {phase}")
        lines.append("")
        lines.append("| Readiness item | Status | Suggested owner | Evidence required | Next action | Current evidence |")
        lines.append("|---|---:|---|---|---|---|")
        for row in phase_rows:
            current_evidence = row.get("current_evidence_reference") or "None"
            lines.append(
                "| `{check}` | **{status}** | {owner} | {evidence} | {next_action} | {current} |".format(
                    check=row.get("check_id"),
                    status=row.get("status"),
                    owner=row.get("suggested_owner"),
                    evidence=row.get("required_evidence"),
                    next_action=row.get("next_action"),
                    current=current_evidence,
                )
            )
        lines.append("")

    lines.append("## Recommended approval sequence")
    lines.append("")
    lines.append("1. **Access & Data Governance** — agree on the least-privilege Odoo subset and complete data-classification/security review.")
    lines.append("2. **Physical Mapping & Metadata** — field-verify the selected warehouse area, bin capacities, and product physical metadata sources.")
    lines.append("3. **Warehouse Operating Controls** — document handling, reservation, traceability, and flight-critical rules for advisory recommendations.")
    lines.append("4. **Governance & Ownership** — assign the warehouse pilot owner and the human approval role, then define success/stop criteria.")
    lines.append("5. Re-run `evaluate_production_pilot_readiness.py`. Only when every gate passes should the team consider requesting a controlled **read-only** production pilot review.")
    lines.append("")
    lines.append("## Guardrails")
    lines.append("")
    for guardrail in packet.get("guardrails") or []:
        lines.append(f"- {guardrail}")
    lines.append("")

    md_path.write_text("\n".join(lines), encoding="utf-8")
    print(
        json.dumps(
            {
                "mode": packet.get("mode"),
                "source_readiness_status": packet.get("source_readiness_status"),
                "ready_to_request_read_only_pilot_approval": packet.get(
                    "ready_to_request_read_only_pilot_approval"
                ),
                "summary": packet.get("summary"),
                "json_output": str(json_path),
                "markdown_output": str(md_path),
                "odoo_mutated": False,
                "safe_to_execute_inventory_moves": False,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
