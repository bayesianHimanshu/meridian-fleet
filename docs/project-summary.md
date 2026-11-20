# Meridian Agent Fleet: the project on one page

The project was developed in a private repository; this summary and the phase summaries
stand for that history in the public repository, which starts at v1.1.0 as a single commit.
The numbers come from [project-stats.md](project-stats.md), computed from the ledger at seq
20158 (it verifies), and from the phase summaries, which give each number's own seqs.

## The problem

Clinical development produces work that is routine, high-volume and consequential:
- checking sites' data for risk signals;
- reconciling serious adverse events between two databases;
- keeping the Trial Master File complete;
- reviewing the audit trail;
- reading a toxicology study for its NOAEL, and turning that into a first human dose;
- checking an IND package, and preparing a stage gate.

Agents can do much of the computing. They must not decide, must not see what blinding
hides, and must leave a record that someone else can check.

## Design principles

- **People decide; agents compute and cite.** An agent raises a finding with its evidence
  and the rule it applied. A named person accepts, dismisses or escalates it through the
  Fleet API, and that is a separate ledger event. The NOAEL, the starting dose and the gate
  decision are people's decisions. The pack and the outlook state no verdict.
- **Containment by infrastructure, not by instruction.**
  - Each agent runs in a sandbox: an OpenShell sandbox, or a gVisor actor in Agent
    Substrate.
  - The sandbox holds only its zone's staged inputs and has no network. The three zones
    that need one endpoint get exactly that endpoint.
  - Policies are rendered from code, proved against a boundary, and tested in real
    sandboxes.
  - Leak checks plant a canary and search every store, with a positive control in each one.
- **An append-only, hash-chained ledger.**
  - One SQLite table. Triggers refuse UPDATE and DELETE.
  - Each event's hash covers the previous one, and a verifier recomputes the chain.
  - It has one writer, and runs are appended in a fixed order, so the same pass gives the
    same ledger.
- **Evaluation that can fail.**
  - Targets are pre-registered in a commit before the data is seen.
  - Thresholds are frozen before holdouts.
  - Positive controls are planted.
  - Independent sealed sets where possible.
  - The misses are reported next to the passes.

## What was built

| Phase | Built |
|---|---|
| 1 | Three RBQM agents on the CDISC pilot study (as MRD-201); the ledger; a fault injector with ground truth kept apart; null cuts, holdouts and a magnitude sweep |
| 2 | One OpenShell sandbox per run; zone policies, conformance, the policy prover; the first LLM agent (the narrator), its key held by a provider, never by the sandbox |
| 3 | The Fleet API and the Command Center; SAE reconciliation; a local model with two-model agreement; TMF completeness; the audit trail reviewer and QA observations |
| 4a | The no-network zones in Agent Substrate actors on kind, one fresh actor per run, in parallel |
| 4b | The same zones on GKE behind one in-cluster dispatcher, the only Substrate identity, with inputs held in its memory |
| 5 | A GLP rat study in SEND; the NOAEL as the toxicologist's decision; FDA 2005 dose translation; IND readiness; the translational bridge; FAERS signals; the portfolio outlook and the stage-gate pack |

## Key numbers

- **The ledger.**
  - 20,158 events and 1,334 findings from 16 agents.
  - 8,543 completed runs:
    - 6,387 in Substrate on kind and 910 on GKE;
    - 492 in OpenShell;
    - 632 in process (evaluation cuts only);
    - 122 in phase 1, before sandboxes existed.
  - 28 decisions and requests by people, by role:
    - toxicologist 22;
    - medical monitor 2;
    - clinical ops lead 2;
    - portfolio lead 2.
  - 844 policy refusals recorded.
  - 108 conformance runs: 104 passed and 4 failed. In the 4 failures (13 November, phase 4b
    step 2), every egress was refused but the gateway's log line was not found.
- **A full pass of 910 runs in 270 s on GKE** (3.37 runs a second), against 86 s on kind.
  The single port-forward is the ceiling. The 910 drafts and inputs manifests equal the
  kind pass, and a rerun appended nothing. Phase 4b cost about USD 3.50.
- **Leak checks with positive controls.** 13 leak checks are in var/leakcheck, and 11
  passed. Both failures were real:
  - one positive control was not found (the autoscaler had cut the scan's stream);
  - one bucket held snapshots other than the golden ones.

  Both passed when run again. The canary was found in no store: the dispatcher's memory,
  its pod and logs, the snapshot bucket (every golden snapshot decoded), every node's
  filesystem, the laptop's var/ and Cloud Logging.
- **Two-model agreement catches wrong "yes" answers.** One local model gave 26 of 33 right
  on a sealed set (target 0.80). Asking two models and answering only when both agree
  gave 0 wrong "yes" answers on 75 dev pairs, and on the 15 assessments checked end to
  end. The price is correct answers given up: versions 1.1.0 and 1.1.1 got 55 and 57 of
  the 75 right.
- **From the NOAEL to the starting dose to the gate.**
  1. The toxicologist decided a NOAEL of 2 mg/kg/day for males, females and combined
     (seq 19941 to 19943). That took 17 decisions, 14 of them superseded while the form's
     rules were found wanting.
  2. dose_translation turned it into a starting dose of 1.94 mg for a 60 kg adult by FDA's
     2005 method (HED / 10), and the medical monitor accepted it (19966).
  3. The translational bridge mapped the target organs to Phase 1 monitoring, which was
     accepted (20067).
  4. The portfolio lead recorded a conditional go on the IND gate (20158), with five
     conditions.
- **A queue is a product.** The signal scout's 76 FAERS signals became 9 to review once they
  were triaged (78 signals, 9 priority, exactly the 9 pre-registered).

## The misses, as plainly

- **Phase 1:** detector recall was 0.67 against a target of 0.8. AE under-reporting is not
  detectable on this data with any of the five pre-registered variants. The exit criteria
  were revised after step 4, and both versions are shown.
- **Narrator validator 1.0.0** caught none of the 5 known-bad narratives. **1.1.0** caught
  all 5, but rejected 28 of 47 good ones.
- **SAE reconciler:** the sealed stress set met its ideal on 8 of 12 cases (target 10).
  **Term adjudicator 1.0.0** got 0.788 (target 0.80). **Local narrator 1.0.0** accepted 16
  of 77 summaries, until the host wrote the opening.
- **tox_study_monitor 1.0.0 and 1.1.0** both missed their null targets:
  - 1.0.0: 9.2 per null cut, against 6;
  - 1.1.0: 5.95 per null cut, against 5;
  - 1.1.0 also missed three holdout power targets.

  1.1.0 raised 98 findings on the real study, against the 50 to 90 expected.
- **Attribution.** A CLI flag recorded the medical monitor as requesting a scan they never
  touched (seq 19985). An AttributionCorrection (20068) records the truth, and the CLI now
  records only operator ids.
- **The scout's rerun.** A new scout version reran that old scan request with live calls
  (20144). Requests are now answered once.
- **Author bias.** Every stress set was written by the same author as its agent. Only the
  user's sealed sets were independent, and both missed.

More: [architecture.md](architecture.md), [lessons.md](lessons.md),
[backlog.md](backlog.md), and the phase summaries ([1](phase1-summary.md),
[2](phase2-summary.md), [3](phase3-summary.md), [4a](phase4a-summary.md),
[4b](phase4b-summary.md), [5](phase5-summary.md)).
