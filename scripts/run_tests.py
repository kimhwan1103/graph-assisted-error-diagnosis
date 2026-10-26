"""Run the portable test suite with a fresh, repository-local temporary directory."""

from pathlib import Path
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[1]


def main():
    temporary = (ROOT / ".test-tmp" / uuid4().hex[:12]).resolve()
    if not temporary.is_relative_to(ROOT) or temporary.exists():
        raise ValueError("Tests require a new temporary directory inside this repository")
    temporary.parent.mkdir(parents=True, exist_ok=True)
    return pytest.main(
        ["-q", str(ROOT / "tests"), "-p", "no:cacheprovider", "--basetemp", str(temporary)]
    )


if __name__ == "__main__":
    raise SystemExit(main())
