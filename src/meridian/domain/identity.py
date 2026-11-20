"""Who an actor is (phase 5 step 3b): a person, an operator, an agent or the system.

A person (config/people.toml) acts only through the Fleet API, with their identity; nothing
else may record a person as having done something. CLI commands and the runner act under a
non-person identity: an operator (``operator:<os user>``), an agent (its id), or a system
component (``system.*``, ``meridian.*``). Step 3a's signal scan was recorded as requested by
the medical monitor from the CLI; this module is the rule that prevents it.
"""

import getpass
import re
import tomllib
from collections.abc import Collection
from pathlib import Path
from typing import Literal

OPERATOR_PREFIX = "operator:"
ActorKind = Literal["person", "operator", "agent", "system"]
_OPERATOR = re.compile(r"^operator:[a-z_][a-z0-9_.-]{0,62}$")


class NotAnOperatorError(ValueError):
    pass


def operator_id(user: str | None = None) -> str:
    """operator:<os user>, lower-cased, with any character outside the allowed set as _."""
    name = (user if user is not None else getpass.getuser()).lower()
    name = re.sub(r"[^a-z0-9_.-]", "_", name) or "unknown"
    if not re.match(r"[a-z_]", name[0]):
        name = "_" + name
    return OPERATOR_PREFIX + name[:63]


def is_operator(actor: str) -> bool:
    return bool(_OPERATOR.fullmatch(actor))


def kind_of(actor: str, people: Collection[str], agents: Collection[str] = ()) -> ActorKind:
    if actor in people:
        return "person"
    if is_operator(actor):
        return "operator"
    if actor in agents:
        return "agent"
    return "system"


def require_operator(actor: str, people: Collection[str]) -> str:
    """The id a CLI command records: an operator id, never a person's."""
    if actor in people:
        raise NotAnOperatorError(
            f"{actor} is a person: a person acts only through the Fleet API with their own "
            "identity; the CLI records an operator (operator:<os user>)"
        )
    if not is_operator(actor):
        raise NotAnOperatorError(f"{actor!r} is not an operator id (operator:<name>)")
    return actor


def people_ids(path: Path) -> frozenset[str]:
    """The person ids of config/people.toml (the Fleet API's development identities)."""
    with path.open("rb") as handle:
        return frozenset(tomllib.load(handle)["people"])
