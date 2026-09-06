from codeandconfirm.perf import compare_series


def test_regression_detected_beyond_noise():
    base = [1000, 1010, 990, 1005, 995]
    cand = [1600, 1590, 1610, 1605, 1595]
    r = compare_series(base, cand, pct_budget=25)
    assert r["verdict"] == "regression" and r["delta"] > r["threshold"]


def test_within_budget_is_not_regression():
    base = [1000, 1010, 990, 1005, 995]
    cand = [1100, 1110, 1090, 1105, 1095]      # +10% < 25% budget
    assert compare_series(base, cand, 25)["verdict"] == "within noise"


def test_noisy_base_widens_threshold():
    base = [800, 1200, 900, 1300, 1000]        # MAD = 100 → floor 300 (> 25% of 1000 = 250)
    cand = [1250, 1260, 1240, 1255, 1245]
    r = compare_series(base, cand, 25)
    assert r["threshold"] == 600 and r["verdict"] == "within noise"


def test_insufficient_samples_is_inconclusive():
    assert compare_series([1, 2], [1, 2, 3], 25)["verdict"] == "inconclusive"


def test_improvement():
    assert compare_series([1000] * 5, [500] * 5, 25)["verdict"] == "improvement"
