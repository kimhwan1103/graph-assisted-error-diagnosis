"""Verify publication bytes and provenance locally using the standard library."""

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    manifest = json.loads((ROOT / "release-manifest.json").read_text("utf-8"))
    provenance = json.loads((ROOT / "publication-provenance.json").read_text("utf-8"))
    errors = []
    for name, digest in manifest["files"].items():
        path = (ROOT / name).resolve()
        if not path.is_relative_to(ROOT) or not path.is_file() or sha(path) != digest:
            errors.append(name)
    for name, record in provenance["copied_files"].items():
        path = ROOT / name
        if not path.is_file() or sha(path) != record["export_sha256"]:
            errors.append("provenance:" + name)
        if (
            record["transformation"] == "byte-identical"
            and record["source_sha256"] != record["export_sha256"]
        ):
            errors.append("source:" + name)
    print(
        json.dumps(
            {
                "passed": not errors,
                "published_files_verified": len(manifest["files"]),
                "source_copies_verified": len(provenance["copied_files"]),
                "errors": errors,
                "model_calls": 0,
                "docker_calls": 0,
                "scope": "Publication file integrity, not independent raw-run validation",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
