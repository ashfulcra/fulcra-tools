# The anti-slop patterns, in Python

[dmmulroy/anti-slop](https://github.com/dmmulroy/anti-slop) ships as an Oxlint
plugin, so on its own it reaches exactly the 106 TypeScript files in
`packages/attention/chrome` and none of the ~35,000 lines of Python where the
coordination engine actually lives.

Ash, 2026-09-04: *"the value of the antislop is the patterns, not the language
specificity."* This document is that correction made concrete — each upstream
rule, the behaviour it is actually protecting, and how that behaviour is
enforced here.

Run it: `scripts/anti-slop-python.sh` (add paths to narrow it).

## What the ruleset is really about

Strip the TypeScript and four claims remain, none of them language-specific:

1. **Do not fabricate evidence.** An assertion that a value has a type is a
   claim. If nothing checked it, the claim is invented.
2. **Do not launder a known value through a dynamic type.** Widening to
   `unknown` / `Any` and narrowing back later throws away what the caller
   already knew and makes the loss invisible.
3. **Parse at the boundary, don't narrow ad hoc.** One place converts foreign
   data into a domain type. Scattered `typeof` / `isinstance` checks are that
   parse, done repeatedly and inconsistently.
4. **Never swallow the evidence of a failure.** A caught-and-discarded error is
   indistinguishable from success at every call site above it.

Claim 4 is why this repo needed the ruleset. The engine's worst defects this
year were all of that shape: `transport.read` collapsing "absent" and
"unreadable" into `None`, `transport.write` returning a bool nobody read, an
attendance scan reporting a budget-cut result as a clean one. Each was a
failure that arrived looking like an ordinary answer.

## Rule mapping

| anti-slop rule | what it protects | Python enforcement |
| --- | --- | --- |
| `no-unknown-parameters` | inputs carry a real contract | `ANN401` (any-type) |
| `no-unknown-returns` | outputs carry a real contract | `ANN401` |
| `no-unknown-type-aliases` | an alias cannot hide `Any` | `ANN401` |
| `no-unsafe-dictionary-type` | a dict's values have a contract | `ANN401` on `Mapping[str, Any]` etc. |
| `no-known-value-widening` | a known value is not widened away | `ANN401` |
| `no-widen-then-assert` | widen-then-narrow round trips | `ANN401`; `PGH003` for blanket `# type: ignore` |
| `no-chained-type-assertions` | assertion chains discard evidence | `typing.cast` (1 use here) + `PGH003` |
| `require-safety-comment-for-type-assertion` | every assertion states its invariant | reviewed by hand — see below |
| `no-runtime-typeof` | parse at the boundary | partly `BLE001`; the `isinstance` half has no ruff analogue |
| `no-module-mocking` | real dependency seams, not patched modules | not enforced — see below |
| `no-reflect-get` / `no-reflect-apply` | typed access, not dynamic lookup | `getattr`/`setattr`; not enforced |
| `no-conditional-empty-object-spread` | omission ≠ `undefined` | no Python analogue |
| `no-shape-in-symbol-names` | naming | no analogue worth enforcing |
| — | a swallowed failure is not a handled one | `BLE001`, `S110`, `S112`, `SIM105`, `TRY300`, `TRY301` |

## Measured, 2026-09-04

`scripts/anti-slop-python.sh --statistics` over `packages/`:

| rule | count |
| --- | --- |
| `ANN401` any-type | 466 |
| `BLE001` blind-except | 205 |
| `SIM105` suppressible-exception | 45 |
| `TRY300` try-consider-else | 39 |
| `S110` try-except-pass | 21 |
| `TRY301` raise-within-try | 12 |
| `S112` try-except-continue | 4 |
| **total** | **792** |

Narrowed to `coord_engine` + `coord_tracker_bridge` source only: 597.

## Where Python is already clean, and where it is not

Two of the upstream rules turn out to have almost nothing to catch here:
`typing.cast` appears **once** in engine source, and all six `# type: ignore`
comments carry an explicit error code, so `PGH003` reports zero. The
fabricated-evidence family — the heart of anti-slop in TypeScript — is
essentially a non-problem in this codebase, because Python code that wants to
lie about a type does not need a cast to do it.

That is exactly why the mapping matters rather than the port. The same four
claims land on completely different counts in the two languages: TypeScript's
273 findings are 61% missing safety comments on assertions, while Python's 792
are 59% `Any` in annotations and 26% swallowed exceptions. Running the
TypeScript rules and stopping would have reported the Chrome extension as this
repo's risk surface, which is not true.

## Deliberately not enforced

- **`no-module-mocking`.** There are 1,376 `mock.patch` / `monkeypatch.setattr`
  uses across the test suites. The upstream argument is sound — a patched module
  tests the patch — but the coord tests use it heavily and correctly to inject a
  fake transport, which is a real seam expressed through patching rather than an
  absent one. Converting them is a design project, not a lint fix, and it is not
  in scope for this change.
- **The `isinstance` half of `no-runtime-typeof`.** 548 uses, and ruff has no
  rule that distinguishes boundary parsing from ad-hoc narrowing. A rule that
  cannot tell those apart would produce noise, and a noisy rule gets silenced.

## Why this is not in CI

`ruff.lint.select` is empty by default and the script is the only place the rule
list is spelled out as a runnable command. Making 792 findings a required check
would force one of two things: mass-editing 35,000 lines in a single pass, or
silencing rules to reach green. The second destroys the signal the rules were
selected for, and the first is not reviewable.

The honest state is: the patterns are named, the enforcement is runnable, the
count is recorded here so drift is visible, and the cleanup is a separate
decision that has not been made.
