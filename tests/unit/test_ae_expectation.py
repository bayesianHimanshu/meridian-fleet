"""The five expected-AE variants on a hand-built three-site cut.

site  subject  days  AE onset days        abnormal labs
1001  a1       10    1, 8                 HIGH, LOW (and one NORMAL)
1001  a2       20    3
1001  s1       -     (screen failure; its AE must be ignored)
1002  b1       40    2, 4, 6, 30          ABNORMAL
1003  c1       10    5                    HIGH, HIGH
1003  c2       10    -                    HIGH, HIGH
"""

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from meridian.stats.ae_expectation import AE_VARIANTS, ae_expectation, design
from meridian.stats.poisson_glm import fit_poisson
from meridian.stats.robust import ExpectedCount

START = date(2020, 1, 1)
SUBJECTS = {  # usubjid: (site, days, onset days, lab flags, age, sex)
    "a1": ("1001", 10, [1, 8], ["HIGH", "LOW", "NORMAL"], 75.0, "F"),
    "a2": ("1001", 20, [3], [], 75.0, "F"),
    "b1": ("1002", 40, [2, 4, 6, 30], ["ABNORMAL"], 75.0, "F"),
    "c1": ("1003", 10, [5], ["HIGH", "HIGH"], 75.0, "F"),
    "c2": ("1003", 10, [], ["HIGH", "HIGH"], 75.0, "F"),
}
SITES = ["1001", "1002", "1003"]
W = 5


def tables(
    extra_ae: dict[str, list[int]] | None = None,
    people: dict[str, tuple[str, int, list[int], list[str], float, str]] | None = None,
) -> dict[str, pl.DataFrame]:
    people = people or SUBJECTS
    dm = [
        {
            "USUBJID": u,
            "SITEID": s,
            "RFSTDTC": START.isoformat(),
            "RFSTDTC_DATE": START,
            "AGE": age,
            "SEX": sex,
        }
        for u, (s, _, _, _, age, sex) in people.items()
    ]
    dm.append(
        {
            "USUBJID": "s1",
            "SITEID": "1001",
            "RFSTDTC": "",
            "RFSTDTC_DATE": None,
            "AGE": 70.0,
            "SEX": "M",
        }
    )
    sv = [
        {"USUBJID": u, "SVSTDTC_DATE": START + timedelta(days=d - 1)}
        for u, (_, d, _, _, _, _) in people.items()
    ]
    ae = [
        {"USUBJID": u, "AESTDTC_DATE": START + timedelta(days=day - 1)}
        for u, (_, _, onsets, _, _, _) in people.items()
        for day in onsets
    ]
    for u, onsets in (extra_ae or {}).items():
        ae += [{"USUBJID": u, "AESTDTC_DATE": START + timedelta(days=day - 1)} for day in onsets]
    ae.append({"USUBJID": "s1", "AESTDTC_DATE": START})
    lb = [
        {"USUBJID": u, "LBNRIND": f} for u, (_, _, _, flags, _, _) in people.items() for f in flags
    ]
    lb.append({"USUBJID": "s1", "LBNRIND": "HIGH"})
    return {
        "dm": pl.DataFrame(dm, schema_overrides={"RFSTDTC_DATE": pl.Date}),
        "sv": pl.DataFrame(sv),
        "ae": pl.DataFrame(ae),
        "lb": pl.DataFrame(lb),
    }


def run(variant: str, t: dict[str, pl.DataFrame] | None = None) -> dict[str, ExpectedCount]:
    t = t or tables()
    rows = ae_expectation(
        variant,  # type: ignore[arg-type]
        dm=t["dm"],
        sv=t["sv"],
        ae=t["ae"],
        lb=t["lb"],
        tested=SITES,
        early_window_days=W,
    )
    return {r.site: r for r in rows}


@pytest.mark.parametrize(
    ("variant", "observed", "expected"),
    [
        ("a", 3, 5 / 60 * 30),  # others: 5 AEs over 60 subject-days; 1001 has 30
        ("b", 3, 5 / 3 * 2),  # others: 5 AEs over 3 treated subjects; 1001 has 2
        ("c", 2, 3 / 15 * 10),  # onset <= 5: others 2 + 1 over capped days 5 + 10; 1001 has 10
        ("d", 3, 5 / 60 * 30),  # one age group and sex: the regression is the pooled rate
        ("e", 3, 5 / 5 * 2),  # others: 5 AEs over 5 abnormal labs; 1001 has 2
    ],
)
def test_expected_count_for_1001_matches_hand_calculation(
    variant: str, observed: int, expected: float
) -> None:
    row = run(variant)["1001"]
    assert row.observed == observed
    assert row.expected == pytest.approx(expected)


def test_variant_c_for_1003() -> None:
    row = run("c")["1003"]
    assert (row.observed, row.units) == (1, 10.0)
    assert row.expected == pytest.approx((2 + 2) / (10 + 5) * 10)


@pytest.mark.parametrize("variant", sorted(AE_VARIANTS))
def test_leave_one_site_out_excludes_the_site(variant: str) -> None:
    """More AEs at 1001 change O_1001 but never E_1001; they do change the others' E."""
    before = run(variant)
    after = run(variant, tables(extra_ae={"a1": [2, 2, 2, 2]}))
    assert after["1001"].expected == pytest.approx(before["1001"].expected)
    assert after["1001"].observed == before["1001"].observed + 4
    assert after["1002"].expected > before["1002"].expected


Person = tuple[str, int, list[int], list[str], float, str]


def test_covariate_adjusted_uses_the_regression_fitted_on_other_sites() -> None:
    people: dict[str, Person] = {
        "a1": ("1001", 10, [1, 8], [], 65.0, "M"),
        "a2": ("1001", 20, [3], [], 82.0, "F"),
        "b1": ("1002", 40, [2, 4, 6, 30], [], 65.0, "F"),
        "b2": ("1002", 30, [1], [], 82.0, "M"),
        "c1": ("1003", 10, [5], [], 72.0, "F"),
        "c2": ("1003", 25, [3, 9, 12], [], 82.0, "M"),
        "c3": ("1003", 15, [7], [], 65.0, "M"),
    }
    t = tables(people=people)
    rows = run("d", t)
    # By hand: fit on 1002 and 1003 only, predict 1001's two subjects.
    others = [u for u, p in people.items() if p[0] != "1001"]
    frame = pl.DataFrame(
        {"AGE": [people[u][4] for u in others], "SEX": [people[u][5] for u in others]}
    )
    x = design(frame)
    y = np.array([len(people[u][2]) for u in others], dtype=float)
    offset = np.log(np.array([people[u][1] for u in others], dtype=float))
    fit = fit_poisson(x, y, offset)
    own = design(pl.DataFrame({"AGE": [65.0, 82.0], "SEX": ["M", "F"]}))
    expected = float(fit.predict(own, np.log(np.array([10.0, 20.0]))).sum())
    assert rows["1001"].expected == pytest.approx(expected)
    assert rows["1001"].units == rows["1001"].expected
    assert rows["1001"].observed == 3


def test_design_groups_age_and_sex() -> None:
    x = design(pl.DataFrame({"AGE": [69.0, 70.0, 79.0, 80.0], "SEX": ["F", "M", "F", "M"]}))
    assert x.tolist() == [[1, 0, 0, 0], [1, 1, 0, 1], [1, 1, 0, 0], [1, 0, 1, 1]]
