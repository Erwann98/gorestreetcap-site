from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional


@dataclass
class Node:
    name: str


@dataclass
class Line:
    name: str
    from_node: str
    to_node: str
    capacity_mw: float
    susceptance: float = 10.0


@dataclass
class Generator:
    name: str
    node: str
    technology: str
    capacity_mw: float
    marginal_cost_per_mwh: float
    offer_price_per_mwh: float
    min_output_mw: float = 0.0
    minimum_stable_mw: float = 0.0
    available_fraction: float = 1.0
    emissions_t_per_mwh: float = 0.0
    startup_cost: float = 0.0
    no_load_cost_per_hour: float = 0.0
    ramp_up_mw_per_hour: float = 1000000.0
    ramp_down_mw_per_hour: float = 1000000.0
    min_up_periods: int = 1
    min_down_periods: int = 1
    initially_on: bool = True
    initial_output_mw: Optional[float] = None
    reserve_offer_cost_per_mw_h: float = 4.0
    owner: str = "AI Genco"

    @property
    def available_capacity_mw(self) -> float:
        return max(0.0, self.capacity_mw * self.available_fraction)


@dataclass
class BatteryAsset:
    name: str
    node: str
    power_mw: float
    energy_mwh: float
    soc_mwh: float
    round_trip_efficiency: float = 0.88
    degradation_cost_per_mwh: float = 7.0
    min_soc_fraction: float = 0.05
    max_soc_fraction: float = 0.95
    cumulative_throughput_mwh: float = 0.0

    @property
    def eta_charge(self) -> float:
        return self.round_trip_efficiency ** 0.5

    @property
    def eta_discharge(self) -> float:
        return self.round_trip_efficiency ** 0.5

    @property
    def min_soc_mwh(self) -> float:
        return self.energy_mwh * self.min_soc_fraction

    @property
    def max_soc_mwh(self) -> float:
        return self.energy_mwh * self.max_soc_fraction

    def charge_limit_mw(self, duration_hours: float) -> float:
        room = max(0.0, self.max_soc_mwh - self.soc_mwh)
        energy_limited = room / (self.eta_charge * duration_hours) if duration_hours else 0.0
        return min(self.power_mw, energy_limited)

    def discharge_limit_mw(self, duration_hours: float) -> float:
        usable = max(0.0, self.soc_mwh - self.min_soc_mwh)
        energy_limited = usable * self.eta_discharge / duration_hours if duration_hours else 0.0
        return min(self.power_mw, energy_limited)

    def apply_dispatch(self, charge_mw: float, discharge_mw: float, duration_hours: float) -> None:
        if charge_mw > 1e-8 and discharge_mw > 1e-8:
            raise ValueError("Battery cannot charge and discharge simultaneously")
        self.soc_mwh += charge_mw * self.eta_charge * duration_hours
        self.soc_mwh -= discharge_mw / self.eta_discharge * duration_hours
        self.soc_mwh = min(self.max_soc_mwh, max(self.min_soc_mwh, self.soc_mwh))
        self.cumulative_throughput_mwh += (charge_mw + discharge_mw) * duration_hours


@dataclass
class BatteryBid:
    max_charge_price_per_mwh: float
    min_discharge_price_per_mwh: float
    max_charge_mw: Optional[float] = None
    max_discharge_mw: Optional[float] = None
    max_reserve_mw: float = 0.0
    reserve_offer_price_per_mw_h: float = 5.0
    # Fixed availability already sold in the separate dynamic-response auction.
    # It is not an energy-market decision variable, but it consumes symmetric
    # inverter headroom and must remain energy-deliverable.
    dynamic_response_mw: float = 0.0

    def validate(self) -> None:
        if self.max_charge_price_per_mwh >= self.min_discharge_price_per_mwh:
            raise ValueError(
                "Charge bid must be below discharge offer to prevent simultaneous dispatch in the LP"
            )
        if self.max_charge_mw is not None and self.max_charge_mw < 0:
            raise ValueError("max_charge_mw must be non-negative")
        if self.max_discharge_mw is not None and self.max_discharge_mw < 0:
            raise ValueError("max_discharge_mw must be non-negative")
        if self.max_reserve_mw < 0:
            raise ValueError("max_reserve_mw must be non-negative")
        if self.reserve_offer_price_per_mw_h < 0:
            raise ValueError("reserve_offer_price_per_mw_h must be non-negative")
        if self.dynamic_response_mw < 0:
            raise ValueError("dynamic_response_mw must be non-negative")


@dataclass
class DispatchResult:
    success: bool
    objective_value: float
    nodal_prices: Dict[str, float]
    generator_dispatch_mw: Dict[str, float]
    line_flows_mw: Dict[str, float]
    battery_charge_mw: float
    battery_discharge_mw: float
    load_shed_mw: Dict[str, float]
    emergency_absorption_mw: Dict[str, float]
    status: str = ""
    battery_reserve_mw: float = 0.0
    generator_reserve_mw: Dict[str, float] = field(default_factory=dict)
    reserve_requirement_mw: float = 0.0
    reserve_shortfall_mw: float = 0.0
    reserve_price_per_mw_h: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PeriodOutcome:
    period: int
    label: str
    demand_mw: Dict[str, float]
    demand_forecast_mw: Dict[str, float]
    renewable_forecast_mw: Dict[str, float]
    renewable_actual_mw: Dict[str, float]
    day_ahead_prices: Dict[str, float]
    real_time_prices: Dict[str, float]
    battery_da_charge_mw: float
    battery_da_discharge_mw: float
    battery_rt_charge_mw: float
    battery_rt_discharge_mw: float
    soc_end_mwh: float
    da_settlement: float
    rt_deviation_settlement: float
    degradation_cost: float
    net_cashflow: float
    line_flows_mw: Dict[str, float] = field(default_factory=dict)
    generator_dispatch_mw: Dict[str, float] = field(default_factory=dict)
    reserve_price_per_mw_h: float = 0.0
    reserve_shortfall_mw: float = 0.0
    battery_da_reserve_mw: float = 0.0
    battery_rt_reserve_mw: float = 0.0
    da_reserve_settlement: float = 0.0
    rt_reserve_deviation_settlement: float = 0.0
    capacity_call_mw: float = 0.0
    capacity_delivered_mw: float = 0.0
    capacity_revenue: float = 0.0
    capacity_penalty: float = 0.0
    dynamic_response_mw: float = 0.0
    dynamic_response_price_per_mw_h: float = 0.0
    dynamic_response_revenue: float = 0.0
    dynamic_response_throughput_mwh: float = 0.0
    dynamic_response_degradation_cost: float = 0.0
    congestion_value: float = 0.0
    rt_bid: Dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SimulationSummary:
    periods: List[PeriodOutcome]
    total_energy_revenue: float
    total_degradation_cost: float
    total_net_cashflow: float
    equivalent_cycles: float
    final_soc_mwh: float
    metadata: Dict[str, object]

    def to_dict(self) -> dict:
        return {
            "periods": [p.to_dict() for p in self.periods],
            "total_energy_revenue": self.total_energy_revenue,
            "total_degradation_cost": self.total_degradation_cost,
            "total_net_cashflow": self.total_net_cashflow,
            "equivalent_cycles": self.equivalent_cycles,
            "final_soc_mwh": self.final_soc_mwh,
            "metadata": self.metadata,
        }
