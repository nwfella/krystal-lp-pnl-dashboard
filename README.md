# Krystal LP PnL Dashboard

Generalized **address PnL dashboard** for Krystal's LP tracking: enter **any EVM
wallet address** and get every LP-based position Krystal tracks for it — direct
liquidity positions (Uniswap V2/V3/V4, PancakeSwap V2/V3/Infinity, THENA,
Aerodrome, …) across all supported chains, plus the wallet's **Krystal vault
shares** — with PnL and TVL tracked over time.

**Live page:** https://nwfella.github.io/krystal-lp-pnl-dashboard/

## Two modes

1. **Live mode** (default in any normal browser): type an address in the search
   box (or use `?address=0x…`) and the page fetches `api.krystal.app` directly
   (CORS is open) — 19 chains in parallel + vault profile. Works for any address,
   no backend needed.
2. **Static mode** (restricted networks where browser fetch is blocked, e.g.
   monitored work PCs): the page ships with a fully baked-in snapshot — cards,
   tables, charts and history embedded — zero network calls at page load, renders
   even with JS disabled. Regenerate for any address with the CLI.

## What it pulls (all `api.krystal.app`, no auth)

| Data | Endpoint |
|---|---|
| Direct LP positions, all chains (open; per-chain closed counts) | `/all/v2/lp/userPositions?addresses=<wallet>&chainIds=<id>&limit=500&positionStatus=open` |
| Vault holdings (name, TVL, your value, your PnL, APR, risk) | `/all/v1/vaults/profile?userAddress=<wallet>&perPage=500` |
| Vault aggregates (user share value, deposits, APY) | `/all/v1/vaults/profile/stats?userAddress=<wallet>` |

Per position: value, PnL, ROI, impermanent loss, pending fees, APR, 24h
earnings, price range, opened date, DEX (`pool.project`), pair, status
(IN_RANGE / OUT_RANGE). Positions opened through a Krystal vault are tagged
`vault` and are *not* double-counted — direct LP and vault shares are
non-overlapping sets.

Aggregates: **Combined TVL** = direct LP value + vault share value;
**Combined PnL** = LP PnL + vault PnL (both cumulative, gross).

## CLI

```bash
python scripts/pnl.py                     # default wallet from config.json
python scripts/pnl.py 0xABC…              # any address → fetch + snapshot + render + git push
python scripts/pnl.py 0xABC… --no-push    # render only
python scripts/pnl.py --serve 8000        # local server: /?address=0x… → live server-side render
```

Stdlib only. History accumulates in `data/wallets/<address>.json` (deduped by
day, 730-day cap); `data/index.json` lists tracked wallets. A Hermes cron job
snapshots the default wallet daily at 09:00 PT and pushes — that's what fills
the "over time" charts (Krystal exposes no historical API for arbitrary
wallets, so history is built from daily snapshots).

## Known API quirks (handled)

- The vaults profile/stats endpoints are **flaky**: item counts vary per call and
  stats occasionally return zeroed/contradictory values. The collector retries
  (keeps the attempt with the longest profile list) and prefers the displayed
  vault list as ground truth for totals, falling back to the `/stats` aggregate
  only when the profile comes back empty while stats claim joined vaults.
- `riskScore` is a **string label** (LOW / MODERATE / ELEVATED / HIGH), not a number.
- Chain IDs: 1, 10, 25, 56, 100, 137, 169, 250, 288, 324, 1116, 42161, 43114,
  534352, 59144, 81457, 8453, 42220, 8217 (empty chains are skipped).

## Files

- `scripts/pnl.py` — collector + static renderer + local server
- `template.html` — page source with markers; generator fills them
- `index.html` — generated static page (committed so GH Pages works immediately)
- `config.json` — default wallet, chain list, history cap
- `data/` — per-wallet snapshot history + tracked-wallet manifest

## License

MIT — see [LICENSE](LICENSE). Not financial advice.
