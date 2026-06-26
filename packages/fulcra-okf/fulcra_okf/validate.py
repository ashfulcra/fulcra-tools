"""OKF v0.1 conformance validation (spec §9), backend-aware."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from . import frontmatter
from .bundle import Bundle

# Matches a level-2 heading line: "## <text>"
_H2_RE = re.compile(r"^## (.+)$", re.MULTILINE)
# ISO 8601 date: YYYY-MM-DD
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass
class Finding:
    path: str
    severity: str  # "error" | "warn" | "info"
    code: str
    message: str


@dataclass
class Report:
    conformant: bool
    findings: list[Finding] = field(default_factory=list)


def _validate_log_md(path: str, text: str, *, strict: bool) -> list[Finding]:
    """Validate a single log.md file against OKF §7 rules.

    Rules enforced (§7 / §9):
    - Every level-2 (##) heading MUST be a valid YYYY-MM-DD date.
    - Dates MUST appear in newest-first (descending) order.

    Severity: warn by default, error under strict=True.
    """
    findings: list[Finding] = []
    severity = "error" if strict else "warn"
    headings = _H2_RE.findall(text)

    dates: list[date] = []
    for heading in headings:
        heading = heading.strip()
        if not _DATE_RE.match(heading):
            findings.append(Finding(
                path=path,
                severity=severity,
                code="reserved_log_bad_heading",
                message=(
                    f"log.md level-2 heading '{heading}' is not a valid ISO 8601 date "
                    f"(YYYY-MM-DD required, spec §7)"
                ),
            ))
        else:
            try:
                parsed = date.fromisoformat(heading)
            except ValueError:
                findings.append(Finding(
                    path=path,
                    severity=severity,
                    code="reserved_log_bad_heading",
                    message=(
                        f"log.md level-2 heading '{heading}' is not a valid calendar date "
                        f"(spec §7)"
                    ),
                ))
            else:
                dates.append(parsed)

    # Check newest-first order among the valid dates we collected.
    for i in range(1, len(dates)):
        if dates[i] >= dates[i - 1]:
            findings.append(Finding(
                path=path,
                severity=severity,
                code="reserved_log_out_of_order",
                message=(
                    f"log.md dates are not in newest-first order: "
                    f"'{dates[i - 1]}' followed by '{dates[i]}' (spec §7)"
                ),
            ))
            break  # report once per file

    return findings


def validate(bundle: Bundle, *, strict: bool = False) -> Report:
    findings: list[Finding] = []

    # Recorded parse failures: backend-aware classification.
    for rel, message in bundle.parse_errors:
        if frontmatter.BACKEND == "flat":
            findings.append(Finding(
                path=rel, severity="error", code="flat_backend_cannot_parse",
                message=(f"frontmatter not parseable by the stdlib flat backend ({message}); "
                         f"install fulcra-okf[yaml] (PyYAML) to certify rich YAML bundles"),
            ))
        else:
            findings.append(Finding(
                path=rel, severity="error", code="unparseable",
                message=f"frontmatter is not parseable YAML: {message}",
            ))

    # Reserved-file structure validation (§9 rule 3).
    for rel, text in bundle.reserved_files.items():
        if rel == "log.md" or rel.endswith("/log.md"):
            findings.extend(_validate_log_md(rel, text, strict=strict))

    ids = set(bundle.concepts)
    for concept in bundle.concepts.values():
        path = concept.id + ".md"
        if not concept.type:
            findings.append(Finding(
                path=path, severity="error", code="missing_type",
                message="every concept must have a non-empty 'type' (spec §9)",
            ))
        for target in concept.links():
            if target not in ids:
                findings.append(Finding(
                    path=path, severity="error" if strict else "info", code="broken_link",
                    message=f"link target '{target}' is not a concept in the bundle",
                ))

    conformant = not any(f.severity == "error" for f in findings)
    return Report(conformant=conformant, findings=findings)
