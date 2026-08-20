from __future__ import annotations

from dataclasses import replace
from typing import Dict, Optional

from .market import DCOPFMarket, MarketConfig
from .models import BatteryAsset, BatteryBid, PeriodOutcome, SimulationSummary, Line
from .scenario import Scenario, build_stylised_gb_scenario


def _scaled_lines(lines: list[Line], multipliers: Dict[str, float]) -> list[Line]:
    return [
        replace(line, capacity_mw=line.capacity_mw * multipliers.get(line.name, 1.0))
        for line in lines
    ]


def run_day(
    seed: int = 7,
    battery_power_mw: float = 100.0,
    battery_duration_hours: float = 2.0,
    starting_soc_fraction: float = 0.5,
    charge_bid_price: float = 35.0,
    discharge_offer_price: float = 105.0,
    battery_node: str = "Central",
    degradation_cost_per_mwh: float = 7.0,
    round_trip_efficiency: float = 0.88,
    scenario: Optional[Scenario] = None,
) -> SimulationSummary:
    scenario = scenario or build_stylised_gb_scenario(seed=seed)
    duration_hours = 0.5
    energy_mwh = battery_power_mw * battery_duration_hours
    starting_soc = energy_mwh * starting_soc_fraction

    rt_battery = BatteryAsset(
        name="Player BESS",
        node=battery_node,
        power_mw=battery_power_mw,
        energy_mwh=energy_mwh,
        soc_mwh=starting_soc,
        round_trip_efficiency=round_trip_efficiency,
        degradation_cost_per_mwh=degradation_cost_per_mwh,
    )
    da_battery = replace(rt_battery)

    bid = BatteryBid(
        max_charge_price_per_mwh=charge_bid_price,
        min_discharge_price_per_mwh=discharge_offer_price,
    )
    bid.validate()

    config = MarketConfig(value_of_lost_load_per_mwh=5000.0, emergency_absorption_cost_per_mwh=250.0)
    da_market = DCOPFMarket(scenario.nodes, scenario.lines, config)

    da_results = []
    # The DA auction is financial in the EMG, but we propagate a shadow DA SOC path
    # so the player's forward schedule remains physically plausible.
    for period in scenario.periods:
        result = da_market.clear(
            period.demand_forecast_mw,
            period.da_generators,
            duration_hours,
            battery=da_battery,
            battery_bid=bid,
        )
        if not result.success:
            raise RuntimeError(f"Day-ahead clearing failed in {period.label}: {result.status}")
        da_results.append(result)
        da_battery.apply_dispatch(result.battery_charge_mw, result.battery_discharge_mw, duration_hours)

    outcomes = []
    total_energy_revenue = 0.0
    total_degradation = 0.0

    for period, da_result in zip(scenario.periods, da_results):
        rt_lines = _scaled_lines(scenario.lines, period.rt_line_capacity_multiplier)
        rt_market = DCOPFMarket(scenario.nodes, rt_lines, config)
        rt_result = rt_market.clear(
            period.demand_actual_mw,
            period.rt_generators,
            duration_hours,
            battery=rt_battery,
            battery_bid=bid,
        )
        if not rt_result.success:
            raise RuntimeError(f"Real-time clearing failed in {period.label}: {rt_result.status}")

        da_net_mw = da_result.battery_discharge_mw - da_result.battery_charge_mw
        rt_net_mw = rt_result.battery_discharge_mw - rt_result.battery_charge_mw
        p_da = da_result.nodal_prices[battery_node]
        p_rt = rt_result.nodal_prices[battery_node]

        da_settlement = da_net_mw * p_da * duration_hours
        rt_deviation = (rt_net_mw - da_net_mw) * p_rt * duration_hours
        degradation = rt_result.battery_discharge_mw * duration_hours * degradation_cost_per_mwh
        net = da_settlement + rt_deviation - degradation

        rt_battery.apply_dispatch(rt_result.battery_charge_mw, rt_result.battery_discharge_mw, duration_hours)

        total_energy_revenue += da_settlement + rt_deviation
        total_degradation += degradation

        outcomes.append(
            PeriodOutcome(
                period=period.period,
                label=period.label,
                demand_mw=period.demand_actual_mw,
                demand_forecast_mw=period.demand_forecast_mw,
                renewable_forecast_mw=period.renewable_forecast_mw,
                renewable_actual_mw=period.renewable_actual_mw,
                day_ahead_prices=da_result.nodal_prices,
                real_time_prices=rt_result.nodal_prices,
                battery_da_charge_mw=da_result.battery_charge_mw,
                battery_da_discharge_mw=da_result.battery_discharge_mw,
                battery_rt_charge_mw=rt_result.battery_charge_mw,
                battery_rt_discharge_mw=rt_result.battery_discharge_mw,
                soc_end_mwh=rt_battery.soc_mwh,
                da_settlement=da_settlement,
                rt_deviation_settlement=rt_deviation,
                degradation_cost=degradation,
                net_cashflow=net,
                line_flows_mw=rt_result.line_flows_mw,
            )
        )

    equivalent_cycles = rt_battery.cumulative_throughput_mwh / (2 * rt_battery.energy_mwh)
    total_net = total_energy_revenue - total_degradation
    return SimulationSummary(
        periods=outcomes,
        total_energy_revenue=total_energy_revenue,
        total_degradation_cost=total_degradation,
        total_net_cashflow=total_net,
        equivalent_cycles=equivalent_cycles,
        final_soc_mwh=rt_battery.soc_mwh,
        metadata={
            "scenario": scenario.name,
            "seed": scenario.seed,
            "battery_node": battery_node,
            "battery_power_mw": battery_power_mw,
            "battery_energy_mwh": energy_mwh,
            "starting_soc_mwh": starting_soc,
            "round_trip_efficiency": round_trip_efficiency,
            "carbon_price_per_t": scenario.carbon_price_per_t,
            "gas_price_index": scenario.gas_price_index,
            "charge_bid_price": charge_bid_price,
            "discharge_offer_price": discharge_offer_price,
            "line_capacities_mw": {line.name: line.capacity_mw for line in scenario.lines},
        },
    )
