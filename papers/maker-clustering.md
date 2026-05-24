# JoinMarket Maker Wallet Clustering and Taker Anonymity-Set Reduction

> **TL;DR.** JoinMarket is a Bitcoin CoinJoin protocol where a
> *taker* pays a small fee to one or more *makers* to mix coins
> into a single transaction with several equal-value outputs;
> every equal output is supposed to be indistinguishable from
> every other equal output in the same round (makers want
> anonymity from each other and from the taker, just as the taker
> wants anonymity from the makers). We ran a passive on-chain
> experiment on exactly one year of mainnet JoinMarket activity
> (heights 894,697 to 947,358, 2025-05-01 to 2026-05-01, 7,400
> ILP-decoded CoinJoins) and clustered maker wallets using only
> protocol-forced signals: the JoinMarket mixdepth state machine
> (a maker's change stays in the source mixdepth and its equal
> output advances to the next, and the maker chooses the
> mixdepth with the highest balance to re-advertise), the
> integer-linear-program (ILP) recovery of slot membership
> (which inputs and change output belong to the same participant)
> inside each CoinJoin so that the Common Input Ownership
> Heuristic (CIOH) can be applied across the on-CJ and off-CJ
> sides of a maker wallet, and the maker's published fee
> schedule used as a per-CoinJoin disambiguator. A simple example
> of that last signal: if a maker charges 0.1% in one CJ and an
> equal output of that CJ then participates in a later CJ whose
> ILP-recovered slot also charges 0.1% (and no other maker in
> the producer CJ charges 0.1% at that amount), the equal output
> is uniquely bound to that producer slot. This *fee-fingerprint*
> attribution is the only chain edge in this attack that ties a
> specific equal output to a specific maker slot; the
> change-chain and the auxiliary edges (CIOH, FB-funding) cluster
> *slots across CoinJoins* but cannot label which of the
> indistinguishable equal outputs of a producer CJ belongs to
> which maker.
>
> The result is more measured than earlier versions of this
> paper suggested. The mean published anonymity set is 8.65
> equal outputs per CJ; the mean *residual* anonymity set after
> removing makers whose equal outputs are univocally attributed
> by the fee fingerprint (or co-spent with a cluster-mate's
> attributed output in a later CJ) is 8.22. On 34.5% of CJs at
> least one maker's equal output is certified; on 0% of CJs are
> all makers certified. The fee fingerprint is therefore a
> measurable but bounded leak. None of the edges in this attack
> are probabilistic: every merge is a hard same-wallet conclusion
> or no merge at all. The single largest remediation is
> *fee-policy homogenization*: in the simulator, when every maker
> runs the reference client's default policy the fingerprint
> cannot disambiguate and the residual rises to its theoretical
> ceiling $n_{eq}$ ($\S 9.4$). Practical mitigations and their
> trade-offs are discussed in $\S 9$.

## 1. Scope and motivation

A JoinMarket CoinJoin (CJ) is an atomic transaction in which one
*taker* and $M$ *makers* contribute inputs and produce
$n_{eq} = M + 1$ equal-amount outputs (the *equal outputs*) plus
up to $n_{eq}$ change outputs (one per participant who needs
change; typically all $M$ makers and usually the taker too). Each
participant contributes one *slot*: a bundle of one or more
inputs they own, exactly one equal-amount output, and at most one
change output. The published anonymity property is that the
taker's equal output is indistinguishable from the makers' equal
outputs: the taker hides in a set of $n_{eq}$ candidates per
round.

JoinMarket defends this set in several layered ways:

- Both makers and takers run the same wallet software and keep
  funds in five separate *mixdepths* (numbered $d \in
  \{0, 1, 2, 3, 4\}$). A single slot uses inputs from one
  mixdepth only; the equal output goes to mixdepth
  $d{+}1 \bmod 5$ of the same wallet (the equal output is the
  part that gained privacy and gets to advance); the change
  output stays at mixdepth $d$ (its address derivation belongs to
  the same mixdepth as the inputs, and JoinMarket clients refuse
  to co-spend UTXOs across mixdepths). Outputs from different
  mixdepths of the same wallet are therefore never co-spent
  inside the wallet without an explicit consolidation step in
  the client.
- Each maker advertises offers on the JoinMarket directory-server
  overlay. Today the overlay is a small set of directory nodes
  that relay (often end-to-end encrypted) Tor messages between
  participants; earlier versions used IRC. Nicks are randomized
  per session, and offers can be advertised either with or
  without a fidelity bond. A *fidelity bond* (FB) is a timelocked
  P2WSH UTXO the maker proves they control; bondless offers can
  be advertised freely and are therefore trivially Sybil-able,
  so takers in practice almost always select bonded makers (the
  taker selection algorithm weights by bond value). The FB is
  the only durable label a passive observer sees, and the
  orderbook publishes it in cleartext.
- The taker's identity at round $T$ does not, by itself, leak
  which of the $n_{eq}$ equal outputs it owns.

This paper studies what the same passive adversary *can* still
learn from on-chain data. The central observation is that a maker
who participates in two CJs leaves a deterministic same-wallet
*slot* edge in the chain: either an equal output of CJ $T$ (at
mixdepth $d{+}1$) reused as a maker input in CJ $S$ (the maker
later advertising mixdepth $d{+}1$), or a change output of $T$
(at mixdepth $d$) reused later when the maker advertises $d$
again. Following those edges clusters maker *slots* across CJs.
Cluster membership reduces the taker's hide-set in CJ $T$ only
when an equal output of $T$ can be individually attributed to a
specific producer slot; this attribution requires the
fee-fingerprint signal of $\S 5.2$ (or, in a small number of
cases, a downstream co-spend with a cluster-mate's already
attributed output). Slot clustering alone is necessary but not
sufficient for taker anonymity-set reduction.

This paper answers:

1. How many JoinMarket maker wallets can be clustered from
   on-chain data alone, at precision = 1.0 (under the gate and
   corpus described in $\S 5.2$ and $\S 6.1$), on the mainnet
   corpus?
2. By how much does that clustering reduce the per-CJ taker
   anonymity set, once we restrict to the certification channel
   that can actually identify an individual equal output (fee
   fingerprint and the small number of co-spend cases that lift
   on top of it)?
3. Are the resulting clusters protocol-correct against an
   independent ground-truth source (an active probing campaign
   that collects real maker UTXO-to-nick bindings)?

## 2. Threat model

Passive on-chain adversary with full corpus access:

- a snapshot of every JoinMarket CoinJoin reachable by a forward
  and backward crawl seeded from probe-collected addresses. The
  crawl covers the public mainnet history through May 2026 and
  finds 23,876 JM-flagged CJs going back to mid-2021, though the
  density is heavily skewed to the last ~14 months (75% of the
  decoded CJs sit in 2025-04 onward);
- the public orderbook
  ([joinmarket-ng.sgn.space/orderbook.json](https://joinmarket-ng.sgn.space/orderbook.json));
- the ability to solve a CJ-sized ILP (less than 30 inputs);
- compute on the order of CPU-hours (the full corpus pass fits in
  under 15 minutes on 14 cores).

The adversary does *not* participate in any CoinJoin and is not
assumed to control any maker. We did run a small off-chain
probing campaign in late April 2026 (three rounds, 72 maker nicks,
described in §6.2) that contributed seed addresses to the corpus
crawl and is reused as a ground-truth oracle in §6. The probing
was done in good faith, at the smallest size that still gives a
useful precision check. To avoid republishing privacy-sensitive
on-chain identifiers, the probe artefacts kept in this
repository are reduced to per-nick advertised-UTXO *counts* and
the precision-check outcomes (matched-nick set, cross-nick
collision counts); we deliberately do not publish the maker
outpoints, amounts, addresses, or fidelity-bond UTXOs collected
during probing. The probing data contributes nothing to the
clustering itself.

## 3. JoinMarket protocol primer

Three protocol facts are load-bearing for the clusterer:

1. **Per-CJ slot uniqueness.** Each participant (taker or maker)
   contributes exactly one slot. A slot may aggregate several
   UTXOs to cover the offered amount, but it always produces one
   equal-amount output and at most one change output, all in the
   same mixdepth.

2. **Same-mixdepth change (sticky change).** A slot whose inputs
   come from mixdepth $d$ lands its change output back in
   mixdepth $d$. This is enforced by the wallet (the change
   address is derived from the same mixdepth's key tree), so the
   change UTXO is eligible to be a future input of the *same
   maker* whenever the maker next advertises mixdepth $d$. The
   downstream consequence is what makes the change-chain edge so
   sharp: if some later CJ slot in our corpus has inputs whose
   ILP-selected combination *exactly* matches this change UTXO
   (typically as one of several inputs in a subset-sum
   decomposition), the two slots are the same wallet by
   construction.

3. **Mixdepth-advancing equal output.** The slot's equal output
   lands in mixdepth $d{+}1 \bmod 5$. JoinMarket makers normally
   advertise from whichever mixdepth currently holds the most
   coins, which after a successful round is often (but not
   always) the mixdepth that just received the equal output.
   Deposits, withdrawals, and consolidations can push the
   "fattest mixdepth" elsewhere, so the next advertisement is not
   forced to be $d{+}1$. When the maker does come back from
   mixdepth $d{+}1$, the equal output of $T$ is a natural input
   for that next slot.

   This produces an "equal-chain" same-wallet edge that v6 does
   not use directly, because within one CJ all $n_{eq}$ equal
   outputs look identical (any permutation of equal-output owners
   is consistent with the ILP-recovered fee constraints).
   Section 5.2 (v7) restores this edge by using the consumer
   slot's own realized fee as a per-CJ fingerprint: if exactly
   one slot in the producer CJ would have charged this fee at the
   producer CJ's amount, we have identified that slot.

Two more JoinMarket details matter for the analysis pipeline but
not for the clusterer itself:

- A maker offer is *either* relative or absolute, not both
  (`cjfee_r` or `cjfee_a`, the field name picks the kind). Most
  makers run a single relative offer.
- The maker's contribution to the on-chain fee (`txfee`) is 0
  sats in the default policy and in practice is 0 across the
  observed corpus.

The clusterer uses fact 1 as a hard pairwise must-not-link, fact
2 as a definite same-wallet must-link, and fact 3 (via the
fee-fingerprint rule of §5.2) as a per-CJ disambiguator that
sometimes turns the producer-to-consumer equal-output reuse into
a definite must-link.
Fees are therefore *used* by the clusterer, but only in a
precision-preserving way: we accept a fee match as evidence only
when it picks a single producer slot inside a single producer CJ,
never as a global fee-band that pools several CJs. Fidelity-bond
values, nick patterns, and any other off-chain signal are not
used (with the explicit exception of §5.5, which uses the
public orderbook to anchor FB-owner identity to funding-tx
inputs). The intentional restriction to protocol-forced or
single-CJ-unambiguous evidence is what gives precision = 1.0 by
construction (under the gate and corpus stated in §5.2 and §6.1;
loosening either is the failure mode discussed in §6.1).

### 3.1 Worked example

A two-maker CJ at amount 1,000,000 sats with one taker and makers
$A$, $B$. Each maker charges a CoinJoin fee of 1,000 sats; the
miner fee for the whole transaction is 4,000 sats and is paid in
full by the taker (default JoinMarket policy: $\mathit{txfee} = 0$
for each maker offer):

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

- **Taker**: pays $2 \cdot 1{,}000$ (maker fees) plus $4{,}000$
  (miner fee), a **6,000 sat net cost** for the mix.
- **Maker A**: receives a 1,000,000 equal output and a 51,000
  change output for a total of 1,051,000 against 1,050,000
  inputs, **earning +1,000 sats**.
- **Maker B**: receives a 1,000,000 equal output and a 31,000
  change output for a total of 1,031,000 against 1,030,000
  inputs, **earning +1,000 sats**.
- **Miner**: receives 4,000 sats.

The taker funds both maker payouts and the miner fee; the makers
are paid for providing liquidity. A maker's per-CJ cashflow is
always non-negative (zero for a maker who advertises a zero fee).

The ILP decomposition tells us which subset of inputs and which
change output each participant contributed; it cannot tell which
of the three equal-amount outputs is whose (the equal outputs are
indistinguishable on-chain by amount alone). The change output
for $A$ (51,000 sats in mixdepth 1) will reappear as an input in
some future CJ where $A$ again advertises mixdepth 1; that future
CJ is the chain edge that v6 walks. The same applies to $B$'s
change in mixdepth 0.

The equal outputs go to mixdepth 2 of their respective owners and
become natural inputs for whichever participant next advertises
from mixdepth 2 (the wallet's depth-rotation policy will usually
prefer the fattest mixdepth, which after this CJ is often
mixdepth 2 but not necessarily). When such a reuse happens in a
later CJ $S$, the consumer slot's realized fee in $S$ identifies
the producer slot in $T$ if and only if no other slot in $T$ would
have charged the same fee at $S$'s amount: that is the
fee-fingerprint rule of §5.2 in concrete form. For example, if
$A$ charges 0.1% relative and $B$ charges a fixed 800 sats, then
on $S$ at amount $1{,}500{,}000$ the consumer slot's fee would be
1,500 sats if it came from $A$ and 800 sats if it came from $B$:
those values are distinct, so an observer who sees fee 1,500
sats in $S$ for a slot whose first input is one of $T$'s equal
outputs concludes the producer slot was $A$.

## 4. Mainnet corpus

A forward and backward crawl seeded from probe-collected
addresses, walking only outspends from already-classified
JoinMarket CoinJoins, produced the snapshot used here. We
restrict the window to exactly one year of mainnet JoinMarket
activity, block heights 894,697 to 947,358 (UTC dates 2025-05-01
to 2026-05-01):

| metric                | count        |
|-----------------------|-------------:|
| JM CoinJoin txs in window | 10,581 |
| time span             | 1 year (heights 894,697 to 947,358) |
| ILP-decoded CJs       | 7,400 (69.9%) |
| ILP failures (timeout / infeasible at `max_fee_rel = 0.05`, `time_limit = 2s`) | 3,181 (30.1%) |
| maker slots recovered | 56,614 |

An ILP run on one CJ *succeeds* when it returns a feasible
slot-by-slot decomposition that satisfies every per-slot
constraint: each slot is a single mixdepth's worth of inputs that
sums to exactly one equal output plus one change output, every
slot's realized fee is non-negative, and the per-CJ fee budget is
respected. We count anything else (solver timeout at 2 s, an LP
relaxation that proves infeasibility under
$\mathit{max\_fee\_rel} = 0.05$, or a feasible-but-degenerate
solution that leaves any slot unassigned) as a failure. There is
no partial decomposition output: every slot in a CJ either gets
a unique attribution or the whole CJ is dropped.

CJs that do not decode contribute no slots and no chain edges.
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
- **Must-link, fee-fingerprint attribution (fact 3).** In the
  simulator the equal-output owner is known and v6 unions producer
  with consumer directly. On mainnet, v7 (§5.2) restores this
  same-wallet edge by picking the producer slot from a *fee
  fingerprint* of the consumer slot in the next CJ; the edge
  fires only when that fingerprint identifies exactly one slot in
  the producer CJ, otherwise no edge is added. This is the only
  signal in the attack that ties a specific equal output of $T$
  to a specific producer slot of $T$ ($\S 7$ Path A); the other
  edges in this list (and in $\S 5.3$-$\S 5.5$) cluster slots
  across CJs but do not attribute equal outputs.

The clusterer uses fees, but only locally: v7's fee-fingerprint
rule (§5.2) selects at most one producer slot inside a single
producer CJ from the $n_{eq}$ equal-output candidates. It does
*not* pool slots across CJs by their advertised
$(\mathit{cjfee}_r, \mathit{cjfee}_a)$ tuple, which is what
earlier fee-band heuristics tried and what would silently union
distinct same-policy makers. Addresses, amounts, fidelity-bond
values, and nick patterns are never used as a clustering signal
by themselves (§5.5 anchors known FB-owner identity to its own
funding transaction's inputs via CIOH, which is a per-bond
provable same-wallet edge, not a pattern match across bonds).
Every merge is a direct consequence of the protocol. By
construction the clusterer can only *under-cluster*: a maker
whose downstream remix is missing from the corpus (crawl
frontier, ILP failure, or exit from the ecosystem) appears as a
singleton even when their wallet served many more CJs in
reality. Precision is therefore $= 1.0$ by construction under
the gate stated here (see §5.2 for the three gate strengths and
§6.1 for their measured behavior), and recall is bounded below
by the fraction of CJs the corpus successfully observes and
decodes.

### 5.1 Cluster size distribution

The full pass over the 56,614 mainnet slots in the 1y window
produces the following v7.3 final-cluster summary:

| metric                                       | v7.3      |
|----------------------------------------------|----------:|
| total clusters                               |   26,218  |
| singleton clusters                           |   15,588  |
| non-trivial clusters (size $\geq 2$)          |   10,630  |
| largest cluster                              | 117 slots |
| same-CJ slot collisions (must-not-link violations) | 0   |

Per-edge incremental contributions on top of the v6 (change-chain)
baseline: v7 fee-fingerprint attribution edges are 2,996 outpoints
(equivalent cluster unions are bookkept inside the constraint-
propagation step, not as separate cluster-merge counts). v7.1 adds
58 cross-CJ unions through the CIOH co-spend edge; v7.2 adds 73
through the round-trip edge; v7.3 adds 3 through the FB-funding
back-anchor. Precision is 1.0 across all edges.
non-CJ co-spend edge (§5.3), removing 82 further singletons. v7.2
adds 153 cross-CJ unions through the non-CJ round-trip edge
(§5.4), removing 100 more clusters. v7.3 adds 6 cross-cluster
unions through the fidelity-bond funding-tx CIOH edge (§5.5),
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

### 5.2 v7: fee-fingerprint equal-output attribution

Every maker advertises a single offer (relative $\mathit{cjfee}_r$
or absolute $\mathit{cjfee}_a$, not both) and the on-chain fee
they earn in a CJ is a deterministic function of that offer and
the equal-output amount $a$:

$$
\mathit{fee}(a) \;=\;
\begin{cases}
\mathit{cjfee}_a & \text{(absolute maker)} \\
\mathrm{round\_half\_even}(\mathit{cjfee}_r \cdot a) & \text{(relative maker)},
\end{cases}
$$

where the relative case uses banker's rounding to the nearest
satoshi, matching JoinMarket-NG's
`Decimal(cjfee) * Decimal(amount)`.`quantize(Decimal(1))`
in `jmcore/src/jmcore/bitcoin.py` (and the equivalent path in
joinmarket-clientserver).

The ILP recovers this realized fee per slot from the input total,
equal output, and change output. The fee is observable but not by
itself identifying: thousands of slots share the same
$(\mathit{cjfee}_r, \mathit{cjfee}_a)$ fingerprint across the
corpus, so a global fee-band clusterer would silently merge
distinct same-policy makers.

v7 uses the fee fingerprint locally, *within a single producer
CJ*. When an equal output of producer CJ $T$ is spent as a maker
input of slot $c$ in a later CJ $S$, the consumer slot $c$
carries its own realized fee $f_c$ at amount $a_S$. We then look
at $T$'s maker slots $S_1, \dots, S_n$ (at most about 22 in
practice) and ask: is there exactly one $S_i$ whose advertised
offer would produce $f_c$ at amount $a_S$? Concretely, for each
$S_i$ with realized fee $f_i$ at amount $a_T$ we check the two
admissible interpretations:

- *absolute* match: $f_i = f_c$ (so $S_i$'s policy is
  $\mathit{cjfee}_a = f_c$);
- *relative* match: $f_i / a_T$ and $f_c / a_S$ agree to the
  nearest part per million. Each side's ppm value is computed
  in integer arithmetic over the satoshi-denominated fee and
  equal-amount: $\mathrm{ppm}(f, a) = \mathrm{banker\_round}(f
  \cdot 10^6 / a)$, evaluated as a single `divmod` on
  arbitrary-precision integers so that no float64 truncation
  enters the equality test. Two slots match on the relative
  fingerprint iff their integer ppm values are equal, which
  in turn implies $S_i$'s policy is $\mathit{cjfee}_r = f_c /
  a_S$ to that resolution.

v7 admits three gate strengths, in increasing order of
precision and decreasing order of recall. All three share the
same enumeration above; they differ only in which of the
enumerated matches becomes a same-wallet edge.

**Per-CJ univocal gate (loose).** The original v7 gate. An edge
is added if and only if exactly one $S_i$ matches under absolute
*or* relative, and the absolute and relative interpretations do
not point at two different slots. Ambiguous within-$T$ matches
and conflicting interpretations are dropped.

**Per-CJ both-interpretations gate (strict).** An edge is added
only when both interpretations independently identify the *same*
$S_i$ as the unique match. This is the
``unique_both_same_slot`` subset of the loose gate. It rejects
single-criterion matches whose target slot may have coincidentally
collided with the consumer fingerprint under per-announcement fee
jitter (\u00a76.1).

**Corpus-unique gate.** An edge is added only when the chosen
$S_i$'s fingerprint is, across the entire corpus of v7-enriched
slots, free of doppelgangers: no slot outside $T$ and outside
the consumer $c$ carries the same absolute *or* the same
relative fingerprint. This is strictly stronger than per-CJ
univocality because it requires the producer's fee policy to
be unique in the corpus, not merely unique inside $T$, and the
"or" makes the reject conservative (any single-axis collision
elsewhere in the corpus is enough to drop the edge). The
corpus-unique gate is the one that
restores precision = 1.0 in the controlled simulator
experiment (\u00a76.1, Table). The two per-CJ gates are
heuristic: they preserve precision under sparse fingerprint
collisions, as on the mainnet corpus where the empirical
violation rate is 0 (\u00a76.2), but they cannot guarantee it
under adversarial jitter where many makers concentrate near the
same fee policy.

Empirically on the mainnet corpus (51,439 cross-CJ equal-output
reuses) under the per-CJ loose gate:

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
mainnet (\u00a75.1: 0 collisions, \u00a76.2: 0 cross-nick
collisions). The mainnet headline numbers reported throughout
the rest of this paper use the per-CJ loose gate, since the
empirical violation rate is 0 on this corpus and the recall delta
to the corpus-unique gate is non-trivial. The corpus-unique gate
is the conservative choice when fee-policy density is high or
when an adversarial maker can deliberately jitter fees to forge
edges; see \u00a76.1 for the simulator comparison.

### 5.3 v7.1: non-CJ co-spend (Common Input Ownership Heuristic)

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

The v7.1 module reuses the v7 union-find with the same
forbid-set semantics.

### 5.4 v7.2: non-CJ round-trip CIOH

The v7.1 filter caps non-CJ spenders at two outputs because we
cannot prove same-owner across more outputs. Many real on-CJ
remixes also leave a single-hop non-CJ trail in between: a maker
consumes their change at mixdepth $d$, immediately rebroadcasts
through a two-output hop transaction (consolidation, fee bump,
deposit echo), and one of the hop outputs is then consumed as an
input of a later maker slot. This is the change-chain edge of v6
with one non-CJ hop interposed.

The v7.2 edge fires when a non-JM transaction $H$ satisfies all
of:

1. $H$ has at most two outputs (same CIOH-safety filter as v7.1);
2. $H$ consumes at least one maker change UTXO from a known
   producer slot $p$;
3. at least one of $H$'s outputs is consumed as an input of a
   maker slot $c$ in a later CJ.

In that case $p$ and $c$ are unioned. The consumer slot $c$ may
have used the hop output as any of its inputs (the slot input set
is order-independent in the ILP), not just the first one.
Same-CJ pairs are dropped by the inherited forbid-set.

**Equal-amount discard (future work).** A v7.2 hop $H$ with at
most two outputs *could* in principle have one of its outputs
match a JM equal-output amount. In that case, when $H$'s output
funds a maker slot at amount $a$, the output is structurally
indistinguishable from a maker who simply picked an equal-amount
UTXO from any earlier source: the unionisation reduces to the
same equal-chain inference v7 already does in §5.2. A stricter
v7.2 variant would discard such hops, treating them as equal-
chain candidates rather than CIOH candidates. We have not
applied this discard in the headline numbers; on the mainnet
corpus 15,935 distinct JM equal-output amounts exist, but most
v7.2 hops carry change-shaped (non-round) values that do not
match any of them, so the expected effect on the 153 reported
unions is small.

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

Together, v7.1 and v7.2 close the off-chain CIOH side of the
maker wallet: any non-CJ transaction whose shape is consistent
with a simple consolidation or single-hop forwarding is used as
a same-wallet edge, while richer non-CJ shapes are conservatively
ignored.

### 5.5 v7.3: fidelity-bond funding-tx CIOH

JoinMarket makers advertise a *fidelity bond* (FB): a timelocked
P2WSH output that the maker proves they own. The orderbook
directory nodes publish, in cleartext, the nick to FB-UTXO
mapping; any passive observer can pull a snapshot. The bond UTXO
itself is timelocked and almost never spent in our corpus, but
the transaction $F$ that *created* it is a regular spend whose
inputs are same-wallet as the FB owner by the common-input
ownership heuristic (CIOH).

v7.3 turns the public orderbook into two new same-wallet edges
over the v7.2 maker-slot graph:

* **Backward FB-funding CIOH.** Let nick $N$ own FB UTXO $(F,
  v_{FB})$. Every input outpoint of $F$ is same-wallet as $N$.
  If any such input equals the change output of a maker slot $s$
  in the v7.2 graph (i.e., $s$'s change was consumed when $N$'s
  wallet funded its bond), then $s$ belongs to $N$'s wallet.
* **Strict forward FB-funding sibling.** If $F$ has at most two
  outputs, the non-FB output is change of $N$'s wallet. If that
  change outpoint is later consumed as a maker-slot input $s'$,
  then $s'$ is in $N$'s wallet.

Two slots anchored to the same nick are then unioned, subject to
the same-CJ forbid-set inherited from v6.

How are FBs funded in practice? We classified the inputs of every
FB-funding tx in our snapshot against (i) whether the funding tx
is itself a JM CJ, and (ii) whether the inputs reuse known JM
outputs (equal outputs or maker change):

| funding-tx class                                       | count |
|--------------------------------------------------------|------:|
| funding tx is itself a JM CJ                           |     2 |
| funded entirely from inputs with no observable JM provenance | 66 |
| funded from a mix of external and JM-derived inputs    |    17 |
| funded from one or more inputs that are themselves JM-CJ outputs (but not classified as equal or change in our slot table) | 10 |
| total FBs in orderbook snapshot (2026-05-22)           |    95 |

The headline observation is structural: 66 of 95 FBs (69%) are
funded from inputs with **no observable JoinMarket provenance**.
Their funding wallets look like ordinary external Bitcoin wallets
to a passive observer (cold storage, exchange withdrawal, a
separate fresh wallet). This is the privacy-preserving funding
pattern: the FB owner spends external coins to create the bond
and avoids tying the bond key to any prior on-chain JM activity.
A natural follow-up question is whether a one-hop CIOH check
understates the leak (the funding tx might still trace back to
JM coins one or two hops earlier). We did not extend the walk
beyond the immediate funding tx in this paper. A multi-hop
backward CIOH walk over non-CJ ancestors is a natural extension
and is listed in §10 as future work; on the present snapshot
69% of FBs cleanly fund from one-hop-external wallets, but the
true privacy-preserving share could be smaller once deeper
walks are folded in. The 17 mixed and 10 JM-input funding txs
are the ones that *do* leak a same-wallet edge to a v7.2 maker
slot at one hop, and they produce the 17 backward-via-change
anchors that v7.3 acts on.

Safety guards. v7.3 has two precision-protecting filters:

1. **JM CoinJoin exclusion with taker-eq-out re-examination.**
   The default rule excludes any funding tx $F$ that is itself a
   known JM CJ, because in a CJ the inputs do *not* all share
   one wallet. On our corpus two of 95 FB-funding txs fall under
   this rule. We re-examined both: in one case the FB-owner slot
   is identifiable because exactly one slot of $F$ pays an
   outlier fee (8,556,774 sats out of 8,562,887 total fees paid,
   which is the taker contribution), so the FB owner is that
   taker slot and CIOH applied to its inputs alone would be
   sound. In the other case ($n_{eq} = 13$ all at value
   946,399 sats, no outlier-fee slot), the taker slot cannot be
   isolated from the maker slots and the FB owner cannot be
   identified. We do *not* add either anchor: the gain from the
   identifiable case (one slot, one input UTXO) is below the
   threshold at which we are willing to add a special-case rule,
   and we report the analysis so that future iterations can
   decide whether to include the outlier-fee-slot variant.
   Restricting CIOH to a single slot's inputs (the *taker-equal-
   output* discard) is the structurally correct generalisation
   for any CJ-shaped funding tx: only the slot whose equal output
   funds the bond contributes inputs to the FB owner's wallet.
2. **Same-CJ forbid.** Two slots anchored to the same nick that
   happen to sit in the same CJ would be a hard precision
   violation; they are dropped by the constrained union-find.

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
| nicks spanning $\geq 2$ v7.2 clusters (merge candidates) |     4 |
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

## 6. Ground-truth validation

We validate the clusterer (v6 through v7.3) against three
independent ground-truth sources, all of which a passive on-chain
analyst would *not* have but which we can construct here.

The precision validation in this section uses a larger 5-year
mainnet corpus (129,301 maker slots from 16,890 ILP-decoded CJs)
to keep the probe and gate-comparison evidence as large as
possible; the 1y window of $\S 4$ is a subset of this corpus
used for the headline $\S 7$ residual numbers. Precision = 1.0
holds on both corpora: every chain edge in this attack is a
hard same-wallet conclusion, so the corpus choice does not
affect precision, only the absolute cluster counts.

### 6.1 Simulator end-to-end and the v7 gate hierarchy

The accompanying coinjoin-simulator builds a synthetic JoinMarket
network with the same ILP pipeline driving v6 and v7. We use it
in two regimes:

- **uniform regime**: every maker runs the *same* default
  relative-fee policy (the JoinMarket factory default). Fee
  fingerprints collapse onto a single point in fee space.
- **varied regime**: makers draw policy parameters
  $(\mathit{cjfee}_r, \mathit{cjfee}_a, \mathit{txfee},
  \mathit{minsize})$ from a discrete diversity grid, with a small
  per-announcement multiplicative jitter (~10%) on the realized
  fingerprint. This approximates the empirical fee diversity
  observed in JoinMarket order-book snapshots.

We run each regime at 500 makers / 20,000 rounds / 5 batches
(~100k maker slots / ~20k CJs after deposit and role-switching),
and report cluster quality under three observation modes:

- **ground-truth mode**: the clusterer sees the simulator's
  ``equal_output`` ownership labels directly (no fingerprinting
  needed). This isolates the v6 chain-edge backbone.
- **blinded mode**: equal-output labels stripped; v7 must recover
  ownership from fee fingerprints, as on mainnet.
- **torture mode**: both equal-output and change-output labels
  stripped; the clusterer has only the fingerprint signal and
  same-CJ must-not-link.

For each mode we evaluate four gate variants of v7: loose
(per-CJ univocal under abs OR rel), strict (per-CJ univocal under
abs AND rel pointing at the same slot), corpus-unique (the
producer slot's fingerprint is corpus-wide free of doppelgangers),
and strict + corpus-unique (intersection).

**Varied regime** (final maker count 189 after retirement and
role-switching, 100,000 maker slots, 100,000 CJs decoded):

| mode                | gate                   | clusters | ARI    | recall proxy | precision-violating clusters |
|---------------------|------------------------|---------:|-------:|-------------:|-----------------------------:|
| ground truth        | n/a (full labels)      |      244 | 0.879  | 0.999        |                            0 |
| blinded             | loose                  |    4,620 | 0.449  | 0.956        |                          136 |
| blinded             | strict                 |    8,203 | 0.433  | 0.920        |                           98 |
| blinded             | corpus-unique          |    8,928 | 0.323  | 0.912        |                        **0** |
| blinded             | strict + corpus-unique |    8,928 | 0.323  | 0.912        |                        **0** |
| torture             | loose                  |   87,135 | 0.000  | 0.129        |                        5,430 |
| torture             | strict                 |   98,842 | 0.000  | 0.012        |                          579 |
| torture             | corpus-unique          |  100,000 | 0.000  | 0.000        |                        **0** |
| torture             | strict + corpus-unique |  100,000 | 0.000  | 0.000        |                        **0** |

**Uniform regime** (final maker count 717, 100,000 maker slots):

| mode                | gate                   | clusters | ARI    | recall proxy | precision-violating clusters |
|---------------------|------------------------|---------:|-------:|-------------:|-----------------------------:|
| ground truth        | n/a                    |      717 | 1.000  | 1.000        |                            0 |
| blinded             | any                    |      717 | 1.000  | 1.000        |                            0 |
| torture             | any                    |  100,000 | 0.000  | 0.000        |                            0 |

The precision-violating-cluster count is the number of clusters
that contain maker slots owned by more than one simulator wallet.
Recall proxy is $\max(0, 1 - (n_{\text{clusters}} - n_{\text{owners}}) / (n_{\text{slots}} - n_{\text{owners}}))$,
and ARI is the standard adjusted Rand index against the
simulator's ownership labels.

Three findings deserve emphasis.

First, in the **uniform regime** the v7 fee-fingerprint gate
never fires: every maker advertises the same offer, so the
within-$T$ fingerprint set is constant and no $S_i$ is unique.
v7 collapses to v6, and v6's chain backbone alone is sufficient
to recover identity perfectly because the simulator preserves
chain labels. This is a counterfactual on the fee-policy axis,
not a recall failure of v7: when fees are uniform, the equal-
chain attribution is uninformative by construction, and the
clusterer correctly abstains.

Second, in the **varied regime under the per-CJ loose gate** the
blinded clusterer produces 136 precision-violating clusters out
of 4,620 (2.9%). The violations are all driven by the same
mechanism: per-announcement fee jitter occasionally makes a
maker's realized fingerprint in producer CJ $T$ coincide
*numerically* with a different maker's policy, while the true
producer of the consumed equal output has drifted off the
consumer's fingerprint. The per-CJ univocal test selects the
wrong $S_i$ because the right $S_i$ is no longer the unique
match. The strict gate reduces violations from 136 to 98 (a 28%
reduction) by demanding that both interpretations agree on the
same slot, but cannot eliminate them: jitter occasionally aligns
both abs and rel on the wrong slot.

Third, the **corpus-unique gate eliminates precision violations
entirely**, at a recall cost of ~5% (ARI 0.449 to 0.323, recall
proxy 0.956 to 0.912). The mechanism is direct: under jitter, a
falsely chosen $S_i$ shares its fingerprint with at least one
other slot somewhere else in the corpus (the true producer, or
yet another jitter-aligned maker). The corpus-wide doppelganger
check intercepts exactly these cases. In the torture regime the
corpus-unique gate produces no edges at all because in the
absence of chain-edge backbone every fingerprint has many
candidate doppelgangers; this is the desired behavior for an
adversarial setting in which fees alone do not carry attribution
signal.

The simulator therefore separates two questions that mainnet
data cannot: (a) is the per-CJ univocal test sound? and (b) does
the corpus carry enough fingerprint diversity for soundness to
matter? Under sparse, real-world fee distributions both gates
report no violations (the corpus-unique gate adds zero corrections
on mainnet, see \u00a76.2 cross-nick checks at 0 collisions for
the loose gate); under controlled jitter the per-CJ gate breaks
while the corpus-unique gate holds. The headline mainnet numbers
elsewhere in this paper use the loose gate, which is the right
choice when the empirical violation rate is 0 and recall is
worth ~5% per gate step; an adversarial-jitter setting should
prefer the corpus-unique gate.

For completeness we re-ran the gate hierarchy on the mainnet
corpus (129,301 maker slots from 16,890 ILP-decoded CJs):

| gate                    | clusters | non-trivial | largest | same-CJ violations | cross-nick violations | nicks matched | UTXOs matched |
|-------------------------|---------:|------------:|--------:|-------------------:|----------------------:|--------------:|--------------:|
| loose                   |   69,184 |      23,313 |     125 |                  0 |                     0 |        35 / 72 |     40 / 101 |
| strict                  |   74,051 |      24,274 |      91 |                  0 |                     0 |        35 / 72 |     40 / 101 |
| corpus-unique           |   74,469 |      24,343 |      91 |                  0 |                     0 |        35 / 72 |     40 / 101 |
| strict + corpus-unique  |   74,471 |      24,345 |      91 |                  0 |                     0 |        35 / 72 |     40 / 101 |

Moving from loose to corpus-unique splits 5,285 clusters (+7.6%)
and shrinks the largest cluster from 125 to 91 maker slots. The
probe-validator outcome is identical across all four gates: 35
matched nicks, 40 matched UTXOs, 0 cross-nick collisions, 4
nicks split across 2+ clusters with a maximum of 3 clusters per
nick. The recall cost of the corpus-unique gate is therefore not
visible in the probe-set granularity (which samples 72 of an
unknown total maker population): the 5,285 extra splits happen
entirely outside the probed nick set. We keep the loose gate as
the headline default for the rest of the paper but note that on
this corpus the choice does not affect the probe-validated
precision result.

### 6.2 Active probing of real maker wallets

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
of the same wallet**. This is the property §6.2 uses both to
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

The three precision checks converge: under the loose gate of
§5.2 and the 16,890-CJ corpus, v6 through v7.3 are precision =
1.0 by construction, by the within-CJ structural check on
16,890 mainnet CJs, and by the probing ground truth on 35
matched real maker nicks across 40 distinct UTXOs. The strict
and corpus-unique gates of §5.2 produce different cluster
counts (§6.1) but pass the same probe check (§6.2).

## 7. Anonymity-set reduction

For each ILP-decoded CJ $T$, the taker hides in a published
anonymity set of $n_{eq} = m + 1$ equal outputs (one per maker
plus the taker). The taker's anonymity reduces only when a
specific equal output of $T$ is individually attributed to a
specific maker; cluster membership of *slots* alone is not
enough, because within $T$ the equal outputs are indistinguishable
by amount. We therefore define a producer slot $s$ in CJ $T$,
with equal-output outpoint $O_s$, as *certified* iff at least one
of the following two paths holds.

**Path A (fee fingerprint).** $O_s$ is consumed by a maker slot
in some later CJ $S$, and the fee fingerprint of $\S 5.2$
identifies $s$ univocally inside $T$ (under the loose
either-fingerprint gate of $\S 5.2$). This is the only edge in
the clusterer that ties a specific equal output to a specific
producer slot.

**Path B (cluster co-spend).** $O_s$ is consumed by a maker slot
$s'$ in some later CJ $S$, $s'$ also consumes an input $u$ that is
the change output of a slot whose cluster is the same as $s$'s
cluster, and no other equal-output input of $s'$ anchors a rival
cluster. This is a downstream confirmation: a later CJ
demonstrates, by co-spend, that the equal output came from a
slot in $s$'s cluster. It contributes a small number of
additional certifications on top of Path A.

Cluster membership through change-chain, $\S 5.3$ CIOH or
$\S 5.5$ FB-funding by itself is *not* a certification. Those
edges merge maker slots across CJs (they establish "these two
slots have the same wallet") but they do not label any equal
output of the producer CJ $T$: within $T$, an outside observer
cannot tell two equal outputs apart without a fingerprint.

The residual anonymity set lower bound for round $T$ is

$$
k(T) \;=\; n_{eq}(T) \;-\; n_{\text{certified}}(T),
$$

where $n_{\text{certified}}(T)$ counts the producer slots of $T$
that are certified by Path A or Path B. The taker is never
certified (we have no chain evidence about who the taker is, so
they always remain in the residual set); the minimum value of
$k(T)$ is therefore 1.

We do not subtract more even when the taker's own slot happens to
chain forward, because the evidence does not distinguish "taker
who remixes" from "maker whose remix we observed". The reported
$k(T)$ is the **lower bound** on the true residual anonymity set.

### 7.1 Headline

Across the 7,400 ILP-decoded mainnet CJs in the 1y window:

| metric                                          | v6 (change-chain only) | v7 (Path A) | v7.1 (+ co-spend) | v7.2 (+ round-trip) | v7.3 (+ FB-funding) |
|-------------------------------------------------|-----------------------:|------------:|------------------:|--------------------:|--------------------:|
| mean published $n_{eq}$                         | 8.65                   | 8.65        | 8.65              | 8.65                | 8.65                |
| mean certified makers per CJ                    | 0.00                   | 0.40        | 0.43              | 0.43                | **0.43**            |
| mean residual anonymity set                     | 8.65                   | 8.25        | 8.22              | 8.22                | **8.22**            |
| share of CJs with at least one certified maker  | 0.0%                   | 33.6%       | 34.4%             | 34.5%               | **34.5%**           |
| median residual anonymity set                   | 9                      | 9           | 9                 | 9                   | 9                   |
| share of CJs reaching residual = 1 (taker alone)| 0.0%                   | 0.0%        | 0.0%              | 0.0%                | **0.0%**            |
| attribution edges (Path A, outpoints)           | 0                      | 2,996       | 2,996             | 2,996               | **2,996**           |
| Path-B-only credits                             | 0                      | n/a         | 295               | 295                 | **295**             |

The v6 column is the residual one obtains if change-chain alone
were used; under the corrected definition, v6 certifies no equal
outputs (it only clusters slots across CJs). The v7 column adds
Path A (the fee fingerprint), which attributes 2,996 outpoints
univocally to a producer slot. v7.1/v7.2/v7.3 add cluster edges
that enable a small number of additional Path-B credits.

The reduction is bounded: the fee fingerprint univocally
identifies 2,996 of the 27,043 cross-CJ equal-output reuses in
the window (11.1%). The change-chain clusters maker slots
extensively (10,630 non-trivial clusters out of 26,218 total),
but those clusters cannot, on their own, identify which equal
output came from which slot. The residual drops from 8.65 to
8.22 on average (a 5.0% reduction), 34.5% of CJs leak at least
one maker, and 0.0% reach residual = 1.

![residual anonymity set histogram (v7.3, 1y window)](figures/anonset_reduction_hist.svg)

The overlay across all five iterations makes the per-iteration
contribution visible:

![v6 through v7.3 anonset overlay](figures/v6_vs_v7_anonset_overlay.svg)

Under the corrected residual metric (Path A plus univocal Path B),
the bulk of the reduction comes from the v6 -> v7 step, where the
fee-fingerprint attribution edges (Path A) become available: mean
residual drops from 8.62 (v6, Path B only) to 8.22 (v7). The
later iterations v7.1, v7.2 and v7.3 add cross-CJ cluster edges
via CIOH, round-trip hops and fidelity-bond funding respectively;
each adds a small number of additional univocal Path B
certifications (292, 294, 295 across the 1y window) but the mean
residual moves by less than 0.001 because most newly clustered
maker slots are not paired with a same-cluster change anchor at a
downstream consumer CJ. The taker-facing implication is that the
fee fingerprint, not the cluster graph, drives most of the
anonymity-set reduction.

### 7.2 Per-$n_{eq}$ breakdown

The reduction holds across every round size in the corpus:

![mean anonset before and after, by n_eq](figures/anonset_per_n_eq.svg)

The grey bars are the published anonymity sets the taker thinks
they hide in; the red bars are the v7.3 lower-bound residual
after certified makers are removed. The residual tracks $n_{eq}$
closely, decreasing by under one candidate on average across the
entire range from $n_{eq} = 3$ to $n_{eq} = 17$. Larger rounds
publish larger sets and retain larger residuals; the reduction
is roughly constant in absolute terms, not in relative terms.

### 7.3 What drives the residual

The residual anonymity set has two structural sources:

1. **The true taker.** Always one slot. The protocol guarantees
   exactly one taker per CJ, and the taker is never certified.
2. **Makers whose equal output is not univocally attributed.**
   The fee fingerprint disambiguates only when one slot in the
   producer CJ has a fee policy that no other slot in $T$ would
   have charged at the consumer's amount. Of the 27,043 cross-CJ
   equal-output reuses in the window, 2,996 (11.1%) are
   univocally attributed; the rest are either ambiguous (1,032),
   matched but corpus-non-unique under the strict gate, or
   produce no fingerprint match at all. Path B contributes only
   295 additional certifications.

Two factors would shift the histogram leftward (smaller
residuals) under the same threat model:

- **A larger crawl frontier or higher ILP time budget.** Some
  cross-CJ reuses are dropped because the consumer slot's CJ is
  among the 3,181 ILP failures of $\S 4$, and some producer CJs
  are outside the window. Both effects under-report Path A
  attributions.
- **Greater fee-policy diversity.** Counterintuitively, a wider
  fee grid makes more producer slots uniquely fingerprintable;
  the maker population that runs the reference client's default
  policy is *less* attackable (their fingerprint collides with
  every other default-policy maker). The simulator results of
  $\S 9.4$ quantify this.

## 8. Role-change taker exposure (supplementary)

A taker who later participates as a maker in a future CJ $S$ may
leak their cross-round identity if one of the taker's equal
outputs from $T$ is consumed by a slot of $S$ and that slot is
univocally attributable (Path A of $\S 7$) back to a producer
slot of $T$. The attack is the §7 attribution rule applied with
the taker's equal output in the producer role: if the fee
fingerprint at $S$'s consumption matches a slot of $T$, that
producer slot of $T$ is the taker (the only slot of $T$ whose
equal output is the one being spent).

In the 1y corpus, this concrete role-change exposure (the
forward equal output of $T$ is in a downstream Path A
attribution) accounts for at most a few hundred CJs and is a
strict subset of the 2,996 Path A attribution edges of $\S 7$.
We mention this for completeness; the $\S 7$ anonymity-set
reduction is the structurally stronger attack in this paper.
The role-change exposure depends on the additional event of a
taker becoming a maker; the $\S 7$ reduction applies on every
CJ regardless.

## 9. Countermeasures

The attack of $\S 7$ has a single load-bearing chain edge: the
fee fingerprint of $\S 5.2$ that ties a specific equal output of
producer CJ $T$ to a specific producer slot of $T$ (Path A). The
change-chain (v6) and the auxiliary edges (CIOH, FB-funding,
co-spend) of $\S 5.3$-$\S 5.5$ are *cluster-merging* edges: they
group maker slots across CJs into wallet-shaped clusters, but
they cannot, on their own, label which of the indistinguishable
equal outputs of $T$ belongs to which slot. Closing Path A
closes the user-facing $\S 7$ residual reduction. The
change-chain matters only insofar as Path B of $\S 7$ uses
cluster co-spend to confirm a small number of additional
attributions ($295$ of the $3{,}206$ certified slots in the 1y
corpus).

The deployable countermeasure is therefore
**fee-policy homogenization**: every maker advertises the
reference client's default policy. The fee fingerprint of
$\S 5.2$ collapses, no producer slot is univocally identifiable
from its fee, and Path A produces zero attributions. The mainnet
orderbook today is too varied for the fingerprint to fail
naturally; a coordinated client default change is what makes it
fail.

### 9.1 What an analyst actually sees

The on-chain artefacts that the $\S 5$ pipeline exploits,
ordered by their contribution to the user-facing residual
anonymity set (smaller is better for the attacker):

1. **Within-CJ fee fingerprint (Path A, signal 1).** The only
   chain edge that labels an equal output with a specific
   producer slot. A maker on a unique fee policy under the
   wide mainnet fee grid leaks the equal-output owner
   permutation through ILP recovery; a maker running the
   reference client's default policy collides with the other
   default-policy makers and is locally ambiguous, so its
   equal output is *not* univocally attributable. This is the
   one signal whose suppression alone moves the residual to
   the ceiling $n_{eq}$ ($\S 9.4$).
2. **Cluster co-spend (Path B, signal 2).** A downstream slot
   that spends a producer-CJ equal output of $T$ alongside a
   change output of another slot in the same v7.3 cluster
   confirms, by co-spend, that the equal output came from a
   slot in that cluster. Path B contributes only $295$
   additional certifications on top of Path A's $2{,}996$ in
   the 1y corpus.
3. **Change reuse (v6 chain edge).** A maker's change UTXO is
   spent as an input of a later CJ slot owned by the same
   maker. This is structurally unavoidable in JoinMarket as
   deployed: the maker's input selection follows the richest
   mixdepth and the change is emitted back into that same
   mixdepth, so on a long enough horizon the change becomes an
   input again. The change-chain *clusters slots* but does not
   attribute equal outputs. Its contribution to the $\S 7$
   residual is only the indirect Path B credits.
4. **Non-CJ CIOH and FB-funding edges (v7.1, v7.2, v7.3).**
   $\leq 2$-output non-CJ transactions that spend a maker's
   UTXOs ($\S 5.3$-$\S 5.4$), and the FB-funding back-walk of
   $\S 5.5$. These also widen the wallet boundary around an
   already-clustered set of slots; they contribute Path B
   credits in principle and 0 additional credits in practice
   on the 1y corpus (the Path B count is already saturated by
   v7.1's co-spend edge).

The user-facing per-CJ residual anonymity set ($\S 7$) is
sensitive to signal (1) above all else: Path A alone produces
$2{,}996$ of the $3{,}206$ certifications in the 1y corpus
(93.5%). Suppress signal (1) and the residual rises to
$n_{eq}$ minus the small Path B contribution; suppress signals
(2)-(4) without (1) and the residual barely moves.

### 9.2 What does not work, and why

Several measures are sometimes suggested as countermeasures
but do not, on their own, reduce the user-facing residual
under our threat model:

- *Relocate change to a different mixdepth* or *use a fresh
  wallet derivation between rounds*. The analyst's chain edge
  is keyed on UTXO identity, not on mixdepth or xpub branch.
  None of the cluster-merging edges (v6, v7.1, v7.2, v7.3)
  attribute equal outputs to slots either, so the $\S 7$
  residual is unaffected.
- *Batch-consolidate change UTXOs in a non-CJ tx* before the
  next round. Combining several change UTXOs into a single
  output before the next CJ collapses same-wallet UTXOs into
  one cluster, which is the attack's premise; it does not
  break Path A.
- *Refuse to spend a JM-emitted change UTXO as a CJ input*
  ("no change as input"). Closes the v6 cluster-merging edge
  and a fraction of Path B but not Path A; the simulator of
  $\S 9.4$ shows that this is *counterproductive* on its own:
  it forces makers to spend their equal outputs more
  aggressively, increasing the Path A attack surface (e.g.
  the `no_change_as_input` + `maker_only_cj` variant lands at
  residual $4.04$, *worse* than the baseline's $4.51$, with
  $24{,}133$ Path A attribution edges versus the baseline's
  $8{,}982$).
- *Use a JM-disjoint fidelity-bond wallet* (66 of 95 FBs in
  our snapshot already do). This closes the $\S 5.5$
  FB-funding edge against a passive analyst that does not
  interact with the orderbook. Useful as a recall-tightening
  hygiene measure, not as a residual-anonset countermeasure.
- *Route non-CJ spends through batched
  ($\geq 3$-output) transactions.* Closes the $\S 5.3$-$\S 5.4$
  CIOH channel but, as a cluster-merging signal, does not move
  the residual.
- *Run the maker over Tor only.* The whole attack runs on
  the public chain.

### 9.3 Future protocol-level mitigations

A common suggestion is that the JoinMarket protocol should
add a cryptographic per-round commitment that hides which
producer slot emitted which equal output (e.g. the JMP-0005
ZKP proposal). Under our threat model this *would* close the
attack: the $\S 5.2$ fee fingerprint relies on the ILP
recovery of $\S 5.1$ to identify which slot emitted which
equal output, and the ILP recovery in turn uses the
public per-output offer mapping in the maker `!ioauth`
message and the published fee schedule. A commitment that
hides the equal-output permutation while still proving fee
correctness removes Path A by construction; Path B is
unaffected (it does not depend on the permutation being
public), and the residual lower bound becomes $n_{eq}$ minus
the small Path B contribution. We do not model JMP-0005 in
the simulator of $\S 9.4$ because the deployable defense
(uniform fees, the same effect on Path A) is already
sufficient to drive the simulator residual to $n_{eq}$.

### 9.4 Countermeasure simulator evaluation

We enumerate the $2 \times 2 \times 2 = 8$ simulator
configurations over the three knobs a JoinMarket maker client
can actually expose:

* **`uniform_fee`** (Tier A, configuration). Every maker
  advertises the reference client's default policy. Kills
  the fee fingerprint (signal 1) at the cost of removing
  inter-maker fee competition.
* **`no_change_as_input`** (Tier B, behavioral). Makers
  refuse to spend a JM-emitted change UTXO as a subsequent
  CJ input. Closes signal (3) (the v6 edge) and most of
  Path B (signal 2).
* **`maker_only_cj`** (Tier B, behavioral). The protocol
  periodically emits a synthetic CJ that consumes one
  held-back change UTXO per participant and produces fresh
  equal outputs that re-enter the regular CJ input pool. We
  schedule one synthetic CJ per four taker-driven CJs with
  five participants per synthetic CJ.

Each run uses 150 seed makers, 5 batches of 4,000 takers
each, 10% maker churn between batches, 5 makers per CJ
($n_{eq} = 5$), and the wide fee grid that matches the
mainnet orderbook snapshot. Precision against simulator
ground truth is 1.0 in every variant. The clusterer is v7.3.

| variant                                              | n_eq | certified | residual | share res=1 | share any cert | Path A | Path B |
|------------------------------------------------------|-----:|----------:|---------:|------------:|---------------:|-------:|-------:|
| `uniform_fee`                                        | 5.00 |      0.00 | **5.00** |       0.00% |          0.00% |      0 |      0 |
| `uniform_fee` + `maker_only_cj`                      | 5.00 |      0.00 | **5.00** |       0.00% |          0.00% |      0 |      0 |
| `uniform_fee` + `no_change_as_input`                 | 5.00 |      0.95 |     4.05 |       0.66% |         65.43% |    717 |      0 |
| `uniform_fee` + `no_change_as_input` + `maker_only_cj` | 5.00 |    0.68 |     4.32 |       0.36% |         45.99% | 16,970 |      0 |
| baseline (today's JM)                                | 5.00 |      0.49 |     4.51 |       0.03% |         38.35% |  8,982 |    794 |
| `maker_only_cj`                                      | 5.00 |      0.49 |     4.51 |       0.03% |         38.35% |  8,982 |    794 |
| `no_change_as_input`                                 | 5.00 |      0.45 |     4.55 |       0.13% |         34.09% |    339 |      0 |
| `no_change_as_input` + `maker_only_cj`               | 5.00 |      0.96 | **4.04** |       0.82% |         59.98% | 24,133 |      0 |

![Mean residual anonymity set across the eight simulator
variants. `uniform_fee` (alone or paired with `maker_only_cj`)
is the only configuration that drives the residual to the
$n_{eq} = 5$ ceiling. The `no_change_as_input` family is
*counterproductive*: by removing the v6 cluster-merging edge
without touching the fee fingerprint, it forces makers to spend
equal outputs more aggressively, increasing Path A attribution
counts.](figures/countermeasure_effectiveness.svg)

Four observations:

1. **`uniform_fee` is the deployable defense.** It is the only
   configuration that drives the residual to $n_{eq}$ on
   every CJ, certifies no makers, and produces zero Path A
   attributions. Pairing it with `maker_only_cj` does not
   change the result (Path A is already shut). Pairing it
   with `no_change_as_input` reopens the attack (see
   observation 3 below).
2. **`no_change_as_input` is counterproductive.** Removing the
   v6 cluster-merging edge does *not* reduce Path A. Worse,
   the behavioral effect of "do not spend change as input"
   forces makers to spend their equal outputs more often,
   inflating the cross-CJ equal-output reuse pool and the
   Path A attribution count. The `no_change_as_input` +
   `maker_only_cj` variant lands at residual $4.04$, *worse*
   than the baseline's $4.51$, with $24{,}133$ Path A edges
   versus the baseline's $8{,}982$.
3. **`uniform_fee` + `no_change_as_input` is also
   counterproductive.** Even with the fee fingerprint
   collapsed, the simulator's strict fee-collision gate still
   admits a small number ($717$, then $16{,}970$ when
   `maker_only_cj` is added) of edge cases where a default-
   policy maker's effective fee happens to differ from
   another's because the slot amounts differ; this residual
   leak through `uniform_fee` is small in absolute terms but
   it interacts badly with `no_change_as_input`. The clean
   defense is `uniform_fee` alone.
4. **Throughput is preserved in the deployable defense.**
   `uniform_fee` does not change the simulator's taker-CJ
   throughput (the synthetic `maker_only_cj` stream is not
   needed). The only configurations with throughput issues
   are `no_change_as_input` alone (makers run out of
   spendable change; we did not measure throughput
   degradation in this version of the simulator but the
   liquidity-starvation argument of the v3 simulator
   continues to apply).

The qualitative answer is that the JoinMarket protocol is
*hardenable today*, by a coordinated client-default change.
Three deployment positions can be defended on the basis of
the table:

* **Today (no protocol change, no client change):** the
  user-facing residual under the $\S 5$ passive on-chain
  adversary is $4.51$ in the simulator and $8.22$ on the 1y
  mainnet corpus. $0.0\%$ of mainnet CJs reach residual $= 1$.
* **Client patch (`uniform_fee` default):** the user-facing
  residual rises to $n_{eq}$ in the simulator and would rise
  to $n_{eq}$ minus the small Path B contribution on
  mainnet. This is the recommended deployment.
* **Protocol change (JMP-0005-class ZKP):** removes Path A
  by construction (hides the equal-output permutation behind
  a zero-knowledge proof). On top of `uniform_fee` it
  defends in depth against an adaptive adversary that
  probes the orderbook as a taker; on its own (without
  `uniform_fee`) it has the same effect as `uniform_fee`
  alone on the $\S 7$ residual.

Simulator caveats. The simulator does not model non-CJ maker
spends, so the $\S 5.3$-$\S 5.4$ CIOH edges and the $\S 5.5$
FB-funding edges cannot fire in any variant. On the mainnet
corpus these contribute zero additional Path B credits
beyond v7.1's co-spend edge, so the simulator's *relative
ordering* across countermeasures matches the live attack.
The simulator also does not model an adaptive adversary that
probes the orderbook as a taker.

## 10. Limitations and future work

- The corpus is a finite snapshot. The 29.3% ILP failure rate is
  the dominant residual; with a higher per-tx ILP budget (10s,
  30s) the decoded fraction would climb and the residual
  anonymity set would shrink further. We chose 2 s/tx to keep
  the full corpus pass under 15 minutes on 14 cores.
- v7 produces a fee-fingerprint attribution edge ($\S 7$ Path A)
  only when the fingerprint is unambiguous within the producer
  CJ. Of the 27,043 cross-CJ equal-output reuses in the 1y
  window, 2,996 are univocally attributed (11.1%) and the rest
  are either ambiguous (multiple producer slots share the same
  fee under at least one interpretation), disagree between
  absolute and relative interpretations, or fail to match at all.
  These non-univocal edges are deliberately dropped to preserve
  precision = 1.0. A complementary per-CJ commitment scheme that
  publishes the equal-output owner permutation in zero knowledge
  (e.g. JMP-0005) would resolve these residual cases by removing
  the analyst's disambiguation channel entirely.
- The per-CJ univocal gate is a *heuristic* under per-announcement
  fee jitter. The scaled simulator (\u00a76.1) shows that in the
  varied/blinded regime the loose gate produces 136 multi-owner
  clusters out of 4,620 (2.9% precision-violating clusters) and
  the strict variant reduces this to 98 (28% reduction) without
  eliminating violations. The corpus-unique gate, which requires
  the chosen producer slot to be free of fingerprint
  doppelgangers anywhere in the corpus, is the conservative
  choice that restores precision = 1.0 at a recall cost of ~5%.
  On the mainnet corpus the four gates pass the cross-nick check
  identically (0 violations, same 35/72 matched nicks, same
  40/101 matched UTXOs); the corpus-unique gate splits 5,285
  extra clusters but none across a probed nick boundary, so the
  empirical precision of all gates is identical at the current
  corpus density. An adversarial setting (a Sybil deliberately
  jittering fees to forge edges) should switch to corpus-unique.
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
  probe data (§6.2) suggests this is not the dominant pattern,
  but a direct count is not yet available.
- Active-adversary edge (probed nick to advertised UTXO).
  We prototyped a v7.4 layer that unions any v7.3 clusters whose
  inputs or change outputs match the UTXOs advertised by a single
  nick during probing (the §6.2 probe corpus, 72 nicks across
  101 advertised UTXOs; per §2 the published artifact is reduced
  to per-nick counts, the per-UTXO mapping is kept local). Four
  nicks span multiple v7.3 clusters, yielding four safe unions
  over nine clusters (delta -5 clusters, 68,998 to 68,993) with
  zero forbid conflicts and zero precision violations. The mean
  residual anonymity set is
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
passes the zero-cross-nick-collision check under the loose gate
(precision = 1.0 against the §6.2 probe corpus). The newer
merges concentrate in older corpus regions where more chain and
non-CJ CIOH edges have time to fire, away from the probed-nick
set of recent live makers. The four nicks v7.3 merges are
orderbook-only nicks not present in the probe set, so the
probe-side precision check is independent evidence that v7.3 did
not over-merge. Future improvements should target ILP recall
(more decoded CJs across the long tail of larger rounds) and
multi-hop backward walks from FB-funding txs through chains of
non-CJ ancestors.

## 11. Conclusion

The JoinMarket equal-output anonymity set as published per round
($n_{eq}$) overstates the protocol's privacy budget against a
passive on-chain adversary, but the overstatement is bounded.
A protocol-correct chain-following clusterer at precision = 1.0
reduces the published anonymity set from a mean of 8.65 to 8.22
on the 1y mainnet corpus, with 34.5% of CJs losing at least one
candidate to certified-maker removal and 0.0% of CJs reaching
residual = 1. The structural channel the attack exploits, namely
JoinMarket's fee-fingerprint signal ($\S 5.2$) that ties a
specific equal output of producer CJ $T$ to a specific producer
slot of $T$, is intrinsic to the protocol and to the typical
maker fee-advertisement workflow; it is not a fixable
implementation bug.

The precision = 1.0 guarantee is what makes the result
actionable: under the per-CJ loose gate the clusterer never
merges two distinct maker wallets on this corpus, validated by
three independent ground-truth sources. Each certified maker the
analyst extracts is a *deterministic* hide-set reduction, not a
probabilistic one. The precision guarantee is gate-and-corpus
dependent: the scaled simulator ($\S 6.1$) shows that under
adversarial fee jitter the per-CJ gate breaks at $\sim 3\%$ of
edges and only the corpus-unique gate restores precision = 1.0
by construction. On the present mainnet snapshot the gates are
empirically indistinguishable; on a future snapshot in which an
adversary deliberately concentrates many makers near similar
fee policies the analyst should switch to the corpus-unique
gate at a recall cost of about 5%.

The practical implication for JoinMarket users is that the
relevant privacy figure for a round is not its published
$n_{eq}$ but the v7.3 residual: today, around 95% of $n_{eq}$.
The simulator ($\S 9.4$) identifies the deployable mitigation:
**fee-policy homogenization** (`uniform_fee`, every maker on the
reference client's default policy) drives the residual to the
full $n_{eq}$ ceiling and produces zero Path A attributions.
The behavioral knobs that the previous version of this paper
proposed (`no_change_as_input`, `maker_only_cj`) are at best
neutral and at worst *counterproductive*: by suppressing the
v6 change-chain cluster-merging edge without touching the fee
fingerprint, they force makers to recycle equal outputs more
aggressively and *increase* the Path A attribution surface. The
protocol is therefore hardenable today, by a coordinated client
default change, without any protocol-level cryptographic
addition; a future JMP-0005-class equal-output permutation
commitment would close the residual leak in depth against an
adaptive adversary. Until a JoinMarket client release ships the
`uniform_fee` default, the v7.3 residual is the privacy budget
the protocol gives its users.
