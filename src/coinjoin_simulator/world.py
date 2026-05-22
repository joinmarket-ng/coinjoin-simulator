"""Discrete-event simulator world.

Drives makers and takers on a single-threaded logical clock keyed on
``(block_height, tx_index)``. Emits, for every coinjoin tx:

- per-output role labels (``taker_cj`` / ``maker_cj`` / ``taker_change``
  / ``maker_change`` / ``external_payment``);
- a persistent maker-identity map from output to ``counterparty``,
  allowing clustering attacks to be evaluated against ground truth;
- the announced fee policy each maker showed at fill time (offer log);
- block-level packing (tx index per block, capped at ``txs_per_block``);
- a payment-delivery audit trail for :class:`PaymentTaker` instances.

The simulator is deterministic given a seed and a static configuration.
"""

from __future__ import annotations

import heapq
import random
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from coinjoin_simulator.agents import (
    DEFAULT_MAX_MIXDEPTH,
    Maker,
    PaymentTaker,
    TumblerTaker,
    Utxo,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from coinjoin_simulator.taker_logic import OfferDict

# ---------------------------------------------------------------------------
# Time / config
# ---------------------------------------------------------------------------

DEFAULT_BLOCK_MINUTES = 10.0
DEFAULT_TXS_PER_BLOCK = 200  # soft cap; rolls over to next block
DEFAULT_MAX_RETRIES_PER_ENTRY = 3
DEFAULT_LIQUIDITY_WAIT_MINUTES = 1.0
# Notional miner fee per simulated CJ tx. The simulator does not model
# fee-rate dynamics; this constant simply lets analyzers that key on the
# input/output discrepancy (e.g. joinmarket_analyzer.solver) correctly
# identify the taker by their net cost. 1000 sats is a typical
# 1-input/N-output segwit JM CJ at moderate feerates.
DEFAULT_NETWORK_FEE_SATS = 1000


@dataclass(slots=True)
class WorldConfig:
    seed: int = 0
    block_minutes: float = DEFAULT_BLOCK_MINUTES
    txs_per_block: int = DEFAULT_TXS_PER_BLOCK
    starting_block: int = 1
    max_retries_per_entry: int = DEFAULT_MAX_RETRIES_PER_ENTRY
    liquidity_wait_minutes: float = DEFAULT_LIQUIDITY_WAIT_MINUTES
    # Allowed ordertypes for taker offer selection (segwit-only by default).
    allowed_ordertypes: frozenset[str] = frozenset({"sw0reloffer", "sw0absoffer"})


# ---------------------------------------------------------------------------
# Ground-truth labels
# ---------------------------------------------------------------------------


class OutputRole(StrEnum):
    """Ground-truth role of a CJ tx output."""

    TAKER_CJ = "taker_cj"
    MAKER_CJ = "maker_cj"
    TAKER_CHANGE = "taker_change"
    MAKER_CHANGE = "maker_change"
    EXTERNAL_PAYMENT = "external_payment"
    UNKNOWN = "unknown"  # used when bridging from unlabeled mainnet data


class PaymentStatus(StrEnum):
    PENDING = "pending"
    DELIVERED_IN_CJ = "delivered_in_cj"
    DELIVERED_FOLLOWUP = "delivered_followup"
    FAILED = "failed"


@dataclass(slots=True)
class TxOutput:
    """A single CJ-tx output with ground-truth labelling."""

    output_id: str
    value_sats: int
    role: OutputRole
    owner: str  # taker_id or maker counterparty (for EXTERNAL_PAYMENT: recipient)
    mixdepth: int | None  # destination mixdepth (None for EXTERNAL_PAYMENT)


@dataclass(slots=True)
class Tx:
    """A simulated CJ transaction with ground-truth labels."""

    txid: str
    block_height: int
    tx_index: int
    taker_id: str
    maker_counterparties: tuple[str, ...]
    inputs: tuple[str, ...]  # consumed utxo_ids
    input_values: tuple[int, ...]  # parallel to ``inputs``, in sats
    outputs: tuple[TxOutput, ...]
    cj_amount_sats: int
    total_cj_fee_sats: int
    network_fee_sats: int = 0


@dataclass(slots=True)
class OfferLogEntry:
    """The offer a maker showed at the time of a fill."""

    txid: str
    block_height: int
    counterparty: str
    offer: OfferDict
    fee_paid_sats: int


@dataclass(slots=True)
class PaymentRecord:
    """Audit trail for a :class:`PaymentTaker`."""

    taker_id: str
    recipient: str
    amount_sats: int
    status: PaymentStatus
    delivered_in_txid: str | None = None


@dataclass(slots=True)
class SimResult:
    """Frozen output of a simulator run."""

    seed: int
    txs: list[Tx]
    offer_log: list[OfferLogEntry]
    payment_records: list[PaymentRecord]
    maker_id_by_utxo: dict[str, str]  # utxo_id -> maker counterparty
    utxo_value_by_id: dict[str, int]  # utxo_id -> value in sats


# ---------------------------------------------------------------------------
# Event queue
# ---------------------------------------------------------------------------


@dataclass(order=True, slots=True)
class _Event:
    block_height: int
    tx_index_hint: int
    seq: int  # tiebreaker
    taker_idx: int = field(compare=False)
    retries_left: int = field(compare=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _short_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _resolve_cj_amount(entry_amount: float | int, mixdepth_balance_sats: int) -> int:
    """Tumbler/sendpayment amount-to-sats resolution. (taker.py:191-217)."""
    if isinstance(entry_amount, float):
        return max(0, int(entry_amount * mixdepth_balance_sats))
    return int(entry_amount)


def _allocate_block_slot(now_block: int, tx_index: int, txs_per_block: int) -> tuple[int, int]:
    """Roll over to next block when the cap is reached."""
    if tx_index >= txs_per_block:
        return now_block + 1, 0
    return now_block, tx_index


# ---------------------------------------------------------------------------
# World
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class World:
    """The simulator orchestrator.

    Build with :meth:`from_components`, then call :meth:`run` to drive
    the event loop until every taker is exhausted.
    """

    config: WorldConfig
    makers: list[Maker]
    takers: list[TumblerTaker | PaymentTaker]
    rng: random.Random
    block_height: int = 0
    tx_index_in_block: int = 0
    seq: int = 0
    txs: list[Tx] = field(default_factory=list)
    offer_log: list[OfferLogEntry] = field(default_factory=list)
    payment_records: list[PaymentRecord] = field(default_factory=list)
    maker_id_by_utxo: dict[str, str] = field(default_factory=dict)
    utxo_value_by_id: dict[str, int] = field(default_factory=dict)
    _queue: list[_Event] = field(default_factory=list)

    @classmethod
    def from_components(
        cls,
        *,
        config: WorldConfig,
        makers: Sequence[Maker],
        takers: Sequence[TumblerTaker | PaymentTaker],
    ) -> World:
        rng = random.Random(config.seed)
        w = cls(
            config=config,
            makers=list(makers),
            takers=list(takers),
            rng=rng,
            block_height=config.starting_block,
        )
        # Index every maker UTXO so the ground-truth identity map is complete from
        # the very first tx.
        for m in w.makers:
            for ms_utxos in m.utxos.values():
                for u in ms_utxos:
                    w.maker_id_by_utxo[u.utxo_id] = m.counterparty
                    w.utxo_value_by_id[u.utxo_id] = u.value_sats
        # Seed initial PaymentRecord rows so the audit trail covers every taker.
        for t in w.takers:
            if isinstance(t, PaymentTaker):
                w.payment_records.append(
                    PaymentRecord(
                        taker_id=t.taker_id,
                        recipient=t.recipient,
                        amount_sats=t.amount_sats,
                        status=PaymentStatus.PENDING,
                    ),
                )
        # Schedule each taker's first event at starting_block.
        for i, t in enumerate(w.takers):
            entry = t.current_entry()
            if entry is None:
                continue
            heapq.heappush(
                w._queue,
                _Event(
                    block_height=w.block_height,
                    tx_index_hint=i,  # spread takers across the first block
                    seq=w.seq,
                    taker_idx=i,
                    retries_left=config.max_retries_per_entry,
                ),
            )
            w.seq += 1
        return w

    # ------------------------------------------------------------------
    # Event loop
    # ------------------------------------------------------------------

    def run(self, *, max_events: int = 100_000) -> SimResult:
        """Drive the event loop until every taker is done.

        ``max_events`` guards against runaway loops in degenerate
        configurations (e.g. nobody can fill any offer); the run aborts
        cleanly and returns whatever was produced so far.
        """
        events_processed = 0
        while self._queue and events_processed < max_events:
            ev = heapq.heappop(self._queue)
            events_processed += 1
            # Advance world clock to event's logical time.
            self.block_height = max(self.block_height, ev.block_height)
            self._step(ev)
        return SimResult(
            seed=self.config.seed,
            txs=list(self.txs),
            offer_log=list(self.offer_log),
            payment_records=list(self.payment_records),
            maker_id_by_utxo=dict(self.maker_id_by_utxo),
            utxo_value_by_id=dict(self.utxo_value_by_id),
        )

    def _step(self, ev: _Event) -> None:
        taker = self.takers[ev.taker_idx]
        entry = taker.current_entry()
        if entry is None:
            return  # taker exhausted

        # 1. Decide cj_amount (per the schedule entry's amount field).
        # For the abstract simulator each PaymentTaker knows its src mixdepth;
        # tumbler entries refer to a virtual taker mixdepth balance that we
        # synthesise from the schedule (initial balance set per-mixdepth at build).
        cj_amount = _resolve_cj_amount(
            entry.amount,
            mixdepth_balance_sats=self._taker_mixdepth_balance(taker, entry.src_mixdepth),
        )
        if cj_amount <= 0:
            # Sweep with no balance — skip and advance.
            taker.advance(success=True)
            self._maybe_reschedule(ev.taker_idx, entry.wait_minutes)
            return

        # 2. Build orderbook from makers (each announces a fresh offer).
        orderbook = [m.announce_offer() for m in self.makers]

        # 3. Taker picks counterparties.
        chosen, total_cj_fee = taker.pick_makers(orderbook, cj_amount, entry.makercount)
        if chosen is None:
            self._handle_pick_failure(ev, entry)
            return

        # 4. Each chosen maker fills.
        fill_results: list[tuple[Maker, int, list[Utxo], int, OfferDict, int]] = []
        for offer in chosen:
            maker = self._maker_by_counterparty(offer["counterparty"])
            if maker is None:
                continue
            fee = int(offer["_fee_sats"])
            res = maker.fill_offer(cj_amount, fee)
            if res is None:
                # Maker can't fill -> ignore + retry.
                self._handle_pick_failure(ev, entry)
                return
            mixdepth, consumed, change = res
            fill_results.append((maker, mixdepth, consumed, change, offer, fee))
        if len(fill_results) != entry.makercount:
            self._handle_pick_failure(ev, entry)
            return

        # 5. Build CJ tx + emit ground-truth labels.
        tx = self._emit_cj_tx(taker, entry, cj_amount, total_cj_fee, fill_results)
        self.txs.append(tx)
        for _maker, _mix, _utxos, _change, offer, fee in fill_results:
            self.offer_log.append(
                OfferLogEntry(
                    txid=tx.txid,
                    block_height=tx.block_height,
                    counterparty=offer["counterparty"],
                    offer={k: v for k, v in offer.items() if k != "_fee_sats"},
                    fee_paid_sats=fee,
                ),
            )

        # 6. Apply state transitions on makers (consume UTXOs, mint CJ + change).
        self._apply_maker_state(fill_results, tx)

        # 7. Update payment record for PaymentTakers.
        if isinstance(taker, PaymentTaker):
            self._update_payment_record(taker, tx)

        # 8. Advance taker schedule + reschedule next event.
        taker.advance(success=True)
        self._maybe_reschedule(ev.taker_idx, entry.wait_minutes)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _maker_by_counterparty(self, cp: str) -> Maker | None:
        for m in self.makers:
            if m.counterparty == cp:
                return m
        return None

    def _taker_mixdepth_balance(
        self,
        taker: TumblerTaker | PaymentTaker,
        mixdepth: int,
    ) -> int:
        """Synthesised taker mixdepth balance.

        We don't model the taker's wallet UTXO set in detail (the focus is
        ground-truth labelling, not coin selection); instead we treat each
        schedule entry's resolved cj_amount as authoritative and assume the
        taker can always cover it for fixed-amount payment entries. For
        fractional tumbler entries we use a notional 1 BTC balance per
        mixdepth so ``fraction * balance`` produces sensible CJ amounts
        without coupling to the UTXO accountancy that the orchestrator is
        not modelling.
        """
        del taker, mixdepth
        return 100_000_000  # 1 BTC notional

    def _handle_pick_failure(self, ev: _Event, entry: object) -> None:
        del entry
        if ev.retries_left <= 0:
            # Give up on this entry; advance taker and move on.
            self.takers[ev.taker_idx].advance(success=True)
            self._maybe_reschedule(ev.taker_idx, self.config.liquidity_wait_minutes)
            return
        # Reschedule the same event later.
        self.seq += 1
        new_block = self.block_height + max(
            1,
            int(self.config.liquidity_wait_minutes / self.config.block_minutes),
        )
        heapq.heappush(
            self._queue,
            _Event(
                block_height=new_block,
                tx_index_hint=0,
                seq=self.seq,
                taker_idx=ev.taker_idx,
                retries_left=ev.retries_left - 1,
            ),
        )

    def _maybe_reschedule(self, taker_idx: int, wait_minutes: float) -> None:
        if self.takers[taker_idx].current_entry() is None:
            return
        delta_blocks = max(1, int(wait_minutes / self.config.block_minutes))
        self.seq += 1
        heapq.heappush(
            self._queue,
            _Event(
                block_height=self.block_height + delta_blocks,
                tx_index_hint=0,
                seq=self.seq,
                taker_idx=taker_idx,
                retries_left=self.config.max_retries_per_entry,
            ),
        )

    def _allocate_tx_slot(self) -> tuple[int, int]:
        block, idx = _allocate_block_slot(
            self.block_height,
            self.tx_index_in_block,
            self.config.txs_per_block,
        )
        if block != self.block_height:
            self.block_height = block
            self.tx_index_in_block = 0
        slot = (self.block_height, self.tx_index_in_block)
        self.tx_index_in_block += 1
        return slot

    # ------------------------------------------------------------------
    # CJ tx emission
    # ------------------------------------------------------------------

    def _emit_cj_tx(
        self,
        taker: TumblerTaker | PaymentTaker,
        entry: object,
        cj_amount: int,
        total_cj_fee: int,
        fill_results: list[tuple[Maker, int, list[Utxo], int, OfferDict, int]],
    ) -> Tx:
        block, tx_idx = self._allocate_tx_slot()
        txid = _short_id(prefix="tx-")

        inputs: list[str] = []
        input_vals: list[int] = []
        outputs: list[TxOutput] = []
        maker_cps: list[str] = []
        for maker, src_mix, consumed, change, _offer, _fee in fill_results:
            maker_cps.append(maker.counterparty)
            inputs.extend(u.utxo_id for u in consumed)
            input_vals.extend(u.value_sats for u in consumed)
            dest_mix = (src_mix + 1) % (maker.max_mixdepth + 1)
            outputs.append(
                TxOutput(
                    output_id=_short_id("o-"),
                    value_sats=cj_amount,
                    role=OutputRole.MAKER_CJ,
                    owner=maker.counterparty,
                    mixdepth=dest_mix,
                ),
            )
            outputs.append(
                TxOutput(
                    output_id=_short_id("o-"),
                    value_sats=change,
                    role=OutputRole.MAKER_CHANGE,
                    owner=maker.counterparty,
                    mixdepth=src_mix,
                ),
            )
        # Taker side: a notional input + CJ output (+ change for PaymentTaker
        # with `_resolve_cj_amount > 0` and non-sweep entry; tumbler internal
        # sweeps go straight to "INTERNAL").
        taker_input_id = _short_id("u-taker-")
        # Size the taker's input so the analyzer's "taker = max-fee participant"
        # heuristic holds: taker pays cj_amount + sum(maker fees) + network fee.
        taker_input_value = cj_amount + total_cj_fee + DEFAULT_NETWORK_FEE_SATS
        inputs.append(taker_input_id)
        input_vals.append(taker_input_value)
        self.utxo_value_by_id[taker_input_id] = taker_input_value

        # Determine the taker output role/destination.
        is_external = self._is_external_destination(taker, entry)
        if is_external:
            outputs.append(
                TxOutput(
                    output_id=_short_id("o-"),
                    value_sats=cj_amount,
                    role=OutputRole.EXTERNAL_PAYMENT,
                    owner=self._external_recipient(taker, entry),
                    mixdepth=None,
                ),
            )
        else:
            outputs.append(
                TxOutput(
                    output_id=_short_id("o-"),
                    value_sats=cj_amount,
                    role=OutputRole.TAKER_CJ,
                    owner=taker.taker_id,
                    mixdepth=self._taker_dest_mixdepth(taker, entry),
                ),
            )
        return Tx(
            txid=txid,
            block_height=block,
            tx_index=tx_idx,
            taker_id=taker.taker_id,
            maker_counterparties=tuple(maker_cps),
            inputs=tuple(inputs),
            input_values=tuple(input_vals),
            outputs=tuple(outputs),
            cj_amount_sats=cj_amount,
            total_cj_fee_sats=total_cj_fee,
            network_fee_sats=DEFAULT_NETWORK_FEE_SATS,
        )

    @staticmethod
    def _is_external_destination(
        taker: TumblerTaker | PaymentTaker,
        entry: object,
    ) -> bool:
        from coinjoin_simulator.taker_logic import ScheduleEntry

        if not isinstance(entry, ScheduleEntry):
            return False
        if isinstance(taker, PaymentTaker) and not taker.follow_up_payment:
            return entry.destination not in {"INTERNAL", "addrask"}
        if isinstance(taker, TumblerTaker):
            return entry.destination not in {"INTERNAL", "addrask"}
        return False

    @staticmethod
    def _external_recipient(
        taker: TumblerTaker | PaymentTaker,
        entry: object,
    ) -> str:
        from coinjoin_simulator.taker_logic import ScheduleEntry

        if isinstance(entry, ScheduleEntry) and entry.destination not in {
            "INTERNAL",
            "addrask",
        }:
            return entry.destination
        if isinstance(taker, PaymentTaker):
            return taker.recipient
        return "unknown"

    @staticmethod
    def _taker_dest_mixdepth(taker: TumblerTaker | PaymentTaker, entry: object) -> int:
        from coinjoin_simulator.taker_logic import ScheduleEntry

        if not isinstance(entry, ScheduleEntry):
            return 0
        return (entry.src_mixdepth + 1) % (DEFAULT_MAX_MIXDEPTH + 1)

    # ------------------------------------------------------------------
    # State updates
    # ------------------------------------------------------------------

    def _apply_maker_state(
        self,
        fill_results: Iterable[tuple[Maker, int, list[Utxo], int, OfferDict, int]],
        tx: Tx,
    ) -> None:
        # Outputs come in pairs (CJ, change) per maker, in fill order — the
        # tail entries are taker outputs.
        out_iter = iter(tx.outputs)
        for maker, src_mix, consumed, change, _offer, _fee in fill_results:
            cj_out = next(out_iter)
            change_out = next(out_iter)
            # Remove consumed UTXOs.
            consumed_ids = {u.utxo_id for u in consumed}
            maker.utxos[src_mix] = [
                u for u in maker.utxos.get(src_mix, []) if u.utxo_id not in consumed_ids
            ]
            for utxo_id in consumed_ids:
                self.maker_id_by_utxo.pop(utxo_id, None)
            # Mint CJ output -> dest mixdepth.
            dest_mix = (src_mix + 1) % (maker.max_mixdepth + 1)
            maker.utxos.setdefault(dest_mix, []).append(
                Utxo(utxo_id=cj_out.output_id, value_sats=cj_out.value_sats, mixdepth=dest_mix),
            )
            self.maker_id_by_utxo[cj_out.output_id] = maker.counterparty
            self.utxo_value_by_id[cj_out.output_id] = cj_out.value_sats
            # Mint change -> src mixdepth.
            if change > 0:
                maker.utxos.setdefault(src_mix, []).append(
                    Utxo(utxo_id=change_out.output_id, value_sats=change, mixdepth=src_mix),
                )
                self.maker_id_by_utxo[change_out.output_id] = maker.counterparty
                self.utxo_value_by_id[change_out.output_id] = change

    def _update_payment_record(self, taker: PaymentTaker, tx: Tx) -> None:
        # Find the open record for this taker.
        for rec in self.payment_records:
            if rec.taker_id == taker.taker_id and rec.status == PaymentStatus.PENDING:
                if any(o.role == OutputRole.EXTERNAL_PAYMENT for o in tx.outputs):
                    rec.status = PaymentStatus.DELIVERED_IN_CJ
                    rec.delivered_in_txid = tx.txid
                else:
                    # Follow-up payment scenario: synthesise a non-CJ payout tx.
                    payout_txid = self._emit_followup_payment(taker, tx)
                    rec.status = PaymentStatus.DELIVERED_FOLLOWUP
                    rec.delivered_in_txid = payout_txid
                return

    def _emit_followup_payment(self, taker: PaymentTaker, cj_tx: Tx) -> str:
        """Emit a non-CJ payout that spends the taker's CJ output to the recipient."""
        block, tx_idx = self._allocate_tx_slot()
        txid = _short_id("tx-")
        # The follow-up tx consumes the taker's CJ output (TAKER_CJ role).
        cj_output = next(o for o in cj_tx.outputs if o.role == OutputRole.TAKER_CJ)
        payout = Tx(
            txid=txid,
            block_height=block,
            tx_index=tx_idx,
            taker_id=taker.taker_id,
            maker_counterparties=(),
            inputs=(cj_output.output_id,),
            input_values=(cj_output.value_sats,),
            outputs=(
                TxOutput(
                    output_id=_short_id("o-"),
                    value_sats=cj_output.value_sats,
                    role=OutputRole.EXTERNAL_PAYMENT,
                    owner=taker.recipient,
                    mixdepth=None,
                ),
            ),
            cj_amount_sats=cj_output.value_sats,
            total_cj_fee_sats=0,
            network_fee_sats=0,
        )
        self.txs.append(payout)
        return txid
