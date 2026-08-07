"""Statistics for the research module.

Pure standard library on purpose: the clinic deploys on a small host and
adding SciPy/NumPy to the dependency chain for a handful of tests is not worth
the install size or the version risk. Every function here is checked against
published worked examples in tests/test_stats.py.

Distribution tails use continued-fraction expansions of the incomplete beta
and gamma functions (Numerical Recipes, ch. 6), which are accurate to well
beyond the 3-4 significant figures a paper reports.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

MAX_ITERATIONS = 300
EPSILON = 3.0e-12


# ---------------------------------------------------------------------------
# Distribution helpers
# ---------------------------------------------------------------------------
def _log_beta(a: float, b: float) -> float:
    return math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta function."""
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < 1e-30:
        d = 1e-30
    d = 1.0 / d
    h = d
    for m in range(1, MAX_ITERATIONS + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-30:
            d = 1e-30
        c = 1.0 + aa / c
        if abs(c) < 1e-30:
            c = 1e-30
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-30:
            d = 1e-30
        c = 1.0 + aa / c
        if abs(c) < 1e-30:
            c = 1e-30
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < EPSILON:
            break
    return h


def incomplete_beta(a: float, b: float, x: float) -> float:
    """Regularised incomplete beta function I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    front = math.exp(a * math.log(x) + b * math.log(1.0 - x) - _log_beta(a, b))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def student_t_sf(t: float, df: float) -> float:
    """One-tailed survival function P(T > t) for Student's t."""
    if df <= 0:
        return float("nan")
    x = df / (df + t * t)
    tail = 0.5 * incomplete_beta(df / 2.0, 0.5, x)
    return tail if t > 0 else 1.0 - tail


def t_two_tailed_p(t: float, df: float) -> float:
    """Two-tailed p-value for a t statistic."""
    if df <= 0 or math.isnan(t):
        return float("nan")
    return min(1.0, 2.0 * student_t_sf(abs(t), df))


def normal_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def normal_two_tailed_p(z: float) -> float:
    return min(1.0, 2.0 * (1.0 - normal_cdf(abs(z))))


def _gammap_series(a: float, x: float) -> float:
    ap = a
    total = 1.0 / a
    delta = total
    for _ in range(MAX_ITERATIONS):
        ap += 1.0
        delta *= x / ap
        total += delta
        if abs(delta) < abs(total) * EPSILON:
            break
    return total * math.exp(-x + a * math.log(x) - math.lgamma(a))


def _gammaq_continued(a: float, x: float) -> float:
    b = x + 1.0 - a
    c = 1.0 / 1e-30
    d = 1.0 / b
    h = d
    for i in range(1, MAX_ITERATIONS + 1):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        if abs(d) < 1e-30:
            d = 1e-30
        c = b + an / c
        if abs(c) < 1e-30:
            c = 1e-30
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < EPSILON:
            break
    return h * math.exp(-x + a * math.log(x) - math.lgamma(a))


def chi_square_sf(statistic: float, df: int) -> float:
    """Upper-tail probability of the chi-square distribution."""
    if df <= 0 or statistic < 0:
        return float("nan")
    if statistic == 0:
        return 1.0
    a, x = df / 2.0, statistic / 2.0
    if x < a + 1.0:
        return 1.0 - _gammap_series(a, x)
    return _gammaq_continued(a, x)


# ---------------------------------------------------------------------------
# Descriptive statistics
# ---------------------------------------------------------------------------
@dataclass
class Descriptive:
    n: int
    mean: float | None
    sd: float | None
    median: float | None
    q1: float | None
    q3: float | None
    minimum: float | None
    maximum: float | None
    ci_low: float | None
    ci_high: float | None

    def summary(self, digits: int = 1) -> str:
        """'mean ± sd' — the usual way a paper reports a continuous variable."""
        if self.n == 0 or self.mean is None:
            return "-"
        if self.sd is None:
            return f"{self.mean:.{digits}f}"
        return f"{self.mean:.{digits}f} ± {self.sd:.{digits}f}"

    def median_iqr(self, digits: int = 1) -> str:
        if self.n == 0 or self.median is None:
            return "-"
        return f"{self.median:.{digits}f} ({self.q1:.{digits}f}–{self.q3:.{digits}f})"


def _percentile(sorted_values: list[float], fraction: float) -> float:
    """Linear-interpolation percentile (the R type-7 / Excel definition)."""
    if not sorted_values:
        return float("nan")
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = fraction * (len(sorted_values) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return sorted_values[int(position)]
    weight = position - lower
    return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight


def describe(values) -> Descriptive:
    """Mean, SD, median, IQR, range and the 95% CI of the mean."""
    clean = [float(v) for v in values if v is not None and not _is_nan(v)]
    n = len(clean)
    if n == 0:
        return Descriptive(0, None, None, None, None, None, None, None, None, None)
    mean = sum(clean) / n
    if n == 1:
        return Descriptive(1, mean, None, mean, mean, mean, mean, mean, None, None)
    variance = sum((v - mean) ** 2 for v in clean) / (n - 1)
    sd = math.sqrt(variance)
    ordered = sorted(clean)
    standard_error = sd / math.sqrt(n)
    margin = t_critical(0.05, n - 1) * standard_error
    return Descriptive(
        n=n, mean=mean, sd=sd,
        median=_percentile(ordered, 0.5),
        q1=_percentile(ordered, 0.25),
        q3=_percentile(ordered, 0.75),
        minimum=ordered[0], maximum=ordered[-1],
        ci_low=mean - margin, ci_high=mean + margin,
    )


def _is_nan(value) -> bool:
    try:
        return math.isnan(float(value))
    except (TypeError, ValueError):
        return True


def t_critical(alpha: float, df: int) -> float:
    """Two-tailed critical t value, found by bisection on the CDF."""
    if df <= 0:
        return float("nan")
    target = alpha / 2.0
    low, high = 0.0, 200.0
    for _ in range(200):
        mid = (low + high) / 2.0
        if student_t_sf(mid, df) > target:
            low = mid
        else:
            high = mid
    return (low + high) / 2.0


# ---------------------------------------------------------------------------
# Hypothesis tests
# ---------------------------------------------------------------------------
@dataclass
class TestResult:
    name: str
    statistic: float | None
    p_value: float | None
    df: float | None = None
    n: int = 0
    effect_size: float | None = None
    effect_label: str = ""
    detail: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def significant(self) -> bool:
        return self.p_value is not None and self.p_value < 0.05

    def p_text(self) -> str:
        """APA-style p-value formatting."""
        if self.p_value is None or math.isnan(self.p_value):
            return "-"
        if self.p_value < 0.001:
            return "< 0.001"
        return f"{self.p_value:.3f}"


def paired_t_test(before, after) -> TestResult:
    """Paired-samples t-test on before/after measurements of the same people."""
    pairs = [
        (float(b), float(a))
        for b, a in zip(before, after, strict=False)
        if b is not None and a is not None and not _is_nan(b) and not _is_nan(a)
    ]
    n = len(pairs)
    if n < 2:
        return TestResult("Paired t-test", None, None, n=n,
                          detail="ต้องมีข้อมูลคู่อย่างน้อย 2 ราย")
    differences = [a - b for b, a in pairs]
    stats = describe(differences)
    if not stats.sd or stats.sd == 0:
        return TestResult("Paired t-test", None, None, n=n,
                          detail="ผลต่างเท่ากันทุกรายจึงคำนวณค่า p ไม่ได้")
    t = stats.mean / (stats.sd / math.sqrt(n))
    df = n - 1
    # Cohen's d for paired data uses the SD of the differences.
    return TestResult(
        "Paired t-test", t, t_two_tailed_p(t, df), df=df, n=n,
        effect_size=stats.mean / stats.sd, effect_label="Cohen's d",
        detail=f"ผลต่างเฉลี่ย {stats.mean:.2f} (95% CI {stats.ci_low:.2f} ถึง {stats.ci_high:.2f})",
        extra={"mean_difference": stats.mean, "ci_low": stats.ci_low, "ci_high": stats.ci_high},
    )


def independent_t_test(group_a, group_b, equal_variance: bool = False) -> TestResult:
    """Two-sample t-test; Welch's by default since group sizes usually differ."""
    a = describe(group_a)
    b = describe(group_b)
    if a.n < 2 or b.n < 2:
        return TestResult("Independent t-test", None, None, n=a.n + b.n,
                          detail="แต่ละกลุ่มต้องมีข้อมูลอย่างน้อย 2 ราย")
    if a.sd == 0 and b.sd == 0:
        return TestResult("Independent t-test", None, None, n=a.n + b.n,
                          detail="ไม่มีความแปรปรวนในข้อมูล")

    va, vb = a.sd ** 2, b.sd ** 2
    if equal_variance:
        pooled = ((a.n - 1) * va + (b.n - 1) * vb) / (a.n + b.n - 2)
        standard_error = math.sqrt(pooled * (1 / a.n + 1 / b.n))
        df = a.n + b.n - 2
        name = "Independent t-test (pooled)"
        effect = (a.mean - b.mean) / math.sqrt(pooled) if pooled > 0 else None
    else:
        standard_error = math.sqrt(va / a.n + vb / b.n)
        numerator = (va / a.n + vb / b.n) ** 2
        denominator = (va / a.n) ** 2 / (a.n - 1) + (vb / b.n) ** 2 / (b.n - 1)
        df = numerator / denominator if denominator else a.n + b.n - 2
        name = "Welch's t-test"
        pooled = ((a.n - 1) * va + (b.n - 1) * vb) / (a.n + b.n - 2)
        effect = (a.mean - b.mean) / math.sqrt(pooled) if pooled > 0 else None
    if standard_error == 0:
        return TestResult(name, None, None, n=a.n + b.n, detail="ค่าเบี่ยงเบนเป็นศูนย์")
    t = (a.mean - b.mean) / standard_error
    return TestResult(
        name, t, t_two_tailed_p(t, df), df=df, n=a.n + b.n,
        effect_size=effect, effect_label="Cohen's d",
        detail=f"ผลต่างค่าเฉลี่ย {a.mean - b.mean:.2f}",
        extra={"mean_a": a.mean, "mean_b": b.mean, "n_a": a.n, "n_b": b.n},
    )


def wilcoxon_signed_rank(before, after) -> TestResult:
    """Non-parametric paired test — used when the outcome is skewed.

    Normal approximation with a continuity correction and tie correction; that
    is what statistics packages use once n exceeds about 20, and it is a
    reasonable approximation below that for a descriptive clinic report.
    """
    pairs = [
        (float(b), float(a))
        for b, a in zip(before, after, strict=False)
        if b is not None and a is not None and not _is_nan(b) and not _is_nan(a)
    ]
    differences = [a - b for b, a in pairs if a - b != 0]
    n = len(differences)
    if n < 5:
        return TestResult("Wilcoxon signed-rank", None, None, n=n,
                          detail="ต้องมีผลต่างที่ไม่เป็นศูนย์อย่างน้อย 5 ราย")

    ranked = _average_ranks([abs(d) for d in differences])
    w_plus = sum(rank for diff, rank in zip(differences, ranked, strict=True) if diff > 0)
    w_minus = sum(rank for diff, rank in zip(differences, ranked, strict=True) if diff < 0)
    w = min(w_plus, w_minus)

    mean_w = n * (n + 1) / 4.0
    tie_correction = _tie_correction([abs(d) for d in differences])
    variance = (n * (n + 1) * (2 * n + 1) - tie_correction / 2.0) / 24.0
    if variance <= 0:
        return TestResult("Wilcoxon signed-rank", w, None, n=n, detail="ความแปรปรวนเป็นศูนย์")
    z = (w - mean_w + 0.5) / math.sqrt(variance)
    return TestResult(
        "Wilcoxon signed-rank", w, normal_two_tailed_p(z), n=n,
        effect_size=abs(z) / math.sqrt(n), effect_label="r",
        detail=f"W+ = {w_plus:.1f}, W− = {w_minus:.1f}, z = {z:.3f}",
        extra={"w_plus": w_plus, "w_minus": w_minus, "z": z},
    )


def _average_ranks(values: list[float]) -> list[float]:
    """Ranks with ties averaged, keeping the original ordering."""
    ordered = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    index = 0
    while index < len(ordered):
        end = index
        while end + 1 < len(ordered) and values[ordered[end + 1]] == values[ordered[index]]:
            end += 1
        average = (index + end) / 2.0 + 1.0
        for position in range(index, end + 1):
            ranks[ordered[position]] = average
        index = end + 1
    return ranks


def _tie_correction(values: list[float]) -> float:
    counts: dict[float, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return sum(c ** 3 - c for c in counts.values() if c > 1)


def mcnemar_test(before, after) -> TestResult:
    """Paired test for a yes/no outcome measured twice on the same people."""
    pairs = [
        (bool(b), bool(a))
        for b, a in zip(before, after, strict=False)
        if b is not None and a is not None
    ]
    if not pairs:
        return TestResult("McNemar's test", None, None, n=0, detail="ไม่มีข้อมูลคู่")
    both_yes = sum(1 for b, a in pairs if b and a)
    only_before = sum(1 for b, a in pairs if b and not a)
    only_after = sum(1 for b, a in pairs if not b and a)
    both_no = sum(1 for b, a in pairs if not b and not a)
    discordant = only_before + only_after
    table = {
        "both_yes": both_yes, "only_before": only_before,
        "only_after": only_after, "both_no": both_no,
    }
    if discordant == 0:
        return TestResult("McNemar's test", None, None, n=len(pairs),
                          detail="ไม่มีผู้ที่เปลี่ยนสถานะ", extra=table)
    # Yates continuity correction — standard when the discordant count is small.
    statistic = (abs(only_before - only_after) - 1) ** 2 / discordant
    return TestResult(
        "McNemar's test", statistic, chi_square_sf(statistic, 1), df=1, n=len(pairs),
        detail=f"เปลี่ยนเป็นดีขึ้น {only_after} ราย, แย่ลง {only_before} ราย",
        extra=table,
    )


def chi_square_test(table: list[list[int]]) -> TestResult:
    """Chi-square test of independence on a contingency table."""
    rows = len(table)
    columns = len(table[0]) if rows else 0
    if rows < 2 or columns < 2:
        return TestResult("Chi-square test", None, None, detail="ต้องมีตารางอย่างน้อย 2×2")
    total = sum(sum(row) for row in table)
    if total == 0:
        return TestResult("Chi-square test", None, None, detail="ไม่มีข้อมูล")
    row_totals = [sum(row) for row in table]
    column_totals = [sum(table[r][c] for r in range(rows)) for c in range(columns)]

    statistic = 0.0
    minimum_expected = float("inf")
    for r in range(rows):
        for c in range(columns):
            expected = row_totals[r] * column_totals[c] / total
            minimum_expected = min(minimum_expected, expected)
            if expected > 0:
                statistic += (table[r][c] - expected) ** 2 / expected
    df = (rows - 1) * (columns - 1)
    note = ""
    if minimum_expected < 5:
        note = "⚠️ มีช่องที่ค่าคาดหวัง < 5 ควรใช้ Fisher's exact test แทน"
    cramers_v = (
        math.sqrt(statistic / (total * min(rows - 1, columns - 1)))
        if total and min(rows - 1, columns - 1) else None
    )
    return TestResult(
        "Chi-square test", statistic, chi_square_sf(statistic, df), df=df, n=total,
        effect_size=cramers_v, effect_label="Cramér's V", detail=note,
        extra={"min_expected": minimum_expected},
    )


def proportion_ci(successes: int, total: int) -> tuple[float, float, float]:
    """Proportion with a Wilson 95% CI (behaves near 0% and 100%)."""
    if total <= 0:
        return 0.0, 0.0, 0.0
    proportion = successes / total
    z = 1.959963985
    denominator = 1 + z * z / total
    centre = (proportion + z * z / (2 * total)) / denominator
    margin = (
        z * math.sqrt(proportion * (1 - proportion) / total + z * z / (4 * total * total))
        / denominator
    )
    return proportion * 100, max(0.0, (centre - margin) * 100), min(100.0, (centre + margin) * 100)


def number_needed_to_treat(
    events_treated: int, n_treated: int, events_control: int, n_control: int
) -> dict:
    """Risk difference and NNT for a binary outcome between two arms."""
    if n_treated <= 0 or n_control <= 0:
        return {"risk_treated": None, "risk_control": None, "risk_difference": None, "nnt": None}
    risk_treated = events_treated / n_treated
    risk_control = events_control / n_control
    difference = risk_treated - risk_control
    return {
        "risk_treated": risk_treated * 100,
        "risk_control": risk_control * 100,
        "risk_difference": difference * 100,
        "nnt": abs(1 / difference) if difference else None,
    }
