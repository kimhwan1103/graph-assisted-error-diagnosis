"""Offline tests of recovery guards; never execute model calls or Docker."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from recover_undispatched_cross_program_v4 import recover


@pytest.fixture
def interrupted(tmp_path):
    source = tmp_path.resolve()
    slot = {"cell_id": "a" * 20, "case_id": "case", "arm": "adaptive", "repeat": 0}
    cell = source / "cells" / slot["cell_id"]
    (cell / "calls").mkdir(parents=True)
    (cell / "state.json").write_text(json.dumps({**slot, "status": "started"}))
    call = cell / "calls/00.json"
    call.write_text(json.dumps({"status": "prepared", "dispatched": False}))
    return source, slot, cell, call, {"schedule": [slot]}


def test_check_is_readonly_and_apply_preserves_bytes(interrupted):
    source, slot, cell, _, frozen = interrupted
    previous = {p.relative_to(cell): p.read_bytes() for p in cell.rglob("*") if p.is_file()}
    other = source / "cells/other-result.json"
    other.write_bytes(b"existing completed result")
    result = recover(source, slot["cell_id"], frozen)
    assert not result["applied"] and cell.exists()
    assert not (source / "interruption-recovery").exists()
    result = recover(source, slot["cell_id"], frozen, apply=True)
    archived = Path(result["archive"]) / "cell"
    assert not cell.exists()
    assert previous == {
        p.relative_to(archived): p.read_bytes() for p in archived.rglob("*") if p.is_file()
    }
    assert other.read_bytes() == b"existing completed result"
    record = json.loads((archived.parent / "recovery.json").read_text())
    assert record["new_model_calls"] == 0 and record["dispatched_calls"] == 0


@pytest.mark.parametrize(
    "change",
    [
        {"dispatched": True},
        {"dispatched": None},
        {"status": "in_flight"},
        {"status": "completed"},
        {"raw": {}},
        {"dispatch_time": 0},
        {"error": "timeout"},
    ],
)
def test_ambiguous_or_sent_requests_are_never_reset(interrupted, change):
    source, slot, cell, call, frozen = interrupted
    call.write_text(json.dumps({"status": "prepared", "dispatched": False, **change}))
    with pytest.raises(ValueError, match="dispatched"):
        recover(source, slot["cell_id"], frozen, apply=True)
    assert cell.exists() and not (source / "interruption-recovery").exists()


@pytest.mark.parametrize("artifact", ["result.json", "calls/00.json.tmp"])
def test_terminal_or_uncommitted_artifact_blocks_recovery(interrupted, artifact):
    source, slot, cell, _, frozen = interrupted
    (cell / artifact).write_text("{}")
    with pytest.raises(ValueError, match="Unexpected"):
        recover(source, slot["cell_id"], frozen, apply=True)


def test_identity_and_archive_overwrite_are_rejected(interrupted):
    source, slot, cell, _, frozen = interrupted
    with pytest.raises(ValueError, match="Invalid cell ID"):
        recover(source, "../outside", frozen, apply=True)
    with pytest.raises(ValueError, match="exactly once"):
        recover(source, slot["cell_id"], {"schedule": []}, apply=True)
    (source / "interruption-recovery" / slot["cell_id"]).mkdir(parents=True)
    with pytest.raises(ValueError, match="never overwrite"):
        recover(source, slot["cell_id"], frozen, apply=True)
    assert cell.exists()
