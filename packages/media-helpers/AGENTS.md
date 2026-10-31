# Media agent guide

Read the packaged [fulcra-media skill](skills/fulcra-media/SKILL.md) for the
import roster, JSON envelope and user-authorized scheduling contract.

Read the [migration boundary](README.md#v1-migration-boundary-development-branch)
and [standalone authority contract](README.md#standalone-v1-adapter-development-branch)
before changing writers. Collect media plugins and all fourteen standalone import routes listed in
the package guide use guarded v1 Events on this source branch. The standalone
webhook server now shares that authority; the released app has not switched. Helper tests alone do not
establish caller migration or rollout.

Bootstrap writes profile-local `cli-media-v1.json` atomically. All retained legacy
category selections require explicit Event/UUID mappings. Validate saved account
before recovering original pending work; recover before migrated source resolution, discovery or parsing.
Preserve evidence on holds. Status is cached read-only inspection, UNKNOWN is not
all clear, and preview never drains pending work. Reset is retired for every flag
combination. CLI credentials remain separate from Collect authentication.

The five [standalone poll commands](README.md#standalone-v1-polling-development-branch)
now select strict scans. The same five daemon routes use their
[separate v2 scope and progress contract](README.md#daemon-v1-polling-development-branch),
with no annotation/pre-strict cursor adoption. Keep scan completion, actual
current delivery and cursor decisions together; recovered pending data is not
current-source progress. [Immutable observed progress](README.md#observed-import-progress-development-branch)
now survives later errors and all fourteen CLI routes preserve known counts
after cleanup. Public `cursor` is a closed state/reason or null; successful
withheld delivery does not prove caught up. Shared last-invocation snapshots are not
caller-bound receipts.

The [retired unbound writer contract](README.md#retired-legacy-media-writers-development-branch)
keeps construction/read/admin/tag compatibility but refuses the four old record
seams before auth, iteration, empty/cached success or preview. Preserve the captured
Collect bridge, native importer-namespace helper and pure conversion APIs. Do not
restore annotation creation, wrapped/typed pipeline or orphan `run_and_emit` as a
fallback; all fourteen imports and the standalone server have guarded v1 routes.

The [standalone Trakt contract](README.md#standalone-trakt-policy-development-branch)
uses invocation-only choices and immutable exact offered pairs intersected with
fresh shared proof. Prompt inspection is read-only; existing hints need owned
readback/exclusions, and excluded current-input highs cannot justify skips.
Explicit ask fails before source in JSON/non-TTY; omitted twin policy keeps and
warns only for candidates there. Post-cluster totals include loose-policy skips;
Trakt has no cursor. Keep shared writer `None` config/default precedence intact.

The [standalone webhook contract](README.md#standalone-webhook-development-branch)
uses `fulcra-media webhook --port 8766 --json` (no `serve` subcommand), preserving
default 8765 and explicit port selection without fallback. Recovery and all-category
preparation precede bind; private owner-thread authority and profile leases last
through cleanup. Handler jobs get their own settled outcomes, never shared last
progress. Stop admission, drain accepted jobs, close the runtime on its opening
thread and join before successful shutdown. Late parsers cannot regain authority.
Health is cached liveness, not ownership proof; ready follows the real-loop health
handshake. Keep loopback/nonloopback bearer rules and actual lifecycle tests.
