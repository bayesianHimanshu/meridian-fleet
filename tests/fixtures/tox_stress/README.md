# Stress set for preclinical.tox_study_monitor (phase 5 step 1)

Hand-written before the agent code (docs/preclinical-eval.md, "Stress set"). Each case is
a study spec (meridian.preclinical.synth.StudySpec: dose groups, animals, effects and
record edits, built with a fixed seed) and its **ideal**: the findings a reader of the
pre-registered rules would expect, as `check:endpoint:sex:phase` for effects and
`data_quality:problem:...` for unusable records.

Endpoint ids: `lb:<LBTESTCD>`, `bw:BW`, `bw:GAIN`, `om:<OMSPEC>:abs`, `om:<OMSPEC>:rel`,
`mi:<MISPEC>:<MISTRESC>`, `ma:<MASPEC>:<MASTRESC>`, `cl:<CLCAT>:<CLSTRESC>`, `death`.
Checks: `dose_related_change`, `pairwise_change`, `dose_related_incidence`,
`pairwise_incidence`, `data_quality`.

`actual` is filled once, at the end, by `uv run python scripts/tox_stress.py`, per agent
version. The set tests the specification: it is written by the author of
the agent, so agreement shows the code follows the rules, not that the rules are right.

1.1.0 (phase 5 step 1b): the ideals of the 13 cases under the 1.1.0 rules, and three new
cases for its new rules (14 to 16), committed with the 1.1.0 pre-registration before its
code. New checks: `unscheduled_death:<USUBJID>`, `neoplasm:mi:<MISPEC>:<MISTRESC>:<sex>`,
`organ_weight_secondary_to_bw:<sex>:<phase>:r<rank>`,
`recovery_only_change|recovery_only_incidence:<endpoint>:<sex>:recovery`; recovery groups
are evidence on the main finding.
