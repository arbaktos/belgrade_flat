from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Any


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
    elevator: bool
    furnished: str | None        # raw value from source: "yes"/"no"/"semi"/etc.
    heating_type: str | None     # raw value from source
    pets_allowed: bool | None
    title: str
    description: str
    address: str | None
    place_names: list[str]
    image_url: str | None
    is_agency: bool
    created_at: datetime

    @property
    def fingerprint_key(self) -> str:
        """Stable per-source id for dedup within a single portal."""
        return f"{self.source}:{self.id}"

    def to_row(self) -> dict[str, Any]:
        d = asdict(self)
        d["created_at"] = self.created_at.isoformat()
        d["place_names"] = ",".join(self.place_names)
        d["fingerprint_key"] = self.fingerprint_key
        return d
