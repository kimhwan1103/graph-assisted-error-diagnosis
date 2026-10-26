"""Archive an interrupted cell ONLY when no request was dispatched. No API/Docker calls."""

import argparse
import contextlib
import json
import os
import re
from pathlib import Path

from cross_program_common_v4 import ROOT, hashes, load_source, now, read, write


@contextlib.contextmanager
def existing_exclusive_lock(source):
    """Exclude the runner and other recovery processes without changing writer.lock."""
    path = source / "writer.lock"
    if not path.is_file() or path.stat().st_size < 1:
        raise ValueError("Existing nonempty writer.lock is required")
    with path.open("r+b") as handle:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def inside(source, path):
    resolved = path.resolve()
    if source not in resolved.parents or resolved != path.absolute():
        raise ValueError("Recovery path must stay inside the run without links")
    return resolved


def inspect_cell(source, cell_id, frozen):
    if not re.fullmatch(r"[a-f0-9]{20}", cell_id):
        raise ValueError("Invalid cell ID")
    slots = [s for s in frozen["schedule"] if s["cell_id"] == cell_id]
    if len(slots) != 1:
        raise ValueError("Cell must appear exactly once in the frozen schedule")
    cell = inside(source, source / "cells" / cell_id)
    state = read(cell / "state.json")
    if state.get("status") != "started" or any(
        state.get(k) != value for k, value in slots[0].items()
    ):
        raise ValueError("Only the matching interrupted started cell can be recovered")
    for path in cell.rglob("*"):
        inside(source, path)
        name = path.relative_to(cell).as_posix()
        if path.is_dir() and name == "calls":
            continue
        if not path.is_file() or not (
            name == "state.json" or re.fullmatch(r"calls/[0-9]{2}\.json", name)
        ):
            raise ValueError(f"Unexpected cell artifact; preserve and investigate: {name}")
    calls = sorted((cell / "calls").glob("*.json"))
    for path in calls:
        call = read(path)
        if (
            call.get("status") != "prepared"
            or call.get("dispatched") is not False
            or any(k in call for k in ("raw", "error", "dispatch_time", "network_ms"))
        ):
            raise ValueError("A call may have been dispatched; recovery is forbidden")
    archive = inside(source, source / "interruption-recovery" / cell_id)
    if archive.exists():
        raise ValueError("Recovery archive already exists; never overwrite it")
    return (
        cell,
        archive,
        {
            "version": "cross-program-undispatched-recovery.v1",
            "action": "archive_undispatched_cell_for_resume",
            "reason": "User stopped the run before any request in this cell was dispatched",
            "cell_id": cell_id,
            "prepared_calls": len(calls),
            "dispatched_calls": 0,
            "source_files_sha256": hashes(cell),
            "frozen_schedule_unchanged": True,
            "new_model_calls": 0,
        },
    )


def recover(source, cell_id, frozen, *, apply=False):
    """Caller holds the exclusive lock; archive all bytes instead of excluding a cell."""
    cell, archive, record = inspect_cell(source, cell_id, frozen)
    if apply:
        archive.mkdir(parents=True, exist_ok=False)
        write(archive / "recovery.json", {**record, "at": now()})
        destination = inside(source, archive / "cell")
        # Both absolute paths have been checked inside this run; never overwrite a target.
        if destination.exists():
            raise ValueError("Archive destination exists")
        cell.rename(destination)
        if hashes(destination) != record["source_files_sha256"]:
            raise ValueError("Archived bytes differ; preserve and investigate")
    return {
        "cell_id": cell_id,
        "applied": apply,
        "safe_to_resume": True,
        "prepared_calls": record["prepared_calls"],
        "dispatched_calls": 0,
        "archive": str(archive),
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source-run", type=Path, default=ROOT / "results/cross-program-v4-live")
    p.add_argument("--cell", required=True)
    p.add_argument("--apply", action="store_true", help="Archive the cell; otherwise inspect only")
    a = p.parse_args()
    source = a.source_run.resolve()
    if (ROOT / "results").resolve() not in source.parents:
        raise ValueError("Recovery is restricted to this project's results directory")
    with existing_exclusive_lock(source):
        frozen = load_source(source, ROOT / "evaluation/cross-program-v4")
        result = recover(source, a.cell, frozen, apply=a.apply)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
