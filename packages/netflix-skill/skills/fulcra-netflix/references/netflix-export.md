# Obtain a Netflix viewing-history export

Provider instructions checked against Netflix's help pages on 2026-10-24. Those
pages can change; an exported file's actual headers and parse result determine
what this importer supports. Do not infer complete account coverage from a
successful parse or upload.

## Profile viewing activity (slim CSV)

Netflix's [viewing-history instructions](https://help.netflix.com/en/node/101917)
say to open Account in a browser, choose Profiles and the intended profile, then
Viewing activity. Show More expands a limited on-screen list; Download all at the
bottom exports a CSV. The help page does not establish that CSV coverage equals
only the rows already expanded on screen. Do not require endless Show More
clicks or certify coverage from the page alone.

Save the CSV locally and run local check-only before import. The supported slim
format has `Title,Date` with `M/D/YY` dates. It has no real time-of-day, duration,
device or profile identity. Each parsed observation retains a synthetic noon-UTC
start and one-second interval, low confidence and original date/occurrence
provenance. Within a file, repeated identical raw date/title pairs receive separate
occurrence IDs. Title/fingerprint ambiguities remain the established parser's
behavior, not a new claim to identify every film correctly.

A slim file supplies no profile key. Identical raw date/title/occurrence tuples
from different profiles/files can therefore share identity. Partial overlapping
exports cannot prove which same-day rewatch an occurrence represents. Do not
promise independent profile histories or arbitrary overlap reconciliation; use a
source with profile/time identity when that distinction matters.

## Personal-information request (rich CSV)

Netflix's [personal-information request instructions](https://help.netflix.com/en/node/100624)
link to [getmyinfo](https://www.netflix.com/account/getmyinfo) and say producing the
information can take up to 30 days after verification. Follow the actual request,
verification and download-email instructions, including any stated expiry; this
guide does not promise a typical delivery time or fixed link lifetime.

The existing supported rich layout is `CONTENT_INTERACTION/ViewingActivity.csv`,
with headers including Profile Name, Start Time, Duration, Title, Supplemental
Video Type and Device Type. Inspect the downloaded archive/headers; a different
layout requires parser support rather than guessed column mapping. Rich parser
records retain real UTC times/durations, profile/device provenance and high
confidence. Existing supplemental trailer/promotional filtering remains.
Review which profiles are represented before importing a household export.
Neither the help page nor a CSV parse proves the complete historical account
lifetime was supplied.

## Upgrade and identity

Both variants use the same import invocation; headers select the parser. Rich and
slim source namespaces intentionally differ, so importing rich after slim retains
both sets instead of replacing the low-confidence originals. Fingerprints are
provenance useful to consumers, not authorization to collapse observations or
skip readback. See [record-schema.md](record-schema.md) for exact identities,
fields and version boundaries.
