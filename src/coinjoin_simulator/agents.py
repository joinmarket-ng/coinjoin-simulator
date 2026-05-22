"""Simulator agents: makers and takers.

Behavioral ports of the JoinMarket-Org/joinmarket-clientserver
yieldgenerator and tumbler/sendpayment, with the wallet, network, and
PSBT layers stripped away.

Agents operate on abstract sat-denominated UTXOs and a fixed number of
mixdepths (default 5, matching upstream). The simulator orchestrator
(``world.py``) is responsible for matching a taker's offer pick against
makers' inventories and emitting a ground-truth labeled CJ tx.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from coinjoin_simulator.taker_logic import (
    DUST_THRESHOLD_SATS,
    MAKER_DEFAULT_CJFEE_A_SATS,
    MAKER_DEFAULT_CJFEE_FACTOR,
    MAKER_DEFAULT_CJFEE_R,
    MAKER_DEFAULT_MINSIZE,
    MAKER_DEFAULT_SIZE_FACTOR,
    MAKER_DEFAULT_TXFEE_CONTRIBUTION,
    MAKER_DEFAULT_TXFEE_FACTOR,
    NO_ROUNDING,
    OfferDict,
    ScheduleEntry,
    choose_orders,
    get_tumble_schedule,
    quantize_cjfee_r,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from coinjoin_simulator.orderbook_priors import CounterpartyProfile

DEFAULT_MAX_MIXDEPTH = 4  # 5 mixdepths total (0..4), matching upstream.

# ---------------------------------------------------------------------------
# UTXO model
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Utxo:
    """A single sat-denominated UTXO held by an agent in a given mixdepth."""

    utxo_id: str
    value_sats: int
    mixdepth: int


# ---------------------------------------------------------------------------
# Maker
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class MakerFeePolicy:
    """Static parameters for a maker's fee announcement.

    The yg-privacyenhanced jitter (`*_factor`) is applied per re-announce
    by :meth:`Maker.announce_offer`, so the static values here represent
    the center of each randomization interval.
    """

    ordertype: str = "sw0reloffer"  # one of the relative or absolute ordertypes
    cjfee_a_sats: int = MAKER_DEFAULT_CJFEE_A_SATS
    cjfee_r: float = MAKER_DEFAULT_CJFEE_R
    cjfee_factor: float = MAKER_DEFAULT_CJFEE_FACTOR
    txfee_contribution: int = MAKER_DEFAULT_TXFEE_CONTRIBUTION
    txfee_factor: float = MAKER_DEFAULT_TXFEE_FACTOR
    minsize_sats: int = MAKER_DEFAULT_MINSIZE
    size_factor: float = MAKER_DEFAULT_SIZE_FACTOR
    fidelity_bond_value: float = 0.0
    # §8.2 maker-clustering: when set to a positive log10 stride (typically
    # ``0.1`` to match the on-chain clusterer), every announced relative
    # ``cjfee_r`` is snapped to that grid so the maker's offers cluster
    # with same-cell peers instead of forming a private band.  Absolute
    # offers are unaffected (they are already discrete sat counts).
    quantize_log_stride: float | None = None


@dataclass(slots=True)
class Maker:
    """A yieldgenerator-style maker.

    Holds a UTXO inventory across mixdepths, announces an offer per
    invocation (with yg-pe jitter), and exposes :meth:`fill_offer` which
    consumes inputs from the chosen mixdepth and produces a CJ output to
    the next mixdepth (cyclic) plus a change output to the source
    mixdepth (yieldgenerator.py:264-276).
    """

    counterparty: str
    policy: MakerFeePolicy
    utxos: dict[int, list[Utxo]] = field(default_factory=dict)
    max_mixdepth: int = DEFAULT_MAX_MIXDEPTH
    rng: random.Random = field(default_factory=random.Random)

    @classmethod
    def from_profile(
        cls,
        profile: CounterpartyProfile,
        *,
        seed: int | None = None,
        max_mixdepth: int = DEFAULT_MAX_MIXDEPTH,
    ) -> Maker:
        """Construct a maker by sampling its first offer from a snapshot profile."""
        offer = profile.offers[0]
        rng = random.Random(seed)
        ordertype = offer.ordertype
        return cls(
            counterparty=profile.counterparty,
            policy=MakerFeePolicy(
                ordertype=ordertype,
                cjfee_a_sats=int(offer.cjfee)
                if offer.fee_type == "absolute"
                else MAKER_DEFAULT_CJFEE_A_SATS,
                cjfee_r=float(offer.cjfee)
                if offer.fee_type == "relative"
                else MAKER_DEFAULT_CJFEE_R,
                cjfee_factor=MAKER_DEFAULT_CJFEE_FACTOR,
                txfee_contribution=int(offer.txfee_sats),
                minsize_sats=int(offer.minsize_sats),
                fidelity_bond_value=float(profile.fidelity_bond_value),
            ),
            max_mixdepth=max_mixdepth,
            rng=rng,
        )

    # ------------------------------------------------------------------
    # Inventory helpers
    # ------------------------------------------------------------------

    def total_balance_sats(self) -> int:
        return sum(u.value_sats for ms in self.utxos.values() for u in ms)

    def balance_in_mixdepth(self, mixdepth: int) -> int:
        return sum(u.value_sats for u in self.utxos.get(mixdepth, []))

    def largest_mixdepth(self) -> int:
        if not self.utxos:
            return 0
        return max(self.utxos, key=lambda m: self.balance_in_mixdepth(m))

    # ------------------------------------------------------------------
    # Offer announcement (yg-privacyenhanced.py:46-107)
    # ------------------------------------------------------------------

    def announce_offer(self) -> OfferDict:
        """Produce a freshly-randomized offer dict ready for the orderbook."""
        p = self.policy
        cf = p.cjfee_factor
        sf = p.size_factor
        tcf = p.txfee_factor
        txfee = self.rng.uniform(p.txfee_contribution * (1 - tcf), p.txfee_contribution * (1 + tcf))
        minsize = max(
            DUST_THRESHOLD_SATS,
            int(self.rng.uniform(p.minsize_sats * (1 - sf), p.minsize_sats * (1 + sf))),
        )
        possible_max = max(
            minsize + 1,
            self.balance_in_mixdepth(self.largest_mixdepth())
            - max(DUST_THRESHOLD_SATS, p.txfee_contribution),
        )
        maxsize = max(minsize + 1, int(self.rng.uniform(possible_max * (1 - sf), possible_max)))
        if p.ordertype in {"sw0reloffer", "swreloffer", "reloffer"}:
            cjfee_r = round(self.rng.uniform(p.cjfee_r * (1 - cf), p.cjfee_r * (1 + cf)), 6)
            if p.quantize_log_stride is not None and p.quantize_log_stride > 0:
                # §8.2 maker-clustering: snap the announced cjfee_r to a
                # shared log10 grid so this maker's change outputs land in
                # the same fee-band cell as its grid neighbours.
                cjfee_r = quantize_cjfee_r(cjfee_r, p.quantize_log_stride)
            tries = 20
            while int(txfee) >= cjfee_r * minsize and tries > 0:
                txfee /= 2.0
                tries -= 1
            cjfee: float = float(cjfee_r)
        else:
            cjfee_a = self.rng.uniform(p.cjfee_a_sats * (1 - cf), p.cjfee_a_sats * (1 + cf))
            cjfee = float(int(cjfee_a) + int(txfee))
        return {
            "counterparty": self.counterparty,
            "oid": 0,
            "ordertype": p.ordertype,
            "minsize": int(minsize),
            "maxsize": int(maxsize),
            "txfee": int(txfee),
            "cjfee": cjfee,
            "fidelity_bond_value": p.fidelity_bond_value,
        }

    # ------------------------------------------------------------------
    # Offer filling
    # ------------------------------------------------------------------

    def select_input_mixdepth(self, amount_sats: int) -> int | None:
        """Lowest mixdepth with enough balance.  (yieldgenerator.py:256-262)."""
        for m in sorted(self.utxos.keys()):
            if self.balance_in_mixdepth(m) >= amount_sats:
                return m
        return None

    def fill_offer(
        self,
        cj_amount_sats: int,
        cj_fee_sats: int,
    ) -> tuple[int, list[Utxo], int] | None:
        """Spend ``cj_amount_sats`` worth of inputs from the lowest viable mixdepth.

        Returns ``(input_mixdepth, consumed_utxos, change_sats)`` or
        ``None`` when no mixdepth holds enough.
        """
        m = self.select_input_mixdepth(cj_amount_sats)
        if m is None:
            return None
        # Greedy: take the largest UTXOs first until we cover the amount.
        sorted_utxos = sorted(self.utxos[m], key=lambda u: u.value_sats, reverse=True)
        consumed: list[Utxo] = []
        total = 0
        for u in sorted_utxos:
            consumed.append(u)
            total += u.value_sats
            if total >= cj_amount_sats:
                break
        if total < cj_amount_sats:
            return None
        change = total - cj_amount_sats + cj_fee_sats  # maker keeps fee
        return m, consumed, change


# ---------------------------------------------------------------------------
# Takers
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class TakerConfig:
    max_fee_rel: float = 0.001
    max_fee_abs: int = 10_000
    chooser: str = "weighted"  # one of taker_logic.OrderChooser
    minimum_makers: int = 4
    # §8.2 maker-clustering: when set to a positive log10 stride
    # (typically ``0.1``), every relative offer's ``cjfee_r`` is snapped
    # to the shared grid before fee comparison and selection.  Pairs
    # naturally with :class:`MakerFeePolicy.quantize_log_stride`; either
    # side alone already coarsens the per-maker fee identifier visible
    # to the forward-spend matcher of §3.
    quantize_log_stride: float | None = None


@dataclass(slots=True)
class TumblerTaker:
    """A faithful schedule-driven tumbler.

    The simulator orchestrator drives the schedule, calling
    :meth:`pick_makers` at each step to obtain the chosen counterparties
    and their joint fee. Wallet/PSBT/IRC layers are not modeled.
    """

    taker_id: str
    destinations: list[str]
    schedule: list[ScheduleEntry]
    config: TakerConfig = field(default_factory=TakerConfig)
    schedule_index: int = 0
    rng: random.Random = field(default_factory=random.Random)

    @classmethod
    def build(
        cls,
        *,
        rng: random.Random,
        destaddrs: Sequence[str],
        mixdepth_balances_sats: dict[int, int],
        config: TakerConfig | None = None,
        max_mixdepth_in_wallet: int = DEFAULT_MAX_MIXDEPTH,
    ) -> TumblerTaker:
        sched = get_tumble_schedule(
            rng=rng,
            destaddrs=destaddrs,
            mixdepth_balances_sats=mixdepth_balances_sats,
            max_mixdepth_in_wallet=max_mixdepth_in_wallet,
        )
        return cls(
            taker_id=f"tumbler-{uuid.uuid4().hex[:8]}",
            destinations=list(destaddrs),
            schedule=sched,
            config=config or TakerConfig(),
            rng=rng,
        )

    def current_entry(self) -> ScheduleEntry | None:
        if self.schedule_index >= len(self.schedule):
            return None
        return self.schedule[self.schedule_index]

    def pick_makers(
        self,
        orderbook: Sequence[OfferDict],
        cj_amount_sats: int,
        n: int,
    ) -> tuple[list[OfferDict] | None, int]:
        return choose_orders(
            orderbook,
            cj_amount_sats,
            n,
            rng=self.rng,
            max_fee_rel=self.config.max_fee_rel,
            max_fee_abs=self.config.max_fee_abs,
            chooser=self.config.chooser,  # type: ignore[arg-type]
            quantize_log_stride=self.config.quantize_log_stride,
        )

    def advance(self, *, success: bool) -> None:
        entry = self.current_entry()
        if entry is None:
            return
        if success:
            entry.completed = 1
            self.schedule_index += 1


@dataclass(slots=True)
class PaymentTaker:
    """One-shot CJ payment with optional follow-up cold-storage / channel-open tx.

    Builds a single-entry schedule (sendpayment.py:127-128). When
    ``follow_up_payment`` is true, the recipient address is held back and
    the CJ sends to an internal address; the payment is then made in a
    separate non-CJ tx. Otherwise the CJ output goes directly to the
    recipient.
    """

    taker_id: str
    recipient: str
    amount_sats: int
    src_mixdepth: int
    schedule: list[ScheduleEntry]
    config: TakerConfig = field(default_factory=TakerConfig)
    follow_up_payment: bool = False
    schedule_index: int = 0
    rng: random.Random = field(default_factory=random.Random)

    @classmethod
    def build(
        cls,
        *,
        rng: random.Random,
        recipient: str,
        amount_sats: int,
        src_mixdepth: int = 0,
        makercount: int = 9,
        follow_up_payment: bool = False,
        sweep: bool = False,
        config: TakerConfig | None = None,
    ) -> PaymentTaker:
        dest = "INTERNAL" if follow_up_payment else recipient
        amount_field: int | float = 0 if sweep else int(amount_sats)
        sched = [
            ScheduleEntry(
                src_mixdepth=src_mixdepth,
                amount=amount_field,
                makercount=makercount,
                destination=dest,
                wait_minutes=0.0,
                rounding=NO_ROUNDING,
                completed=0,
            ),
        ]
        return cls(
            taker_id=f"pay-{uuid.uuid4().hex[:8]}",
            recipient=recipient,
            amount_sats=amount_sats,
            src_mixdepth=src_mixdepth,
            schedule=sched,
            config=config or TakerConfig(),
            follow_up_payment=follow_up_payment,
            rng=rng,
        )

    def current_entry(self) -> ScheduleEntry | None:
        if self.schedule_index >= len(self.schedule):
            return None
        return self.schedule[self.schedule_index]

    def pick_makers(
        self,
        orderbook: Sequence[OfferDict],
        cj_amount_sats: int,
        n: int,
    ) -> tuple[list[OfferDict] | None, int]:
        return choose_orders(
            orderbook,
            cj_amount_sats,
            n,
            rng=self.rng,
            max_fee_rel=self.config.max_fee_rel,
            max_fee_abs=self.config.max_fee_abs,
            chooser=self.config.chooser,  # type: ignore[arg-type]
            quantize_log_stride=self.config.quantize_log_stride,
        )

    def advance(self, *, success: bool) -> None:
        entry = self.current_entry()
        if entry is None:
            return
        if success:
            entry.completed = 1
            self.schedule_index += 1
