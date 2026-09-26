"""Repository-owned input for legacy manifest regression tests."""

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MANIFEST = REPO / "tests" / "fixtures" / "drift-synthetic" / "services.toml"
