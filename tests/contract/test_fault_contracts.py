"""Fault injection guarantees and the agents import boundary."""

import ast
import json
from collections.abc import Iterator
from pathlib import Path

import polars as pl
import pytest

from meridian.data.blinding import forbidden_columns_in
from meridian.data.cut_writer import MANIFEST_NAME, CutManifest
from meridian.data.datacut import find_data_cut
from meridian.data.files import sha256_file
from meridian.data.study_store import ParquetStudyReader, StudyAccessError
from meridian.faults.catalog import REGISTRY
from meridian.faults.inject import FaultInjector, GroundTruth, InjectionError, InjectResult
from meridian.faults.reader import FullCutReader
from meridian.faults.scenario import RBQM_CORE_OUT_CUT, load_scenario
from meridian.ledger.store import SqliteLedger
from meridian.ledger.verify import verify
from meridian.platform.clock import FixedClock
from tests.rbqm_support import STUDY, make_tables, write_tables_cut
from tests.support import T0, make_ledger

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src" / "meridian"
FORBIDDEN_FOR_AGENTS = ("meridian.faults", "meridian.evals", "meridian.data.ingest")


# Import boundary


def _module_name(path: Path) -> str:
    rel = path.relative_to(SRC.parent).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _imports_of(path: Path) -> set[str]:
    package = (
        _module_name(path) if path.name == "__init__.py" else _module_name(path).rsplit(".", 1)[0]
    )
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".")[: len(package.split(".")) - (node.level - 1)]
                module = ".".join([*base, node.module] if node.module else base)
            else:
                module = node.module or ""
            found.add(module)
            found.update(f"{module}.{alias.name}" for alias in node.names)
    return {m for m in found if m == "meridian" or m.startswith("meridian.")}


def _graph() -> dict[str, set[str]]:
    modules = {_module_name(p): p for p in SRC.rglob("*.py")}
    graph: dict[str, set[str]] = {}
    for name, path in modules.items():
        graph[name] = {m for m in _imports_of(path) if m in modules}
    return graph


def _closure(graph: dict[str, set[str]], start: str) -> set[str]:
    seen: set[str] = set()
    stack = [start]
    while stack:
        current = stack.pop()
        for dep in graph.get(current, set()):
            if dep not in seen:
                seen.add(dep)
                stack.append(dep)
    return seen


def _is_forbidden(module: str) -> bool:
    return any(module == f or module.startswith(f"{f}.") for f in FORBIDDEN_FOR_AGENTS)


@pytest.mark.parametrize("path", sorted((SRC / "agents").rglob("*.py")), ids=lambda p: p.name)
def test_agents_do_not_import_faults_evals_or_ingest(path: Path) -> None:
    direct = sorted(m for m in _imports_of(path) if _is_forbidden(m))
    assert direct == [], f"{path.name} imports {direct}"


def test_agents_do_not_reach_them_transitively_either() -> None:
    graph = _graph()
    for module in sorted(m for m in graph if m.startswith("meridian.agents")):
        reached = sorted(m for m in _closure(graph, module) if _is_forbidden(m))
        assert reached == [], f"{module} reaches {reached}"


def test_evals_do_not_import_agents_directly_or_transitively() -> None:
    graph = _graph()
    evals = sorted(m for m in graph if m == "meridian.evals" or m.startswith("meridian.evals."))
    assert len(evals) >= 4
    for module in evals:
        reached = sorted(
            m
            for m in _closure(graph, module)
            if m == "meridian.agents" or m.startswith("meridian.agents.")
        )
        assert reached == [], f"{module} reaches {reached}"


def test_import_scan_sees_real_imports() -> None:
    assert "meridian.data.study_store" in _imports_of(SRC / "agents" / "base.py")
    assert "meridian.data.ingest" in _imports_of(SRC / "platform" / "container.py")


# Injection on a synthetic study


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[SqliteLedger]:
    with make_ledger(tmp_path / "ledger.sqlite3", FixedClock(T0)) as opened:
        yield opened


def _injector(root: Path, ledger: SqliteLedger) -> FaultInjector:
    return FaultInjector(
        clock=FixedClock(T0),
        ledger=ledger,
        curated_root=root / "data" / "curated",
        study_id=STUDY,
        ground_truth_dir=root / "data" / "ground_truth",
    )


def _base(root: Path) -> Path:
    blinded, ops, unblinded = make_tables(per_site=20)
    return write_tables_cut(root / "data" / "curated", "DC-BASE", blinded, ops, unblinded)


@pytest.fixture
def injected(tmp_path: Path, ledger: SqliteLedger) -> tuple[Path, InjectResult]:
    _base(tmp_path)
    scenario = tmp_path / "scenarios" / "rbqm_core.toml"  # generated from the base cut
    return scenario, _injector(tmp_path, ledger).inject(scenario)


def _tokens(scenario_path: Path) -> tuple[set[str], set[float]]:
    scenario = load_scenario(scenario_path)
    names = set(REGISTRY) | {p for kind in REGISTRY.values() for p in kind.params}
    names |= {"base_cut", "out_cut", "faults", scenario_path.stem, scenario_path.name}
    names |= {"scenario", "fault_type", "fault_id", "ground_truth", "expected_kind"}
    values = {v for f in scenario.faults for v in f.params.values()}
    return names, values


def _walk_json(obj: object, key: str = "") -> Iterator[tuple[str, object]]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k, k
            yield from _walk_json(v, k)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_json(item, key)
    else:
        yield key, obj


def _assert_no_trace(cut_dir: Path, names: set[str], values: set[float]) -> None:
    lowered = {n.lower() for n in names}
    for path in sorted(p for p in cut_dir.rglob("*") if p.is_file()):
        raw = path.read_bytes().lower()
        for name in lowered:
            assert name.encode() not in raw, f"{name!r} found in {path.relative_to(cut_dir)}"
        if path.suffix == ".parquet":
            frame = pl.read_parquet(path)
            for column in frame.columns:
                assert column.lower() not in lowered
                if frame.schema[column] == pl.String:
                    cells = {str(v).lower() for v in frame[column].unique().drop_nulls()}
                    assert not (cells & lowered), (path.name, column)
            assert not set(map(str.lower, pl.read_parquet_metadata(path).values())) & lowered
    manifest = json.loads((cut_dir / MANIFEST_NAME).read_text())
    for key, value in _walk_json(manifest):
        if key == "rows" or isinstance(value, bool):
            continue
        if isinstance(value, int | float):
            assert float(value) not in values, f"parameter value {value} in manifest key {key}"


def test_faulted_cut_has_no_trace_of_the_faults(injected: tuple[Path, InjectResult]) -> None:
    scenario, result = injected
    names, values = _tokens(scenario)
    _assert_no_trace(result.cut_path, names, values)
    manifest = CutManifest.load(result.cut_path)
    assert manifest.sources == ()
    assert manifest.seed_record == {}
    assert manifest.derived_from_cut == "DC-BASE"
    assert manifest.derived_from_manifest_sha256 == sha256_file(
        result.cut_path.parent / "DC-BASE" / MANIFEST_NAME
    )


def test_data_cut_payload_has_no_trace(injected: tuple[Path, InjectResult]) -> None:
    scenario, result = injected
    names, values = _tokens(scenario)
    payload = result.event.payload.model_dump_json().lower()
    for name in names:
        assert name.lower() not in payload
    for _key, value in _walk_json(json.loads(payload)):
        assert not (isinstance(value, float) and value in values)
    assert result.event.actor == "system.faults"


def test_repo_faulted_cut_has_no_trace_if_present() -> None:
    cut = REPO_ROOT / "data" / "curated" / STUDY / RBQM_CORE_OUT_CUT
    scenario = REPO_ROOT / "config" / "scenarios" / "rbqm_core.toml"
    if not cut.exists():
        pytest.skip("no local DC-RBQM-CORE cut")
    names, values = _tokens(scenario)
    _assert_no_trace(cut, names, values)


def test_faulted_cut_passes_the_forbidden_column_scan(injected: tuple[Path, InjectResult]) -> None:
    _scenario, result = injected
    checked = 0
    for area in ("blinded", "ops"):
        for path in sorted((result.cut_path / area).glob("*.parquet")):
            assert forbidden_columns_in(pl.read_parquet_schema(path)) == [], path
            checked += 1
    assert checked == 11


def test_unblinded_is_copied_unchanged(injected: tuple[Path, InjectResult]) -> None:
    _scenario, result = injected
    base = result.cut_path.parent / "DC-BASE"
    for path in sorted((base / "unblinded").glob("*.parquet")):
        assert sha256_file(path) == sha256_file(result.cut_path / "unblinded" / path.name)


def test_ground_truth_records_every_fault(injected: tuple[Path, InjectResult]) -> None:
    scenario, result = injected
    truth = GroundTruth.load(result.ground_truth_path)
    assert truth.scenario_sha256 == sha256_file(scenario)
    assert [f.fault_id for f in truth.faults] == [f"F-{i:03d}" for i in range(1, 7)]
    assert [f.fault_type for f in truth.faults] == [f.type for f in load_scenario(scenario).faults]
    assert all(sum(f.rows_touched.values()) > 0 for f in truth.faults)


def test_reader_refuses_ground_truth(injected: tuple[Path, InjectResult]) -> None:
    _scenario, result = injected
    assert result.ground_truth_path.exists()
    reader = ParquetStudyReader(result.cut_path.parent, RBQM_CORE_OUT_CUT)
    for name in ("../../../ground_truth/DC-RBQM-CORE", "../../ground_truth", "ground_truth"):
        with pytest.raises((StudyAccessError, KeyError)):
            reader.table(name)
    with pytest.raises(StudyAccessError):
        reader.table(str(result.ground_truth_path))
    assert all("ground" not in t for t in reader.tables())


def test_reinject_is_a_no_op_and_data_cut_is_appended_once(
    tmp_path: Path, ledger: SqliteLedger, injected: tuple[Path, InjectResult]
) -> None:
    scenario, first = injected
    second = _injector(tmp_path, ledger).inject(scenario)
    assert first.cut_created
    assert first.event_appended
    assert not second.cut_created
    assert not second.event_appended
    assert second.event.seq == first.event.seq
    assert find_data_cut(ledger, STUDY, RBQM_CORE_OUT_CUT) is not None
    assert verify(ledger).ok


def test_ground_truth_is_never_overwritten(
    tmp_path: Path, ledger: SqliteLedger, injected: tuple[Path, InjectResult]
) -> None:
    scenario, result = injected
    result.ground_truth_path.write_text(
        result.ground_truth_path.read_text().replace("F-001", "F-999")
    )
    tampered = result.ground_truth_path.read_bytes()
    with pytest.raises(InjectionError, match="ground truth is written once"):
        _injector(tmp_path, ledger).inject(scenario)
    assert result.ground_truth_path.read_bytes() == tampered


def test_same_seed_into_two_folders_is_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    results = []
    for name in ("a", "b"):
        root = tmp_path / name
        _base(root)
        monkeypatch.chdir(root)  # the scenario path is recorded as given, like the CLI does
        with make_ledger(root / "l.sqlite3", FixedClock(T0)) as led:
            results.append((root, _injector(root, led).inject(Path("scenarios/rbqm_core.toml"))))
    (root_a, a), (root_b, b) = results
    assert (root_a / "scenarios" / "rbqm_core.toml").read_bytes() == (
        root_b / "scenarios" / "rbqm_core.toml"
    ).read_bytes()
    assert a.manifest.file_hashes() == b.manifest.file_hashes()
    assert a.ground_truth_path.read_bytes() == b.ground_truth_path.read_bytes()


def test_full_cut_reader_reads_every_area(injected: tuple[Path, InjectResult]) -> None:
    _scenario, result = injected
    cut = FullCutReader(result.cut_path).read()
    assert set(cut.unblinded) == {"ex", "randomization"}
    assert "ARM" in cut.unblinded["randomization"].columns
