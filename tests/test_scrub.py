"""Tier 1 param-strip: drop auth/tracking params from query+fragment."""
from __future__ import annotations

import pytest

from fulcra_attention.scrub import scrub_url

# (input, expected) table. Keep alphabetized within each section.
CASES = [
    # ---- Auth-bearing ----
    ("https://x.com/p?access_token=abc",     "https://x.com/p"),
    ("https://x.com/p?code=ABC&state=xyz",   "https://x.com/p"),
    ("https://x.com/p?apikey=k",             "https://x.com/p"),
    ("https://x.com/p?api_key=k",            "https://x.com/p"),
    ("https://x.com/p?key=k",                "https://x.com/p"),
    ("https://x.com/p?token=t",              "https://x.com/p"),
    ("https://x.com/p?authorization=a",      "https://x.com/p"),
    ("https://x.com/p?id_token=jwt",         "https://x.com/p"),
    ("https://x.com/p?refresh_token=r",      "https://x.com/p"),
    ("https://x.com/p?nonce=n",              "https://x.com/p"),
    ("https://x.com/p?client_secret=cs",     "https://x.com/p"),
    ("https://x.com/p?assertion=a",          "https://x.com/p"),
    ("https://x.com/p?session=s",            "https://x.com/p"),
    ("https://x.com/p?sid=s",                "https://x.com/p"),
    ("https://x.com/p?sessionid=s",          "https://x.com/p"),
    ("https://x.com/p?auth=a",               "https://x.com/p"),
    ("https://x.com/p?signature=s",          "https://x.com/p"),
    ("https://x.com/p?sig=s",                "https://x.com/p"),
    ("https://x.com/p?hmac=h",               "https://x.com/p"),
    ("https://x.com/p?password=p",           "https://x.com/p"),
    ("https://x.com/p?pwd=p",                "https://x.com/p"),
    ("https://x.com/p?otp=123",              "https://x.com/p"),
    ("https://x.com/p?magic=m",              "https://x.com/p"),
    ("https://x.com/p?share_token=s",        "https://x.com/p"),
    ("https://x.com/p?invite=i",             "https://x.com/p"),
    ("https://x.com/p?confirmation_token=c", "https://x.com/p"),
    ("https://x.com/p?_csrf=c",              "https://x.com/p"),
    ("https://x.com/p?csrf_token=c",         "https://x.com/p"),
    ("https://x.com/p?xsrf=x",               "https://x.com/p"),
    ("https://x.com/p?ticket=t",             "https://x.com/p"),
    ("https://x.com/p?ott=o",                "https://x.com/p"),
    # ---- AWS signed URLs ----
    ("https://s3.aws.com/bucket/k?X-Amz-Signature=abc&X-Amz-Credential=c&X-Amz-Security-Token=t&Expires=123",
     "https://s3.aws.com/bucket/k"),
    # ---- Tracking ----
    ("https://x.com/p?utm_source=newsletter", "https://x.com/p"),
    ("https://x.com/p?utm_medium=email&utm_campaign=launch", "https://x.com/p"),
    ("https://x.com/p?gclid=g",               "https://x.com/p"),
    ("https://x.com/p?fbclid=f",              "https://x.com/p"),
    ("https://x.com/p?msclkid=m",             "https://x.com/p"),
    ("https://x.com/p?mc_eid=e&mc_cid=c",     "https://x.com/p"),
    ("https://x.com/p?_hsenc=h&_hsmi=h",      "https://x.com/p"),
    ("https://x.com/p?igshid=i",              "https://x.com/p"),
    ("https://x.com/p?yclid=y",               "https://x.com/p"),
    # ---- One-click action ----
    ("https://x.com/p?unsubscribe=u",         "https://x.com/p"),
    ("https://x.com/p?verify=v",              "https://x.com/p"),
    ("https://x.com/p?reset=r",               "https://x.com/p"),
    ("https://x.com/p?confirm=c",             "https://x.com/p"),
    ("https://x.com/p?activate=a",            "https://x.com/p"),
    # ---- Case-insensitivity ----
    ("https://x.com/p?ACCESS_TOKEN=a",        "https://x.com/p"),
    ("https://x.com/p?Code=c",                "https://x.com/p"),
    # ---- Legit params preserved ----
    ("https://x.com/p?q=foo&page=2",          "https://x.com/p?q=foo&page=2"),
    ("https://x.com/p?id=123",                "https://x.com/p?id=123"),
    # ---- Mixed: some stripped, some preserved ----
    ("https://x.com/p?id=1&access_token=t&page=2", "https://x.com/p?id=1&page=2"),
    # ---- Fragment dropped by default ----
    ("https://x.com/p#section1",              "https://x.com/p"),
    ("https://x.com/p?id=1#access_token=t",   "https://x.com/p?id=1"),
    # ---- No path ----
    ("https://x.com/?utm_source=x",           "https://x.com/"),
    # ---- No query, no fragment ----
    ("https://x.com/p",                       "https://x.com/p"),
]


@pytest.mark.parametrize("raw,expected", CASES, ids=[c[0] for c in CASES])
def test_scrub_url(raw: str, expected: str):
    assert scrub_url(raw) == expected
