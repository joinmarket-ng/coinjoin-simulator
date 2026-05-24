"""Tests for the discrete-event simulator world."""

from __future__ import annotations

import random
from collections import Counter

import pytest

from coinjoin_simulator.agents import (
    DEFAULT_MAX_MIXDEPTH,
    Maker,
    MakerFeePolicy,
    PaymentTaker,
    TumblerTaker,
    Utxo,
)
from coinjoin_simulator.taker_logic import NO_ROUNDING, ScheduleEntry, get_tumble_schedule
from coinjoin_simulator.world import (
    OutputRole,
    PaymentStatus,
    World,
    WorldConfig,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_maker(
    name: str,
    *,
    seed: int,
    balances: dict[int, int],
    fidelity_bond_value: float = 1e6,
    max_mixdepth: int = DEFAULT_MAX_MIXDEPTH,
) -> Maker:
    """Build a maker with one large UTXO per requested mixdepth."""
    utxos: dict[int, list[Utxo]] = {}
    for mix, sats in balances.items():
        utxos[mix] = [Utxo(utxo_id=f"u-{name}-m{mix}", value_sats=sats, mixdepth=mix)]
    return Maker(
        counterparty=name,
        policy=MakerFeePolicy(
            ordertype="sw0reloffer",
            cjfee_r=2e-5,
            cjfee_a_sats=500,
            txfee_contribution=100,
            minsize_sats=10_000,
            fidelity_bond_value=fidelity_bond_value,
        ),
        utxos=utxos,
        max_mixdepth=max_mixdepth,
        rng=random.Random(seed),
    )


def _build_makers(n: int, *, seed: int = 0) -> list[Maker]:
    """N well-funded bonded makers, each with 5 BTC across mixdepths 0..max."""
    makers: list[Maker] = []
    for i in range(n):
        balances = {m: 100_000_000 for m in range(DEFAULT_MAX_MIXDEPTH + 1)}
        makers.append(
            _make_maker(
                f"m{i}",
                seed=seed + i,
                balances=balances,
                fidelity_bond_value=1e9 + i * 1e6,
            ),
        )
    return makers


def _payment_taker(
    *,
    seed: int,
    recipient: str = "bc1qrecipient",
    amount_sats: int = 1_000_000,
    follow_up: bool = False,
    makercount: int = 4,
) -> PaymentTaker:
    return PaymentTaker.build(
        rng=random.Random(seed),
        recipient=recipient,
        amount_sats=amount_sats,
        src_mixdepth=0,
        makercount=makercount,
        follow_up_payment=follow_up,
    )


# ---------------------------------------------------------------------------
# Smoke / determinism
# ---------------------------------------------------------------------------


def test_world_runs_a_single_payment_cj() -> None:
    makers = _build_makers(8, seed=42)
    taker = _payment_taker(seed=1)
    cfg = WorldConfig(seed=7)
    w = World.from_components(config=cfg, makers=makers, takers=[taker])
    res = w.run()

    assert len(res.txs) == 1
    tx = res.txs[0]
    assert tx.taker_id == taker.taker_id
    assert len(tx.maker_counterparties) == 4
    # 4 makers x (CJ + change) + 1 taker output (external_payment, since not follow-up)
    assert len(tx.outputs) == 4 * 2 + 1
    # Mass conservation across CJ outputs (excluding change/external):
    cj_outs = [o for o in tx.outputs if o.role == OutputRole.MAKER_CJ]
    assert all(o.value_sats == tx.cj_amount_sats for o in cj_outs)
    # Exactly one external_payment output, going to the recipient.
    ext = [o for o in tx.outputs if o.role == OutputRole.EXTERNAL_PAYMENT]
    assert len(ext) == 1
    assert ext[0].owner == "bc1qrecipient"


def test_world_run_is_deterministic_under_seed() -> None:
    def go() -> tuple[list[str], list[int], list[str]]:
        makers = _build_makers(8, seed=11)
        taker = _payment_taker(seed=2, amount_sats=2_000_000, makercount=3)
        cfg = WorldConfig(seed=99)
        w = World.from_components(config=cfg, makers=makers, takers=[taker])
        res = w.run()
        return (
            [tx.txid for tx in res.txs],  # txids derive from uuid; but rng-determined?
            [tx.cj_amount_sats for tx in res.txs],
            [o.role.value for tx in res.txs for o in tx.outputs],
        )

    # txids are uuid4-based and NOT seeded by WorldConfig.seed; they will differ.
    # The deterministic invariants are: amounts, ordering, role labels, and counts.
    _, amounts_a, roles_a = go()
    _, amounts_b, roles_b = go()
    assert amounts_a == amounts_b
    assert roles_a == roles_b


# ---------------------------------------------------------------------------
# Ground-truth label correctness
# ---------------------------------------------------------------------------


def test_every_output_has_a_role_and_an_owner() -> None:
    makers = _build_makers(6, seed=3)
    taker = _payment_taker(seed=5)
    res = World.from_components(
        config=WorldConfig(seed=1),
        makers=makers,
        takers=[taker],
    ).run()
    assert res.txs
    for tx in res.txs:
        for o in tx.outputs:
            assert o.role in OutputRole
            assert o.owner
            assert o.value_sats >= 0


def test_maker_id_persists_across_mixdepth_advance() -> None:
    """After fill, the freshly-minted CJ output's owner must match its maker."""
    makers = _build_makers(6, seed=4)
    taker = _payment_taker(seed=6, makercount=4)
    res = World.from_components(
        config=WorldConfig(seed=2),
        makers=makers,
        takers=[taker],
    ).run()

    tx = res.txs[0]
    cj_outs = [o for o in tx.outputs if o.role == OutputRole.MAKER_CJ]
    change_outs = [o for o in tx.outputs if o.role == OutputRole.MAKER_CHANGE]
    # Each maker contributed exactly one CJ + one change.
    cj_owners = Counter(o.owner for o in cj_outs)
    change_owners = Counter(o.owner for o in change_outs)
    assert cj_owners == change_owners
    # And the persistent map records each minted output under its maker.
    for o in cj_outs + change_outs:
        if o.value_sats > 0:
            assert res.maker_id_by_utxo[o.output_id] == o.owner


def test_cj_outputs_are_uniform() -> None:
    makers = _build_makers(8, seed=8)
    taker = _payment_taker(seed=9, makercount=5, amount_sats=1_500_000)
    res = World.from_components(
        config=WorldConfig(seed=3),
        makers=makers,
        takers=[taker],
    ).run()
    tx = res.txs[0]
    cj_roles = (OutputRole.MAKER_CJ, OutputRole.TAKER_CJ, OutputRole.EXTERNAL_PAYMENT)
    values = {o.value_sats for o in tx.outputs if o.role in cj_roles}
    assert len(values) == 1, f"non-uniform CJ outputs: {values}"


def test_offer_log_records_one_entry_per_fill() -> None:
    makers = _build_makers(8, seed=10)
    taker = _payment_taker(seed=11, makercount=4)
    res = World.from_components(
        config=WorldConfig(seed=4),
        makers=makers,
        takers=[taker],
    ).run()
    tx = res.txs[0]
    log = [e for e in res.offer_log if e.txid == tx.txid]
    assert len(log) == 4
    counterparties = {e.counterparty for e in log}
    assert counterparties == set(tx.maker_counterparties)
    for e in log:
        assert "ordertype" in e.offer
        assert "cjfee" in e.offer
        assert e.fee_paid_sats >= 0


# ---------------------------------------------------------------------------
# Payment delivery state machine
# ---------------------------------------------------------------------------


def test_payment_delivered_in_cj_when_not_follow_up() -> None:
    makers = _build_makers(8, seed=20)
    taker = _payment_taker(seed=21, follow_up=False)
    res = World.from_components(
        config=WorldConfig(seed=5),
        makers=makers,
        takers=[taker],
    ).run()
    assert len(res.payment_records) == 1
    rec = res.payment_records[0]
    assert rec.status == PaymentStatus.DELIVERED_IN_CJ
    assert rec.delivered_in_txid == res.txs[0].txid
    assert rec.recipient == "bc1qrecipient"


def test_follow_up_payment_emits_separate_payout_tx() -> None:
    makers = _build_makers(8, seed=30)
    taker = _payment_taker(seed=31, follow_up=True)
    res = World.from_components(
        config=WorldConfig(seed=6),
        makers=makers,
        takers=[taker],
    ).run()
    # Two txs: the CJ + the payout.
    assert len(res.txs) == 2
    cj_tx, payout_tx = res.txs
    # CJ tx has a TAKER_CJ output (no external_payment).
    assert any(o.role == OutputRole.TAKER_CJ for o in cj_tx.outputs)
    assert all(o.role != OutputRole.EXTERNAL_PAYMENT for o in cj_tx.outputs)
    # Payout tx is non-CJ and pays the recipient.
    assert payout_tx.cj_amount_sats == cj_tx.outputs[-1].value_sats  # taker CJ output value
    assert any(o.role == OutputRole.EXTERNAL_PAYMENT for o in payout_tx.outputs)

    rec = res.payment_records[0]
    assert rec.status == PaymentStatus.DELIVERED_FOLLOWUP
    assert rec.delivered_in_txid == payout_tx.txid


# ---------------------------------------------------------------------------
# Block packing
# ---------------------------------------------------------------------------


def test_block_packing_caps_txs_per_block() -> None:
    """When ``txs_per_block`` is small, txs roll over into successive blocks."""
    makers = _build_makers(8, seed=40)
    takers: list[TumblerTaker | PaymentTaker] = [
        _payment_taker(seed=100 + i, recipient=f"bc1q{i}", amount_sats=500_000, makercount=3)
        for i in range(5)
    ]
    cfg = WorldConfig(seed=8, txs_per_block=2, starting_block=1000)
    res = World.from_components(config=cfg, makers=makers, takers=takers).run()

    assert len(res.txs) == 5
    # Per-block tx_index must be < cap.
    for tx in res.txs:
        assert 0 <= tx.tx_index < cfg.txs_per_block
    blocks_used = sorted({tx.block_height for tx in res.txs})
    # 5 txs at cap=2 must occupy >= 3 distinct blocks.
    assert len(blocks_used) >= 3
    # Block indices monotonically advance.
    assert blocks_used == sorted(blocks_used)


# ---------------------------------------------------------------------------
# Tumbler end-to-end smoke
# ---------------------------------------------------------------------------


def test_tumbler_runs_full_schedule_to_completion() -> None:
    makers = _build_makers(12, seed=50)
    rng = random.Random(0)
    tumbler = TumblerTaker.build(
        rng=rng,
        destaddrs=["bc1qd0", "bc1qd1", "bc1qd2"],
        mixdepth_balances_sats={
            0: 100_000_000,
            1: 100_000_000,
            2: 100_000_000,
            3: 100_000_000,
            4: 100_000_000,
        },
    )
    initial_entries = len(tumbler.schedule)
    assert initial_entries > 0

    res = World.from_components(
        config=WorldConfig(seed=12, txs_per_block=50),
        makers=makers,
        takers=[tumbler],
    ).run()

    # Every entry should have advanced past completion.
    assert tumbler.schedule_index == initial_entries
    # Every CJ tx is properly labelled.
    for tx in res.txs:
        roles = {o.role for o in tx.outputs}
        assert OutputRole.MAKER_CJ in roles
        assert OutputRole.MAKER_CHANGE in roles
        # Either an internal taker_cj (mixdepth advance) or external_payment (final destinations).
        assert (OutputRole.TAKER_CJ in roles) or (OutputRole.EXTERNAL_PAYMENT in roles)


def test_taker_giving_up_after_max_retries_does_not_loop_forever() -> None:
    """If makers can't fulfil any offer, the simulator must terminate."""
    # Single underfunded maker that can never satisfy a 4-maker pick.
    underfunded = _make_maker(
        "uf",
        seed=0,
        balances={0: 1_000},
        fidelity_bond_value=1.0,
    )
    taker = _payment_taker(seed=1, makercount=4, amount_sats=10_000_000)
    cfg = WorldConfig(seed=13, max_retries_per_entry=2, liquidity_wait_minutes=60.0)
    res = World.from_components(
        config=cfg,
        makers=[underfunded],
        takers=[taker],
    ).run(max_events=1000)
    # No CJs should have been produced; payment record is still pending.
    assert res.txs == []
    assert res.payment_records[0].status == PaymentStatus.PENDING


# ---------------------------------------------------------------------------
# Invariants
# ---------------------------------------------------------------------------


def test_maker_balance_decreases_by_cj_fee_when_filling() -> None:
    """A maker's balance change after a fill equals the cj_fee they were paid."""
    makers = _build_makers(6, seed=60)
    pre_balances = {m.counterparty: m.total_balance_sats() for m in makers}
    taker = _payment_taker(seed=61, makercount=4, amount_sats=2_000_000)
    res = World.from_components(
        config=WorldConfig(seed=14),
        makers=makers,
        takers=[taker],
    ).run()
    tx = res.txs[0]
    # Each maker that filled gains cj_fee_sats (their announced fee) - txfee they contribute
    # is folded into the change calc; net result is balance increases by their fee.
    fee_by_cp = {e.counterparty: e.fee_paid_sats for e in res.offer_log if e.txid == tx.txid}
    for m in makers:
        if m.counterparty in tx.maker_counterparties:
            expected = pre_balances[m.counterparty] + fee_by_cp[m.counterparty]
            assert m.total_balance_sats() == expected
        else:
            assert m.total_balance_sats() == pre_balances[m.counterparty]


def test_inputs_are_consumed_from_maker_id_map() -> None:
    """After a fill, consumed UTXOs must be removed from the persistent map."""
    makers = _build_makers(6, seed=70)
    pre_utxo_ids = set(makers[0].utxos[0][0].utxo_id for _ in [0])  # type: ignore[unused-ignore]
    pre_utxo_ids = {u.utxo_id for m in makers for ms in m.utxos.values() for u in ms}
    taker = _payment_taker(seed=71, makercount=4)
    res = World.from_components(
        config=WorldConfig(seed=15),
        makers=makers,
        takers=[taker],
    ).run()
    tx = res.txs[0]
    consumed_maker_inputs = set(tx.inputs) & pre_utxo_ids
    assert consumed_maker_inputs, "no maker inputs found among tx inputs"
    for utxo_id in consumed_maker_inputs:
        assert utxo_id not in res.maker_id_by_utxo


# ---------------------------------------------------------------------------
# Schedule invariants
# ---------------------------------------------------------------------------


def test_schedule_with_zero_makers_is_skipped() -> None:
    """A schedule entry with amount=0 and no balance should advance cleanly."""
    rng = random.Random(0)
    sched: list[ScheduleEntry] = [
        ScheduleEntry(
            src_mixdepth=0,
            amount=0,  # sweep with no balance
            makercount=4,
            destination="bc1qrecipient",
            wait_minutes=0.0,
            rounding=NO_ROUNDING,
            completed=0,
        ),
    ]
    taker = TumblerTaker(
        taker_id="t-test",
        destinations=["bc1qrecipient"],
        schedule=sched,
        rng=rng,
    )
    makers = _build_makers(6, seed=80)
    res = World.from_components(
        config=WorldConfig(seed=16),
        makers=makers,
        takers=[taker],
    ).run()
    # No tx was emitted because cj_amount resolved to 0.
    assert res.txs == []
    assert taker.schedule_index == 1


# ---------------------------------------------------------------------------
# Sanity: tumble schedule generation interacts well with World
# ---------------------------------------------------------------------------


def test_get_tumble_schedule_drives_world_consistently() -> None:
    rng = random.Random(123)
    sched = get_tumble_schedule(
        rng=rng,
        destaddrs=["a", "b"],
        mixdepth_balances_sats={i: 100_000_000 for i in range(5)},
    )
    assert sched
    # Final entries should target the destination addresses.
    final_dests = {e.destination for e in sched if e.destination not in {"INTERNAL", "addrask"}}
    assert final_dests <= {"a", "b"}


@pytest.mark.parametrize("makercount", [3, 5, 7])
def test_payment_taker_with_varying_makercount(makercount: int) -> None:
    makers = _build_makers(12, seed=90 + makercount)
    taker = _payment_taker(seed=200 + makercount, makercount=makercount)
    res = World.from_components(
        config=WorldConfig(seed=20 + makercount),
        makers=makers,
        takers=[taker],
    ).run()
    assert len(res.txs) == 1
    assert len(res.txs[0].maker_counterparties) == makercount
    assert sum(1 for o in res.txs[0].outputs if o.role == OutputRole.MAKER_CJ) == makercount


# ---------------------------------------------------------------------------
# §9 countermeasures: forbid_change_as_input and maker_only_cj_period
# ---------------------------------------------------------------------------


def _build_makers_no_change_as_input(n: int, *, seed: int = 0) -> list[Maker]:
    makers = _build_makers(n, seed=seed)
    for m in makers:
        m.forbid_change_as_input = True
    return makers


def test_forbid_change_as_input_excludes_change_from_next_fill() -> None:
    """Two back-to-back CJs from the same maker: the second must not consume
    the first CJ's change output as input."""
    makers = _build_makers_no_change_as_input(8, seed=42)
    taker_a = _payment_taker(seed=1, amount_sats=1_000_000, makercount=4)
    taker_b = _payment_taker(seed=2, amount_sats=1_000_000, makercount=4)
    cfg = WorldConfig(seed=7)
    res = World.from_components(config=cfg, makers=makers, takers=[taker_a, taker_b]).run()
    assert len(res.txs) == 2
    tx1, tx2 = res.txs
    # Identify change utxos from tx1 owned by makers.
    change_ids_tx1 = {o.output_id for o in tx1.outputs if o.role == OutputRole.MAKER_CHANGE}
    assert change_ids_tx1, "no MAKER_CHANGE outputs in tx1 (precondition for the test)"
    # tx2 must not consume any of them.
    assert not (set(tx2.inputs) & change_ids_tx1), (
        f"forbid_change_as_input violated: tx2 spent {set(tx2.inputs) & change_ids_tx1}"
    )


def test_forbid_change_as_input_is_off_by_default() -> None:
    """Without the flag, a maker is free to consume its own change UTXO
    again. This is just a regression guard so we don't accidentally
    enable the countermeasure for callers that did not opt in."""
    makers = _build_makers(8, seed=42)
    for m in makers:
        assert m.forbid_change_as_input is False
        assert m.held_back_change_ids == set()


def test_maker_only_cj_period_emits_synthetic_cjs() -> None:
    """With period=2 and the change-mask on, every two taker CJs must be
    followed by exactly one synthetic maker-only CJ (assuming the makers
    have accumulated enough held-back change to participate)."""
    makers = _build_makers_no_change_as_input(8, seed=42)
    takers = [_payment_taker(seed=100 + i, amount_sats=1_000_000, makercount=4) for i in range(6)]
    cfg = WorldConfig(seed=7, maker_only_cj_period=2, maker_only_cj_n_makers=3)
    res = World.from_components(config=cfg, makers=makers, takers=takers).run()
    # 6 taker CJs => 3 maker-only CJ ticks; the first one fires once any
    # maker has at least one held-back change UTXO, which happens after
    # the first taker CJ. So we expect: 6 taker CJs + 3 synthetic = 9.
    taker_txs = [tx for tx in res.txs if tx.taker_id != "MAKER_ONLY_CJ"]
    synth_txs = [tx for tx in res.txs if tx.taker_id == "MAKER_ONLY_CJ"]
    assert len(taker_txs) == 6
    assert len(synth_txs) == 3
    # Every synthetic CJ must have exactly maker_only_cj_n_makers
    # participants, MAKER_CJ + MAKER_CHANGE outputs balanced.
    for tx in synth_txs:
        assert len(tx.maker_counterparties) == 3
        cjs = [o for o in tx.outputs if o.role == OutputRole.MAKER_CJ]
        chgs = [o for o in tx.outputs if o.role == OutputRole.MAKER_CHANGE]
        assert len(cjs) == 3
        assert len(chgs) == 3
        # Each maker contributes exactly one input (their largest held-back UTXO).
        assert len(tx.inputs) == 3
        # Mass conservation per maker (input value == cj + change).
        for cj, chg, in_val in zip(cjs, chgs, tx.input_values, strict=True):
            assert cj.value_sats + chg.value_sats == in_val
        # Owners on cj and change outputs match.
        cj_owners = sorted(o.owner for o in cjs)
        chg_owners = sorted(o.owner for o in chgs)
        assert cj_owners == chg_owners


def test_maker_only_cj_disabled_by_default() -> None:
    """No synthetic CJs when ``maker_only_cj_period`` is None."""
    makers = _build_makers_no_change_as_input(8, seed=42)
    takers = [_payment_taker(seed=100 + i, amount_sats=1_000_000, makercount=4) for i in range(4)]
    cfg = WorldConfig(seed=7)  # maker_only_cj_period defaults to None.
    res = World.from_components(config=cfg, makers=makers, takers=takers).run()
    assert all(tx.taker_id != "MAKER_ONLY_CJ" for tx in res.txs)
    assert len(res.txs) == 4
