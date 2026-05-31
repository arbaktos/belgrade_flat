from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src import score
from src.destinations import Destination
from src.models import Listing


OFFICE = Destination(name="office", lat=44.806, lng=20.460, gates=True, score_weight=0.45)
SADIK = Destination(name="Sadik Enter", lat=44.807, lng=20.464, gates=False, score_weight=0.25)
DESTS = [OFFICE, SADIK]
DAYS = 7


def _l(*, office=20, sadik=25, **over) -> Listing:
    commute = over.pop("commute", None)
    if commute is None:
        commute = {}
        if office is not None:
            commute["office"] = office
        if sadik is not None:
            commute["Sadik Enter"] = sadik
    base = dict(
        id="x", source="4zida", url="https://x", price_eur=900, m2=60, rooms=2.0,
        floor=3, total_floors=5, last_floor=False, elevator=True,
        furnished="yes", heating_type="district", pets_allowed=True,
        title="t", description="d", address=None, place_names=[], image_url=None,
        is_agency=False,
        created_at=datetime.now(timezone.utc) - timedelta(hours=1),
        commute=commute,
    )
    base.update(over)
    return Listing(**base)


def _s(l):
    return score.score(l, destinations=DESTS, freshness_days=DAYS)


def test_score_in_unit_range():
    assert 0.0 <= _s(_l()) <= 1.0


def test_price_does_not_affect_score():
    assert _s(_l(price_eur=400)) == pytest.approx(_s(_l(price_eur=1099)), abs=1e-6)


def test_closer_office_scores_higher():
    fast = _l(office=10, sadik=10)
    slow = _l(office=38, sadik=38)
    assert _s(fast) > _s(slow)


def test_office_weight_dominates_sadik():
    # Improving the office walk (0.45) helps more than the same improvement to
    # Sadik (0.25). near_office beats near_sadik when the totals are mirrored.
    near_office = _l(office=10, sadik=38)
    near_sadik = _l(office=38, sadik=10)
    assert _s(near_office) > _s(near_sadik)


def test_sadik_distance_contributes_to_score():
    # Office identical; closer Sadik must score higher (0.25 weight is live).
    close_sadik = _l(office=20, sadik=8)
    far_sadik = _l(office=20, sadik=39)
    assert _s(close_sadik) > _s(far_sadik)


def test_bigger_scores_higher():
    assert _s(_l(m2=78)) > _s(_l(m2=56))


def test_fresher_scores_higher():
    fresh = _l(created_at=datetime.now(timezone.utc))
    old = _l(created_at=datetime.now(timezone.utc) - timedelta(days=6, hours=23))
    assert _s(fresh) > _s(old)


def test_missing_office_yields_zero_office_term():
    # No office walk → office term drops, but Sadik + other terms keep score > 0.
    no_office = _l(office=None, sadik=10)
    assert 0 < _s(no_office) < 1


def test_office_only_when_sadik_missing():
    # Sadik not computed at all; office still drives the score.
    a = _l(office=10, sadik=None)
    b = _l(office=35, sadik=None)
    assert _s(a) > _s(b)


def test_walk_credit_extends_to_cap_40():
    s_35 = _s(_l(office=35, sadik=None))
    s_45 = _s(_l(office=45, sadik=None))
    assert s_35 > s_45                      # closer walk wins
    s_60 = _s(_l(office=60, sadik=None))
    assert s_45 == pytest.approx(s_60, abs=1e-6)   # both past the 40 cap → 0


def test_elevator_present_scores_higher_than_missing():
    s_lift = _s(_l(elevator=True))
    s_no = _s(_l(elevator=False))
    s_unknown = _s(_l(elevator=None))
    assert s_lift > s_no
    assert s_lift > s_unknown
    assert s_no == pytest.approx(s_unknown, abs=1e-6)


def test_rank_descending_orders_correctly():
    a = _l(id="a", office=15)
    b = _l(id="b", office=38)
    c = _l(id="c", office=22)
    ranked = score.rank_descending([b, a, c], destinations=DESTS, freshness_days=DAYS)
    assert [l.id for l in ranked] == ["a", "c", "b"]


def test_pet_friendly_always_outranks_non_pet_friendly():
    pet_bad = _l(id="pet_bad", office=39, sadik=39, pets_allowed=True)
    nonpet_great = _l(id="nonpet_great", office=5, sadik=5, pets_allowed=None)
    ranked = score.rank_descending([nonpet_great, pet_bad], destinations=DESTS, freshness_days=DAYS)
    assert [l.id for l in ranked] == ["pet_bad", "nonpet_great"]


def test_pet_friendly_tier_sorts_by_score_within():
    pet_fast = _l(id="pet_fast", office=10, pets_allowed=True)
    pet_slow = _l(id="pet_slow", office=35, pets_allowed=True)
    ranked = score.rank_descending([pet_slow, pet_fast], destinations=DESTS, freshness_days=DAYS)
    assert [l.id for l in ranked] == ["pet_fast", "pet_slow"]


def test_llm_pet_yes_also_qualifies_for_pet_tier():
    from src.models import Extraction
    e = Extraction(pets_allowed="yes")
    pet_llm = _l(id="pet_llm", office=25, pets_allowed=None, extraction=e)
    nonpet = _l(id="nonpet", office=10, pets_allowed=None)
    ranked = score.rank_descending([nonpet, pet_llm], destinations=DESTS, freshness_days=DAYS)
    assert [l.id for l in ranked] == ["pet_llm", "nonpet"]
