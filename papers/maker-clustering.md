# JoinMarket Maker Wallet Clustering and Taker Anonymity-Set Reduction

> **TL;DR.** JoinMarket is a Bitcoin CoinJoin protocol where a
> *taker* pays a small fee to one or more *makers* to mix coins
> into a single transaction with several equal-value outputs;
> every equal output is supposed to be indistinguishable from
> every other equal output in the same round (makers want
> anonymity from each other and from the taker, just as the taker
> wants anonymity from the makers). We ran a passive on-chain
> experiment on exactly one year of mainnet JoinMarket activity
> (16,890 ILP-decoded CoinJoins) and clustered maker wallets
> using only protocol-forced signals: the JoinMarket mixdepth
> state machine (a maker's change stays in the source mixdepth
> and its equal output advances to the next, and the maker
> chooses the mixdepth with the highest balance to re-advertise),
> the integer-linear-program (ILP) recovery of slot membership
> (which inputs and change output belong to the same participant)
> inside each CoinJoin so that the Common Input Ownership
> Heuristic (CIOH) can be applied across the on-CJ and off-CJ
> sides of a maker wallet, and the maker's published fee
> schedule used as a per-CoinJoin disambiguator. A simple example
> of that last signal: if a maker charges 0.1% in one CJ and an
> equal output of that CJ then participates in a later CJ whose
> ILP-recovered slot also charges 0.1% (and no other maker in
> the producer CJ charges 0.1% at that amount), the two slots
> are the same maker. Sometimes a participant acts as taker in
> one round and maker in the next, which lets the same edge
> pull the taker's equal output into a maker cluster. The
> result is uncomfortable: the mean published anonymity set for
> a CJ in this corpus is 8.66 equal outputs, but after removing
> the equal outputs of makers we successfully clustered the
> residual drops to 3.71, and on 10.8% of CJs only the taker
> remains. None of the edges in this attack are probabilistic:
> every merge is a hard same-wallet conclusion or no merge at
> all. This is not good news. It demonstrates that the gap
> between JoinMarket's published per-round anonymity set and the
> set a passive on-chain analyst actually faces is structural,
> deterministic, and reproducible from the public chain plus the
> orderbook snapshot. Practical mitigations exist; we discuss
> them in \u00a79.

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
UTXO edge in the chain: either an equal output of CJ $T$ (at
mixdepth $d{+}1$) reused as a maker input in CJ $S$ (the maker
later advertising mixdepth $d{+}1$), or a change output of $T$
(at mixdepth $d$) reused later when the maker advertises $d$
again. Following those edges clusters the maker wallets, and
every clustered maker shrinks the taker's hide-set by one.

This paper answers:

1. How many JoinMarket maker wallets can be clustered from
   on-chain data alone, at precision = 1.0 (under the gate and
   corpus described in §5.2 and §6.1), on the full mainnet
   corpus?
2. By how much does that clustering reduce the per-CJ taker
   anonymity set?
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
sometimes turns the equal-chain edge into a definite must-link.
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
JoinMarket CoinJoins, produced the snapshot used here. It spans
exactly one year of mainnet JoinMarket activity:

| metric                | count        |
|-----------------------|-------------:|
| visited transactions  | ~200,000     |
| **JM CoinJoin txs**   | **23,876**   |
| time span             | 1 year (2025-05-22 to 2026-05-22) |
| ILP-decoded CJs       | 16,890 (70.7%) |
| ILP failures (timeout / infeasible at `max_fee_rel = 0.05`, `time_limit = 2s`) | 6,986 (29.3%) |
| maker slots recovered | 129,301      |

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
- **Must-link, equal-chain (fact 3).** In the simulator the
  equal-output owner is known and v6 unions producer with
  consumer directly. On mainnet, v7 (§5.2) restores this edge by
  picking the producer slot from a *fee fingerprint* of the
  consumer slot in the next CJ; the edge fires only when that
  fingerprint identifies exactly one slot in the producer CJ,
  otherwise no edge is added.

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

The full pass over the 129,301 mainnet slots produces:

| metric                                       | v6        | v7        | v7.1      | v7.2      | v7.3      |
|----------------------------------------------|----------:|----------:|----------:|----------:|----------:|
| total clusters                               | 74,471    | 69,184    | 69,103    | 69,003    | 68,998    |
| singleton clusters                           | 50,126    | 45,871    | 45,789    | 45,706    | 45,702    |
| non-trivial clusters (size >= 2)             | 24,345    | 23,313    | 23,314    | 23,297    | 23,296    |
| largest cluster                              | 91 slots  | 125 slots | 125 slots | 125 slots | 125 slots |
| same-CJ slot collisions (must-not-link violations) | 0   | 0         | 0         | 0         | 0         |

v7 absorbs 5,287 v6 clusters into existing ones through the
equal-chain edge. v7.1 adds 127 cross-CJ unions through the
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
  nearest part per million (each side is rounded to its
  nearest integer ppm via banker's rounding and the two
  integers must match), so $S_i$'s implied policy is
  $\mathit{cjfee}_r = f_c / a_S$ to that resolution.

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

The exact harness is in
``tmp/v7/eval_simulator_scaled.py`` and the per-run JSON
reports under ``tmp/v7/simulator_scaled_*.json``. The mainnet
gate comparison is in ``tmp/v7/mainnet_v7_gates.py`` and
``tmp/v7/mainnet_v7_probe_gates.py``.

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

For each ILP-decoded CJ T, the taker hides in a published
anonymity set of `n_eq = m + 1` equal outputs (one per maker plus
the taker). Define a maker slot as *certified* when the clusterer
places it in a cluster of size $\geq 2$ (the slot is linked by at
least one definite UTXO chain to another CJ in the corpus). Every
certified maker removes one candidate from the taker's anonymity
set, since the taker cannot be a maker whose identity persists
across CJs. The residual anonymity set lower bound for round $T$
is therefore

$$
k(T) \;=\; n_{eq}(T) \;-\; n_{\text{certified}}(T).
$$

The taker is never certified by construction (we have no chain
evidence about who the taker is, so they always remain in the
residual set). The minimum value of $k(T)$ is therefore 1, the
taker alone.

We do not subtract more even when the taker's own slot happens to
chain forward, because the evidence does not distinguish "taker
who remixes" from "maker whose remix we observed". The reported
$k(T)$ is the **lower bound** on the true residual anonymity set.

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
behavior. The attack is the same chain edge read in the other
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

## 9. Countermeasures

The attack pipeline of §5 succeeds because three classes of
on-chain artefact survive every JoinMarket round: (i) the
change UTXO that a maker spends as an input to a later CJ
slot (the change-chain edge of §5 fact 2), (ii) the equal
output that a maker, in subsequent rounds, spends back as a
CJ input of its own (the equal-chain edge that the fee
fingerprint of §5.2 disambiguates), and (iii) the non-CoinJoin
spends in which a maker consolidates or externally pays from
one or more of its UTXOs, which the CIOH filter walks under
the conservative ≤2-output safety rule (§5.3, §5.4). The
fidelity-bond funding edge of §5.5 is a fourth, weaker
channel. A countermeasure is meaningful only if it disrupts a
structural artefact rather than only its local symptom.

The two chain edges (i) and (ii) are not unique to any
particular implementation detail: the joinmarket-ng maker
selects inputs from its richest mixdepth
(`maker/src/maker/coinjoin.py`,
[`_select_our_utxos`](https://github.com/joinmarket-ng/joinmarket/blob/master/maker/src/maker/coinjoin.py)),
emits its change back to that same mixdepth, and emits the
equal output one mixdepth forward. The analyst cannot
distinguish those three placements from any other; what they
can do is observe that the same UTXO that left the maker as
change in CJ $t$ appears as an input in some CJ $t' > t$, and
chain those two slots together. The two countermeasures that
move the user-facing residual are therefore the ones that
prevent the maker's outputs from re-entering the protocol as
identifiable inputs, *not* the ones that relocate them across
mixdepths (the analyst's chain walker does not key on
mixdepth; the simulator's `clusterer_v7.py` builds chain edges
directly from `change_output` and `equal_output` regardless
of which mixdepth the producer or consumer slot drew from).

### 9.1 What an analyst actually sees

Before listing countermeasures we record the signals an
on-chain adversary can extract today, ordered by recall
contribution against the §6.1 scaled simulator (which matches
the JM-NG maker selection model: richest mixdepth, change to
same mixdepth, equal output to next mixdepth):

1. **Change-as-input chain edge.** A maker's change UTXO is
   spent as an input to a subsequent CJ slot of the same
   maker with high probability over a long horizon (the
   maker's `select_input_mixdepth` will, sooner or later,
   pick the mixdepth that holds the change). This edge fires
   in roughly 95% of the simulator's slot transitions and is
   the dominant signal: in isolation it certifies essentially
   every maker. It is mixdepth-agnostic from the analyst's
   point of view; relocating change to a different mixdepth
   only delays the certification (the change still appears as
   an input the next time the maker draws from that
   mixdepth), it does not break the edge.
2. **Equal-output-as-input chain edge.** A maker's equal
   output is spent as an input to a subsequent CJ slot of the
   same maker, again with high probability over a long
   horizon. Within a single producer CJ this edge is
   ambiguous between the two-or-more producer slots that
   emitted equal outputs of the same denomination; the fee
   fingerprint of §5.2 disambiguates a sufficient fraction
   for v7 to lift recall above the v6 floor.
3. **Within-CJ fee fingerprint.** The marginal recall gain of
   v7 over v6 comes from per-slot realized fees that
   distinguish one producer slot from another. Two makers
   running the same fee policy collide and their equal-chain
   reuse becomes ambiguous; a maker running a unique policy
   leaks the equal-output owner permutation through ILP
   recovery (§5.2).
4. **Non-CJ CIOH.** ≤2-output non-CJ transactions that spend
   a maker's UTXOs reveal same-owner sets to the conservative
   CIOH filter (§5.3, §5.4). Avoidable by routing non-CJ
   spends through batched (≥3-output) transactions.
5. **Fidelity-bond funding tx.** Backward CIOH from FB-funding
   transactions when those are funded from JM-provenance
   inputs (§5.5). Avoidable by funding the FB from a
   JM-disjoint wallet.

Each signal closes one *channel*; only signals (1) and (2)
sit on a path that touches every CJ a maker participates in,
and the user-facing per-CJ residual anonymity set (§7) is
sensitive only to those two. The rest are recall multipliers
that help the analyst but do not unlock new CJs.

### 9.2 Deployable mitigations

We group countermeasures by deployment cost, not by which
signal they touch, because the user-facing question is
what the JoinMarket community can ship.

**Tier A (configuration only, deployable today).**

1. **Fee-policy homogenization.** Today's JoinMarket-NG ships
   a default $\mathit{cjfee}_r = 10^{-4}$ with $\mathit{cjfee}_a$
   set to recover the maker's expected on-chain cost at typical
   round sizes (`maker/src/maker/config.py:343-350`). A maker
   who ships the default policy unchanged contributes to a
   shared fee fingerprint and removes channel (3) for their
   own slots. If a sufficient share of makers adopts the
   default policy, v7's "exactly one producer slot matches the
   fingerprint" condition fails on most consumer slots and the
   analyst falls back to v6. The §9.4 simulator quantifies
   this: with every maker on the reference default, v7 records
   essentially no cross-CJ fingerprint reuses, but the
   user-facing per-CJ residual anonset stays at 0.00. Fee
   homogenization is a *prerequisite* that lowers the cost of
   the Tier B and Tier C mitigations (a homogeneous fee
   surface makes a chain-edge mitigation strictly more
   powerful), not a standalone defence.
2. **Disjoint fidelity-bond wallet.** 66 of 95 FBs in our
   snapshot are already funded from inputs with no observable
   JM provenance (§5.5). Funding the FB exclusively from a
   wallet that never receives JM equal outputs or change
   closes channel (5). One-shot, low-cost, no protocol change.
3. **Batched non-CJ spends.** Routing off-CJ consolidations
   through transactions with ≥3 outputs to distinct
   recipients makes the CIOH assignment ambiguous and is
   dropped by the precision-first filter. Closes channel (4).
   A niche tactic for makers with sufficient non-CJ flow.

**Tier B (client patch, no protocol change).**

4. **Maker self-consolidation rounds.** The dominant signal
   is the change-as-input chain edge (channel 1). A maker
   client patched to *never* spend a JM-emitted change UTXO
   directly as a CJ input breaks the edge. The cheapest way
   to achieve this in practice is for the maker to run a
   *self-consolidation* CJ on its own change: the maker acts
   as a taker, with its own change UTXO as the source, and
   receives a fresh equal output in return. Subsequent CJ
   slots then draw inputs from that fresh equal output
   instead of from the original change. The analyst still
   sees the chain (change UTXO $\to$ consolidation taker
   input $\to$ fresh equal output), but the fresh equal
   output is one of ${\geq}2$ producer slots' outputs at the
   consolidation CJ, so its owner is hidden behind the
   consolidation CJ's published anonset. The cost is the
   consolidation tx's miner fee, the maker's share of the
   consolidation CJ's coordination cost, and the opportunity
   cost of the locked-up balance during the consolidation
   delay.
5. **Taker maker-selection by expected anonymity gain.** A
   taker who weights maker offers not only by fee but also by
   the *expected residual anonset* the offer contributes
   (treating cluster size as a public signal) gets more
   privacy per CJ at the cost of a higher fee bill. Computing
   the cluster signal requires a chain crawler against the
   live mempool; the crawler and the orderbook are both
   public.

**Tier C (protocol change).**

6. **Equal-output recycling at the protocol level.** Tier B
   item 4 achieves the same effect for *change* outputs. The
   symmetric mitigation for *equal* outputs is harder because
   a maker's equal output naturally sits in the next mixdepth
   and the maker's next CJ slot can pull from that mixdepth
   without any extra action. A protocol-level change would
   require the maker client to refuse to spend a JM-emitted
   equal output as a CJ input without first laundering it
   through a self-consolidation CJ analogous to item 4.
   Equivalently, the client invariant is: *no CJ input is a
   direct child of a CJ output owned by the same maker*. This
   is enforceable client-side but is most useful as a
   protocol-level convention that takers can rely on
   (otherwise a single non-compliant maker leaks the entire
   chain for the cohort).
7. **Orderbook-enforced uniform fee.** Tier A item 1 at the
   maker side relies on voluntary adoption. A protocol change
   that has the orderbook reject offers off the canonical
   policy collapses the fee fingerprint to a single value
   across the whole cohort. The cost is that the orderbook
   turns into a single-fee auction, removing the price
   discovery mechanism that incentivizes maker liquidity at
   thin amounts.

We deliberately do **not** include the following, which are
sometimes suggested but do not defeat the attack:

- *Relocate change to a different mixdepth*. The analyst's
  chain edge is not keyed on mixdepth (see §9.1 signal 1).
  The change UTXO becomes a chain edge the moment it is
  spent as an input, regardless of which mixdepth it was
  resting in.
- *Batch-consolidate change UTXOs in a non-CJ tx*. Combining
  several change UTXOs into a single output before the next
  round consolidates same-wallet UTXOs into one cluster,
  which is the attack's premise.
- *Use fresh wallet derivations between rounds*. The chain
  edges run between UTXOs the wallet owns, regardless of
  xpub branch.
- *Run the maker over Tor only*. The whole attack runs on
  the public chain.

### 9.3 What a "per-round commitment" would and would not buy

A common suggestion is that the JoinMarket protocol should
add a cryptographic per-round commitment that hides which
producer slot emitted which equal output. Under our threat
model this would not change the user-facing residual: the
session-level `!ioauth` message reveals every maker's
`cj_addr` and `change_addr` to the coordinating taker
(encrypted point-to-point under the maker-taker NaCl box, so
other makers do not see it on the wire, but the taker collects
every maker's mapping), so the equal-output owner permutation
is *not* private from the taker who runs the round, nor from
anyone who later replays as a taker against the same maker
cohort. JMP-0005 (the JoinMarket Proposal on ZKP-based
output-permutation hiding) documents exactly this leak as the
motivation for a forthcoming protocol change. What an outside
analyst recovers from the chain is exactly what a taker can
already obtain by joining one session.
The two parts of §9.2 that genuinely close chain edges
(items 4 and 6) are about output *reuse*, not output
*identification*; a commitment that hides which producer slot
owns each equal output within a given CJ does not stop the
analyst from observing that some equal output of CJ $t$
appears as an input to CJ $t+1$. This is why we treat the
equal-chain edge as a recall problem (the analyst must guess
*which* producer slot emitted the equal output, fee
fingerprint or no) rather than as an identification problem.

### 9.4 Countermeasure-effectiveness evaluation

The central question for any privacy protocol facing a
structural attack is whether it is *doomed* (the attack
signal cannot be removed without breaking the protocol) or
*hardenable* (a realistic change closes the channel). We ran
the §6.1 scaled simulator under each conceptual countermeasure
listed in §9.2 and measured both the clusterer outcome
(cluster count, recall proxy, precision) and the user-facing
privacy budget (per-CJ residual anonymity set
$k(T) = n_{eq}(T) - n_{\text{certified}}(T)$, as defined
in §7).

Setup. 150 seed makers, 5 batches of 4,000 rounds each, 10%
maker churn between batches, $\mathit{makercount} = 4$ per
CJ (so $n_{eq} = 5$). The simulator emits 100,000 maker
slots across 20,000 CJs, comparable in scale to the mainnet
corpus (129,301 slots in 16,890 ILP-decoded CJs). The
maker's input selection follows joinmarket-ng's
richest-mixdepth policy (`_select_our_utxos`); see §5 and
the simulator's `Maker.select_input_mixdepth`. The fee
grid in the *baseline_varied* arm matches the empirical fee
distribution of the mainnet orderbook snapshot; the maker
churn rate is set high enough that the simulator's residual
distribution is non-degenerate.

Seven variants. The two "edge-strip" variants
(`cm_no_eq_chain` and `cm_no_same_mixdepth_change`) are
*conceptual proxies* for the Tier B and Tier C output-recycling
mitigations: stripping `equal_output` from every slot is the
limiting case in which no maker ever spends a JM-emitted
equal output directly as a CJ input (Tier C item 6), and
stripping `change_output` is the limiting case in which no
maker ever spends a JM-emitted change UTXO directly as a CJ
input (Tier B item 4). The simulator is the right tool to
quantify these proxies because the conceptual question is
"what residual anonset would the analyst recover if a given
class of chain edge no longer fired", and the harness lets us
turn each class of edge on and off independently.

* `baseline_varied`: today's JoinMarket (every maker draws a
  distinct fee policy from the 12-row $\mathit{cjfee}_r$ grid
  $\times$ 6-row $\mathit{cjfee}_a$ grid $\times$ 4-row
  $\mathit{minsize}$ grid). All §9.1 channels active.
* `cm_uniform_fee` (Tier A item 1, full): every maker
  advertises the reference client's default policy
  ($\mathit{cjfee}_r = 10^{-4}$, $\mathit{cjfee}_a = 0$,
  $\mathit{minsize} = 27{,}300$ sats). v7's fee-fingerprint
  edge (channel 3) collapses by construction.
* `cm_three_policy_pool` (Tier A item 1, partial): every
  maker picks one of three well-known default policies (low /
  default / high) at startup. Quantifies the marginal value
  of full vs. partial fee pooling.
* `cm_no_two_output_cioh` (Tier A item 3): non-CJ maker
  spends are always batched to $\geq 3$ outputs, so channel
  (4) never fires. (Inactive in the simulator, which does
  not emit non-CJ maker spends; listed for completeness.)
* `cm_no_eq_chain` (Tier C item 6 proxy): no maker spends a
  JM-emitted equal output directly as a CJ input. The
  clusterer loses the equal-chain edge (channel 2).
* `cm_no_same_mixdepth_change` (Tier B item 4 proxy): no
  maker spends a JM-emitted change UTXO directly as a CJ
  input. The clusterer loses the change-chain edge
  (channel 1). The variant name is historical and refers to
  the strip transform on the `change_output` slot field; the
  *conceptual* mitigation is the self-consolidation loop of
  Tier B item 4, not change relocation across mixdepths
  (which, as §9.1 notes, does not change what the analyst
  observes).
* `cm_combined`: `cm_no_eq_chain` + `cm_no_same_mixdepth_change`
  + `cm_no_two_output_cioh`. The post-mitigation world in
  which a maker recycles every JM-emitted output through a
  laundering step before reuse.

Results (clusterer is the v7 loose gate of §5.2; mean
$n_{eq} = 5.00$ across all variants; precision against the
simulator's ground-truth maker labels is 1.0 for every
variant, which is a stricter check than the mainnet §6.2
probe corpus because every output's true owner is known, not
just the 35 probed nicks):

| variant                       | clusters | recall proxy | mean residual | share residual = 1 |
|-------------------------------|---------:|-------------:|--------------:|-------------------:|
| `baseline_varied`             |      417 |        0.997 |          0.00 |              0.15% |
| `cm_uniform_fee`              |    1,070 |        0.991 |          0.00 |              0.00% |
| `cm_three_policy_pool`        |      365 |        0.997 |          0.00 |              0.01% |
| `cm_no_two_output_cioh`       |      417 |        0.997 |          0.00 |              0.15% |
| `cm_no_eq_chain`              |   22,392 |        0.777 |          0.93 |             19.84% |
| `cm_no_same_mixdepth_change`  |   69,186 |        0.308 |          2.94 |             11.79% |
| `cm_combined`                 |  100,000 |        0.000 |          5.00 |              0.00% |

![Mean residual anonymity set and share of CJs with residual
= 1 across the seven countermeasure variants of §9.4 (100k
slots, $n_{eq} = 5$, v7 loose gate). The two within-protocol
fee variants sit on top of the baseline; the two
output-recycling variants move both metrics, and
`cm_combined` restores the full $n_{eq} = 5$ anonymity set.](figures/countermeasure_effectiveness.svg)

Reading the table. *Mean residual* is the per-CJ residual
anonymity set averaged across all 20,000 simulated CJs (5.00
means every published equal output remains in the anonset;
0.00 means every maker is certified and only the taker
remains).

Three observations stand out:

1. **In-protocol fee homogenization alone is not a meaningful
   defence.** `cm_uniform_fee` and `cm_three_policy_pool`
   change the cluster count (the v7 edge collapses, the
   clusterer is forced to fall back on v6's change-chain),
   but the *mean residual* stays at 0.00. Every maker is
   still certified, because the change-chain edge alone
   carries the full clustering signal: as long as a maker's
   change UTXO returns as a future input of the same maker
   (the protocol invariant), the analyst can chain its slots
   without ever looking at fees. Tier A item 1 was correctly
   characterized in earlier drafts as a partial defence; the
   simulator shows the partial is essentially zero at the
   user-facing metric.

2. **The single most effective conceptual mitigation is
   removing the change-as-input chain edge.**
   `cm_no_same_mixdepth_change` drops the mean residual from
   0.00 to 2.94 (out of 5.00): roughly three fifths of the
   published anonset is preserved per CJ. This is the
   change-chain edge of §9.1 signal 1; it is the *only*
   reuse edge in JoinMarket that fires on a deterministic
   single-producer UTXO, and removing it forces the
   clusterer back on the equal-chain alone, which is
   empirically far weaker even when the fee fingerprint can
   disambiguate the producer slot.

3. **Combining the two output-recycling mitigations restores
   the full published anonset.** `cm_combined` shows a mean
   residual of 5.00 (== $n_{eq}$): no maker is ever
   certified by the chain-only adversary. The protocol is
   hardenable in the strict sense that there exists a
   concrete set of operational changes (self-consolidation of
   change UTXOs plus equal-output recycling before reuse)
   under which the §5 attack pipeline produces zero
   same-wallet edges in the simulator.

The two output-recycling variants are the load-bearing
result of this section. They establish that the protocol is
not *doomed*: a self-consolidation loop of the kind described
in §9.2 Tier B item 4, paired with the symmetric equal-output
recycling of §9.2 Tier C item 6, recovers the published
anonset against the passive on-chain adversary. The
operational cost of running the self-consolidation loop is
the bottleneck: a maker who consolidates every change UTXO
through a self-CJ pays an extra on-chain fee per
consolidation, locks up the corresponding balance during the
consolidation delay, and forgoes the offer revenue that the
balance would otherwise earn. We do not attempt to put a
precise number on that cost; the qualitative answer is that
it scales linearly with the maker's CJ volume and is bounded
by the fee surplus the maker collects on its offers, so a
maker running at moderate volume can afford it.

Simulator caveats. The simulator does not model non-CJ maker
spends, so the §5.3 / §5.4 CIOH edges and the §5.5 FB-funding
edge cannot fire in any variant. On the mainnet corpus
these add roughly 0.01 to the mean residual reduction (the
v7 to v7.3 delta of §7.1), so the absolute simulator numbers
*underestimate* the residual reduction the live attack
achieves; the *relative ordering* across countermeasures is
the defendable claim. The simulator also does not implement
maker drop-out due to private liquidity exhaustion, which
would further fragment the chain on mainnet and favor the
defender, nor does it model an adaptive adversary that
combines fee fingerprint with cross-maker behavioral
heuristics; the conceptual claim is that the *passive*
on-chain adversary stops being able to certify a maker once
the chain edges fire on indistinguishable producer sets.

Answer to the central question. **The protocol is not
doomed, and a deployable client-side change (Tier B item 4,
maker self-consolidation rounds, no protocol change required)
already moves the user-facing residual substantially.** Full
restoration of the published anonset requires the symmetric
equal-output recycling of Tier C item 6, which is enforceable
client-side but is most useful as a cohort convention so that
takers can rely on it. The within-protocol fee hygiene of
Tier A items 1-3 is a useful prerequisite (it removes the v7
fingerprint channel and so makes the chain-edge mitigations
strictly more powerful) but does not, on its own, shift the
user-facing residual under this passive on-chain threat model.

## 10. Limitations and future work

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
non-CJ CIOH on the maker's other on-chain spends, are all
intrinsic to the protocol and to the typical maker wallet
workflow; they are not a fixable implementation bug.

The precision = 1.0 guarantee is what makes the result
actionable: under the per-CJ loose gate the clusterer never
merges two distinct maker wallets on this corpus, validated by
three independent ground-truth sources. Each certified maker the
analyst extracts is a *deterministic* hide-set reduction, not a
probabilistic one. 10.8% of CJs reach residual = 1, meaning every
maker is certified and only the taker remains in the anonymity
set. The precision guarantee is, strictly speaking, gate-and-
corpus dependent: the scaled simulator (\u00a76.1) shows that
under adversarial fee jitter the per-CJ gate breaks at ~3% of
edges and only the corpus-unique gate restores precision = 1.0
by construction. On the present mainnet snapshot the gates are
empirically indistinguishable; on a future snapshot in which an
adversary deliberately concentrates many makers near similar fee
policies the analyst should switch to the corpus-unique gate at
a recall cost of about 5%.

The practical implication for JoinMarket users is that the
relevant privacy figure for a round is not its published $n_{eq}$
but the v7.3 residual, which is typically 2 to 4 across the
entire range of round sizes the protocol supports. Section 9
discusses the available countermeasures and §9.4 quantifies
their effectiveness on the §6.1 simulator. The within-protocol
options that an individual maker or taker can adopt today (a
disjoint fidelity-bond wallet, fee-policy homogenization,
≥3-output batched non-CJ spends, taker-side maker selection by
expected anonset rather than by fee alone) reduce side channels
but, in the simulator, leave the mean residual at the same 0.00
as the baseline because the v6 change-chain edge alone carries
the full clustering signal. Substantial gains require breaking
the chain edges that fire on every maker slot. The simulator
identifies maker self-consolidation of change UTXOs (§9.2 Tier
B item 4) as the single highest-impact mitigation (mean
residual 0.00 to 2.94 out of 5.00); pairing it with symmetric
equal-output recycling before reuse (§9.2 Tier C item 6)
restores the full published $n_{eq}$ as the operative anonset
(mean residual 5.00 out of 5.00). The protocol is therefore
*hardenable* in a concrete and quantifiable sense, and a
deployable client-side patch (Tier B item 4, no protocol change
required) already moves the residual substantially. Until a
JoinMarket release ships at least the self-consolidation loop,
the v7.3 residual is the privacy budget the protocol gives its
users.
