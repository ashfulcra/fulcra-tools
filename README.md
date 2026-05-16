# FulcraMediaHelpers

Import your media consumption (Watched, Listened) into Fulcra as annotations.

See `docs/superpowers/specs/2026-05-16-fulcra-media-helpers-design.md` for the design.

## Install

    pip install -e ".[dev]"

## Bootstrap (once)

    fulcra auth login           # via the underlying fulcra-api CLI
    fulcra-media bootstrap      # create the Watched/Listened annotation definitions

## Import Netflix

    fulcra-media wizard netflix           # interactive walkthrough
    # or, if you already have a CSV in hand:
    fulcra-media import netflix takeouts/NetflixViewingHistory.csv
    # or from your Fulcra Library:
    fulcra-media import netflix fulcra:/takeouts/NetflixViewingHistory.csv
