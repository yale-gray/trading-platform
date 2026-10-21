"""Load and dump strategy specs (YAML via ``yaml.safe_load``, or JSON)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import TypeAdapter

from tradebot.strategy.schema import RotationSpec, RulesSpec, StrategySpec, _strategy_adapter

# src/tradebot/strategy/io.py -> repo root is parents[3]
STRATEGIES_DIR = Path(__file__).resolve().parents[3] / "strategies"

_ADAPTER: TypeAdapter[RotationSpec | RulesSpec] = _strategy_adapter()


class _UniqueKeyLoader(yaml.SafeLoader):
    """``SafeLoader`` that rejects duplicate mapping keys."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        seen: set[Any] = set()
        for key_node, _ in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge":
                continue
            key = self.construct_object(key_node, deep=True)
            try:
                dup = key in seen
            except TypeError:
                continue  # unhashable: the base constructor reports it
            if dup:
                raise ValueError(f"duplicate key {key!r} in strategy document")
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def _no_dupes(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise ValueError(f"duplicate key {k!r} in strategy document")
        out[k] = v
    return out


def _parse_text(text: str) -> Any:
    """Parse JSON when the text starts with ``{``, else YAML (safe, duplicate keys rejected)."""
    if text.lstrip().startswith("{"):
        return json.loads(text, object_pairs_hook=_no_dupes)
    return yaml.load(text, Loader=_UniqueKeyLoader)  # noqa: S506 - SafeLoader subclass


def load_strategy(path_or_text: str | Path) -> StrategySpec:
    """Load a strategy from a file path (``Path`` or an existing-file string) or raw text.

    Format is JSON if the content starts with ``{``, otherwise YAML. Raises
    ``pydantic.ValidationError`` on an invalid spec.
    """
    text: str
    if isinstance(path_or_text, Path):
        text = path_or_text.read_text(encoding="utf-8")
    else:
        text = path_or_text
        if "\n" not in text and not text.lstrip().startswith("{"):
            try:
                if Path(text).is_file():
                    text = Path(text).read_text(encoding="utf-8")
            except OSError:
                pass  # not a usable path: treat as raw text
    data = _parse_text(text)
    if not isinstance(data, dict):
        raise ValueError(f"strategy document must be a mapping, got {type(data).__name__}")
    return _ADAPTER.validate_python(data)


def dump_strategy(spec: StrategySpec, fmt: Literal["yaml", "json"] = "yaml") -> str:
    """Serialise a spec to YAML or JSON text. ``load_strategy`` of the result equals ``spec``."""
    data = spec.model_dump(mode="json", by_alias=True, exclude_none=True)
    if fmt == "json":
        return json.dumps(data, indent=2)
    if fmt == "yaml":
        return yaml.safe_dump(data, sort_keys=False, default_flow_style=False)
    raise ValueError(f"unknown format {fmt!r}")
