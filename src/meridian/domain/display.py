"""How numbers are shown to a person (wrap-up). Pure; the ledger keeps every value at
full precision, and these apply only when text is shown."""

import re

# A decimal number before a dose unit.
_DOSE = re.compile(r"(?<![\w.])(\d+\.\d+)(?= ?(?:mg|mg/kg|mg/kg/day)\b)")


def sig(value: float, digits: int = 3) -> str:
    """A value at a fixed number of significant figures, for display."""
    return f"{value:.{digits}g}"


def display_doses(text: str) -> str:
    """Every dose a person reads at 3 significant figures (1.94 mg, not 1.93548 mg).
    Applied to agent text when it is shown; the ledger keeps what was recorded."""
    return _DOSE.sub(lambda m: sig(float(m.group(1))), text)


__all__ = ["display_doses", "sig"]
