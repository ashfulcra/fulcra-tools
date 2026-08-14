"""The data-source catalogue must not silently omit a shipped plugin.

`docs/how-do-i-get-my-data.md` opens by claiming it "lists every data source
Fulcra Collect can pull from today". Nothing enforced that claim, so it drifted:
`gmail` and `purpleair` shipped and were never added, the Apple TV section never
mentioned the on-device `apple-tv` reader, and the Apple Music section
affirmatively stated no Apple Music plugin existed while `apple-music-takeout`
was in the registry.

That is the same failure shape as the frozen-bundle manifest (PR #455): several
hand-maintained lists of the same thing, with nothing comparing them. The fix
there was to derive every list from one source; the fix here is to derive the
CHECK from the registry, so a new plugin fails this test until it is documented.

Deliberately a coverage check, not a content check. It asserts the source is
mentioned at all — it cannot know whether the prose is accurate, and pretending
otherwise would be a worse lie than the gap it replaces.
"""
from __future__ import annotations

import re
from pathlib import Path

from fulcra_collect import registry

_REPO = Path(__file__).resolve().parents[3]
CATALOGUE = _REPO / "docs" / "how-do-i-get-my-data.md"

#: Plugins deliberately absent from the user-facing catalogue, each with the
#: reason. Keep this SHORT — an entry here is a promise that a user looking for
#: the source would not expect to find it, not a place to silence the test.
_NOT_USER_FACING = {
    # Not a source: the receiving half of the browser extension, documented in
    # the "Browser activity" section under the extension's own name.
    "attention-relay": "internal receiver for the Attention extension",
}


def _documented(text: str, plugin_id: str) -> bool:
    """The catalogue cites plugins by id in backticks. Match the id as a whole
    token so `apple-tv` is not satisfied by `apple-tv-something`."""
    return re.search(rf"(?<![\w-]){re.escape(plugin_id)}(?![\w-])", text) is not None


def test_every_registered_plugin_appears_in_the_data_source_catalogue():
    text = CATALOGUE.read_text()

    # Positive control: the check must be capable of failing. A regex that
    # matched nothing would make this test pass vacuously for every plugin —
    # which is exactly the "green because it never looked" failure the
    # catalogue itself just had.
    assert _documented(text, "lastfm"), (
        "sanity check failed: the matcher cannot find a plugin known to be "
        "documented, so a passing result here would prove nothing"
    )

    registered = set(registry.discover().plugins)
    expected = registered - set(_NOT_USER_FACING)
    missing = sorted(p for p in expected if not _documented(text, p))

    assert not missing, (
        f"{CATALOGUE.relative_to(_REPO)} claims to list every data source but "
        f"does not mention: {', '.join(missing)}. Add a section (or a pathway "
        f"to an existing section) — or, if the plugin genuinely is not a "
        f"user-facing source, add it to _NOT_USER_FACING with a reason."
    )


def test_the_exemption_list_only_names_plugins_that_exist():
    """A stale exemption silently un-guards a plugin that was renamed or
    removed — the exemption outlives the reason for it."""
    registered = set(registry.discover().plugins)
    stale = sorted(set(_NOT_USER_FACING) - registered)
    assert not stale, f"_NOT_USER_FACING names plugins that are not registered: {stale}"
