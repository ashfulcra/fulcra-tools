"""The proof must be run by a job whose ACTUAL `runs-on` is a macOS runner, with no `if:` that can
skip the step. *(r15, codex-reviewer round 14: `"macos" in job` matched a comment; and this test was
run by nothing.)* It runs from the always-on uv-workspace job and from Task 0 (which materializes the
workflow), so deleting the proof workflow turns a required gate red. Honest status: a green run of
the proof workflow in CI has NOT yet been observed; the G29 gate is the PASSED record from a host."""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[3]
WORKFLOWS = ROOT / ".github" / "workflows"


def _jobs(text):
    """Split a workflow into its jobs (2-space-indented keys under `jobs:`), comments stripped."""
    body = re.sub(r"(?m)^\s*#.*$", "", text)
    m = re.search(r"(?m)^jobs:\s*$", body)
    if not m:
        return []
    jobs_text = body[m.end():]
    return [j for j in re.split(r"(?m)^  (?=[A-Za-z_][A-Za-z0-9_-]*:\s*$)", jobs_text) if j.strip()]


def test_a_macos_job_runs_the_proof_unconditionally():
    found = []
    for wf in sorted(WORKFLOWS.glob("*.yml")):
        for job in _jobs(wf.read_text()):
            if "tests/proof/run_proof.py" not in job:
                continue
            runs_on = re.search(r"(?m)^\s+runs-on:\s*(\S+)", job)
            step_if = re.search(r"(?m)^\s+if:", job)
            found.append((wf.name, runs_on.group(1) if runs_on else None, bool(step_if)))
    ok = [f for f in found if f[1] and f[1].startswith("macos") and not f[2]]
    assert ok, f"no job with runs-on: macos-* runs the proof unconditionally; found {found}"
