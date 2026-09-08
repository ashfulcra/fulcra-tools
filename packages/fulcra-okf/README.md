# fulcra-okf

A Python library and CLI for Open Knowledge Format (OKF) v0.1 bundles:
directories of Markdown concepts with frontmatter and links. It loads, validates,
and writes concept files. It has no Fulcra-specific runtime logic and needs
neither an account nor Collect.

## Install

Requires Python 3.11+. From the repository root:

```bash
uv tool install './packages/fulcra-okf[yaml]'
fulcra-okf --help
```

The base package has no runtime dependencies. The optional `yaml` extra adds
PyYAML for nested and richer YAML data. Without it, the flat backend supports
scalar values and scalar lists and reports structures it cannot parse.
Formatting normalizes frontmatter; it does not preserve YAML comments or layout.

## CLI

```bash
fulcra-okf validate ./knowledge --strict --json
fulcra-okf info ./knowledge
fulcra-okf fmt ./knowledge --check
```

`validate` checks missing types, frontmatter parse failures, internal concept
links, and the date headings/order in reserved `log.md` files. Broken links are
informational by default; `--strict` makes them errors and promotes log warnings
to errors. Exit 0 means the implemented checks found no errors; exit 1 means
nonconformance. It is not a certification of every possible OKF requirement.

`info` summarizes concepts, types, reserved files, and broken links. It does not
replace validation. `fmt --check` reports files that would change and exits 1
when changes or parse/render errors exist. **`fmt` without `--check` rewrites
concept files in place.** Inspect a copy or version-controlled bundle first.

## Python API

```python
from fulcra_okf import Bundle, validate

bundle = Bundle.load_dir("knowledge", lenient=True)
report = validate(bundle, strict=True)
for finding in report.findings:
    print(finding.severity, finding.path, finding.message)
if report.conformant:
    bundle.write_dir("normalized-concepts")
```

`lenient=True` records parse errors for the validator rather than raising on
the first malformed concept. `Bundle.write_dir()` writes concept files only;
it does not copy reserved `index.md` or `log.md` files. Copy those separately
when producing a complete bundle. Extension helpers live in
[`fulcra_okf.ext`](fulcra_okf/ext.py).

The [vendored specification](SPEC.md) is the format reference. The
[validator](fulcra_okf/validate.py) defines the checks implemented here.

## Test

From the repository root:

```bash
uv run --package fulcra-okf --extra dev --no-editable pytest packages/fulcra-okf/tests -q
```
