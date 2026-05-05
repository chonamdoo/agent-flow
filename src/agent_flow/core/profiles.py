from __future__ import annotations

from pathlib import Path


def detect_profile(root: Path) -> str:
    if (root / "package.json").exists():
        package_text = (root / "package.json").read_text(encoding="utf-8", errors="ignore")
        if "react-native" in package_text:
            return "react-native"
        if "next" in package_text:
            return "nextjs"
        return "node"
    if (root / "pyproject.toml").exists():
        return "python"
    return "generic"

