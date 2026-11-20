from pathlib import Path

import pytest

from meridian.data.sites import (
    SiteMappingError,
    build_site_map,
    ensure_site_map,
    load_site_map,
    meridian_site_id,
    write_site_map,
)

ARABIC_INDIC_1701 = "".join(chr(0x0660 + d) for d in (1, 7, 0, 1))


@pytest.mark.parametrize(
    ("pilot", "meridian"), [("701", "1001"), ("718", "1018"), ("1699", "1999")]
)
def test_rule(pilot: str, meridian: str) -> None:
    assert meridian_site_id(pilot) == meridian


@pytest.mark.parametrize(
    "bad", ["7A1", "", " 701", "701 ", "-701", "70", "9800", ARABIC_INDIC_1701]
)
def test_values_that_do_not_fit_are_rejected(bad: str) -> None:
    with pytest.raises(SiteMappingError):
        meridian_site_id(bad)


def test_build_reports_every_bad_value_and_all_values_found() -> None:
    with pytest.raises(SiteMappingError) as info:
        build_site_map(["701", "X12", "702", "9800", "X12"])
    message = str(info.value)
    assert "['9800', 'X12']" in message
    assert "['701', '702', '9800', 'X12']" in message


def test_ensure_generates_once_then_reads_the_file(tmp_path: Path) -> None:
    path = tmp_path / "sites.toml"
    assert ensure_site_map(path, ["702", "701"]) == {"701": "1001", "702": "1002"}
    # Hand-edit the file: from now on the file, not the rule, is the source of truth.
    path.write_text(path.read_text().replace('"702" = "1002"', '"702" = "1099"'))
    assert ensure_site_map(path, ["701", "702"]) == {"701": "1001", "702": "1099"}


def test_ensure_fails_when_file_lacks_a_site(tmp_path: Path) -> None:
    path = tmp_path / "sites.toml"
    ensure_site_map(path, ["701"])
    with pytest.raises(SiteMappingError, match="703"):
        ensure_site_map(path, ["701", "703"])


def test_write_never_overwrites(tmp_path: Path) -> None:
    path = tmp_path / "sites.toml"
    write_site_map(path, {"701": "1001"})
    with pytest.raises(FileExistsError):
        write_site_map(path, {"701": "1002"})
    assert load_site_map(path) == {"701": "1001"}


def test_load_rejects_invalid_ids(tmp_path: Path) -> None:
    path = tmp_path / "sites.toml"
    path.write_text('[sites]\n"701" = "10001"\n')
    with pytest.raises(SiteMappingError, match="invalid"):
        load_site_map(path)


def test_repo_sites_file_matches_the_rule() -> None:
    repo = Path(__file__).resolve().parents[2] / "config" / "sites.toml"
    mapping = load_site_map(repo)
    assert mapping == {p: meridian_site_id(p) for p in mapping}
