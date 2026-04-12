"""Surveillance and probing attack simulation.

Models Chainalysis-style adversaries who:
1. Continuously probe makers via the PoDLE commitment process to learn their UTXOs
2. Monitor the blockchain for CoinJoin transactions
3. Attempt to cluster maker UTXOs and reduce anonymity of past CoinJoins

Key questions:
- Can a surveillance entity cluster each maker's UTXOs? How?
- How much information does UTXO probing provide about past CoinJoins?
- What mitigations exist (PoDLE cost, rate limiting, UTXO rotation)?
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .models import (
    CoinJoinTransaction,
    Role,
    SimulationConfig,
    SurveillanceResult,
)


@dataclass
class MakerState:
    """State of a maker as observed by the surveillance entity."""

    entity_id: str
    maker_id: str
    known_utxos: set[str] = field(default_factory=set)
    # UTXOs confirmed to belong to this maker via probing
    probed_utxos: set[str] = field(default_factory=set)
    # UTXOs seen in CoinJoin inputs alongside probed UTXOs
    inferred_utxos: set[str] = field(default_factory=set)
    # CoinJoins this maker participated in (as observed)
    coinjoins_participated: list[str] = field(default_factory=list)
    # Fidelity bond info (publicly visible)
    fidelity_bond_utxo: str | None = None


@dataclass
class UTXOCluster:
    """A cluster of UTXOs believed to belong to the same entity."""

    cluster_id: str
    utxos: set[str] = field(default_factory=set)
    entity_id: str | None = None  # True entity if known
    confidence: float = 0.0  # 0-1 confidence score


class SurveillanceSimulator:
    """Simulates surveillance/probing attacks against JoinMarket makers.

    Attack Model:
    1. UTXO Probing (Issue #47):
       - Attacker sends !fill requests with PoDLE commitments
       - Maker responds with !ioauth containing their UTXOs
       - Attacker aborts before signing, collecting UTXO sets
       - Cost: 3 PoDLE commitments per UTXO (low cost)

    2. Blockchain Monitoring:
       - Watch for CoinJoin transactions (identifiable by equal outputs)
       - Track which UTXOs appear as inputs
       - Link inputs to previously probed maker UTXOs

    3. Cross-CoinJoin Clustering:
       - Same maker's UTXOs appear in different CoinJoins
       - Change outputs from one CJ become inputs to the next
       - Over time, full UTXO graph of each maker is revealed

    Mitigations:
    - PoDLE commitment cost (3 uses per UTXO)
    - Rate limiting probes (blacklisting nicks)
    - UTXO rotation (mixdepth cycling)
    - Power-of-2 maxsize rounding (prevents balance fingerprinting)
    """

    def __init__(self, config: SimulationConfig, seed: int | None = None) -> None:
        self.config = config
        self.rng = np.random.default_rng(seed)
        self.maker_states: dict[str, MakerState] = {}
        self.utxo_clusters: dict[str, UTXOCluster] = {}
        self.observed_coinjoins: list[CoinJoinTransaction] = []

    def probe_maker(
        self,
        maker_id: str,
        entity_id: str,
        utxos: list[str],
        probe_cost: int = 1,
    ) -> set[str]:
        """Simulate probing a maker to learn their UTXOs.

        In the real protocol, a taker can learn maker UTXOs by starting
        the CoinJoin protocol and aborting after receiving !ioauth.

        Each probe costs one PoDLE commitment (up to 3 per UTXO).

        Args:
            maker_id: The maker's nick/identifier.
            entity_id: True entity controlling this maker.
            utxos: The UTXOs the maker would reveal.
            probe_cost: Number of PoDLE commitments consumed.

        Returns:
            Set of UTXOs learned from this probe.
        """
        if maker_id not in self.maker_states:
            self.maker_states[maker_id] = MakerState(
                entity_id=entity_id,
                maker_id=maker_id,
            )

        state = self.maker_states[maker_id]
        learned = set(utxos)
        state.probed_utxos.update(learned)
        state.known_utxos.update(learned)

        # Add to cluster
        cluster_id = f"cluster_{maker_id}"
        if cluster_id not in self.utxo_clusters:
            self.utxo_clusters[cluster_id] = UTXOCluster(
                cluster_id=cluster_id,
                entity_id=entity_id,
            )
        self.utxo_clusters[cluster_id].utxos.update(learned)
        self.utxo_clusters[cluster_id].confidence = 1.0  # Direct observation

        return learned

    def observe_coinjoin(self, tx: CoinJoinTransaction) -> dict[str, set[str]]:
        """Observe a CoinJoin transaction on the blockchain.

        The observer can see:
        - All inputs (but not which belong to which participant)
        - All outputs (equal and change, but not ownership)
        - The equal output amount

        With prior probe data, the observer can:
        - Match inputs to previously probed maker UTXOs
        - Infer which change outputs belong to which maker
        - Narrow down which equal output belongs to the taker

        Returns:
            Mapping from maker_id to set of newly matched UTXOs.
        """
        self.observed_coinjoins.append(tx)
        all_input_outpoints = {u.outpoint for p in tx.participants for u in p.utxos_in}

        new_matches: dict[str, set[str]] = {}

        for maker_id, state in self.maker_states.items():
            # Check if any probed UTXOs appear as inputs
            matched = state.probed_utxos & all_input_outpoints
            if matched:
                state.coinjoins_participated.append(tx.tx_id)

                # If we can identify which input belongs to this maker,
                # we can also identify their change output via subset-sum
                for p in tx.participants:
                    p_inputs = {u.outpoint for u in p.utxos_in}
                    if p_inputs & matched:
                        # Found this maker's inputs
                        if p.change_output:
                            state.inferred_utxos.add(p.change_output.outpoint)
                            state.known_utxos.add(p.change_output.outpoint)

                            cluster_id = f"cluster_{maker_id}"
                            if cluster_id in self.utxo_clusters:
                                self.utxo_clusters[cluster_id].utxos.add(p.change_output.outpoint)
                        if p.equal_output:
                            state.known_utxos.add(p.equal_output.outpoint)
                            cluster_id = f"cluster_{maker_id}"
                            if cluster_id in self.utxo_clusters:
                                self.utxo_clusters[cluster_id].utxos.add(p.equal_output.outpoint)

                new_matches[maker_id] = matched

        return new_matches

    def compute_anonymity_reduction(self, tx: CoinJoinTransaction) -> dict[str, float]:
        """Compute how much the surveillance reduces anonymity for each output.

        For each equal output in the transaction, if the observer knows which
        makers participated (via probing), the anonymity set is reduced by
        removing those known makers from the set of possible taker candidates.

        Returns:
            Mapping from outpoint to effective anonymity set size.
        """
        n_equal = len(tx.equal_outputs)
        all_input_outpoints = {u.outpoint for p in tx.participants for u in p.utxos_in}

        # Count how many participants the observer can identify
        identified_participants: set[str] = set()
        for maker_id, state in self.maker_states.items():
            matched = state.probed_utxos & all_input_outpoints
            if matched:
                identified_participants.add(maker_id)

        n_identified = len(identified_participants)
        # The observer knows these are makers, not takers.
        # So the anonymity set for the taker's equal output is reduced.
        # From the observer's perspective, the taker is one of the
        # (n_equal - n_identified) unidentified participants.
        effective_anon = max(1, n_equal - n_identified)

        results: dict[str, float] = {}
        for p in tx.participants:
            if p.equal_output:
                if p.participant_id in identified_participants:
                    # Observer knows this is a maker
                    results[p.equal_output.outpoint] = 1.0
                else:
                    results[p.equal_output.outpoint] = float(effective_anon)

        return results

    def simulate_continuous_probing(
        self,
        coinjoins: list[CoinJoinTransaction],
        probe_fraction: float = 0.8,
    ) -> SurveillanceResult:
        """Simulate continuous probing of the orderbook.

        The attacker probes a fraction of makers before each CoinJoin
        and observes all CoinJoins on-chain.

        Args:
            coinjoins: Sequence of CoinJoin transactions to observe.
            probe_fraction: Fraction of makers probed before each CJ.

        Returns:
            SurveillanceResult with clustering and anonymity metrics.
        """
        all_makers: set[str] = set()
        total_anon_reduction = 0.0
        coinjoins_with_reduction = 0

        for tx in coinjoins:
            # Probe makers before this CoinJoin
            for p in tx.participants:
                if p.role == Role.MAKER:
                    all_makers.add(p.participant_id)
                    if self.rng.random() < probe_fraction:
                        utxos = [u.outpoint for u in p.utxos_in]
                        self.probe_maker(p.participant_id, p.entity_id, utxos)

            # Observe the CoinJoin
            self.observe_coinjoin(tx)

            # Compute anonymity reduction
            reductions = self.compute_anonymity_reduction(tx)
            n_equal = len(tx.equal_outputs)
            for _outpoint, eff_anon in reductions.items():
                if eff_anon < n_equal:
                    total_anon_reduction += (n_equal - eff_anon) / n_equal
                    coinjoins_with_reduction += 1

        # Compute final clustering statistics
        total_utxos = sum(len(s.known_utxos) for s in self.maker_states.values())
        cluster_sizes = [len(c.utxos) for c in self.utxo_clusters.values()]

        n_total_outputs = sum(len(tx.all_outputs) for tx in coinjoins)
        avg_reduction = total_anon_reduction / n_total_outputs if n_total_outputs > 0 else 0.0

        return SurveillanceResult(
            n_probes=sum(len(s.probed_utxos) for s in self.maker_states.values()),
            n_coinjoins_observed=len(coinjoins),
            n_makers_clustered=len(self.maker_states),
            n_utxos_clustered=total_utxos,
            cluster_sizes=sorted(cluster_sizes, reverse=True),
            coinjoins_deanonymized=coinjoins_with_reduction,
            avg_anon_set_reduction=avg_reduction,
        )

    def simulate_with_mitigations(
        self,
        coinjoins: list[CoinJoinTransaction],
        podle_cost_per_probe: int = 3,
        max_probes_per_maker: int = 10,
        utxo_rotation_interval: int = 5,
    ) -> SurveillanceResult:
        """Simulate probing attack with privacy mitigations active.

        Mitigations modeled:
        1. PoDLE cost: Each probe costs PoDLE commitments (limits probing)
        2. Probe rate limiting: Max probes per maker
        3. UTXO rotation: Makers rotate UTXOs between mixdepths

        Args:
            coinjoins: CoinJoin transactions to observe.
            podle_cost_per_probe: PoDLE commitments consumed per probe.
            max_probes_per_maker: Max allowed probes before blacklisting.
            utxo_rotation_interval: CoinJoins between UTXO rotations.
        """
        probe_counts: dict[str, int] = {}
        total_anon_reduction = 0.0
        coinjoins_with_reduction = 0

        for cj_idx, tx in enumerate(coinjoins):
            for p in tx.participants:
                if p.role != Role.MAKER:
                    continue

                probe_counts.setdefault(p.participant_id, 0)

                # Rate limiting: can only probe each maker limited times
                if probe_counts[p.participant_id] >= max_probes_per_maker:
                    continue

                # UTXO rotation: maker changes UTXOs periodically
                if cj_idx % utxo_rotation_interval == 0 and p.participant_id in self.maker_states:
                    # Maker rotated UTXOs - old probed data is stale
                    self.maker_states[p.participant_id].probed_utxos.clear()

                # Probe with limited success
                utxos = [u.outpoint for u in p.utxos_in]
                self.probe_maker(p.participant_id, p.entity_id, utxos)
                probe_counts[p.participant_id] += podle_cost_per_probe

            # Observe CoinJoin
            self.observe_coinjoin(tx)

            # Compute reduction
            reductions = self.compute_anonymity_reduction(tx)
            n_equal = len(tx.equal_outputs)
            for _outpoint, eff_anon in reductions.items():
                if eff_anon < n_equal:
                    total_anon_reduction += (n_equal - eff_anon) / n_equal
                    coinjoins_with_reduction += 1

        total_utxos = sum(len(s.known_utxos) for s in self.maker_states.values())
        cluster_sizes = [len(c.utxos) for c in self.utxo_clusters.values()]
        n_total = sum(len(tx.all_outputs) for tx in coinjoins)
        avg_reduction = total_anon_reduction / n_total if n_total > 0 else 0.0

        return SurveillanceResult(
            n_probes=sum(probe_counts.values()),
            n_coinjoins_observed=len(coinjoins),
            n_makers_clustered=len(self.maker_states),
            n_utxos_clustered=total_utxos,
            cluster_sizes=sorted(cluster_sizes, reverse=True),
            coinjoins_deanonymized=coinjoins_with_reduction,
            avg_anon_set_reduction=avg_reduction,
            mitigated_utxos_clustered=total_utxos,
            mitigated_anon_set_reduction=avg_reduction,
        )
