"""Development identity: NOT authentication.

A request names a person in X-Meridian-Actor and a role in X-Meridian-Role. The API
accepts it only if config/people.toml lists the person with exactly that role; a missing
header, an unknown person or a mismatched role is 401. Anyone who can reach the port can
claim to be anyone in the file: this exists so the Command Center can be built and tested
before real authentication (docs/api-notes.md says what that needs).
"""

import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from meridian.domain.enums import Role

ACTOR_HEADER = "X-Meridian-Actor"
ROLE_HEADER = "X-Meridian-Role"
AUTH_HEADER = "X-Meridian-Auth"
AUTH_MODE = "development"


class Person(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    actor: str
    name: str
    role: Role


class People(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    people: dict[str, Person]

    @classmethod
    def load(cls, path: Path) -> People:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)["people"]
        return cls(
            people={
                actor: Person(actor=actor, name=entry["name"], role=Role(entry["role"]))
                for actor, entry in raw.items()
            }
        )

    def resolve(self, actor: str | None, role: str | None) -> Person | None:
        """The person, if the headers name a known person with their own role; else None."""
        if not actor or not role:
            return None
        person = self.people.get(actor)
        if person is None or person.role.value != role:
            return None
        return person
