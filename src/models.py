from __future__ import annotations

from dataclasses import dataclass, asdict, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class WinterSmog:
    """Winter smog exposure from the offline Belgrade grid (annotate only)."""
    band: str                    # better | moderate | worse
    pm25_winter_mean: float
    pm25_best: float             # winter P10 — cleaner spells in this cell
    pm25_worse: float            # winter P90 — bad inversion days in this cell
    smog_warning: bool           # True when cell pm25_worse is in city worst third
    motorway_m: float | None
    score: float
    cell_lat: float
    cell_lng: float


@dataclass
class Extraction:
    """LLM-derived fields per spec §5. None = LLM hasn't seen this listing yet."""
    pets_allowed: str | None = None              # "yes" | "no" | "unknown"
    dishwasher: bool | None = None
    elevator_confirmed: bool | None = None
    heating_type_confirmed: str | None = None    # "centralno" | "etazno" | "podno" | "TA" | "klima" | "..."
    furnishing_confirmed: str | None = None      # "furnished" | "semi-furnished" | "unfurnished" | None
    max_lease_months: int | None = None
    bills_estimate_eur: int | None = None
    agency_or_owner: str | None = None           # "agency" | "owner" | "unknown"
    red_flags: list[str] = field(default_factory=list)
    summary_en: str | None = None
    description_en: str | None = None            # full English translation of the listing body


@dataclass
class Listing:
    id: str                 # source-scoped id (e.g. 4zida's ObjectId)
    source: str             # "4zida" | "nekretnine" | "halooglasi" | "cityexpert"
    url: str
    price_eur: float
    m2: float
    rooms: float
    floor: int | None
    total_floors: int | None
    last_floor: bool
    elevator: bool | None
    furnished: str | None        # raw value from source: "yes"/"no"/"semi"/etc.
    heating_type: str | None     # raw value from source (4zida 'district', nekretnine 'Centralno', etc.)
    pets_allowed: bool | None
    title: str
    description: str
    address: str | None
    place_names: list[str]
    image_url: str | None
    is_agency: bool
    created_at: datetime
    dishwasher: bool | None = None       # only some sources expose this structurally; None = ask the LLM
    lat: float | None = None
    lng: float | None = None
    # Walking minutes to each named destination, keyed by destination name
    # (e.g. {"office": 18, "Sadik Enter": 31}). Populated by route.py; a None
    # value means "Google found no walking route / pre-filtered as too far".
    commute: dict[str, int | None] = field(default_factory=dict)
    image_phash: str | None = None       # populated by dedup.compute_phashes (hex string)
    extraction: Extraction | None = None
    winter_smog: WinterSmog | None = None   # from data/belgrade_winter_smog.json + geocode

    @property
    def fingerprint_key(self) -> str:
        """Stable per-source id for dedup within a single portal."""
        return f"{self.source}:{self.id}"

    def to_row(self) -> dict[str, Any]:
        d = asdict(self)
        d["created_at"] = self.created_at.isoformat()
        d["place_names"] = ",".join(self.place_names)
        d["fingerprint_key"] = self.fingerprint_key
        # extraction + commute live in their own columns / are computed at runtime;
        # strip them from the row we send to the listings table.
        for k in ("extraction", "winter_smog", "commute"):
            d.pop(k, None)
        return d
