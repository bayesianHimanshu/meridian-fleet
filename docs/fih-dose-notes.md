# From a NOAEL to a first human dose: notes for MRD-101 (phase 5 step 2)

The dose is MRD-101's own first-in-human starting dose (corrected in step 3a: MRD-110 is a
separate asset). The medical monitor accepted PRE-0232 as provisional (seq 19966).

What preclinical.dose_translation and preclinical.ind_readiness do, the numbers for
MRD-101-TOX-01, and what they leave out. Method: FDA, "Estimating the Maximum Safe
Starting Dose in Initial Clinical Trials for Therapeutics in Adult Healthy Volunteers"
(July 2005), read in full for this step; section numbers are the guidance's.

## NOAEL to HED to MRSD, with this study's numbers

| Step | Rule (FDA 2005) | MRD-101-TOX-01 |
|---|---|---|
| 1. NOAEL | the toxicologist's decision on the study (section IV) | 2 mg/kg/day, combined sexes (NOAEL-cd4154d72a20, seq 19943) |
| 2. HED | NOAEL / 6.2 for the rat (section V.A, Table 1) | 2 / 6.2 = 0.323 mg/kg |
| 3. Species | the most appropriate species, by default the one with the lowest HED (section VI) | the rat, the only species with a NOAEL |
| 4. MRSD | HED / safety factor, default 10 (section VII) | 0.323 / 10 = 0.0323 mg/kg |
| 5. Starting dose | MRSD x 60 kg (Table 1, footnote a: "Assumes 60 kg human") | 0.0323 x 60 = 1.94 mg |

Table 1's other column (multiply by 0.16) gives 0.32 mg/kg and 1.92 mg; going through km
(2 x 6 / 37) gives 0.324 mg/kg. The agent uses the divisor and shows the other two as a
cross-check. The guidance's own worked example (Appendix D, Example 3): 50 mg/kg in rats
/ 6.2 = 8 mg/kg; a test checks the code against it and against the hand calculation above.

Two things the numbers do not show:
- The dose levels are 2, 20 and 200 mg/kg/day, a factor of 10 apart. The NOAEL is the
  highest dose without an adverse effect *that was tested*; the true threshold is
  somewhere between 2 and 20, so the NOAEL is conservative by up to that factor.
- The NOAEL holds only because two borderline calls went one way (kidney tubular necrosis
  and adrenal vacuolization at 2 mg/kg/day, docs/noael-assessment-MRD-101-TOX-01.md, B1 and
  B2). Either the other way: NOAEL not established, and no starting dose at all.

## Why body surface area

Toxic doses of systemically acting drugs (the MTD, the LD10) line up across species when
expressed per square metre of body surface, not per kilogram (section V.A, after Freireich
et al. 1966). Smaller animals have a faster metabolism per kilogram, so a rat tolerates a
higher mg/kg dose than a human. Body surface area scales with weight to the 0.67 power, so
HED = animal dose x (animal weight / human weight)^0.33 (Table 1, footnote a; Appendix C).
Table 1's divisors are that formula at standard weights (rat 0.15 kg, human 60 kg).
Scaling by mg/kg instead would give a six times higher HED for the rat (section V.B), and
the guidance accepts it only under conditions listed there (NOAELs similar in mg/kg across
species, and more); with one species they cannot be shown.

## Why a safety factor of 10, and when to change it

Ten is "a historically accepted value" (section VII). It covers: humans more sensitive to
the drug's pharmacology, toxicities animals cannot show (headache, mental disturbance),
different receptor densities, unexpected toxicities, and species differences in ADME.

The guidance lists reasons to raise it (VII.A). Several match what this study showed:
- **severe toxicities**: deaths from hepatocellular carcinoma at 200 mg/kg/day;
- **irreversible toxicity**: liver necrosis at 20 mg/kg/day still present after recovery;
- **inadequate dose-response data**: three dose levels, ten-fold apart;
- **novel therapeutic targets**: if ALK5 inhibition has no clinical precedent for the
  reviewer, the guidance treats that as more uncertainty;
- **nonmonitorable toxicity**: the heart inflammation at 20 mg/kg/day has no clinical
  pathology marker in this study.
It can be lowered (VII.B) for well-characterized classes with monitorable, reversible
toxicity, or when the animal study is much longer than the trial (13 weeks against at most
14 days here, but the toxicity here is not shown to be cumulative only). The agent uses
10 and says so; choosing another factor is the reviewer's judgement, and the guidance asks
that a factor other than 10 be explained, "particularly if it is less than 10".

## MABEL, and why it does not apply here

MABEL, the minimum anticipated biological effect level, starts from pharmacology instead
of toxicity: the lowest dose expected to produce any biological effect in humans, from
receptor binding, in vitro potency and PK/PD modelling. The European guideline on
first-in-human trials (EMEA/CHMP/SWP/28367/07, revised 2017; not read in full for this
step) asks for it for high-risk products, after the 2006 TGN1412 trial, where a
NOAEL-based dose of an agonist antibody caused a cytokine storm. FDA 2005 has the same
idea in section VIII: compare the MRSD with the pharmacologically active dose (PAD), and
lower the starting dose if the PAD is lower.

It does not apply here in the sense that it cannot be computed: this project has no
pharmacology data for MRD-101 at all (no potency, no receptor occupancy, no PK/PD). So the
proposal says plainly that there is no MABEL or PAD comparison. For a small-molecule
inhibitor the NOAEL route is the usual one, but whether 1.94 mg is pharmacologically
active, or already well above the active dose, is unknown.

## What ICH M3(R2) asks for before first-in-human

config/ind_requirements.toml quotes each, with its section; preclinical.ind_readiness
checks the inventory against them (docs/ind-readiness-eval.md):
- repeated-dose toxicity in a rodent and a nonrodent, at least as long as the trial, two
  weeks at minimum (M3(R2) V, V.A and Table 1);
- the safety pharmacology core battery: central nervous, cardiovascular and respiratory
  systems (S7A 2.7, before first administration, 2.10.1; M3(R2) II);
- an in vitro IKr (hERG) assay (S7B 2.3.1; S7B 2.4 says it "should be considered");
- genotoxicity: a bacterial gene mutation test, and for multiple doses a test for
  chromosomal damage in a mammalian system (M3(R2) IX; S2(R1) 2.2);
- systemic exposure (toxicokinetics) in the repeated-dose species, and in vitro metabolism
  and plasma protein binding for animals and humans (M3(R2) III).

MRD-101's inventory meets 7 of these 11 (63.6%): no in vitro metabolism study, the dog
study and its toxicokinetics not QA audited before the planned IND date, and one safety
pharmacology study without a GLP status.

## What this inventory leaves out

- **Reproductive toxicity** (M3(R2) XI): men can enter early trials on the repeat-dose
  histopathology; women of childbearing potential need more. MRD-101's first-in-human
  population is not modelled, so these are not checked.
- **Acute toxicity, local tolerance, immunotoxicity, phototoxicity, abuse liability,
  juvenile studies**: case by case in M3(R2); not listed.
- **Carcinogenicity**: not needed before first-in-human, but this study's liver tumours at
  13 weeks are a finding a regulator would ask about at once.
- **Dose levels and exposure margins**: M3(R2) I.E limits high doses by exposure multiples;
  the inventory records durations, not doses or AUCs.
- **The content of each study**: a study "meets" a requirement by its type, species,
  duration, GLP status and report status. Whether its design or result is adequate is
  not checked. A non-GLP pivotal study is shown, not raised.
- **CMC and impurities** (Q3A, Q3B) and clinical protocol content: outside this agent.
- **The report check measures the plan, not the rule**: FDA's 1995 Phase 1 IND guidance
  (III.G.2) allows an IND on unaudited draft reports, with final reports within 120 days
  of the trial's start.
