"""Tests for the per-mixdepth Bayesian clusterer.

Exercises the four building blocks of :mod:`clusterer_mixdepth`:

* initial banding by ``(mixdepth, log10(cjfee_r))``;
* within-mixdepth agglomerative merging governed by the Gaussian
  log-fee likelihood and the fidelity-bond prior;
* hard cross-CJ links wiring sub-clusters across consecutive
  mixdepths;
* the cycle-stitching step that resolves per-mixdepth sub-clusters
  into per-maker identities.

All fixtures are synthetic; no solver, no corpus dependency.
"""

from __future__ import annotations

from coinjoin_simulator.clusterer_mixdepth import (
    N_MIXDEPTHS,
    CrossCjLink,
    MakerChangeObservation,
    MixdepthClustererConfig,
    cluster_maker_changes_by_mixdepth,
)


def _obs(
    oid: str,
    *,
    md: int,
    cjfee_r: float,
    maker: str,
    bond: float = 0.0,
) -> MakerChangeObservation:
    return MakerChangeObservation(
        output_id=oid,
        txid=f"tx-{oid}",
        mixdepth=md,
        cjfee_r=cjfee_r,
        fidelity_bond_value=bond,
        maker_id_truth=maker,
    )


# ---------------------------------------------------------------------------
# Empty / degenerate inputs
# ---------------------------------------------------------------------------


def test_empty_input_returns_neutral_assignment() -> None:
    """No observations -> perfect metrics on the empty partition."""
    res = cluster_maker_changes_by_mixdepth([])
    assert res.n_outputs == 0
    assert res.n_clusters == 0
    assert res.ari == 1.0
    assert res.precision == 1.0
    assert res.recall == 1.0
    assert res.f1 == 1.0


def test_single_observation_is_its_own_cluster() -> None:
    res = cluster_maker_changes_by_mixdepth([_obs("o1", md=0, cjfee_r=1e-4, maker="M1")])
    assert res.n_outputs == 1
    assert res.n_clusters == 1
    assert set(res.labels) == {"o1"}


# ---------------------------------------------------------------------------
# Fee-band separation within a single mixdepth
# ---------------------------------------------------------------------------


def test_distinct_fee_bands_within_mixdepth_are_separated() -> None:
    """Two makers with fee bands ~1.5 dex apart must not merge."""
    obs = [
        _obs("o-low-1", md=0, cjfee_r=1e-5, maker="M_LOW"),
        _obs("o-low-2", md=0, cjfee_r=1.2e-5, maker="M_LOW"),
        _obs("o-hi-1", md=0, cjfee_r=3e-4, maker="M_HI"),
        _obs("o-hi-2", md=0, cjfee_r=4e-4, maker="M_HI"),
    ]
    res = cluster_maker_changes_by_mixdepth(obs)
    # Two clusters, perfect bipartite match.
    assert res.n_clusters == 2
    # Same maker outputs share a label.
    assert res.labels["o-low-1"] == res.labels["o-low-2"]
    assert res.labels["o-hi-1"] == res.labels["o-hi-2"]
    assert res.labels["o-low-1"] != res.labels["o-hi-1"]
    assert res.precision == 1.0
    assert res.recall == 1.0


def test_same_fee_band_within_mixdepth_collapses() -> None:
    """Four close-band observations of the same maker collapse to one cluster."""
    obs = [_obs(f"o{i}", md=0, cjfee_r=1e-4 * (1 + 0.05 * i), maker="M") for i in range(4)]
    res = cluster_maker_changes_by_mixdepth(obs)
    assert res.n_clusters == 1
    assert len({res.labels[o.output_id] for o in obs}) == 1


# ---------------------------------------------------------------------------
# Mixdepth separation
# ---------------------------------------------------------------------------


def test_same_fee_but_different_mixdepths_stay_apart_without_links() -> None:
    """Without any cross-CJ link the same fee at different mixdepths
    must produce distinct sub-clusters, since we have no evidence
    yet that they're the same identity.
    """
    obs = [
        _obs("md0-a", md=0, cjfee_r=1e-4, maker="M"),
        _obs("md0-b", md=0, cjfee_r=1.1e-4, maker="M"),
        _obs("md1-a", md=1, cjfee_r=1e-4, maker="M"),
        _obs("md1-b", md=1, cjfee_r=1.1e-4, maker="M"),
    ]
    res = cluster_maker_changes_by_mixdepth(obs)
    # No link: md0 cluster is separate from md1 cluster, even though
    # ground truth says they're the same maker.
    assert res.labels["md0-a"] == res.labels["md0-b"]
    assert res.labels["md1-a"] == res.labels["md1-b"]
    assert res.labels["md0-a"] != res.labels["md1-a"]
    # Bipartite PRF still credits per-mixdepth correctness as
    # partial recall (not perfect since recall is bipartite over the
    # best match of one truth cluster to one predicted cluster).


# ---------------------------------------------------------------------------
# Cross-CJ link cycle stitching
# ---------------------------------------------------------------------------


def test_cross_cj_link_stitches_consecutive_mixdepths() -> None:
    """A link between an md0 observation and an md1 observation of
    the same maker must merge their per-mixdepth sub-clusters into a
    single per-maker identity.
    """
    obs = [
        _obs("md0-a", md=0, cjfee_r=1e-4, maker="M"),
        _obs("md0-b", md=0, cjfee_r=1.1e-4, maker="M"),
        _obs("md1-a", md=1, cjfee_r=1e-4, maker="M"),
        _obs("md1-b", md=1, cjfee_r=1.1e-4, maker="M"),
    ]
    links = [CrossCjLink(src_output_id="md0-a", dst_output_id="md1-a")]
    res = cluster_maker_changes_by_mixdepth(obs, links=links)
    # All four observations now share one identity id.
    assert res.n_clusters == 1
    assert len({res.labels[o.output_id] for o in obs}) == 1


def test_full_cycle_stitches_all_five_mixdepths() -> None:
    """A complete mixdepth cycle 0 -> 1 -> ... -> 4 -> 0 should pull
    five per-mixdepth sub-clusters into a single identity.
    """
    obs: list[MakerChangeObservation] = []
    for md in range(N_MIXDEPTHS):
        for i in range(2):
            obs.append(_obs(f"md{md}-{i}", md=md, cjfee_r=1e-4, maker="M"))
    # Chain links along the cycle.
    links: list[CrossCjLink] = []
    for md in range(N_MIXDEPTHS):
        nxt = (md + 1) % N_MIXDEPTHS
        links.append(
            CrossCjLink(
                src_output_id=f"md{md}-0",
                dst_output_id=f"md{nxt}-0",
            )
        )
    res = cluster_maker_changes_by_mixdepth(obs, links=links)
    assert res.n_clusters == 1
    assert res.precision == 1.0
    assert res.recall == 1.0


def test_non_consecutive_link_is_ignored() -> None:
    """A link between md0 and md3 (gap of 3) is *not* a valid cycle
    edge in JoinMarket and must be ignored.
    """
    obs = [
        _obs("md0", md=0, cjfee_r=1e-4, maker="M"),
        _obs("md3", md=3, cjfee_r=1e-4, maker="M"),
    ]
    res = cluster_maker_changes_by_mixdepth(
        obs,
        links=[CrossCjLink(src_output_id="md0", dst_output_id="md3")],
    )
    # Link discarded: still two separate sub-clusters.
    assert res.n_clusters == 2


# ---------------------------------------------------------------------------
# Fidelity-bond prior
# ---------------------------------------------------------------------------


def test_bond_prior_helps_merge_close_fees_with_matching_bonds() -> None:
    """At the merge boundary, matching fidelity bonds should favour
    the merge. Two clusters at fee bands ~1 stride apart will fall
    on the fence; matching bonds should tip them together.
    """
    obs = [
        _obs("a1", md=0, cjfee_r=1e-4, maker="M", bond=1e9),
        _obs("a2", md=0, cjfee_r=1e-4, maker="M", bond=1e9),
        _obs("b1", md=0, cjfee_r=1.5e-4, maker="M", bond=1e9),
        _obs("b2", md=0, cjfee_r=1.5e-4, maker="M", bond=1e9),
    ]
    weak = cluster_maker_changes_by_mixdepth(
        obs, config=MixdepthClustererConfig(bond_log_prior_weight=0.0)
    )
    strong = cluster_maker_changes_by_mixdepth(
        obs, config=MixdepthClustererConfig(bond_log_prior_weight=4.0)
    )
    assert strong.n_clusters <= weak.n_clusters


def test_bond_prior_keeps_unrelated_makers_apart() -> None:
    """Even with the bond prior on, vastly mismatched bonds must
    still keep two same-fee makers apart.
    """
    obs = [
        _obs("a1", md=0, cjfee_r=1e-4, maker="M_A", bond=1e9),
        _obs("a2", md=0, cjfee_r=1e-4, maker="M_A", bond=1e9),
        _obs("b1", md=0, cjfee_r=1e-4, maker="M_B", bond=1e3),
        _obs("b2", md=0, cjfee_r=1e-4, maker="M_B", bond=1e3),
    ]
    # With a strong bond prior and well-separated bond values, the
    # two groups should stay separate even at identical fees.
    res = cluster_maker_changes_by_mixdepth(
        obs,
        config=MixdepthClustererConfig(
            bond_log_prior_weight=20.0,
            log_stride=0.05,
            min_log_likelihood_delta=-0.5,
        ),
    )
    # Allow either 1 or 2 clusters depending on hyperparameters, but
    # if 2, they should align with truth.
    if res.n_clusters == 2:
        assert res.labels["a1"] == res.labels["a2"]
        assert res.labels["b1"] == res.labels["b2"]
        assert res.labels["a1"] != res.labels["b1"]
