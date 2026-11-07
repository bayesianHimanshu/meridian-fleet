"""The finding dedup key: who found what, where, on which cut, with which check.

Format: "<agent_id>@<agent_version>|<kind>|<study_id>|<site_id or STUDY>|<cut_id>|<check or ->".
Keys written before agent versions were part of identity (phase 1 step 3) have no
"@<version>"; they parse with agent_version None.
"""

from typing import NamedTuple

from meridian.domain.enums import FindingKind

STUDY_SCOPE = "STUDY"
NO_CHECK = "-"
SEPARATOR = "|"
VERSION_MARK = "@"


class DedupKeyParts(NamedTuple):
    agent_id: str
    agent_version: str | None
    kind: str
    study_id: str
    site: str
    cut_id: str
    check: str


def make_dedup_key(
    *,
    agent_id: str,
    agent_version: str,
    kind: FindingKind,
    study_id: str,
    site_id: str | None,
    cut_id: str,
    check: str | None,
) -> str:
    parts = (
        f"{agent_id}{VERSION_MARK}{agent_version}",
        kind.value,
        study_id,
        site_id or STUDY_SCOPE,
        cut_id,
        check or NO_CHECK,
    )
    for part in (agent_id, agent_version, *parts[1:]):
        if not part or SEPARATOR in part:
            raise ValueError(f"dedup key part {part!r} is empty or contains {SEPARATOR!r}")
    if VERSION_MARK in agent_id or VERSION_MARK in agent_version:
        raise ValueError(f"agent id and version may not contain {VERSION_MARK!r}")
    return SEPARATOR.join(parts)


def parse_dedup_key(key: str) -> DedupKeyParts:
    parts = key.split(SEPARATOR)
    if len(parts) != 6:
        raise ValueError(f"not a dedup key: {key!r}")
    who, kind, study_id, site, cut_id, check = parts
    agent_id, mark, version = who.partition(VERSION_MARK)
    return DedupKeyParts(agent_id, version if mark else None, kind, study_id, site, cut_id, check)


def dedup_prefix(agent_id: str, agent_version: str) -> str:
    return f"{agent_id}{VERSION_MARK}{agent_version}{SEPARATOR}"
