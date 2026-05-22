"""Pure algorithms ported from JoinMarket-Org/joinmarket-clientserver.

Faithful behavioral ports of the reference tumbler / sendpayment
schedule generation and offer-selection logic, free of the wallet,
network, and PSBT layers. These functions are deterministic given a
``random.Random`` instance, which makes them easy to unit-test and to
seed for reproducible simulator runs.

References (citations are file:line into the upstream tree, kept here
so the port stays auditable):

- ``schedule.get_tumble_schedule``  — schedule.py:91
- ``schedule.get_amount_fractions``  — schedule.py:64
- ``support.choose_orders``  — support.py:250
- ``support.choose_sweep_orders``  — support.py:311
- ``support.weighted_order_choose``  — support.py:180
- ``support.random_under_max_order_choose``  — support.py:211
- ``support.cheapest_order_choose``  — support.py:216
- ``support.fidelity_bond_weighted_order_choose``  — support.py:222
- ``taker_utils.tumbler_filter_orders_callback``  — taker_utils.py:503

Constants below mirror the upstream defaults (``cli_options.py`` and
``configure.py``); the simulator can override them per-run.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    import random
    from collections.abc import Callable, Sequence

# Offers from the orderbook are loose dicts, mirroring upstream's JSON shape.
OfferDict = dict[str, Any]

# ---------------------------------------------------------------------------
# Reference defaults (cli_options.py:280-405, configure.py:96/340/466-488).
# ---------------------------------------------------------------------------

# Tumbler defaults.
DEFAULT_MIXDEPTHCOUNT = 4
DEFAULT_MIN_TXCOUNT = 2
DEFAULT_TXCOUNT_PARAMS = (2.0, 1.0)  # (mu, sigma) for Normal txcount draw.
DEFAULT_MAKERCOUNT_RANGE = (9.0, 1.0)  # (mu, sigma) for Normal makercount draw.
DEFAULT_MIN_MAKERCOUNT = 4
DEFAULT_ADDRCOUNT = 3
DEFAULT_TIMELAMBDA_MIN = 60.0
DEFAULT_STAGE1_TIMELAMBDA_INCREASE = 3.0
DEFAULT_MIN_CJ_AMOUNT_SATS = 100_000
DEFAULT_ROUNDING_CHANCE = 0.25
DEFAULT_ROUNDING_SIGFIG_WEIGHTS = (55.0, 15.0, 25.0, 65.0, 40.0)  # for k = 1..5
NO_ROUNDING = 16

# Maker policy defaults.
MAKER_DEFAULT_CJFEE_A_SATS = 500
MAKER_DEFAULT_CJFEE_R = 0.00002
MAKER_DEFAULT_CJFEE_FACTOR = 0.1
MAKER_DEFAULT_TXFEE_CONTRIBUTION = 0
MAKER_DEFAULT_TXFEE_FACTOR = 0.3
MAKER_DEFAULT_MINSIZE = 100_000
MAKER_DEFAULT_SIZE_FACTOR = 0.1
DUST_THRESHOLD_SATS = 2730  # bitcoin-core dust

# Network policy.
POLICY_MINIMUM_MAKERS = 4
DEFAULT_BONDLESS_MAKERS_ALLOWANCE = 0.125

OrderChooser = Literal["weighted", "cheapest", "random_under_max", "fidelity_bond_weighted"]

# ---------------------------------------------------------------------------
# Schedule entry
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ScheduleEntry:
    """One row of a tumbler / sendpayment schedule.

    Mirrors the seven-tuple used upstream (schedule.py:60).

    ``amount`` is a fraction of the source mixdepth balance when ``float``,
    a satoshi amount when ``int``, and ``0`` when the entry is a sweep.
    """

    src_mixdepth: int
    amount: float | int  # fraction (float) | sats (int) | 0 (sweep sentinel)
    makercount: int
    destination: str  # "INTERNAL" | external bech32 | "addrask"
    wait_minutes: float
    rounding: int  # NO_ROUNDING or sigfig count
    completed: int  # 0 = pending, 1 = succeeded


# ---------------------------------------------------------------------------
# Offer-selection primitives  (port of support.py)
# ---------------------------------------------------------------------------


# Default log10 stride for cjfee_r quantization.  Matches the on-chain
# clusterer's ``log_stride = 0.1``: a maker that snaps to this grid lands
# in the same fee-band cell as its log-decimal neighbours, so the
# forward-spend matcher can no longer separate it from peers.
DEFAULT_QUANTIZE_LOG_STRIDE = 0.1


def quantize_cjfee_r(value: float, log_stride: float) -> float:
    """Snap a positive ``cjfee_r`` to a log10 grid of width ``log_stride``.

    Returns the lower edge of the band, ``10 ** (floor(log10(value) /
    stride) * stride)``.  All values within one band thus map to the same
    quantised fee, which is the property §8.2 of the maker-clustering
    paper relies on: makers within a cell quote literally the same number
    after quantization, and a taker's cheapest-first preference becomes a
    uniform tie-break across them.

    A non-positive ``value`` is returned unchanged (clusterer convention:
    a free or zero-fee offer is its own degenerate band).  A
    ``log_stride <= 0`` disables quantization (returns ``value``).
    """
    if value <= 0 or log_stride <= 0:
        return value
    band = math.floor(math.log10(value) / log_stride)
    return 10 ** (band * log_stride)


def calc_cj_fee(ordertype: str, cjfee: float, cj_amount_sats: int) -> int:
    """Per-offer fee in sats.  (support.py:169-177).

    Absolute offers: ``int(cjfee)`` directly.
    Relative offers: ``int(round(cjfee * cj_amount_sats))``.
    """
    if ordertype in {"absoffer", "sw0absoffer", "swabsoffer"}:
        return int(cjfee)
    if ordertype in {"reloffer", "sw0reloffer", "swreloffer"}:
        return int((Decimal(str(cjfee)) * Decimal(cj_amount_sats)).quantize(Decimal(1)))
    msg = f"unknown ordertype: {ordertype}"
    raise ValueError(msg)


def is_within_max_limits(
    fee_sats: int, cj_amount_sats: int, max_fee_rel: float, max_fee_abs: int
) -> bool:
    """Per-offer max-fee check.  (support.py:243-247).

    Reject only when BOTH the absolute and relative ceilings are exceeded
    (lenient OR-pass).
    """
    return not (fee_sats > max_fee_abs and fee_sats > cj_amount_sats * max_fee_rel)


def _allowed_for_amount(
    offers: Sequence[OfferDict],
    cj_amount_sats: int,
    ignored: frozenset[str],
    allowed_ordertypes: frozenset[str],
) -> list[OfferDict]:
    """Pre-filter offers per support.py:250-262."""
    out: list[OfferDict] = []
    for o in offers:
        if o["counterparty"] in ignored:
            continue
        if o["minsize"] >= cj_amount_sats or o["maxsize"] <= cj_amount_sats:
            continue
        if o["ordertype"] not in allowed_ordertypes:
            continue
        out.append(o)
    return out


def _annotate_fees(
    offers: Sequence[OfferDict],
    cj_amount_sats: int,
    max_fee_rel: float,
    max_fee_abs: int,
    *,
    quantize_log_stride: float | None = None,
) -> list[OfferDict]:
    """Return offers with a ``_fee_sats`` key, dropping rows over BOTH ceilings.

    When ``quantize_log_stride`` is positive, every relative offer's
    ``cjfee`` is first snapped to the log10 grid via
    :func:`quantize_cjfee_r` (§8.2 of the maker-clustering paper).  The
    snap is applied to a per-offer copy and is the value the taker uses
    for fee comparison and the value persisted into the chosen offer
    dict; the original orderbook entry is not mutated.
    """
    out: list[OfferDict] = []
    for o in offers:
        cjfee = o["cjfee"]
        if (
            quantize_log_stride is not None
            and quantize_log_stride > 0
            and o["ordertype"] in {"reloffer", "sw0reloffer", "swreloffer"}
        ):
            cjfee = quantize_cjfee_r(float(cjfee), quantize_log_stride)
        fee = calc_cj_fee(o["ordertype"], cjfee, cj_amount_sats) - int(o.get("txfee", 0))
        if not is_within_max_limits(fee, cj_amount_sats, max_fee_rel, max_fee_abs):
            continue
        annotated = dict(o)
        annotated["cjfee"] = cjfee
        annotated["_fee_sats"] = fee
        out.append(annotated)
    return out


def _dedupe_cheapest_per_counterparty(offers: Sequence[OfferDict]) -> list[OfferDict]:
    """Keep cheapest offer per counterparty, sorted ascending by fee. (support.py:282-292)."""
    best: dict[str, OfferDict] = {}
    for o in offers:
        cp = o["counterparty"]
        if cp not in best or o["_fee_sats"] < best[cp]["_fee_sats"]:
            best[cp] = o
    return sorted(best.values(), key=lambda o: o["_fee_sats"])


def weighted_order_choose(orders: Sequence[OfferDict], n: int, rng: random.Random) -> OfferDict:
    """Exponentially weighted choice.  (support.py:180-208)."""
    fees = [o["_fee_sats"] for o in orders]
    fmin = min(fees)
    M = min(3 * n, len(orders) - 1)
    phi = fees[M] - fmin
    if phi <= 0:
        return rng.choice(list(orders))
    weights = [math.exp(-(f - fmin) / phi) for f in fees]
    return rng.choices(list(orders), weights=weights, k=1)[0]


def cheapest_order_choose(orders: Sequence[OfferDict], n: int, rng: random.Random) -> OfferDict:
    """Always pick the lowest-fee offer.  (support.py:216-220)."""
    del n, rng
    return min(orders, key=lambda o: o["_fee_sats"])


def random_under_max_order_choose(
    orders: Sequence[OfferDict],
    n: int,
    rng: random.Random,
) -> OfferDict:
    """Uniform random over already-pre-filtered offers.  (support.py:211-213)."""
    del n
    return rng.choice(list(orders))


def fidelity_bond_weighted_order_choose(
    orders: Sequence[OfferDict],
    n: int,
    rng: random.Random,
    *,
    bondless_allowance: float = DEFAULT_BONDLESS_MAKERS_ALLOWANCE,
) -> OfferDict:
    """Bond-weighted choice with a bondless-fallback.  (support.py:222-241)."""
    if rng.random() < bondless_allowance:
        return random_under_max_order_choose(orders, n, rng)
    bonded = [o for o in orders if o.get("fidelity_bond_value", 0.0) > 0.0]
    if not bonded:
        return random_under_max_order_choose(orders, n, rng)
    weights = [float(o["fidelity_bond_value"]) for o in bonded]
    return rng.choices(bonded, weights=weights, k=1)[0]


_CHOOSERS: dict[str, Callable[[Sequence[OfferDict], int, random.Random], OfferDict]] = {
    "weighted": weighted_order_choose,
    "cheapest": cheapest_order_choose,
    "random_under_max": random_under_max_order_choose,
    "fidelity_bond_weighted": fidelity_bond_weighted_order_choose,
}


def choose_orders(
    orderbook: Sequence[OfferDict],
    cj_amount_sats: int,
    n: int,
    *,
    rng: random.Random,
    ignored: frozenset[str] = frozenset(),
    allowed_ordertypes: frozenset[str] = frozenset({"sw0reloffer", "sw0absoffer"}),
    max_fee_rel: float = 0.001,
    max_fee_abs: int = 10_000,
    chooser: OrderChooser = "weighted",
    quantize_log_stride: float | None = None,
) -> tuple[list[OfferDict] | None, int]:
    """Select ``n`` distinct counterparties for a CJ.

    Returns ``(selected_offers, total_cjfee_sats)`` or ``(None, 0)`` when
    fewer than ``n`` distinct counterparties survive the filters
    (support.py:295-308).

    When ``quantize_log_stride`` is positive, every relative offer's
    ``cjfee`` is snapped to a log10 grid before fee comparison and
    selection (§8.2 maker-clustering: shared-grid quantization).  This
    is the simulator hook for the taker side of the offer-quantization
    defence; it is independent of any maker-side snapping and pairs
    naturally with :class:`coinjoin_simulator.agents.MakerFeePolicy`'s
    own ``quantize_log_stride`` field.
    """
    pool = _allowed_for_amount(orderbook, cj_amount_sats, ignored, allowed_ordertypes)
    pool = _annotate_fees(
        pool, cj_amount_sats, max_fee_rel, max_fee_abs, quantize_log_stride=quantize_log_stride
    )
    pool = _dedupe_cheapest_per_counterparty(pool)
    if len(pool) < n:
        return None, 0
    chosen: list[OfferDict] = []
    total_fee = 0
    chooser_fn = _CHOOSERS[chooser]
    remaining = list(pool)
    for _ in range(n):
        pick = chooser_fn(remaining, n, rng)
        chosen.append(pick)
        total_fee += int(pick["_fee_sats"])
        remaining = [o for o in remaining if o["counterparty"] != pick["counterparty"]]
    return chosen, total_fee


def tumbler_filter_orders_acceptable(
    total_cj_fee_sats: int,
    n: int,
    cj_amount_sats: int,
    *,
    max_fee_rel: float,
    max_fee_abs: int,
) -> bool:
    """Tumbler-level fee acceptance.  (taker_utils.py:503-517).

    Reject only when BOTH the average per-maker absolute fee exceeds
    ``max_fee_abs`` AND the average relative fee exceeds ``max_fee_rel``
    (same lenient OR-pass as per-offer).
    """
    abs_avg = total_cj_fee_sats / max(n, 1)
    rel_avg = abs_avg / max(cj_amount_sats, 1)
    return not (rel_avg > max_fee_rel and abs_avg > max_fee_abs)


# ---------------------------------------------------------------------------
# Schedule generation  (port of schedule.py)
# ---------------------------------------------------------------------------


def get_amount_fractions(
    count: int, rng: random.Random, *, min_last_fraction: float = 0.05
) -> list[float]:
    """Broken-stick partition of [0,1] into ``count`` parts.  (schedule.py:64-89).

    Reject and resample if the LAST fraction is ``<= min_last_fraction`` so
    there is enough headroom for the trailing sweep.
    """
    if count < 1:
        msg = "count must be >= 1"
        raise ValueError(msg)
    while True:
        if count == 1:
            return [1.0]
        knives = sorted(rng.random() for _ in range(count - 1))
        edges = [0.0, *knives, 1.0]
        fracs = [edges[i + 1] - edges[i] for i in range(count)]
        fracs.sort(reverse=True)
        if fracs[-1] > min_last_fraction:
            return fracs


def _rand_exp_minutes(timelambda: float, rng: random.Random) -> float:
    """Exponential wait, in minutes, rounded to 2 decimals.  (support.py:43-45)."""
    return round(rng.expovariate(1.0 / timelambda), 2)


def _rand_weighted_choice_int(weights: Sequence[float], rng: random.Random) -> int:
    """Index 0..len(weights)-1 picked with prob proportional to weights."""
    return rng.choices(range(len(weights)), weights=list(weights), k=1)[0]


def _draw_txcount(
    rng: random.Random,
    *,
    txcount_params: tuple[float, float] = DEFAULT_TXCOUNT_PARAMS,
    min_txcount: int = DEFAULT_MIN_TXCOUNT,
) -> int:
    mu, sigma = txcount_params
    return max(min_txcount, int(rng.gauss(mu, sigma)))


def _draw_makercount(
    rng: random.Random,
    *,
    makercount_range: tuple[float, float] = DEFAULT_MAKERCOUNT_RANGE,
    min_makercount: int = DEFAULT_MIN_MAKERCOUNT,
) -> int:
    mu, sigma = makercount_range
    return max(min_makercount, int(rng.gauss(mu, sigma)))


def get_tumble_schedule(
    *,
    rng: random.Random,
    destaddrs: Sequence[str],
    mixdepth_balances_sats: dict[int, int],
    max_mixdepth_in_wallet: int = 4,
    mixdepthcount: int = DEFAULT_MIXDEPTHCOUNT,
    txcount_params: tuple[float, float] = DEFAULT_TXCOUNT_PARAMS,
    min_txcount: int = DEFAULT_MIN_TXCOUNT,
    makercount_range: tuple[float, float] = DEFAULT_MAKERCOUNT_RANGE,
    min_makercount: int = DEFAULT_MIN_MAKERCOUNT,
    addrcount: int = DEFAULT_ADDRCOUNT,
    timelambda_min: float = DEFAULT_TIMELAMBDA_MIN,
    stage1_timelambda_increase: float = DEFAULT_STAGE1_TIMELAMBDA_INCREASE,
    rounding_chance: float = DEFAULT_ROUNDING_CHANCE,
    rounding_sigfig_weights: tuple[float, ...] = DEFAULT_ROUNDING_SIGFIG_WEIGHTS,
) -> list[ScheduleEntry]:
    """Build a tumbler schedule.  (schedule.py:91-201).

    ``mixdepth_balances_sats`` maps mixdepth -> balance in sats; mixdepths
    with zero balance are skipped in stage 1.

    Returns a list of :class:`ScheduleEntry` with ``completed=0``.
    """
    schedule: list[ScheduleEntry] = []
    cycle = max_mixdepth_in_wallet + 1

    # Stage 1: sweeps of every nonempty mixdepth, descending order.
    nonempty = sorted((m for m, b in mixdepth_balances_sats.items() if b > 0), reverse=True)
    stage1_lambda = timelambda_min * stage1_timelambda_increase
    for m in nonempty:
        schedule.append(
            ScheduleEntry(
                src_mixdepth=m,
                amount=0,
                makercount=_draw_makercount(
                    rng, makercount_range=makercount_range, min_makercount=min_makercount
                ),
                destination="INTERNAL",
                wait_minutes=_rand_exp_minutes(stage1_lambda, rng),
                rounding=NO_ROUNDING,
                completed=0,
            ),
        )

    # Lowest mixdepth that actually has coins after stage 1 — for our
    # abstract simulator that's whichever was lowest before stage 1.
    lowest_nonempty = min(nonempty) if nonempty else 0

    # Stage 2: mixdepthcount mixdepths in cyclic order.
    stage2_entries: list[
        tuple[int, ScheduleEntry, int]
    ] = []  # (mix_offset, entry, idx_in_mixdepth)
    for mix_offset in range(mixdepthcount - 1, -1, -1):
        # Iterate from last mixdepth (offset 0) backwards so the "last addrcount"
        # rule maps cleanly onto the user's destaddrs (schedule.py:181-201).
        m = (lowest_nonempty + (mixdepthcount - 1 - mix_offset)) % cycle
        in_external_window = mix_offset < addrcount
        # Force txcount >= 2 when this mixdepth must send out, but isn't the very last.
        forced_min = max(min_txcount, 2) if in_external_window and mix_offset != 0 else min_txcount
        txcount = max(
            forced_min, _draw_txcount(rng, txcount_params=txcount_params, min_txcount=min_txcount)
        )
        fractions = get_amount_fractions(txcount, rng)
        for i, frac in enumerate(fractions):
            entry = ScheduleEntry(
                src_mixdepth=m,
                amount=float(frac),
                makercount=_draw_makercount(
                    rng,
                    makercount_range=makercount_range,
                    min_makercount=min_makercount,
                ),
                destination="INTERNAL",
                wait_minutes=_rand_exp_minutes(timelambda_min, rng),
                rounding=(
                    _rand_weighted_choice_int(rounding_sigfig_weights, rng) + 1
                    if rng.random() < rounding_chance
                    else NO_ROUNDING
                ),
                completed=0,
            )
            stage2_entries.append((mix_offset, entry, i))

    # Force the last entry per mixdepth to a sweep (schedule.py:178-180).
    by_mix: dict[int, list[tuple[int, ScheduleEntry, int]]] = {}
    for tup in stage2_entries:
        by_mix.setdefault(tup[1].src_mixdepth, []).append(tup)
    for entries in by_mix.values():
        last_mix_offset, last_entry, _ = entries[-1]
        last_entry.amount = 0
        last_entry.rounding = NO_ROUNDING

    # External destination assignment in the last addrcount mixdepths
    # (schedule.py:181-189).
    external = ["addrask"] * (addrcount - len(destaddrs)) + list(reversed(destaddrs))
    # mix_offset 0 = last mixdepth; up to addrcount mixdepths get an external dest.
    for mix_offset in range(addrcount):
        # Find entries for this mix_offset's mixdepth.
        mix_entries = [t for t in stage2_entries if t[0] == mix_offset]
        if not mix_entries:
            continue
        # The "last internal entry" (i.e. the trailing sweep) becomes the external send.
        _, last_entry, _ = mix_entries[-1]
        last_entry.destination = external[mix_offset] if mix_offset < len(external) else "addrask"

    # Last mixdepth collapse (schedule.py:190-201): drop all INTERNAL entries
    # in the very last mixdepth and zero out the rest -- effectively a single sweep.
    last_mix_entries = [t for t in stage2_entries if t[0] == 0]
    if last_mix_entries:
        kept_for_last_mix: list[tuple[int, ScheduleEntry, int]] = []
        for tup in last_mix_entries:
            mix_offset, entry, _idx = tup
            if entry.destination == "INTERNAL":
                continue  # delete
            entry.amount = 0
            kept_for_last_mix.append(tup)
        stage2_entries = [t for t in stage2_entries if t[0] != 0] + kept_for_last_mix

    schedule.extend(t[1] for t in stage2_entries)
    return schedule


# ---------------------------------------------------------------------------
# Tumble-schedule tweak on failure (schedule.py:209-263)
# ---------------------------------------------------------------------------


def tweak_tumble_schedule(
    *,
    rng: random.Random,
    schedule: list[ScheduleEntry],
    failed_index: int,
    user_destaddrs: Sequence[str],
    minimum_makers: int = POLICY_MINIMUM_MAKERS,
) -> list[ScheduleEntry]:
    """Adjust the failed entry and remaining stage-2 entries.

    - Set the failed entry's destination to INTERNAL if it isn't a user dest.
    - For sweep entries (amount==0), decrement makercount down to the floor.
    - For non-sweep entries, redraw amount fractions for the remainder of
      the source mixdepth proportional to ``1 - already_spent``, last entry
      forced back to sweep.
    """
    failed = schedule[failed_index]
    if failed.destination not in user_destaddrs:
        failed.destination = "INTERNAL"
    if failed.amount == 0:
        failed.makercount = max(minimum_makers, failed.makercount - 1)
        return schedule

    # Find the remaining entries in the same mixdepth (incl. the failed one),
    # and redraw fractions for them.
    mix = failed.src_mixdepth
    remaining_idxs = [
        i
        for i, e in enumerate(schedule)
        if e.src_mixdepth == mix and i >= failed_index and e.completed == 0
    ]
    spent = sum(
        float(e.amount)
        for i, e in enumerate(schedule)
        if e.src_mixdepth == mix and isinstance(e.amount, float) and e.completed == 1
    )
    headroom = max(1e-6, 1.0 - spent)
    fracs = get_amount_fractions(len(remaining_idxs), rng)
    for j, i in enumerate(remaining_idxs):
        schedule[i].amount = float(fracs[j] * headroom)
    schedule[remaining_idxs[-1]].amount = 0  # last is a sweep
    return schedule
