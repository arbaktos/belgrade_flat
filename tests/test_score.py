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


def test_price_does_not_affect_score():
    # User: anything ≤ €1100 is acceptable; price is a hard cap, not a ranking signal.
    cheap = _l(price_eur=400)
    pricey = _l(price_eur=1099)
    assert score.score(cheap, price_cap_eur=CAP, freshness_days=DAYS) == pytest.approx(
        score.score(pricey, price_cap_eur=CAP, freshness_days=DAYS), abs=1e-6
    )


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


def test_walking_outweighs_transit_when_both_short():
    # Same total commute (15 min) but one is all-walk, one is all-transit.
    # Walking should win because WALK_WEIGHT (0.45) > TRANSIT_WEIGHT (0.25).
    walker = _l(walk_min=15, transit_min=30)
    rider = _l(walk_min=30, transit_min=15)
    assert score.score(walker, price_cap_eur=CAP, freshness_days=DAYS) > score.score(rider, price_cap_eur=CAP, freshness_days=DAYS)


def test_short_walk_beats_short_transit_when_other_term_missing():
    # Pure walk credit (5 min walk, no transit data) vs pure transit credit
    # (no walk data, 5 min transit). Walking weighted double.
    walk_only = _l(walk_min=5, transit_min=None)
    transit_only = _l(walk_min=None, transit_min=5)
    assert score.score(walk_only, price_cap_eur=CAP, freshness_days=DAYS) > score.score(transit_only, price_cap_eur=CAP, freshness_days=DAYS)


def test_transit_breaks_tie_when_walk_is_equal():
    # Same walk_min, different transit_min — shorter transit wins.
    a = _l(id="a", walk_min=22, transit_min=10)
    b = _l(id="b", walk_min=22, transit_min=25)
    assert score.score(a, price_cap_eur=CAP, freshness_days=DAYS) > score.score(b, price_cap_eur=CAP, freshness_days=DAYS)


def test_elevator_present_scores_higher_than_missing():
    # Same everything else; only elevator differs. Lift earns +0.10.
    with_lift = _l(elevator=True)
    no_lift = _l(elevator=False)
    unknown = _l(elevator=None)
    s_lift = score.score(with_lift, price_cap_eur=CAP, freshness_days=DAYS)
    s_no = score.score(no_lift, price_cap_eur=CAP, freshness_days=DAYS)
    s_unknown = score.score(unknown, price_cap_eur=CAP, freshness_days=DAYS)
    assert s_lift > s_no
    assert s_lift > s_unknown
    # Unknown and explicit "no" are treated the same (no bonus).
    assert s_no == pytest.approx(s_unknown, abs=1e-6)


def test_walk_credit_extends_to_new_cap():
    # 35-min walk must earn some credit now that WALK_CAP_MIN = 40.
    walks_35 = _l(walk_min=35, transit_min=None)
    walks_45 = _l(walk_min=45, transit_min=None)
    s_35 = score.score(walks_35, price_cap_eur=CAP, freshness_days=DAYS)
    s_45 = score.score(walks_45, price_cap_eur=CAP, freshness_days=DAYS)
    assert s_35 > s_45  # closer walk wins
    # 45 min is past the cap → walk term zero.
    walks_60 = _l(walk_min=60, transit_min=None)
    assert s_45 == pytest.approx(score.score(walks_60, price_cap_eur=CAP, freshness_days=DAYS), abs=1e-6)


def test_rank_descending_orders_correctly():
    a = _l(id="a", price_eur=500, walk_min=15)
    b = _l(id="b", price_eur=900, walk_min=28)
    c = _l(id="c", price_eur=700, walk_min=20)
    ranked = score.rank_descending([b, a, c], price_cap_eur=CAP, freshness_days=DAYS)
    assert [l.id for l in ranked] == ["a", "c", "b"]


def test_pet_friendly_always_outranks_non_pet_friendly():
    # Even a terrible commute + bad floor — if pets are explicitly allowed,
    # the listing tops anything that doesn't explicitly allow pets.
    pet_bad = _l(id="pet_bad", walk_min=39, transit_min=29, pets_allowed=True)
    nonpet_great = _l(id="nonpet_great", walk_min=5, transit_min=5, pets_allowed=None)
    ranked = score.rank_descending([nonpet_great, pet_bad], price_cap_eur=CAP, freshness_days=DAYS)
    assert [l.id for l in ranked] == ["pet_bad", "nonpet_great"]


def test_pet_friendly_tier_sorts_by_score_within():
    # Within the pet-friendly tier the composite score still tiebreaks.
    pet_fast = _l(id="pet_fast", walk_min=10, pets_allowed=True)
    pet_slow = _l(id="pet_slow", walk_min=30, pets_allowed=True)
    ranked = score.rank_descending([pet_slow, pet_fast], price_cap_eur=CAP, freshness_days=DAYS)
    assert [l.id for l in ranked] == ["pet_fast", "pet_slow"]


def test_llm_pet_yes_also_qualifies_for_pet_tier():
    # When structured pets_allowed is None/False but the LLM read "yes" from
    # the description text, the listing still earns the pet-tier.
    from src.models import Extraction
    e = Extraction(pets_allowed="yes")
    pet_llm = _l(id="pet_llm", walk_min=25, pets_allowed=None, extraction=e)
    nonpet = _l(id="nonpet", walk_min=10, pets_allowed=None)
    ranked = score.rank_descending([nonpet, pet_llm], price_cap_eur=CAP, freshness_days=DAYS)
    assert [l.id for l in ranked] == ["pet_llm", "nonpet"]
