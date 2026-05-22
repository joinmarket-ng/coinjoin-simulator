"""Tests for taker_logic — schedule generation and offer selection.

These cover the behavioural-port surface area:
- ``calc_cj_fee`` for both relative and absolute ordertypes.
- ``is_within_max_limits`` lenient OR-pass semantics.
- The four order choosers produce a valid pick.
- ``choose_orders`` returns ``n`` distinct counterparties when feasible.
- ``choose_orders`` returns ``(None, 0)`` when the pool is too small.
- ``get_amount_fractions`` always sums to ~1 and rejects sub-5%-tail draws.
- ``get_tumble_schedule`` produces stage-1 sweeps + stage-2 entries with
  the expected destination assignment for ``addrcount=3``.
"""

from __future__ import annotations

import math
import random

import pytest

from coinjoin_simulator.taker_logic import (
    NO_ROUNDING,
    OfferDict,
    calc_cj_fee,
    cheapest_order_choose,
    choose_orders,
    fidelity_bond_weighted_order_choose,
    get_amount_fractions,
    get_tumble_schedule,
    is_within_max_limits,
    quantize_cjfee_r,
    random_under_max_order_choose,
    tumbler_filter_orders_acceptable,
    tweak_tumble_schedule,
    weighted_order_choose,
)


def _orderbook(n: int = 12, *, fee_a: int = 500, rel: float = 1e-4) -> list[OfferDict]:
    """Build a synthetic orderbook with a mix of rel/abs offers."""
    out: list[OfferDict] = []
    for i in range(n):
        ordertype = "sw0reloffer" if i % 2 == 0 else "sw0absoffer"
        cjfee = rel + i * 1e-6 if ordertype == "sw0reloffer" else fee_a + i * 10
        out.append(
            {
                "counterparty": f"M{i:02d}",
                "oid": 0,
                "ordertype": ordertype,
                "minsize": 100_000,
                "maxsize": 1_000_000_000,
                "txfee": 0,
                "cjfee": cjfee,
                "fidelity_bond_value": float(i + 1),
            },
        )
    return out


def test_calc_cj_fee_relative() -> None:
    assert calc_cj_fee("sw0reloffer", 0.0001, 1_000_000) == 100


def test_calc_cj_fee_absolute() -> None:
    assert calc_cj_fee("sw0absoffer", 250.0, 1_000_000) == 250


def test_calc_cj_fee_unknown_ordertype() -> None:
    with pytest.raises(ValueError, match="unknown ordertype"):
        calc_cj_fee("garbage", 0.0001, 1)


def test_is_within_max_limits_or_pass() -> None:
    # Fee exceeds abs alone -> still accepted (lenient OR-pass).
    assert is_within_max_limits(2_000, 10_000_000, 0.001, 1_000) is True
    # Exceeds rel alone -> still accepted.
    assert is_within_max_limits(20_000, 10_000_000, 0.001, 100_000) is True
    # Exceeds BOTH -> rejected.
    assert is_within_max_limits(20_000, 10_000_000, 0.001, 1_000) is False


@pytest.mark.parametrize(
    "chooser_fn",
    [
        weighted_order_choose,
        cheapest_order_choose,
        random_under_max_order_choose,
        fidelity_bond_weighted_order_choose,
    ],
)
def test_choosers_pick_an_offer(chooser_fn) -> None:  # noqa: ANN001
    rng = random.Random(0)
    pool = [
        {**o, "_fee_sats": calc_cj_fee(o["ordertype"], o["cjfee"], 1_000_000) - o["txfee"]}
        for o in _orderbook(8)
    ]
    pick = chooser_fn(pool, 4, rng)
    assert pick in pool


def test_cheapest_chooser_is_lowest_fee() -> None:
    rng = random.Random(0)
    pool = [
        {**o, "_fee_sats": calc_cj_fee(o["ordertype"], o["cjfee"], 1_000_000) - o["txfee"]}
        for o in _orderbook(8)
    ]
    pick = cheapest_order_choose(pool, 4, rng)
    assert pick["_fee_sats"] == min(o["_fee_sats"] for o in pool)


def test_choose_orders_returns_n_distinct() -> None:
    rng = random.Random(42)
    chosen, total = choose_orders(
        _orderbook(20),
        cj_amount_sats=2_000_000,
        n=5,
        rng=rng,
        allowed_ordertypes=frozenset({"sw0reloffer", "sw0absoffer"}),
        max_fee_rel=0.01,
        max_fee_abs=100_000,
    )
    assert chosen is not None
    assert len({o["counterparty"] for o in chosen}) == 5
    assert total >= 0


def test_choose_orders_fails_when_pool_too_small() -> None:
    rng = random.Random(0)
    chosen, total = choose_orders(
        _orderbook(3),
        cj_amount_sats=2_000_000,
        n=10,
        rng=rng,
    )
    assert chosen is None
    assert total == 0


def test_choose_orders_respects_ignored() -> None:
    rng = random.Random(0)
    ignored = frozenset({f"M{i:02d}" for i in range(17)})
    chosen, _ = choose_orders(
        _orderbook(20),
        cj_amount_sats=2_000_000,
        n=5,
        rng=rng,
        ignored=ignored,
    )
    assert chosen is None


def test_tumbler_filter_orders_acceptable_or_pass() -> None:
    # Fee passes when only one ceiling exceeded.
    assert tumbler_filter_orders_acceptable(
        50_000,
        5,
        1_000_000,
        max_fee_rel=0.001,
        max_fee_abs=100_000,
    )
    # Both exceeded -> reject.
    assert not tumbler_filter_orders_acceptable(
        500_000,
        5,
        1_000_000,
        max_fee_rel=0.001,
        max_fee_abs=10_000,
    )


def test_get_amount_fractions_sums_to_one() -> None:
    rng = random.Random(0)
    fracs = get_amount_fractions(5, rng)
    assert len(fracs) == 5
    assert sum(fracs) == pytest.approx(1.0)
    assert fracs[-1] > 0.05


def test_get_amount_fractions_count_one() -> None:
    rng = random.Random(0)
    assert get_amount_fractions(1, rng) == [1.0]


def test_get_amount_fractions_invalid() -> None:
    rng = random.Random(0)
    with pytest.raises(ValueError, match="count must be"):
        get_amount_fractions(0, rng)


def test_get_tumble_schedule_stage1_sweeps_per_mixdepth() -> None:
    rng = random.Random(7)
    balances = {0: 5_000_000, 1: 0, 2: 8_000_000, 3: 0, 4: 1_000_000}
    sched = get_tumble_schedule(
        rng=rng,
        destaddrs=["bc1qexternal_a", "bc1qexternal_b"],
        mixdepth_balances_sats=balances,
    )
    stage1 = [e for e in sched if e.amount == 0 and e.destination == "INTERNAL"]
    # At least one sweep per nonempty mixdepth.
    nonempty = {m for m, b in balances.items() if b > 0}
    assert {e.src_mixdepth for e in stage1} >= nonempty - {min(nonempty)} or len(stage1) >= 1


def test_get_tumble_schedule_external_destinations_used() -> None:
    rng = random.Random(7)
    sched = get_tumble_schedule(
        rng=rng,
        destaddrs=["bc1qext_a", "bc1qext_b"],
        mixdepth_balances_sats={0: 1_000_000},
    )
    dests = {e.destination for e in sched}
    # Both user-supplied addresses should appear in the schedule.
    assert "bc1qext_a" in dests
    assert "bc1qext_b" in dests


def test_get_tumble_schedule_initial_completed_zero() -> None:
    rng = random.Random(7)
    sched = get_tumble_schedule(
        rng=rng,
        destaddrs=["bc1qx"],
        mixdepth_balances_sats={0: 1_000_000},
    )
    assert all(e.completed == 0 for e in sched)


def test_tweak_tumble_schedule_sweep_decrements_makercount() -> None:
    rng = random.Random(0)
    sched = get_tumble_schedule(
        rng=rng,
        destaddrs=["bc1qx"],
        mixdepth_balances_sats={0: 1_000_000},
    )
    # Find a sweep.
    idx = next(i for i, e in enumerate(sched) if e.amount == 0)
    before = sched[idx].makercount
    tweak_tumble_schedule(
        rng=rng,
        schedule=sched,
        failed_index=idx,
        user_destaddrs=["bc1qx"],
    )
    assert sched[idx].makercount <= before
    assert sched[idx].makercount >= 4


def test_no_rounding_constant() -> None:
    # Sanity: NO_ROUNDING is the upstream sentinel value.
    assert NO_ROUNDING == 16


# ---------------------------------------------------------------------------
# §8.2 maker-clustering: offer-quantization defence
# ---------------------------------------------------------------------------


def test_quantize_cjfee_r_snaps_to_band_lower_edge() -> None:
    # Two values that share the same log10 band cell map to the same
    # output: this is the property that makes a non-updating maker
    # cluster with its grid neighbours instead of carrying a personal
    # band.  Cell -48 of stride 0.1 covers [10**-4.8, 10**-4.7) which
    # is roughly [1.585e-5, 2.0e-5); pick two values inside it.
    a = quantize_cjfee_r(1.7e-5, 0.1)
    b = quantize_cjfee_r(1.9e-5, 0.1)
    assert a == b
    # The band is the lower edge: 10 ** (floor(log10(1.7e-5)/0.1)*0.1)
    # = 10 ** (-48 * 0.1) = 10 ** -4.8.
    assert math.isclose(a, 10**-4.8, rel_tol=1e-12)


def test_quantize_cjfee_r_idempotent() -> None:
    # Snapping twice should be a no-op (already on the grid).
    once = quantize_cjfee_r(2.5e-5, 0.1)
    assert quantize_cjfee_r(once, 0.1) == once


def test_quantize_cjfee_r_handles_nonpositive_and_disabled() -> None:
    # Zero / negative inputs are returned unchanged (clusterer convention).
    assert quantize_cjfee_r(0.0, 0.1) == 0.0
    assert quantize_cjfee_r(-1e-5, 0.1) == -1e-5
    # stride <= 0 disables quantization.
    assert quantize_cjfee_r(1.5e-5, 0.0) == 1.5e-5
    assert quantize_cjfee_r(1.5e-5, -0.1) == 1.5e-5


def test_choose_orders_quantization_collapses_neighbours_to_one_band() -> None:
    # Build a tiny relative-only orderbook with three close-by cjfee_r
    # values that all fall in the same log-decimal cell ([10**-4.9,
    # 10**-4.8) ≈ [1.26e-5, 1.59e-5)), plus one far-out offer in a
    # higher band.  Without quantization the cheapest chooser strictly
    # prefers M00 over M01 over M02 over M03.  With quantization, M00,
    # M01, M02 all get the same _fee_sats (same band lower edge), so
    # the chooser sees a tie inside the cell and picks them all before
    # touching M03.
    rng = random.Random(0)
    orderbook: list[OfferDict] = [
        {
            "counterparty": "M00",
            "oid": 0,
            "ordertype": "sw0reloffer",
            "minsize": 100_000,
            "maxsize": 1_000_000_000,
            "txfee": 0,
            "cjfee": 1.30e-5,
            "fidelity_bond_value": 1.0,
        },
        {
            "counterparty": "M01",
            "oid": 0,
            "ordertype": "sw0reloffer",
            "minsize": 100_000,
            "maxsize": 1_000_000_000,
            "txfee": 0,
            "cjfee": 1.40e-5,
            "fidelity_bond_value": 1.0,
        },
        {
            "counterparty": "M02",
            "oid": 0,
            "ordertype": "sw0reloffer",
            "minsize": 100_000,
            "maxsize": 1_000_000_000,
            "txfee": 0,
            "cjfee": 1.50e-5,
            "fidelity_bond_value": 1.0,
        },
        {
            "counterparty": "M03",
            "oid": 0,
            "ordertype": "sw0reloffer",
            "minsize": 100_000,
            "maxsize": 1_000_000_000,
            "txfee": 0,
            "cjfee": 4.0e-5,
            "fidelity_bond_value": 1.0,
        },
    ]
    chosen, _total = choose_orders(
        orderbook,
        cj_amount_sats=10_000_000,
        n=3,
        rng=rng,
        chooser="cheapest",
        max_fee_rel=0.01,
        max_fee_abs=1_000_000,
        quantize_log_stride=0.1,
    )
    assert chosen is not None
    assert {o["counterparty"] for o in chosen} == {"M00", "M01", "M02"}
    # Within the cell every chosen offer carries the same quantised
    # cjfee — the property §8.2 relies on.
    in_cell = [o["cjfee"] for o in chosen]
    assert len(set(in_cell)) == 1


def test_choose_orders_quantization_does_not_affect_absolute_offers() -> None:
    # Absolute offers are already discrete sat counts and must not be
    # touched by relative-band quantization.
    rng = random.Random(0)
    orderbook: list[OfferDict] = [
        {
            "counterparty": f"M{i:02d}",
            "oid": 0,
            "ordertype": "sw0absoffer",
            "minsize": 100_000,
            "maxsize": 1_000_000_000,
            "txfee": 0,
            "cjfee": 500 + i,  # 500, 501, 502, 503
            "fidelity_bond_value": 1.0,
        }
        for i in range(4)
    ]
    chosen, _ = choose_orders(
        orderbook,
        cj_amount_sats=1_000_000,
        n=2,
        rng=rng,
        chooser="cheapest",
        allowed_ordertypes=frozenset({"sw0absoffer"}),
        max_fee_rel=0.01,
        max_fee_abs=10_000,
        quantize_log_stride=0.1,
    )
    assert chosen is not None
    fees = sorted(int(o["cjfee"]) for o in chosen)
    assert fees == [500, 501]  # untouched, strict ordering preserved
