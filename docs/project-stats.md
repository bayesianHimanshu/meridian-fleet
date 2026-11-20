# Project stats

Computed from the ledger at seq **20158** by `meridian stats` (src/meridian/project_stats.py). Do not edit by hand: run `uv run python scripts/project_stats.py` again.

Ledger verification: **ok**, 20158 events checked. First event recorded 2026-11-07T07:47:20.354929+00:00, last 2026-11-20T07:08:37.987984+00:00.

## Events by type

| Event type | Count |
|---|---:|
| actor_woke | 8559 |
| actor_suspended | 8543 |
| finding_raised | 1334 |
| policy_denied | 844 |
| finding_narrative | 517 |
| data_cut | 182 |
| containment_check_completed | 108 |
| finding_assessment | 30 |
| noael_decision | 17 |
| human_decision | 9 |
| audit_review_requested | 5 |
| signal_scan_requested | 2 |
| attribution_correction | 1 |
| gate_decision | 1 |
| gate_pack | 1 |
| gate_request | 1 |
| inventory_loaded | 1 |
| portfolio_outlook | 1 |
| portfolio_review_requested | 1 |
| send_study_loaded | 1 |

Total: 20158.

## Findings

1334 findings raised. Status is the latest human decision on each finding, or open; a finding replaced by a newer agent version keeps its own status here.

| Zone | Count |
|---|---:|
| safety | 489 |
| rbqm | 336 |
| preclinical | 237 |
| quality | 178 |
| portfolio | 94 |

| Status | Count |
|---|---:|
| open (no decision) | 1325 |
| accepted | 5 |
| dismissed | 4 |

| Zone | Status counts |
|---|---|
| portfolio | open (no decision) 94 |
| preclinical | open (no decision) 230, accepted 4, dismissed 3 |
| quality | open (no decision) 178 |
| rbqm | open (no decision) 334, accepted 1, dismissed 1 |
| safety | open (no decision) 489 |

| Agent and version | Count |
|---|---:|
| portfolio.safety_signal_scout 1.0.0 | 76 |
| portfolio.safety_signal_scout 1.0.1 | 18 |
| preclinical.dose_translation 1.0.0 | 1 |
| preclinical.ind_readiness 1.0.0 | 4 |
| preclinical.tox_study_monitor 1.0.0 | 133 |
| preclinical.tox_study_monitor 1.1.0 | 98 |
| preclinical.translational_bridge 1.0.0 | 1 |
| quality.audit_trail_reviewer 1.0.0 | 7 |
| quality.audit_trail_reviewer 1.1.0 | 7 |
| quality.audit_trail_reviewer 1.2.0 | 7 |
| quality.audit_trail_reviewer 1.3.0 | 8 |
| quality.tmf_completeness 1.0.0 | 149 |
| rbqm.csm 1.0.0 | 26 |
| rbqm.csm 2.0.0 | 4 |
| rbqm.csm 2.1.0 | 4 |
| rbqm.kri_monitor 1.0.0 | 10 |
| rbqm.kri_monitor 2.0.0 | 34 |
| rbqm.kri_monitor 2.1.0 | 46 |
| rbqm.kri_monitor 2.2.0 | 196 |
| rbqm.qtl_detector 1.0.0 | 2 |
| rbqm.qtl_detector 2.0.0 | 7 |
| rbqm.qtl_detector 2.1.0 | 7 |
| safety.sae_reconciler 1.0.0 | 158 |
| safety.sae_reconciler 1.1.0 | 158 |
| safety.sae_reconciler 1.1.1 | 173 |

## Human decisions

Every event whose actor is a person (config/people.toml), by the person's role. People act only through the Fleet API.

| Role: action | Count | Ledger seqs |
|---|---:|---|
| toxicologist: noael_decision | 17 | 19927 to 19943 |
| toxicologist: dismissed | 3 | 19944 to 19946 |
| medical_monitor: accepted | 2 | 19966, 20067 |
| toxicologist: accepted | 2 | 20141 to 20142 |
| clinical_ops_lead: accepted | 1 | 787 |
| clinical_ops_lead: dismissed | 1 | 788 |
| portfolio_lead: gate_decision:conditional_go | 1 | 20158 |
| portfolio_lead: gate_request | 1 | 20143 |

Total: 28. Attribution corrections: seq 20068 corrects seq 19985 (requested_by).

## Runs

8543 runs completed (ActorSuspended); 8559 started (ActorWoke); 16 started and not completed (failed, retried as a new run).

| Executor | Count |
|---|---:|
| substrate (kind) | 6387 |
| substrate (gke) | 910 |
| inprocess | 632 |
| openshell | 492 |
| inprocess (phase 1, before sandboxes) | 122 |

| Cut scope | Count |
|---|---:|
| evaluation | 8469 |
| operational | 74 |

| Executor | By scope |
|---|---|
| inprocess | evaluation 632 |
| inprocess (phase 1, before sandboxes) | evaluation 115, operational 7 |
| openshell | evaluation 481, operational 11 |
| substrate (gke) | evaluation 905, operational 5 |
| substrate (kind) | evaluation 6336, operational 51 |

## Containment

844 policy refusals recorded (PolicyDenied).

| Refusal source | Count |
|---|---:|
| harness | 520 |
| substrate_log | 228 |
| openshell_log | 89 |
| substrate_authz | 4 |
| runner_guard | 3 |

| Refusals by zone | Count |
|---|---:|
| quality | 215 |
| rbqm | 171 |
| safety | 163 |
| safety_local | 69 |
| preclinical | 59 |
| portfolio_signal | 51 |
| portfolio | 43 |
| study_ops | 35 |
| unblinded | 31 |
| unrecorded | 7 |

| Conformance runs | Count |
|---|---:|
| openshell: passed | 60 |
| substrate: passed | 44 |
| substrate: failed | 4 |

## Leak checks (result files, not ledger events)

13 run, 11 passed.

| File | Cluster | Passed | Runs | Why it failed |
|---|---|---|---:|---|
| var/leakcheck/gke-20261114T074818Z.json | gke | yes | 20 |  |
| var/leakcheck/gke-20261114T080254Z.json | gke | yes | 24 |  |
| var/leakcheck/gke-20261114T143939Z.json | gke | yes | 20 |  |
| var/leakcheck/gke-20261114T145207Z.json | gke | yes | 24 |  |
| var/leakcheck/gke-20261114T154231Z.json | gke | yes | 24 |  |
| var/leakcheck/gke-20261114T160450Z.json | gke | no | 24 | dispatcher pod: positive control not found (the marker in /tmp/meridian-leakcheck-control) |
| var/leakcheck/gke-20261114T163138Z.json | gke | yes | 24 |  |
| var/leakcheck/kind-20261113T201716Z.json | kind | yes | 20 |  |
| var/leakcheck/kind-20261113T201841Z.json | kind | yes | 24 |  |
| var/leakcheck/kind-20261114T105902Z.json | kind | yes | 24 |  |
| var/leakcheck/kind-20261115T081613Z.json | kind | no | 20 | bucket: non_golden_tags ['10bf7a8a-8840-45ce-813f-5b473151c05c', '4419515b-16c3-443b-9d21-9539b9981e9c', '561be7ad-eb06-45c9-a1cb-7eeabc4a325b', '5b9905d5-b6c6- |
| var/leakcheck/kind-20261115T081914Z.json | kind | yes | 20 |  |
| var/leakcheck/kind-20261119T202520Z.json | kind | yes | 20 |  |

Phase 4a's canary proof files (no verdict field; docs/substrate-eval.md, C1): var/substrate/canary-dispatcher.json, var/substrate/canary.json.

## Agents and versions

16 agents.

| Agent | Versions |
|---|---|
| portfolio.pipeline_intelligence | 1.0.0 |
| portfolio.safety_signal_scout | 1.0.0, 1.0.1 |
| portfolio.stage_gate_pack | 1.0.0 |
| preclinical.dose_translation | 1.0.0 |
| preclinical.ind_readiness | 1.0.0 |
| preclinical.tox_study_monitor | 1.0.0, 1.1.0 |
| preclinical.translational_bridge | 1.0.0 |
| quality.audit_trail_reviewer | 1.0.0, 1.1.0, 1.2.0, 1.3.0 |
| quality.tmf_completeness | 1.0.0 |
| rbqm.csm | 1.0.0, 2.0.0, 2.1.0 |
| rbqm.kri_monitor | 1.0.0, 2.0.0, 2.1.0, 2.2.0 |
| rbqm.narrator | 1.0.0, 1.1.0, 1.1.1 |
| rbqm.qtl_detector | 1.0.0, 2.0.0, 2.1.0 |
| safety.local_narrator | 1.0.0, 1.0.1, 1.0.2 |
| safety.sae_reconciler | 1.0.0, 1.1.0, 1.1.1 |
| safety.term_adjudicator | 1.1.0, 1.1.1 |
