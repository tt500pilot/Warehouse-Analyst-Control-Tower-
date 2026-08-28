from __future__ import annotations

import argparse
import csv
import json
import math
import random
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

SEED = 42
AISLES = "ABCDEFGH"
BAYS = range(1, 7)
LEVELS = (1, 2)
SIDES = ("A", "B")
LAYOUT_VERSION = "mock-v1"
UNITS = "ft"


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        raise ValueError(f"No rows for {path}")
    fields = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def zone_for_aisle(aisle: str) -> str:
    if aisle in "AB":
        return "FAST"
    if aisle in "CDE":
        return "STANDARD"
    if aisle in "FG":
        return "CONTROLLED"
    return "BULK"


def build_warehouse():
    metadata = {
        "layout_version": LAYOUT_VERSION,
        "coordinate_system": "local_cartesian",
        "units": UNITS,
        "datum": {
            "name": "SOUTHWEST_INSIDE_FLOOR_CORNER",
            "x": 0.0,
            "y": 0.0,
            "z": 0.0,
            "rule": "Permanent mock datum. Operational stations may move; datum does not.",
        },
        "floor_z": 0.0,
        "aisle_spacing_ft": 18.0,
        "bay_spacing_ft": 12.0,
        "notes": "Synthetic warehouse geometry for AWIA algorithm development only.",
    }

    stations = [
        {"station_id": "ST_RECEIVING", "odoo_location_id": 90004, "name": "Receiving", "station_type": "receiving", "x": 8.0, "y": 92.0, "z": 0.0, "graph_node_id": "NODE_RECEIVING"},
        {"station_id": "ST_KITTING", "odoo_location_id": 90001, "name": "Kitting", "station_type": "kitting", "x": 8.0, "y": 8.0, "z": 0.0, "graph_node_id": "NODE_KITTING"},
        {"station_id": "ST_SHIPPING", "odoo_location_id": 90005, "name": "Shipping", "station_type": "shipping", "x": 26.0, "y": 8.0, "z": 0.0, "graph_node_id": "NODE_SHIPPING"},
        {"station_id": "ST_QA", "odoo_location_id": 90006, "name": "Quality", "station_type": "quality", "x": 26.0, "y": 92.0, "z": 0.0, "graph_node_id": "NODE_QA"},
        {"station_id": "ST_MRB", "odoo_location_id": 90007, "name": "MRB", "station_type": "mrb", "x": 44.0, "y": 92.0, "z": 0.0, "graph_node_id": "NODE_MRB"},
    ]

    nodes: list[dict] = []
    edges: list[dict] = []
    node_lookup: dict[str, dict] = {}

    def add_node(node_id, node_type, x, y, z=0.0):
        row = {"node_id": node_id, "node_type": node_type, "x": round(x, 3), "y": round(y, 3), "z": round(z, 3)}
        nodes.append(row)
        node_lookup[node_id] = row

    def add_edge(a, b, edge_type="walk"):
        na, nb = node_lookup[a], node_lookup[b]
        distance = math.dist((na["x"], na["y"], na["z"]), (nb["x"], nb["y"], nb["z"]))
        edges.append({"from_node": a, "to_node": b, "distance_ft": round(distance, 3), "edge_type": edge_type, "bidirectional": True})

    for station in stations:
        add_node(station["graph_node_id"], "station", station["x"], station["y"], station["z"])

    aisle_x = {aisle: 44.0 + i * 18.0 for i, aisle in enumerate(AISLES)}
    bay_y = {bay: 20.0 + (bay - 1) * 12.0 for bay in BAYS}
    south_y, north_y = 8.0, 92.0

    for aisle in AISLES:
        add_node(f"CROSS_S_{aisle}", "cross_aisle", aisle_x[aisle], south_y)
        add_node(f"CROSS_N_{aisle}", "cross_aisle", aisle_x[aisle], north_y)
        for bay in BAYS:
            add_node(f"NODE_{aisle}_{bay:02d}", "aisle", aisle_x[aisle], bay_y[bay])

    for i, aisle in enumerate(AISLES):
        add_edge(f"CROSS_S_{aisle}", f"NODE_{aisle}_01", "aisle")
        for bay in range(1, 6):
            add_edge(f"NODE_{aisle}_{bay:02d}", f"NODE_{aisle}_{bay+1:02d}", "aisle")
        add_edge(f"NODE_{aisle}_06", f"CROSS_N_{aisle}", "aisle")
        if i > 0:
            prev = AISLES[i - 1]
            add_edge(f"CROSS_S_{prev}", f"CROSS_S_{aisle}", "cross_aisle")
            add_edge(f"CROSS_N_{prev}", f"CROSS_N_{aisle}", "cross_aisle")

    add_edge("NODE_KITTING", "CROSS_S_A", "station_access")
    add_edge("NODE_SHIPPING", "CROSS_S_A", "station_access")
    add_edge("NODE_RECEIVING", "CROSS_N_A", "station_access")
    add_edge("NODE_QA", "CROSS_N_A", "station_access")
    add_edge("NODE_MRB", "CROSS_N_A", "station_access")

    locations: list[dict] = []
    odoo_location_id = 10000
    for aisle in AISLES:
        zone = zone_for_aisle(aisle)
        for bay in BAYS:
            for level in LEVELS:
                for side in SIDES:
                    odoo_location_id += 1
                    z = 2.5 if level == 1 else 7.0
                    x_offset = -4.0 if side == "A" else 4.0
                    code = f"WH/Stock/{zone}/{aisle}-{bay:02d}-L{level}-B{side}"
                    locations.append({
                        "odoo_location_id": odoo_location_id,
                        "odoo_complete_name": code,
                        "barcode": f"LOC-{aisle}{bay:02d}L{level}{side}",
                        "warehouse": "WH",
                        "zone": zone,
                        "aisle": aisle,
                        "bay": bay,
                        "rack": f"R{bay:02d}",
                        "level": level,
                        "bin": side,
                        "x": round(aisle_x[aisle] + x_offset, 3),
                        "y": round(bay_y[bay], 3),
                        "z": z,
                        "graph_node_id": f"NODE_{aisle}_{bay:02d}",
                        "layout_version": LAYOUT_VERSION,
                        "pick_tier": "PRIME" if aisle in "AB" and level == 1 else ("STANDARD" if aisle in "CDE" else "RESERVE"),
                        "capacity_units": 250 if zone != "BULK" else 600,
                        "capacity_weight_lb": 500 if zone != "BULK" else 1800,
                        "secure": zone == "CONTROLLED",
                        "flight_critical_allowed": zone != "BULK",
                    })

    return metadata, stations, locations, nodes, edges


def build_stock_locations(bin_locations, stations):
    rows = [
        {"odoo_location_id": 90000, "complete_name": "WH", "name": "WH", "parent_location_id": "", "usage": "view", "barcode": "WH", "location_kind": "warehouse"},
        {"odoo_location_id": 90002, "complete_name": "WH/Stock", "name": "Stock", "parent_location_id": 90000, "usage": "view", "barcode": "WH-STOCK", "location_kind": "parent"},
        {"odoo_location_id": 90003, "complete_name": "WH/Staging", "name": "Staging", "parent_location_id": 90000, "usage": "internal", "barcode": "WH-STAGING", "location_kind": "station"},
        {"odoo_location_id": 91000, "complete_name": "Partners/Vendors", "name": "Vendors", "parent_location_id": "", "usage": "supplier", "barcode": "VENDORS", "location_kind": "external"},
    ]
    station_names = {
        "ST_RECEIVING": "WH/Receiving",
        "ST_KITTING": "WH/Kitting",
        "ST_SHIPPING": "WH/Shipping",
        "ST_QA": "WH/Quality",
        "ST_MRB": "WH/MRB",
    }
    for station in stations:
        rows.append({
            "odoo_location_id": station["odoo_location_id"],
            "complete_name": station_names[station["station_id"]],
            "name": station["name"],
            "parent_location_id": 90000,
            "usage": "internal",
            "barcode": station["station_id"],
            "location_kind": "station",
        })
    for loc in bin_locations:
        rows.append({
            "odoo_location_id": loc["odoo_location_id"],
            "complete_name": loc["odoo_complete_name"],
            "name": loc["odoo_complete_name"].split("/")[-1],
            "parent_location_id": 90002,
            "usage": "internal",
            "barcode": loc["barcode"],
            "location_kind": "bin",
        })
    return rows


BENCHMARK_PRODUCTS = [
    ("BOLT-104", "High Velocity Titanium Bolt", "Fasteners", 8.50, "none", 0.08, False, "HIGH"),
    ("BRACKET-77", "Low Velocity Structural Bracket", "Structures", 185.00, "lot", 2.6, False, "LOW"),
    ("VALVE-441", "Propulsion Isolation Valve", "Propulsion", 2250.00, "serial", 4.4, True, "MEDIUM"),
    ("BRACKET-221", "Propulsion Mount Bracket", "Structures", 420.00, "lot", 3.1, True, "MEDIUM"),
    ("HARNESS-310", "Avionics Harness Assembly", "Avionics", 1450.00, "serial", 1.8, True, "MEDIUM"),
    ("FASTENER-900", "Flight Critical Fastener Kit", "Fasteners", 95.00, "lot", 0.5, True, "HIGH"),
    ("SEAL-218", "Propulsion Seal", "Seals", 38.00, "lot", 0.1, True, "HIGH"),
    ("REGULATOR-552", "Pressure Regulator", "Propulsion", 1850.00, "serial", 5.0, True, "MEDIUM"),
    ("SERIAL-AVX-7", "Avionics Control Unit", "Avionics", 12500.00, "serial", 7.5, True, "HIGH"),
    ("COMPOSITE-88", "Composite Panel", "Structures", 8200.00, "serial", 18.0, True, "LOW"),
]


def build_products():
    rows = []
    product_id = 2000
    for code, name, category, cost, tracking, weight, flight, velocity in BENCHMARK_PRODUCTS:
        product_id += 1
        rows.append({
            "odoo_product_id": product_id,
            "default_code": code,
            "name": name,
            "category": category,
            "standard_price": cost,
            "tracking": tracking,
            "weight_lb": weight,
            "volume_ft3": round(max(weight / 40.0, 0.02), 3),
            "x_is_flight_critical": flight,
            "velocity_profile": velocity,
            "secure_required": category == "Avionics",
        })

    categories = [
        ("Fasteners", "none", 12.0, 0.15),
        ("Fittings", "lot", 85.0, 0.4),
        ("Seals", "lot", 42.0, 0.1),
        ("Structures", "lot", 390.0, 3.0),
        ("Propulsion", "serial", 2300.0, 5.0),
        ("Avionics", "serial", 5100.0, 4.0),
        ("GSE", "none", 650.0, 12.0),
    ]
    for idx in range(70):
        product_id += 1
        category, tracking, base_cost, base_weight = categories[idx % len(categories)]
        code = f"{category[:3].upper()}-{1000+idx}"
        velocity = "HIGH" if idx < 18 else ("MEDIUM" if idx < 48 else "LOW")
        flight = category in {"Propulsion", "Avionics"} and idx % 3 != 0
        rows.append({
            "odoo_product_id": product_id,
            "default_code": code,
            "name": f"Mock {category} Component {idx+1:02d}",
            "category": category,
            "standard_price": round(base_cost * (1 + (idx % 7) * 0.17), 2),
            "tracking": tracking,
            "weight_lb": round(base_weight * (1 + (idx % 5) * 0.2), 2),
            "volume_ft3": round(max(base_weight / 35.0, 0.02), 3),
            "x_is_flight_critical": flight,
            "velocity_profile": velocity,
            "secure_required": category == "Avionics",
        })
    return rows


def build_boms(products):
    by_code = {p["default_code"]: p for p in products}
    generated_codes = [p["default_code"] for p in products if p["default_code"] not in {b[0] for b in BENCHMARK_PRODUCTS}]
    templates = [
        ("BOM-OTS", "Orbital Transfer Stage", ["VALVE-441", "BRACKET-221", "HARNESS-310", "FASTENER-900", "SEAL-218"] + generated_codes[:7]),
        ("BOM-PROP", "Propulsion Subassembly", ["VALVE-441", "REGULATOR-552", "SEAL-218", "FASTENER-900"] + generated_codes[10:18]),
        ("BOM-AV", "Avionics Panel", ["HARNESS-310", "SERIAL-AVX-7", "FASTENER-900"] + generated_codes[25:34]),
        ("BOM-GSE", "Ground Support Assembly", ["BRACKET-77"] + generated_codes[45:57]),
    ]
    boms, lines = [], []
    for bom_id, name, codes in templates:
        boms.append({"mock_bom_id": bom_id, "name": name, "product_qty": 1})
        for line_no, code in enumerate(codes, start=1):
            p = by_code[code]
            lines.append({
                "mock_bom_line_id": f"{bom_id}-L{line_no:02d}",
                "mock_bom_id": bom_id,
                "component_product_id": p["odoo_product_id"],
                "component_code": code,
                "quantity": 2 if p["category"] in {"Fasteners", "Seals"} else 1,
            })
    return boms, lines


def build_quants(products, locations):
    aisle_candidates = defaultdict(list)
    for loc in locations:
        aisle_candidates[loc["aisle"]].append(loc)

    preferred = {
        "BOLT-104": "WH/Stock/BULK/H-06-L2-BB",
        "BRACKET-77": "WH/Stock/FAST/A-01-L1-BA",
        "VALVE-441": "WH/Stock/CONTROLLED/F-05-L1-BA",
        "BRACKET-221": "WH/Stock/STANDARD/C-03-L1-BA",
        "HARNESS-310": "WH/Stock/CONTROLLED/G-04-L1-BA",
        "FASTENER-900": "WH/Stock/FAST/B-01-L1-BA",
        "SEAL-218": "WH/Stock/FAST/B-02-L1-BA",
        "REGULATOR-552": "WH/Stock/BULK/H-04-L1-BA",
        "SERIAL-AVX-7": "WH/Stock/CONTROLLED/F-02-L1-BA",
        "COMPOSITE-88": "WH/Stock/BULK/H-06-L1-BA",
    }
    by_loc = {loc["odoo_complete_name"]: loc for loc in locations}

    rows = []
    quant_id = 30000
    for idx, product in enumerate(products):
        code = product["default_code"]
        if code in preferred:
            loc = by_loc[preferred[code]]
        else:
            if product["secure_required"]:
                aisle = "F" if idx % 2 == 0 else "G"
            elif product["velocity_profile"] == "HIGH":
                aisle = "A" if idx % 2 == 0 else "B"
            elif product["velocity_profile"] == "MEDIUM":
                aisle = "CDE"[idx % 3]
            else:
                aisle = "H"
            candidates = [l for l in aisle_candidates[aisle] if l["level"] == (1 if product["velocity_profile"] != "LOW" else 2)]
            loc = candidates[idx % len(candidates)]
        qty = 220 if product["velocity_profile"] == "HIGH" else (90 if product["velocity_profile"] == "MEDIUM" else 18)
        if code == "SEAL-218":
            qty = 12
        reserved = min(qty * 0.15, qty - 1)
        if code == "SEAL-218":
            reserved = 10
        quant_id += 1
        rows.append({
            "odoo_quant_id": quant_id,
            "product_id": product["odoo_product_id"],
            "product_code": code,
            "location_id": loc["odoo_location_id"],
            "location_code": loc["odoo_complete_name"],
            "quantity": round(qty, 3),
            "reserved_quantity": round(reserved, 3),
        })
        if idx % 17 == 0 and code not in {"BOLT-104", "BRACKET-77"}:
            second = aisle_candidates["E"][(idx * 3) % len(aisle_candidates["E"])]
            quant_id += 1
            rows.append({
                "odoo_quant_id": quant_id,
                "product_id": product["odoo_product_id"],
                "product_code": code,
                "location_id": second["odoo_location_id"],
                "location_code": second["odoo_complete_name"],
                "quantity": round(max(qty * 0.12, 2), 3),
                "reserved_quantity": 0,
            })
    return rows


def build_mos_and_moves(products, locations, bom_lines):
    rng = random.Random(SEED)
    line_by_bom = defaultdict(list)
    for line in bom_lines:
        line_by_bom[line["mock_bom_id"]].append(line)

    programs = [("BOM-OTS", "OTS"), ("BOM-PROP", "PROP"), ("BOM-AV", "AVIONICS"), ("BOM-GSE", "GSE")]
    now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
    mos, pickings, move_specs = [], [], []
    mo_id = 50000
    picking_id = 60000

    for i in range(120):
        bom_id, program = programs[i % len(programs)]
        mo_id += 1
        picking_id += 1
        day_age = (119 - i) % 90
        start = now - timedelta(days=day_age, hours=(i % 8))
        lines = list(line_by_bom[bom_id])
        if i % 5 == 0 and len(lines) > 8:
            lines = lines[:-2]
        pick_minutes = 12 + len(lines) * 1.25 + rng.uniform(0, 9)
        shortage = ("SEAL-218" in {line["component_code"] for line in lines}) and (i % 11 == 0)
        if shortage:
            pick_minutes += 22
        finish = start + timedelta(minutes=pick_minutes)
        mo_name = f"MO/MOCK/{mo_id}"
        picking_name = f"PICK/MOCK/{picking_id}"
        mos.append({
            "mock_mo_id": mo_id,
            "name": mo_name,
            "program": program,
            "mock_bom_id": bom_id,
            "state": "done",
            "date_start": start.isoformat(),
            "date_finished": finish.isoformat(),
            "component_line_count": len(lines),
            "shortage_affected": shortage,
            "first_pass_complete": not shortage,
        })
        pickings.append({
            "mock_picking_id": picking_id,
            "name": picking_name,
            "picking_type": "Pick Components",
            "origin": mo_name,
            "program": program,
            "scheduled_date": start.isoformat(),
            "date_done": finish.isoformat(),
            "state": "done",
            "line_count": len(lines),
            "shortage_affected": shortage,
        })
        shuffled = list(lines)
        rng.shuffle(shuffled)
        for seq, line in enumerate(shuffled, start=1):
            move_time = start + timedelta(minutes=2 + seq * 1.3)
            move_specs.append({
                "mock_move_line_id": f"ML-{picking_id}-{seq:02d}",
                "picking_id": picking_id,
                "picking_name": picking_name,
                "origin_mo": mo_name,
                "program": program,
                "product_id": line["component_product_id"],
                "product_code": line["component_code"],
                "quantity": line["quantity"],
                "date": move_time.isoformat(),
                "event_type": "kit_pick",
                "write_uid": 10 + (i % 6),
            })

    return mos, pickings, move_specs


def attach_move_locations(move_specs, quants, products, locations):
    primary_location = {}
    for quant in quants:
        primary_location.setdefault(quant["product_id"], quant)
    rows = []
    for row in move_specs:
        quant = primary_location[row["product_id"]]
        rows.append({
            **row,
            "location_id": quant["location_id"],
            "location_code": quant["location_code"],
            "location_dest_id": 90001,
            "location_dest_code": "WH/Kitting",
        })
    return rows


def add_general_moves(move_lines, products, quants):
    rng = random.Random(SEED + 7)
    now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
    primary = {}
    for quant in quants:
        primary.setdefault(quant["product_id"], quant)

    rows = list(move_lines)
    counter = 80000
    profiles = {"HIGH": 28, "MEDIUM": 11, "LOW": 3}
    for product in products:
        count = profiles[product["velocity_profile"]]
        if product["default_code"] == "SERIAL-AVX-7":
            count = 55
        for n in range(count):
            counter += 1
            age_days = (n % 7) + rng.random() if product["default_code"] == "SERIAL-AVX-7" else rng.uniform(0, 28)
            quant = primary[product["odoo_product_id"]]
            event = "internal_transfer" if n % 4 else "receipt"
            if event == "receipt":
                source_id, source_code = 91000, "Partners/Vendors"
                dest_id, dest_code = quant["location_id"], quant["location_code"]
            else:
                source_id, source_code = quant["location_id"], quant["location_code"]
                dest_id, dest_code = 90003, "WH/Staging"
            rows.append({
                "mock_move_line_id": f"ML-GEN-{counter}",
                "picking_id": "",
                "picking_name": f"GEN/{counter}",
                "origin_mo": "",
                "program": "",
                "product_id": product["odoo_product_id"],
                "product_code": product["default_code"],
                "quantity": 1 if product["tracking"] == "serial" else (2 + (n % 8)),
                "date": (now - timedelta(days=age_days)).isoformat(),
                "event_type": event,
                "write_uid": 20 + (n % 8),
                "location_id": source_id,
                "location_code": source_code,
                "location_dest_id": dest_id,
                "location_dest_code": dest_code,
            })
    rows.sort(key=lambda row: row["date"])
    return rows


def expected_findings():
    return {
        "layout_version": LAYOUT_VERSION,
        "benchmarks": [
            {"id": "bad_slotting_high_velocity", "product_code": "BOLT-104", "expected": "High-velocity SKU intentionally stored in far BULK H-06 reserve location; slotting engine should flag it."},
            {"id": "premium_slot_misuse", "product_code": "BRACKET-77", "expected": "Low-velocity SKU intentionally occupies a PRIME A-01 pick face."},
            {"id": "co_pick_affinity", "product_codes": ["VALVE-441", "BRACKET-221", "HARNESS-310", "FASTENER-900"], "expected": "Core Orbital Transfer Stage components repeatedly occur together and should form a strong affinity cluster."},
            {"id": "cycle_count_risk", "product_code": "SERIAL-AVX-7", "expected": "Serial-tracked flight-critical avionics SKU has 55 synthetic touches inside seven days."},
            {"id": "shortage_prone_component", "product_code": "SEAL-218", "expected": "Low available quantity and periodic shortage-affected kits should produce material-risk signals."},
            {"id": "putaway_inefficiency", "product_code": "REGULATOR-552", "expected": "Medium-velocity propulsion component intentionally located in distant BULK storage."},
        ],
    }


def generate(output_dir: Path):
    metadata, stations, locations, nodes, edges = build_warehouse()
    stock_locations = build_stock_locations(locations, stations)
    products = build_products()
    boms, bom_lines = build_boms(products)
    quants = build_quants(products, locations)
    mos, pickings, move_specs = build_mos_and_moves(products, locations, bom_lines)
    move_lines = attach_move_locations(move_specs, quants, products, locations)
    move_lines = add_general_moves(move_lines, products, quants)

    warehouse_dir = output_dir / "mock_warehouse"
    odoo_dir = output_dir / "mock_odoo"

    write_json(warehouse_dir / "warehouse_metadata.json", metadata)
    write_csv(warehouse_dir / "stations.csv", stations)
    write_csv(warehouse_dir / "locations.csv", locations)
    write_csv(warehouse_dir / "nodes.csv", nodes)
    write_csv(warehouse_dir / "edges.csv", edges)

    write_csv(odoo_dir / "stock_locations.csv", stock_locations)
    write_csv(odoo_dir / "products.csv", products)
    write_csv(odoo_dir / "quants.csv", quants)
    write_csv(odoo_dir / "boms.csv", boms)
    write_csv(odoo_dir / "bom_lines.csv", bom_lines)
    write_csv(odoo_dir / "manufacturing_orders.csv", mos)
    write_csv(odoo_dir / "stock_pickings.csv", pickings)
    write_csv(odoo_dir / "stock_move_lines.csv", move_lines)
    write_json(output_dir / "expected_findings.json", expected_findings())

    manifest = {
        "seed": SEED,
        "layout_version": LAYOUT_VERSION,
        "generated_at": "deterministic",
        "counts": {
            "stations": len(stations),
            "bin_locations": len(locations),
            "graph_nodes": len(nodes),
            "graph_edges": len(edges),
            "stock_locations": len(stock_locations),
            "products": len(products),
            "quants": len(quants),
            "boms": len(boms),
            "bom_lines": len(bom_lines),
            "manufacturing_orders": len(mos),
            "stock_pickings": len(pickings),
            "stock_move_lines": len(move_lines),
        },
    }
    write_json(output_dir / "manifest.json", manifest)
    return manifest


def main():
    parser = argparse.ArgumentParser(description="Generate deterministic AWIA simulation sandbox data.")
    parser.add_argument("--output", default="data/simulation_sandbox", help="Output directory")
    args = parser.parse_args()
    manifest = generate(Path(args.output))
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
