from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src import score
from src.models import Listing


def _l(**over) -> Listing:
    base = dict(
        id="x", source="4zida", url="https://x", price_eur=900, m2=60, rooms=2.0,
        floor=3, total_floors=5, last_floor=False, elevator=True,
        furnished="yes", heating_type="district", pets_allowed=True,
        title="t", description="d", address=None, place_names=[], image_url=None,
        is_agency=False,
        created_at=datetime.now(timezone.utc) - timedelta(hours=1),
        walk_min=20, transit_min=15,
    )
    base.update(over)
    return Listing(**base)


CAP = 1000
DAYS = 7


def test_score_in_unit_range():
    s = score.score(_l(), price_cap_eur=CAP, freshness_days=DAYS)
    assert 0.0 <= s <= 1.0


def test_cheaper_scores_higher():
    cheap = _l(price_eur=400)
    pricey = _l(price_eur=900)
    assert score.score(cheap, price_cap_eur=CAP, freshness_days=DAYS) > score.score(pricey, price_cap_eur=CAP, freshness_days=DAYS)


def test_faster_commute_scores_higher():
    fast = _l(walk_min=10, transit_min=8)
    slow = _l(walk_min=28, transit_min=29)
    assert score.score(fast, price_cap_eur=CAP, freshness_days=DAYS) > score.score(slow, price_cap_eur=CAP, freshness_days=DAYS)


def test_bigger_scores_higher():
    big = _l(m2=78)
    small = _l(m2=56)
    assert score.score(big, price_cap_eur=CAP, freshness_days=DAYS) > score.score(small, price_cap_eur=CAP, freshness_days=DAYS)


def test_fresher_scores_higher():
    fresh = _l(created_at=datetime.now(timezone.utc))
    old = _l(created_at=datetime.now(timezone.utc) - timedelta(days=6, hours=23))
    assert score.score(fresh, price_cap_eur=CAP, freshness_days=DAYS) > score.score(old, price_cap_eur=CAP, freshness_days=DAYS)


def test_no_commute_yields_zero_commute_term():
    no_route = _l(walk_min=None, transit_min=None)
    s = score.score(no_route, price_cap_eur=CAP, freshness_days=DAYS)
    # missing commute drops the commute term; should still produce some score from other terms
    assert 0 < s < 1


def test_rank_descending_orders_correctly():
    a = _l(id="a", price_eur=500, walk_min=15)
    b = _l(id="b", price_eur=900, walk_min=28)
    c = _l(id="c", price_eur=700, walk_min=20)
    ranked = score.rank_descending([b, a, c], price_cap_eur=CAP, freshness_days=DAYS)
    assert [l.id for l in ranked] == ["a", "c", "b"]
