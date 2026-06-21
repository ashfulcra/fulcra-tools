"""OKF v0.1 conformance validation (spec §9), backend-aware."""
from __future__ import annotations

from dataclasses import dataclass, field

from . import frontmatter
from .bundle import Bundle


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
