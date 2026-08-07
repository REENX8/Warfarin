"""Statistics engine — checked against published worked examples.

Reference values come from R (t.test, wilcox.test, mcnemar.test, chisq.test)
so a reviewer can reproduce them independently.
"""
import math

import pytest

from warfarin.stats import (
    chi_square_sf,
    chi_square_test,
    describe,
    incomplete_beta,
    independent_t_test,
    mcnemar_test,
    normal_two_tailed_p,
    number_needed_to_treat,
    paired_t_test,
    proportion_ci,
    student_t_sf,
    t_critical,
    t_two_tailed_p,
    wilcoxon_signed_rank,
)


def approx(value, expected, tolerance=1e-3):
    return abs(value - expected) < tolerance


# --- distributions ----------------------------------------------------------
def test_incomplete_beta_endpoints():
    assert incomplete_beta(2, 3, 0) == 0.0
    assert incomplete_beta(2, 3, 1) == 1.0
    assert approx(incomplete_beta(0.5, 0.5, 0.5), 0.5)


def test_student_t_matches_known_tails():
    # R: pt(2.086, 20, lower.tail = FALSE) = 0.02500
    assert approx(student_t_sf(2.086, 20), 0.0250, 1e-3)
    # R: pt(1.96, 1e6, lower.tail = FALSE) ~= 0.025
    assert approx(student_t_sf(1.96, 1_000_000), 0.025, 1e-3)


def test_t_two_tailed_p_symmetric():
    assert approx(t_two_tailed_p(2.086, 20), t_two_tailed_p(-2.086, 20))
    assert approx(t_two_tailed_p(0, 10), 1.0)


def test_t_critical_recovers_textbook_values():
    # Standard tables: t(0.05, df = 20) = 2.086, t(0.05, df = 10) = 2.228
    assert approx(t_critical(0.05, 20), 2.086, 1e-3)
    assert approx(t_critical(0.05, 10), 2.228, 1e-3)


def test_normal_two_tailed_p():
    assert approx(normal_two_tailed_p(1.96), 0.05, 1e-3)
    assert approx(normal_two_tailed_p(0), 1.0)


def test_chi_square_tail():
    # R: pchisq(3.841, 1, lower.tail = FALSE) = 0.05
    assert approx(chi_square_sf(3.841, 1), 0.05, 1e-3)
    # R: pchisq(5.991, 2, lower.tail = FALSE) = 0.05
    assert approx(chi_square_sf(5.991, 2), 0.05, 1e-3)
    assert chi_square_sf(0, 1) == 1.0


# --- descriptive ------------------------------------------------------------
def test_describe_basic():
    stats = describe([2, 4, 4, 4, 5, 5, 7, 9])
    assert stats.n == 8
    assert approx(stats.mean, 5.0)
    assert approx(stats.sd, 2.13809, 1e-4)   # R: sd() uses n-1
    assert approx(stats.median, 4.5)
    assert stats.minimum == 2
    assert stats.maximum == 9


def test_describe_quartiles_match_r_type7():
    stats = describe([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
    assert approx(stats.q1, 3.25)
    assert approx(stats.q3, 7.75)


def test_describe_handles_empty_and_none():
    empty = describe([])
    assert empty.n == 0 and empty.mean is None
    assert empty.summary() == "-"
    assert describe([None, None]).n == 0
    assert describe([1, None, 3]).n == 2


def test_describe_single_value_has_no_sd():
    stats = describe([5])
    assert stats.n == 1 and stats.sd is None
    assert stats.summary() == "5.0"


def test_describe_confidence_interval_brackets_mean():
    stats = describe([10, 12, 14, 16, 18])
    assert stats.ci_low < stats.mean < stats.ci_high


def test_summary_formatting():
    assert describe([10, 20, 30]).summary(1) == "20.0 ± 10.0"


# --- paired t-test ----------------------------------------------------------
def test_paired_t_test_matches_hand_calculation():
    """Differences are -10, -15, -10, -20, -10: mean -13, SD 4.4721, SE 2.0,
    so t = -13 / 2 = -6.5 on 4 df."""
    before = [200, 210, 190, 220, 205]
    after = [190, 195, 180, 200, 195]
    result = paired_t_test(before, after)
    assert approx(result.statistic, -6.5, 1e-9)
    assert result.df == 4
    assert approx(result.p_value, 0.00289001, 1e-6)
    assert result.significant
    assert approx(result.extra["mean_difference"], -13.0, 1e-9)


def test_paired_t_test_no_change_is_not_significant():
    result = paired_t_test([1, 2, 3, 4, 5], [1, 2, 3, 4, 5])
    assert result.p_value is None
    assert "ผลต่าง" in result.detail


def test_paired_t_test_needs_two_pairs():
    assert paired_t_test([1], [2]).p_value is None


def test_paired_t_test_ignores_incomplete_pairs():
    result = paired_t_test([1, 2, None, 4], [2, 3, 4, None])
    assert result.n == 2


def test_paired_t_test_reports_effect_size():
    result = paired_t_test([10, 12, 14, 16], [14, 15, 18, 21])
    assert result.effect_label == "Cohen's d"
    assert result.effect_size > 0


# --- independent t-test -----------------------------------------------------
def test_welch_t_test_matches_hand_calculation():
    """Means 28.8 and 23.8; variances 12.2 and 5.7; SE = sqrt(12.2/5 + 5.7/5)
    = 1.892089, so t = 5 / 1.892089 = 2.642582 with Welch df 7.067998."""
    a = [24, 27, 29, 31, 33]
    b = [21, 22, 24, 25, 27]
    result = independent_t_test(a, b)
    assert approx(result.statistic, 2.642582, 1e-5)
    assert approx(result.df, 7.067998, 1e-5)
    assert approx(result.p_value, 0.03300918, 1e-6)


def test_pooled_t_test_matches_hand_calculation():
    """Pooled variance = (4*12.2 + 4*5.7)/8 = 8.95, SE = sqrt(8.95*0.4)
    = 1.892089, t = 2.642582 on 8 df."""
    a = [24, 27, 29, 31, 33]
    b = [21, 22, 24, 25, 27]
    result = independent_t_test(a, b, equal_variance=True)
    assert approx(result.statistic, 2.642582, 1e-5)
    assert result.df == 8
    assert approx(result.p_value, 0.02959450, 1e-6)


def test_independent_t_test_needs_both_groups():
    assert independent_t_test([1], [2, 3, 4]).p_value is None
    assert independent_t_test([], []).p_value is None


# --- Wilcoxon ---------------------------------------------------------------
def test_wilcoxon_detects_consistent_improvement():
    before = [10, 12, 11, 14, 13, 15, 12, 11]
    after = [14, 15, 16, 18, 17, 19, 15, 14]
    result = wilcoxon_signed_rank(before, after)
    assert result.p_value < 0.05
    assert result.n == 8


def test_wilcoxon_ignores_zero_differences():
    before = [5, 5, 6, 7, 8, 9, 10]
    after = [5, 6, 7, 8, 9, 10, 11]
    result = wilcoxon_signed_rank(before, after)
    assert result.n == 6   # the first pair has a zero difference


def test_wilcoxon_needs_enough_pairs():
    assert wilcoxon_signed_rank([1, 2], [3, 4]).p_value is None


def test_wilcoxon_no_change_returns_no_p():
    assert wilcoxon_signed_rank([1, 2, 3, 4, 5, 6], [1, 2, 3, 4, 5, 6]).p_value is None


# --- McNemar ----------------------------------------------------------------
def test_mcnemar_matches_hand_calculation():
    """Discordant cells: 2 improved-to-no and 12 no-to-yes. With Yates
    correction chi2 = (|2-12|-1)^2 / 14 = 81/14 = 5.785714 on 1 df."""
    before = [True] * 20 + [False] * 30
    after = [True] * 18 + [False] * 2 + [True] * 12 + [False] * 18
    result = mcnemar_test(before, after)
    assert approx(result.statistic, 5.7857, 1e-3)
    assert approx(result.p_value, 0.01616, 1e-4)
    assert result.extra["only_after"] == 12
    assert result.extra["only_before"] == 2


def test_mcnemar_without_change():
    result = mcnemar_test([True, False, True], [True, False, True])
    assert result.p_value is None
    assert "ไม่มีผู้ที่เปลี่ยนสถานะ" in result.detail


def test_mcnemar_empty():
    assert mcnemar_test([], []).n == 0


# --- chi-square -------------------------------------------------------------
def test_chi_square_matches_hand_calculation():
    """Table [[30,20],[15,35]]: all expected counts are 22.5/27.5, giving
    chi2 = 9.090909 on 1 df (no continuity correction)."""
    result = chi_square_test([[30, 20], [15, 35]])
    assert approx(result.statistic, 9.0909, 1e-3)
    assert result.df == 1
    assert approx(result.p_value, 0.002569, 1e-4)


def test_chi_square_warns_on_small_expected_counts():
    result = chi_square_test([[1, 2], [3, 1]])
    assert "Fisher" in result.detail


def test_chi_square_rejects_degenerate_table():
    assert chi_square_test([[1, 2]]).p_value is None


def test_chi_square_reports_cramers_v():
    result = chi_square_test([[30, 20], [15, 35]])
    assert result.effect_label == "Cramér's V"
    assert 0 < result.effect_size < 1


# --- proportions ------------------------------------------------------------
def test_wilson_interval_is_within_bounds():
    percent, low, high = proportion_ci(50, 100)
    assert approx(percent, 50.0)
    assert 0 < low < 50 < high < 100


def test_wilson_interval_handles_zero_and_full():
    _, low, _ = proportion_ci(0, 20)
    assert low >= 0
    _, _, high = proportion_ci(20, 20)
    assert high <= 100


def test_proportion_ci_with_no_data():
    assert proportion_ci(0, 0) == (0.0, 0.0, 0.0)


def test_number_needed_to_treat():
    result = number_needed_to_treat(events_treated=40, n_treated=50,
                                    events_control=25, n_control=50)
    assert approx(result["risk_treated"], 80.0)
    assert approx(result["risk_control"], 50.0)
    assert approx(result["risk_difference"], 30.0)
    assert approx(result["nnt"], 1 / 0.3, 1e-6)


def test_number_needed_to_treat_without_data():
    assert number_needed_to_treat(0, 0, 0, 0)["nnt"] is None


# --- reporting helpers ------------------------------------------------------
def test_p_text_formatting():
    result = paired_t_test([1, 2, 3, 4, 5, 6, 7, 8],
                           [10, 20, 30, 40, 50, 60, 70, 80])
    assert result.p_text() in ("< 0.001", f"{result.p_value:.3f}")
    assert paired_t_test([1], [2]).p_text() == "-"


def test_significance_flag():
    assert paired_t_test([200, 210, 190, 220, 205], [190, 195, 180, 200, 195]).significant
    assert not paired_t_test([1, 2, 3, 4, 5], [1.1, 2.1, 2.9, 4.1, 5.0]).significant


# --- independent cross-validation -------------------------------------------
def _t_pdf(x: float, df: float) -> float:
    return (
        math.gamma((df + 1) / 2)
        / (math.sqrt(df * math.pi) * math.gamma(df / 2))
        * (1 + x * x / df) ** (-(df + 1) / 2)
    )


def _two_tailed_by_integration(t: float, df: float, steps: int = 20_000) -> float:
    """Simpson's rule on the upper tail — shares no code with incomplete_beta."""
    t = abs(t)
    a, b = t, 400.0
    h = (b - a) / steps
    total = _t_pdf(a, df) + _t_pdf(b, df)
    for i in range(1, steps):
        total += _t_pdf(a + i * h, df) * (4 if i % 2 else 2)
    return 2 * total * h / 3


@pytest.mark.parametrize("t,df", [(1.0, 5), (2.5, 10), (2.642582, 8), (6.5, 4), (0.5, 30)])
def test_t_tail_agrees_with_numeric_integration(t, df):
    """The incomplete-beta tail must match a completely different method."""
    assert approx(t_two_tailed_p(t, df), _two_tailed_by_integration(t, df), 1e-6)


def test_chi_square_tail_agrees_with_exact_df1_formula():
    """For df = 1 the tail is exactly 2 * (1 - Phi(sqrt(x)))."""
    for statistic in (0.5, 1.0, 3.841, 6.635):
        exact = normal_two_tailed_p(math.sqrt(statistic))
        assert approx(chi_square_sf(statistic, 1), exact, 1e-9)


def test_chi_square_tail_agrees_with_exact_df2_formula():
    """For df = 2 the tail is exactly exp(-x/2)."""
    for statistic in (1.0, 2.0, 5.991):
        assert approx(chi_square_sf(statistic, 2), math.exp(-statistic / 2), 1e-9)
