"""Fault catalogue loader."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CATALOG = REPO_ROOT / "faults" / "faults.yaml"


@dataclass(frozen=True)
class FaultVariant:
    fault: str
    variant: str
    name: str
    config_path: str | None
    commands: tuple[str, ...]

    @property
    def key(self) -> str:
        return f"{self.fault}-{self.variant}"


def load_catalog(path: Path = DEFAULT_CATALOG) -> dict[str, FaultVariant]:
    data = yaml.safe_load(Path(path).read_text())
    out: dict[str, FaultVariant] = {}
    for fid, spec in data["faults"].items():
        for vid, v in spec["variants"].items():
            fv = FaultVariant(
                fault=fid,
                variant=vid,
                name=spec["name"],
                config_path=v.get("config_path"),
                commands=tuple(" ".join(c.split()) for c in v.get("commands", [])),
            )
            out[fv.key] = fv
    return out


def fault_names(path: Path = DEFAULT_CATALOG) -> dict[str, str]:
    data = yaml.safe_load(Path(path).read_text())
    return {fid: spec["name"] for fid, spec in data["faults"].items()}
