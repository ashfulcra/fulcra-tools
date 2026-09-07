"""ship_check's PRIVATE-STATE helpers: the gate-owned temp root, ACL inspection and removal, private directories,
and the owned-file read. Split out of ship_check.py (coord-fold-ship-24c1e518, both reviewers P1: the 400-line
ceiling now governs the scripts tree too). Pure OS-level helpers: nothing here executes a trust root or reads the
store, so ship_check imports this module and never the reverse. Loaded by ship_check through an explicit
file-location spec beside itself, which the gate's own clean-tree check (git status on packages/coord-fold at
the exact head) covers exactly as it covers ship_check.py."""
import os
import re
import subprocess
import sys


def gate_tmp_root():
    """The ONLY place the gate creates temporary state: ~/.local/state/coord-fold/tmp, created 0700 and verified
    (owned by this uid, no group/other bits) on every call. An inherited TMPDIR is never consulted (codex-coder round 29:
    tempfile.mkdtemp under an uncontrolled TMPDIR is a pathname handoff the gate did not own)."""
    import tempfile
    root = os.path.join(os.path.expanduser("~"), ".local", "state", "coord-fold", "tmp")
    if not os.path.isdir(root):
        os.makedirs(root, mode=0o700, exist_ok=True)
        os.chmod(root, 0o700)                       # umask-proof on creation only; an EXISTING root that lost its privacy is refused, never repaired
    st = os.stat(root)
    if st.st_uid != os.getuid() or (st.st_mode & 0o077):
        raise RuntimeError(f"gate temp root {root} is not a private directory of this user (uid {st.st_uid}, mode {oct(st.st_mode & 0o777)})")
    strip_acls(root)                                # r38: an inherited ACL on the root would survive the chmod above
    tempfile.tempdir = root
    return root


LS_LED_HEADER = re.compile(r"^[dl-][rwxsStT-]{9}[+@ ]?\s+\d+\s+\S+\s+\S+\s+\d+\s+\S.*$")
LS_LED_ENTRY = re.compile(r"^\s*\d+:\s\S.*$")


def parse_ls_led(path, out):
    """The ACL entries in `/bin/ls -led` output, accepting ONLY the shape ls prints: line 1 is the long-format
    header for `path` (mode, links, owner, group, size, date, name) and every later line is an ACL entry
    (` N: user:... allow/deny ...`). r40 (codex-reviewer, coord-fold-ship-24c1e518 P0): an rc-0 body of any other
    shape used to parse as `[]`, i.e. as PROOF that no ACL is present. An inspection whose shape the gate does not
    understand is a failed inspection, and a failed inspection refuses; it is never "no ACL"."""
    lines = [ln.rstrip("\r") for ln in out.splitlines() if ln.strip()]
    if not lines or not LS_LED_HEADER.match(lines[0]):
        raise PermissionError(f"ACL inspection of {path}: `ls -led` output is not the long-format header the gate expects: {lines[0][:80] if lines else '(empty)'!r}")
    entries = lines[1:]
    bad = [ln for ln in entries if not LS_LED_ENTRY.match(ln)]
    if bad:
        raise PermissionError(f"ACL inspection of {path}: unrecognised line in `ls -led` output: {bad[0][:80]!r}")
    return [ln.strip() for ln in entries]


def acl_entries(path):
    """ACL entries on a path. r38 (codex-reviewer round 33): on macOS an ACL survives chmod and is INVISIBLE to stat, so a
    directory that reports 0700 can still grant everyone write/delete via an inherited entry. Listed through the OS's own
    /bin/ls (an OS trust root, like /bin/chmod below); on Linux, the POSIX-ACL xattr."""
    # r39 (both reviewers, round 34): an inspection that FAILS is not "no ACL". A failed ls / listxattr refuses.
    if sys.platform == "darwin":
        p = subprocess.run(["/bin/ls", "-led", path], capture_output=True, text=True)
        if p.returncode != 0 or not p.stdout.strip():
            raise PermissionError(f"ACL inspection of {path} failed (rc {p.returncode}): {p.stderr.strip()[:120]}")
        return parse_ls_led(path, p.stdout)
    try:
        return [x for x in os.listxattr(path) if x.startswith("system.posix_acl")]
    except OSError as exc:
        raise PermissionError(f"ACL inspection of {path} failed: {exc}") from exc


def strip_acls(path):
    """Remove every ACL entry (inherited ones included) from a path the gate just created, then PROVE none remain."""
    # r39 (both reviewers, round 34): a removal that FAILS refuses; it is never "stripped".
    if sys.platform == "darwin":
        p = subprocess.run(["/bin/chmod", "-N", path], capture_output=True, text=True)
        if p.returncode != 0:
            raise RuntimeError(f"ACL removal on {path} failed (rc {p.returncode}): {p.stderr.strip()[:120]}")
    else:
        for x in acl_entries(path):
            try:
                os.removexattr(path, x)
            except OSError as exc:
                raise RuntimeError(f"ACL removal on {path} failed: {exc}") from exc
    left = acl_entries(path)
    if left:
        raise RuntimeError(f"{path} still carries ACL entries after stripping: {left[:2]}")


def private_dir(prefix):
    """A fresh 0700 directory under the gate's own temp root, with no ACL entries (r38)."""
    import tempfile
    d = tempfile.mkdtemp(prefix=prefix, dir=gate_tmp_root())
    os.chmod(d, 0o700)
    strip_acls(d)
    return d


def read_owned_file(path):
    """Read a file the gate expects to own, refusing if the handoff state changed before the read: a symlink, a
    non-regular file, another owner, or a containing directory that is no longer private (r34)."""
    import stat as _stat
    d = os.path.dirname(path)
    ds = os.stat(d)
    if ds.st_uid != os.getuid() or (ds.st_mode & 0o077):
        raise PermissionError(f"the private directory {d} is no longer private (uid {ds.st_uid}, mode {oct(ds.st_mode & 0o777)})")
    if acl_entries(d):
        raise PermissionError(f"the private directory {d} carries ACL entries: {acl_entries(d)[:2]}")      # r38: invisible to stat
    ls = os.lstat(path)
    if _stat.S_ISLNK(ls.st_mode) or not _stat.S_ISREG(ls.st_mode):
        raise PermissionError(f"{path} is not a regular file the gate wrote")
    if ls.st_uid != os.getuid():
        raise PermissionError(f"{path} is owned by uid {ls.st_uid}, not this user")
    if acl_entries(path):
        raise PermissionError(f"{path} carries ACL entries: {acl_entries(path)[:2]}")                          # r38: an ACL can grant write past the mode
    if ls.st_mode & 0o022:                                  # r36 (codex-reviewer round 31): the BODY's own mode, not only the directory's.
        # The guarantee is INTEGRITY (nobody else could have modified the body between the CLI's write and this read),
        # so the group/other WRITE bits are what matter; a 0644 body — what any CLI writes under a normal umask — is fine.
        raise PermissionError(f"{path} is writable by others (mode {oct(ls.st_mode & 0o777)})")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, encoding="utf-8") as fh:
        return fh.read()
