import pytest

from scripts.execute_remaining_kitting_transfers import collect_execution_queue


def _report():
    return {
        "transactions": [
            {
                "manufacturing_order": "WH/MO/00001",
                "awia_origin": "AWIA-MOCK-MO-001",
                "product": "FG-1",
                "pick_component_transfers": [
                    {
                        "picking_id": 1,
                        "picking_name": "WH/PC/00001",
                        "state": "done",
                        "execution_ready": False,
                        "component_move_count": 10,
                        "move_line_count": 10,
                    }
                ],
            },
            {
                "manufacturing_order": "WH/MO/00003",
                "awia_origin": "AWIA-MOCK-MO-003",
                "product": "FG-3",
                "pick_component_transfers": [
                    {
                        "picking_id": 3,
                        "picking_name": "WH/PC/00003",
                        "state": "assigned",
                        "execution_ready": True,
                        "component_move_count": 12,
                        "move_line_count": 12,
                    }
                ],
            },
            {
                "manufacturing_order": "WH/MO/00002",
                "awia_origin": "AWIA-MOCK-MO-002",
                "product": "FG-2",
                "pick_component_transfers": [
                    {
                        "picking_id": 2,
                        "picking_name": "WH/PC/00002",
                        "state": "assigned",
                        "execution_ready": True,
                        "component_move_count": 11,
                        "move_line_count": 11,
                    }
                ],
            },
            {
                "manufacturing_order": "WH/MO/00004",
                "awia_origin": "AWIA-MOCK-MO-004",
                "product": "FG-4",
                "pick_component_transfers": [
                    {
                        "picking_id": 4,
                        "picking_name": "WH/PC/00004",
                        "state": "assigned",
                        "execution_ready": False,
                        "component_move_count": 9,
                        "move_line_count": 9,
                    }
                ],
            },
        ]
    }


def test_collect_execution_queue_skips_done_and_not_ready_and_sorts():
    queue = collect_execution_queue(_report())
    assert [row["picking_id"] for row in queue] == [2, 3]
    assert [row["awia_origin"] for row in queue] == [
        "AWIA-MOCK-MO-002",
        "AWIA-MOCK-MO-003",
    ]


def test_collect_execution_queue_respects_max_transfers():
    queue = collect_execution_queue(_report(), max_transfers=1)
    assert [row["picking_id"] for row in queue] == [2]


def test_collect_execution_queue_rejects_nonpositive_cap():
    with pytest.raises(ValueError, match="greater than zero"):
        collect_execution_queue(_report(), max_transfers=0)
