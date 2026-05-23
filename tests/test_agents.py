"""Tests for agents (Maker, TumblerTaker, PaymentTaker)."""

from __future__ import annotations

import math
import random

from coinjoin_simulator.agents import (
    DEFAULT_MAX_MIXDEPTH,
    Maker,
    MakerFeePolicy,
    PaymentTaker,
    TumblerTaker,
    Utxo,
)
from coinjoin_simulator.orderbook_priors import CounterpartyProfile, Offer
from coinjoin_simulator.taker_logic import NO_ROUNDING


def _make_maker(seed: int = 0, balance: int = 5_000_000) -> Maker:
    m = Maker(
        counterparty=f"M-{seed}",
        policy=MakerFeePolicy(),
        rng=random.Random(seed),
    )
    m.utxos[0] = [Utxo(utxo_id=f"u-{seed}-0", value_sats=balance, mixdepth=0)]
    return m


def test_maker_announce_offer_has_required_fields() -> None:
    m = _make_maker(seed=1, balance=10_000_000)
    o = m.announce_offer()
    for k in ("counterparty", "oid", "ordertype", "minsize", "maxsize", "txfee", "cjfee"):
        assert k in o
    assert o["counterparty"] == m.counterparty
    assert o["minsize"] >= 0
    assert o["maxsize"] > o["minsize"]


def test_maker_announce_offer_jitter_changes_values() -> None:
    m = _make_maker(seed=2, balance=10_000_000)
    o1 = m.announce_offer()
    o2 = m.announce_offer()
    # With non-zero jitter factors the cjfee should drift between calls.
    keys1 = (o1["cjfee"], o1["minsize"], o1["maxsize"])
    keys2 = (o2["cjfee"], o2["minsize"], o2["maxsize"])
    assert keys1 != keys2


def test_maker_announce_offer_no_jitter_is_stable() -> None:
    m = Maker(
        counterparty="M",
        policy=MakerFeePolicy(cjfee_factor=0.0, txfee_factor=0.0, size_factor=0.0),
        rng=random.Random(0),
    )
    m.utxos[0] = [Utxo("u", 1_000_000, 0)]
    o1 = m.announce_offer()
    o2 = m.announce_offer()
    assert o1["cjfee"] == o2["cjfee"]
    assert o1["minsize"] == o2["minsize"]


def test_maker_announce_offer_quantization_snaps_relative_cjfee() -> None:
    # Two makers whose centre fees fall inside the same log-decimal cell
    # should announce the *same* quantised cjfee_r once snapping is on.
    # This is the property §8.2 maker-clustering relies on: a maker that
    # opts into the grid (or is opted in by default) loses its private
    # personal band and merges with its grid neighbours.
    base_cjfee_r = 1.30e-5
    neighbour_cjfee_r = 1.50e-5  # same cell -49 as 1.30e-5 at log_stride 0.1
    p1 = MakerFeePolicy(
        cjfee_r=base_cjfee_r,
        cjfee_factor=0.0,
        txfee_factor=0.0,
        size_factor=0.0,
        quantize_log_stride=0.1,
    )
    p2 = MakerFeePolicy(
        cjfee_r=neighbour_cjfee_r,
        cjfee_factor=0.0,
        txfee_factor=0.0,
        size_factor=0.0,
        quantize_log_stride=0.1,
    )
    m1 = Maker(counterparty="M1", policy=p1, rng=random.Random(0))
    m2 = Maker(counterparty="M2", policy=p2, rng=random.Random(0))
    m1.utxos[0] = [Utxo("u1", 1_000_000, 0)]
    m2.utxos[0] = [Utxo("u2", 1_000_000, 0)]
    o1 = m1.announce_offer()
    o2 = m2.announce_offer()
    assert o1["cjfee"] == o2["cjfee"]
    # And each is exactly on the grid (idempotent snap).
    from coinjoin_simulator.taker_logic import quantize_cjfee_r

    assert o1["cjfee"] == quantize_cjfee_r(float(o1["cjfee"]), 0.1)


def test_maker_announce_offer_quantization_disabled_by_default() -> None:
    # Default policy must not silently quantise; the snap is opt-in.
    p = MakerFeePolicy(cjfee_r=1.4e-5, cjfee_factor=0.0, txfee_factor=0.0, size_factor=0.0)
    assert p.quantize_log_stride is None
    m = Maker(counterparty="M", policy=p, rng=random.Random(0))
    m.utxos[0] = [Utxo("u", 1_000_000, 0)]
    o = m.announce_offer()
    assert math.isclose(float(o["cjfee"]), 1.4e-5, rel_tol=1e-6)


def test_maker_select_input_mixdepth_picks_richest() -> None:
    # joinmarket-ng selects the richest eligible mixdepth (max balance),
    # not the lowest. mixdepth 2 has 5 BTC vs. mixdepth 0's 2 BTC, so any
    # amount that fits both should pick 2; larger amounts that only fit
    # in 2 still pick 2; unfittable amounts return None.
    m = _make_maker(seed=3, balance=2_000_000)
    m.utxos[2] = [Utxo("u-2", 5_000_000, 2)]
    assert m.select_input_mixdepth(1_000_000) == 2
    assert m.select_input_mixdepth(3_000_000) == 2
    assert m.select_input_mixdepth(10_000_000) is None


def test_maker_fill_offer_consumes_utxos() -> None:
    m = _make_maker(seed=4, balance=10_000_000)
    res = m.fill_offer(cj_amount_sats=2_000_000, cj_fee_sats=500)
    assert res is not None
    mixdepth, consumed, change = res
    assert mixdepth == 0
    assert sum(u.value_sats for u in consumed) >= 2_000_000
    assert change == 10_000_000 - 2_000_000 + 500


def test_maker_fill_offer_returns_none_when_short() -> None:
    m = _make_maker(seed=5, balance=1_000_000)
    assert m.fill_offer(10_000_000, 100) is None


def test_maker_from_profile() -> None:
    profile = CounterpartyProfile(
        counterparty="bonded-M",
        offers=(
            Offer(
                oid=0,
                ordertype="sw0reloffer",
                fee_type="relative",
                cjfee=0.0001,
                minsize_sats=200_000,
                maxsize_sats=50_000_000,
                txfee_sats=100,
            ),
        ),
        fidelity_bond_value=1.5e7,
        bond_locktime=1788220800,
        bond_amount_sats=50_000_000,
    )
    m = Maker.from_profile(profile, seed=42)
    assert m.counterparty == "bonded-M"
    assert m.policy.ordertype == "sw0reloffer"
    assert m.policy.cjfee_r == 0.0001
    assert m.policy.minsize_sats == 200_000
    assert m.policy.fidelity_bond_value == 1.5e7
    assert m.max_mixdepth == DEFAULT_MAX_MIXDEPTH


def test_tumbler_taker_build_produces_schedule() -> None:
    rng = random.Random(0)
    t = TumblerTaker.build(
        rng=rng,
        destaddrs=["bc1qextA"],
        mixdepth_balances_sats={0: 5_000_000, 2: 8_000_000},
    )
    assert t.taker_id.startswith("tumbler-")
    assert len(t.schedule) > 0
    assert t.schedule_index == 0
    # Initial entry exists.
    assert t.current_entry() is not None


def test_tumbler_taker_advance_marks_completed() -> None:
    rng = random.Random(0)
    t = TumblerTaker.build(
        rng=rng,
        destaddrs=["bc1qextA"],
        mixdepth_balances_sats={0: 5_000_000},
    )
    first = t.schedule[0]
    t.advance(success=True)
    assert first.completed == 1
    assert t.schedule_index == 1


def test_tumbler_taker_advance_failure_does_not_progress() -> None:
    rng = random.Random(0)
    t = TumblerTaker.build(
        rng=rng,
        destaddrs=["bc1qextA"],
        mixdepth_balances_sats={0: 5_000_000},
    )
    t.advance(success=False)
    assert t.schedule_index == 0
    assert t.schedule[0].completed == 0


def test_payment_taker_build_with_change() -> None:
    rng = random.Random(0)
    p = PaymentTaker.build(
        rng=rng,
        recipient="bc1qrecipient",
        amount_sats=500_000,
        src_mixdepth=1,
        makercount=6,
    )
    assert p.taker_id.startswith("pay-")
    assert len(p.schedule) == 1
    e = p.schedule[0]
    assert e.amount == 500_000
    assert e.destination == "bc1qrecipient"
    assert e.makercount == 6
    assert e.rounding == NO_ROUNDING


def test_payment_taker_build_sweep() -> None:
    rng = random.Random(0)
    p = PaymentTaker.build(
        rng=rng,
        recipient="bc1qrecipient",
        amount_sats=0,
        sweep=True,
    )
    assert p.schedule[0].amount == 0


def test_payment_taker_follow_up_holds_destination() -> None:
    rng = random.Random(0)
    p = PaymentTaker.build(
        rng=rng,
        recipient="bc1qrecipient",
        amount_sats=500_000,
        follow_up_payment=True,
    )
    assert p.schedule[0].destination == "INTERNAL"
    assert p.recipient == "bc1qrecipient"


def test_payment_taker_pick_makers_returns_distinct() -> None:
    rng = random.Random(1)
    p = PaymentTaker.build(
        rng=rng,
        recipient="bc1qrecipient",
        amount_sats=500_000,
        makercount=4,
    )
    book = [
        {
            "counterparty": f"M{i}",
            "oid": 0,
            "ordertype": "sw0reloffer",
            "minsize": 100_000,
            "maxsize": 100_000_000,
            "txfee": 0,
            "cjfee": 0.00005 + i * 1e-6,
            "fidelity_bond_value": 1.0,
        }
        for i in range(8)
    ]
    chosen, total = p.pick_makers(book, 500_000, 4)
    assert chosen is not None
    assert len({o["counterparty"] for o in chosen}) == 4
    assert total >= 0
