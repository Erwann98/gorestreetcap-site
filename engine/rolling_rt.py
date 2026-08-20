from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

import numpy as np
from scipy.optimize import linprog

from .market import MarketConfig
from .models import BatteryAsset, BatteryBid, DispatchResult, Generator, Line, Node
from .unit_commitment import RESERVE_TECH


@dataclass
class RollingRTResult:
    success: bool
    status: str
    objective_value: float
    first_period: DispatchResult
    horizon_prices: List[Dict[str, float]]
    horizon_reserve_prices: List[float]
    horizon_battery_charge_mw: List[float]
    horizon_battery_discharge_mw: List[float]
    horizon_battery_reserve_mw: List[float]
    horizon_soc_mwh: List[float]
    horizon_line_flows_mw: List[Dict[str, float]]
    horizon_reserve_shortfall_mw: List[float]


class RollingRealTimeMarket:
    """Short-horizon multi-period RT economic dispatch and pricing LP.

    The commitment decision is taken as given. Within that commitment the LP
    co-optimises energy, operating reserve, network flows and the player's BESS
    across a rolling horizon. Generator ramp limits and BESS SOC couple periods,
    so the first-period LMP reflects near-term physical opportunity costs rather
    than treating each half-hour as an isolated merit-order clear.
    """

    def __init__(
        self,
        nodes: Sequence[Node],
        duration_hours: float = 0.5,
        config: Optional[MarketConfig] = None,
    ) -> None:
        self.nodes = list(nodes)
        self.node_names = [n.name for n in self.nodes]
        self.dt = duration_hours
        self.config = config or MarketConfig()

    def clear(
        self,
        *,
        demands: Sequence[Dict[str, float]],
        fleets: Sequence[Sequence[Generator]],
        lines_by_period: Sequence[Sequence[Line]],
        battery: BatteryAsset,
        battery_bids: Sequence[BatteryBid],
        reserve_requirements_mw: Sequence[float],
        previous_dispatch_mw: Dict[str, float],
        startup_units_by_period: Optional[Sequence[Sequence[str]]] = None,
        shutdown_units_by_period: Optional[Sequence[Sequence[str]]] = None,
        terminal_soc_value_per_mwh: float = 0.0,
    ) -> RollingRTResult:
        T = len(demands)
        if T == 0:
            raise ValueError("Rolling horizon cannot be empty")
        if not (len(fleets) == len(lines_by_period) == len(battery_bids) == len(reserve_requirements_mw) == T):
            raise ValueError("All rolling-horizon inputs must have the same length")
        if any(set(d) != set(self.node_names) for d in demands):
            raise ValueError("Every demand period must include every node")

        names = [g.name for g in fleets[0]]
        if any([g.name for g in fleet] != names for fleet in fleets):
            raise ValueError("Generator ordering/names must remain constant across the rolling horizon")
        G = len(names)
        N = len(self.node_names)
        line_names = [line.name for line in lines_by_period[0]]
        if any([line.name for line in lines] != line_names for lines in lines_by_period):
            raise ValueError("Line ordering/names must remain constant across the rolling horizon")
        L = len(line_names)

        startup_sets = [set(x) for x in (startup_units_by_period or [[] for _ in range(T)])]
        shutdown_sets = [set(x) for x in (shutdown_units_by_period or [[] for _ in range(T)])]

        # Variable blocks per horizon period.
        cursor = 0
        def alloc(n: int) -> int:
            nonlocal cursor
            s = cursor
            cursor += n
            return s

        pg0 = alloc(T * G)
        rg0 = alloc(T * G)
        bd0 = alloc(T)
        bc0 = alloc(T)
        br0 = alloc(T)
        soc0 = alloc(T)
        rshort0 = alloc(T)
        shed0 = alloc(T * N)
        absorb0 = alloc(T * N)
        flow0 = alloc(T * L)
        theta0 = alloc(T * N)
        nvars = cursor

        def pg(t: int, g: int) -> int: return pg0 + t * G + g
        def rg(t: int, g: int) -> int: return rg0 + t * G + g
        def bd(t: int) -> int: return bd0 + t
        def bc(t: int) -> int: return bc0 + t
        def br(t: int) -> int: return br0 + t
        def soc(t: int) -> int: return soc0 + t
        def rshort(t: int) -> int: return rshort0 + t
        def shed(t: int, n: int) -> int: return shed0 + t * N + n
        def absorb(t: int, n: int) -> int: return absorb0 + t * N + n
        def flow(t: int, l: int) -> int: return flow0 + t * L + l
        def theta(t: int, n: int) -> int: return theta0 + t * N + n

        c = np.zeros(nvars)
        bounds: List[tuple[Optional[float], Optional[float]]] = [(None, None)] * nvars
        dt = self.dt

        # Bounds and objective coefficients.
        for t in range(T):
            for gi, gen in enumerate(fleets[t]):
                cap = max(0.0, gen.available_capacity_mw)
                pmin = min(max(0.0, gen.min_output_mw), cap)
                bounds[pg(t, gi)] = (pmin, cap)
                c[pg(t, gi)] = gen.offer_price_per_mwh * dt
                if gen.technology in RESERVE_TECH and cap > 0:
                    response_cap = min(cap, max(0.0, gen.ramp_up_mw_per_hour * self.config.reserve_response_hours))
                    bounds[rg(t, gi)] = (0.0, response_cap)
                    c[rg(t, gi)] = gen.reserve_offer_cost_per_mw_h * dt
                else:
                    bounds[rg(t, gi)] = (0.0, 0.0)

            bid = battery_bids[t]
            bid.validate()
            dynamic_mw = min(battery.power_mw, max(0.0, bid.dynamic_response_mw))
            charge_limit = max(0.0, battery.power_mw - dynamic_mw)
            discharge_limit = max(0.0, battery.power_mw - dynamic_mw)
            if bid.max_charge_mw is not None:
                charge_limit = min(charge_limit, bid.max_charge_mw)
            if bid.max_discharge_mw is not None:
                discharge_limit = min(discharge_limit, bid.max_discharge_mw)
            bounds[bc(t)] = (0.0, max(0.0, charge_limit))
            bounds[bd(t)] = (0.0, max(0.0, discharge_limit))
            bounds[br(t)] = (0.0, min(battery.power_mw, max(0.0, bid.max_reserve_mw)))
            bounds[soc(t)] = (battery.min_soc_mwh, battery.max_soc_mwh)
            c[bc(t)] = -bid.max_charge_price_per_mwh * dt
            c[bd(t)] = bid.min_discharge_price_per_mwh * dt
            c[br(t)] = bid.reserve_offer_price_per_mw_h * dt

            bounds[rshort(t)] = (0.0, None)
            c[rshort(t)] = self.config.reserve_shortfall_cost_per_mwh * dt
            for ni in range(N):
                bounds[shed(t, ni)] = (0.0, None)
                c[shed(t, ni)] = self.config.value_of_lost_load_per_mwh * dt
                bounds[absorb(t, ni)] = (0.0, None)
                c[absorb(t, ni)] = self.config.emergency_absorption_cost_per_mwh * dt
            for li, line in enumerate(lines_by_period[t]):
                bounds[flow(t, li)] = (-line.capacity_mw, line.capacity_mw)
            for ni in range(N):
                bounds[theta(t, ni)] = (0.0, 0.0) if ni == 0 else (-self.config.angle_bound, self.config.angle_bound)

        # Continuation value at the rolling-horizon boundary avoids an artificial
        # incentive to empty the battery at the last visible half-hour.
        c[soc(T - 1)] -= max(0.0, terminal_soc_value_per_mwh)

        A_eq: List[np.ndarray] = []
        b_eq: List[float] = []
        balance_rows: List[Dict[str, int]] = []

        # Nodal balance + DC flow per period.
        for t in range(T):
            rows_for_t: Dict[str, int] = {}
            for ni, node in enumerate(self.node_names):
                row = np.zeros(nvars)
                for gi, gen in enumerate(fleets[t]):
                    if gen.node == node:
                        row[pg(t, gi)] += 1.0
                if battery.node == node:
                    row[bd(t)] += 1.0
                    row[bc(t)] -= 1.0
                row[shed(t, ni)] += 1.0
                row[absorb(t, ni)] -= 1.0
                for li, line in enumerate(lines_by_period[t]):
                    if line.from_node == node:
                        row[flow(t, li)] -= 1.0
                    elif line.to_node == node:
                        row[flow(t, li)] += 1.0
                rows_for_t[node] = len(A_eq)
                A_eq.append(row)
                b_eq.append(float(demands[t][node]))
            balance_rows.append(rows_for_t)

            for li, line in enumerate(lines_by_period[t]):
                row = np.zeros(nvars)
                row[flow(t, li)] = 1.0
                row[theta(t, self.node_names.index(line.from_node))] -= line.susceptance
                row[theta(t, self.node_names.index(line.to_node))] += line.susceptance
                A_eq.append(row)
                b_eq.append(0.0)

            # Battery SOC recurrence.
            row = np.zeros(nvars)
            row[soc(t)] = 1.0
            row[bc(t)] = -battery.eta_charge * dt
            row[bd(t)] = dt / battery.eta_discharge
            if t == 0:
                A_eq.append(row)
                b_eq.append(float(battery.soc_mwh))
            else:
                row[soc(t - 1)] = -1.0
                A_eq.append(row)
                b_eq.append(0.0)

        A_ub: List[np.ndarray] = []
        b_ub: List[float] = []
        reserve_rows: List[int] = []

        for t in range(T):
            # Generator energy + reserve headroom.
            for gi, gen in enumerate(fleets[t]):
                if gen.technology not in RESERVE_TECH:
                    continue
                row = np.zeros(nvars)
                row[pg(t, gi)] = 1.0
                row[rg(t, gi)] = 1.0
                A_ub.append(row)
                b_ub.append(max(0.0, gen.available_capacity_mw))

            # Generator ramping from the observed previous RT dispatch and then
            # period-to-period through the rolling horizon. Known DA starts/stops
            # relax the associated transition rather than making the LP infeasible.
            for gi, gen in enumerate(fleets[t]):
                if gen.ramp_up_mw_per_hour >= 999999 and gen.ramp_down_mw_per_hour >= 999999:
                    continue
                cap = max(0.0, gen.available_capacity_mw)
                if t == 0:
                    prev = max(0.0, previous_dispatch_mw.get(gen.name, 0.0))
                    if gen.name not in startup_sets[t] and not (gen.technology == "ocgt" and prev <= 1e-6):
                        row = np.zeros(nvars)
                        row[pg(t, gi)] = 1.0
                        A_ub.append(row)
                        b_ub.append(prev + gen.ramp_up_mw_per_hour * dt)
                    # A forced outage/derate can reduce available capacity faster
                    # than the unit's normal dispatch ramp. In that case the outage
                    # physics overrides the economic ramp-down limit.
                    forced_drop = cap < prev - gen.ramp_down_mw_per_hour * dt - 1e-9
                    if gen.name not in shutdown_sets[t] and not forced_drop:
                        row = np.zeros(nvars)
                        row[pg(t, gi)] = -1.0
                        A_ub.append(row)
                        b_ub.append(gen.ramp_down_mw_per_hour * dt - prev)
                else:
                    if gen.name not in startup_sets[t]:
                        row = np.zeros(nvars)
                        row[pg(t, gi)] = 1.0
                        row[pg(t - 1, gi)] = -1.0
                        A_ub.append(row)
                        b_ub.append(gen.ramp_up_mw_per_hour * dt)
                    prior_cap = max(0.0, fleets[t - 1][gi].available_capacity_mw)
                    forced_drop = cap < prior_cap - gen.ramp_down_mw_per_hour * dt - 1e-9
                    if gen.name not in shutdown_sets[t] and not forced_drop:
                        row = np.zeros(nvars)
                        row[pg(t - 1, gi)] = 1.0
                        row[pg(t, gi)] = -1.0
                        A_ub.append(row)
                        b_ub.append(gen.ramp_down_mw_per_hour * dt)

            # BESS inverter and reserve-energy feasibility.
            dynamic_mw = min(
                battery.power_mw,
                max(0.0, battery_bids[t].dynamic_response_mw),
            )
            row = np.zeros(nvars)
            row[bd(t)] = 1.0
            row[br(t)] = 1.0
            A_ub.append(row)
            b_ub.append(max(0.0, battery.power_mw - dynamic_mw))

            row = np.zeros(nvars)
            row[bc(t)] = 1.0
            A_ub.append(row)
            b_ub.append(max(0.0, battery.power_mw - dynamic_mw))

            response_h = self.config.reserve_response_hours
            dynamic_h = self.config.dynamic_response_hours
            row = np.zeros(nvars)
            # Scheduled charge/discharge is already embedded in end-of-period SOC.
            # Upward reserve can first be delivered by curtailing a charging baseline;
            # only reserve beyond that baseline needs stored energy after the schedule.
            row[br(t)] = response_h / battery.eta_discharge
            row[bc(t)] = -response_h / battery.eta_discharge
            row[soc(t)] = -1.0
            A_ub.append(row)
            b_ub.append(
                -(battery.min_soc_mwh + dynamic_mw * dynamic_h / battery.eta_discharge)
            )

            # Maintain empty energy capacity for a high-frequency response as well.
            row = np.zeros(nvars)
            row[soc(t)] = 1.0
            A_ub.append(row)
            b_ub.append(
                battery.max_soc_mwh - dynamic_mw * battery.eta_charge * dynamic_h
            )

            # Operating reserve requirement.
            row = np.zeros(nvars)
            for gi, gen in enumerate(fleets[t]):
                if gen.technology in RESERVE_TECH:
                    row[rg(t, gi)] = -1.0
            row[br(t)] = -1.0
            row[rshort(t)] = -1.0
            reserve_rows.append(len(A_ub))
            A_ub.append(row)
            b_ub.append(-float(reserve_requirements_mw[t]))

        result = linprog(
            c,
            A_ub=np.asarray(A_ub) if A_ub else None,
            b_ub=np.asarray(b_ub) if b_ub else None,
            A_eq=np.asarray(A_eq),
            b_eq=np.asarray(b_eq),
            bounds=bounds,
            method="highs",
        )

        if not result.success or result.x is None:
            failed = DispatchResult(
                success=False,
                objective_value=float("nan"), nodal_prices={}, generator_dispatch_mw={},
                line_flows_mw={}, battery_charge_mw=0.0, battery_discharge_mw=0.0,
                load_shed_mw={}, emergency_absorption_mw={}, status=result.message,
            )
            return RollingRTResult(
                False, result.message, float("nan"), failed, [], [], [], [], [], [], [], []
            )

        x = result.x
        horizon_prices: List[Dict[str, float]] = []
        horizon_reserve_prices: List[float] = []
        horizon_flows: List[Dict[str, float]] = []
        for t in range(T):
            horizon_prices.append({
                node: float(result.eqlin.marginals[balance_rows[t][node]] / dt)
                for node in self.node_names
            })
            horizon_reserve_prices.append(max(0.0, float(-result.ineqlin.marginals[reserve_rows[t]] / dt)))
            horizon_flows.append({line.name: float(x[flow(t, li)]) for li, line in enumerate(lines_by_period[t])})

        first = DispatchResult(
            success=True,
            objective_value=float(result.fun),
            nodal_prices=horizon_prices[0],
            generator_dispatch_mw={g.name: float(x[pg(0, gi)]) for gi, g in enumerate(fleets[0])},
            line_flows_mw=horizon_flows[0],
            battery_charge_mw=float(x[bc(0)]),
            battery_discharge_mw=float(x[bd(0)]),
            load_shed_mw={node: float(x[shed(0, ni)]) for ni, node in enumerate(self.node_names)},
            emergency_absorption_mw={node: float(x[absorb(0, ni)]) for ni, node in enumerate(self.node_names)},
            status=result.message,
            battery_reserve_mw=float(x[br(0)]),
            generator_reserve_mw={g.name: float(x[rg(0, gi)]) for gi, g in enumerate(fleets[0])},
            reserve_requirement_mw=float(reserve_requirements_mw[0]),
            reserve_shortfall_mw=float(x[rshort(0)]),
            reserve_price_per_mw_h=horizon_reserve_prices[0],
        )

        return RollingRTResult(
            success=True,
            status=result.message,
            objective_value=float(result.fun),
            first_period=first,
            horizon_prices=horizon_prices,
            horizon_reserve_prices=horizon_reserve_prices,
            horizon_battery_charge_mw=[float(x[bc(t)]) for t in range(T)],
            horizon_battery_discharge_mw=[float(x[bd(t)]) for t in range(T)],
            horizon_battery_reserve_mw=[float(x[br(t)]) for t in range(T)],
            horizon_soc_mwh=[float(x[soc(t)]) for t in range(T)],
            horizon_line_flows_mw=horizon_flows,
            horizon_reserve_shortfall_mw=[float(x[rshort(t)]) for t in range(T)],
        )
