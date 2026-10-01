import numpy as np

from sports_aggregator.nfl.distribution_calibration import (
    KS, _empirical_pmf, _key_weights, _pmf, _score, _tail,
)


def test_pmf_sums_to_one_and_tail_symmetry():
    pmf = _pmf(0.0, 13.0)
    assert abs(pmf.sum() - 1.0) < 1e-9
    assert abs(_tail(pmf, 0.0) - 0.5) < 1e-9


def test_push_mass_is_removed_on_integer_thresholds():
    pmf = _pmf(3.0, 13.0)
    on_line, off_line = _tail(pmf, 3.0), _tail(pmf, 3.5)
    assert abs(on_line - 0.5) < 0.03          # push removed, roughly even
    assert off_line < on_line                 # a half point harder to clear


def test_key_weights_boost_overrepresented_margins():
    rng = np.random.default_rng(0)
    margins = np.rint(rng.normal(2, 13, 6000)).astype(int)
    margins[:600] = 3                         # inject a key-number pile-up
    w = dict(zip(KS, _key_weights(margins, 13.0)))
    assert w[3] > 1.5 and abs(w[20] - 1.0) < 0.6 and w[60] == 1.0


def test_empirical_pmf_is_normalised_and_never_zero():
    pmf = _empirical_pmf(2.0, np.array([-3.0, 0.0, 4.0]))
    assert abs(pmf.sum() - 1.0) < 1e-9 and (pmf > 0).all()


def test_score_penalises_confident_misses():
    good = _score([(0.9, 1), (0.1, 0)])
    bad = _score([(0.9, 0), (0.1, 1)])
    assert good["log_loss"] < 0.2 < bad["log_loss"]
