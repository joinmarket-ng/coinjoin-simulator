# CoinJoin Simulator

Compact simulation toolkit for analyzing CoinJoin privacy under probing and adversarial taker pressure.

This repository is curated for publishing: it keeps the key datasets, a focused GitHub Pages report, and the core simulation code.

## What This Repository Shows

The published report focuses on four high-signal views:

1. Mitigation impact at fixed CoinJoin size (8 makers/CJ)
2. Long-run sustained attack outcomes (baseline vs recommended policy)
3. Probe-intensity privacy/cost trade-off
4. Attack-to-recovery timeline

Published output:

- `docs/index.html`
- `docs/publish_summary.json`

## Quick Start

```bash
pip install -e ".[dev]"
```

Requires Python 3.11+.

Generate the curated publish page from the tracked datasets:

```bash
PYTHONPATH=src python -m coinjoin_simulator.publish_site \
  --mitigation mitigation_experiments.json \
  --longrun longrun_policy_results.json \
  --daily daily_cost_study_results.json \
  --output docs/index.html \
  --data-output docs/publish_summary.json
```

You can also run it through the CLI:

```bash
PYTHONPATH=src python -m coinjoin_simulator publish-site
```

## Reproduce Input Datasets

These scripts regenerate the three datasets consumed by the publish page:

- `run_mitigation_experiments.py` -> `mitigation_experiments.json`
- `run_longrun_policy_study.py` -> `longrun_policy_results.json`
- `run_daily_cost_study.py` -> `daily_cost_study_results.json`

Note: these studies sample from the live JoinMarket orderbook URL configured in `src/coinjoin_simulator/network.py`.

## Core CLI Commands

```bash
# list built-in scenarios
coinjoin-sim list

# run one scenario
coinjoin-sim run --scenario naive_baseline

# run all scenarios
coinjoin-sim benchmark

# run realistic network sweep
coinjoin-sim network --makers 100 --rounds 1000 --evil-fractions 0.0,0.2,0.4,0.6
```

## Repository Layout

```text
src/coinjoin_simulator/
  network.py         realistic maker/probing simulator
  publish.py         curated metric extraction
  publish_site.py    GitHub Pages report generation
  ...                core anonymity/sybil/role/surveillance modules

tests/               unit tests
docs/                publish-ready report assets for GitHub Pages
```

## Development Checks

```bash
PYTHONPATH=src ruff check .
PYTHONPATH=src mypy src/
PYTHONPATH=src pytest
```

## License

MIT
