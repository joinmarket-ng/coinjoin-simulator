# CoinJoin Simulator

A simulation engine for evaluating privacy attacks and anonymity metrics in CoinJoin protocols,
with a focus on JoinMarket-style maker/taker architectures.

## What it does

The simulator models realistic JoinMarket networks, including maker wallet structures, fidelity
bonds, and mixdepths sampled from live orderbook data. It lets you run controlled experiments
against different attack models (probing, Sybil, role identification, surveillance) and measure
their impact on taker anonymity sets.

## Published studies

Results from two studies run with this simulator are available on the
[project site](https://joinmarket-ng.github.io/coinjoin-simulator/):

- **Probing attack and countermeasures** - how a malicious participant builds a UTXO database
  of makers by probing, and which protocol changes limit the leakage
- **JoinMarket equal-output anonymity in practice** - a fee-fingerprint clustering and
  forward-spend attribution attack replayed against a mainnet JoinMarket corpus

## Getting started

Requires Python 3.11+.

```bash
pip install -e ".[dev]"
```

Run the test suite:

```bash
pytest tests/
```

Rebuild the published site from the pinned study datasets:

```bash
python -m coinjoin_simulator.publish_site
python build_docs.py
```

## License

MIT
