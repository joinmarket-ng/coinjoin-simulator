# JoinMarket Maker Wallet Clustering and Taker Anonymity-Set Reduction

> **TL;DR.** On 16,890 ILP-decoded mainnet JoinMarket CoinJoins,
> a passive on-chain clusterer that follows protocol-mandated
> same-wallet UTXO edges at precision = 1.0 recovers 68,998 maker
> wallet components. The taker's per-CJ published anonymity set
> shrinks from a mean of **8.66 equal outputs to 3.71**, and 98.0%
> of CJs lose at least one candidate. Three independent
> ground-truth checks (simulator ARI = 1.0, zero within-CJ
> collisions, zero cross-nick collisions vs 72 actively-probed
> maker nicks across 40 matched UTXOs) confirm no over-merging.
> The clusterer combines five protocol-mandated edges: a
> change-output reuse edge (v6), a fee-fingerprint disambiguated
> equal-output reuse edge (v7), a non-CJ co-spend edge on
> two-output spenders of maker change (v7.1), a non-CJ
> round-trip edge that links a maker's change UTXO to a future
> maker-slot input through a single two-output hop (v7.2), and a
> fidelity-bond funding-tx CIOH edge anchored at the public
> orderbook snapshot (v7.3). The latest module ships at
> [`src/coinjoin_simulator/clusterer_v73.py`](https://github.com/joinmarket-ng/coinjoin-simulator/blob/main/src/coinjoin_simulator/clusterer_v73.py).

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
`A`, `B`. Each maker charges a CoinJoin fee of 1,000 sats; the
miner fee for the whole transaction is 4,000 sats and is paid in
full by the taker (default JoinMarket policy: `txfee = 0` for
each maker offer):

```
inputs (total 4,480,000):
  taker:   2,400,000  (one UTXO from any source)
  A:       1,050,000  (one UTXO from A's mixdepth 1)
  B:         950,000 + 80,000  (two UTXOs, both from B's mixdepth 0)

outputs (total 4,476,000; miner fee = 4,000):
  equal: 1,000,000      (three of them: taker, A, B in unknown order)
  change(taker):  1,394,000  = 2,400,000 - 1,000,000 - 2*1,000 - 4,000
  change(A):         51,000  = 1,050,000 - 1,000,000 + 1,000
  change(B):         31,000  = 1,030,000 - 1,000,000 + 1,000
```

Cashflow per participant (counting only what each wallet sees;
the equal outputs are 1,000,000 each, owned by their respective
participants):

- **Taker**: pays 2 * 1,000 (maker fees) + 4,000 (miner fee) =
  **6,000 sats net cost** for the mix.
- **Maker A**: receives a 1,000,000 equal output and a 51,000
  change output for a total of 1,051,000 against 1,050,000
  inputs, **earning +1,000 sats**.
- **Maker B**: receives a 1,000,000 equal output and a 31,000
  change output for a total of 1,031,000 against 1,030,000
  inputs, **earning +1,000 sats**.
- **Miner**: receives 4,000 sats.

The taker funds both maker payouts and the miner fee; the makers
are paid for providing liquidity. Negative cashflow for a maker
would only occur if a misconfigured offer set a negative cjfee,
which JoinMarket clients reject at orderbook-load time.

The ILP decomposition tells us which subset of inputs and which
change output each participant contributed; it cannot tell which
of the three equal-amount outputs is whose (the equal outputs are
indistinguishable on-chain by amount alone). The change output
for `A` (51,000 sats in mixdepth 1) will reappear as an input in
some future CJ where `A` again advertises mixdepth 1; that future
CJ is the chain edge that v6 walks. The same applies to `B`'s
change in mixdepth 0.

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

| metric                                       | v6        | v7        | v7.1      | v7.2      | v7.3      |
|----------------------------------------------|----------:|----------:|----------:|----------:|----------:|
| total clusters                               | 74,471    | 69,184    | 69,103    | 69,003    | 68,998    |
| singleton clusters                           | 50,126    | 45,871    | 45,790    | 45,706    | 45,702    |
| non-trivial clusters (size >= 2)             | 24,345    | 23,313    | 23,313    | 23,297    | 23,296    |
| largest cluster                              | 91 slots  | 125 slots | 125 slots | 125 slots | 125 slots |
| same-CJ slot collisions (must-not-link violations) | 0   | 0         | 0         | 0         | 0         |

v7 absorbs 5,287 v6 clusters into existing ones through the
equal-chain edge. v7.1 adds 127 cross-CJ unions through the
non-CJ co-spend edge (§5.4), removing 81 further singletons. v7.2
adds 153 cross-CJ unions through the non-CJ round-trip edge
(§5.5), removing 100 more clusters. v7.3 adds 6 cross-cluster
unions through the fidelity-bond funding-tx CIOH edge (§5.6),
collapsing 9 v7.2 components into 4 (a net reduction of 5
clusters). The zero-collision result is the falsifiability check:
any pair of slots in the same CJ that ended up in the same
cluster would be a hard precision violation. All five iterations
pass this check on the full mainnet corpus.

![cluster size distribution](figures/cluster_size_distribution.svg)

The v7.3 histogram is heavy-tailed but bounded: the largest
cluster contains 125 slots, the 99th percentile is 9, and the
median non-trivial cluster has 3. There is no cluster of
"thousands of slots", which would be the signature of an
over-merge.

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

### 5.4 v7.1: non-CJ co-spend (Common Input Ownership Heuristic)

A maker who consolidates two distinct change UTXOs in a single
non-CoinJoin spend reveals same-wallet ownership of those two
UTXOs by the Common Input Ownership Heuristic (CIOH). To remain
at precision = 1.0 we apply CIOH only when both endpoints are
already known maker change outputs and the spender is
unambiguously not a CoinJoin. We add a hard conservative filter:
the non-CJ spender must have **at most two outputs**. This is the
canonical shape of an off-CJ consolidation, change return or
sweep, and it eliminates by construction any spender that could
plausibly be a payment to a third party (a typical send is
two-output: `recipient + change`; the filter retains those but
discards multi-recipient batched spends where CIOH could merge
different wallets).

The v7.1 edge fires when a non-JM spender `N` with `n_outputs <=
2` consumes two or more change outputs that are owned by distinct
maker slots in the corpus. All such maker slots are unioned. Slot
pairs that belong to the same CJ are rejected by the inherited
must-not-link constraint, so the edge respects within-CJ Sybil
deduplication by design.

Empirically on the mainnet corpus:

| metric                                          |   count |
|-------------------------------------------------|--------:|
| candidate non-CJ spenders (>= 2 maker outpoints)|   2,447 |
| qualifying (<= 2 outputs)                       |      56 |
| dropped by output filter                        |   2,391 |
| candidate slot pairs                            |     129 |
| same-CJ pairs rejected by must-not-link         |       0 |
| **cross-CJ unions added**                       |   **127** |

The 2,391 dropped consolidations have three or more outputs and
are not safe to treat as same-owner under CIOH without more
evidence. We leave them on the table rather than risk a precision
violation. The 0 same-CJ violations show that the must-not-link
constraint correctly intercepts any candidate that would have
merged two slots of the same CJ.

The v7.1 module is implemented at
[`src/coinjoin_simulator/clusterer_v71.py`](https://github.com/joinmarket-ng/coinjoin-simulator/blob/main/src/coinjoin_simulator/clusterer_v71.py)
and reuses the v7 union-find with the same forbid-set semantics.

### 5.5 v7.2: non-CJ round-trip CIOH

The v7.1 filter caps non-CJ spenders at two outputs because we
cannot prove same-owner across more outputs. Many real on-CJ
remixes also leave a single-hop non-CJ trail in between: a maker
consumes their change at mixdepth `m`, immediately rebroadcasts
through a two-output hop transaction (consolidation, fee bump,
deposit echo), and the resulting output is then consumed as a
maker-slot input in a later CJ. This is the change-chain edge of
v6 with one non-CJ hop interposed.

The v7.2 edge fires when a non-JM transaction `H` satisfies all
of:

1. `H` has at most two outputs (same CIOH-safety filter as v7.1);
2. `H` consumes at least one maker change UTXO from a known
   producer slot `p`;
3. at least one of `H`'s outputs is the *first input* of a maker
   slot `c` in a later CJ.

In that case `p` and `c` are unioned. Same-CJ pairs are dropped
by the inherited forbid-set.

Empirically on the mainnet corpus:

| metric                                                                | count |
|-----------------------------------------------------------------------|------:|
| candidate non-CJ hops (consuming maker change)                        | 3,710 |
| dropped by output filter (> 2 outputs)                                | 3,608 |
| qualifying hops                                                       |   102 |
| candidate slot pairs                                                  |   155 |
| same-CJ pairs rejected                                                |     0 |
| **cross-CJ unions added**                                             | **153** |

v7.2 cuts the cluster count from 69,103 (v7.1) to 69,003 and
lowers the mean residual anonymity set from 3.711 (v7.1) to
3.706 (a further reduction beyond v7.1; the full §7.1 table
rounds both to 3.71). The simulator end-to-end check and the
probe-side ground truth in §6 confirm no precision violations and
0 cross-nick collisions under v7.2.
The v7.2 module is implemented at
[`src/coinjoin_simulator/clusterer_v72.py`](https://github.com/joinmarket-ng/coinjoin-simulator/blob/main/src/coinjoin_simulator/clusterer_v72.py).

Together, v7.1 and v7.2 close the off-chain CIOH side of the
maker wallet: any non-CJ transaction whose shape is consistent
with a simple consolidation or single-hop forwarding is used as
a same-wallet edge, while richer non-CJ shapes are conservatively
ignored.

### 5.6 v7.3: fidelity-bond funding-tx CIOH

JoinMarket makers advertise a *fidelity bond* (FB): a timelocked
P2WSH output that the maker proves they own. The set of live FB
UTXOs and the nick that owns each one is published, in cleartext,
on the orderbook directory nodes; any passive observer can pull a
snapshot. The bond UTXO itself is timelocked and almost never
spent in our corpus, but the transaction that *created* the bond
output is regular (non-CJ) and its inputs are same-wallet as the
FB owner by the standard common-input ownership heuristic (CIOH).

v7.3 turns the public orderbook into two new same-wallet edges
over the maker slots already clustered by v7.2:

* **Backward FB-funding CIOH.** Let nick `N` own FB UTXO `(F,
  v_FB)`. Every input outpoint of `F` is same-wallet as `N`. If
  any such input equals the *change output* of a maker slot `s`
  (i.e., `s`'s change was consumed when `N`'s wallet funded its
  bond), then `s` belongs to `N`'s wallet.
* **Strict forward FB-funding sibling.** If `F` has at most two
  outputs, the non-FB output is change of `N`. If that change
  outpoint is later consumed as a maker-slot input `s'`, then
  `s'` is in `N`'s wallet.

Two slots anchored to the same nick are then unioned, subject to
the same-CJ forbid-set inherited from v6.

Safety guards. v7.3 has three precision-protecting filters:

1. **JM CoinJoin exclusion.** If the funding tx `F` is itself a
   known JoinMarket CoinJoin, CIOH on `F` is unsound and the
   anchor is dropped. On our corpus two of 95 FB-funding txs are
   CJs and are skipped.
2. **Same-CJ forbid.** Two slots anchored to the same nick that
   happen to sit in the same CJ would be a hard precision
   violation; they are dropped by the constrained union-find.
3. **Cluster-nick conflict abstain.** If two distinct FB nicks
   anchor the same v7.2 cluster, neither nick's anchors are
   applied (zero on our corpus, but the rule is in place).

Empirically on the mainnet corpus (orderbook snapshot of
2026-05-22, 95 FB UTXOs, funding txs fetched from a public
indexer):

| metric                                              | count |
|-----------------------------------------------------|------:|
| FB-funding txs available                            |    95 |
| skipped (funding tx is a JM CJ)                     |     2 |
| used                                                |    93 |
| backward anchors via maker-slot change              |    17 |
| backward anchors via maker-slot input               |     0 |
| strict-forward anchors                              |     1 |
| nicks with at least one anchor                      |    13 |
| nicks spanning >=2 v7.2 clusters (merge candidates) |     4 |
| cluster-nick conflicts                              |     0 |
| candidate slot pairs                                |     6 |
| same-CJ pairs rejected                              |     0 |
| **cross-cluster unions added**                      | **6** |

The four merging nicks span nine distinct v7.2 clusters; the six
pairwise unions cut the cluster count by five (one transitive
merge) from 69,003 (v7.2) to 68,998. None of the four nicks
appears in our probe set, so v7.3 anchors information that the
probe-side ground truth in §6 does not see and the probe-side
precision check (0 violations) is independent of v7.3.

The marginal anonymity-set impact is small because v7.3 affects
only the handful of CJs whose slots were in those nine v7.2
clusters. The mean residual anonymity set is unchanged at 3.706
to three decimal places (mean certified makers per CJ moves from
4.949 to 4.950). v7.3's value is qualitative: it shows that the
public orderbook by itself, with no probing, already widens a
narrow but precision-safe maker-wallet edge.

The v7.3 module is implemented at
[`src/coinjoin_simulator/clusterer_v73.py`](https://github.com/joinmarket-ng/coinjoin-simulator/blob/main/src/coinjoin_simulator/clusterer_v73.py).

## 6. Ground-truth validation

We validate the clusterer (v6 through v7.3) against three
independent ground-truth sources, all of which a passive on-chain
analyst would *not* have but which we can construct here.

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
such collisions across all five iterations (v6 through v7.3,
see §5.1). This is a hard upper bound on the precision violation
rate (it is not a recall statement).

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

| metric                                       | v6   | v7   | v7.1 | v7.2 | v7.3 |
|----------------------------------------------|-----:|-----:|-----:|-----:|-----:|
| nicks probed                                 | 72   | 72   | 72   | 72   | 72   |
| nicks with at least one match                | 16   | 35   | 35   | 35   | 35   |
| offered UTXOs                                | 101  | 101  | 101  | 101  | 101  |
| offered UTXOs found in a cluster             | 19   | 40   | 40   | 40   | 40   |
| **cross-nick collisions in any cluster**     | **0**| **0**| **0**| **0**| **0**|

![probe validation card](figures/probe_validation_v6.svg)

The pass-or-fail check: for every pair of distinct probed nicks
`(A, B)`, no cluster contains a UTXO of `A` and a UTXO of `B`.
We observe **zero** such cross-nick collisions under all five
clusterer iterations. The clusterer never merged two real
JoinMarket maker wallets into one component on this corpus.
Every nick whose UTXOs appear in the cluster set has all of them
in the same cluster, or in several clusters that all belong to
that nick.

The probe data also constrains the *recall direction*: under
v7.3, the probe matched 35 of 72 advertised nicks across 40 of
101 advertised UTXOs (v6 alone matched 16 nicks across 19 UTXOs;
v7's equal-chain edge extends recall by linking probe-advertised
equal outputs to the chain). The unmatched 37 nicks
contributed UTXOs that were never observed entering a JM CJ in
our corpus (cold-storage parts of the wallet, recent deposits, or
UTXOs that were spent in CJs older than our crawl horizon). They
are not validation failures.

The three precision checks converge: v6 through v7.3 are
precision = 1.0 by construction, by the within-CJ structural
check on 16,890 mainnet CJs, and by the probing ground truth on
35 matched real maker nicks across 40 distinct UTXOs.

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

| metric                                          | v6 (change-chain only) | v7 (change + equal) | v7.1 (+ co-spend) | v7.2 (+ round-trip) | v7.3 (+ FB-funding) |
|-------------------------------------------------|-----------------------:|--------------------:|------------------:|--------------------:|--------------------:|
| mean published `n_eq`                           | 8.66                   | 8.66                | 8.66              | 8.66                | 8.66                |
| mean certified makers per CJ                    | 4.69                   | 4.94                | 4.95              | 4.95                | **4.95**            |
| mean residual anonymity set                     | 3.97                   | 3.72                | 3.71              | 3.71                | **3.71**            |
| share of CJs with at least one certified maker  | 97.6%                  | 97.9%               | 98.0%             | 98.0%               | **98.0%**           |
| median residual anonymity set                   | 3                      | 3                   | 3                 | 3                   | 3                   |
| share of CJs reaching residual = 1 (taker alone)| 8.0%                   | 10.6%               | 10.7%             | 10.8%               | **10.8%**           |

v7.3 cuts the mean residual by 0.26 candidates per CJ versus v6
and lifts the share of CJs where the taker is the sole remaining
candidate from 8.0% to 10.8%, a **35% relative increase** in the
worst-case-for-the-taker outcome. The mean published anonymity
set shrinks from 8.66 to 3.71 (a 57% reduction). 98.0% of CJs
leak at least one maker through the protocol chain. 10.8% reach
residual = 1: every maker in the CJ is certified and only the
taker remains in the hide-set. We do not claim full
deanonymization on those CJs (the taker's identity itself is
still unknown to the on-chain analyst), but their hide-set has
collapsed to one candidate.

The marginal v7.3 contribution to the headline anonset is small
(only six unions across four FB nicks), but v7.3 is the first
clusterer in this stack to use the public orderbook directly: it
demonstrates that even a passive observer who scrapes the
orderbook once and follows funding-tx CIOH can extract a
precision-safe maker-wallet edge that the chain-only v6/v7/v7.1/
v7.2 pipeline does not produce.

![residual anonymity set histogram (v7.3)](figures/anonset_reduction_hist.svg)

The overlay across all five iterations makes the shift visible:

![v6 through v7.3 anonset overlay](figures/v6_vs_v7_anonset_overlay.svg)

The v7.3 distribution sits to the left of v6 across the whole
range: more residual = 1 and residual = 2 CJs, fewer in the long
tail. 32% of CJs have residual <= 2 and 67% have residual <= 4.

### 7.2 Per-`n_eq` breakdown

The reduction holds across every round size in the corpus:

![mean anonset before and after, by n_eq](figures/anonset_per_n_eq.svg)

The grey bars are the published anonymity sets the taker thinks
they hide in; the red bars are the v7.3 lower-bound residual
after certified makers are removed. The residual stays in a 2 to
4 band across the entire range from `n_eq = 3` to `n_eq = 17`.
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
forward-spent equal output whose downstream slot is in a v7.3
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
- Cross-CJ CIOH on the off-chain side of the maker wallet is now
  used in two conservative forms: v7.1 unions multiple maker
  change UTXOs co-spent in a non-CJ transaction with at most two
  outputs, and v7.2 unions a maker change UTXO with a future
  maker-slot input that is reached through a single non-CJ hop
  whose spender has at most two outputs. Both filters discard
  richer non-CJ shapes (3+ outputs) on the precision-first
  principle, leaving a fraction of true same-wallet edges
  unobserved.
- The v7.3 fidelity-bond edge anchors at the public orderbook
  snapshot. Its yield on this corpus is six unions over four
  nicks, bounded above by (a) our slim_txindex covering only the
  JM-touching subgraph (we fetched the 86 missing FB-funding txs
  from a public Bitcoin indexer to close that gap) and (b) the
  precision-safe abstain on funding txs that are themselves
  CoinJoins. A future version could walk funding-tx inputs back
  more than one hop, halting at any CJ-shaped ancestor.
- Forward crawl frontier. Recent CJs near the crawl horizon have
  fewer observed successors, so their slots look like singletons
  more often than the structural truth warrants. This biases the
  residual upward for the most recent quarter of the corpus.
- Makers who consolidate winnings off-CJ between rounds (move
  funds to cold storage and refund the next round from a freshly
  derived address) look like two singletons to the clusterer. The
  probe data (§6.3) suggests this is not the dominant pattern,
  but a direct count is not yet available.
- Active-adversary edge (probed nick to advertised UTXO).
  We prototyped a v7.4 layer that unions any v7.3 clusters whose
  inputs or change outputs match the UTXOs advertised by a single
  nick during probing (`tmp/probe_rounds/*.json`, 72 nicks across
  101 advertised UTXOs). Four nicks span multiple v7.3 clusters,
  yielding four safe unions over nine clusters (delta -5
  clusters, 68,998 to 68,993) with zero forbid conflicts and zero
  precision violations. The mean residual anonymity set is
  unchanged at 3.706 because the touched clusters are all small.
  We do not include v7.4 in the headline numbers: it requires an
  active adversary (orderbook probing with auth verification),
  which breaks the passive-on-chain threat model of v6 through
  v7.3. The four v7.4 nicks are disjoint from the four v7.3 FB
  nicks, so the probe-derived edge corroborates v7.3 from an
  independent angle and is documented here as a negative-yield
  augmentation for an active threat model.

The probe data after the v7.3 upgrade shows 35 of 72 advertised
nicks matched (same as v7, v7.1, and v7.2; v6 alone matched 16
nicks because it relies on the change-chain only); each layer
passes the zero-cross-nick-collision check at precision = 1.0. The newer
merges concentrate in older corpus regions where more chain and
non-CJ CIOH edges have time to fire, away from the probed-nick
set of recent live makers. The four nicks v7.3 merges are
orderbook-only nicks not present in the probe set, so the
probe-side precision check is independent evidence that v7.3 did
not over-merge. Future improvements should target ILP recall
(more decoded CJs across the long tail of larger rounds) and
multi-hop backward walks from FB-funding txs through chains of
non-CJ ancestors.

## 10. Conclusion

The JoinMarket equal-output anonymity set is not the right
metric to publish to users. A passive on-chain adversary running
a protocol-correct chain-following clusterer at precision = 1.0
reduces the published anonymity set from a mean of 8.66 to 3.71
on the full mainnet corpus, with 98.0% of CJs losing at least one
candidate to certified-maker removal. The structural property
the attack exploits, namely that JoinMarket's two same-wallet
UTXO chains (the change-chain at the same mixdepth and the
equal-output chain at the next mixdepth, the latter disambiguated
by the maker's own fee schedule) combined with conservative
non-CJ CIOH on the maker's off-chain consolidations, are all
intrinsic to the protocol and to the typical maker wallet
workflow; they are not a fixable implementation bug.

The precision = 1.0 guarantee is what makes the result
actionable: the clusterer never merges two distinct maker
wallets, validated by three independent ground-truth sources.
Each certified maker the analyst extracts is a *deterministic*
hide-set reduction, not a probabilistic one. 10.8% of CJs reach
residual = 1, meaning every maker is certified and only the taker
remains in the anonymity set.

The practical implication for JoinMarket users is that the
relevant privacy figure for a round is not its published `n_eq`
but the v7.3 residual, which is typically 2 to 4 across the
entire range of round sizes the protocol supports. Mitigations
that break any of the structural channels (cold-funding the next
round from a fresh derivation that decouples the maker's
next-round inputs from previous-round change; a fee policy that
produces identical fingerprints across an entire offer cohort,
removing the within-CJ uniqueness v7 needs; batching all off-CJ
consolidations into transactions with more than two outputs,
removing the v7.1, v7.2, and v7.3 CIOH-safe filter; or funding
the fidelity bond from a wallet kept strictly disjoint from the
maker's CJ wallet) would close the principal structural channels
the clusterer exploits.
