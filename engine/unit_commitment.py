from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Sequence
import math

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import coo_matrix

from .game_types import CommitmentBid
from .models import BatteryAsset, Generator, Line, Node


@dataclass
class UnitCommitmentResult:
    success: bool
    status: str
    objective_value: float
    commitment: List[Dict[str, bool]]
    dispatch_mw: List[Dict[str, float]]
    generator_reserve_mw: List[Dict[str, float]]
    startup: List[Dict[str, bool]]
    shutdown: List[Dict[str, bool]]
    battery_charge_mw: List[float]
    battery_discharge_mw: List[float]
    battery_reserve_mw: List[float]
    battery_dynamic_response_mw: List[float]
    battery_soc_mwh: List[float]
    reserve_requirement_mw: List[float]
    reserve_shortfall_mw: List[float]
    line_flows_mw: List[Dict[str, float]]


COMMITMENT_TECH = {"nuclear", "ccgt", "coal", "biomass", "ocgt"}
RESERVE_TECH = {"nuclear", "ccgt", "coal", "biomass", "ocgt"}


class DayAheadUnitCommitment:
    """Compact multi-period security-aware unit commitment for the stylised game.

    It is deliberately small enough to solve interactively. The MILP co-optimises
    thermal commitment, dispatch, the player's DA battery bids, DC line flows and a
    spinning-reserve headroom requirement. LMP pricing is performed separately after
    fixing commitment because startup/no-load costs are non-convex.
    """

    def __init__(
        self,
        nodes: Sequence[Node],
        lines: Sequence[Line],
        duration_hours: float = 0.5,
        value_of_lost_load_per_mwh: float = 5000.0,
        emergency_absorption_cost_per_mwh: float = 250.0,
        reserve_shortfall_cost_per_mwh: float = 3000.0,
        reserve_fraction_of_demand: float = 0.08,
        reserve_floor_mw: float = 180.0,
    ) -> None:
        self.nodes = list(nodes)
        self.lines = list(lines)
        self.node_names = [n.name for n in self.nodes]
        self.duration_hours = duration_hours
        self.voll = value_of_lost_load_per_mwh
        self.absorb_cost = emergency_absorption_cost_per_mwh
        self.reserve_shortfall_cost = reserve_shortfall_cost_per_mwh
        self.reserve_fraction = reserve_fraction_of_demand
        self.reserve_floor_mw = reserve_floor_mw

    def solve(
        self,
        demands: Sequence[Dict[str, float]],
        fleets: Sequence[Sequence[Generator]],
        battery: BatteryAsset,
        bids: Sequence[CommitmentBid],
    ) -> UnitCommitmentResult:
        T = len(demands)
        if not T or len(fleets) != T or len(bids) != T:
            raise ValueError("demands, fleets and bids must have the same non-zero length")
        names = [g.name for g in fleets[0]]
        G = len(names)
        if any([g.name for g in fleet] != names for fleet in fleets):
            raise ValueError("Generator ordering/names must remain constant across periods")
        if any(set(d) != set(self.node_names) for d in demands):
            raise ValueError("Every demand period must contain all market nodes")

        # Variable block allocation.
        cursor = 0
        def alloc(count: int):
            nonlocal cursor
            start = cursor
            cursor += count
            return start

        p0 = alloc(T * G)
        res0 = alloc(T * G)
        u0 = alloc(T * G)
        y0 = alloc(T * G)
        z0 = alloc(T * G)
        ch0 = alloc(T)
        dis0 = alloc(T)
        mode0 = alloc(T)
        bres0 = alloc(T)
        soc0 = alloc(T)
        shed0 = alloc(T * len(self.node_names))
        absorb0 = alloc(T * len(self.node_names))
        flow0 = alloc(T * len(self.lines))
        theta0 = alloc(T * len(self.node_names))
        rshort0 = alloc(T)
        nvars = cursor

        def pg(t, g): return p0 + t * G + g
        def rg(t, g): return res0 + t * G + g
        def ug(t, g): return u0 + t * G + g
        def yg(t, g): return y0 + t * G + g
        def zg(t, g): return z0 + t * G + g
        def ch(t): return ch0 + t
        def dis(t): return dis0 + t
        def mode(t): return mode0 + t
        def bres(t): return bres0 + t
        def soc(t): return soc0 + t
        def shed(t, n): return shed0 + t * len(self.node_names) + n
        def absorb(t, n): return absorb0 + t * len(self.node_names) + n
        def flow(t, l): return flow0 + t * len(self.lines) + l
        def theta(t, n): return theta0 + t * len(self.node_names) + n
        def rshort(t): return rshort0 + t

        c = np.zeros(nvars)
        lb = np.full(nvars, -np.inf)
        ub = np.full(nvars, np.inf)
        integrality = np.zeros(nvars, dtype=int)
        dt = self.duration_hours

        # Bounds and objective.
        for t in range(T):
            for gi, gen in enumerate(fleets[t]):
                cap = max(0.0, gen.available_capacity_mw)
                lb[pg(t, gi)], ub[pg(t, gi)] = 0.0, cap
                c[pg(t, gi)] = gen.offer_price_per_mwh * dt
                if gen.technology in RESERVE_TECH:
                    response_cap = min(cap, max(0.0, gen.ramp_up_mw_per_hour * 0.25))
                    lb[rg(t, gi)], ub[rg(t, gi)] = 0.0, response_cap
                    c[rg(t, gi)] = gen.reserve_offer_cost_per_mw_h * dt
                else:
                    lb[rg(t, gi)] = ub[rg(t, gi)] = 0.0
                if gen.technology in COMMITMENT_TECH:
                    lb[ug(t, gi)], ub[ug(t, gi)] = 0.0, 1.0
                    integrality[ug(t, gi)] = 1
                    lb[yg(t, gi)], ub[yg(t, gi)] = 0.0, 1.0
                    integrality[yg(t, gi)] = 1
                    lb[zg(t, gi)], ub[zg(t, gi)] = 0.0, 1.0
                    integrality[zg(t, gi)] = 1
                    c[ug(t, gi)] = gen.no_load_cost_per_hour * dt
                    c[yg(t, gi)] = gen.startup_cost
                else:
                    lb[ug(t, gi)] = ub[ug(t, gi)] = 1.0
                    lb[yg(t, gi)] = ub[yg(t, gi)] = 0.0
                    lb[zg(t, gi)] = ub[zg(t, gi)] = 0.0

            bid = bids[t]
            bid.validate(battery.power_mw)
            dynamic_mw = min(battery.power_mw, max(0.0, bid.dynamic_response_award_mw))
            available = max(
                0.0,
                battery.power_mw * (1.0 - bid.reserve_holdback_fraction) - dynamic_mw,
            )
            ch_cap = min(available, bid.max_charge_mw if bid.max_charge_mw is not None else available)
            dis_cap = min(available, bid.max_discharge_mw if bid.max_discharge_mw is not None else available)
            lb[ch(t)], ub[ch(t)] = 0.0, max(0.0, ch_cap)
            lb[dis(t)], ub[dis(t)] = 0.0, max(0.0, dis_cap)
            c[ch(t)] = -bid.max_charge_price_per_mwh * dt
            c[dis(t)] = bid.min_discharge_price_per_mwh * dt
            lb[mode(t)], ub[mode(t)] = 0.0, 1.0
            integrality[mode(t)] = 1
            reserve_cap = min(
                battery.power_mw * bid.reserve_holdback_fraction,
                max(0.0, battery.power_mw - dynamic_mw),
            )
            lb[bres(t)], ub[bres(t)] = 0.0, max(0.0, reserve_cap)
            c[bres(t)] = bid.reserve_offer_price_per_mw_h * dt
            lb[soc(t)], ub[soc(t)] = battery.min_soc_mwh, battery.max_soc_mwh

            for ni in range(len(self.node_names)):
                lb[shed(t, ni)], ub[shed(t, ni)] = 0.0, np.inf
                c[shed(t, ni)] = self.voll * dt
                lb[absorb(t, ni)], ub[absorb(t, ni)] = 0.0, np.inf
                c[absorb(t, ni)] = self.absorb_cost * dt
            for li, line in enumerate(self.lines):
                lb[flow(t, li)], ub[flow(t, li)] = -line.capacity_mw, line.capacity_mw
            for ni in range(len(self.node_names)):
                if ni == 0:
                    lb[theta(t, ni)] = ub[theta(t, ni)] = 0.0
                else:
                    lb[theta(t, ni)], ub[theta(t, ni)] = -10_000.0, 10_000.0
            lb[rshort(t)], ub[rshort(t)] = 0.0, np.inf
            c[rshort(t)] = self.reserve_shortfall_cost * dt

        rows: List[int] = []
        cols: List[int] = []
        data: List[float] = []
        clb: List[float] = []
        cub: List[float] = []

        def add(coeffs: Dict[int, float], lower=-np.inf, upper=np.inf):
            r = len(clb)
            for col, val in coeffs.items():
                if abs(val) > 1e-12:
                    rows.append(r); cols.append(col); data.append(val)
            clb.append(lower); cub.append(upper)

        # Nodal balance and DC flow equations.
        for t in range(T):
            for ni, node in enumerate(self.node_names):
                coeffs: Dict[int, float] = {}
                for gi, gen in enumerate(fleets[t]):
                    if gen.node == node:
                        coeffs[pg(t, gi)] = 1.0
                if battery.node == node:
                    coeffs[dis(t)] = coeffs.get(dis(t), 0.0) + 1.0
                    coeffs[ch(t)] = coeffs.get(ch(t), 0.0) - 1.0
                coeffs[shed(t, ni)] = 1.0
                coeffs[absorb(t, ni)] = -1.0
                for li, line in enumerate(self.lines):
                    if line.from_node == node:
                        coeffs[flow(t, li)] = coeffs.get(flow(t, li), 0.0) - 1.0
                    elif line.to_node == node:
                        coeffs[flow(t, li)] = coeffs.get(flow(t, li), 0.0) + 1.0
                rhs = float(demands[t][node])
                add(coeffs, rhs, rhs)

            for li, line in enumerate(self.lines):
                fi = self.node_names.index(line.from_node)
                ti = self.node_names.index(line.to_node)
                add({flow(t, li): 1.0, theta(t, fi): -line.susceptance, theta(t, ti): line.susceptance}, 0.0, 0.0)

        # Generator commitment, start/stop logic, ramping and min up/down.
        first_fleet = fleets[0]
        for gi, base in enumerate(first_fleet):
            if base.technology not in COMMITMENT_TECH:
                continue
            initial_on = 1.0 if base.initially_on else 0.0
            initial_output = base.initial_output_mw
            if initial_output is None:
                stable = base.minimum_stable_mw if base.minimum_stable_mw > 0 else base.min_output_mw
                initial_output = stable if base.initially_on else 0.0

            for t in range(T):
                gen = fleets[t][gi]
                cap = max(0.0, gen.available_capacity_mw)
                stable = gen.minimum_stable_mw if gen.minimum_stable_mw > 0 else gen.min_output_mw
                pmin = min(max(0.0, stable), cap)
                add({pg(t, gi): 1.0, ug(t, gi): -cap}, upper=0.0)
                add({pg(t, gi): 1.0, ug(t, gi): -pmin}, lower=0.0)
                if gen.technology in RESERVE_TECH:
                    response_cap = min(cap, max(0.0, gen.ramp_up_mw_per_hour * 0.25))
                    add({rg(t, gi): 1.0, ug(t, gi): -response_cap}, upper=0.0)
                    add({pg(t, gi): 1.0, rg(t, gi): 1.0, ug(t, gi): -cap}, upper=0.0)

                if t == 0:
                    add({ug(t, gi): 1.0, yg(t, gi): -1.0, zg(t, gi): 1.0}, initial_on, initial_on)
                    ramp_up = max(gen.ramp_up_mw_per_hour, pmin / dt if dt else pmin) * dt
                    ramp_down = max(gen.ramp_down_mw_per_hour, pmin / dt if dt else pmin) * dt
                    add({pg(t, gi): 1.0, yg(t, gi): -cap}, upper=initial_output + ramp_up)
                    add({pg(t, gi): -1.0, zg(t, gi): -cap}, upper=ramp_down - initial_output)
                else:
                    add({ug(t, gi): 1.0, ug(t-1, gi): -1.0, yg(t, gi): -1.0, zg(t, gi): 1.0}, 0.0, 0.0)
                    ramp_up = gen.ramp_up_mw_per_hour * dt
                    ramp_down = gen.ramp_down_mw_per_hour * dt
                    # Startup/shutdown relaxations permit a jump to/from the stable minimum.
                    add({pg(t, gi): 1.0, pg(t-1, gi): -1.0, yg(t, gi): -cap}, upper=ramp_up)
                    add({pg(t-1, gi): 1.0, pg(t, gi): -1.0, zg(t, gi): -cap}, upper=ramp_down)

                up = max(1, int(gen.min_up_periods))
                if up > 1:
                    end = min(T, t + up)
                    L = end - t
                    coeff = {ug(k, gi): 1.0 for k in range(t, end)}
                    coeff[yg(t, gi)] = -float(L)
                    add(coeff, lower=0.0)
                down = max(1, int(gen.min_down_periods))
                if down > 1:
                    end = min(T, t + down)
                    L = end - t
                    coeff = {ug(k, gi): 1.0 for k in range(t, end)}
                    coeff[zg(t, gi)] = float(L)
                    add(coeff, upper=float(L))

        # Battery intertemporal physics and mutually exclusive operating mode.
        for t in range(T):
            bid = bids[t]
            dynamic_mw = min(battery.power_mw, max(0.0, bid.dynamic_response_award_mw))
            available = max(
                0.0,
                battery.power_mw * (1.0 - bid.reserve_holdback_fraction) - dynamic_mw,
            )
            ch_cap = min(available, bid.max_charge_mw if bid.max_charge_mw is not None else available)
            dis_cap = min(available, bid.max_discharge_mw if bid.max_discharge_mw is not None else available)
            # mode=0 => charge allowed, mode=1 => discharge allowed.
            add({ch(t): 1.0, mode(t): max(0.0, ch_cap)}, upper=max(0.0, ch_cap))
            add({dis(t): 1.0, mode(t): -max(0.0, dis_cap)}, upper=0.0)
            # Held converter capacity can be sold as upward operating reserve.
            add(
                {dis(t): 1.0, bres(t): 1.0},
                upper=max(0.0, battery.power_mw - dynamic_mw),
            )
            # Symmetric dynamic response also needs downward inverter headroom.
            add({ch(t): 1.0}, upper=max(0.0, battery.power_mw - dynamic_mw))
            coeff = {soc(t): 1.0, ch(t): -battery.eta_charge * dt, dis(t): dt / battery.eta_discharge}
            if t == 0:
                add(coeff, battery.soc_mwh, battery.soc_mwh)
            else:
                coeff[soc(t-1)] = -1.0
                add(coeff, 0.0, 0.0)

            # The awarded reserve must remain energy-deliverable for the response
            # interval after the scheduled DA energy position. A charging baseline
            # can provide upward response first by reducing its consumption.
            response_h = 0.25
            add(
                {bres(t): response_h / battery.eta_discharge,
                 ch(t): -response_h / battery.eta_discharge,
                 soc(t): -1.0},
                upper=-(battery.min_soc_mwh + dynamic_mw * response_h / battery.eta_discharge),
            )
            # High-frequency response may require absorbing energy, so maintain a
            # matching upper-SOC buffer as well.
            add(
                {soc(t): 1.0},
                upper=battery.max_soc_mwh - dynamic_mw * battery.eta_charge * response_h,
            )

        # Deliverable operating reserve: explicit reserve variables are limited by
        # both committed headroom and each unit's response-rate capability.
        reserve_requirements = []
        for t in range(T):
            req = max(self.reserve_floor_mw, self.reserve_fraction * sum(demands[t].values()))
            reserve_requirements.append(req)
            coeff: Dict[int, float] = {rshort(t): 1.0, bres(t): 1.0}
            for gi, gen in enumerate(fleets[t]):
                if gen.technology in RESERVE_TECH:
                    coeff[rg(t, gi)] = 1.0
            add(coeff, lower=req)

        A = coo_matrix((data, (rows, cols)), shape=(len(clb), nvars)).tocsr()
        constraint = LinearConstraint(A, np.asarray(clb), np.asarray(cub))
        result = milp(
            c,
            integrality=integrality,
            bounds=Bounds(lb, ub),
            constraints=constraint,
            options={"time_limit": 10.0, "mip_rel_gap": 5e-3, "presolve": True},
        )

        # HiGHS may hit the interactive time limit after finding a valid incumbent.
        # SciPy reports that as success=False even though result.x can be a fully
        # feasible integer schedule. Validate such an incumbent explicitly and use
        # it rather than failing the whole trading day.
        x = result.x
        incumbent_ok = False
        if x is not None and np.all(np.isfinite(x)):
            tol = 2e-5
            in_bounds = np.all(x >= np.where(np.isfinite(lb), lb - tol, -np.inf)) and np.all(
                x <= np.where(np.isfinite(ub), ub + tol, np.inf)
            )
            activity = A @ x
            constraints_ok = np.all(activity >= np.asarray(clb) - tol) and np.all(
                activity <= np.asarray(cub) + tol
            )
            integer_idx = np.flatnonzero(integrality)
            integer_ok = (
                integer_idx.size == 0
                or np.max(np.abs(x[integer_idx] - np.rint(x[integer_idx]))) <= 2e-4
            )
            incumbent_ok = bool(in_bounds and constraints_ok and integer_ok)

        if x is None or (not result.success and not incumbent_ok):
            return UnitCommitmentResult(
                success=False,
                status=result.message,
                objective_value=float("nan"),
                commitment=[], dispatch_mw=[], generator_reserve_mw=[], startup=[], shutdown=[],
                battery_charge_mw=[], battery_discharge_mw=[], battery_reserve_mw=[],
                battery_dynamic_response_mw=[], battery_soc_mwh=[],
                reserve_requirement_mw=reserve_requirements, reserve_shortfall_mw=[], line_flows_mw=[]
            )

        status = result.message
        if not result.success and incumbent_ok:
            status = f"{result.message} Feasible incumbent accepted for interactive play."
        commitment: List[Dict[str, bool]] = []
        dispatch: List[Dict[str, float]] = []
        reserves: List[Dict[str, float]] = []
        startups: List[Dict[str, bool]] = []
        shutdowns: List[Dict[str, bool]] = []
        line_flows: List[Dict[str, float]] = []
        for t in range(T):
            commitment.append({names[g]: bool(x[ug(t,g)] >= 0.5) for g in range(G)})
            dispatch.append({names[g]: float(x[pg(t,g)]) for g in range(G)})
            reserves.append({names[g]: float(x[rg(t,g)]) for g in range(G)})
            startups.append({names[g]: bool(x[yg(t,g)] >= 0.5) for g in range(G) if first_fleet[g].technology in COMMITMENT_TECH})
            shutdowns.append({names[g]: bool(x[zg(t,g)] >= 0.5) for g in range(G) if first_fleet[g].technology in COMMITMENT_TECH})
            line_flows.append({line.name: float(x[flow(t,li)]) for li,line in enumerate(self.lines)})

        return UnitCommitmentResult(
            success=True,
            status=status,
            objective_value=float(result.fun if result.fun is not None else c @ x),
            commitment=commitment,
            dispatch_mw=dispatch,
            generator_reserve_mw=reserves,
            startup=startups,
            shutdown=shutdowns,
            battery_charge_mw=[float(x[ch(t)]) for t in range(T)],
            battery_discharge_mw=[float(x[dis(t)]) for t in range(T)],
            battery_reserve_mw=[float(x[bres(t)]) for t in range(T)],
            battery_dynamic_response_mw=[float(bids[t].dynamic_response_award_mw) for t in range(T)],
            battery_soc_mwh=[float(x[soc(t)]) for t in range(T)],
            reserve_requirement_mw=reserve_requirements,
            reserve_shortfall_mw=[float(x[rshort(t)]) for t in range(T)],
            line_flows_mw=line_flows,
        )
