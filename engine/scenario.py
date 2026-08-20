from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List
import math
import random

from .models import Generator, Line, Node


@dataclass
class PeriodInputs:
    period: int
    label: str
    demand_forecast_mw: Dict[str, float]
    demand_actual_mw: Dict[str, float]
    renewable_forecast_mw: Dict[str, float]
    renewable_actual_mw: Dict[str, float]
    da_generators: List[Generator]
    rt_generators: List[Generator]
    rt_line_capacity_multiplier: Dict[str, float]


@dataclass
class Scenario:
    name: str
    seed: int
    carbon_price_per_t: float
    gas_price_index: float
    nodes: List[Node]
    lines: List[Line]
    periods: List[PeriodInputs]
    known_events: List[dict]


def _gaussian_bump(hour: float, center: float, width: float) -> float:
    return math.exp(-((hour - center) ** 2) / (2 * width ** 2))


def _demand_shape(hour: float) -> float:
    morning = 0.28 * _gaussian_bump(hour, 8.0, 2.0)
    evening = 0.55 * _gaussian_bump(hour, 18.5, 2.5)
    overnight = -0.20 * _gaussian_bump(hour, 3.0, 2.5)
    return 0.84 + morning + evening + overnight


def _solar_shape(hour: float) -> float:
    if hour < 5.5 or hour > 20.5:
        return 0.0
    x = (hour - 5.5) / 15.0 * math.pi
    return max(0.0, math.sin(x)) ** 1.6


def _wind_shape(hour: float, phase: float) -> float:
    return max(0.08, min(0.95, 0.47 + 0.17 * math.sin(hour / 24 * 2 * math.pi + phase)))


def _make_fleet(
    renewable_north: float,
    renewable_south: float,
    carbon_price: float,
    gas_index: float,
    tightness: float,
    rng: random.Random,
    real_time: bool,
) -> List[Generator]:
    # Costs are stylised rather than calibrated to a specific historical market.
    gas_ccgt_cost = 38.0 * gas_index + 0.36 * carbon_price + 4.0
    gas_ocgt_cost = 58.0 * gas_index + 0.52 * carbon_price + 6.0
    coal_cost = 30.0 + 0.86 * carbon_price + 5.0

    # Strategic markups rise as the system tightens. Real-time can be somewhat more volatile.
    rt_vol = 1.15 if real_time else 1.0
    tight_markup = max(0.0, (tightness - 0.50) * 500.0) * rt_vol

    fleet = [
        Generator("North Nuclear", "North", "nuclear", 780, 13, 14, min_output_mw=600),
        Generator("North Wind", "North", "wind", renewable_north, -22, -22),
        Generator("North CCGT", "North", "ccgt", 720, gas_ccgt_cost, gas_ccgt_cost + 7 + 0.35 * tight_markup),
        Generator("Central CCGT A", "Central", "ccgt", 650, gas_ccgt_cost, gas_ccgt_cost + 8 + 0.55 * tight_markup),
        Generator("Central CCGT B", "Central", "ccgt", 520, gas_ccgt_cost + 2, gas_ccgt_cost + 12 + 0.70 * tight_markup),
        Generator("Central Biomass", "Central", "biomass", 230, 46, 50 + 0.20 * tight_markup),
        Generator("South Solar", "South", "solar", renewable_south, -15, -15),
        Generator("South CCGT", "South", "ccgt", 900, gas_ccgt_cost + 1, gas_ccgt_cost + 10 + 0.60 * tight_markup),
        Generator("South Coal", "South", "coal", 430, coal_cost, coal_cost + 8 + 0.40 * tight_markup),
        Generator("Central OCGT", "Central", "ocgt", 310, gas_ocgt_cost, gas_ocgt_cost + 28 + 1.10 * tight_markup),
        Generator("South OCGT", "South", "ocgt", 450, gas_ocgt_cost + 2, gas_ocgt_cost + 35 + 1.25 * tight_markup),
        Generator("Interconnector", "South", "import", 360, 72, 75 + 0.25 * tight_markup),
    ]

    physical = {
        "North Nuclear": dict(minimum_stable_mw=600, reserve_offer_cost_per_mw_h=2.0, startup_cost=65000, no_load_cost_per_hour=6000, ramp_up_mw_per_hour=100, ramp_down_mw_per_hour=100, min_up_periods=16, min_down_periods=8, initially_on=True, initial_output_mw=650),
        "North CCGT": dict(minimum_stable_mw=180, reserve_offer_cost_per_mw_h=3.0, startup_cost=18000, no_load_cost_per_hour=1700, ramp_up_mw_per_hour=360, ramp_down_mw_per_hour=420, min_up_periods=4, min_down_periods=2, initially_on=True, initial_output_mw=300),
        "Central CCGT A": dict(minimum_stable_mw=160, reserve_offer_cost_per_mw_h=3.0, startup_cost=16500, no_load_cost_per_hour=1550, ramp_up_mw_per_hour=390, ramp_down_mw_per_hour=440, min_up_periods=4, min_down_periods=2, initially_on=True, initial_output_mw=280),
        "Central CCGT B": dict(minimum_stable_mw=130, reserve_offer_cost_per_mw_h=3.5, startup_cost=15000, no_load_cost_per_hour=1400, ramp_up_mw_per_hour=350, ramp_down_mw_per_hour=400, min_up_periods=4, min_down_periods=2, initially_on=False, initial_output_mw=0),
        "Central Biomass": dict(minimum_stable_mw=95, reserve_offer_cost_per_mw_h=3.0, startup_cost=12000, no_load_cost_per_hour=900, ramp_up_mw_per_hour=120, ramp_down_mw_per_hour=140, min_up_periods=6, min_down_periods=4, initially_on=True, initial_output_mw=120),
        "South CCGT": dict(minimum_stable_mw=220, reserve_offer_cost_per_mw_h=3.0, startup_cost=21000, no_load_cost_per_hour=1900, ramp_up_mw_per_hour=420, ramp_down_mw_per_hour=480, min_up_periods=4, min_down_periods=2, initially_on=True, initial_output_mw=350),
        "South Coal": dict(minimum_stable_mw=180, reserve_offer_cost_per_mw_h=4.5, startup_cost=32000, no_load_cost_per_hour=2600, ramp_up_mw_per_hour=110, ramp_down_mw_per_hour=130, min_up_periods=8, min_down_periods=6, initially_on=False, initial_output_mw=0),
        "Central OCGT": dict(minimum_stable_mw=45, reserve_offer_cost_per_mw_h=7.0, startup_cost=2800, no_load_cost_per_hour=260, ramp_up_mw_per_hour=1200, ramp_down_mw_per_hour=1200, min_up_periods=1, min_down_periods=1, initially_on=False, initial_output_mw=0),
        "South OCGT": dict(minimum_stable_mw=60, reserve_offer_cost_per_mw_h=7.0, startup_cost=3400, no_load_cost_per_hour=320, ramp_up_mw_per_hour=1400, ramp_down_mw_per_hour=1400, min_up_periods=1, min_down_periods=1, initially_on=False, initial_output_mw=0),
    }
    for g in fleet:
        for key, value in physical.get(g.name, {}).items():
            setattr(g, key, value)

    return fleet


def build_stylised_gb_scenario(seed: int = 7, periods: int = 48, start_hour: int = 0) -> Scenario:
    rng = random.Random(seed)
    nodes = [Node("North"), Node("Central"), Node("South")]
    lines = [
        Line("North-Central", "North", "Central", 900, susceptance=11.0),
        Line("Central-South", "Central", "South", 920, susceptance=10.0),
        Line("North-South", "North", "South", 700, susceptance=5.5),
    ]

    carbon_price = round(rng.uniform(42, 78), 2)
    gas_index = round(rng.uniform(0.86, 1.22), 3)
    wind_phase = rng.uniform(0, 2 * math.pi)

    known_events: List[dict] = []
    planned_maintenance: Dict[str, Dict[str, float]] = {}
    if rng.random() < 0.78:
        unit = rng.choice([
            "North CCGT", "Central CCGT A", "Central CCGT B",
            "Central Biomass", "South CCGT", "South Coal",
        ])
        start_p = rng.randint(8, 30)
        duration_p = rng.randint(5, 12)
        fraction = rng.choice([0.0, 0.55, 0.70])
        planned_maintenance[unit] = {
            "start": float(start_p), "end": float(min(periods, start_p + duration_p)), "fraction": fraction
        }
        start_label = (datetime(2026, 1, 15, start_hour, 0) + timedelta(minutes=30 * start_p)).strftime("%H:%M")
        end_label = (datetime(2026, 1, 15, start_hour, 0) + timedelta(minutes=30 * min(periods, start_p + duration_p))).strftime("%H:%M")
        action = "offline" if fraction == 0 else f"derated to {fraction:.0%}"
        known_events.append({
            "type": "planned_maintenance",
            "unit": unit,
            "start_period": start_p,
            "end_period": min(periods, start_p + duration_p),
            "available_fraction": fraction,
            "severity": "medium",
            "text": f"Planned maintenance: {unit} {action} from {start_label} to {end_label}",
        })

    node_base = {"North": 720.0, "Central": 1260.0, "South": 1440.0}
    period_inputs: List[PeriodInputs] = []
    start = datetime(2026, 1, 15, start_hour, 0)

    # Slow-moving error terms create realistic persistence in forecast misses.
    demand_error_state = {n: 0.0 for n in node_base}
    wind_error = 0.0
    solar_error = 0.0
    # Forced outages persist for multiple settlement periods instead of flickering
    # independently each half-hour. State is keyed by generator name.
    forced_outage_state: Dict[str, Dict[str, float]] = {}

    for p in range(periods):
        hour = (start_hour + p * 0.5) % 24
        label = (start + timedelta(minutes=30 * p)).strftime("%H:%M")
        shape = _demand_shape(hour)

        demand_forecast: Dict[str, float] = {}
        demand_actual: Dict[str, float] = {}
        for node, base in node_base.items():
            nodal_bias = {"North": 0.96, "Central": 1.0, "South": 1.04}[node]
            fc = base * shape * nodal_bias
            demand_error_state[node] = 0.72 * demand_error_state[node] + rng.gauss(0, 0.028)
            actual = fc * max(0.82, 1.0 + demand_error_state[node])
            demand_forecast[node] = round(fc, 2)
            demand_actual[node] = round(actual, 2)

        wind_cf_fc = _wind_shape(hour, wind_phase)
        wind_error = 0.76 * wind_error + rng.gauss(0, 0.095)
        wind_cf_actual = max(0.02, min(1.0, wind_cf_fc + wind_error))

        solar_cf_fc = _solar_shape(hour)
        solar_error = 0.55 * solar_error + rng.gauss(0, 0.06)
        solar_cf_actual = max(0.0, min(1.0, solar_cf_fc * (1.0 + solar_error)))

        renew_fc = {
            "North": round(1500 * wind_cf_fc, 2),
            "Central": 0.0,
            "South": round(1050 * solar_cf_fc, 2),
        }
        renew_actual = {
            "North": round(1500 * wind_cf_actual, 2),
            "Central": 0.0,
            "South": round(1050 * solar_cf_actual, 2),
        }

        total_fc_load = sum(demand_forecast.values())
        total_fc_ren = sum(renew_fc.values())
        thermal_nameplate = 5350.0
        tightness_fc = max(0.0, (total_fc_load - total_fc_ren) / thermal_nameplate)

        total_rt_load = sum(demand_actual.values())
        total_rt_ren = sum(renew_actual.values())
        tightness_rt = max(0.0, (total_rt_load - total_rt_ren) / thermal_nameplate)

        da_fleet = _make_fleet(
            renew_fc["North"], renew_fc["South"], carbon_price, gas_index, tightness_fc, rng, False
        )
        rt_fleet = _make_fleet(
            renew_actual["North"], renew_actual["South"], carbon_price, gas_index, tightness_rt, rng, True
        )

        for fleet in (da_fleet, rt_fleet):
            for g in fleet:
                maint = planned_maintenance.get(g.name)
                if maint is not None and maint["start"] <= p < maint["end"]:
                    g.available_fraction = min(g.available_fraction, float(maint["fraction"]))
                    g.min_output_mw = min(g.min_output_mw, g.available_capacity_mw)

        for g in rt_fleet:
            if g.technology not in {"ccgt", "ocgt", "coal", "nuclear", "biomass"}:
                continue
            state = forced_outage_state.get(g.name)
            if state is None:
                r = rng.random()
                if r < 0.003:
                    state = {"remaining": float(rng.randint(2, 8)), "fraction": 0.0}
                    forced_outage_state[g.name] = state
                elif r < 0.011:
                    state = {"remaining": float(rng.randint(2, 6)), "fraction": rng.uniform(0.45, 0.8)}
                    forced_outage_state[g.name] = state
            if state is not None:
                g.available_fraction = float(state["fraction"])
                g.min_output_mw = min(g.min_output_mw, g.available_capacity_mw)
                state["remaining"] -= 1.0
                if state["remaining"] <= 0:
                    forced_outage_state.pop(g.name, None)

        rt_line_mult = {line.name: 1.0 for line in lines}
        # Rare, material transmission derates create congestion surprises.
        if rng.random() < 0.035:
            chosen = rng.choice(lines)
            rt_line_mult[chosen.name] = rng.uniform(0.35, 0.7)

        period_inputs.append(
            PeriodInputs(
                period=p,
                label=label,
                demand_forecast_mw=demand_forecast,
                demand_actual_mw=demand_actual,
                renewable_forecast_mw=renew_fc,
                renewable_actual_mw=renew_actual,
                da_generators=da_fleet,
                rt_generators=rt_fleet,
                rt_line_capacity_multiplier=rt_line_mult,
            )
        )

    return Scenario(
        name="Stylised GB three-zone market",
        seed=seed,
        carbon_price_per_t=carbon_price,
        gas_price_index=gas_index,
        nodes=nodes,
        lines=lines,
        periods=period_inputs,
        known_events=known_events,
    )
