from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Dict, Iterable, List, Optional
import math
import random
import uuid

from .market import DCOPFMarket, MarketConfig
from .game_types import CommitmentBid
from .models import BatteryAsset, BatteryBid, DispatchResult, Line, PeriodOutcome
from .scenario import PeriodInputs, Scenario, build_stylised_gb_scenario
from .unit_commitment import COMMITMENT_TECH, DayAheadUnitCommitment, UnitCommitmentResult
from .rolling_rt import RollingRealTimeMarket, RollingRTResult


PERIOD_HOURS = 0.5


@dataclass
class PeriodBidPlan:
    max_charge_price_per_mwh: float = 35.0
    min_discharge_price_per_mwh: float = 105.0
    max_charge_mw: Optional[float] = None
    max_discharge_mw: Optional[float] = None
    reserve_holdback_fraction: float = 0.0
    reserve_offer_price_per_mw_h: float = 5.0
    dynamic_response_fraction: float = 0.0
    dynamic_response_offer_price_per_mw_h: float = 8.0

    def validate(self, battery_power_mw: float) -> None:
        if self.max_charge_price_per_mwh >= self.min_discharge_price_per_mwh:
            raise ValueError("Charge bid must be below discharge offer")
        if not 0.0 <= self.reserve_holdback_fraction <= 0.95:
            raise ValueError("reserve_holdback_fraction must be between 0 and 0.95")
        if self.reserve_offer_price_per_mw_h < 0:
            raise ValueError("reserve_offer_price_per_mw_h must be non-negative")
        if not 0.0 <= self.dynamic_response_fraction <= 0.95:
            raise ValueError("dynamic_response_fraction must be between 0 and 0.95")
        if self.reserve_holdback_fraction + self.dynamic_response_fraction > 0.95 + 1e-9:
            raise ValueError("Combined reserve and dynamic-response holdback cannot exceed 95%")
        if self.dynamic_response_offer_price_per_mw_h < 0:
            raise ValueError("dynamic_response_offer_price_per_mw_h must be non-negative")
        for name, value in (
            ("max_charge_mw", self.max_charge_mw),
            ("max_discharge_mw", self.max_discharge_mw),
        ):
            if value is not None and not 0.0 <= value <= battery_power_mw:
                raise ValueError(f"{name} must be between 0 and battery power")

    def to_battery_bid(
        self,
        battery_power_mw: float,
        *,
        dynamic_response_award_mw: float = 0.0,
    ) -> BatteryBid:
        self.validate(battery_power_mw)
        dynamic_response_award_mw = min(
            battery_power_mw, max(0.0, dynamic_response_award_mw)
        )
        available = max(
            0.0,
            battery_power_mw * (1.0 - self.reserve_holdback_fraction)
            - dynamic_response_award_mw,
        )
        charge = available if self.max_charge_mw is None else min(available, self.max_charge_mw)
        discharge = available if self.max_discharge_mw is None else min(available, self.max_discharge_mw)
        return BatteryBid(
            max_charge_price_per_mwh=self.max_charge_price_per_mwh,
            min_discharge_price_per_mwh=self.min_discharge_price_per_mwh,
            max_charge_mw=charge,
            max_discharge_mw=discharge,
            max_reserve_mw=min(
                battery_power_mw * self.reserve_holdback_fraction,
                max(0.0, battery_power_mw - dynamic_response_award_mw),
            ),
            reserve_offer_price_per_mw_h=self.reserve_offer_price_per_mw_h,
            dynamic_response_mw=dynamic_response_award_mw,
        )

    def to_commitment_bid(
        self,
        battery_power_mw: float,
        *,
        dynamic_response_award_mw: float = 0.0,
    ) -> CommitmentBid:
        self.validate(battery_power_mw)
        return CommitmentBid(
            max_charge_price_per_mwh=self.max_charge_price_per_mwh,
            min_discharge_price_per_mwh=self.min_discharge_price_per_mwh,
            max_charge_mw=self.max_charge_mw,
            max_discharge_mw=self.max_discharge_mw,
            reserve_holdback_fraction=self.reserve_holdback_fraction,
            reserve_offer_price_per_mw_h=self.reserve_offer_price_per_mw_h,
            dynamic_response_award_mw=dynamic_response_award_mw,
        )


@dataclass
class DayAheadScheduleItem:
    period: int
    label: str
    bid: PeriodBidPlan
    price: float
    nodal_prices: Dict[str, float]
    charge_mw: float
    discharge_mw: float
    soc_end_mwh: float
    line_flows_mw: Dict[str, float]
    committed_units: List[str] = field(default_factory=list)
    startup_units: List[str] = field(default_factory=list)
    reserve_requirement_mw: float = 0.0
    reserve_shortfall_mw: float = 0.0
    reserve_price_per_mw_h: float = 0.0
    battery_reserve_mw: float = 0.0
    dynamic_response_mw: float = 0.0
    dynamic_response_price_per_mw_h: float = 0.0
    dynamic_response_requirement_mw: float = 0.0

    @property
    def net_mw(self) -> float:
        return self.discharge_mw - self.charge_mw

    def to_dict(self) -> dict:
        out = asdict(self)
        out["net_mw"] = self.net_mw
        return out


@dataclass
class ForecastItem:
    period: int
    label: str
    node_price_forecast: float
    nodal_price_forecast: Dict[str, float]
    total_demand_forecast_mw: float
    demand_low_mw: float
    demand_high_mw: float
    renewable_forecast_mw: float
    renewable_low_mw: float
    renewable_high_mw: float
    congestion_risk: str
    line_flows_mw: Dict[str, float]
    dynamic_response_reference_price_per_mw_h: float = 0.0
    dynamic_response_requirement_mw: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


def _scaled_lines(lines: List[Line], multipliers: Dict[str, float]) -> List[Line]:
    return [
        replace(line, capacity_mw=line.capacity_mw * multipliers.get(line.name, 1.0))
        for line in lines
    ]


def _sum(values: Dict[str, float]) -> float:
    return float(sum(values.values()))


def _apply_commitment_to_fleet(fleet, commitment: Dict[str, bool], *, real_time: bool) -> list:
    """Return a fleet with DA commitment translated into physical availability.

    OCGTs are treated as fast-start in real time; slower thermal units that were
    not committed day-ahead cannot appear magically in the balancing stack.
    """
    out = []
    for gen in fleet:
        if gen.technology not in COMMITMENT_TECH:
            out.append(replace(gen))
            continue
        on = commitment.get(gen.name, True)
        if real_time and gen.technology == "ocgt":
            on = True
        if not on:
            out.append(replace(gen, available_fraction=0.0, min_output_mw=0.0))
            continue
        stable = gen.minimum_stable_mw if gen.minimum_stable_mw > 0 else gen.min_output_mw
        stable = min(stable, gen.available_capacity_mw)
        out.append(replace(gen, min_output_mw=stable))
    return out


def _apply_rt_commitment_for_rolling(fleet, commitment: Dict[str, bool]) -> list:
    """Translate DA commitment into the rolling RT feasible fleet.

    Slow thermal units remain bound by the DA commitment. OCGTs are fast-start
    and may enter RT from zero output, so they keep capacity but do not carry a
    forced minimum-output block when they were not already committed.
    """
    out = []
    for gen in fleet:
        if gen.technology not in COMMITMENT_TECH:
            out.append(replace(gen))
            continue
        if gen.technology == "ocgt":
            if commitment.get(gen.name, False):
                stable = gen.minimum_stable_mw if gen.minimum_stable_mw > 0 else gen.min_output_mw
                stable = min(stable, gen.available_capacity_mw)
                out.append(replace(gen, min_output_mw=stable))
            else:
                out.append(replace(gen, min_output_mw=0.0))
            continue
        if not commitment.get(gen.name, True):
            out.append(replace(gen, available_fraction=0.0, min_output_mw=0.0))
            continue
        stable = gen.minimum_stable_mw if gen.minimum_stable_mw > 0 else gen.min_output_mw
        stable = min(stable, gen.available_capacity_mw)
        out.append(replace(gen, min_output_mw=stable))
    return out


def _initial_rt_dispatch_state(fleet) -> Dict[str, float]:
    state: Dict[str, float] = {}
    for gen in fleet:
        if gen.technology not in COMMITMENT_TECH:
            continue
        if gen.initial_output_mw is not None:
            state[gen.name] = max(0.0, gen.initial_output_mw)
        elif gen.initially_on:
            stable = gen.minimum_stable_mw if gen.minimum_stable_mw > 0 else gen.min_output_mw
            state[gen.name] = max(0.0, stable)
        else:
            state[gen.name] = 0.0
    return state


def _apply_rt_ramp_limits(
    fleet,
    commitment: Dict[str, bool],
    previous_dispatch: Dict[str, float],
    startup_units: Iterable[str],
) -> list:
    committed = _apply_commitment_to_fleet(fleet, commitment, real_time=True)
    startup = set(startup_units)
    out = []
    for gen in committed:
        if gen.technology not in COMMITMENT_TECH:
            out.append(gen)
            continue
        cap = gen.available_capacity_mw
        if cap <= 1e-9:
            out.append(replace(gen, min_output_mw=0.0))
            continue
        prev = max(0.0, previous_dispatch.get(gen.name, 0.0))
        # OCGT is fast-start; a DA startup or a restart after a forced outage can
        # jump onto its stable operating range instead of being trapped at zero.
        restart = gen.name in startup or gen.technology == "ocgt" or prev <= 1e-6
        if restart:
            upper = cap
            lower = min(gen.min_output_mw, upper)
        else:
            upper = min(cap, prev + gen.ramp_up_mw_per_hour * PERIOD_HOURS)
            lower = max(0.0, prev - gen.ramp_down_mw_per_hour * PERIOD_HOURS)
            if gen.min_output_mw > 0 and upper >= gen.min_output_mw:
                lower = max(lower, gen.min_output_mw)
            lower = min(lower, upper)
        out.append(replace(gen, capacity_mw=upper, available_fraction=1.0, min_output_mw=lower))
    return out


def _congestion_risk(result: DispatchResult, lines: Iterable[Line]) -> str:
    utilisation = []
    for line in lines:
        flow = abs(result.line_flows_mw.get(line.name, 0.0))
        utilisation.append(flow / line.capacity_mw if line.capacity_mw > 0 else 1.0)
    peak = max(utilisation, default=0.0)
    if peak >= 0.97:
        return "binding"
    if peak >= 0.93:
        return "high"
    if peak >= 0.85:
        return "watch"
    return "low"


def _reserve_requirement(demand: Dict[str, float]) -> float:
    return max(180.0, 0.08 * _sum(demand))


def _dynamic_response_auction(
    scenario_seed: int,
    period: PeriodInputs,
    *,
    player_offer_mw: float = 0.0,
    player_offer_price_per_mw_h: float = 1_000_000.0,
) -> dict:
    """Clear a stylised pay-as-clear dynamic-frequency-response auction.

    This product is intentionally separate from the energy/reserve optimisation:
    the GB dynamic response services are procured as availability products, and the
    resulting award then constrains the BESS's inverter/SOC position. Competition is
    represented by a deterministic supply stack so the same scenario seed always
    produces the same market around the player.
    """
    demand = max(1.0, _sum(period.demand_forecast_mw))
    renewables = max(0.0, _sum(period.renewable_forecast_mw))
    nonsynchronous_share = min(0.85, renewables / demand)

    # Higher nonsynchronous penetration increases the stylised requirement. The
    # values are deliberately game-scale rather than a claim about NESO volumes.
    requirement = 430.0 + 420.0 * nonsynchronous_share
    requirement += 55.0 * math.exp(-((period.period - 37.0) ** 2) / (2 * 5.0 ** 2))
    requirement = round(requirement, 1)

    rng = random.Random(scenario_seed * 100_003 + period.period * 1_009 + 17)
    blocks: List[dict] = []
    # A competitive battery/response fleet with a rising availability-price stack.
    market_tightness = 2.5 + 7.5 * nonsynchronous_share
    for i in range(10):
        mw = rng.uniform(65.0, 145.0)
        price = max(0.5, market_tightness + 0.9 * i + rng.gauss(0.0, 0.8))
        blocks.append({"owner": f"response-{i}", "mw": mw, "price": price})
    if player_offer_mw > 1e-9:
        blocks.append(
            {
                "owner": "player",
                "mw": max(0.0, player_offer_mw),
                "price": max(0.0, player_offer_price_per_mw_h),
            }
        )

    blocks.sort(key=lambda x: (x["price"], 0 if x["owner"] == "player" else 1))
    remaining = requirement
    clearing_price = 0.0
    player_award = 0.0
    procured = 0.0
    for block in blocks:
        if remaining <= 1e-9:
            break
        award = min(float(block["mw"]), remaining)
        if award <= 0:
            continue
        procured += award
        remaining -= award
        clearing_price = max(clearing_price, float(block["price"]))
        if block["owner"] == "player":
            player_award += award

    shortfall = max(0.0, requirement - procured)
    if shortfall > 1e-6:
        # A reliability backstop, analogous to a very expensive procurement action.
        clearing_price = max(clearing_price, 250.0)

    return {
        "requirement_mw": requirement,
        "clearing_price_per_mw_h": round(clearing_price, 4),
        "player_award_mw": round(player_award, 6),
        "shortfall_mw": round(shortfall, 6),
    }


def _dynamic_response_activation_fraction(period: PeriodInputs) -> float:
    """Stylised energy-throughput intensity for frequency-response delivery.

    Dynamic response is mostly an availability service, but batteries still cycle
    around their baseline while following frequency. We approximate that wear with
    an energy-neutral throughput factor that rises when forecast errors and network
    disturbances make the system more volatile.
    """
    demand_fc = max(1.0, _sum(period.demand_forecast_mw))
    demand_miss = abs(_sum(period.demand_actual_mw) / demand_fc - 1.0)
    renewable_fc = max(1.0, _sum(period.renewable_forecast_mw))
    renewable_miss = abs(_sum(period.renewable_actual_mw) / renewable_fc - 1.0)
    line_stress = max(
        (1.0 - x for x in period.rt_line_capacity_multiplier.values()),
        default=0.0,
    )
    forced_derate = max(
        (1.0 - g.available_fraction for g in period.rt_generators if g.technology in COMMITMENT_TECH),
        default=0.0,
    )
    return min(
        0.20,
        0.025 + 0.55 * demand_miss + 0.14 * renewable_miss + 0.08 * line_stress + 0.05 * forced_derate,
    )


def _reference_clear(
    scenario: Scenario,
    period: PeriodInputs,
    demand: Dict[str, float],
    generators,
    lines: Optional[List[Line]] = None,
    reserve_requirement_mw: Optional[float] = None,
    battery: Optional[BatteryAsset] = None,
    battery_bid: Optional[BatteryBid] = None,
) -> DispatchResult:
    market = DCOPFMarket(
        scenario.nodes,
        lines if lines is not None else scenario.lines,
        MarketConfig(value_of_lost_load_per_mwh=5000.0, emergency_absorption_cost_per_mwh=250.0),
    )
    if reserve_requirement_mw is None:
        reserve_requirement_mw = _reserve_requirement(demand)
    result = market.clear(
        demand, generators, PERIOD_HOURS, battery=battery, battery_bid=battery_bid,
        reserve_requirement_mw=reserve_requirement_mw
    )
    if not result.success:
        raise RuntimeError(f"Reference clearing failed in {period.label}: {result.status}")
    return result


def _forecast_items(scenario: Scenario, battery_node: str) -> List[ForecastItem]:
    rows: List[ForecastItem] = []
    for period in scenario.periods:
        result = _reference_clear(scenario, period, period.demand_forecast_mw, period.da_generators)
        dynamic = _dynamic_response_auction(scenario.seed, period)
        demand = _sum(period.demand_forecast_mw)
        renewable = _sum(period.renewable_forecast_mw)
        rows.append(
            ForecastItem(
                period=period.period,
                label=period.label,
                node_price_forecast=result.nodal_prices[battery_node],
                nodal_price_forecast=result.nodal_prices,
                total_demand_forecast_mw=demand,
                demand_low_mw=demand * 0.955,
                demand_high_mw=demand * 1.045,
                renewable_forecast_mw=renewable,
                renewable_low_mw=max(0.0, renewable * 0.84),
                renewable_high_mw=renewable * 1.16,
                congestion_risk=_congestion_risk(result, scenario.lines),
                line_flows_mw=result.line_flows_mw,
                dynamic_response_reference_price_per_mw_h=dynamic["clearing_price_per_mw_h"],
                dynamic_response_requirement_mw=dynamic["requirement_mw"],
            )
        )
    return rows


def _nowcast_generators(period: PeriodInputs) -> list:
    """Blend renewable forecast and realised output while retaining known RT outages.

    The player sees updated weather/system information, but not the final realised
    physical balance. Thermal outage/derate status is treated as operationally known.
    """
    renew_fc = period.renewable_forecast_mw
    renew_rt = period.renewable_actual_mw
    out = []
    for gen in period.rt_generators:
        if gen.technology == "wind":
            capacity = 0.30 * renew_fc.get(gen.node, 0.0) + 0.70 * renew_rt.get(gen.node, gen.capacity_mw)
            out.append(replace(gen, capacity_mw=capacity))
        elif gen.technology == "solar":
            capacity = 0.30 * renew_fc.get(gen.node, 0.0) + 0.70 * renew_rt.get(gen.node, gen.capacity_mw)
            out.append(replace(gen, capacity_mw=capacity))
        else:
            out.append(replace(gen))
    return out


def _nowcast_demand(period: PeriodInputs) -> Dict[str, float]:
    return {
        node: 0.30 * period.demand_forecast_mw[node] + 0.70 * period.demand_actual_mw[node]
        for node in period.demand_forecast_mw
    }


def _event_alerts(period: PeriodInputs) -> List[dict]:
    alerts: List[dict] = []
    for gen in period.rt_generators:
        if gen.technology in {"ccgt", "ocgt", "coal", "nuclear", "biomass"} and gen.available_fraction < 0.999:
            if gen.available_fraction <= 0.001:
                alerts.append({"severity": "high", "type": "generator", "text": f"{gen.name} forced outage"})
            else:
                alerts.append(
                    {
                        "severity": "medium",
                        "type": "generator",
                        "text": f"{gen.name} derated to {gen.available_fraction:.0%}",
                    }
                )
    for line, multiplier in period.rt_line_capacity_multiplier.items():
        if multiplier < 0.999:
            alerts.append(
                {
                    "severity": "high" if multiplier < 0.55 else "medium",
                    "type": "network",
                    "text": f"{line} capacity reduced to {multiplier:.0%}",
                }
            )
    demand_fc = _sum(period.demand_forecast_mw)
    demand_rt = _sum(period.demand_actual_mw)
    if demand_fc:
        revision = (demand_rt / demand_fc - 1.0) * 100.0
        if abs(revision) >= 4.0:
            direction = "above" if revision > 0 else "below"
            alerts.append(
                {
                    "severity": "medium",
                    "type": "demand",
                    "text": f"Demand now running {abs(revision):.1f}% {direction} day-ahead forecast",
                }
            )
    renewable_fc = _sum(period.renewable_forecast_mw)
    renewable_rt = _sum(period.renewable_actual_mw)
    if renewable_fc > 80:
        revision = (renewable_rt / renewable_fc - 1.0) * 100.0
        if abs(revision) >= 12.0:
            direction = "above" if revision > 0 else "below"
            alerts.append(
                {
                    "severity": "medium",
                    "type": "renewables",
                    "text": f"Renewable output tracking {abs(revision):.0f}% {direction} forecast",
                }
            )
    if not alerts:
        alerts.append({"severity": "low", "type": "system", "text": "No material system alert"})
    return alerts


def _terminal_inventory_value_per_soc(price: float, battery: BatteryAsset) -> float:
    # A stored MWh can later produce eta_discharge MWh at the grid connection.
    # We mark only non-negative optionality; the owner can retain energy through a
    # negative-price period rather than being forced to dispose of it.
    net_grid_value = max(0.0, price - battery.degradation_cost_per_mwh)
    return net_grid_value * battery.eta_discharge


def _oracle_benchmark(
    prices: List[float],
    battery: BatteryAsset,
    duration_hours: float = PERIOD_HOURS,
    state_count: int = 121,
    terminal_price: Optional[float] = None,
) -> dict:
    """Discrete dynamic-programming perfect-foresight price-taking benchmark.

    It uses the counterfactual RT reference price without the player's BESS. That
    makes it a useful trading benchmark while avoiding a false claim that the player
    could know and exploit a price path that their own large asset would itself change.
    """
    min_soc, max_soc = battery.min_soc_mwh, battery.max_soc_mwh
    if max_soc <= min_soc:
        return {"net_cashflow": 0.0, "economic_pnl": 0.0, "schedule": []}

    step = (max_soc - min_soc) / max(2, state_count - 1)
    states = [min_soc + i * step for i in range(state_count)]
    # Ensure the actual starting state is representable exactly.
    if all(abs(s - battery.soc_mwh) > 1e-9 for s in states):
        states.append(battery.soc_mwh)
        states.sort()

    if terminal_price is None:
        terminal_price = sum(prices[-4:]) / min(4, len(prices)) if prices else 0.0
    terminal_value = _terminal_inventory_value_per_soc(terminal_price, battery)
    values = [s * terminal_value for s in states]
    policies: List[List[int]] = []

    # Work backwards. Each transition corresponds to a feasible change in stored MWh.
    for price in reversed(prices):
        new_values = [-math.inf] * len(states)
        policy = [0] * len(states)
        max_soc_charge = battery.power_mw * battery.eta_charge * duration_hours
        max_soc_discharge = battery.power_mw / battery.eta_discharge * duration_hours
        for i, soc in enumerate(states):
            best = -math.inf
            best_j = i
            for j, next_soc in enumerate(states):
                delta = next_soc - soc
                if delta > max_soc_charge + 1e-9 or -delta > max_soc_discharge + 1e-9:
                    continue
                if delta >= 0:
                    grid_mwh = delta / battery.eta_charge
                    cash = -price * grid_mwh
                else:
                    grid_mwh = (-delta) * battery.eta_discharge
                    cash = price * grid_mwh - battery.degradation_cost_per_mwh * grid_mwh
                value = cash + values[j]
                if value > best:
                    best = value
                    best_j = j
            new_values[i] = best
            policy[i] = best_j
        values = new_values
        policies.append(policy)

    policies.reverse()
    start_index = min(range(len(states)), key=lambda i: abs(states[i] - battery.soc_mwh))
    initial_inventory_value = battery.soc_mwh * terminal_value
    economic_pnl = values[start_index] - initial_inventory_value

    schedule = []
    cash_total = 0.0
    idx = start_index
    for t, (price, policy) in enumerate(zip(prices, policies)):
        next_idx = policy[idx]
        delta = states[next_idx] - states[idx]
        if delta >= 0:
            charge_mwh = delta / battery.eta_charge
            charge_mw = charge_mwh / duration_hours
            discharge_mw = 0.0
            cash = -price * charge_mwh
        else:
            discharge_mwh = (-delta) * battery.eta_discharge
            charge_mw = 0.0
            discharge_mw = discharge_mwh / duration_hours
            cash = price * discharge_mwh - battery.degradation_cost_per_mwh * discharge_mwh
        cash_total += cash
        schedule.append(
            {
                "period": t,
                "price": price,
                "charge_mw": charge_mw,
                "discharge_mw": discharge_mw,
                "soc_end_mwh": states[next_idx],
                "cashflow": cash,
            }
        )
        idx = next_idx

    return {
        "net_cashflow": cash_total,
        "economic_pnl": economic_pnl,
        "terminal_price": terminal_price,
        "terminal_inventory_value_per_mwh_soc": terminal_value,
        "schedule": schedule,
        "method": "price-taking perfect foresight on no-BESS-dispatch RT prices under the cleared DA commitment",
    }


class GameSession:
    def __init__(
        self,
        *,
        seed: int = 7,
        battery_power_mw: float = 100.0,
        battery_duration_hours: float = 2.0,
        starting_soc_fraction: float = 0.5,
        battery_node: str = "Central",
        degradation_cost_per_mwh: float = 7.0,
        round_trip_efficiency: float = 0.88,
        capacity_contract_fraction: float = 0.0,
        capacity_payment_per_kw_year: float = 45.0,
        scenario: Optional[Scenario] = None,
    ) -> None:
        if battery_node not in {"North", "Central", "South"}:
            raise ValueError("battery_node must be North, Central, or South")
        if not 0.0 <= capacity_contract_fraction <= 1.0:
            raise ValueError("capacity_contract_fraction must be between 0 and 1")
        if capacity_payment_per_kw_year < 0:
            raise ValueError("capacity_payment_per_kw_year must be non-negative")
        self.id = uuid.uuid4().hex[:12]
        self.scenario = scenario or build_stylised_gb_scenario(seed=seed)
        energy_mwh = battery_power_mw * battery_duration_hours
        starting_soc = energy_mwh * starting_soc_fraction
        self.base_battery = BatteryAsset(
            name="Player BESS",
            node=battery_node,
            power_mw=battery_power_mw,
            energy_mwh=energy_mwh,
            soc_mwh=starting_soc,
            round_trip_efficiency=round_trip_efficiency,
            degradation_cost_per_mwh=degradation_cost_per_mwh,
        )
        self.capacity_contract_mw = battery_power_mw * capacity_contract_fraction
        self.capacity_payment_per_kw_year = capacity_payment_per_kw_year
        # Stylised reliability contract: daily capacity fee, with a material
        # non-delivery penalty during system-stress calls.
        self.capacity_penalty_per_mwh = 1000.0
        self.capacity_call_price_threshold = 250.0
        self.da_battery = replace(self.base_battery)
        self.rt_battery = replace(self.base_battery)
        self.forecasts = _forecast_items(self.scenario, battery_node)
        self.da_schedule: List[DayAheadScheduleItem] = []
        self.da_results: List[DispatchResult] = []
        self.dynamic_response_auctions: List[dict] = []
        self.outcomes: List[PeriodOutcome] = []
        self.rt_bids: List[PeriodBidPlan] = []
        self.current_period = 0
        self.phase = "forecast"
        self.config = MarketConfig(value_of_lost_load_per_mwh=5000.0, emergency_absorption_cost_per_mwh=250.0)
        self._reference_rt_prices: Optional[List[float]] = None
        self.uc_result: Optional[UnitCommitmentResult] = None
        self.rt_prev_dispatch = _initial_rt_dispatch_state(self.scenario.periods[0].rt_generators)
        self.rt_lookahead_periods = 8  # four hours at 30-minute settlement
        self._last_rt_lookahead: Optional[RollingRTResult] = None

    @property
    def battery_node(self) -> str:
        return self.base_battery.node

    def start_payload(self) -> dict:
        prices = [x.node_price_forecast for x in self.forecasts]
        return {
            "game_id": self.id,
            "phase": self.phase,
            "metadata": self._metadata(),
            "forecast": [x.to_dict() for x in self.forecasts],
            "forecast_summary": {
                "min_price": min(prices),
                "max_price": max(prices),
                "average_price": sum(prices) / len(prices),
                "negative_periods": sum(1 for p in prices if p < 0),
                "high_congestion_risk_periods": sum(1 for x in self.forecasts if x.congestion_risk == "binding"),
            },
            "known_events": list(self.scenario.known_events),
        }

    def clear_day_ahead(self, bids: List[PeriodBidPlan]) -> dict:
        if self.phase != "forecast":
            raise ValueError("Day-ahead book is already locked")
        if len(bids) != len(self.scenario.periods):
            raise ValueError(f"Expected {len(self.scenario.periods)} day-ahead bids")
        for bid in bids:
            bid.validate(self.base_battery.power_mw)

        self.da_schedule = []
        self.da_results = []
        self.dynamic_response_auctions = []
        self.da_battery = replace(self.base_battery)

        for period, plan in zip(self.scenario.periods, bids):
            self.dynamic_response_auctions.append(
                _dynamic_response_auction(
                    self.scenario.seed,
                    period,
                    player_offer_mw=self.base_battery.power_mw * plan.dynamic_response_fraction,
                    player_offer_price_per_mw_h=plan.dynamic_response_offer_price_per_mw_h,
                )
            )

        # v0.3 physical foundation: commit the thermal fleet across the whole day.
        # The player's DA battery bids participate in the same MILP, so the asset can
        # change which plants need to start rather than merely taking a price series.
        uc = DayAheadUnitCommitment(
            self.scenario.nodes, self.scenario.lines, duration_hours=PERIOD_HOURS,
            reserve_fraction_of_demand=0.10, reserve_floor_mw=220.0,
        )
        self.uc_result = uc.solve(
            [p.demand_forecast_mw for p in self.scenario.periods],
            [p.da_generators for p in self.scenario.periods],
            self.base_battery,
            [
                bid.to_commitment_bid(
                    self.base_battery.power_mw,
                    dynamic_response_award_mw=self.dynamic_response_auctions[t]["player_award_mw"],
                )
                for t, bid in enumerate(bids)
            ],
        )
        if not self.uc_result.success:
            raise RuntimeError(f"Day-ahead unit commitment failed: {self.uc_result.status}")

        for t, (period, plan) in enumerate(zip(self.scenario.periods, bids)):
            dynamic = self.dynamic_response_auctions[t]
            dynamic_award_mw = dynamic["player_award_mw"]
            commitment = self.uc_result.commitment[t]
            committed_fleet = _apply_commitment_to_fleet(period.da_generators, commitment, real_time=False)
            charge_mw = self.uc_result.battery_charge_mw[t]
            discharge_mw = self.uc_result.battery_discharge_mw[t]

            # Price the convex dispatch with commitment and the battery schedule fixed.
            # This mirrors common LMP + uplift logic: startup/no-load costs determine
            # commitment, while the energy price comes from the fixed-commitment LP.
            priced_demand = dict(period.demand_forecast_mw)
            priced_demand[self.battery_node] += charge_mw - discharge_mw
            market_reserve_requirement = _reserve_requirement(period.demand_forecast_mw)
            # Convex DA pricing includes the BESS as a reserve supplier while its
            # energy schedule is fixed through the adjusted nodal demand. This lets
            # reserve prices reflect the battery's actual reserve offer rather than
            # paying it an arbitrary generator-only price.
            reserve_pricing_battery = replace(
                self.base_battery, soc_mwh=self.uc_result.battery_soc_mwh[t]
            )
            reserve_pricing_bid = BatteryBid(
                max_charge_price_per_mwh=-1_000_000.0,
                min_discharge_price_per_mwh=1_000_000.0,
                max_charge_mw=0.0, max_discharge_mw=0.0,
                max_reserve_mw=min(
                    self.base_battery.power_mw * plan.reserve_holdback_fraction,
                    max(0.0, self.base_battery.power_mw - dynamic_award_mw),
                ),
                reserve_offer_price_per_mw_h=plan.reserve_offer_price_per_mw_h,
                dynamic_response_mw=dynamic_award_mw,
            )
            priced = _reference_clear(
                self.scenario, period, priced_demand, committed_fleet,
                reserve_requirement_mw=market_reserve_requirement,
                battery=reserve_pricing_battery, battery_bid=reserve_pricing_bid,
            )
            result = replace(
                priced,
                battery_charge_mw=charge_mw,
                battery_discharge_mw=discharge_mw,
                battery_reserve_mw=self.uc_result.battery_reserve_mw[t],
            )
            self.da_results.append(result)
            self.da_battery.soc_mwh = self.uc_result.battery_soc_mwh[t]
            self.da_schedule.append(
                DayAheadScheduleItem(
                    period=period.period,
                    label=period.label,
                    bid=plan,
                    price=result.nodal_prices[self.battery_node],
                    nodal_prices=result.nodal_prices,
                    charge_mw=charge_mw,
                    discharge_mw=discharge_mw,
                    soc_end_mwh=self.da_battery.soc_mwh,
                    line_flows_mw=result.line_flows_mw,
                    committed_units=[name for name, on in commitment.items() if on],
                    startup_units=[name for name, on in self.uc_result.startup[t].items() if on],
                    reserve_requirement_mw=market_reserve_requirement,
                    reserve_shortfall_mw=max(self.uc_result.reserve_shortfall_mw[t], result.reserve_shortfall_mw),
                    reserve_price_per_mw_h=result.reserve_price_per_mw_h,
                    battery_reserve_mw=self.uc_result.battery_reserve_mw[t],
                    dynamic_response_mw=dynamic_award_mw,
                    dynamic_response_price_per_mw_h=dynamic["clearing_price_per_mw_h"],
                    dynamic_response_requirement_mw=dynamic["requirement_mw"],
                )
            )

        self.phase = "real_time"
        self.current_period = 0
        return {
            "game_id": self.id,
            "phase": self.phase,
            "day_ahead_schedule": [x.to_dict() for x in self.da_schedule],
            "day_ahead_summary": self._da_summary(),
            "briefing": self.current_briefing(),
        }

    def _rolling_rt_inputs(self, current_bid: PeriodBidPlan, *, use_actual_first: bool) -> dict:
        """Build a public-information RT outlook without peeking at future realisations.

        Period zero uses either the final realised system state (for settlement) or
        the player's current nowcast (for the pre-clear briefing). Later periods are
        forecast values revised by the currently observed demand/renewable miss.
        Current forced outages and transmission derates are assumed to persist across
        the short horizon because their repair time is not known to the player.
        """
        start = self.current_period
        end = min(len(self.scenario.periods), start + self.rt_lookahead_periods)
        current = self.scenario.periods[start]
        current_now_demand = _nowcast_demand(current)
        current_now_fleet = _nowcast_generators(current)

        demand_revision = {
            node: (current_now_demand[node] / current.demand_forecast_mw[node] - 1.0)
            if current.demand_forecast_mw[node] > 1e-9 else 0.0
            for node in current.demand_forecast_mw
        }
        current_ren_fc = current.renewable_forecast_mw
        current_ren_now = {n: 0.0 for n in current_ren_fc}
        for g in current_now_fleet:
            if g.technology in {"wind", "solar"}:
                current_ren_now[g.node] = current_ren_now.get(g.node, 0.0) + g.capacity_mw
        renewable_revision = {
            node: (current_ren_now.get(node, 0.0) / current_ren_fc[node] - 1.0)
            if current_ren_fc.get(node, 0.0) > 1.0 else 0.0
            for node in current_ren_fc
        }

        current_da_avail = {g.name: g.available_fraction for g in current.da_generators}
        current_rt_avail = {g.name: g.available_fraction for g in current.rt_generators}
        forced_availability = {}
        for name, rt_avail in current_rt_avail.items():
            da_avail = current_da_avail.get(name, 1.0)
            if rt_avail + 1e-9 < da_avail:
                forced_availability[name] = rt_avail

        # The currently observed line condition is assumed to persist through the
        # four-hour operator horizon. Future random line derates remain hidden.
        line_multiplier = dict(current.rt_line_capacity_multiplier)

        demands: List[Dict[str, float]] = []
        fleets: List[list] = []
        lines_by_period: List[List[Line]] = []
        bids: List[BatteryBid] = []
        reserve_requirements: List[float] = []
        labels: List[str] = []
        startup_units: List[List[str]] = []
        shutdown_units: List[List[str]] = []

        for idx in range(start, end):
            p = self.scenario.periods[idx]
            lead = idx - start
            decay = 0.68 ** lead
            if lead == 0:
                demand = dict(p.demand_actual_mw if use_actual_first else current_now_demand)
                fleet = [replace(g) for g in (p.rt_generators if use_actual_first else current_now_fleet)]
            else:
                demand = {
                    node: max(0.0, p.demand_forecast_mw[node] * (1.0 + demand_revision.get(node, 0.0) * decay))
                    for node in p.demand_forecast_mw
                }
                fleet = [replace(g) for g in p.da_generators]
                for g in fleet:
                    if g.technology in {"wind", "solar"}:
                        g.capacity_mw = max(0.0, g.capacity_mw * (1.0 + renewable_revision.get(g.node, 0.0) * decay))
                    elif g.name in forced_availability:
                        g.available_fraction = min(g.available_fraction, forced_availability[g.name])
                        g.min_output_mw = min(g.min_output_mw, g.available_capacity_mw)

            if self.uc_result is not None:
                fleet = _apply_rt_commitment_for_rolling(fleet, self.uc_result.commitment[idx])
                startup_names = [name for name, on in self.uc_result.startup[idx].items() if on]
                shutdown_names = [name for name, on in self.uc_result.shutdown[idx].items() if on]
            else:
                startup_names = []
                shutdown_names = []

            # When an outage/maintenance derate recovers, the unit can be physically
            # online below its normal stable minimum while it ramps back into range.
            # Cap the temporary minimum at a ramp-reachable level so the rolling LP
            # does not become infeasible merely because capacity returned suddenly.
            prior_by_name = {g.name: g for g in fleets[-1]} if fleets else {}
            adjusted = []
            for g in fleet:
                if g.technology not in COMMITMENT_TECH or g.available_capacity_mw <= 1e-9:
                    adjusted.append(g)
                    continue
                if g.name in startup_names or (g.technology == "ocgt" and self.rt_prev_dispatch.get(g.name, 0.0) <= 1e-6 and lead == 0):
                    adjusted.append(g)
                    continue
                if lead == 0:
                    prior_level = max(0.0, self.rt_prev_dispatch.get(g.name, 0.0))
                else:
                    prior = prior_by_name.get(g.name)
                    prior_level = max(0.0, prior.min_output_mw if prior is not None else 0.0)
                reachable = prior_level + g.ramp_up_mw_per_hour * PERIOD_HOURS
                if g.min_output_mw > reachable + 1e-9:
                    g = replace(g, min_output_mw=min(g.available_capacity_mw, reachable))
                adjusted.append(g)
            fleet = adjusted
            startup_units.append(startup_names)
            shutdown_units.append(shutdown_names)

            demands.append(demand)
            fleets.append(fleet)
            lines_by_period.append(_scaled_lines(self.scenario.lines, line_multiplier))
            reserve_requirements.append(_reserve_requirement(demand))
            labels.append(p.label)
            if lead == 0:
                bids.append(
                    current_bid.to_battery_bid(
                        self.base_battery.power_mw,
                        dynamic_response_award_mw=self.da_schedule[idx].dynamic_response_mw,
                    )
                )
            else:
                bids.append(
                    self.da_schedule[idx].bid.to_battery_bid(
                        self.base_battery.power_mw,
                        dynamic_response_award_mw=self.da_schedule[idx].dynamic_response_mw,
                    )
                )

        # Continuation value is based on the DA curve immediately beyond the visible
        # RT horizon, not on hidden realised spot prices.
        ref_start = end
        ref_end = min(len(self.da_schedule), end + 4)
        if ref_start < ref_end:
            continuation_price = sum(self.da_schedule[i].price for i in range(ref_start, ref_end)) / (ref_end - ref_start)
        elif self.da_schedule:
            tail = self.da_schedule[max(0, len(self.da_schedule) - 4):]
            continuation_price = sum(x.price for x in tail) / len(tail)
        else:
            continuation_price = 0.0
        terminal_soc_value = _terminal_inventory_value_per_soc(continuation_price, self.base_battery)

        return {
            "demands": demands,
            "fleets": fleets,
            "lines_by_period": lines_by_period,
            "bids": bids,
            "reserve_requirements": reserve_requirements,
            "labels": labels,
            "startup_units": startup_units,
            "shutdown_units": shutdown_units,
            "terminal_soc_value": terminal_soc_value,
            "continuation_price": continuation_price,
        }

    def _rolling_rt_clear(self, current_bid: PeriodBidPlan, *, use_actual_first: bool) -> tuple[RollingRTResult, dict]:
        inputs = self._rolling_rt_inputs(current_bid, use_actual_first=use_actual_first)
        rolling = RollingRealTimeMarket(self.scenario.nodes, PERIOD_HOURS, self.config)
        result = rolling.clear(
            demands=inputs["demands"],
            fleets=inputs["fleets"],
            lines_by_period=inputs["lines_by_period"],
            battery=self.rt_battery,
            battery_bids=inputs["bids"],
            reserve_requirements_mw=inputs["reserve_requirements"],
            previous_dispatch_mw=self.rt_prev_dispatch,
            startup_units_by_period=inputs["startup_units"],
            shutdown_units_by_period=inputs["shutdown_units"],
            terminal_soc_value_per_mwh=inputs["terminal_soc_value"],
        )
        if not result.success:
            raise RuntimeError(f"Rolling real-time clearing failed: {result.status}")
        return result, inputs

    def _lookahead_payload(self, result: RollingRTResult, inputs: dict) -> List[dict]:
        rows = []
        for k, label in enumerate(inputs["labels"]):
            lines = inputs["lines_by_period"][k]
            caps = {line.name: line.capacity_mw for line in lines}
            flows = result.horizon_line_flows_mw[k]
            peak_util = max((abs(flows.get(name, 0.0)) / cap if cap > 0 else 1.0) for name, cap in caps.items()) if caps else 0.0
            if peak_util >= 0.97:
                congestion = "binding"
            elif peak_util >= 0.93:
                congestion = "high"
            elif peak_util >= 0.85:
                congestion = "watch"
            else:
                congestion = "low"
            rows.append({
                "period": self.current_period + k,
                "label": label,
                "price": result.horizon_prices[k][self.battery_node],
                "reserve_price_per_mw_h": result.horizon_reserve_prices[k],
                "reserve_shortfall_mw": result.horizon_reserve_shortfall_mw[k],
                "battery_charge_mw": result.horizon_battery_charge_mw[k],
                "battery_discharge_mw": result.horizon_battery_discharge_mw[k],
                "battery_reserve_mw": result.horizon_battery_reserve_mw[k],
                "dynamic_response_mw": inputs["bids"][k].dynamic_response_mw,
                "projected_soc_mwh": result.horizon_soc_mwh[k],
                "congestion_risk": congestion,
            })
        return rows

    def current_briefing(self) -> Optional[dict]:
        if self.phase != "real_time" or self.current_period >= len(self.scenario.periods):
            return None
        p = self.scenario.periods[self.current_period]
        da = self.da_schedule[self.current_period]
        # The pre-clear desk view uses only nowcasts and revised forecasts. The same
        # rolling multi-period formulation used for settlement is therefore visible
        # to the player without revealing future realised demand/weather/outages.
        indicative_roll, indicative_inputs = self._rolling_rt_clear(da.bid, use_actual_first=False)
        indicative = indicative_roll.first_period
        lookahead = self._lookahead_payload(indicative_roll, indicative_inputs)
        lines = indicative_inputs["lines_by_period"][0]
        demand = indicative_inputs["demands"][0]
        generators = indicative_inputs["fleets"][0]
        indicative_reserve_requirement = indicative_inputs["reserve_requirements"][0]
        total_fc = _sum(p.demand_forecast_mw)
        total_now = _sum(demand)
        renewable_fc = _sum(p.renewable_forecast_mw)
        renewable_now = sum(g.capacity_mw for g in generators if g.technology in {"wind", "solar"})
        alerts = _event_alerts(p)
        capacity_stress_watch = (
            self.capacity_contract_mw > 0
            and any(
                row["price"] >= self.capacity_call_price_threshold
                or row["reserve_price_per_mw_h"] >= 100.0
                or row["reserve_shortfall_mw"] > 0.1
                for row in lookahead
            )
        )
        if capacity_stress_watch:
            alerts.insert(0, {
                "severity": "high",
                "text": f"Capacity stress watch: {self.capacity_contract_mw:.0f} MW availability contract may be called",
            })
        return {
            "period": p.period,
            "label": p.label,
            "soc_mwh": self.rt_battery.soc_mwh,
            "soc_fraction": self.rt_battery.soc_mwh / self.rt_battery.energy_mwh,
            "charge_limit_mw": min(
                self.rt_battery.charge_limit_mw(PERIOD_HOURS),
                max(0.0, self.base_battery.power_mw - da.dynamic_response_mw),
            ),
            "discharge_limit_mw": min(
                self.rt_battery.discharge_limit_mw(PERIOD_HOURS),
                max(0.0, self.base_battery.power_mw - da.dynamic_response_mw),
            ),
            "day_ahead_price": da.price,
            "day_ahead_net_mw": da.net_mw,
            "day_ahead_charge_mw": da.charge_mw,
            "day_ahead_discharge_mw": da.discharge_mw,
            "indicative_price": indicative.nodal_prices[self.battery_node],
            "indicative_nodal_prices": indicative.nodal_prices,
            "congestion_risk": _congestion_risk(indicative, lines),
            "demand_forecast_mw": total_fc,
            "demand_nowcast_mw": total_now,
            "demand_revision_pct": (total_now / total_fc - 1.0) * 100.0 if total_fc else 0.0,
            "renewable_forecast_mw": renewable_fc,
            "renewable_nowcast_mw": renewable_now,
            "renewable_revision_pct": (renewable_now / renewable_fc - 1.0) * 100.0 if renewable_fc > 1.0 else 0.0,
            "alerts": alerts,
            "capacity_contract_mw": self.capacity_contract_mw,
            "capacity_stress_watch": capacity_stress_watch,
            "startup_units": self.da_schedule[self.current_period].startup_units,
            "committed_units": self.da_schedule[self.current_period].committed_units,
            "reserve_requirement_mw": indicative_reserve_requirement,
            "reserve_shortfall_mw": indicative.reserve_shortfall_mw,
            "indicative_reserve_price_per_mw_h": indicative.reserve_price_per_mw_h,
            "day_ahead_reserve_price_per_mw_h": self.da_schedule[self.current_period].reserve_price_per_mw_h,
            "day_ahead_reserve_mw": self.da_schedule[self.current_period].battery_reserve_mw,
            "dynamic_response_mw": da.dynamic_response_mw,
            "dynamic_response_price_per_mw_h": da.dynamic_response_price_per_mw_h,
            "dynamic_response_requirement_mw": da.dynamic_response_requirement_mw,
            "rt_lookahead_hours": len(lookahead) * PERIOD_HOURS,
            "lookahead": lookahead,
            "lookahead_continuation_price": indicative_inputs["continuation_price"],
        }

    def step_real_time(self, bid: PeriodBidPlan) -> dict:
        if self.phase != "real_time":
            raise ValueError("Game is not in real-time phase")
        if self.current_period >= len(self.scenario.periods):
            raise ValueError("All real-time periods have already cleared")
        bid.validate(self.base_battery.power_mw)

        p = self.scenario.periods[self.current_period]
        da_result = self.da_results[self.current_period]

        # Physical RT dispatch is the first interval of a rolling four-hour LP.
        # Period zero uses realised demand/renewables/outages; future intervals use
        # only revised forecasts. Generator ramps and BESS SOC therefore transmit
        # near-term scarcity/opportunity cost into the current LMP and reserve price.
        rolling_result, rolling_inputs = self._rolling_rt_clear(bid, use_actual_first=True)
        self._last_rt_lookahead = rolling_result
        result = rolling_result.first_period
        lines = rolling_inputs["lines_by_period"][0]
        rt_fleet = rolling_inputs["fleets"][0]

        stress_reference = None
        if self.capacity_contract_mw > 0:
            stress_reference = _reference_clear(
                self.scenario, p, p.demand_actual_mw, rt_fleet, lines,
                reserve_requirement_mw=_reserve_requirement(p.demand_actual_mw),
            )

        da_net_mw = da_result.battery_discharge_mw - da_result.battery_charge_mw
        rt_net_mw = result.battery_discharge_mw - result.battery_charge_mw
        p_da = da_result.nodal_prices[self.battery_node]
        p_rt = result.nodal_prices[self.battery_node]
        da_settlement = da_net_mw * p_da * PERIOD_HOURS
        rt_deviation = (rt_net_mw - da_net_mw) * p_rt * PERIOD_HOURS
        da_reserve_mw = self.da_schedule[self.current_period].battery_reserve_mw
        rt_reserve_mw = result.battery_reserve_mw
        da_reserve_settlement = da_reserve_mw * da_result.reserve_price_per_mw_h * PERIOD_HOURS
        rt_reserve_deviation = (rt_reserve_mw - da_reserve_mw) * result.reserve_price_per_mw_h * PERIOD_HOURS
        dynamic_mw = self.da_schedule[self.current_period].dynamic_response_mw
        dynamic_price = self.da_schedule[self.current_period].dynamic_response_price_per_mw_h
        dynamic_revenue = dynamic_mw * dynamic_price * PERIOD_HOURS
        dynamic_throughput = dynamic_mw * PERIOD_HOURS * _dynamic_response_activation_fraction(p)
        dynamic_degradation = dynamic_throughput * self.base_battery.degradation_cost_per_mwh
        energy_degradation = (
            result.battery_discharge_mw * PERIOD_HOURS * self.base_battery.degradation_cost_per_mwh
        )
        degradation = energy_degradation + dynamic_degradation

        capacity_call_mw = 0.0
        if stress_reference is not None:
            stressed = (
                stress_reference.nodal_prices[self.battery_node] >= self.capacity_call_price_threshold
                or stress_reference.reserve_price_per_mw_h >= 100.0
                or stress_reference.reserve_shortfall_mw > 0.1
                or sum(stress_reference.load_shed_mw.values()) > 0.01
            )
            if stressed:
                capacity_call_mw = self.capacity_contract_mw
        capacity_delivered_mw = min(
            capacity_call_mw, result.battery_discharge_mw + result.battery_reserve_mw
        )
        capacity_penalty = (
            max(0.0, capacity_call_mw - capacity_delivered_mw)
            * PERIOD_HOURS * self.capacity_penalty_per_mwh
        )
        daily_capacity_revenue = (
            self.capacity_contract_mw * 1000.0 * self.capacity_payment_per_kw_year / 365.0
        )
        capacity_revenue = daily_capacity_revenue / len(self.scenario.periods)
        net = (
            da_settlement + rt_deviation + da_reserve_settlement + rt_reserve_deviation
            + dynamic_revenue + capacity_revenue - capacity_penalty - degradation
        )

        # Congestion value measures how much of physical RT battery value came from
        # the battery's nodal spread relative to the simple mean system LMP.
        system_price = sum(result.nodal_prices.values()) / len(result.nodal_prices)
        congestion_value = rt_net_mw * (p_rt - system_price) * PERIOD_HOURS

        self.rt_battery.apply_dispatch(result.battery_charge_mw, result.battery_discharge_mw, PERIOD_HOURS)
        self.rt_battery.cumulative_throughput_mwh += dynamic_throughput
        outcome = PeriodOutcome(
            period=p.period,
            label=p.label,
            demand_mw=p.demand_actual_mw,
            demand_forecast_mw=p.demand_forecast_mw,
            renewable_forecast_mw=p.renewable_forecast_mw,
            renewable_actual_mw=p.renewable_actual_mw,
            day_ahead_prices=da_result.nodal_prices,
            real_time_prices=result.nodal_prices,
            battery_da_charge_mw=da_result.battery_charge_mw,
            battery_da_discharge_mw=da_result.battery_discharge_mw,
            battery_rt_charge_mw=result.battery_charge_mw,
            battery_rt_discharge_mw=result.battery_discharge_mw,
            soc_end_mwh=self.rt_battery.soc_mwh,
            da_settlement=da_settlement,
            rt_deviation_settlement=rt_deviation,
            degradation_cost=degradation,
            net_cashflow=net,
            line_flows_mw=result.line_flows_mw,
            generator_dispatch_mw=result.generator_dispatch_mw,
            reserve_price_per_mw_h=result.reserve_price_per_mw_h,
            reserve_shortfall_mw=result.reserve_shortfall_mw,
            battery_da_reserve_mw=da_reserve_mw,
            battery_rt_reserve_mw=rt_reserve_mw,
            da_reserve_settlement=da_reserve_settlement,
            rt_reserve_deviation_settlement=rt_reserve_deviation,
            capacity_call_mw=capacity_call_mw,
            capacity_delivered_mw=capacity_delivered_mw,
            capacity_revenue=capacity_revenue,
            capacity_penalty=capacity_penalty,
            dynamic_response_mw=dynamic_mw,
            dynamic_response_price_per_mw_h=dynamic_price,
            dynamic_response_revenue=dynamic_revenue,
            dynamic_response_throughput_mwh=dynamic_throughput,
            dynamic_response_degradation_cost=dynamic_degradation,
            congestion_value=congestion_value,
            rt_bid=asdict(bid),
        )
        self.outcomes.append(outcome)
        self.rt_bids.append(bid)
        for name, dispatch_mw in result.generator_dispatch_mw.items():
            self.rt_prev_dispatch[name] = dispatch_mw
        self.current_period += 1

        period_payload = outcome.to_dict()
        period_payload["system_price"] = system_price
        period_payload["events"] = _event_alerts(p)
        if capacity_call_mw > 0:
            shortfall = max(0.0, capacity_call_mw - capacity_delivered_mw)
            period_payload["events"].insert(0, {
                "severity": "high" if shortfall > 0.1 else "medium",
                "text": f"Capacity call {capacity_call_mw:.0f} MW · delivered/available {capacity_delivered_mw:.0f} MW"
                        + (f" · shortfall {shortfall:.0f} MW" if shortfall > 0.1 else ""),
            })
        period_payload["line_capacities_mw"] = {line.name: line.capacity_mw for line in lines}
        period_payload["rolling_horizon"] = self._lookahead_payload(rolling_result, rolling_inputs)
        period_payload["rolling_horizon_hours"] = len(rolling_inputs["labels"]) * PERIOD_HOURS

        if self.current_period >= len(self.scenario.periods):
            self.phase = "complete"
            return {
                "game_id": self.id,
                "phase": self.phase,
                "cleared_period": period_payload,
                "briefing": None,
                "progress": self.progress_payload(),
                "result": self.final_result(),
            }

        return {
            "game_id": self.id,
            "phase": self.phase,
            "cleared_period": period_payload,
            "briefing": self.current_briefing(),
            "progress": self.progress_payload(),
        }

    def progress_payload(self) -> dict:
        cash = sum(x.net_cashflow for x in self.outcomes)
        energy = sum(x.da_settlement + x.rt_deviation_settlement for x in self.outcomes)
        degradation = sum(x.degradation_cost for x in self.outcomes)
        reserve = sum(x.da_reserve_settlement + x.rt_reserve_deviation_settlement for x in self.outcomes)
        dynamic_response = sum(x.dynamic_response_revenue for x in self.outcomes)
        capacity_revenue = sum(x.capacity_revenue for x in self.outcomes)
        capacity_penalties = sum(x.capacity_penalty for x in self.outcomes)
        congestion = sum(getattr(x, "congestion_value", 0.0) for x in self.outcomes)
        return {
            "periods_cleared": len(self.outcomes),
            "periods_total": len(self.scenario.periods),
            "cash_pnl": cash,
            "energy_settlement": energy,
            "degradation_cost": degradation,
            "reserve_settlement": reserve,
            "dynamic_response_revenue": dynamic_response,
            "capacity_revenue": capacity_revenue,
            "capacity_penalties": capacity_penalties,
            "capacity_net": capacity_revenue - capacity_penalties,
            "congestion_value": congestion,
            "soc_mwh": self.rt_battery.soc_mwh,
            "equivalent_cycles": self.rt_battery.cumulative_throughput_mwh / (2 * self.rt_battery.energy_mwh),
        }

    def _counterfactual_rt_prices(self) -> List[float]:
        if self._reference_rt_prices is None:
            prices = []
            prev = _initial_rt_dispatch_state(self.scenario.periods[0].rt_generators)
            for t, p in enumerate(self.scenario.periods):
                lines = _scaled_lines(self.scenario.lines, p.rt_line_capacity_multiplier)
                fleet = p.rt_generators
                if self.uc_result is not None:
                    fleet = _apply_rt_ramp_limits(
                        p.rt_generators,
                        self.uc_result.commitment[t],
                        prev,
                        self.da_schedule[t].startup_units,
                    )
                result = _reference_clear(
                    self.scenario, p, p.demand_actual_mw, fleet, lines,
                    reserve_requirement_mw=_reserve_requirement(p.demand_actual_mw),
                )
                prices.append(result.nodal_prices[self.battery_node])
                prev.update(result.generator_dispatch_mw)
            self._reference_rt_prices = prices
        return self._reference_rt_prices

    def final_result(self) -> dict:
        if self.phase != "complete":
            raise ValueError("The real-time day has not finished")
        progress = self.progress_payload()
        ref_prices = self._counterfactual_rt_prices()
        # Mark carry at the closing DA reference rather than the realised final spot.
        # This prevents an ex-post scarcity event near midnight from retroactively
        # revaluing the energy the player happened to start the day with.
        da_close_prices = [x.price for x in self.da_schedule[-4:]]
        terminal_price = sum(da_close_prices) / len(da_close_prices)
        terminal_unit_value = _terminal_inventory_value_per_soc(terminal_price, self.base_battery)
        inventory_mark = (self.rt_battery.soc_mwh - self.base_battery.soc_mwh) * terminal_unit_value
        economic_pnl = progress["cash_pnl"] + inventory_mark
        oracle = _oracle_benchmark(ref_prices, self.base_battery, terminal_price=terminal_price)
        oracle_pnl = oracle["economic_pnl"]
        reserve_pnl = progress["reserve_settlement"]
        dynamic_response_pnl = progress["dynamic_response_revenue"]
        capacity_net = progress["capacity_net"]
        energy_economic_pnl = economic_pnl - reserve_pnl - dynamic_response_pnl - capacity_net
        capture = energy_economic_pnl / oracle_pnl if oracle_pnl > 1e-6 else None

        rt_prices = [x.real_time_prices[self.battery_node] for x in self.outcomes]
        da_prices = [x.day_ahead_prices[self.battery_node] for x in self.outcomes]
        price_forecast_mae = sum(abs(a - b) for a, b in zip(rt_prices, da_prices)) / len(rt_prices)
        congestion_periods = 0
        for p, outcome in zip(self.scenario.periods, self.outcomes):
            lines = _scaled_lines(self.scenario.lines, p.rt_line_capacity_multiplier)
            caps = {line.name: line.capacity_mw for line in lines}
            if any(abs(outcome.line_flows_mw.get(name, 0.0)) >= 0.97 * cap for name, cap in caps.items()):
                congestion_periods += 1

        return {
            "progress": progress,
            "economic_pnl": economic_pnl,
            "inventory_mark": inventory_mark,
            "terminal_reference_price": terminal_price,
            "perfect_foresight": oracle,
            "opportunity_capture": capture,
            "energy_economic_pnl": energy_economic_pnl,
            "reserve_pnl": reserve_pnl,
            "dynamic_response_pnl": dynamic_response_pnl,
            "capacity_net": capacity_net,
            "attribution": {
                "day_ahead_settlement": sum(x.da_settlement for x in self.outcomes),
                "real_time_deviation": sum(x.rt_deviation_settlement for x in self.outcomes),
                "day_ahead_reserve": sum(x.da_reserve_settlement for x in self.outcomes),
                "real_time_reserve_deviation": sum(x.rt_reserve_deviation_settlement for x in self.outcomes),
                "dynamic_response_revenue": sum(x.dynamic_response_revenue for x in self.outcomes),
                "dynamic_response_degradation": -sum(x.dynamic_response_degradation_cost for x in self.outcomes),
                "dynamic_response_throughput_mwh": sum(x.dynamic_response_throughput_mwh for x in self.outcomes),
                "dynamic_response_awarded_periods": sum(1 for x in self.outcomes if x.dynamic_response_mw > 0.05),
                "capacity_revenue": sum(x.capacity_revenue for x in self.outcomes),
                "capacity_penalties": -sum(x.capacity_penalty for x in self.outcomes),
                "capacity_calls": sum(1 for x in self.outcomes if x.capacity_call_mw > 0.05),
                "capacity_shortfall_mwh": sum(max(0.0, x.capacity_call_mw - x.capacity_delivered_mw) * PERIOD_HOURS for x in self.outcomes),
                "degradation": -sum(x.degradation_cost for x in self.outcomes),
                "inventory_mark": inventory_mark,
                "congestion_value": sum(getattr(x, "congestion_value", 0.0) for x in self.outcomes),
                "price_forecast_mae": price_forecast_mae,
                "congested_periods": congestion_periods,
                "reserve_scarcity_periods": sum(1 for x in self.outcomes if x.reserve_price_per_mw_h >= 50.0),
                "reserve_shortfall_mwh": sum(x.reserve_shortfall_mw * PERIOD_HOURS for x in self.outcomes),
                "average_reserve_price_per_mw_h": sum(x.reserve_price_per_mw_h for x in self.outcomes) / len(self.outcomes),
            },
            "periods": [x.to_dict() for x in self.outcomes],
            "counterfactual_rt_prices": ref_prices,
            "market_context": {
                "known_events": list(self.scenario.known_events),
                "day_ahead_uplift": self._da_uplift_summary(),
                "rolling_rt": {
                    "enabled": True,
                    "lookahead_periods": self.rt_lookahead_periods,
                    "lookahead_hours": self.rt_lookahead_periods * PERIOD_HOURS,
                    "future_information": "revised forecasts only; future realised demand, renewables, outages and line derates remain hidden",
                },
                "dynamic_response": {
                    "enabled": True,
                    "market": "stylised pay-as-clear availability auction",
                    "response_duration_hours": self.config.dynamic_response_hours,
                    "physical_rule": "symmetric inverter headroom plus upper/lower SOC buffer",
                },
            },
            "metadata": self._metadata(),
        }

    def _da_uplift_summary(self) -> dict:
        """Offer-based make-whole for committed thermal units.

        Startup/no-load costs are non-convex and therefore are not generally
        recovered through a fixed-commitment LMP alone. We report the aggregate
        daily shortfall as system uplift rather than hiding those economics.
        This is market context only: it is not charged to the player in v0.3.
        """
        if self.uc_result is None or not self.da_results:
            return {"total_uplift": 0.0, "total_startup_cost": 0.0, "by_generator": []}

        accounts: Dict[str, dict] = {}
        for t, (period, result) in enumerate(zip(self.scenario.periods, self.da_results)):
            for g in period.da_generators:
                if g.technology not in COMMITMENT_TECH:
                    continue
                row = accounts.setdefault(g.name, {
                    "generator": g.name, "node": g.node, "technology": g.technology,
                    "energy_revenue": 0.0, "reserve_revenue": 0.0,
                    "offer_energy_cost": 0.0, "no_load_cost": 0.0,
                    "startup_cost": 0.0, "starts": 0, "dispatch_mwh": 0.0,
                })
                dispatch = result.generator_dispatch_mw.get(g.name, 0.0)
                reserve = result.generator_reserve_mw.get(g.name, 0.0)
                on = bool(self.uc_result.commitment[t].get(g.name, False))
                started = bool(self.uc_result.startup[t].get(g.name, False))
                row["dispatch_mwh"] += dispatch * PERIOD_HOURS
                row["energy_revenue"] += dispatch * result.nodal_prices[g.node] * PERIOD_HOURS
                row["reserve_revenue"] += reserve * result.reserve_price_per_mw_h * PERIOD_HOURS
                row["offer_energy_cost"] += dispatch * g.offer_price_per_mwh * PERIOD_HOURS
                if on:
                    row["no_load_cost"] += g.no_load_cost_per_hour * PERIOD_HOURS
                if started:
                    row["startup_cost"] += g.startup_cost
                    row["starts"] += 1

        rows = []
        total_uplift = 0.0
        total_startup = 0.0
        for row in accounts.values():
            revenue = row["energy_revenue"] + row["reserve_revenue"]
            offered_cost = row["offer_energy_cost"] + row["no_load_cost"] + row["startup_cost"]
            uplift = max(0.0, offered_cost - revenue)
            row["total_market_revenue"] = revenue
            row["total_offered_cost"] = offered_cost
            row["uplift"] = uplift
            total_uplift += uplift
            total_startup += row["startup_cost"]
            rows.append(row)
        rows.sort(key=lambda x: x["uplift"], reverse=True)
        return {
            "total_uplift": total_uplift,
            "total_startup_cost": total_startup,
            "by_generator": rows,
        }

    def _da_summary(self) -> dict:
        net_mwh = sum(x.net_mw * PERIOD_HOURS for x in self.da_schedule)
        gross = sum(x.net_mw * x.price * PERIOD_HOURS for x in self.da_schedule)
        uplift = self._da_uplift_summary()
        return {
            "net_scheduled_mwh": net_mwh,
            "gross_da_settlement_if_held": gross,
            "ending_shadow_soc_mwh": self.da_battery.soc_mwh,
            "charge_periods": sum(1 for x in self.da_schedule if x.charge_mw > 0.05),
            "discharge_periods": sum(1 for x in self.da_schedule if x.discharge_mw > 0.05),
            "thermal_startups": sum(len(x.startup_units) for x in self.da_schedule),
            "reserve_shortfall_mwh": sum(x.reserve_shortfall_mw * PERIOD_HOURS for x in self.da_schedule),
            "average_reserve_price_per_mw_h": sum(x.reserve_price_per_mw_h for x in self.da_schedule) / len(self.da_schedule),
            "battery_reserve_mwh_equivalent": sum(x.battery_reserve_mw * PERIOD_HOURS for x in self.da_schedule),
            "battery_reserve_periods": sum(1 for x in self.da_schedule if x.battery_reserve_mw > 0.05),
            "dynamic_response_mwh_equivalent": sum(x.dynamic_response_mw * PERIOD_HOURS for x in self.da_schedule),
            "dynamic_response_periods": sum(1 for x in self.da_schedule if x.dynamic_response_mw > 0.05),
            "average_dynamic_response_price_per_mw_h": sum(
                x.dynamic_response_price_per_mw_h for x in self.da_schedule
            ) / len(self.da_schedule),
            "dynamic_response_da_revenue": sum(
                x.dynamic_response_mw * x.dynamic_response_price_per_mw_h * PERIOD_HOURS
                for x in self.da_schedule
            ),
            "system_uplift": uplift["total_uplift"],
            "system_startup_cost": uplift["total_startup_cost"],
            "system_uplift_by_generator": uplift["by_generator"],
        }

    def _metadata(self) -> dict:
        return {
            "scenario": self.scenario.name,
            "seed": self.scenario.seed,
            "battery_node": self.battery_node,
            "battery_power_mw": self.base_battery.power_mw,
            "battery_energy_mwh": self.base_battery.energy_mwh,
            "starting_soc_mwh": self.base_battery.soc_mwh,
            "round_trip_efficiency": self.base_battery.round_trip_efficiency,
            "degradation_cost_per_mwh": self.base_battery.degradation_cost_per_mwh,
            "capacity_contract_mw": self.capacity_contract_mw,
            "capacity_payment_per_kw_year": self.capacity_payment_per_kw_year,
            "capacity_penalty_per_mwh": self.capacity_penalty_per_mwh,
            "capacity_call_price_threshold": self.capacity_call_price_threshold,
            "carbon_price_per_t": self.scenario.carbon_price_per_t,
            "gas_price_index": self.scenario.gas_price_index,
            "line_capacities_mw": {line.name: line.capacity_mw for line in self.scenario.lines},
            "period_hours": PERIOD_HOURS,
            "unit_commitment_enabled": True,
            "rolling_rt_enabled": True,
            "dynamic_response_enabled": True,
            "rt_lookahead_periods": self.rt_lookahead_periods,
            "rt_lookahead_hours": self.rt_lookahead_periods * PERIOD_HOURS,
        }
