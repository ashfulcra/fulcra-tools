---
name: sealed-secrets
description: "Use when an agent or role needs credentials on a machine that does not have them — a fresh container, a rebuilt host, a successor session — or when sealing, rotating, or verifying secrets kept in a shared object store."
homepage: "https://github.com/ashfulcra/fulcra-tools"
license: "MIT"
user-invocable: true
metadata: { "openclaw": { "emoji": "🔐" } }
---

# Sealed Secrets

**A successor session on a fresh machine must be able to recover the role's
credentials — and must never be told a revoked one is fine.**

Agents lose their machines. Containers get reclaimed, hosts are rebuilt from
snapshots, a session resumes tomorrow on hardware that has never seen your
config. Everything else an agent needs can live in a shared object store. Its
credentials cannot — not in the clear, because the store is readable by every
agent on the team and by whoever operates it.

So seal them. The store carries ciphertext; the operator carries one passphrase;
the successor runs one command and learns, per secret, whether it **actually
works**.

> **A decrypt that succeeds is not a credential that authenticates.**

That sentence is the whole design. A bundle that decrypts perfectly to a revoked
token passes every cryptographic check and still leaves the successor dead.

## Where to start — the re-entrancy probes

Run in order; enter at the **first probe that fails**. Every state below is safe
to re-enter: sealing is a new bundle version, unlocking is read-only, and
verifying makes no writes at all.

| Probe (run in order) | Command | Passes when | If it fails, enter at |
|---|---|---|---|
| 1. Role has a bundle pointer | read the role's charter frontmatter | a `sealed_bundle:` key is present and non-empty | **§1 Seal** |
| 2. Bundle object exists | read the path that key names | the read returns an object (not absent, not an error) | **§1 Seal** — the pointer is dangling |
| 3. Envelope parses | parse it as JSON | it has `schema`, `wraps` (non-empty list), `payload` | **§1 Seal** — re-seal; do not hand-edit |
| 4. You can open it | run the unlock command | it prints a per-secret table, no crypto errors | **§2 Unlock** |
| 5. The secrets WORK | read that same table | every secret reads `VERIFIED` | **§3 When verify fails** |

**Probe 5 is the only one that matters to the caller.** Probes 1-4 can all pass
on a bundle full of revoked credentials. Never report "unlocked" as success;
report the verify table.

There is deliberately **no probe for "is a bundle listed in the store"** — see
the discovery rule in §1.

## 1. Seal

### Envelope shape

    {
      "schema": "sealed-bundle/v1",
      "bundle_id": "<opaque>",          // NOT derived from role or service name
      "role": "<role>",
      "version": 3,
      "alg": "ChaCha20-Poly1305",       // payload AND wrap AEAD
      "wraps": [                        // N independent unlock paths
        {"kid": "op-passphrase-2026-08",
         "kdf": {"name": "scrypt", "n": 131072, "r": 8, "p": 1,
                 "maxmem": 135266304, "salt": "<b64>"},
         "wrapped_dek": "<b64>", "nonce": "<b64>"}
      ],
      "payload": {"nonce": "<b64>", "ct": "<b64>"}
    }

Everything below is a wire decision. Two implementations that both "follow the
prose" will not interoperate unless these are pinned, so pin them:

| Field | Decision |
|---|---|
| `alg` | `ChaCha20-Poly1305`, 32-byte key, **12-byte** nonce, **16-byte tag appended** to the ciphertext. The same algorithm for payload and wraps. An unrecognised `alg` is a hard failure, never a fallback. |
| base64 | RFC 4648 §4 standard alphabet, **with** `=` padding. Not URL-safe, not unpadded. |
| `version` | integer, monotonic, starts at 1 |
| `bundle_id` | 16 random bytes, base64 — carries no role or service name |
| `salt` | 32 random bytes per wrap, never shared between wraps |
| payload plaintext | the JSON object in **Sealed payload** below, UTF-8, before encryption |

### Canonical AAD — length-prefixed, never concatenated

`schema || bundle_id || role || version` as raw concatenation is
**boundary-ambiguous**: `role="a", version=12` and `role="a1", version=2` produce
identical bytes, so a bundle can be made to authenticate under an identity that
is not its own. Length-prefix every field:

```python
def aad(schema: str, bundle_id: str, role: str, version: int) -> bytes:
    """Unambiguous AAD: each field as 4-byte big-endian length + UTF-8 bytes,
    in this fixed order. Any change to this function is a schema change."""
    parts = [schema.encode(), bundle_id.encode(), role.encode(),
             str(version).encode()]
    return b"".join(len(p).to_bytes(4, "big") + p for p in parts)
```

The **same** AAD authenticates the payload and every wrap.

### Sealed payload

The plaintext inside `payload.ct`, so verifier config never sits in the clear:

    {
      "secrets": [
        {"name": "api_token",
         "value": "<the credential>",
         "verify": {"kind": "http", "method": "GET",
                    "url": "https://api.example.com/v1/self",
                    "expect_status": 200, "auth": "bearer"}}
      ]
    }

### Reference flow

Runnable end to end; it is the interop test. If your implementation can open a
bundle this produces, it is compatible.

```python
import base64, json, os, hashlib
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

b64  = lambda b: base64.b64encode(b).decode()
# validate=True: silently discarding non-alphabet bytes turns a corrupted or
# doctored field into a shorter valid-looking one instead of an error.
ub64 = lambda s: base64.b64decode(s, validate=True)

def seal(role, version, payload_obj, passphrase):
    bundle_id = b64(os.urandom(16))
    A = aad("sealed-bundle/v1", bundle_id, role, version)
    dek = ChaCha20Poly1305.generate_key()          # ONE message per DEK, ever
    pn  = os.urandom(12)
    ct  = ChaCha20Poly1305(dek).encrypt(
              pn, json.dumps(payload_obj).encode(), A)
    salt = os.urandom(32)
    kek  = derive_kek(passphrase, salt)
    wn   = os.urandom(12)
    wdek = ChaCha20Poly1305(kek).encrypt(wn, dek, A)
    return {"schema": "sealed-bundle/v1", "bundle_id": bundle_id, "role": role,
            "version": version, "alg": "ChaCha20-Poly1305",
            "wraps": [{"kid": "op-passphrase",
                       "kdf": {"name": "scrypt", "n": N, "r": R, "p": P,
                               "maxmem": MAXMEM, "salt": b64(salt)},
                       "wrapped_dek": b64(wdek), "nonce": b64(wn)}],
            "payload": {"nonce": b64(pn), "ct": b64(ct)}}

class WrongPassphrase(Exception):
    """This wrap is not yours. The ONLY error that means "try the next wrap"."""

class BundleRejected(Exception):
    """The bundle is not the one the caller asked for, or is malformed."""

# Bounds on KDF parameters read from an ATTACKER-INFLUENCED document. n=2**22
# would demand 4 GiB and hang the host; n=2 would cost nothing to brute-force.
KDF_MIN_N, KDF_MAX_N, KDF_MAX_R, KDF_MAX_P = 2**14, 2**20, 32, 4

def _check_kdf(k):
    if k.get("name") != "scrypt":
        raise BundleRejected(f"unsupported kdf {k.get('name')!r}")
    n, r, p = k.get("n"), k.get("r"), k.get("p")
    if not all(isinstance(v, int) for v in (n, r, p)):
        raise BundleRejected("kdf parameters must be integers")
    if not (KDF_MIN_N <= n <= KDF_MAX_N) or n & (n - 1):
        raise BundleRejected(f"kdf n={n} out of bounds or not a power of two")
    if not (1 <= r <= KDF_MAX_R) or not (1 <= p <= KDF_MAX_P):
        raise BundleRejected(f"kdf r={r}/p={p} out of bounds")
    return n, r, p

def unseal(bundle, passphrase, *, expected_role, expected_version=None,
           expected_schema="sealed-bundle/v1"):
    """Open a bundle the caller has NAMED. `expected_role` is not optional.

    A bundle is a file someone else can point you at. AAD binding stops a role
    field from being EDITED inside a bundle — every field in the AAD comes from
    the same bundle, so a self-consistent one always authenticates itself. It
    does NOT stop SUBSTITUTION: repoint role B's charter at role A's untouched
    bundle and, with the right passphrase, it opens perfectly and hands A's
    credentials to B's flow. The identity check has to come from the CALLER.
    """
    if bundle.get("schema") != expected_schema:
        raise BundleRejected(f"schema {bundle.get('schema')!r} != {expected_schema!r}")
    if bundle.get("alg") != "ChaCha20-Poly1305":
        raise BundleRejected(f"unsupported alg {bundle.get('alg')!r}")  # never fall back
    # BEFORE any KDF work or decryption: is this even the bundle we asked for?
    if bundle.get("role") != expected_role:
        raise BundleRejected(
            f"bundle is for role {bundle.get('role')!r}, caller expected "
            f"{expected_role!r} — refusing to open somebody else's secrets")
    if expected_version is not None and bundle.get("version") != expected_version:
        raise BundleRejected(
            f"bundle version {bundle.get('version')!r} != {expected_version!r}")

    A = aad(bundle["schema"], bundle["bundle_id"],
            bundle["role"], bundle["version"])
    tried = 0
    for w in bundle["wraps"]:                  # try each unlock path
        n, r, p = _check_kdf(w["kdf"])         # malformed => BundleRejected, not skipped
        try:
            kek = hashlib.scrypt(passphrase, salt=ub64(w["kdf"]["salt"]),
                                 n=n, r=r, p=p, dklen=32,
                                 maxmem=128 * n * r + (1 << 20))
        except ValueError as e:
            # "memory limit exceeded" is a HOST problem. Swallowing it as a bad
            # passphrase is the failure this document warns about two sections
            # down; do not let the code do what the prose forbids.
            raise BundleRejected(f"cannot run this bundle's KDF here: {e}") from e
        try:
            dek = ChaCha20Poly1305(kek).decrypt(
                      ub64(w["nonce"]), ub64(w["wrapped_dek"]), A)
        except Exception:
            tried += 1
            continue                            # wrong passphrase for THIS wrap
        pt = ChaCha20Poly1305(dek).decrypt(
                 ub64(bundle["payload"]["nonce"]),
                 ub64(bundle["payload"]["ct"]), A)
        return json.loads(pt)
    # AEAD cannot tell a wrong key from a tampered ciphertext — both are just
    # "tag did not verify". Say all three possibilities rather than the one that
    # sounds most likely, or a tampered bundle reads as a typo forever.
    raise WrongPassphrase(
        f"no wrap opened ({tried} tried) — wrong passphrase, none of these "
        "unlock paths are yours, or the bundle has been altered since sealing")
```

**Four properties worth testing, because every one of them is silent when
broken.** A bundle whose `role` or `version` is *edited* must fail to open (AAD
binding). Re-sealing the same payload must produce a *different* DEK and nonce
(the one-message rule). A bundle for a *different* role, entirely untouched and
sealed with the same passphrase, must be **rejected before any decryption** —
that is pointer substitution, and it is the one the AAD cannot catch for you.
And a host that cannot afford the KDF must raise something distinguishable from
a wrong passphrase, or a caller retypes forever against a memory error.

A random **data key (DEK)** encrypts the payload. Each entry in `wraps`
encrypts that DEK to one **key-encrypting key (KEK)**. `wraps` is a list from
day one even when it holds one entry: adding a second operator, or a hardware
key later, must not require re-encrypting the payload, and revoking one unlock
path must not disturb the others.

### One message per DEK — impossible by construction

AEAD nonces must never repeat under the same key. In a fleet, the writers are
uncoordinated agents and the store usually offers no compare-and-swap, so there
is no counter they can safely share.

Do not solve this with discipline. Solve it structurally:

> **A DEK encrypts exactly one message, ever. Any change to the payload mints a
> new DEK and a new bundle version.**

Nonce reuse then cannot happen, rather than being unlikely. A 96-bit random
nonce (ChaCha20-Poly1305, AES-GCM) is safe under that rule with no bookkeeping.

### Bind the ciphertext to its identity

Authenticate over `schema || bundle_id || role || version` as associated data,
in **both** the payload and every wrap.

Without this, anyone who can write to the store can copy another role's bundle
under a path they control and induce the unlock flow to open it for them. The
cryptography would be flawless and the authorization entirely absent.

### Key derivation: an operator passphrase, and not only a device

Derive the KEK from an operator passphrase with a memory-hard KDF — scrypt
(`n=2^17, r=8, p=1`, 32-byte random salt per wrap) if you want a standard-library
dependency footprint, Argon2id if you can afford the library.

**Those parameters cost ~128 MiB of RAM, and most stdlib bindings refuse them by
default.** scrypt needs `128 · n · r` bytes = 134,217,728 (128 MiB) at
`n=2^17, r=8`, which is above the default memory ceiling OpenSSL applies. Python's
`hashlib.scrypt` raises `ValueError: memory limit exceeded` unless you pass
`maxmem` explicitly, so the parameters must always travel with their memory
budget:

```python
import hashlib

N, R, P, DKLEN = 2**17, 8, 1, 32
# scrypt's working set is 128*N*R bytes; the implementation needs a little more
# on top. Measured minimum on CPython 3.11 / OpenSSL 3: 128*N*R + 3072 bytes.
# 1 MiB of headroom is the smallest margin that is obviously safe and still
# states its own reasoning.
MAXMEM = 128 * N * R + (1 << 20)          # ~129 MiB

def derive_kek(passphrase: bytes, salt: bytes) -> bytes:
    """32-byte KEK. Raises ValueError if the host will not grant the memory."""
    return hashlib.scrypt(passphrase, salt=salt, n=N, r=R, p=P,
                          dklen=DKLEN, maxmem=MAXMEM)
```

**The passphrase is bytes, and which bytes must not depend on the host.** A
passphrase typed on one machine and retyped on another can differ while looking
identical: composed vs decomposed accents, a non-breaking space, a trailing
newline from a here-doc. scrypt hashes bytes, so any of those is simply a
different passphrase, and the failure it produces is indistinguishable from a
typo. Pin it once, at the edge, and never re-normalize deeper in:

```python
import unicodedata

def passphrase_bytes(text: str) -> bytes:
    """NFKC, stripped of surrounding whitespace, encoded UTF-8. One definition,
    applied at seal AND unseal, or the two derive different KEKs."""
    pw = unicodedata.normalize("NFKC", text).strip()
    if len(pw) < 20:
        # This bundle is readable by everyone the store is readable by, so its
        # only real defence is the cost of guessing this string. A memory-hard
        # KDF buys orders of magnitude; it does not rescue a weak passphrase.
        raise ValueError("passphrase too short for an offline-readable bundle "
                         "— use a generated phrase of 20+ characters")
    return pw.encode("utf-8")
```

Two failure modes to handle rather than discover:

- **`ValueError: memory limit exceeded`** means the binding's ceiling is below
  `MAXMEM`, not that the passphrase is wrong. Say which it is; a caller who reads
  this as a bad passphrase will retype it forever.
- **A memory-constrained host** (small container, tight cgroup) may genuinely be
  unable to spare 128 MiB. Lower `n` deliberately and record the parameters *in
  the wrap* — they are already there in `kdf` — so bundles remain openable. Never
  lower them silently: the wrap's stored parameters are what a successor derives
  with, and a bundle sealed at one cost must be openable at that same cost.

The numbers are not arbitrary: `n=2^17` is the work factor, `r=8` sets the block
size scrypt was specified around, and `p=1` keeps it single-threaded so the cost
is memory rather than cores. Raise `n` to raise the cost; every doubling doubles
both time and memory.

**A device-bound key must never be the only wrap.** It is a fine *addition* — a
second entry in `wraps` that skips the prompt on a stable workstation. But hosts
that rebuild themselves each wake keep nothing device-bound, and those are
exactly the hosts a portable-secrets design exists for. A device-only scheme is
a secrets design for the machines that were already fine.

### Where the ciphertext lives — and the discovery rule

Put bundles in a prefix **nothing walks**, and make the role's own definition
carry a pointer:

    # the role's charter
    sealed_bundle: <secrets-prefix>/<role>/<bundle-id>.json

Two independent reasons, and the second is binding:

1. **Cost.** Role directories tend to be listed on hot paths — every routing
   fold, every wake, for every agent. Credential blobs do not belong in a
   listing something walks on a schedule.
2. **The pointer is the ONLY discovery path.** Nothing may ever *list* the
   secrets prefix. A listing is itself a disclosure: it enumerates which roles
   hold credentials and how many, to anyone who can read the store. If your
   engine ever grows a reader for that prefix, add a guard test that fails the
   build.

"Travels with the role" means **discoverable from the role**, not stored beside it.

### What goes outside the seal — almost nothing

Only `schema`, `bundle_id`, `role`, `version`, and the wrap metadata. In
particular, **verifier configuration lives INSIDE the payload.**

If verifier config sits in the clear so the unlock flow knows what to check,
then anyone who can read the store — or who steals a checkpoint carrying the
pointer — learns that this role holds a token for service X, a key for host Y,
and a credential for Z. That is not a credential leak; it is a map of which
credentials exist and where. Filenames are opaque ids for the same reason: never
`github-pat.json`.

## 2. Unlock

One operator action, on resume, and it is a prompt:

    $ <your-tool> secrets unlock <role>
    passphrase: ‹not echoed›
      api_token       VERIFIED   (identity endpoint → 200)
      deploy_key      FAILED     (auth refused) — rotate before relying on it
      metrics_key     UNKNOWN    (no network) — not proven; treat as unavailable
    1 of 3 secrets proven working.

Non-negotiables, each one a way this leaks in practice:

- **Never in argv.** Prompt on a TTY; offer `--passphrase-fd N` for automation.
  A passphrase in `argv` is in the process table and the shell history.
- **Never echo it**, and never include a secret in an error message. A failed
  verify prints the service's status code, not the credential.
- **Plaintext never goes back to the store**, into a log, a note, a checkpoint,
  or a bug report. It lives in process memory or a `0600` file that dies with
  the session.
- **Do not cache plaintext across sessions.** On a host that rebuilds each wake
  you could not anyway; making it universal keeps the model honest.

## 3. Verify — the part that earns the design

Every secret carries its own verifier, **inside the sealed payload**:

    {"name": "api_token",
     "verify": {"kind": "http", "method": "GET",
                "url": "https://api.example.com/v1/self",
                "expect_status": 200, "auth": "bearer"}}

Three outcomes, and the third is the discipline:

| Outcome | Meaning | What the caller does |
|---|---|---|
| **VERIFIED** | the verifier ran; the service accepted the credential | rely on it |
| **FAILED** | the verifier ran; the service rejected it | rotate it — the successor knows *before* depending on it |
| **UNKNOWN** | the verifier could not run: no network, unknown `kind`, timeout, DNS failure | treat as unavailable |

> **UNKNOWN is never PASS.**

A verify step that reports success when it could not reach the service is worse
than no verify step, because it converts "I don't know" into "you're fine" at
precisely the moment someone is deciding whether to trust a credential. If every
verifier returns UNKNOWN, the run reports *"0 of N proven"* — not "unlocked".

**Never print `decrypted OK` as a success line.** Decryption is a precondition,
not a result. It is the exact claim that a revoked token satisfies.

Verification rules:

- Send the credential to **the real service**, over TLS. That is the point; a
  local format check proves nothing about revocation.
- Use the service's **own identity endpoint** — never a third-party echo service,
  which would hand your credential to an unrelated party.
- Log the **outcome only**. Never the request with its credential attached.

## 4. Rotate and revoke

- Rotation writes a **new bundle version with a new DEK**, then advances the
  charter pointer. Never edit a bundle in place: it breaks the one-message-per-DEK
  rule that makes the nonce safe.
- **A retained old bundle cannot be revoked by rotating forward.** Say this
  plainly, because the tempting claim is false: minting a new DEK and a new
  passphrase protects only the *new* version. The old object still carries its
  own wrap, derived from the old passphrase, over its own ciphertext — anyone
  holding the exposed passphrase decrypts it for as long as the object exists.
  New keys do not reach backwards.

  So when a passphrase is exposed, the only real remedies are:

  1. **Hard-delete the old bundles.** If your store cannot guarantee deletion
     (soft deletes, snapshots, replicas), you do not have this remedy.
  2. **Wrap to an external revocable key** — a KMS key or hardware token you can
     destroy — so revoking the wrapping key orphans every bundle that used it.
     This is the only mechanism that revokes ciphertext you cannot delete.
  3. **Rotate the credentials themselves** at each service, and treat every
     secret in every retained bundle as compromised from the moment of exposure.

  (3) is the one that always works and the one you must not skip. The plaintext
  is only worth what the credential is still worth.
- Concurrent rotations can clobber each other on a store with no conditional
  write. Use a monotonic `version`, write the new object first, and advance the
  pointer only after that write lands.

## What this does NOT protect against

State it plainly rather than letting a reader assume more:

- **A compromised machine at unlock time.** The passphrase and the plaintext are
  both there by construction.
- **A compromised session after unlock.** Once a process holds a live
  credential, it can use it. The mitigations are scope and rotation, not crypto.
- **Traffic analysis.** The store operator sees object sizes and write times. A
  bundle rewritten right after an incident is a visible signal.

## Adapting this

Everything above is storage-agnostic: it needs an object store with read, write,
and per-path addressing. Substitute your own store, your own role-definition
format, and your own CLI verbs. The parts that are not negotiable, because each
one is a failure someone has already had:

1. One message per DEK.
2. AAD binds the ciphertext to its identity.
3. A passphrase wrap always exists; a device wrap is never the only one.
4. Nothing lists the secrets prefix; the role pointer is the only discovery path.
5. Verifier config lives inside the seal.
6. UNKNOWN is never PASS, and `decrypted OK` is never a success line.
