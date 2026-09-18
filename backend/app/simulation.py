"""Monte Carlo risk engine: quantifies stockout loss for "do nothing" versus each mitigation option.

Model (per simulated run, daily steps over the horizon):
  * demand ~ Normal(mean, std) clipped at 0
  * each inbound shipment arrives at base_day + delay, delay ~ Gamma(mean, std) (right-skewed, >= 0)
  * unmet demand is lost; loss = lost_units * loss_per_unit + stockout_days * daily_penalty
Common random numbers (same demand matrix and same per-shipment arrival draws) are reused across
options so option comparisons are low-variance.
"""
import hashlib
from dataclasses import dataclass, field

import numpy as np


@dataclass
class Inbound:
    label: str
    qty: float
    base_day: float
    delay_mean: float = 0.0
    delay_std: float = 0.5


@dataclass
class ScenarioInput:
    on_hand: float
    demand_mean: float
    demand_std: float
    loss_per_unit: float
    daily_penalty: float = 0.0
    horizon_days: int = 60
    runs: int = 10000
    seed: int = 7
    inbounds: list[Inbound] = field(default_factory=list)


class MonteCarloSimulator:
    def __init__(self, scenario: ScenarioInput):
        self.s = scenario
        rng = np.random.default_rng(scenario.seed)
        self.demand = np.clip(
            rng.normal(scenario.demand_mean, max(scenario.demand_std, 1e-9),
                       (scenario.runs, scenario.horizon_days)), 0, None)

    def _arrivals(self, inbound: Inbound) -> np.ndarray:
        # Seed by label so the same shipment gets identical draws in every option evaluation.
        seed = int.from_bytes(hashlib.sha256(f"{self.s.seed}:{inbound.label}".encode()).digest()[:8], "little")
        rng = np.random.default_rng(seed)
        n = self.s.runs
        if inbound.delay_mean > 0:
            std = max(inbound.delay_std, 0.1)
            shape = (inbound.delay_mean / std) ** 2
            scale = std ** 2 / inbound.delay_mean
            delay = rng.gamma(shape, scale, n)
        else:
            delay = rng.normal(0, max(inbound.delay_std, 0.01), n)
        return np.clip(np.floor(inbound.base_day + delay), 0, None)

    def run(self, inbounds: list[Inbound]) -> dict:
        s = self.s
        arrivals = [(ib.qty, self._arrivals(ib)) for ib in inbounds if ib.qty > 0]
        inv = np.full(s.runs, float(s.on_hand))
        lost = np.zeros(s.runs)
        stockout_days = np.zeros(s.runs)
        last_stockout = np.full(s.runs, -1.0)
        for d in range(s.horizon_days):
            for qty, arr in arrivals:
                inv += qty * (arr == d)
            dem = self.demand[:, d]
            sold = np.minimum(inv, dem)
            short = dem - sold
            inv -= sold
            lost += short
            out = short > 1e-6
            stockout_days += out
            last_stockout = np.where(out, d, last_stockout)
        loss = lost * s.loss_per_unit + stockout_days * s.daily_penalty
        return summarize(loss, lost, stockout_days, last_stockout + 1)


def summarize(loss, lost_units, stockout_days, recovery_day) -> dict:
    pct = {f"p{p}": round(float(np.percentile(loss, p)), 2) for p in (5, 15, 50, 85, 95)}
    counts, edges = np.histogram(loss, bins=24)
    return {
        "expected_loss": round(float(loss.mean()), 2),
        "std_loss": round(float(loss.std()), 2),
        **pct,
        "prob_any_loss": round(float((loss > 0).mean()), 4),
        "expected_lost_units": round(float(lost_units.mean()), 1),
        "expected_stockout_days": round(float(stockout_days.mean()), 2),
        "expected_recovery_day": round(float(recovery_day.mean()), 2),
        "histogram": {"counts": counts.tolist(), "edges": [round(float(e), 2) for e in edges]},
    }


def prob_loss_at_least(result: dict) -> tuple[float, float]:
    """(probability, amount): the loss the scenario exceeds with 85% probability (its 15th percentile)."""
    return 0.85, result["p15"]
