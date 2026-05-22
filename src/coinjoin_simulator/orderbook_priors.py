"""Orderbook snapshot loader.

Pulls the live JoinMarket-NG orderbook from a directory node, distils
per-counterparty priors useful for the simulator (offer sizes, fee
policies, fidelity-bond value), and persists the snapshot to disk so
downstream pipelines stay deterministic.

Source: ``https://joinmarket-ng.sgn.space/orderbook.json``.

Bonded-offer convention (per the project's stated rule): only offers
whose maker has ``fidelity_bond_value > 0`` count for clustering /
statistics. Bondless offers are not included unless their ``cjfee``
is exactly zero, in which case they may still be sampled as
"free-rider" makers.

The snapshot persists raw counterparty offers along with derived
distributions so the simulator can sample without re-fetching:

- offer-size distribution (sats): empirical CDF of ``maxsize`` for
  bonded counterparties.
- fee-policy distribution: separately for ``relative`` (sw0reloffer
  / swreloffer / reloffer) and ``absolute`` (sw0absoffer /
  swabsoffer / absoffer).
- fidelity-bond value distribution: empirical CDF.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal
from urllib.request import Request, urlopen

if TYPE_CHECKING:
    from pathlib import Path

DEFAULT_ORDERBOOK_URL = "https://joinmarket-ng.sgn.space/orderbook.json"

# JoinMarket order types we care about. Anything else is ignored.
RELATIVE_ORDERTYPES = frozenset({"reloffer", "sw0reloffer", "swreloffer"})
ABSOLUTE_ORDERTYPES = frozenset({"absoffer", "sw0absoffer", "swabsoffer"})

FeeType = Literal["relative", "absolute"]


@dataclass(frozen=True, slots=True)
class Offer:
    """A single offer from one counterparty."""

    oid: int
    ordertype: str
    fee_type: FeeType
    cjfee: float  # absolute: sats; relative: fraction of cj amount
    minsize_sats: int
    maxsize_sats: int
    txfee_sats: int


@dataclass(frozen=True, slots=True)
class CounterpartyProfile:
    """A bonded maker (or zero-fee bondless one), aggregating its offers."""

    counterparty: str
    offers: tuple[Offer, ...]
    fidelity_bond_value: float
    bond_locktime: int | None
    bond_amount_sats: int | None

    @property
    def is_bonded(self) -> bool:
        return self.fidelity_bond_value > 0.0

    @property
    def max_size_sats(self) -> int:
        return max(o.maxsize_sats for o in self.offers)

    @property
    def min_size_sats(self) -> int:
        return min(o.minsize_sats for o in self.offers)

    @property
    def primary_fee_type(self) -> FeeType:
        # Pick the fee_type used by the offer with the largest maxsize.
        return max(self.offers, key=lambda o: o.maxsize_sats).fee_type

    @property
    def primary_cjfee(self) -> float:
        return max(self.offers, key=lambda o: o.maxsize_sats).cjfee


@dataclass(frozen=True, slots=True)
class OrderbookSnapshot:
    """Persisted, derived view of a single orderbook fetch."""

    fetched_at: int  # unix timestamp from server
    source_url: str
    counterparties: tuple[CounterpartyProfile, ...]
    n_total_offers: int
    n_bonded_counterparties: int
    n_zero_fee_bondless: int

    # Empirical distributions, kept as sorted tuples for fast quantile lookup.
    bond_values: tuple[float, ...] = field(default_factory=tuple)
    max_sizes_sats: tuple[int, ...] = field(default_factory=tuple)
    relative_cjfees: tuple[float, ...] = field(default_factory=tuple)
    absolute_cjfees_sats: tuple[float, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


def fetch_orderbook(url: str = DEFAULT_ORDERBOOK_URL, timeout: int = 30) -> dict[str, Any]:
    req = Request(url, headers={"User-Agent": "coinjoin-simulator/orderbook-priors"})
    with urlopen(req, timeout=timeout) as resp:  # noqa: S310 - trusted scheme
        raw = resp.read()
    decoded = json.loads(raw)
    if not isinstance(decoded, dict):
        raise ValueError("orderbook payload is not a JSON object")
    return decoded


# ---------------------------------------------------------------------------
# Derive
# ---------------------------------------------------------------------------


def _coerce_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def _coerce_float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _classify_fee_type(ordertype: str) -> FeeType | None:
    if ordertype in RELATIVE_ORDERTYPES:
        return "relative"
    if ordertype in ABSOLUTE_ORDERTYPES:
        return "absolute"
    return None


def _parse_timestamp(raw: Any) -> int:
    """Parse a directory-node timestamp into a unix-epoch int."""
    if isinstance(raw, (int, float)):
        return int(raw)
    if isinstance(raw, str):
        try:
            return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
        except ValueError:
            return 0
    return 0


def derive_snapshot(
    raw: dict[str, Any],
    source_url: str = DEFAULT_ORDERBOOK_URL,
) -> OrderbookSnapshot:
    """Turn a raw orderbook payload into a typed, persistable snapshot."""

    fetched_at = _parse_timestamp(raw.get("timestamp"))
    offers_raw = raw.get("offers")
    if not isinstance(offers_raw, list):
        raise ValueError("orderbook missing 'offers' list")

    bonds_raw = raw.get("fidelitybonds") or []
    bonds_by_cp: dict[str, dict[str, Any]] = {}
    if isinstance(bonds_raw, list):
        for fb in bonds_raw:
            if not isinstance(fb, dict):
                continue
            cp = fb.get("counterparty")
            if isinstance(cp, str):
                bonds_by_cp[cp] = fb

    # Group offers by counterparty.
    grouped: dict[str, list[Offer]] = {}
    n_total = 0
    for offer_raw in offers_raw:
        if not isinstance(offer_raw, dict):
            continue
        cp = offer_raw.get("counterparty")
        if not isinstance(cp, str) or not cp:
            continue
        ordertype = offer_raw.get("ordertype")
        if not isinstance(ordertype, str):
            continue
        fee_type = _classify_fee_type(ordertype)
        if fee_type is None:
            continue
        maxsize = _coerce_int(offer_raw.get("maxsize"), 0)
        minsize = _coerce_int(offer_raw.get("minsize"), 0)
        if maxsize <= 0 or minsize <= 0 or minsize > maxsize:
            continue
        cjfee = _coerce_float(offer_raw.get("cjfee"), 0.0)
        oid = _coerce_int(offer_raw.get("oid"), 0)
        txfee = _coerce_int(offer_raw.get("txfee"), 0)
        grouped.setdefault(cp, []).append(
            Offer(
                oid=oid,
                ordertype=ordertype,
                fee_type=fee_type,
                cjfee=cjfee,
                minsize_sats=minsize,
                maxsize_sats=maxsize,
                txfee_sats=txfee,
            ),
        )
        n_total += 1

    profiles: list[CounterpartyProfile] = []
    n_bonded = 0
    n_zero_fee = 0
    for cp, offers in grouped.items():
        fb = bonds_by_cp.get(cp)
        bond_value = _coerce_float(fb.get("bond_value"), 0.0) if fb else 0.0
        bond_locktime = _coerce_int(fb["locktime"], 0) if fb and "locktime" in fb else None
        bond_amount = _coerce_int(fb["amount"], 0) if fb and "amount" in fb else None

        is_bonded = bond_value > 0
        # Apply rule: keep bonded; allow bondless only if every offer has cjfee == 0
        if not is_bonded:
            if not all(o.cjfee == 0 for o in offers):
                continue
            n_zero_fee += 1
        else:
            n_bonded += 1

        profiles.append(
            CounterpartyProfile(
                counterparty=cp,
                offers=tuple(offers),
                fidelity_bond_value=bond_value,
                bond_locktime=bond_locktime,
                bond_amount_sats=bond_amount,
            ),
        )

    bond_values = tuple(sorted(p.fidelity_bond_value for p in profiles if p.is_bonded))
    max_sizes = tuple(sorted(p.max_size_sats for p in profiles))
    rel_fees = tuple(
        sorted(o.cjfee for p in profiles for o in p.offers if o.fee_type == "relative"),
    )
    abs_fees = tuple(
        sorted(o.cjfee for p in profiles for o in p.offers if o.fee_type == "absolute"),
    )

    return OrderbookSnapshot(
        fetched_at=fetched_at,
        source_url=source_url,
        counterparties=tuple(profiles),
        n_total_offers=n_total,
        n_bonded_counterparties=n_bonded,
        n_zero_fee_bondless=n_zero_fee,
        bond_values=bond_values,
        max_sizes_sats=max_sizes,
        relative_cjfees=rel_fees,
        absolute_cjfees_sats=abs_fees,
    )


# ---------------------------------------------------------------------------
# Persist / load
# ---------------------------------------------------------------------------


def snapshot_to_dict(s: OrderbookSnapshot) -> dict[str, Any]:
    return {
        "fetched_at": s.fetched_at,
        "source_url": s.source_url,
        "n_total_offers": s.n_total_offers,
        "n_bonded_counterparties": s.n_bonded_counterparties,
        "n_zero_fee_bondless": s.n_zero_fee_bondless,
        "counterparties": [
            {
                **{k: v for k, v in asdict(p).items() if k != "offers"},
                "offers": [asdict(o) for o in p.offers],
            }
            for p in s.counterparties
        ],
        "distributions": {
            "bond_values": list(s.bond_values),
            "max_sizes_sats": list(s.max_sizes_sats),
            "relative_cjfees": list(s.relative_cjfees),
            "absolute_cjfees_sats": list(s.absolute_cjfees_sats),
        },
    }


def snapshot_from_dict(payload: dict[str, Any]) -> OrderbookSnapshot:
    cps_raw = payload.get("counterparties", [])
    profiles: list[CounterpartyProfile] = []
    for c in cps_raw:
        offers = tuple(
            Offer(
                oid=int(o["oid"]),
                ordertype=str(o["ordertype"]),
                fee_type=str(o["fee_type"]),  # type: ignore[arg-type]
                cjfee=float(o["cjfee"]),
                minsize_sats=int(o["minsize_sats"]),
                maxsize_sats=int(o["maxsize_sats"]),
                txfee_sats=int(o["txfee_sats"]),
            )
            for o in c["offers"]
        )
        profiles.append(
            CounterpartyProfile(
                counterparty=str(c["counterparty"]),
                offers=offers,
                fidelity_bond_value=float(c["fidelity_bond_value"]),
                bond_locktime=c.get("bond_locktime"),
                bond_amount_sats=c.get("bond_amount_sats"),
            ),
        )
    dist = payload.get("distributions", {})
    return OrderbookSnapshot(
        fetched_at=int(payload.get("fetched_at", 0)),
        source_url=str(payload.get("source_url", DEFAULT_ORDERBOOK_URL)),
        counterparties=tuple(profiles),
        n_total_offers=int(payload.get("n_total_offers", 0)),
        n_bonded_counterparties=int(payload.get("n_bonded_counterparties", 0)),
        n_zero_fee_bondless=int(payload.get("n_zero_fee_bondless", 0)),
        bond_values=tuple(float(x) for x in dist.get("bond_values", ())),
        max_sizes_sats=tuple(int(x) for x in dist.get("max_sizes_sats", ())),
        relative_cjfees=tuple(float(x) for x in dist.get("relative_cjfees", ())),
        absolute_cjfees_sats=tuple(float(x) for x in dist.get("absolute_cjfees_sats", ())),
    )


def save_snapshot(s: OrderbookSnapshot, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snapshot_to_dict(s), indent=2, sort_keys=True) + "\n")


def load_snapshot(path: Path) -> OrderbookSnapshot:
    return snapshot_from_dict(json.loads(path.read_text()))


# ---------------------------------------------------------------------------
# Convenience
# ---------------------------------------------------------------------------


def refresh_snapshot(
    path: Path,
    url: str = DEFAULT_ORDERBOOK_URL,
    timeout: int = 30,
) -> OrderbookSnapshot:
    """Fetch + derive + persist in one call."""
    raw = fetch_orderbook(url=url, timeout=timeout)
    snap = derive_snapshot(raw, source_url=url)
    save_snapshot(snap, path)
    return snap
