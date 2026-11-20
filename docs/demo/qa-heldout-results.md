# Demo Q&A: run 2, the held-out 20 questions

Questions: [qa-heldout.json](qa-heldout.json), committed before the code
they evaluate, run once with the page's own search (web/demo/search.mjs: BM25; it declines
when the best passage covers less than 0.5 of the question's idf weight, a word in
no passage weighing as BM25's idf for df = 0). Nothing was tuned on them. Written by
`node web/demo/eval-qa.mjs`.

Hits. One-source: top-1 = the first passage is from an expected source; top-3 = any of the
top 3 is. Two-source: top-1 = the first passage is from either expected source; top-3 =
both expected sources are in the top 3 (stricter). A declined question is no hit.

| Set | Questions | Top-1 hits | Top-3 hits | False declines | Correct declines |
|---|---:|---:|---:|---:|---:|
| Answerable from one source | 10 | 7 | 9 | 1 | |
| Not covered | 10 | | | | 10 |

False declines (answerable, but declined): b02.

The index holds 148 passages (transcript segments and document sections).

## Every question

| Id | Question | Declined | Top-1 | Top-3 | Best coverage | First retrieved source |
|---|---|---|---|---|---:|---|
| b01 | What does the dispatcher do, and which actions does it refuse? | no | yes | yes | 0.51 | docs/phase4b-summary.md#The dispatcher and the input store |
| b02 | How many policy refusals has the ledger recorded? | yes | no | no | 0.44 | transcript:scene-4 (2:21) |
| b03 | How many false positives did the clean cut have before and after tuning in phase 1? | no | yes | yes | 0.60 | docs/phase1-summary.md#Five lessons |
| b04 | What did the toxicologist decide about the deaths PRE-0221 and PRE-0222? | no | no | yes | 0.89 | README.md#Project status |
| b05 | How are a pass's inputs held in the cluster on GKE? | no | no | yes | 0.83 | docs/project-summary.md#What was built |
| b06 | What limits throughput on GKE? | no | yes | yes | 1.00 | docs/lessons.md#Operations |
| b07 | Does the gate pack give a verdict, and who records the gate decision? | no | yes | yes | 0.88 | transcript:scene-7 (5:23) |
| b08 | Why did the local narrator lose most of its summaries before the host wrote the opening? | no | yes | yes | 1.00 | docs/phase3-summary.md#Ten lessons |
| b09 | What happened when a new signal scout version reran an old scan request? | no | yes | yes | 0.63 | docs/phase5-summary.md#Human decisions recorded in phase 5, and by whom |
| b10 | How long does a sandboxed OpenShell run take compared with an in-process run? | no | yes | yes | 0.53 | docs/phase3-summary.md#Costs and timings |
| v01 | How do I connect the fleet to Veeva Vault? | yes |  | correctly declined | 0.24 | docs/phase4b-summary.md#Gaps |
| v02 | Does the Command Center integrate with Medidata Rave? | yes |  | correctly declined | 0.20 | transcript:scene-2 (0:47) |
| v03 | Can the ledger be stored in Snowflake? | yes |  | correctly declined | 0.41 | docs/phase2-summary.md#What containment proves, and how |
| v04 | How is the fleet deployed on Amazon EKS? | yes |  | correctly declined | 0.34 | README.md#Quickstart: a laptop with kind |
| v05 | Which Salesforce objects does the portfolio page sync? | yes |  | correctly declined | 0.23 | docs/phase4b-summary.md#What moved and what stayed |
| v06 | What is the half-life of xanomeline in humans? | yes |  | correctly declined | 0.13 | transcript:scene-1 (0:39) |
| v07 | What dose adjustment is recommended for patients with kidney impairment? | yes |  | correctly declined | 0.23 | README.md#The agents |
| v08 | How many patients have enrolled in the MRD-301 ulcerative colitis trial? | yes |  | correctly declined | 0.07 | transcript:scene-2 (1:03) |
| v09 | What were the overall survival results? | yes |  | correctly declined | 0.18 | transcript:scene-9 (6:33) |
| v10 | Which countries have approved the drug? | yes |  | correctly declined | 0.33 | transcript:scene-5 (3:18) |
