"""The term pairs the recoded_term and lookalike_term safety faults plant (step 3b2).

Each pair starts from a preferred term the CDISC pilot uses (the clinical AE keeps it)
and gives the term the safety case is coded to instead:

* RECODED: the same event under another preferred term (an administration-site term for
  an application-site one, a lowest-level term coded as the preferred term, a more
  specific term). The case keeps the reporter's verbatim words: only the coding differs.
  The expected adjudication is "yes".
* LOOKALIKE: a different event with a similar name (an opposite, another rhythm, another
  condition of the same site). The case's verbatim term is the new term in lower case.
  The expected adjudication is "no".

No pair (in either order) is a pair of data/term_pairs/dev.jsonl or of the user's sealed
term set (tests/unit/test_term_faults.py checks dev, and the sealed set when it is
present locally): the faults must not measure what those sets measure.
"""

RECODED: tuple[tuple[str, str], ...] = (
    ("APPLICATION SITE PRURITUS", "ADMINISTRATION SITE PRURITUS"),
    ("APPLICATION SITE ERYTHEMA", "ADMINISTRATION SITE ERYTHEMA"),
    ("APPLICATION SITE IRRITATION", "ADMINISTRATION SITE IRRITATION"),
    ("APPLICATION SITE DERMATITIS", "ADMINISTRATION SITE DERMATITIS"),
    ("PRURITUS", "PRURITUS GENERALISED"),
    ("SINUS BRADYCARDIA", "BRADYCARDIA"),
    ("NASOPHARYNGITIS", "COMMON COLD"),
    ("HEADACHE", "TENSION HEADACHE"),
    ("HYPERHIDROSIS", "SWEATING INCREASED"),
    ("ABDOMINAL PAIN", "STOMACH ACHE"),
    ("FATIGUE", "TIREDNESS"),
    ("CONTUSION", "BRUISE"),
    ("OEDEMA PERIPHERAL", "PERIPHERAL SWELLING"),
    ("SOMNOLENCE", "DROWSINESS"),
    ("BACK PAIN", "BACKACHE"),
    ("RASH", "SKIN ERUPTION"),
    ("CONFUSIONAL STATE", "CONFUSION"),
    ("ARTHRALGIA", "JOINT PAIN"),
    ("HYPERTENSION", "BLOOD PRESSURE INCREASED"),
    ("COUGH", "COUGHING"),
)

LOOKALIKE: tuple[tuple[str, str], ...] = (
    ("SINUS BRADYCARDIA", "SINUS TACHYCARDIA"),
    ("HYPERHIDROSIS", "HYPOHIDROSIS"),
    ("ATRIAL FIBRILLATION", "VENTRICULAR FIBRILLATION"),
    ("VENTRICULAR EXTRASYSTOLES", "SUPRAVENTRICULAR EXTRASYSTOLES"),
    ("INSOMNIA", "HYPERSOMNIA"),
    ("DECREASED APPETITE", "INCREASED APPETITE"),
    ("NASOPHARYNGITIS", "LARYNGITIS"),
    ("PRURITUS", "PRURIGO"),
    ("ERYTHEMA", "ERYTHEMA NODOSUM"),
    ("APPLICATION SITE PRURITUS", "APPLICATION SITE PAIN"),
    ("APPLICATION SITE ERYTHEMA", "APPLICATION SITE ULCER"),
    ("HYPERTENSION", "PULMONARY HYPERTENSION"),
    ("URTICARIA", "URTICARIA PIGMENTOSA"),
    ("CONJUNCTIVITIS", "CONJUNCTIVAL HAEMORRHAGE"),
)

RECODED_TERM = "recoded_term"
LOOKALIKE_TERM = "lookalike_term"
PAIRS: dict[str, tuple[tuple[str, str], ...]] = {
    RECODED_TERM: RECODED,
    LOOKALIKE_TERM: LOOKALIKE,
}
EXPECTED_VERDICT: dict[str, str] = {RECODED_TERM: "yes", LOOKALIKE_TERM: "no"}
