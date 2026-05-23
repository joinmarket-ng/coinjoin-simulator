# JoinMarket Maker Wallet Clustering and Taker Anonymity-Set Reduction

# JoinMarket Maker Wallet Clustering and Taker Anonymity-Set Reduction

> **TL;DR.** JoinMarket is a Bitcoin CoinJoin protocol where a
> *taker* pays a small fee to one or more *makers* to mix coins
> into a single transaction with several equal-value outputs;
> each maker's equal output is supposed to be indistinguishable
> from the taker's. We ran a passive on-chain experiment on the
> last few years of mainnet JoinMarket activity (16,890
> ILP-decoded CoinJoins) and clustered maker wallets using only
> protocol-forced signals: the JoinMarket mixdepth state machine
> (a maker's change stays in the source mixdepth and its equal
> output advances to the next, and the maker tends to re-advertise
> from whichever mixdepth holds the most coins), the Common Input
> Ownership Heuristic on non-CoinJoin maker spends recovered with
> ILP, and the maker's published fee schedule used as a
> per-CoinJoin disambiguator. A simple example of that last
> signal: if a maker charges 0.1% in one CJ and an equal output of
> that CJ then participates in a later CJ whose ILP-recovered
> slot also charges 0.1% (and no other maker in the producer CJ
> charges 0.1% at that amount), the two slots are the same maker.
> Sometimes a participant acts as taker in one round and maker in
> the next, which lets the same edge pull the taker's equal
> output into a maker cluster. The result is uncomfortable: the
> mean published anonymity set for a CJ in this corpus is 8.66
> equal outputs, but after removing certified makers it drops to
> 3.71, and on 10.8% of CJs only the taker remains. The good news
> is that practical mitigations exist (we discuss them in §9), and
> none of the steps in this attack are probabilistic: every merge
> is a hard same-wallet conclusion or no merge at all.

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
  without an off-chain consolidation. The same mixdepth
  separation protects the taker as much as the maker: the
  protections in this paper apply to anyone who reuses a
  JoinMarket wallet across roles.
- Each maker advertises offers on the JoinMarket directory-server
  overlay. Today the overlay is a small set of directory nodes
  that relay (often end-to-end encrypted) Tor messages between
  participants; earlier versions used IRC. Nicks are randomised
  per session, but every offer is anchored to a *fidelity bond*
  (FB): a timelocked P2WSH UTXO the maker proves they control.
  The FB is the only durable label a passive observer sees, and
  the orderbook publishes it in cleartext.
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
   on-chain data alone, at precision = 1.0, on the full mainnet
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
useful precision check, and the maker-UTXO values and addresses
have been zeroed in the published probe data so that only the
outpoint identifiers needed for the precision check survive. The
probing data contributes nothing to the clustering itself.

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
   slot's own realised fee as a per-CJ fingerprint: if exactly
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
construction.

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
later CJ $S$, the consumer slot's realised fee in $S$ identifies
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
JoinMarket CoinJoins, produced the snapshot used here:

| metric                | count        |
|-----------------------|-------------:|
| visited transactions  | ~200,000     |
| **JM CoinJoin txs**   | **23,876**   |
| ILP-decoded CJs       | 16,890 (70.7%) |
| ILP failures (timeout / infeasible at `max_fee_rel = 0.05`, `time_limit = 2s`) | 6,986 (29.3%) |
| maker slots recovered | 129,301      |

An ILP run on one CJ *succeeds* when it returns a feasible
slot-by-slot decomposition that satisfies every per-slot
constraint: each slot is a single mixdepth's worth of inputs that
sums to exactly one equal output plus one change output, every
slot's realised fee is non-negative, and the per-CJ fee budget is
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
reality. Precision is therefore $= 1.0$ by construction, and
recall is bounded below by the fraction of CJs the corpus
successfully observes and decodes.

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
non-CJ co-spend edge (§5.3), removing 81 further singletons. v7.2
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
\lfloor \mathit{cjfee}_r \cdot a \rceil & \text{(relative maker, sat-rounded)}.
\end{cases}
$$

The ILP recovers this realised fee per slot from the input total,
equal output, and change output. The fee is observable but not by
itself identifying: thousands of slots share the same
$(\mathit{cjfee}_r, \mathit{cjfee}_a)$ fingerprint across the
corpus, so a global fee-band clusterer would silently merge
distinct same-policy makers.

v7 uses the fee fingerprint locally, *within a single producer
CJ*. When an equal output of producer CJ $T$ is spent as a maker
input of slot $c$ in a later CJ $S$, the consumer slot $c$
carries its own realised fee $f_c$ at amount $a_S$. We then look
at $T$'s maker slots $S_1, \dots, S_n$ (at most about 22 in
practice) and ask: is there exactly one $S_i$ whose advertised
offer would produce $f_c$ at amount $a_S$? Concretely, for each
$S_i$ with realised fee $f_i$ at amount $a_T$ we check the two
admissible interpretations:

- *absolute* match: $f_i = f_c$ (so $S_i$'s policy is
  $\mathit{cjfee}_a = f_c$);
- *relative* match: $f_i / a_T \approx f_c / a_S$ within a 1 ppm
  tolerance (so $S_i$'s policy is
  $\mathit{cjfee}_r = f_c / a_S$).

v7 adds the equal-chain edge if and only if **exactly one $S_i$
matches under absolute OR relative**, *and* the absolute and
relative interpretations do not point at two different slots. If
two or more slots match (ambiguous), or the absolute and relative
candidates disagree (conflict), no edge is added. Conflicting
interpretations are dropped because we cannot decide which is
correct without knowing the maker's policy.

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
mainnet (§5.1: 0 collisions, §6.2: 0 cross-nick collisions).

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
The 17 mixed and 10 JM-input funding txs are the ones that *do*
leak a same-wallet edge to a v7.2 maker slot, and they produce
the 17 backward-via-change anchors that v7.3 acts on.

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

The three precision checks converge: v6 through v7.3 are
precision = 1.0 by construction, by the within-CJ structural
check on 16,890 mainnet CJs, and by the probing ground truth on
35 matched real maker nicks across 40 distinct UTXOs.

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
  probe data (§6.2) suggests this is not the dominant pattern,
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
