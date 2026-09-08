# Public repository privacy

Collect and every Fulcra tool in this repository must work for a new user.
Keep credentials, account IDs, personal contacts, notes, browsing or media
history, health records, device names, home paths, private team reports, and
real library measurements out of source, examples, tests, public messages,
and release artifacts. Obtain runtime values from the current user's settings
or authenticated account. Do not use a maintainer's account as a default.

Fixtures must be deliberately synthetic. Preserve the protocol shape and edge
cases, replace identifiers and values, and record provenance when a fixture is
derived from captured output. Redacting a name alone does not make a record
safe. Check retired as well as current account identifiers, including test
constants and installer fallbacks. UUID syntax is not evidence of anonymity;
use deliberately invented values and trace copied fixtures to their source.
Capture helpers must sanitize before writing into the checkout. Keep raw
captures and audit evidence outside the repository.

Before publishing, review the complete staged diff and run:

```sh
python3 scripts/privacy_guard.py --self-test
python3 scripts/privacy_guard.py --export-to /tmp/collect-public-tree
# The export destination must be empty.
gitleaks dir /tmp/collect-public-tree --redact --no-banner --max-archive-depth 3
```

CI runs these checks on tracked files, including supported archive contents and
metadata. The guard also rejects known private configuration filenames (`.env`,
`linear.env`, and `answers-linear-ids.json`), even inside archives. Keep these
local and ignored; publish a synthetic `.example` file for setup instructions.
The guard detects selected high-confidence patterns; manual review
must cover names, account identifiers, measurements, and fixture provenance.
Diagnostics must not echo matched private values. Do not disable a detector
for an entire fixture directory to silence a finding.

The macOS build removes nonportable pip entry points and rejects the builder's
home path in filenames, links, text, and binary metadata before signing. Verify
that packaged data contains no credentials, user configuration, database
snapshots, or logs. Signing certificates include public publisher attribution;
use an explicitly approved publisher identity.

A clean current tree does not erase Git history, old pull-request refs, forks,
or cached downloads. Investigate prior exposure separately. Revoke exposed
credentials when applicable and arrange coordinated history/cache cleanup
without silently breaking consumers pinned to existing commits.

Coordination defaults use `user` as the human principal. Existing deployments
with a different principal should configure their policy or pass `--principal`
explicitly; public examples must use synthetic identities.
