# Fulcra authentication for native Netflix imports

The importer uses installed standalone-runtime CLI authentication, independently
of Collect's saved daemon account. It does not ask for passwords or print bearer
tokens. Required installed sibling capabilities are checked before live auth or
source acquisition; local check-only needs neither authentication nor runtime.
Use the [package installation guide](../../../README.md#install-and-invocation).

## Reuse or authenticate

Use the installed `fulcra-api user-info` command to inspect the CLI account.
A successful identity response is useful evidence; a nonzero/network failure is
UNKNOWN, not proof that the user needs a different account. Diagnose connectivity
or expired auth before starting another login. An actual import re-proves its
captured principal and destination authority; this preliminary CLI read is not
permission to substitute accounts during recovery.

When login is needed, the installed CLI supports the split browser flow:

```sh
fulcra-api auth login --get-auth-url
fulcra-api auth login --device-code <DEVICE_CODE> --poll-timeout=5
```

The split-flow flags are `--get-auth-url`, `--device-code` and `--poll-timeout`.
Relay the returned web URL/code to the user; keep the device code private.
Use the CLI's returned expiry and polling instructions, bounded polls, and one
active flow. Do not print tokens, credential-file contents or device codes into
chat/log excerpts, run an interactive foreground login that cannot be completed,
or start repeated auth flows merely because a provider read failed. Existing
user authorization to install/authenticate still applies; explain a specific
missing prerequisite rather than asking generic permission to continue.

After success, recheck user-info and use the intended same CLI account/profile
for import/recovery. The default profile is `~/.config/fulcra-collect`;
`FULCRA_COLLECT_HOME` can select a different one explicitly. Preserve its journals
and pending records. Changed account/pin/spec/profile holds pending work; never
clear it by swapping credentials, deleting state or retyping legacy definitions.

## Importer boundary

The old per-run `get_token` subprocess loop and sampled-readback token helpers
are retired from the Netflix script. The installed shared client/runtime owns
authentication and same-principal freshness checks. No script fallback installs
a CLI, prints/mints a bearer for an agent to handle, or retries through a legacy
annotation endpoint. Do not paste `auth print-access-token` output into a
conversation as a setup/debug step. Credential storage/refresh behavior belongs
to the installed CLI/runtime, not a promise that authentication is side-effect
free or always writes a particular file.

For synthetic actual-caller tests, fence both the pre-migration auth path and the
replacement before loading/executing the canonical script. The root Python
network guard is not subprocess isolation. The Netflix package's function-scoped
fixture blocks external execution/network/saved-auth fallbacks, injects fake
credentials/MockTransport, and restores patches after each test. See the package
guide for scope limits; no test is authority to reset real credentials.
