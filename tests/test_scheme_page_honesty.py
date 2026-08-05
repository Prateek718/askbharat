"""The scheme page must never assert something about a source it has not read.

This is a regression test for a real defect. Atal Pension Yojana's page told
citizens "The official page does not state eligibility conditions" while the
government's page listed eligibility in detail — the extraction queue simply
had not reached it. An absent value and an unexamined page are the same shape
in the database and opposite claims to a reader.

Rendering goes through the real app so the assertion covers the template, not a
reimplementation of it.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from askbharat.web.app import app

# Any wording that asserts the *source* is silent. Only legitimate once we have
# actually extracted the page.
CLAIMS_ABOUT_SOURCE = ["does not state", "not stated"]


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


def _find(client, extracted: bool, has_page: bool | None = None) -> str | None:
    """A slug in a given state, or None if no scheme is in that state.

    `has_page` splits the un-extracted case in two, because they are different
    claims: a page we have archived but not yet read, versus a scheme whose
    page the source does not serve at all.
    """
    from sqlalchemy import text

    from askbharat.db.session import session_scope
    where = ["sr.id IS NOT NULL" if extracted else "sr.id IS NULL"]
    if has_page is not None:
        where.append("rd.id IS NOT NULL" if has_page else "rd.id IS NULL")
    with session_scope() as s:
        return s.execute(text(f"""
            SELECT c.slug FROM scheme_catalogue c
              LEFT JOIN service_records sr ON sr.id = 'myscheme:' || c.slug
              LEFT JOIN raw_documents rd
                     ON rd.meta->>'slug' = c.slug
                    AND rd.source_id = (SELECT id FROM sources WHERE slug='myscheme')
             WHERE {" AND ".join(where)}
             LIMIT 1
        """)).scalar_one_or_none()


def test_unextracted_page_never_claims_the_source_is_silent(client):
    slug = _find(client, extracted=False)
    if slug is None:
        pytest.skip("every scheme has been extracted")
    body = client.get(f"/scheme/{slug}").text
    for claim in CLAIMS_ABOUT_SOURCE:
        assert claim not in body, (
            f"un-extracted page {slug} asserts {claim!r} about a source we "
            "have not read"
        )


def test_archived_but_unread_page_says_so_and_links_out(client):
    """State (3): we hold the page, we just have not extracted it."""
    slug = _find(client, extracted=False, has_page=True)
    if slug is None:
        pytest.skip("no archived-but-unread pages left")
    body = client.get(f"/scheme/{slug}").text
    assert "not read yet" in body.lower()
    # and still points the citizen at the real thing
    assert "myscheme.gov.in" in body


def test_page_the_source_does_not_serve_never_promises_a_later_read(client):
    """State (4): myScheme lists the scheme but 404s on its own detail link.

    "Not read yet" here promises a state that will never arrive, and the link
    it offers as the way to find out is the very URL that 404s. The page has to
    say the source is unavailable and stop sending the reader there.
    """
    slug = _find(client, extracted=False, has_page=False)
    if slug is None:
        pytest.skip("every listed scheme has a harvested page")
    body = client.get(f"/scheme/{slug}").text
    low = body.lower()
    assert "unavailable" in low
    assert "not read yet" not in low, (
        f"{slug} has no source page to read, so 'yet' promises a later state "
        "that cannot arrive"
    )
    assert "myscheme.gov.in/schemes/" not in body, (
        f"{slug} offers the scheme URL that is known to 404"
    )


def test_extracted_page_may_state_the_source_is_silent(client):
    """The honest version of the claim stays available once we have looked."""
    slug = _find(client, extracted=True)
    if slug is None:
        pytest.skip("nothing extracted yet")
    r = client.get(f"/scheme/{slug}")
    assert r.status_code == 200
    assert "not read yet" not in r.text.lower()


def test_missing_scheme_is_a_404_not_a_crash(client):
    assert client.get("/scheme/definitely-not-a-real-slug").status_code == 404
