from __future__ import annotations

from dataclasses import dataclass, asdict, field
from datetime import datetime
from typing import Any


@dataclass
class Extraction:
    """LLM-derived fields per spec §5. None = LLM hasn't seen this listing yet."""
    pets_allowed: str | None = None              # "yes" | "no" | "unknown"
    dishwasher: bool | None = None
    elevator_confirmed: bool | None = None
    heating_type_confirmed: str | None = None    # "centralno" | "etazno" | "podno" | "TA" | "klima" | "..."
    max_lease_months: int | None = None
    bills_estimate_eur: int | None = None
    agency_or_owner: str | None = None           # "agency" | "owner" | "unknown"
    red_flags: list[str] = field(default_factory=list)
    summary_en: str | None = None


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
    extraction: Extraction | None = None

    @property
    def fingerprint_key(self) -> str:
        """Stable per-source id for dedup within a single portal."""
        return f"{self.source}:{self.id}"

    def to_row(self) -> dict[str, Any]:
        d = asdict(self)
        d["created_at"] = self.created_at.isoformat()
        d["place_names"] = ",".join(self.place_names)
        d["fingerprint_key"] = self.fingerprint_key
        # extraction is stored separately; strip from the listings row
        d.pop("extraction", None)
        return d
