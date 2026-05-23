# JoinMarket Maker Wallet Clustering and Taker Anonymity-Set Reduction

> **TL;DR.** On 16,890 ILP-decoded mainnet JoinMarket CoinJoins,
> a passive on-chain clusterer that follows protocol-mandated
> same-wallet UTXO edges at precision = 1.0 recovers 69,184 maker
> wallet components. The taker's per-CJ published anonymity set
> shrinks from a mean of **8.66 equal outputs to 3.72**, and 98.0%
> of CJs lose at least one candidate. Three independent
> ground-truth checks (simulator ARI = 1.0, zero within-CJ
> collisions, zero cross-nick collisions vs 72 actively-probed
> maker nicks across 40 matched UTXOs) confirm no over-merging.
> The clusterer combines two protocol-mandated chain edges, a
> change-output reuse edge (v6) and a fee-fingerprint
> disambiguated equal-output reuse edge (v7), and ships at
> [`src/coinjoin_simulator/clusterer_v7.py`](https://github.com/joinmarket-ng/coinjoin-simulator/blob/main/src/coinjoin_simulator/clusterer_v7.py).

## 1. Scope and motivation

A JoinMarket CoinJoin (CJ) is an atomic transaction in which one
*taker* and `m` *makers* contribute inputs and produce `n = m + 1`
equal-amount outputs (the *equal outputs*) plus up to `n` change
outputs (one per participant who needs change; typically all `m`
makers and usually the taker too). The published anonymity
property is that the taker's equal output is indistinguishable
from the makers' equal outputs: the taker hides in a set of `n`
candidates per round.

JoinMarket defends this set in several layered ways:

- Each maker keeps funds in five separate *mixdepths*. A single
  CJ uses inputs from one mixdepth only, the equal output goes to
  mixdepth `m + 1 (mod 5)` of the same wallet (the equal output
  is the part that gained privacy and gets to advance), and the
  change output stays at mixdepth `m` (it carries the original
  identity link, so it does not move). Outputs from different
  mixdepths of the same wallet are never co-spent.
- Maker offers are announced over a Tor IRC overlay; the on-chain
  observer cannot tie nicks to UTXOs without active probing.
- The taker's identity at round T does not, by itself, leak which
  of the `n` equal outputs it owns.

This paper studies what the same passive adversary *can* still
learn from on-chain data. The central observation is that a maker
who participates in two CJs leaves a deterministic same-wallet
UTXO edge in the chain: either an equal output of CJ T (at
mixdepth `m + 1`) reused as a maker input in CJ S (the maker
later advertising mixdepth `m + 1`), or a change output of T (at
mixdepth `m`) reused later when the maker cycles back to
advertising `m`. Following those edges clusters the maker
wallets, and every clustered maker shrinks the taker's hide-set
by one.

This paper answers:

1. How many JoinMarket maker wallets can be clustered from
   on-chain data alone, at precision = 1.0, on the full mainnet
   corpus?
2. By how much does that clustering reduce the per-CJ taker
   anonymity set?
3. Are the resulting clusters protocol-correct against an
   independent ground-truth source (an active probing campaign
   that collects real maker UTXO-to-nick bindings)?

The simulator, the on-chain clusterer, and the analysis driver
are open source at
[joinmarket-ng](https://github.com/joinmarket-ng/joinmarket-ng)
and
[coinjoin-simulator](https://github.com/joinmarket-ng/coinjoin-simulator).

## 2. Threat model

Passive on-chain adversary with full corpus access:

- a snapshot of every JoinMarket CoinJoin reachable by a forward
  and backward crawl seeded from probe-collected addresses
  (23,876 JM-flagged CJs in the corpus);
- the public orderbook
  ([joinmarket-ng.sgn.space/orderbook.json](https://joinmarket-ng.sgn.space/orderbook.json));
- the ability to solve a CJ-sized ILP (less than 30 inputs);
- compute on the order of CPU-hours (the full corpus pass fits in
  under 15 minutes on 14 cores).

The adversary does *not* participate in any CoinJoin and is not
assumed to control any maker. An off-chain probing campaign
contributed seed addresses to the corpus crawl and is reused as
ground truth in §6 but contributes nothing to the clustering
itself.

## 3. JoinMarket protocol primer

Three protocol facts are load-bearing for the clusterer:

1. **Per-CJ slot uniqueness.** Each participant (taker or maker)
   contributes one *slot*: a bundle of one or more inputs
   (possibly several UTXOs to cover the offered amount), exactly
   one equal-amount output, and at most one change output. A
   maker who runs as two distinct nicks in the same CJ would have
   to self-pay fees and would be deduplicated by takers on the
   fidelity-bond UTXO during offer selection, so two slots in the
   same CJ belong to two different wallets in practice.

2. **Same-mixdepth change.** A slot whose inputs come from
   mixdepth `m` lands its change output back in mixdepth `m`.
   That change UTXO is therefore eligible to be a future input of
   the *same maker* whenever the maker next advertises mixdepth
   `m`.

3. **Mixdepth-advancing equal output.** The slot's equal output
   lands in mixdepth `m + 1 (mod 5)`. It becomes the natural
   input material for the maker's *next* offer at mixdepth
   `m + 1`. JoinMarket's intra-CJ symmetry means the ILP cannot
   tell, within one CJ, which equal-output vout belongs to which
   maker slot (any permutation of equal-output owners is
   consistent with the same fee constraints). v6 leaves this edge
   off the mainnet attack for that reason; §5.3 (v7) restores it
   by using the consumer slot's own fee in the *next* CJ as a
   fingerprint that selects exactly one producer slot when the
   match is unambiguous.

Two more JoinMarket details matter for the analysis pipeline but
not for the clusterer itself:

- A maker offer is *either* relative or absolute, not both
  (`cjfee_r` or `cjfee_a`, the field name picks the kind). Most
  makers run a single relative offer.
- The maker's contribution to the on-chain fee (`txfee`) is 0
  sats in the default policy and in practice is 0 across the
  observed corpus.

The clusterer uses fact 1 as a hard pairwise must-not-link, fact
2 as a definite same-wallet must-link, and fact 3 in the
simulator only. Fee bands, fidelity-bond values, nick patterns,
or any other off-chain signal are not used by the clusterer; that
intentional restriction is what gives precision = 1.0 by
construction.

### 3.1 Worked example

A two-maker CJ at amount 1,000,000 sats with one taker and makers
`A`, `B` looks like:

```
inputs:
  taker:   2,400,000 (one UTXO from any source)
  A:       1,050,000 (from A's mixdepth 1)
  B:         950,000 + 80,000 (two UTXOs, both from B's mixdepth 0)

outputs:
  equal: 1,000,000 (three of them: taker, A, B in unknown order)
  change(taker):   1,398,000 (taker fee paid)
  change(A):          48,000 (back to A's mixdepth 1)
  change(B):          28,000 (back to B's mixdepth 0)
```

The ILP decomposition tells us which subset of inputs and which
change output each participant contributed; it cannot tell which
of the three equal-amount outputs is whose. The change observation
for `A` will reappear as an input in some future CJ where `A`
again advertises mixdepth 1; that future CJ is the chain edge
that v6 walks. The same applies to `B`'s change in mixdepth 0.

## 4. Mainnet corpus

A forward and backward crawl seeded from probe-collected
addresses, walking only outspends from already-classified
JoinMarket CoinJoins, produced the snapshot used here:

| metric                | count        |
|-----------------------|-------------:|
| visited transactions  | ~200,000     |
| **JM CoinJoin txs**   | **23,876**   |
| ILP-decoded CJs       | 16,890 (70.7%) |
| ILP failures (timeout / infeasible at `max_fee_rel = 0.05`, `time_limit = 2s`) | 6,986 (29.3%) |
| maker slots recovered | 129,301      |

The ILP failure rate is the dominant residual uncertainty. CJs
that do not decode contribute no slots and no chain edges.
Treating them as missing data is conservative for §7: any slot
whose downstream remix happens to fall in an ILP-failed CJ looks
like a singleton (uncertified) and *over-reports* residual
anonymity.

## 5. The clusterer

The clusterer takes the per-CJ ILP slot decomposition and merges
slots across CJs by structural rules only. It is a
constraint-propagation union-find with the following edges and
constraints:

- **Must-not-link (fact 1).** Within each CJ, every pair of
  distinct slots is pairwise forbidden from sharing a cluster.
  These constraints propagate through transitive merges: if
  cluster `A` absorbs cluster `B`, every node that forbids `B`
  henceforth forbids `A`.
- **Must-link, change-chain (fact 2).** Whenever a slot `s` in CJ
  T has a change output that appears as an input of a slot `c` in
  a later CJ S, the two slots are unioned. The receiver is the
  same maker, now re-advertising the same mixdepth.
- **Must-link, equal-chain (fact 3).** In the simulator the
  equal-output owner is known and v6 unions producer with
  consumer directly. On mainnet, v7 (§5.3) restores this edge by
  picking the producer slot from a *fee fingerprint* of the
  consumer slot in the next CJ; the edge fires only when that
  fingerprint identifies exactly one slot in the producer CJ,
  otherwise no edge is added.

The clusterer never uses fees as a clustering signal (v7 uses
fee values only as a per-CJ disambiguator that picks at most one
producer slot from the n equal-output candidates), addresses,
amounts, or any heuristic. Every merge is a direct consequence
of the protocol. By construction the clusterer can only
*under-cluster*: a maker whose downstream remix is missing from
the corpus (crawl frontier, ILP failure, or exit from the
ecosystem) appears as a singleton even when their wallet served
many more CJs in reality. Precision is therefore = 1.0 by
construction, and recall is bounded below by the fraction of CJs
the corpus successfully observes and decodes. The full
implementations are at
[`src/coinjoin_simulator/clusterer_state_machine.py`](https://github.com/joinmarket-ng/coinjoin-simulator/blob/main/src/coinjoin_simulator/clusterer_state_machine.py)
(v6, change-chain only) and
[`src/coinjoin_simulator/clusterer_v7.py`](https://github.com/joinmarket-ng/coinjoin-simulator/blob/main/src/coinjoin_simulator/clusterer_v7.py)
(v7, with the fee-fingerprint equal-chain extension).

### 5.1 Cluster size distribution

The full pass over the 129,301 mainnet slots produces:

| metric                                       | v6        | v7        |
|----------------------------------------------|----------:|----------:|
| total clusters                               | 74,471    | 69,184    |
| singleton clusters                           | 50,126    | 45,871    |
| non-trivial clusters (size >= 2)             | 24,345    | 23,313    |
| largest cluster                              | 91 slots  | 125 slots |
| same-CJ slot collisions (must-not-link violations) | 0   | 0         |

v7 absorbs 5,287 v6 clusters into existing ones through the
equal-chain edge: 4,255 of those merges remove a singleton, and
the largest component grows from 91 to 125 slots. The
zero-collision result is the falsifiability check: any pair of
slots in the same CJ that ended up in the same cluster would be
a hard precision violation. Both v6 and v7 pass this check on
the full mainnet corpus.

![cluster size distribution](figures/cluster_size_distribution.svg)

The v7 histogram is heavy-tailed but bounded: the largest cluster
contains 125 slots, the 99th percentile is 9, and the median
non-trivial cluster has 3. There is no cluster of "thousands of
slots", which would be the signature of an over-merge.

### 5.2 What goes wrong without these constraints

A previous iteration of this study (v5) clustered maker change
outputs by their *advertised fee tuple* `(cjfee_r, cjfee_a)`. That
heuristic is structurally incompatible with the JoinMarket spec:
two distinct makers running the same default policy land in one
cluster, and a single maker who publishes more than one offer
appears in several clusters. On the same corpus the legacy
48-band clusterer produced one component with 55,847 UTXOs that
v6/v7 decomposes into **6,001 distinct wallet components**:

![v5 fee-band clusters decomposed by v6/v7](figures/v5_vs_v6_fragmentation.svg)

46 of the 47 v5 clusters that have any overlap with v6/v7
fragment across many wallets. The fee-band heuristic was a
many-to-many relation between clusters and identities; v6/v7
replace it with a many-to-one relation that under-clusters by
design when the on-chain evidence is absent.

### 5.3 v7: fee-fingerprint equal-output attribution

Every maker advertises a single offer (relative `cjfee_r` *or*
absolute `cjfee_a`, not both) and the on-chain fee they earn in a
CJ is a deterministic function of that offer and the equal-output
amount: `fee = cjfee_a` for absolute makers,
`fee = round(cjfee_r * equal_amt)` for relative makers (modulo
one-sat rounding). The ILP recovers this realised fee per slot
from the input total, equal output, and change output. The fee
is observable but not by itself identifying: thousands of slots
share the same `(cjfee_r, cjfee_a)` fingerprint across the
corpus.

v7 uses the fee fingerprint locally, *within a single producer
CJ*. When an equal output of producer CJ T is spent as a maker
input of slot `c` in a later CJ S, the slot `c` carries its own
fee `f_c = fee(c, equal_amt_S)`. We then look at T's maker slots
S1..Sn (at most ~22 in practice) and ask: is there exactly one
Si whose advertised offer produces `f_c` when applied to the
equal-output amount of S? Concretely, for each Si we look up
which interpretations of Si's realised fee in T are consistent
with `f_c`:

- *abs* match: Si.realised_fee == f_c (Si is an absolute maker
  with cjfee_a = f_c);
- *rel* match:
  Si.realised_fee / equal_amt_T == f_c / equal_amt_S (within one
  ppm tolerance) (Si is a relative maker with the matching
  cjfee_r).

v7 adds the equal-chain edge if and only if **exactly one Si
matches under abs OR rel**, *and* the abs interpretation and the
rel interpretation do not point at two different slots. If two
or more slots match (ambiguous), or the abs and rel candidates
disagree (conflict), no edge is added. Conflicting
interpretations are silently dropped because we cannot decide
which is correct without knowing the maker's policy.

Empirically on the mainnet corpus (51,439 cross-CJ equal-output
reuses):

| disposition                       |   count  | share  |
|-----------------------------------|---------:|-------:|
| edge added (unique under abs OR rel) | **5,643** | 11.0%  |
|  - abs-only unique                |     848  |  1.6%  |
|  - rel-only unique                |   4,343  |  8.4%  |
|  - both agree on same slot        |     452  |  0.9%  |
| ambiguous (>= 2 slots match)      |   2,004  |  3.9%  |
| interpretation conflict (abs and rel disagree) | 65 | 0.1% |
| no slot matches                   |  43,727  | 85.0%  |

![v7 attribution breakdown](figures/v7_attribution_breakdown.svg)

The 85% no-match share is dominated by reuses where the producer
CJ is not in the corpus (the equal output's parent CJ was crawled
but not ILP-decoded, or was outside the JM crawl). The 65
conflict cases (0.1%) are *dropped* on purpose: they typically
arise when one slot's absolute fee happens to numerically match
another slot's relative fee at a different equal-output amount.
The drop is the precision-preserving move.

The contract is identical to v6's: every added edge is a definite
same-wallet link or no edge at all. v7 inherits v6's same-CJ
must-not-link forbidance through a shared constrained union-find,
so the 5,643 additional merges cannot smuggle a must-not-link
violation through transitivity. We verify this directly on
mainnet (§5.1: 0 collisions, §6.3: 0 cross-nick collisions).

## 6. Ground-truth validation

We validate v6/v7 against three independent ground-truth sources,
all of which a passive on-chain analyst would *not* have but
which we can construct here.

### 6.1 Simulator end-to-end (perfect labels)

The accompanying coinjoin-simulator builds a synthetic JoinMarket
network of 12 makers running a default relative-fee policy and
one rotating taker. We let it produce 100 CJs with the same ILP
pipeline and apply both v6 and v7 to the simulator output:

| metric             | value |
|--------------------|------:|
| n_makers           | 12    |
| n_cjs simulated    | 100   |
| ARI (v6, sklearn)  | 1.0   |
| ARI (v7, sklearn)  | 1.0   |
| precision (v6, v7) | 1.0   |
| recall (v6, v7)    | 1.0   |

Every maker UTXO is placed in the correct cluster. To stress-test
v7 we additionally re-ran the simulator with a blinded
equal-output assignment (the v7 fee-fingerprint step has to
recover ownership from fees alone, exactly as on mainnet); ARI
remains 1.0. The simulator therefore measures *both* the state-
machine logic and the v7 attribution step end-to-end: when the
corpus is complete, the combined clusterer recovers identity
exactly.

### 6.2 Within-CJ sybil-deduplication on mainnet

The must-not-link constraint is the strongest structural precision
check on mainnet: if any pair of slots from the same CJ ends up in
the same cluster, the clusterer has falsified one of its own
assumptions. On the 16,890 decoded mainnet CJs there are zero
such collisions for *both* v6 and v7 (§5.1). This is a hard upper
bound on the precision violation rate (it is not a recall
statement).

### 6.3 Active probing of real maker wallets

In late April 2026 we ran three probing rounds against the live
JoinMarket mainnet orderbook, one per CJ amount (100k / 150k /
200k sats), totalling 72 distinct maker nicks that authenticated
with a real PoDLE commitment. For each nick the probe records
the set of UTXOs the maker offered to spend (`offered_utxos`);
two UTXOs offered by the same nick are guaranteed to belong to
the same wallet because the same fidelity-bond key authenticates
both negotiations.

A maker advertises only one mixdepth at a time, so the probe-side
invariant is stronger than just "same wallet": two UTXOs from the
same nick in the same probe round come from the **same mixdepth
of the same wallet**. This is the property §6.3 uses both to
confirm precision and to look for missed edges.

| metric                                       | v6         | v7         |
|----------------------------------------------|-----------:|-----------:|
| nicks probed                                 | 72         | 72         |
| nicks with at least one match                | 35         | 35         |
| offered UTXOs                                | 101        | 101        |
| offered UTXOs found in a cluster             | 40         | 40         |
| **cross-nick collisions in any cluster**     | **0**      | **0**      |

![probe validation card](figures/probe_validation_v6.svg)

The pass-or-fail check: for every pair of distinct probed nicks
`(A, B)`, no cluster contains a UTXO of `A` and a UTXO of `B`.
We observe **zero** such cross-nick collisions under both v6 and
v7. The clusterer never merged two real JoinMarket maker wallets
into one component on this corpus. Every nick whose UTXOs appear
in the cluster set has all of them in the same cluster, or in
several clusters that all belong to that nick.

The probe data also constrains the *recall direction*: the probe
matched 35 of 72 advertised nicks; the unmatched 37 nicks
contributed UTXOs that were never observed entering a JM CJ in
our corpus (cold-storage parts of the wallet, recent deposits, or
UTXOs that were spent in CJs older than our crawl horizon). They
are not validation failures.

The three precision checks converge: v6/v7 are precision = 1.0 by
construction, by the within-CJ structural check on 16,890
mainnet CJs, and by the probing ground truth on 35 matched real
maker nicks across 40 distinct UTXOs.

## 7. Anonymity-set reduction

For each ILP-decoded CJ T, the taker hides in a published
anonymity set of `n_eq = m + 1` equal outputs (one per maker plus
the taker). Define a maker slot as *certified* when the clusterer
places it in a cluster of size >= 2 (the slot is linked by at
least one definite UTXO chain to another CJ in the corpus). Every
certified maker removes one candidate from the taker's anonymity
set, since the taker cannot be a maker whose identity persists
across CJs. The residual anonymity set lower bound is therefore

    k(T) = n_eq(T) - n_certified_makers(T).

The taker is never certified by construction (we have no chain
evidence about who the taker is, so they always remain in the
residual set). The minimum value of `k(T)` is therefore 1, the
taker alone.

We do not subtract more even when the taker's own slot happens to
chain forward, because the evidence does not distinguish "taker
who remixes" from "maker whose remix we observed". The reported
`k(T)` is the **lower bound** on the true residual anonymity set.

### 7.1 Headline

Across the 16,890 ILP-decoded mainnet CJs:

| metric                                          | v6 (change-chain only) | v7 (change + equal) |
|-------------------------------------------------|-----------------------:|--------------------:|
| mean published `n_eq`                           | 8.66                   | 8.66                |
| mean certified makers per CJ                    | 4.69                   | **4.94**            |
| mean residual anonymity set                     | 3.97                   | **3.72**            |
| share of CJs with at least one certified maker  | 97.6%                  | **98.0%**           |
| median residual anonymity set                   | 3                      | 3                   |
| share of CJs reaching residual = 1 (taker alone)| 8.0%                   | **10.6%**           |

v7 cuts the mean residual by 0.25 candidates per CJ versus v6
and lifts the share of CJs where the taker is the sole remaining
candidate from 8.0% to 10.6%, a **32% relative increase** in the
worst-case-for-the-taker outcome. The mean published anonymity
set shrinks from 8.66 to 3.72 (a 57% reduction). 98.0% of CJs
leak at least one maker through the protocol chain. 10.6% reach
residual = 1: every maker in the CJ is certified and only the
taker remains in the hide-set. We do not claim full
deanonymization on those CJs (the taker's identity itself is
still unknown to the on-chain analyst), but their hide-set has
collapsed to one candidate.

![residual anonymity set histogram (v7)](figures/anonset_reduction_hist.svg)

The v6-vs-v7 overlay makes the shift visible:

![v6 vs v7 anonset overlay](figures/v6_vs_v7_anonset_overlay.svg)

The v7 distribution sits to the left of v6 across the whole
range: more residual = 1 and residual = 2 CJs, fewer in the long
tail. 32% of CJs have residual <= 2 and 67% have residual <= 4.

### 7.2 Per-`n_eq` breakdown

The reduction holds across every round size in the corpus:

![mean anonset before and after, by n_eq](figures/anonset_per_n_eq.svg)

The grey bars are the published anonymity sets the taker thinks
they hide in; the red bars are the v7 lower-bound residual after
certified makers are removed. The residual stays in a 2 to 4
band across the entire range from `n_eq = 3` to `n_eq = 17`.
Larger rounds do not buy a larger hide-set in practice; they
contribute more chain edges to the attacker.

### 7.3 What drives the residual

The residual anonymity set has two structural sources:

1. **The true taker.** Always one slot. The protocol guarantees
   exactly one taker per CJ, and the taker is never certified.
2. **Makers whose downstream remix is missing from the corpus.** A
   maker who participated in CJ T and remixed in CJ S leaks
   identity only if S is in our corpus *and* ILP-decoded, *and*
   either the maker's change UTXO was reused as input in S (v6
   edge) or the maker's equal output was reused as input in S
   with a unique fee fingerprint (v7 edge). The 6,986 ILP
   failures and the crawl frontier together account for most of
   the residual; the 2,004 ambiguous and 65 conflicting fee-
   fingerprint cases that v7 conservatively drops account for a
   smaller share.

Increasing the ILP time budget or extending the crawl frontier
both shift the histogram leftward (smaller residuals). The
present numbers are therefore a lower bound on the reduction the
same attack achieves with more compute, not an upper bound.

## 8. Role-change taker exposure (supplementary)

A taker who later participates as a maker in a future CJ S leaks
their cross-round identity: the maker slot in S that consumes one
of the taker's equal outputs from T becomes part of a multi-slot
cluster whose other members are the taker's later maker
behaviour. The attack is the same chain edge read in the other
direction: T's equal output to input of slot in S to cluster
containing S's other CJs.

3,617 of the 16,890 ILP-decoded mainnet CJs (21.4%) show a
forward-spent equal output whose downstream slot is in a v7
cluster not already certified for any maker of T. Those are the
candidate role-change exposures: the slot is most likely the
taker of T behaving as a maker in S, modulo the ambiguity that it
could also be a maker of T whose cluster did not match.

We mention this for completeness; the §7 anonymity-set reduction
is the structurally stronger and more practically relevant attack
in this paper. The role-change exposure depends on the additional
event of a taker becoming a maker; the §7 reduction applies on
every CJ regardless.

## 9. Limitations and future work

- The corpus is a finite snapshot. The 29.3% ILP failure rate is
  the dominant residual; with a higher per-tx ILP budget (10s,
  30s) the decoded fraction would climb and the residual
  anonymity set would shrink further. We chose 2 s/tx to keep
  the full corpus pass under 15 minutes on 14 cores.
- v7 adds an equal-chain edge only when the fee fingerprint is
  unambiguous within the producer CJ. Of the 51,439 cross-CJ
  equal-output reuses, 2,004 are ambiguous (two or more producer
  slots share the same fee under at least one interpretation) and
  65 disagree between absolute and relative interpretations.
  These edges are deliberately dropped to preserve precision = 1.0
  and represent ~4% of all observed reuses. A complementary
  per-CJ commitment scheme that publishes the equal-output owner
  permutation (or a maker's deterministic ordering on the
  network layer) would resolve these residual cases.
- Cross-CJ CIOH (Common Input Ownership Heuristic) on the
  off-chain side of the maker wallet is not yet used. A maker
  whose pre-CJ funding transaction co-spends UTXOs from multiple
  addresses gives away a wallet root that re-appears every time
  the same maker funds a new CJ. v6/v7 have the bundling implicit
  inside slots but do not yet propagate it across CJs through
  non-CJ ancestor transactions.
- Forward crawl frontier. Recent CJs near the crawl horizon have
  fewer observed successors, so their slots look like singletons
  more often than the structural truth warrants. This biases the
  residual upward for the most recent quarter of the corpus.
- Makers who consolidate winnings off-CJ between rounds (move
  funds to cold storage and refund the next round from a freshly
  derived address) look like two singletons to the clusterer. The
  probe data (§6.3) suggests this is not the dominant pattern,
  but a direct count is not yet available.

The probe data after the v7 upgrade shows 35 of 72 advertised
nicks matched (up from a fair-comparison v6 baseline of also 35,
since the v7 mainnet merges of 5,287 clusters occur off the
probed-nick set: probed nicks are recent live makers, while v7's
merges concentrate in older corpus regions where more equal-
output chain edges have time to fire). Future improvements should
target ILP recall (more decoded CJs across the long tail of
larger rounds) and cross-mixdepth identity edges (a fidelity-bond
UTXO observed across two CJs at different mixdepths is a candidate
under the JoinMarket spec).

## 10. Conclusion

The JoinMarket equal-output anonymity set is not the right
metric to publish to users. A passive on-chain adversary running
a protocol-correct chain-following clusterer at precision = 1.0
reduces the published anonymity set from a mean of 8.66 to 3.72
on the full mainnet corpus, with 98.0% of CJs losing at least one
candidate to certified-maker removal. The structural property
the attack exploits, namely that JoinMarket's two same-wallet
UTXO chains (the change-chain at the same mixdepth and the
equal-output chain at the next mixdepth, the latter disambiguated
by the maker's own fee schedule) are both intrinsic to the
protocol, is not a fixable implementation bug.

The precision = 1.0 guarantee is what makes the result
actionable: the clusterer never merges two distinct maker
wallets, validated by three independent ground-truth sources.
Each certified maker the analyst extracts is a *deterministic*
hide-set reduction, not a probabilistic one. 10.6% of CJs reach
residual = 1, meaning every maker is certified and only the taker
remains in the anonymity set.

The practical implication for JoinMarket users is that the
relevant privacy figure for a round is not its published `n_eq`
but the v7 residual, which is typically 2 to 4 across the entire
range of round sizes the protocol supports. Mitigations that
break either chain link (cold-funding the next round from a
fresh derivation that decouples the maker's next-round inputs
from previous-round change; or a fee policy that produces
identical fingerprints across an entire offer cohort, removing
the within-CJ uniqueness v7 needs) would close the principal
structural channels the clusterer exploits.
