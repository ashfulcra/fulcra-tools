#!/usr/bin/env bash
# The anti-slop patterns, run against the Python.
#
# dmmulroy/anti-slop is an Oxlint plugin, so upstream it only reaches
# TypeScript. Ash's point (2026-09-04) is that its value is the PATTERNS,
# not the language: reject fabricated type evidence, refuse to launder a
# known value through a dynamic type, parse at the boundary instead of
# narrowing ad hoc, and never swallow the evidence of a failure. Each
# selected rule below is the Python enforcement of one of those.
# docs/anti-slop-patterns-in-python.md holds the rule-by-rule mapping.
#
# DIAGNOSTIC, not a gate. It is not wired into CI on purpose -- see the
# comment in pyproject.toml. Exit status is ruff's, so a caller that wants
# a gate can have one; nothing in this repo currently does.
set -uo pipefail
cd "$(dirname "$0")/.."

RUFF="${RUFF:-.venv/bin/ruff}"
[ -x "$RUFF" ] || RUFF="ruff"

# ANN401 any-type          <- no-unknown-parameters, no-unknown-returns,
#                             no-unsafe-dictionary-type, no-known-value-widening
# BLE001 blind-except      <- the evidence-destroying half of no-runtime-typeof
# S110   try-except-pass   <- same family: a swallowed failure is not a handled one
# S112   try-except-continue
# SIM105 suppressible-exception
# TRY300 try-consider-else <- keeps the success path out of the guarded block
# TRY301 raise-within-try
RULES="ANN401,BLE001,S110,S112,SIM105,TRY300,TRY301"

TARGETS=("$@")
if [ ${#TARGETS[@]} -eq 0 ]; then
  TARGETS=(packages)
fi

exec "$RUFF" check --select "$RULES" "${TARGETS[@]}"
