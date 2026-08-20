from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np
from scipy.optimize import linprog

from .models import BatteryAsset, BatteryBid, DispatchResult, Generator, Line, Node


@dataclass
class MarketConfig:
    value_of_lost_load_per_mwh: float = 5000.0
    emergency_absorption_cost_per_mwh: float = 250.0
    reserve_shortfall_cost_per_mwh: float = 3000.0
    reserve_response_hours: float = 0.25
    dynamic_response_hours: float = 0.25
    angle_bound: float = 10_000.0


RESERVE_TECH = {"nuclear", "ccgt", "ocgt", "coal", "biomass"}


class DCOPFMarket:
    """Single-period DC optimal-power-flow market with energy + operating reserve.

    The LP minimizes offered production cost, reserve procurement cost and reliability
    penalties minus the willingness-to-pay of flexible battery charging. When an
    operating-reserve requirement is supplied, dispatchable generators can carry
    reserve subject to headroom and response-rate limits. The nodal balance duals
    therefore include the opportunity cost of reserve scarcity.
    """

    def __init__(self, nodes: Iterable[Node], lines: Iterable[Line], config: Optional[MarketConfig] = None):
        self.nodes = list(nodes)
        self.lines = list(lines)
        self.node_names = [n.name for n in self.nodes]
        self.node_index = {name: i for i, name in enumerate(self.node_names)}
        self.config = config or MarketConfig()
        for line in self.lines:
            if line.from_node not in self.node_index or line.to_node not in self.node_index:
                raise ValueError(f"Unknown node in line {line.name}")

    def clear(
        self,
        demand_mw: Dict[str, float],
        generators: List[Generator],
        duration_hours: float,
        battery: Optional[BatteryAsset] = None,
        battery_bid: Optional[BatteryBid] = None,
        reserve_requirement_mw: float = 0.0,
    ) -> DispatchResult:
        for node in self.node_names:
            if node not in demand_mw:
                raise ValueError(f"Missing demand for node {node}")
        if battery is not None and battery_bid is None:
            raise ValueError("battery_bid is required when battery is present")
        if battery_bid is not None:
            battery_bid.validate()
        if reserve_requirement_mw < 0:
            raise ValueError("reserve_requirement_mw must be non-negative")

        # Variable ordering:
        # [generator energy | generator reserve | battery discharge | battery charge |
        #  battery reserve | reserve shortfall | shed per node | emergency absorption per node |
        #  line flows | voltage angles]
        n_g = len(generators)
        idx_g = slice(0, n_g)
        idx_r = slice(n_g, 2 * n_g)
        cursor = 2 * n_g

        idx_bd = cursor if battery is not None else None
        cursor += 1 if battery is not None else 0
        idx_bc = cursor if battery is not None else None
        cursor += 1 if battery is not None else 0
        idx_br = cursor if battery is not None else None
        cursor += 1 if battery is not None else 0

        idx_rshort = cursor
        cursor += 1
        idx_shed = {node: cursor + i for i, node in enumerate(self.node_names)}
        cursor += len(self.node_names)
        idx_absorb = {node: cursor + i for i, node in enumerate(self.node_names)}
        cursor += len(self.node_names)
        idx_flow = {line.name: cursor + i for i, line in enumerate(self.lines)}
        cursor += len(self.lines)
        idx_theta = {node: cursor + i for i, node in enumerate(self.node_names)}
        cursor += len(self.node_names)
        n_vars = cursor

        c = np.zeros(n_vars)
        bounds: List[Tuple[Optional[float], Optional[float]]] = [(None, None)] * n_vars

        for i, g in enumerate(generators):
            c[i] = g.offer_price_per_mwh * duration_hours
            cap = g.available_capacity_mw
            pmin = min(max(0.0, g.min_output_mw), cap)
            bounds[i] = (pmin, cap)

            r_idx = n_g + i
            if reserve_requirement_mw > 0 and g.technology in RESERVE_TECH and cap > 0:
                response_cap = min(cap, max(0.0, g.ramp_up_mw_per_hour * self.config.reserve_response_hours))
                bounds[r_idx] = (0.0, response_cap)
                c[r_idx] = g.reserve_offer_cost_per_mw_h * duration_hours
            else:
                bounds[r_idx] = (0.0, 0.0)

        if battery is not None and battery_bid is not None:
            dynamic_mw = min(battery.power_mw, max(0.0, battery_bid.dynamic_response_mw))
            charge_limit = battery.charge_limit_mw(duration_hours)
            discharge_limit = battery.discharge_limit_mw(duration_hours)
            # Dynamic response is modelled as a symmetric availability product:
            # the baseline must leave inverter room to move both up and down.
            charge_limit = min(charge_limit, max(0.0, battery.power_mw - dynamic_mw))
            discharge_limit = min(discharge_limit, max(0.0, battery.power_mw - dynamic_mw))
            if battery_bid.max_charge_mw is not None:
                charge_limit = min(charge_limit, battery_bid.max_charge_mw)
            if battery_bid.max_discharge_mw is not None:
                discharge_limit = min(discharge_limit, battery_bid.max_discharge_mw)

            c[idx_bd] = battery_bid.min_discharge_price_per_mwh * duration_hours
            bounds[idx_bd] = (0.0, discharge_limit)
            c[idx_bc] = -battery_bid.max_charge_price_per_mwh * duration_hours
            bounds[idx_bc] = (0.0, charge_limit)
            if reserve_requirement_mw > 0 and battery_bid.max_reserve_mw > 0:
                reserve_cap = min(battery.power_mw, battery_bid.max_reserve_mw)
                c[idx_br] = battery_bid.reserve_offer_price_per_mw_h * duration_hours
                bounds[idx_br] = (0.0, reserve_cap)
            else:
                bounds[idx_br] = (0.0, 0.0)

        if reserve_requirement_mw > 0:
            bounds[idx_rshort] = (0.0, None)
            c[idx_rshort] = self.config.reserve_shortfall_cost_per_mwh * duration_hours
        else:
            bounds[idx_rshort] = (0.0, 0.0)

        for node in self.node_names:
            c[idx_shed[node]] = self.config.value_of_lost_load_per_mwh * duration_hours
            bounds[idx_shed[node]] = (0.0, None)
            c[idx_absorb[node]] = self.config.emergency_absorption_cost_per_mwh * duration_hours
            bounds[idx_absorb[node]] = (0.0, None)

        for line in self.lines:
            bounds[idx_flow[line.name]] = (-line.capacity_mw, line.capacity_mw)

        for node in self.node_names:
            if node == self.node_names[0]:
                bounds[idx_theta[node]] = (0.0, 0.0)
            else:
                bounds[idx_theta[node]] = (-self.config.angle_bound, self.config.angle_bound)

        A_eq = []
        b_eq = []
        balance_row_positions: Dict[str, int] = {}

        # Nodal energy balance.
        for node in self.node_names:
            row = np.zeros(n_vars)
            for i, g in enumerate(generators):
                if g.node == node:
                    row[i] += 1.0
            if battery is not None and battery.node == node:
                row[idx_bd] += 1.0
                row[idx_bc] -= 1.0
            row[idx_shed[node]] += 1.0
            row[idx_absorb[node]] -= 1.0
            for line in self.lines:
                if line.from_node == node:
                    row[idx_flow[line.name]] -= 1.0
                elif line.to_node == node:
                    row[idx_flow[line.name]] += 1.0
            balance_row_positions[node] = len(A_eq)
            A_eq.append(row)
            b_eq.append(float(demand_mw[node]))

        # DC flow equation: f = B(theta_from - theta_to)
        for line in self.lines:
            row = np.zeros(n_vars)
            row[idx_flow[line.name]] = 1.0
            row[idx_theta[line.from_node]] -= line.susceptance
            row[idx_theta[line.to_node]] += line.susceptance
            A_eq.append(row)
            b_eq.append(0.0)

        A_ub = []
        b_ub = []

        # Energy + reserve cannot exceed available output capacity.
        for i, g in enumerate(generators):
            if reserve_requirement_mw <= 0 or g.technology not in RESERVE_TECH:
                continue
            row = np.zeros(n_vars)
            row[i] = 1.0
            row[n_g + i] = 1.0
            A_ub.append(row)
            b_ub.append(g.available_capacity_mw)

        if battery is not None and battery_bid is not None and idx_br is not None:
            dynamic_mw = min(battery.power_mw, max(0.0, battery_bid.dynamic_response_mw))
            # Converter headroom: scheduled discharge plus upward reserve cannot
            # exceed the inverter's injection capability after fixed dynamic-response
            # availability is reserved. Charge can coexist with upward reserve because
            # reserve can first be delivered by reducing charge.
            row = np.zeros(n_vars)
            row[idx_bd] = 1.0
            row[idx_br] = 1.0
            A_ub.append(row)
            b_ub.append(max(0.0, battery.power_mw - dynamic_mw))

            # Symmetric response also requires downward inverter headroom while the
            # battery is charging.
            row = np.zeros(n_vars)
            row[idx_bc] = 1.0
            A_ub.append(row)
            b_ub.append(max(0.0, battery.power_mw - dynamic_mw))

            # Energy sufficiency for a reserve response after the scheduled period.
            # A charging baseline contributes both stored energy and the ability to
            # create upward response by curtailing that charge.
            eta_c = battery.eta_charge
            eta_d = battery.eta_discharge
            response_h = self.config.reserve_response_hours
            dynamic_h = self.config.dynamic_response_hours
            usable_soc = max(0.0, battery.soc_mwh - battery.min_soc_mwh)
            row = np.zeros(n_vars)
            row[idx_br] = response_h / eta_d
            row[idx_bd] = duration_hours / eta_d
            row[idx_bc] = -(response_h / eta_d + eta_c * duration_hours)
            A_ub.append(row)
            b_ub.append(usable_soc - dynamic_mw * dynamic_h / eta_d)

            # And enough empty energy capacity to absorb a high-frequency response.
            room = max(0.0, battery.max_soc_mwh - battery.soc_mwh)
            row = np.zeros(n_vars)
            row[idx_bc] = eta_c * duration_hours
            row[idx_bd] = -duration_hours / eta_d
            A_ub.append(row)
            b_ub.append(room - dynamic_mw * eta_c * dynamic_h)

        reserve_row_position: Optional[int] = None
        if reserve_requirement_mw > 0:
            row = np.zeros(n_vars)
            for i, g in enumerate(generators):
                if g.technology in RESERVE_TECH:
                    row[n_g + i] = -1.0
            if idx_br is not None:
                row[idx_br] = -1.0
            row[idx_rshort] = -1.0
            reserve_row_position = len(A_ub)
            A_ub.append(row)
            b_ub.append(-float(reserve_requirement_mw))

        result = linprog(
            c,
            A_ub=np.asarray(A_ub) if A_ub else None,
            b_ub=np.asarray(b_ub) if b_ub else None,
            A_eq=np.asarray(A_eq),
            b_eq=np.asarray(b_eq),
            bounds=bounds,
            method="highs",
        )

        if not result.success:
            return DispatchResult(
                success=False,
                objective_value=float("nan"),
                nodal_prices={},
                generator_dispatch_mw={},
                line_flows_mw={},
                battery_charge_mw=0.0,
                battery_discharge_mw=0.0,
                load_shed_mw={},
                emergency_absorption_mw={},
                status=result.message,
                battery_reserve_mw=0.0,
            )

        x = result.x
        prices = {
            node: float(result.eqlin.marginals[balance_row_positions[node]] / duration_hours)
            for node in self.node_names
        }

        reserve_price = 0.0
        if reserve_row_position is not None:
            # The requirement is represented as -reserve <= -requirement, so an
            # increase in the requirement moves the RHS downward. Flip the HiGHS
            # inequality marginal to obtain the positive £/MW/h scarcity price.
            reserve_price = max(
                0.0,
                float(-result.ineqlin.marginals[reserve_row_position] / duration_hours),
            )

        return DispatchResult(
            success=True,
            objective_value=float(result.fun),
            nodal_prices=prices,
            generator_dispatch_mw={g.name: float(x[i]) for i, g in enumerate(generators)},
            line_flows_mw={line.name: float(x[idx_flow[line.name]]) for line in self.lines},
            battery_charge_mw=float(x[idx_bc]) if idx_bc is not None else 0.0,
            battery_discharge_mw=float(x[idx_bd]) if idx_bd is not None else 0.0,
            load_shed_mw={node: float(x[idx_shed[node]]) for node in self.node_names},
            emergency_absorption_mw={node: float(x[idx_absorb[node]]) for node in self.node_names},
            status=result.message,
            battery_reserve_mw=float(x[idx_br]) if idx_br is not None else 0.0,
            generator_reserve_mw={g.name: float(x[n_g + i]) for i, g in enumerate(generators)},
            reserve_requirement_mw=float(reserve_requirement_mw),
            reserve_shortfall_mw=float(x[idx_rshort]),
            reserve_price_per_mw_h=reserve_price,
        )
