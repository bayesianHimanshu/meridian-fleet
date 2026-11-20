# Lessons, across all phases

These are the lessons that mattered most, grouped, each with the incident that taught it.
The phase summaries hold the full lists ([1](phase1-summary.md), [2](phase2-summary.md),
[3](phase3-summary.md), [4a](phase4a-summary.md), [4b](phase4b-summary.md),
[5](phase5-summary.md)).

## Evaluation

1. **Measure the measurement first.**
   - *Incident:* phase 1 step 4 failed its null-cut criterion. The eval had scored a correct
     rule finding as a false positive: a synthetic site really was over its entry-lag
     threshold, at 7.4 days against 7.
   - *Fix and practice:* rules are scored against condition truth and detectors against
     planted faults. The criteria were revised in the open, with the original ones shown
     beside them.
2. **Freeze, pre-register, and accept a null result.**
   - *Incident:* the AE experiment fixed five expected-AE variants and a selection rule in a
     commit before it ran. None beat the baseline, so the baseline stayed, and AE
     under-reporting is stated as undetectable on this data.
   - *Incident:* in phase 5, the signal triage lists were chosen after seeing 1.0.0's
     results. That is stated as such, so the 9 of 9 match is a test of the rule, not a
     blind result.
3. **Evaluation design can be the bug.**
   - *Incident:* tox_study_monitor 1.0.0 missed its planted female liver weight effect in
     90% of copies. The cause was the evaluation, not the agent: effects were planted on a
     permuted study that still carried its own strong effects. 1.1.0 moved to a
     control-only base.
4. **A generated evaluation is a regression check, and only an independent set can
   disagree with you.**
   - *Incident:* SAE, TMF and IND readiness each scored 1.00 on generated sets made by the
     same rules as the agents. The two sealed sets written by the user were the only ones
     to miss: the SAE stress set (8 of 12) and the term pairs (0.788).
5. **Measure the guard, not just the guarded.**
   - *Incident:* narrator 1.0.0 had 53 of 53 narratives accepted. A labelled set showed its
     validator caught none of the 5 bad ones. Validator 1.1.0 caught all 5, and also
     rejected 28 of 47 good ones.
6. **Small models do not abstain when asked; two models disagreeing do.**
   - *Incident:* no prompt made phi4-mini or qwen3.5 answer "unsure". Asking both, and
     answering only when they agree, gave 0 wrong "yes" answers on 75 dev pairs, at the
     cost of correct answers.
7. **A positive control is what makes a negative search mean something.**
   - *Incident:* phase 4a's first canary search found nothing because it could not read
     the snapshot format.
   - *Incident:* in phase 4b, a GKE leak check "found nothing" only because the autoscaler
     had cut its stream. Its positive control failed it.

## Containment

1. **Prove it in the running system, not in the policy file.**
   - *Incident:* OpenShell 0.1.7's effective policy differs from the file. A network rule
     adds `/usr`, `/lib`, `/etc` and `/proc` to the readable paths, and uploads run under
     Landlock. Each was found only by running a sandbox and reading what it reported.
   - *Practice:* conformance per zone, on every new image.
2. **A check counts only for the right reason.**
   - *Incident:* a refusal passes conformance only when the error names the target, and
     the platform's log line is required where the platform writes one.
   - *Incident:* the four failed Substrate conformance runs (13 November) refused every
     egress, and still failed because the gateway's line was not found.
3. **Read the authorization model, not the role names.**
   - *Incident:* "AccessPolicy" suggested fine-grained grants. Substrate's model.fga showed
     that editor includes suspend, revert and tag.
   - *Response:* refuse them at the tool boundary, first in the laptop's runner (phase 4a),
     then in the in-cluster dispatcher (phase 4b). Say plainly that this is a weaker
     control than the platform's.
4. **Keep the secret out of the agent entirely.**
   - *Incident:* the narrator needed an API key. The provider holds it, the sandbox sees a
     placeholder, the client refuses a key that looks real, and the harness records only
     whether the in-sandbox value looks real.
5. **Measure before designing.**
   - *Incident:* the phase 4b plan proposed a bucket for each pass's inputs. Measuring
     showed that inputs repeat by file (731 files, 12.3 MB), not by set (476 sets, 397 MB).
     A memory store keyed by content was enough, and nothing touches disk.
6. **Kind hides what the real cluster will show.**
   - *Incident:* GKE showed three things kind did not:
     - a taint atelet does not tolerate;
     - a new atespace with no access policy, which exits 0 with no output;
     - PodDisruptionBudgets that stall a drain to zero nodes.
   - *Incident, at the wrap-up:* the fresh clone found two things the long-lived kind
     deployment hid:
     - a deploy race that only a first deploy hits;
     - a node label that only a new install lacks.

## People and attribution

1. **Identity is data, and it is typed.**
   - *Incident:* phase 5 step 3a ran a signal scan from the CLI with `--by karan.mehta`.
     The ledger, append-only, now records the medical monitor as asking for a scan they
     never touched (seq 19985).
   - *Fix:* operator ids for the CLI, a person's id refused there, and an
     AttributionCorrection (20068). The audit reviewer now flags any person named outside
     the API.
2. **Keep visibility in one module, and test it as a property.**
   - *Incident:* twice a hypothesis property test found a leak that the endpoint tests
     missed:
     - ledger events about hidden findings;
     - the QualityObservation event.
   - Both fixes were one function in one place.
3. **Segregate duties in the decision path itself.**
   - *Incident:* the audit trail reviewer raises findings about people's actions. The
     rule that nobody decides a finding about their own actions is checked on every
     decision and tested on every finding kind.
4. **Let the host write what must be right.**
   - *Incident:* the local narrator lost 60 of 77 summaries to a missing site id. Then the
     host wrote the opening sentence itself, and 135 of 158 were accepted.
5. **Who sees what decides what an agent may know.**
   - *Incident:* the portfolio facts were built as the portfolio lead, so the outlook shows
     MRD-201's QTL as "not shown" instead of leaking it.
6. **Agents report, and people decide, even when the answer looks obvious.**
   - *Incident:* the gate pack for MRD-101 listed four criteria met and one not met, and
     stopped there. The portfolio lead's conditional go (seq 20158) added a condition no
     agent had raised: reconfirm the starting dose against the dog NOAEL.

## Operations

1. **Determinism is a feature you test for.**
   - *Incident:* an unsorted polars `group_by` reached `np.polyfit` and made a phase 1 cut
     non-reproducible.
   - *Practice:* a contract test checks every group_by; opaque cut ids are a hash of the
     scenario; parallel passes append in a fixed order.
2. **A request is answered once.**
   - *Incident:* a new signal scout version counted the old scan request 19985 as pending,
     and made live openFDA calls again (seq 20144).
   - *Fix:* scan, review and gate requests are now answered by whichever version runs
     first.
3. **A queue is a product.**
   - *Incident:* signal scout 1.0.0 put 76 FAERS signals in the medical monitor's queue.
     Tiers and context left 9 to review, and the other 69 stay on view, each with its
     reason.
4. **Measure what actually limits throughput.**
   - *Incident:*
     - at 1 GiB per worker, the limit was the pods' reservations;
     - at 512 MiB, it was host memory;
     - on GKE, it was the single port-forward, at 3.4 runs a second against kind's 10.6.
5. **Make immutable names carry everything that can change.**
   - *Incident:* Substrate templates are immutable, so a new image or actor size needs a
     new template. Since phase 4a the name carries both (`rbqm-<digest12>-m384`), and a
     deploy never meets an old template under the same name.
6. **Park by stopping workloads first; without free credit, tear down.**
   - *Incident:* phase 4b's two GKE sessions cost about USD 3.50 in all, and a parked
     cluster costs USD 2.43 a day. The cluster was torn down at the end of the phase, and
     nothing has billed since.
7. **Hooks check the working tree.**
   - *Incident:* two commits went in with a failing check before the hook existed. Since
     then, a failing commit is fixed, never forced.
