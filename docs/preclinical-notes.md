# What a toxicologist looks for in a repeat-dose study

Written for a reader new to toxicology, as background for preclinical.tox_study_monitor
(phase 5). Sources are listed at the end and cited as [n].

## The design

A repeat-dose (general) toxicity study gives the test article every day for a fixed period
(2, 4, 13 or 26 weeks in rodents) to find which organs it affects, at which doses, and
whether the effects reverse [1, 2]. A GLP rat study in support of a first-in-human trial
typically has:

- **A concurrent control group** given the vehicle alone, handled exactly like the others.
  Every comparison in the study is against it, never against another study's animals.
- **Three dose groups**, low, mid and high, usually spaced by a factor of 3 to 10. The high
  dose is chosen to produce some toxicity; the low dose aims at a multiple of the expected
  human exposure [1, 2].
- **Both sexes**, analysed separately: male and female rats differ in body weight, liver
  enzymes and drug metabolism, so an effect can appear in one sex only.
- **Ten or more animals per sex per group** for the main (terminal) sacrifice [2].
- **Recovery groups** (often control and high dose, sometimes all groups): animals kept
  without treatment for 2 to 4 weeks after the last dose, to show whether a change reverses.
- **Toxicokinetic (TK) satellite groups**: animals bled for drug exposure only, usually not
  examined for toxicity.

## The endpoints, by SEND domain

SEND (the CDISC Standard for Exchange of Nonclinical Data) holds each kind of observation
in its own domain [3]:

| Domain | What it holds | Read as |
|---|---|---|
| DM, TX, TS, EX | animals and their group; the groups (dose, sex, recovery, TK); the study design; dosing | the design |
| DS | how each animal left the study: scheduled sacrifice, found dead, moribund sacrifice | mortality |
| BW (and BG) | body weight at each weighing; body weight gain | general health; often the most sensitive sign |
| FW | food consumption, often per cage | explains body weight changes |
| CL | clinical observations (signs seen in the cage) | incidence |
| LB | clinical pathology: haematology, clinical chemistry (ALT, AST, ALP), coagulation, urinalysis | group means |
| OM | organ weights, absolute and relative to body weight or brain weight | group means |
| MA | macroscopic (gross) findings at necropsy | incidence |
| MI | microscopic (histopathology) findings, with a severity grade (minimal to severe) | incidence and severity |

A continuous endpoint (a weight, an enzyme level) is compared as group means against the
control; an incidence endpoint (a lesion, a sign, a death) as the number of affected
animals out of those examined.

## Treatment-related is not the same as adverse

Two separate questions are asked of every difference from control [4, 5, 6]:

1. **Is it treatment-related?** That is, caused by the test article rather than chance or
   biological variation. The evidence is a dose response (more or worse at higher doses),
   statistical significance, consistency between sexes and with related endpoints (a rise
   in ALT with liver hypertrophy and a heavier liver), magnitude beyond the normal range,
   and plausibility given the pharmacology.
2. **If treatment-related, is it adverse?** That is, harmful to the animal: an impairment
   of function or a change likely to progress. An adaptive change (liver enlargement from
   enzyme induction with no damage) or an exaggerated but harmless pharmacological effect
   is treatment-related and **not** adverse [5, 6].

Statistics answer neither question alone. With hundreds of endpoints, some differences are
significant by chance; a large effect in a few animals may be real without significance.
The toxicologist and pathologist weigh the whole picture. So a program may say "this
change is dose-related"; only a person says "this change is adverse".

## NOAEL and LOAEL

- **NOAEL** (no observed adverse effect level): the highest dose with no adverse effect.
- **LOAEL** (lowest observed adverse effect level): the lowest dose with an adverse effect.

Both depend on the adversity judgement above, made by the study toxicologist, and on the
doses chosen (the true threshold lies somewhere between the NOAEL and the LOAEL). A NOAEL
can be "not established" when even the low dose is adverse. That is why the NOAEL is a
person's decision, documented with its reasons, and not a computed number [5, 6].

## From the NOAEL to the first dose in people

ICH M3(R2) sets which nonclinical studies must precede each clinical phase [1]. The FDA's
2005 guidance on the maximum recommended starting dose (MRSD) turns the NOAEL into a
starting dose [7]:

1. Take the NOAEL from the most appropriate species (by default the most sensitive).
2. Convert it to a **human equivalent dose (HED)** by body surface area: for the rat,
   HED (mg/kg) = rat NOAEL (mg/kg) / 6.2 [7, Table 1].
3. Divide by a **safety factor**, 10 by default, larger when the toxicity is severe,
   steep or poorly monitorable: MRSD = HED / 10.

Example: a rat NOAEL of 20 mg/kg/day gives HED 3.2 mg/kg and an MRSD of 0.32 mg/kg, about
19 mg for a 60 kg adult. ICH M3(R2) and the FDA guidance also allow exposure-based or
pharmacology-based (MABEL) approaches; the NOAEL route is the default for small molecules.

## Sources

1. ICH M3(R2), Guidance on nonclinical safety studies for the conduct of human clinical
   trials and marketing authorization for pharmaceuticals, 2009.
   https://database.ich.org/sites/default/files/M3_R2__Guideline.pdf
2. OECD Test Guideline 408, Repeated Dose 90-Day Oral Toxicity Study in Rodents, 2018.
   https://doi.org/10.1787/9789264070707-en
3. CDISC SEND Implementation Guide 3.1, 2016. https://www.cdisc.org/standards/foundational/send
4. Lewis RW et al. Recognition of adverse and nonadverse effects in toxicity studies.
   Toxicol Pathol 2002;30(1):66-74. https://doi.org/10.1080/01926230252824725
5. Kerlin R et al. Scientific and Regulatory Policy Committee: recommended ("best")
   practices for determining, communicating, and using adverse effect data from
   nonclinical studies. Toxicol Pathol 2016;44(2):147-162.
   https://doi.org/10.1177/0192623315623265
6. Palazzi X et al. Characterizing "adversity" of pathology findings in nonclinical
   toxicity studies: results from the 4th ESTP International Expert Workshop. Toxicol
   Pathol 2016;44(6):810-824. https://doi.org/10.1177/0192623316642527
7. FDA. Guidance for Industry: Estimating the maximum safe starting dose in initial
   clinical trials for therapeutics in adult healthy volunteers, July 2005.
   https://www.fda.gov/media/72309/download
