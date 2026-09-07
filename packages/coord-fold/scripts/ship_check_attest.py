"""The attestation CHILD program ship_check runs under the gate interpreter with -I -S -B: verifies the installed
coord_engine bytes against the pinned tree, serves them from memory through a verified importer, folds the review
status in that same process, and reports. Kept as a source string because it is executed via `python -c`, never
imported. Split out of ship_check.py (coord-fold-ship-24c1e518, both reviewers P1). Loaded by ship_check through
an explicit file-location spec beside itself; the gate's clean-tree check covers this file exactly as it covers
ship_check.py."""

ATTEST = r"""
import sys, json, io, contextlib, os, glob, hashlib, importlib.abc, importlib.machinery
def refuse(why):
    print(json.dumps({"refused": why})); sys.exit(2)
def verify_tree(site, expected):
    # r26 (codex-reviewer, round 23): read every file ONCE, hash the bytes we KEEP, and execute those bytes.
    # Hashing a pathname and letting the importer reopen it later is a TOCTOU window; this closes it.
    pkg = os.path.join(site, "coord_engine")
    present = {os.path.relpath(os.path.join(dp, f), pkg) for dp, _, fs in os.walk(pkg) for f in fs if "__pycache__" not in dp}
    sourceless = sorted(f for f in present if f.endswith((".pyc", ".pyo", ".pyd", ".so")))
    if sourceless:
        refuse(f"compiled/sourceless files under coord_engine/ could answer: {sourceless[:3]}")
    missing = sorted(set(expected) - present)
    if missing:
        refuse(f"files in the pinned commit's tree are missing from the installed package: {missing[:3]}")
    extra = sorted(present - set(expected))
    if extra:
        refuse(f"files under coord_engine/ that the pinned commit's tree does not contain: {extra[:3]}")
    blobs = {}
    for rel, want in sorted(expected.items()):
        data = open(os.path.join(pkg, rel), "rb").read()
        got = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
        if got != want:
            refuse(f"installed file does not match the pinned commit's blob: {rel}")
        blobs[rel] = data
    return pkg, blobs
class VerifiedResources:
    # importlib.resources reader over the VERIFIED BYTES (r27): package data such as default_models.json is
    # served from memory too, never re-read from disk. resource_path is refused: there is no trusted path.
    def __init__(self, blobs, prefix):
        self.blobs, self.prefix = blobs, prefix
    def open_resource(self, name):
        rel = self.prefix + name
        if rel not in self.blobs:
            raise FileNotFoundError(rel)
        return io.BytesIO(self.blobs[rel])
    def resource_path(self, name):
        raise FileNotFoundError(name)
    def is_resource(self, name):
        return (self.prefix + name) in self.blobs
    def contents(self):
        return [r[len(self.prefix):] for r in self.blobs if r.startswith(self.prefix) and "/" not in r[len(self.prefix):]]
class VerifiedImporter(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    # Serves coord_engine, every submodule AND every package resource from the VERIFIED BYTES. The filesystem
    # is never reopened for package code or data. A coord_engine name outside the verified tree is an
    # ImportError, never a fallback to the path importer.
    def __init__(self, root, blobs):
        self.root, self.blobs, self.loaded = root, blobs, {}
    def get_resource_reader(self, fullname):
        parts = fullname.split(".")[1:]
        return VerifiedResources(self.blobs, "/".join(parts) + "/" if parts else "")
    def find_spec(self, fullname, path=None, target=None):
        parts = fullname.split(".")
        if parts[0] != "coord_engine":
            return None
        sub = "/".join(parts[1:])
        for rel, is_pkg in (((sub + "/" if sub else "") + "__init__.py", True), (sub + ".py", False)):
            if rel in self.blobs:
                origin = os.path.join(self.root, rel)
                spec = importlib.machinery.ModuleSpec(fullname, self, origin=origin, is_package=is_pkg)
                spec.has_location = True
                if is_pkg:
                    spec.submodule_search_locations = [os.path.dirname(origin)]
                return spec
        raise ImportError(f"{fullname} is not in the verified tree")
    def create_module(self, spec):
        return None
    def exec_module(self, module):
        rel = os.path.relpath(module.__spec__.origin, self.root)
        self.loaded[module.__name__] = rel
        exec(compile(self.blobs[rel], module.__spec__.origin, "exec", dont_inherit=True), module.__dict__)
def install_verified_importer(pkg, blobs):
    for n in list(sys.modules):
        if n == "coord_engine" or n.startswith("coord_engine."):
            refuse(f"coord_engine was already imported before the verified importer was installed: {n}")
    imp = VerifiedImporter(pkg, blobs)
    sys.meta_path.insert(0, imp)
    return imp
def run_status(team, slug):
    import coord_engine
    from coord_engine import cli
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(["review", "status", team, slug, "--json"])
    lines = [l for l in buf.getvalue().splitlines() if l.startswith("{")]
    return rc, (json.loads(lines[-1]) if lines else None), os.path.realpath(coord_engine.__file__)
def rogue_modules(imp, site):
    # r27 (codex-coder, round 24): EVERY loaded module is checked, not only coord_engine names. A module whose
    # file lives under the tool environment and was not served by the verified importer executed unverified code.
    root = os.path.realpath(site) + os.sep
    out = []
    for n, m in list(sys.modules.items()):
        served = getattr(m, "__loader__", None) is imp
        if n == "coord_engine" or n.startswith("coord_engine."):
            if not served:
                out.append(n)
            continue
        f = getattr(m, "__file__", None)
        if f and os.path.realpath(f).startswith(root):
            out.append(n)
    return sorted(out)
def paths_under(site):
    root = os.path.realpath(site) + os.sep
    return [p for p in sys.path if os.path.realpath(p or os.getcwd()).startswith(root) or os.path.realpath(p or os.getcwd()) == root[:-1]]
def canonical_tree_digest(tree):
    return hashlib.sha256(json.dumps(tree, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
def main():
    site, team, slug = sys.argv[1:4]
    dis = sorted(glob.glob(os.path.join(site, "coord_engine-*.dist-info")))
    if len(dis) != 1:
        refuse(f"{len(dis)} coord_engine dist-infos under site-packages; exactly one is required")
    # r28 (codex-reviewer, round 25): the expected tree arrives on STDIN — the parent-child pipe — never via a
    # pathname the child would have to trust. The child echoes a canonical digest of exactly what it received and
    # the parent compares it to its own; a same-count substitution has nowhere to happen.
    try:
        expected = json.loads(sys.stdin.read())
    except ValueError:
        refuse("the expected tree on stdin is not JSON")
    if not isinstance(expected, dict) or not expected or not all(isinstance(k, str) and isinstance(v, str) and len(v) == 40 for k, v in expected.items()):
        refuse("the expected tree on stdin is not a non-empty {relpath: blob-sha1} object")
    tree_digest = canonical_tree_digest(expected)
    pkg, blobs = verify_tree(site, expected)
    if sys.pycache_prefix is None or os.listdir(sys.pycache_prefix):
        refuse("bytecode is not redirected to a fresh empty pycache_prefix; stale __pycache__ could answer")
    try:
        du = json.load(open(os.path.join(dis[0], "direct_url.json")))
    except (OSError, ValueError):
        du = {}
    if paths_under(site):
        refuse(f"the tool environment is on sys.path before attestation: {paths_under(site)[:2]}")
    imp = install_verified_importer(pkg, blobs)
    # r27 (codex-coder, round 24): the tool environment's site-packages is NEVER placed on sys.path. r26 inserted it
    # "for metadata lookups" and thereby let a forged top-level argparse.py in that directory answer for the whole
    # attestation. direct_url.json is read by path above; nothing else from that directory is needed.
    rc, status, file = run_status(team, slug)
    rogue = rogue_modules(imp, site)
    if rogue:
        refuse(f"modules were loaded from the tool environment outside the verified importer: {rogue[:3]}")
    if paths_under(site):
        refuse(f"the tool environment appeared on sys.path during attestation: {paths_under(site)[:2]}")
    print(json.dumps({"file": file, "reported_commit": du.get("vcs_info", {}).get("commit_id"),
                      "tree_verified": len(blobs), "dist_info": os.path.basename(dis[0]),
                      "loader": "verified-bytes", "memory_loaded": len(imp.loaded), "tree_digest": tree_digest,
                      "rc": rc, "status": status}))
    sys.exit(rc)                      # the outer process carries the inner verdict's rc too; both are checked
if __name__ == "__main__":
    main()
"""
