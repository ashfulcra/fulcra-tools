#!/bin/sh
# Scan tracked files for team-internal identifiers that must never enter this
# repo: public host addresses, live session refs, and index sections that tell a
# public reader to skip past team history.
#
# Extracted from the workflow into a script for one reason: the workflow also
# has to PROVE this scan can fail (`--self-test`), and a self-test that checks a
# copy of the rules is a self-test of the copy. One implementation, two callers.
#
# Exit 0 = clean, 1 = violation(s) found.

set -eu

# NO \b ANYWHERE IN THIS FILE. `git grep -E` is POSIX ERE, which has no
# word-boundary escape: it does not error on `\b`, it simply never matches, so a
# scan written with it returns empty and reads CLEAN while being structurally
# incapable of finding anything. That is exactly how the first version of this
# guard shipped — the IP arm, written after a live host address sat in a branch
# for two hours, could never have caught that address. Use explicit
# non-digit/non-dot context instead, and keep `--self-test` in front of every
# run so an incapable scan is loud rather than green.
IP_RE='(^|[^0-9.])([0-9]{1,3}[.]){3}[0-9]{1,3}([^0-9.]|$)'

# Loopback, RFC1918, link-local and the TEST-NET ranges are legitimate
# documentation (50+ files use 127.0.0.1 today); a guard that fails 50 files on
# day one gets disabled on day one, and a disabled guard is worse than none
# because everyone believes it runs.
IP_ALLOW='(^|[^0-9])(127\.|10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.|169\.254\.|192\.0\.2\.|198\.51\.100\.|203\.0\.113\.|0\.0\.0\.0|255\.|npm/[0-9]|/v?[0-9]+\.[0-9]+\.[0-9]+)'

scan() {
  found=0
  # The pragma is same-line only, so an annotation cannot silently bless a whole
  # file; each use is visible at the use site and in review.
  ips=$(git grep -InE "$IP_RE" -- . \
    | grep -vE "$IP_ALLOW" \
    | grep -vE '(^|[^0-9])0\.[0-9]+\.[0-9]+\.[0-9]+' \
    | grep -v 'guard-ok: public-ip' || true)
  if [ -n "$ips" ]; then
    echo "::error::public IP address in tracked files — host addresses live on the team store, never here"
    echo "$ips"
    found=1
  fi
  # Session refs bind a document to one team's live infrastructure.
  refs=$(git grep -InE 'session_[A-Za-z0-9]{20,}' -- . || true)
  if [ -n "$refs" ]; then
    echo "::error::session ref in tracked files"
    echo "$refs"
    found=1
  fi
  # An index that tells a public reader to skip a section is a section in the
  # wrong repo (the docs/README "safe to skip" lesson).
  skip=$(git grep -Iin 'safe to skip on a first read' -- docs/ || true)
  if [ -n "$skip" ]; then
    echo "::error::'safe to skip' index section — team history belongs on the team store"
    echo "$skip"
    found=1
  fi
  return $found
}

self_test() {
  # Prove the scan can FAIL before trusting that it passed. A guard's green is
  # only evidence if red is reachable, and this one's red was not: the workflow
  # ran on every PR for as long as it existed and could not have flagged the
  # class of leak it was written for.
  #
  # The fixture is staged into the index because the scan uses `git grep`, which
  # only sees tracked files — testing it any other way would exercise a
  # different code path than the real run. The CI checkout is disposable; the
  # trap restores state anyway so a local run is safe too.
  probe='.no-team-internals-selftest.md'
  trap 'git rm -q -f --cached "$probe" >/dev/null 2>&1 || true; rm -f "$probe"' EXIT INT TERM
  # A public unicast address (TEST-NET-2 is allow-listed above, so this uses a
  # neighbouring address that is deliberately NOT in any allow range) and a
  # session-ref shape. Both must be caught.
  printf 'host: 198.51.101.7\nref: session_%s\n' \
    'AAAAAAAAAAAAAAAAAAAAAAAA' > "$probe"
  git add -f "$probe" >/dev/null

  out=$(scan 2>&1) && rc=0 || rc=1
  # Unstage BEFORE evaluating, so the fixture can never leak into the real scan
  # that follows in the default mode. (It did on the first run of this script:
  # the EXIT trap fires at the end of the PROCESS, and `scan` runs in between.)
  git rm -q -f --cached "$probe" >/dev/null 2>&1 || true
  rm -f "$probe"
  trap - EXIT INT TERM
  if [ "$rc" -eq 0 ]; then
    echo "::error::SELF-TEST FAILED — the scan reported CLEAN on a file containing"
    echo "::error::a public IP and a session ref. Every green run of this guard is"
    echo "::error::meaningless until this is fixed. (Classic cause: a POSIX ERE"
    echo "::error::pattern written with \\b, which never matches under git grep.)"
    return 1
  fi
  echo "$out" | grep -q 'public IP address' || {
    echo "::error::SELF-TEST FAILED — the IP arm did not flag a public address"; return 1; }
  echo "$out" | grep -q 'session ref' || {
    echo "::error::SELF-TEST FAILED — the session-ref arm did not flag a ref"; return 1; }
  echo "self-test OK: the scan flags a planted public IP and session ref"
  return 0
}

case "${1:-}" in
  --self-test) self_test ;;
  "")          self_test && scan ;;
  *)           echo "usage: $0 [--self-test]" >&2; exit 2 ;;
esac
