from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class CommitmentBid:
    max_charge_price_per_mwh: float
    min_discharge_price_per_mwh: float
    max_charge_mw: Optional[float] = None
    max_discharge_mw: Optional[float] = None
    reserve_holdback_fraction: float = 0.0
    reserve_offer_price_per_mw_h: float = 5.0
    # Dynamic response is a separate, day-ahead availability product. The
    # auction award is fixed before the energy/reserve unit-commitment solve and
    # therefore enters the physical model as hard inverter/SOC headroom.
    dynamic_response_award_mw: float = 0.0

    def validate(self, battery_power_mw: float) -> None:
        if self.max_charge_price_per_mwh >= self.min_discharge_price_per_mwh:
            raise ValueError("Charge bid must be below discharge offer")
        if not 0 <= self.reserve_holdback_fraction <= 0.95:
            raise ValueError("reserve_holdback_fraction must be between 0 and 0.95")
        if self.reserve_offer_price_per_mw_h < 0:
            raise ValueError("reserve_offer_price_per_mw_h must be non-negative")
        if not 0 <= self.dynamic_response_award_mw <= battery_power_mw:
            raise ValueError("dynamic_response_award_mw must be between 0 and battery power")
        for name, value in (("max_charge_mw", self.max_charge_mw), ("max_discharge_mw", self.max_discharge_mw)):
            if value is not None and not 0 <= value <= battery_power_mw:
                raise ValueError(f"{name} must be between 0 and battery power")
