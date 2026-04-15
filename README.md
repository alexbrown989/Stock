# Theta Harvest — Systematic Options Income System

A modular, quantitative pipeline for extracting consistent risk-adjusted returns through theta decay in highly liquid equities and ETFs. Operates on a continuous 14-day cycle.

---

## Architecture

```
Stock/
├── pipeline.py              # Main entry point (CLI)
├── config.py                # All tunable parameters
├── requirements.txt
│
├── scanner/
│   ├── universe.py          # Liquid universe screening
│   ├── liquidity.py         # Bid-ask spread & OI analysis
│   ├── macro.py             # Cross-asset regime detection
│   └── sentiment.py         # PCR, IV skew, trend scoring
│
├── analysis/
│   ├── greeks.py            # BSM pricing, Greeks, IV solver
│   ├── iv_analysis.py       # IV Rank, IV Percentile, IV/HV ratio
│   └── probability.py       # POP, EV, theta efficiency
│
├── strategy/
│   ├── filters.py           # Quantitative entry gates
│   ├── trade_builder.py     # Strike selection & trade construction
│   └── theta_harvest.py     # Cycle engine & scan orchestration
│
└── tracking/
    ├── positions.py         # Position lifecycle management
    └── performance.py       # Analytics & adaptive feedback loop
```

---

## Installation

```bash
pip install -r requirements.txt
```

---

## Usage

### Daily scan (full cycle)
```bash
python pipeline.py scan
```

### Scan specific structures
```bash
python pipeline.py scan --structures short_put put_spread iron_condor
```

### View open portfolio
```bash
python pipeline.py positions
```

### Check exit conditions on open positions
```bash
python pipeline.py exit-check
```

### Performance report & adaptive recommendations
```bash
python pipeline.py performance
```

### Record a new trade manually
```bash
python pipeline.py open AAPL --spot 185.50 --strike 175 --expiration 2024-05-17 --credit 1.85 --delta -0.22 --iv 0.28 --dte 32
```

### Close a position
```bash
python pipeline.py close AAPL_2024-05-17_202404151030 0.70 --reason profit_target
```

---

## Quantitative Entry Criteria

| Filter              | Threshold         | Rationale                                       |
|---------------------|-------------------|-------------------------------------------------|
| Delta (absolute)    | 0.15 – 0.35       | Meaningful premium without excessive directional risk |
| Theta efficiency    | ≥ 0.5%/day        | Premium decays at acceptable rate               |
| Gamma stress        | Δdelta < 0.10 (2% move) | Position stable under moderate shocks    |
| IV Rank             | ≥ 30              | Selling elevated, not fairly priced, vol        |
| IV / HV ratio       | ≥ 1.10            | IV premium over realized volatility             |
| Probability of Profit | ≥ 65%           | Statistical edge in every trade                 |
| DTE                 | 14 – 45 days      | Theta acceleration zone                         |

---

## Cycle Management

- **Max concurrent positions**: 6
- **Profit target**: Close at 50% of max profit
- **Stop loss**: Close at 2× credit received
- **Cycle length**: 14 days, rolling

---

## Configuration

All thresholds are centralized in `config.py`. After each cycle, run `pipeline.py performance` to receive data-driven recommendations on which parameters to adjust.

---

## Design Principles

1. Consistency over yield — prefer high-POP setups over maximum premium
2. Data overrides bias — performance feedback drives all parameter adjustments  
3. Simplicity at equal performance — the simplest structure that captures edge wins
4. Risk before opportunity — no filter bypass for "good looking" setups
