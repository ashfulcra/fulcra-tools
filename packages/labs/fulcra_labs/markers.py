"""The canonical marker registry — one Fulcra data track per lab marker.

This is the load-bearing table of the whole pipeline. A wrong unit-conversion
factor here silently corrupts a medical time series, so the rules are:

* Every conversion is ``canonical = raw * factor + offset`` (offset defaults to
  0 — it is non-zero only for HbA1c IFCC↔NGSP, which is genuinely affine, not
  a simple ratio). The canonical unit itself maps to ``(1.0, 0.0)``.
* Where a conversion factor is assay-dependent or genuinely ambiguous (BUN
  vs. urea, insulin pmol/L, Hb mmol/L), the alternate unit is DELIBERATELY
  ABSENT from ``accepted_units`` so a value in that unit lands in the review
  queue instead of being silently mis-scaled. Honesty over coverage.
* ``loinc`` is the single most common LOINC code for the marker, or ``None``
  where it is genuinely ambiguous (e.g. differentials that split absolute vs
  percent, or panels that report multiple methods). We never invent a code.
* ``plausible_range`` is a WIDE physiologic sanity bound in the canonical unit
  — NOT a clinical reference range. Its only job is to catch transcription and
  unit-mixup errors (e.g. glucose 5.5 mislabelled mg/dL), never to flag a
  genuinely abnormal-but-real result.

Registered against LabCorp / Quest / hospital-lab report naming via ``aliases``.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# canonical = raw * factor + offset
Conversion = tuple[float, float]


@dataclass(frozen=True)
class Marker:
    key: str                       # kebab-case canonical id
    display_name: str
    canonical_unit: str            # human-facing canonical unit string
    loinc: str | None
    accepted_units: dict[str, Conversion]  # normalized-unit -> conversion
    aliases: tuple[str, ...]
    plausible_range: tuple[float, float]   # (low, high) in canonical unit
    category: str

    def convert(self, value: float, unit_norm: str) -> float:
        factor, offset = self.accepted_units[unit_norm]
        return value * factor + offset


# --------------------------------------------------------------------------
# Normalization helpers
# --------------------------------------------------------------------------

def normalize_unit(unit: str | None) -> str | None:
    """Normalize a printed unit string to a registry key.

    Lower-cases, folds the micro sign (µ/μ) to ``u``, strips spaces, and
    unifies a few common typographic variants (``x10e3`` etc). Returns None
    for a null/blank input (a MISSING unit — never guessed)."""
    if unit is None:
        return None
    u = unicodedata.normalize("NFKC", unit).strip()
    if not u:
        return None
    u = u.lower()
    u = u.replace("µ", "u").replace("μ", "u")
    u = u.replace(" ", "")
    # Common superscript / exponent spellings.
    u = u.replace("×", "x")
    u = u.replace("10^3", "10e3").replace("10^6", "10e6").replace("10^9", "10e9")
    u = u.replace("10*3", "10e3").replace("10*6", "10e6").replace("10*9", "10e9")
    u = u.replace("k/ul", "10e3/ul").replace("m/ul", "10e6/ul")
    u = u.replace("thous/ul", "10e3/ul").replace("mill/ul", "10e6/ul")
    return u


_ALIAS_STRIP = re.compile(r"[^a-z0-9]+")


def normalize_alias(name: str) -> str:
    """Normalize a marker name for alias matching: NFKC-fold, lower-case,
    collapse every run of non-alphanumerics to a single space, and trim.
    ``"LDL Chol Calc (NIH)"`` and ``"ldl-chol-calc-nih"`` both fold to
    ``"ldl chol calc nih"``."""
    s = unicodedata.normalize("NFKC", name).strip().lower()
    s = _ALIAS_STRIP.sub(" ", s).strip()
    return s


# Convenience conversion constants (canonical listed first in each comment).
_ID: Conversion = (1.0, 0.0)


def _u(*pairs: tuple[str, Conversion]) -> dict[str, Conversion]:
    """Build an accepted-units dict, normalizing each unit key."""
    out: dict[str, Conversion] = {}
    for unit, conv in pairs:
        k = normalize_unit(unit)
        assert k is not None
        out[k] = conv
    return out


# --------------------------------------------------------------------------
# The registry
# --------------------------------------------------------------------------
# Conversion provenance (molar-mass derived unless noted):
#   glucose      mmol/L→mg/dL ×18.016   (MW 180.16)
#   cholesterol  mmol/L→mg/dL ×38.67    (MW ~386.65)
#   triglyceride mmol/L→mg/dL ×88.57
#   creatinine   umol/L→mg/dL ×1/88.42  = 0.011312   (MW 113.12)
#   bilirubin    umol/L→mg/dL ×1/17.104 = 0.058465   (MW 584.66)
#   uric acid    umol/L→mg/dL ×1/59.48  = 0.016812   (MW 168.11)
#   calcium      mmol/L→mg/dL ×4.008    (MW 40.08)
#   iron/TIBC    umol/L→ug/dL ×5.587    (MW 55.85)
#   25-OH vit D  nmol/L→ng/mL ×1/2.496  = 0.40064   (MW 400.6)
#   vitamin B12  pmol/L→pg/mL ×1.355    (MW 1355.4)
#   folate       nmol/L→ng/mL ×1/2.266  = 0.44131   (MW 441.4)
#   free T4      pmol/L→ng/dL ×1/12.87  = 0.077700  (MW 776.87)
#   free T3      pmol/L→pg/mL ×1/1.536  = 0.651042  (MW 650.98)
#   HbA1c        mmol/mol→%(NGSP) ×0.09148 + 2.152   (NGSP master equation, AFFINE)
_MARKERS: tuple[Marker, ...] = (
    # ---- Lipid panel -----------------------------------------------------
    Marker("total-cholesterol", "Total Cholesterol", "mg/dL", "2093-3",
           _u(("mg/dL", _ID), ("mmol/L", (38.67, 0.0))),
           ("cholesterol total", "cholesterol, total", "chol", "total cholesterol",
            "cholesterol"),
           (30.0, 800.0), "lipid"),
    Marker("ldl-c", "LDL Cholesterol", "mg/dL", "13457-7",
           _u(("mg/dL", _ID), ("mmol/L", (38.67, 0.0))),
           ("ldl", "ldl-c", "ldl cholesterol", "ldl chol calc", "ldl chol calc nih",
            "ldl cholesterol calc", "cholesterol ldl", "ldl-c (calculated)",
            "ldl cholesterol calculated"),
           (5.0, 600.0), "lipid"),
    Marker("hdl-c", "HDL Cholesterol", "mg/dL", "2085-9",
           _u(("mg/dL", _ID), ("mmol/L", (38.67, 0.0))),
           ("hdl", "hdl-c", "hdl cholesterol", "cholesterol hdl", "hdl chol"),
           (2.0, 200.0), "lipid"),
    Marker("triglycerides", "Triglycerides", "mg/dL", "2571-8",
           _u(("mg/dL", _ID), ("mmol/L", (88.57, 0.0))),
           ("triglycerides", "trig", "trigs", "triglyceride"),
           (10.0, 10000.0), "lipid"),
    Marker("non-hdl-c", "Non-HDL Cholesterol", "mg/dL", "43396-1",
           _u(("mg/dL", _ID), ("mmol/L", (38.67, 0.0))),
           ("non hdl cholesterol", "non-hdl", "non hdl chol", "non hdl-c",
            "non hdl cholesterol calc"),
           (5.0, 700.0), "lipid"),

    # ---- CBC -------------------------------------------------------------
    Marker("wbc", "White Blood Cell Count", "10^3/uL", "6690-2",
           _u(("10^3/uL", _ID), ("10^9/L", _ID), ("K/uL", _ID), ("x10E3/uL", _ID),
              ("thous/uL", _ID)),
           ("wbc", "white blood cell count", "white blood cells", "leukocytes",
            "wbc count"),
           (0.1, 500.0), "cbc"),
    Marker("rbc", "Red Blood Cell Count", "10^6/uL", "789-8",
           _u(("10^6/uL", _ID), ("10^12/L", _ID), ("M/uL", _ID), ("x10E6/uL", _ID),
              ("mill/uL", _ID)),
           ("rbc", "red blood cell count", "red blood cells", "erythrocytes",
            "rbc count"),
           (0.5, 10.0), "cbc"),
    Marker("hemoglobin", "Hemoglobin", "g/dL", "718-7",
           # Hb mmol/L (used in NL) is DELIBERATELY omitted — the ×1.611 factor
           # is a real trap in US LabCorp/Quest context; left to review.
           _u(("g/dL", _ID), ("g/L", (0.1, 0.0))),
           ("hemoglobin", "hgb", "hb", "haemoglobin"),
           (2.0, 25.0), "cbc"),
    Marker("hematocrit", "Hematocrit", "%", "4544-3",
           _u(("%", _ID)),
           ("hematocrit", "hct", "haematocrit", "pcv"),
           (5.0, 75.0), "cbc"),
    Marker("mcv", "Mean Corpuscular Volume", "fL", "787-2",
           _u(("fL", _ID), ("um^3", _ID)),
           ("mcv", "mean corpuscular volume"),
           (40.0, 160.0), "cbc"),
    Marker("mch", "Mean Corpuscular Hemoglobin", "pg", "785-6",
           _u(("pg", _ID)),
           ("mch", "mean corpuscular hemoglobin"),
           (10.0, 60.0), "cbc"),
    Marker("mchc", "Mean Corpuscular Hemoglobin Concentration", "g/dL", "786-4",
           _u(("g/dL", _ID), ("g/L", (0.1, 0.0))),
           ("mchc", "mean corpuscular hemoglobin concentration"),
           (20.0, 45.0), "cbc"),
    Marker("rdw", "Red Cell Distribution Width", "%", "788-0",
           _u(("%", _ID)),
           ("rdw", "rdw-cv", "red cell distribution width", "rdw cv"),
           (8.0, 40.0), "cbc"),
    Marker("platelets", "Platelet Count", "10^3/uL", "777-3",
           _u(("10^3/uL", _ID), ("10^9/L", _ID), ("K/uL", _ID), ("x10E3/uL", _ID),
              ("thous/uL", _ID)),
           ("platelets", "platelet count", "plt", "thrombocytes"),
           (2.0, 3000.0), "cbc"),
    # Differentials — ABSOLUTE counts only (percent vs absolute share names on
    # reports; the absolute count is the clinically tracked series and its
    # unit disambiguates from the percent form).
    Marker("neutrophils-abs", "Neutrophils (Absolute)", "10^3/uL", "751-8",
           _u(("10^3/uL", _ID), ("10^9/L", _ID), ("K/uL", _ID), ("x10E3/uL", _ID)),
           ("neutrophils absolute", "absolute neutrophils", "neutrophils abs",
            "anc", "abs neutrophils", "neutrophil count"),
           (0.0, 100.0), "cbc"),
    Marker("lymphocytes-abs", "Lymphocytes (Absolute)", "10^3/uL", "731-0",
           _u(("10^3/uL", _ID), ("10^9/L", _ID), ("K/uL", _ID), ("x10E3/uL", _ID)),
           ("lymphocytes absolute", "absolute lymphocytes", "lymphocytes abs",
            "abs lymphocytes", "lymphocyte count"),
           (0.0, 100.0), "cbc"),
    Marker("monocytes-abs", "Monocytes (Absolute)", "10^3/uL", "742-7",
           _u(("10^3/uL", _ID), ("10^9/L", _ID), ("K/uL", _ID), ("x10E3/uL", _ID)),
           ("monocytes absolute", "absolute monocytes", "monocytes abs",
            "abs monocytes", "monocyte count"),
           (0.0, 50.0), "cbc"),
    Marker("eosinophils-abs", "Eosinophils (Absolute)", "10^3/uL", "711-2",
           _u(("10^3/uL", _ID), ("10^9/L", _ID), ("K/uL", _ID), ("x10E3/uL", _ID)),
           ("eosinophils absolute", "absolute eosinophils", "eosinophils abs",
            "abs eos", "eosinophil count"),
           (0.0, 50.0), "cbc"),
    Marker("basophils-abs", "Basophils (Absolute)", "10^3/uL", "704-7",
           _u(("10^3/uL", _ID), ("10^9/L", _ID), ("K/uL", _ID), ("x10E3/uL", _ID)),
           ("basophils absolute", "absolute basophils", "basophils abs",
            "abs baso", "basophil count"),
           (0.0, 20.0), "cbc"),

    # ---- CMP -------------------------------------------------------------
    Marker("glucose", "Glucose", "mg/dL", "2345-7",
           _u(("mg/dL", _ID), ("mmol/L", (18.016, 0.0))),
           ("glucose", "glucose, fasting", "fasting glucose", "glu",
            "glucose serum", "glucose fasting"),
           (10.0, 2000.0), "cmp"),
    Marker("bun", "Blood Urea Nitrogen", "mg/dL", "3094-0",
           # mmol/L urea vs BUN is ambiguous (urea vs urea-nitrogen); omitted.
           _u(("mg/dL", _ID)),
           ("bun", "blood urea nitrogen", "urea nitrogen", "urea nitrogen bun"),
           (1.0, 300.0), "cmp"),
    Marker("creatinine", "Creatinine", "mg/dL", "2160-0",
           _u(("mg/dL", _ID), ("umol/L", (0.011312, 0.0))),
           ("creatinine", "creatinine, serum", "creat", "creatinine serum"),
           (0.1, 25.0), "cmp"),
    Marker("egfr", "Estimated GFR", "mL/min/1.73m2", None,
           _u(("mL/min/1.73m2", _ID), ("mL/min/1.73", _ID)),
           ("egfr", "estimated gfr", "gfr estimated", "egfr non african american",
            "egfr if nonafricn am", "egfr ckd epi", "gfr"),
           (1.0, 200.0), "cmp"),
    Marker("sodium", "Sodium", "mmol/L", "2951-2",
           _u(("mmol/L", _ID), ("mEq/L", _ID)),
           ("sodium", "na", "sodium serum"),
           (100.0, 200.0), "cmp"),
    Marker("potassium", "Potassium", "mmol/L", "2823-3",
           _u(("mmol/L", _ID), ("mEq/L", _ID)),
           ("potassium", "k", "potassium serum"),
           (1.0, 10.0), "cmp"),
    Marker("chloride", "Chloride", "mmol/L", "2075-0",
           _u(("mmol/L", _ID), ("mEq/L", _ID)),
           ("chloride", "cl", "chloride serum"),
           (60.0, 160.0), "cmp"),
    Marker("co2", "Carbon Dioxide (Bicarbonate)", "mmol/L", "2028-9",
           _u(("mmol/L", _ID), ("mEq/L", _ID)),
           ("co2", "carbon dioxide", "carbon dioxide total", "bicarbonate",
            "hco3", "co2 total", "carbon dioxide, total"),
           (5.0, 60.0), "cmp"),
    Marker("calcium", "Calcium", "mg/dL", "17861-6",
           _u(("mg/dL", _ID), ("mmol/L", (4.008, 0.0))),
           ("calcium", "ca", "calcium serum"),
           (3.0, 20.0), "cmp"),
    Marker("total-protein", "Total Protein", "g/dL", "2885-2",
           _u(("g/dL", _ID), ("g/L", (0.1, 0.0))),
           ("total protein", "protein total", "protein, total", "tp"),
           (1.0, 15.0), "cmp"),
    Marker("albumin", "Albumin", "g/dL", "1751-7",
           _u(("g/dL", _ID), ("g/L", (0.1, 0.0))),
           ("albumin", "albumin serum", "alb"),
           (0.5, 7.0), "cmp"),
    Marker("bilirubin-total", "Bilirubin, Total", "mg/dL", "1975-2",
           _u(("mg/dL", _ID), ("umol/L", (0.058465, 0.0))),
           ("bilirubin total", "bilirubin, total", "total bilirubin", "tbili",
            "bili total", "bilirubin"),
           (0.05, 50.0), "cmp"),
    Marker("alp", "Alkaline Phosphatase", "U/L", "6768-6",
           _u(("U/L", _ID), ("IU/L", _ID), ("ukat/L", (60.0, 0.0))),
           ("alkaline phosphatase", "alp", "alk phos", "alkaline phosphatase s"),
           (5.0, 2000.0), "cmp"),
    Marker("ast", "AST (SGOT)", "U/L", "1920-8",
           _u(("U/L", _ID), ("IU/L", _ID), ("ukat/L", (60.0, 0.0))),
           ("ast", "sgot", "ast sgot", "aspartate aminotransferase", "ast (sgot)"),
           (2.0, 20000.0), "cmp"),
    Marker("alt", "ALT (SGPT)", "U/L", "1742-6",
           _u(("U/L", _ID), ("IU/L", _ID), ("ukat/L", (60.0, 0.0))),
           ("alt", "sgpt", "alt sgpt", "alanine aminotransferase", "alt (sgpt)"),
           (2.0, 20000.0), "cmp"),

    # ---- Thyroid ---------------------------------------------------------
    Marker("tsh", "Thyroid Stimulating Hormone", "uIU/mL", "3016-3",
           _u(("uIU/mL", _ID), ("mIU/L", _ID), ("uU/mL", _ID)),
           ("tsh", "thyroid stimulating hormone", "thyrotropin", "tsh 3rd generation"),
           (0.001, 150.0), "thyroid"),
    Marker("free-t4", "Free T4", "ng/dL", "3024-7",
           _u(("ng/dL", _ID), ("pmol/L", (0.077700, 0.0))),
           ("free t4", "t4 free", "t4, free", "ft4", "t4,free(direct)",
            "free thyroxine", "t4 free direct"),
           (0.1, 12.0), "thyroid"),
    Marker("free-t3", "Free T3", "pg/mL", "3051-0",
           _u(("pg/mL", _ID), ("pmol/L", (0.651042, 0.0))),
           ("free t3", "t3 free", "t3, free", "ft3", "triiodothyronine free"),
           (0.5, 30.0), "thyroid"),

    # ---- Metabolic / other ----------------------------------------------
    Marker("hba1c", "Hemoglobin A1c", "%", "4548-4",
           # mmol/mol (IFCC) is AFFINE, not a ratio — modeled with an offset.
           _u(("%", _ID), ("mmol/mol", (0.09148, 2.152))),
           ("hemoglobin a1c", "hba1c", "a1c", "hgb a1c", "glycohemoglobin",
            "hemoglobin a1c hgb"),
           (2.0, 20.0), "metabolic"),
    Marker("insulin", "Insulin", "uIU/mL", "20448-7",
           # pmol/L conversion is assay-dependent (÷6.0 vs ÷6.945); omitted.
           _u(("uIU/mL", _ID), ("mIU/L", _ID), ("uU/mL", _ID)),
           ("insulin", "insulin fasting", "fasting insulin", "insulin serum"),
           (0.1, 1000.0), "metabolic"),
    Marker("uric-acid", "Uric Acid", "mg/dL", "3084-1",
           _u(("mg/dL", _ID), ("umol/L", (0.016812, 0.0))),
           ("uric acid", "urate", "uric acid serum"),
           (0.5, 30.0), "metabolic"),
    Marker("vitamin-d-25oh", "Vitamin D, 25-Hydroxy", "ng/mL", "1989-3",
           _u(("ng/mL", _ID), ("nmol/L", (0.40064, 0.0))),
           ("vitamin d 25 hydroxy", "25 hydroxyvitamin d", "vitamin d 25 oh total",
            "vitamin d 25-hydroxy", "25-oh vitamin d", "vitamin d total",
            "vitamin d, 25-hydroxy"),
           (1.0, 200.0), "metabolic"),
    Marker("vitamin-b12", "Vitamin B12", "pg/mL", "2132-9",
           _u(("pg/mL", _ID), ("pmol/L", (1.355, 0.0)), ("ng/L", _ID)),
           ("vitamin b12", "b12", "cobalamin", "vitamin b 12"),
           (50.0, 5000.0), "metabolic"),
    Marker("folate", "Folate", "ng/mL", "2284-8",
           _u(("ng/mL", _ID), ("nmol/L", (0.44131, 0.0)), ("ug/L", _ID)),
           ("folate", "folate serum", "folic acid", "folate, serum"),
           (0.2, 60.0), "metabolic"),
    Marker("ferritin", "Ferritin", "ng/mL", "2276-4",
           _u(("ng/mL", _ID), ("ug/L", _ID)),
           ("ferritin", "ferritin serum"),
           (1.0, 100000.0), "metabolic"),
    Marker("iron", "Iron", "ug/dL", "2498-4",
           _u(("ug/dL", _ID), ("umol/L", (5.587, 0.0))),
           ("iron", "iron serum", "iron total", "iron, total", "fe"),
           (5.0, 1000.0), "metabolic"),
    Marker("tibc", "Total Iron Binding Capacity", "ug/dL", "2500-7",
           _u(("ug/dL", _ID), ("umol/L", (5.587, 0.0))),
           ("tibc", "total iron binding capacity", "iron binding capacity total"),
           (100.0, 1000.0), "metabolic"),
    Marker("transferrin-saturation", "Transferrin Saturation", "%", "2502-3",
           _u(("%", _ID)),
           ("transferrin saturation", "iron saturation", "% saturation",
            "iron sat", "transferrin sat", "% transferrin saturation",
            "iron saturation %"),
           (0.0, 150.0), "metabolic"),
    Marker("transferrin", "Transferrin", "mg/dL", "3034-6",
           _u(("mg/dL", _ID), ("g/L", (100.0, 0.0))),
           ("transferrin", "transferrin serum"),
           (50.0, 600.0), "metabolic"),
    Marker("crp", "C-Reactive Protein", "mg/L", "1988-5",
           _u(("mg/L", _ID), ("mg/dL", (10.0, 0.0))),
           ("crp", "c reactive protein", "c-reactive protein"),
           (0.1, 500.0), "metabolic"),
    Marker("hs-crp", "hs-CRP (Cardiac)", "mg/L", "30522-7",
           _u(("mg/L", _ID), ("mg/dL", (10.0, 0.0))),
           ("hs crp", "hs-crp", "high sensitivity crp", "cardio crp",
            "c reactive protein cardiac", "crp high sensitivity"),
           (0.01, 100.0), "metabolic"),
    Marker("psa", "Prostate-Specific Antigen", "ng/mL", "2857-1",
           _u(("ng/mL", _ID), ("ug/L", _ID)),
           ("psa", "prostate specific antigen", "psa total", "psa, total",
            "prostate-specific antigen total"),
           (0.01, 10000.0), "metabolic"),
)


# --------------------------------------------------------------------------
# Indexes
# --------------------------------------------------------------------------

BY_KEY: dict[str, Marker] = {m.key: m for m in _MARKERS}

# alias-normalized string -> list of marker keys that claim it (a bare name
# like "cholesterol" may in principle be shared; we keep a list so the
# resolver can disambiguate by unit rather than silently pick one).
_ALIAS_INDEX: dict[str, list[str]] = {}


def _register_alias(alias: str, key: str) -> None:
    norm = normalize_alias(alias)
    if not norm:
        return
    bucket = _ALIAS_INDEX.setdefault(norm, [])
    if key not in bucket:
        bucket.append(key)


for _m in _MARKERS:
    _register_alias(_m.key, _m.key)
    _register_alias(_m.display_name, _m.key)
    for _a in _m.aliases:
        _register_alias(_a, _m.key)


@dataclass
class MarkerResolution:
    """Outcome of resolving a printed marker name.

    ``marker`` is set on an unambiguous exact-alias hit. ``candidates`` holds
    every marker key an exact alias matched (len > 1 → ambiguous, disambiguate
    by unit upstream). ``suggestions`` holds fuzzy near-matches offered ONLY as
    hints — a fuzzy match is NEVER auto-resolved."""
    marker: Marker | None
    candidates: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)


def resolve_marker(marker_raw: str) -> MarkerResolution:
    """Resolve a printed marker name to a canonical Marker.

    Exact alias match (after normalization) wins. If several markers share the
    normalized alias, all are returned as ``candidates`` for unit-based
    disambiguation. On no exact match, close names are returned as
    ``suggestions`` only — fuzzy hits never auto-resolve."""
    norm = normalize_alias(marker_raw)
    keys = _ALIAS_INDEX.get(norm)
    if keys:
        if len(keys) == 1:
            return MarkerResolution(marker=BY_KEY[keys[0]], candidates=list(keys))
        return MarkerResolution(marker=None, candidates=list(keys))
    # No exact hit — offer fuzzy suggestions (hints only).
    import difflib

    close = difflib.get_close_matches(norm, _ALIAS_INDEX.keys(), n=3, cutoff=0.82)
    suggestions: list[str] = []
    for c in close:
        for k in _ALIAS_INDEX[c]:
            if k not in suggestions:
                suggestions.append(k)
    return MarkerResolution(marker=None, suggestions=suggestions)


def all_markers() -> tuple[Marker, ...]:
    return _MARKERS


def marker_namespace(key: str) -> str:
    """The stable machine token embedded in a marker definition's description
    so an existing per-marker track can be adopted across machines/runs."""
    return f"com.fulcra.labs.marker.{key}"
