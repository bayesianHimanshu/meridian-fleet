# Data notes: CDISC pilot to MRD-201

These notes explain the data behind MRD-201, the fictional Meridian study used in this
project. All numbers below were observed in the real files fetched by `meridian data fetch`.

## Where the data comes from

The CDISC SDTM/ADaM pilot project publishes a complete, de-identified submission for a
study called CDISCPILOT01: a 26-week trial of xanomeline (a skin patch) in mild to moderate
Alzheimer's disease. We use its SDTM tabulation files, one SAS transport (XPT) file per
domain. SDTM is the standard layout regulators expect: one table per topic, one row per
observation, and every row tied to a subject by USUBJID.

## The study at a glance

* 306 subjects in DM, at 17 sites (pilot SITEID 701 to 718; there is no 712).
* 254 subjects were randomized and treated. 52 were screen failures.
* All subjects are in the USA. 179 female, 127 male. Age 50 to 89, median 77.
* First treatment dates run from 2012-07-09 to 2014-09-02. 3 subjects died.
* Planned arms (ARM): Placebo 86, Xanomeline Low Dose 84, Xanomeline High Dose 84,
  Screen Failure 52.
* Actual arms (ACTARM) differ: 12 subjects planned for High Dose actually received Low
  Dose, so actual counts are Placebo 86, Low Dose 96, High Dose 72.

## The seven domains we use

| Domain | Rows | Subjects | What one row is |
| --- | ---: | ---: | --- |
| DM, demographics | 306 | 306 | one subject: site, age, sex, race, key dates, arm |
| AE, adverse events | 1,191 | 225 | one adverse event: term, coded term, severity, seriousness, dates |
| VS, vital signs | 29,643 | 254 | one measurement: blood pressure, pulse, temperature, height or weight |
| LB, laboratory | 59,580 | 254 | one lab result (43 tests: chemistry, hematology, urinalysis) |
| DS, disposition | 596 | 306 | one milestone: completed, withdrew, screen failure, final visit |
| SV, subject visits | 3,559 | 306 | one visit a subject attended (36 distinct visit names) |
| EX, exposure | 591 | 254 | one dosing period: treatment, dose, start and end dates |

A few things worth knowing:

* AE has only 3 serious events. The most common terms are pruritus (84), application site
  pruritus (78) and erythema (59), which fits a skin patch.
* DS mixes two kinds of rows: disposition events (completed 110, adverse event 92, screen
  failure 52, withdrawal 27, and smaller reasons) and other milestones such as the final
  lab visit.
* EX has three combinations: placebo 0 mg (226 rows), xanomeline 54 mg (293) and
  xanomeline 81 mg (72).
* Missing text values come through as empty strings, not nulls.
* Dates are ISO 8601 strings, sometimes with a time (2014-07-02T11:45) and sometimes
  partial: 26 AE start dates are only a year and month (2013-07) or only a year (2013).

## What we change when curating

* STUDYID becomes MRD-201 in every table.
* Sites become Meridian ids by a fixed rule (701 becomes 1001). The mapping is written
  once to config/sites.toml and read from there ever after. DM keeps the pilot value in
  SRC_SITEID.
* Subjects get new ids, 201-SITE-NNN, numbered within each site in the order of the
  original ids. The original id stays in SRC_USUBJID in every table, so any row can be
  traced back to the pilot.
* Every --DTC column keeps its original text. A parsed date column (name_DATE) is added
  next to it, filled only when the text has a complete year, month and day. Partial dates
  give null there, and no row is ever dropped for a bad date.

## Blinding: what moved to unblinded and why

In a blinded trial, people running the study must not learn which treatment a subject
receives, because that knowledge can bias how they assess and report outcomes. Our agents
play those people, so they only ever see blinded data.

| Moved to unblinded/ | Why |
| --- | --- |
| DM: ARMCD, ARM, ACTARMCD, ACTARM (as randomization) | these name the treatment arm directly |
| EX in full (as ex) | EXTRT names the drug, and EXDOSE (0, 54 or 81 mg) gives the arm away |

The blinded side keeps an ex_dates table: when each dosing period started and ended,
without treatment or dose, because "was the subject dosed on time" is a legitimate
operational question.

A fixed list of forbidden names (ARM, ARMCD, ACTARM, ACTARMCD, EXTRT, EXDOSE, EXDOSU,
EXDOSFRM, TRT01P, TRT01A, TRTP, TRTA) lives in src/meridian/data/blinding.py. Any column
whose name contains one of them is refused by the cut writer for blinded/ and ops/, before
anything is written, and the tests check every written file again.

One caveat worth remembering: column rules cannot remove indirect signals. Application
site reactions in AE are far more common on active patches than on placebo, so a careful
reader of blinded AE data could still guess arms. Real trials manage that risk with
process, and later steps here may need to as well.

## Screen failures without peeking at ARM

The only explicit screen failure flag in DM is ARM = "Screen Failure", which is forbidden.
The blinded data says the same thing another way: the 52 screen failures are exactly the
subjects with a blank RFSTDTC (no first treatment date), and DS records each of them as
SCREEN FAILURE. Operational synthesis uses the blank RFSTDTC rule.

## Operational data is synthetic

The pilot has no data entry, query, deviation or screening logs, so we generate them per
site from a fixed seed (see config/default.toml, section ops_synth). Every random draw is
kept in a column, and the cut manifest records the seed and the spawn key per table.
These tables are realistic in shape only. They say nothing true about the pilot sites.
