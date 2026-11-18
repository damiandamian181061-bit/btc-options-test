"""Deribit Standard Margin for BTC_USDC options (Damian's choice, 18 Nov 2026).

Formulas from Deribit's "Linear USDC Options" page (updated 18 Nov 2026 07:14 UTC), per BTC:

- short call IM: max(0.15 - OTM / underlying mark, 0.10) x index + mark, OTM = max(K - F, 0)
- short put IM: max((0.15 - OTM / underlying mark) x index, 0.10 x K) + mark, OTM = max(F - K, 0)
- short call MM: 0.075 x index + mark;  short put MM: 0.075 x min(index, K) + mark
- long options: the premium only.

Every expected number below is worked by hand.
"""

from decimal import Decimal

import pytest

from optlab.risk.margin import MarginRules, OptionMarginInput, option_margin, short_option_im
from tests.conftest import REPO_ROOT

D = Decimal


@pytest.fixture
def rules() -> MarginRules:
    from optlab.risk.limits import load_risk_config

    return load_risk_config(REPO_ROOT / "configs", "v1").margin


def test_the_rules_record_their_source(rules: MarginRules) -> None:
    assert rules.account == "standard"
    assert rules.source.endswith("31424932728093-Linear-USDC-Options")
    assert (rules.short_option_im_base, rules.short_option_im_floor) == (D("0.15"), D("0.10"))
    assert rules.short_option_mm == D("0.075")


@pytest.mark.parametrize(
    ("option_type", "strike", "mark", "im", "mm"),
    [
        # Out-of-the-money put, 5,000 below F = 65,000: (0.15 - 5,000/65,000) x 65,000 = 4,750
        # < 0.10 x 60,000 = 6,000, so 6,000 + 800 = 6,800. MM 0.075 x 60,000 + 800 = 5,300.
        ("put", "60000", "800", "6800", "5300"),
        # Out-of-the-money call, 2,000 above: (0.15 x 65,000 - 2,000) = 7,750 > 6,500;
        # + 1,500 = 9,250. MM 0.075 x 65,000 + 1,500 = 6,375.
        ("call", "67000", "1500", "9250", "6375"),
        # Far call, 10,000 above: the 0.10 floor binds: 6,500 + 300 = 6,800; MM 4,875 + 300.
        ("call", "75000", "300", "6800", "5175"),
        # In-the-money put (K above F): OTM 0, 0.15 x 65,000 = 9,750 > 7,000; + 5,200.
        # MM 0.075 x min(65,000, 70,000) + 5,200 = 10,075.
        ("put", "70000", "5200", "14950", "10075"),
    ],
)
def test_short_option_margin_per_btc(
    rules: MarginRules, option_type: str, strike: str, mark: str, im: str, mm: str
) -> None:
    leg = OptionMarginInput(option_type, D(strike), D("-1"), D(mark), D("65000"), D("65000"))
    got = option_margin([leg], rules)
    assert (got.initial, got.maintenance) == (D(im), D(mm))


def test_a_long_option_needs_no_margin_and_a_long_leg_gives_no_offset(rules: MarginRules) -> None:
    short = OptionMarginInput("put", D("60000"), D("-0.5"), D("800"), D("65000"), D("65000"))
    long = OptionMarginInput("put", D("58000"), D("0.5"), D("450"), D("65000"), D("65000"))
    assert option_margin([long], rules).initial == 0
    # Standard Margin: the bull put spread margins its short leg in full (0.5 x 6,800).
    both = option_margin([short, long], rules)
    assert (both.initial, both.maintenance) == (D("3400"), D("2650"))
    assert both.short_notional == D("32500")  # 0.5 BTC x the 65,000 index


def test_index_and_underlying_mark_can_differ(rules: MarginRules) -> None:
    # Call, K 67,000, F 66,000 (the future), index 65,000: OTM 1,000;
    # 0.15 - 1,000/66,000 = 0.134848...; x 65,000 = 8,765.15... (> 6,500); + 1,500.
    im = short_option_im("call", D("67000"), D("65000"), D("66000"), D("1500"), rules)
    expected = (D("0.15") - D("1000") / D("66000")) * D("65000") + D("1500")
    assert abs(im - expected) < D("1E-9")
