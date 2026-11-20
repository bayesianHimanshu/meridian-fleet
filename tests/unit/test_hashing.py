import hashlib
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import pytest

from meridian.domain.enums import Role
from meridian.ledger.hashing import GENESIS_HASH, canonical_json, compute_hash, format_utc


def test_canonical_json_is_key_order_independent() -> None:
    a = {"b": 1, "a": {"y": [1, 2], "x": None}}
    b = {"a": {"x": None, "y": [1, 2]}, "b": 1}
    assert canonical_json(a) == canonical_json(b) == '{"a":{"x":null,"y":[1,2]},"b":1}'


def test_canonical_json_is_stable() -> None:
    obj = {"z": 0.1, "when": datetime(2026, 1, 2, 3, 4, 5, 6, tzinfo=UTC), "s": "café"}
    first = canonical_json(obj)
    assert all(canonical_json(obj) == first for _ in range(10))
    assert first == '{"s":"café","when":"2026-01-02T03:04:05.000006Z","z":0.1}'


def test_datetimes_are_utc_with_z() -> None:
    plus_two = timezone(timedelta(hours=2))
    assert canonical_json(datetime(2026, 1, 1, 2, 0, tzinfo=plus_two)) == (
        '"2026-01-01T00:00:00.000000Z"'
    )


def test_naive_datetime_is_rejected() -> None:
    with pytest.raises(ValueError, match="naive"):
        format_utc(datetime(2026, 1, 1))  # noqa: DTZ001


@pytest.mark.parametrize("value", [0.1, 1e-300, 123456789.123456789, -0.0, 1e22])
def test_floats_use_repr(value: float) -> None:
    assert canonical_json(value) == repr(value)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_floats_are_rejected(value: float) -> None:
    with pytest.raises(ValueError, match="non-finite"):
        canonical_json(value)


def test_sets_are_sorted_and_enums_are_values() -> None:
    roles = frozenset({Role.QA_AUDITOR, Role.MEDICAL_MONITOR, Role.TOXICOLOGIST})
    assert canonical_json(roles) == '["medical_monitor","qa_auditor","toxicologist"]'


def test_uuid_and_tuple() -> None:
    uid = UUID("01890000-0000-7000-8000-000000000000")
    assert canonical_json((uid, 1)) == '["01890000-0000-7000-8000-000000000000",1]'


def test_unknown_types_are_rejected() -> None:
    with pytest.raises(TypeError):
        canonical_json(object())


def test_compute_hash_is_sha256_of_prev_plus_canonical() -> None:
    body = {"b": 2, "a": 1}
    expected = hashlib.sha256((GENESIS_HASH + '{"a":1,"b":2}').encode()).hexdigest()
    assert compute_hash(GENESIS_HASH, body) == expected
    assert GENESIS_HASH == "0" * 64
