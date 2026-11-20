"""Pilot SITEID to Meridian site id mapping, kept in config/sites.toml."""

import re
import tomllib
from collections.abc import Iterable, Mapping
from pathlib import Path

RULE = "meridian_id = str(1000 + int(SITEID) - 700)"
_ASCII_DIGITS = re.compile(r"[0-9]+")
_MERIDIAN_ID = re.compile(r"[0-9]{4}")


class SiteMappingError(Exception):
    """Raised when pilot site ids do not fit the rule or the mapping file is unusable."""


def _apply_rule(pilot_site_id: str) -> str | None:
    if not _ASCII_DIGITS.fullmatch(pilot_site_id):
        return None
    result = str(1000 + int(pilot_site_id) - 700)
    return result if _MERIDIAN_ID.fullmatch(result) else None


def meridian_site_id(pilot_site_id: str) -> str:
    result = _apply_rule(pilot_site_id)
    if result is None:
        raise SiteMappingError(f"SITEID {pilot_site_id!r} does not fit the rule {RULE}")
    return result


def build_site_map(pilot_site_ids: Iterable[str]) -> dict[str, str]:
    """Apply the rule to every distinct SITEID. Reports every offending value at once."""
    distinct = sorted(set(pilot_site_ids))
    bad = [s for s in distinct if _apply_rule(s) is None]
    if bad:
        raise SiteMappingError(
            f"SITEID value(s) that do not fit the rule {RULE}: {bad}; all values found: {distinct}"
        )
    mapping = {s: meridian_site_id(s) for s in distinct}
    if len(set(mapping.values())) != len(mapping):
        raise SiteMappingError(f"rule produced duplicate Meridian ids: {mapping}")
    return mapping


def render_site_map(mapping: Mapping[str, str]) -> str:
    lines = [
        "# Pilot SITEID to Meridian site id, generated once from the CDISC pilot DM domain.",
        f"# Rule: {RULE}. This file is the source of truth; ingest always reads it.",
        "",
        f'rule = "{RULE}"',
        "",
        "[sites]",
        *(f'"{pilot}" = "{meridian}"' for pilot, meridian in sorted(mapping.items())),
        "",
    ]
    return "\n".join(lines)


def write_site_map(path: Path, mapping: Mapping[str, str]) -> None:
    """Create the file. Never overwrites an existing mapping."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(render_site_map(mapping))


def load_site_map(path: Path) -> dict[str, str]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    sites = data.get("sites")
    if not isinstance(sites, dict) or not sites:
        raise SiteMappingError(f"{path} has no [sites] table")
    mapping: dict[str, str] = {}
    for pilot, meridian in sites.items():
        if not isinstance(meridian, str) or not _MERIDIAN_ID.fullmatch(meridian):
            raise SiteMappingError(f"{path}: site {pilot!r} maps to invalid id {meridian!r}")
        mapping[pilot] = meridian
    if len(set(mapping.values())) != len(mapping):
        raise SiteMappingError(f"{path} maps two pilot sites to the same Meridian id")
    return mapping


def ensure_site_map(path: Path, pilot_site_ids: Iterable[str]) -> dict[str, str]:
    """Generate the file from the data if it does not exist, then always read it back."""
    ids = sorted(set(pilot_site_ids))
    if not path.exists():
        write_site_map(path, build_site_map(ids))
    mapping = load_site_map(path)
    missing = [s for s in ids if s not in mapping]
    if missing:
        raise SiteMappingError(f"{path} has no mapping for SITEID value(s) {missing}")
    return mapping
