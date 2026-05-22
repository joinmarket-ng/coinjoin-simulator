"""Tests for orderbook_priors.

Tests use the bundled ``data/orderbook_snapshot.json`` and a small
hand-written raw payload; they do not hit the network.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from coinjoin_simulator.orderbook_priors import (
    Offer,
    OrderbookSnapshot,
    derive_snapshot,
    load_snapshot,
    save_snapshot,
    snapshot_from_dict,
    snapshot_to_dict,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT_PATH = REPO_ROOT / "data" / "orderbook_snapshot.json"


@pytest.fixture()
def raw_payload() -> dict[str, object]:
    """Minimal hand-written orderbook payload with mixed offer types."""
    return {
        "timestamp": "2026-04-28T06:35:12+00:00",
        "offers": [
            {
                "counterparty": "BondedRelMaker",
                "oid": 0,
                "ordertype": "sw0reloffer",
                "minsize": 100_000,
                "maxsize": 50_000_000,
                "txfee": 0,
                "cjfee": 0.0001,
            },
            {
                "counterparty": "BondedAbsMaker",
                "oid": 0,
                "ordertype": "sw0absoffer",
                "minsize": 200_000,
                "maxsize": 200_000_000,
                "txfee": 0,
                "cjfee": 250,
            },
            {
                "counterparty": "BondlessNonzero",
                "oid": 0,
                "ordertype": "sw0reloffer",
                "minsize": 100_000,
                "maxsize": 1_000_000,
                "txfee": 0,
                "cjfee": 0.0001,
            },
            {
                "counterparty": "BondlessZeroFee",
                "oid": 0,
                "ordertype": "sw0reloffer",
                "minsize": 100_000,
                "maxsize": 1_000_000,
                "txfee": 0,
                "cjfee": 0.0,
            },
            {
                "counterparty": "BadOrderType",
                "oid": 0,
                "ordertype": "weirdoffer",
                "minsize": 100_000,
                "maxsize": 1_000_000,
                "txfee": 0,
                "cjfee": 0,
            },
        ],
        "fidelitybonds": [
            {
                "counterparty": "BondedRelMaker",
                "bond_value": 1.0e7,
                "locktime": 1788220800,
                "amount": 50_000_000,
            },
            {
                "counterparty": "BondedAbsMaker",
                "bond_value": 5.0e7,
                "locktime": 1888220800,
                "amount": 100_000_000,
            },
        ],
    }


def test_derive_only_keeps_bonded_and_zero_fee_bondless(raw_payload: dict[str, object]) -> None:
    snap = derive_snapshot(raw_payload, source_url="https://example/orderbook.json")
    cps = {c.counterparty for c in snap.counterparties}
    assert cps == {"BondedRelMaker", "BondedAbsMaker", "BondlessZeroFee"}
    assert snap.n_bonded_counterparties == 2
    assert snap.n_zero_fee_bondless == 1


def test_derive_classifies_fee_types(raw_payload: dict[str, object]) -> None:
    snap = derive_snapshot(raw_payload)
    by_cp = {c.counterparty: c for c in snap.counterparties}
    assert by_cp["BondedRelMaker"].primary_fee_type == "relative"
    assert by_cp["BondedAbsMaker"].primary_fee_type == "absolute"
    assert pytest.approx(by_cp["BondedRelMaker"].primary_cjfee) == 0.0001
    assert by_cp["BondedAbsMaker"].primary_cjfee == 250


def test_derive_distributions_are_sorted(raw_payload: dict[str, object]) -> None:
    snap = derive_snapshot(raw_payload)
    assert list(snap.bond_values) == sorted(snap.bond_values)
    assert list(snap.max_sizes_sats) == sorted(snap.max_sizes_sats)
    assert list(snap.relative_cjfees) == sorted(snap.relative_cjfees)
    assert list(snap.absolute_cjfees_sats) == sorted(snap.absolute_cjfees_sats)


def test_round_trip(raw_payload: dict[str, object], tmp_path: Path) -> None:
    snap = derive_snapshot(raw_payload)
    p = tmp_path / "snap.json"
    save_snapshot(snap, p)
    loaded = load_snapshot(p)
    assert loaded.fetched_at == snap.fetched_at
    assert loaded.n_bonded_counterparties == snap.n_bonded_counterparties
    assert {c.counterparty for c in loaded.counterparties} == {
        c.counterparty for c in snap.counterparties
    }


def test_snapshot_to_dict_is_json_serialisable(raw_payload: dict[str, object]) -> None:
    snap = derive_snapshot(raw_payload)
    json.dumps(snapshot_to_dict(snap))  # must not raise


def test_offer_dataclass_is_frozen() -> None:
    o = Offer(
        oid=0,
        ordertype="sw0reloffer",
        fee_type="relative",
        cjfee=0.0001,
        minsize_sats=1,
        maxsize_sats=2,
        txfee_sats=0,
    )
    with pytest.raises((AttributeError, TypeError, dataclasses.FrozenInstanceError)):
        o.cjfee = 0.0  # type: ignore[misc]


def test_snapshot_from_dict_handles_missing_distributions() -> None:
    s = snapshot_from_dict(
        {
            "fetched_at": 1,
            "source_url": "x",
            "n_total_offers": 0,
            "n_bonded_counterparties": 0,
            "n_zero_fee_bondless": 0,
            "counterparties": [],
        },
    )
    assert isinstance(s, OrderbookSnapshot)
    assert s.bond_values == ()


@pytest.mark.skipif(not SNAPSHOT_PATH.exists(), reason="snapshot not yet produced")
def test_persisted_snapshot_loads() -> None:
    snap = load_snapshot(SNAPSHOT_PATH)
    assert snap.n_bonded_counterparties > 0
    assert all(c.is_bonded or all(o.cjfee == 0 for o in c.offers) for c in snap.counterparties)
    assert all(c.max_size_sats >= c.min_size_sats for c in snap.counterparties)
