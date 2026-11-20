"""From a NOAEL to a maximum recommended starting dose (phase 5 step 2), by FDA's 2005
guidance "Estimating the Maximum Safe Starting Dose in Initial Clinical Trials for
Therapeutics in Adult Healthy Volunteers" (July 2005). Pure arithmetic on a NOAEL and the
cited factors in config ([agents.dose_translation]); preclinical.dose_translation words it
as a proposal. Notes: docs/fih-dose-notes.md.

The steps, each with the section it comes from:
1. the NOAEL in mg/kg (section IV), the toxicologist's current decision;
2. the human equivalent dose: NOAEL / the species' divisor in Table 1 (section V.A);
3. the most appropriate species: here the only species studied (section VI);
4. the MRSD in mg/kg: HED / the safety factor, default 10 (section VII);
5. the starting dose in mg for a 60 kg adult: MRSD x 60 (Table 1, footnote a: "Assumes
   60 kg human").
"""

from dataclasses import dataclass

from meridian.domain.display import sig
from meridian.platform.agent_settings import DoseTranslationConfig, SpeciesConversion


class NoCitedFactorError(ValueError):
    """The study's species has no conversion factor in config: none is used uncited."""


@dataclass(frozen=True, slots=True)
class Working:
    noael_mg_per_kg: float
    species: str
    conversion: SpeciesConversion
    hed_mg_per_kg: float
    hed_by_multiplier_mg_per_kg: float  # Table 1's other column, a cross-check only
    safety_factor: float
    mrsd_mg_per_kg: float
    human_weight_kg: float
    starting_dose_mg: float


def per_kg(dose: float, unit: str) -> float:
    """The NOAEL in mg/kg. The guidance's levels are "mg/kg or mg/kg/day" (section IV); a
    once-daily dose in mg/kg/day is the same number. Any other unit is refused."""
    u = unit.strip().lower().replace(" ", "")
    if u in ("mg/kg", "mg/kg/day", "mg/kg/d"):
        return dose
    raise ValueError(f"a NOAEL in {unit!r}: only mg/kg or mg/kg/day is converted")


def conversion_for(species: str, cfg: DoseTranslationConfig) -> SpeciesConversion:
    key = species.strip().upper()
    if key not in cfg.conversions:
        raise NoCitedFactorError(
            f"no cited conversion factor for species {species!r} in config "
            f"(have {sorted(cfg.conversions)})"
        )
    return cfg.conversions[key]


def working(noael: float, unit: str, species: str, cfg: DoseTranslationConfig) -> Working:
    n = per_kg(noael, unit)
    conv = conversion_for(species, cfg)
    hed = n / conv.divide_by
    mrsd = hed / cfg.safety_factor
    return Working(
        noael_mg_per_kg=n,
        species=species.strip().upper(),
        conversion=conv,
        hed_mg_per_kg=hed,
        hed_by_multiplier_mg_per_kg=n * conv.multiply_by,
        safety_factor=cfg.safety_factor,
        mrsd_mg_per_kg=mrsd,
        human_weight_kg=cfg.human_weight_kg,
        starting_dose_mg=mrsd * cfg.human_weight_kg,
    )


__all__ = [
    "NoCitedFactorError",
    "Working",
    "conversion_for",
    "per_kg",
    "sig",
    "working",
]
