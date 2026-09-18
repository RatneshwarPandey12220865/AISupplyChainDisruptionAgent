from app.simulation import Inbound, MonteCarloSimulator, ScenarioInput


def scenario(**kw):
    base = dict(on_hand=700, demand_mean=100, demand_std=20, loss_per_unit=10, horizon_days=30, runs=4000, seed=1)
    return ScenarioInput(**{**base, **kw})


def test_no_shortage_when_stock_covers_horizon():
    r = MonteCarloSimulator(scenario(on_hand=10_000)).run([])
    assert r["expected_loss"] == 0 and r["prob_any_loss"] == 0


def test_delay_increases_loss_and_mitigation_reduces_it():
    sim = MonteCarloSimulator(scenario())
    on_time = sim.run([Inbound("s", 3000, base_day=7)])
    delayed = sim.run([Inbound("s", 3000, base_day=7, delay_mean=8, delay_std=3)])
    mitigated = sim.run([Inbound("s", 3000, base_day=7, delay_mean=8, delay_std=3), Inbound("alt", 800, base_day=6)])
    assert on_time["expected_loss"] < delayed["expected_loss"]
    assert mitigated["expected_loss"] < delayed["expected_loss"]
    assert delayed["p15"] <= delayed["p50"] <= delayed["p85"] <= delayed["p95"]


def test_reproducible_with_same_seed():
    a = MonteCarloSimulator(scenario()).run([Inbound("s", 3000, base_day=7, delay_mean=5, delay_std=2)])
    b = MonteCarloSimulator(scenario()).run([Inbound("s", 3000, base_day=7, delay_mean=5, delay_std=2)])
    assert a == b


def test_daily_penalty_applies_per_stockout_day():
    no_pen = MonteCarloSimulator(scenario(demand_std=0.001)).run([])
    pen = MonteCarloSimulator(scenario(demand_std=0.001, daily_penalty=100)).run([])
    assert abs(pen["expected_loss"] - no_pen["expected_loss"] - 100 * no_pen["expected_stockout_days"]) < 1
