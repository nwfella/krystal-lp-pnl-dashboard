#!/usr/bin/env python3
"""Generalized Krystal LP PnL dashboard collector.

Given ANY EVM wallet address, pulls every LP-based position Krystal tracks for it
across all supported chains (direct LP positions + Krystal vault shares), computes
PnL / TVL aggregates, appends a daily snapshot to data/wallets/<addr>.json history,
and regenerates a fully static index.html with all data baked in (renders with JS
and network fetches blocked).

Usage:
  python scripts/pnl.py                     # default wallet from config.json
  python scripts/pnl.py 0xABC...            # any address
  python scripts/pnl.py 0xABC... --no-push  # render only, no git push
  python scripts/pnl.py --serve 8000        # local server: /?address=0x... -> live render

Stdlib only — no dependencies.
"""
import argparse
import concurrent.futures as cf
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(REPO, "config.json")
DATA_DIR = os.path.join(REPO, "data", "wallets")
INDEX_MANIFEST = os.path.join(REPO, "data", "index.json")
TEMPLATE_FILE = os.path.join(REPO, "template.html")
INDEX_FILE = os.path.join(REPO, "index.html")

API = "https://api.krystal.app"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0"}
ADDR_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")

CHAIN_NAMES = {
    1: "Ethereum", 10: "Optimism", 25: "Cronos", 56: "BNB Chain", 100: "Gnosis",
    137: "Polygon", 169: "Manta", 250: "Fantom", 288: "Boba", 324: "zkSync Era",
    1116: "Core", 42161: "Arbitrum", 43114: "Avalanche", 534352: "Scroll",
    59144: "Linea", 81457: "Blast", 8453: "Base", 42220: "Celo", 8217: "Klaytn",
}

_print_lock = threading.Lock()


def log(msg):
    with _print_lock:
        print(msg, flush=True)


def fetch_json(url, timeout=40):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def fmt(x, d=2, sign=False):
    if x is None:
        return "n/a"
    v = float(x)
    return ("%+." + str(d) + "f") % v if sign else ("%." + str(d) + "f") % v


def money(x, d=2, sign=False):
    return "n/a" if x is None else "$" + fmt(x, d, sign)


# ---------------------------------------------------------------------------
# Snapshot builders
# ---------------------------------------------------------------------------

def compact_position(p):
    """Reduce a raw Krystal position to the fields the dashboard needs."""
    pool = p.get("pool") or {}
    amts = pool.get("tokenAmounts") or []
    syms = [a.get("token", {}).get("symbol") or "?" for a in amts][:4]
    logos = [a.get("token", {}).get("logo") or "" for a in amts][:4]
    fees = 0.0
    for q in p.get("feePending") or []:
        try:
            fees += float((q.get("quotes") or {}).get("usd", {}).get("value") or 0)
        except (TypeError, ValueError):
            pass
    def f(key, default=None):
        v = p.get(key)
        return default if v is None else float(v)
    return {
        "id": p.get("id"),
        "chainId": p.get("chainId"),
        "chainName": CHAIN_NAMES.get(p.get("chainId"), p.get("chainName") or "?"),
        "status": p.get("status"),
        "project": pool.get("project") or "?",
        "projectLogo": pool.get("projectLogo") or "",
        "pair": "/".join(syms),
        "logos": logos,
        "tokenId": str(p.get("tokenId") or ""),
        "pool": p.get("tokenAddress") or (pool.get("poolAddress") or "")[:10],
        "value": f("currentPositionValue"),
        "initial": f("totalDepositValue", f("initialUnderlyingValue")),
        "pnl": f("pnl"),
        "roi": f("returnOnInvestment"),
        "il": f("impermanentLoss"),
        "feePending": fees,
        "earning24h": f("earning24h"),
        "apr": f("apr"),
        "opened": p.get("openedTime") or p.get("createdTime"),
        "hodl": f("compareWithHodl"),
        "minP": f("minPrice"),
        "maxP": f("maxPrice"),
        "vault": bool(p.get("vaultOwnerAddress")),
    }


def fetch_chain(wallet, chain_id):
    """Fetch one chain's open LP positions. Returns (chain_stats, positions)."""
    url = ("%s/all/v2/lp/userPositions?addresses=%s&walletAddress=%s&chainIds=%d"
           "&limit=500&positionStatus=open" % (API, wallet, wallet, chain_id))
    try:
        d = fetch_json(url)
    except Exception as e:
        return None, str(e)
    stats = (d.get("statsByChain") or {}).get("all") or {}
    positions = [compact_position(p) for p in (d.get("positions") or [])]
    return stats, positions


def fetch_vaults(wallet):
    """Fetch vault holdings + profile stats, with retries.

    The profile/stats endpoints are flaky: item counts vary between calls.
    Retry up to 3x, keeping the attempt (profile+stats pair) with the longest
    profile list so aggregates and rows stay consistent.
    """
    best = None  # (prof, stats, profile_len)
    for _ in range(3):
        try:
            prof = fetch_json("%s/all/v1/vaults/profile?userAddress=%s&perPage=500" % (API, wallet))
            stats = fetch_json("%s/all/v1/vaults/profile/stats?userAddress=%s" % (API, wallet))
            n = len(prof.get("data") or [])
            if best is None or n > best[2]:
                best = (prof, stats, n)
        except Exception:
            continue
    if best is None:
        raise RuntimeError("vaults API failed after 3 attempts")
    prof, stats, _ = best
    vaults = []
    for v in prof.get("data") or []:
        up = v.get("userPerformance") or {}
        if not up:
            continue  # only vaults the wallet actually holds shares in
        def f(key):
            val = v.get(key)
            return None if val is None else float(val)
        vaults.append({
            "name": v.get("name") or "Untitled vault",
            "address": v.get("vaultAddress"),
            "chainId": v.get("chainId"),
            "chainName": v.get("chainName") or CHAIN_NAMES.get(v.get("chainId"), "?"),
            "chainLogo": v.get("chainLogo") or "",
            "tvl": f("tvl"),
            "pnl": f("pnl"),
            "lpValue": f("lpValue"),
            "apr": f("apr"),
            "feeApr": f("feeApr"),
            "farmApr": f("farmApr"),
            "risk": v.get("riskScore"),  # string label: LOW/MODERATE/ELEVATED/HIGH
            "vaultType": v.get("vaultType"),
            "owner": v.get("ownerAddress"),
            "created": v.get("createdBlockTime"),
            "userValue": None if up.get("value") is None else float(up["value"]),
            "userPnl": None if up.get("pnlUsd") is None else float(up["pnlUsd"]),
            "userShares": None if up.get("shares") is None else float(up["shares"]),
            "userDeposit": None if up.get("totalDepositValue") is None else float(up["totalDepositValue"]),
        })
    return vaults, stats


def build_snapshot(wallet, chains):
    wallet = wallet.lower()
    t0 = time.time()
    results = {}
    with cf.ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(fetch_chain, wallet, c): c for c in chains}
        for fut in cf.as_completed(futs):
            c = futs[fut]
            results[c] = fut.result()
    errors = {c: e for c, (s, e) in results.items() if isinstance(e, str)}

    chains_out, positions = [], []
    for c in chains:
        stats, pos = results[c]
        if isinstance(pos, str):
            continue
        if not stats or not (stats.get("openPositionCount") or stats.get("closedPositionCount")):
            continue
        def f(key):
            v = stats.get(key)
            return None if v is None else float(v)
        chains_out.append({
            "chainId": c,
            "chainName": CHAIN_NAMES.get(c, "?"),
            "logo": "https://files.krystal.app/DesignAssets/chains/%s.png" % (stats.get("chainName") or ""),
            "open": int(stats.get("openPositionCount") or 0),
            "closed": int(stats.get("closedPositionCount") or 0),
            "value": f("currentPositionValue"),
            "pnl": f("pnl"),
            "unclaimedFees": f("unclaimedFees"),
            "earning24h": f("earning24h"),
            "totalFeeEarned": f("totalFeeEarned"),
            "apr": f("apr"),
            "roi": f("returnOnInvestment"),
            "protocols": stats.get("protocols") or [],
        })
        positions.extend(pos)
    chains_out.sort(key=lambda c: -(c["open"] or 0))

    vaults, vstats = fetch_vaults(wallet)

    lp_value = sum((p["value"] or 0) for p in positions)
    lp_pnl = sum((p["pnl"] or 0) for p in positions)
    lp_deposited = sum((p["initial"] or 0) for p in positions)
    lp_fees = sum((p["feePending"] or 0) for p in positions)
    vault_value = sum((v["userValue"] or 0) for v in vaults)
    vault_pnl = sum((v["userPnl"] or 0) for v in vaults)
    vault_deposited = sum((v["userDeposit"] or 0) for v in vaults)

    def f(key):
        v = vstats.get(key)
        return None if v is None else float(v)

    errors = {}
    expected = sum((c["open"] or 0) for c in chains_out)
    if expected != len(positions):
        errors["positionCount"] = "expected %d open positions, got %d" % (expected, len(positions))

    # Vault value comes from the displayed list so tables and cards agree.
    # Fall back to the /stats aggregate only if the profile returned zero rows
    # while the API claims the wallet holds joined vaults (fetch flake).
    usv = vault_value
    if not vaults and f("userShareValue") and f("totalJoinedVault"):
        usv = f("userShareValue")
        errors["vaults"] = "vault profile returned no rows; using stats aggregate"
    dep = vault_deposited
    if not dep:
        dep = f("depositedValue")

    return {
        "ts": int(time.time()),
        "date": time.strftime("%Y-%m-%d", time.gmtime()),
        "wallet": wallet,
        "owner": (vstats.get("owner") or {}).get("twitterUsername") or None,
        "totals": {
            "lpValue": round(lp_value, 6),
            "lpPnl": round(lp_pnl, 6),
            "lpDeposited": round(lp_deposited, 6),
            "lpFeesPending": round(lp_fees, 6),
            "lpEarning24h": round(sum((p["earning24h"] or 0) for p in positions), 6),
            "openPositions": len(positions),
            "vaultCount": len(vaults),
            "vaultUserValue": round(usv, 6),
            "vaultUserPnl": round(vault_pnl, 6),
            "vaultDeposited": dep,
            "vaultApy": f("apy"),
            "vaultDailyYield": f("dailyYield"),
            "combinedValue": round(lp_value + usv, 6),
            "combinedPnl": round(lp_pnl + vault_pnl, 6),
        },
        "chains": chains_out,
        "positions": positions,
        "vaults": vaults,
        "fetchSeconds": round(time.time() - t0, 1),
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

def wallet_file(wallet):
    return os.path.join(DATA_DIR, wallet + ".json")


def load_history(wallet):
    path = wallet_file(wallet)
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            h = json.load(f)
        return h if isinstance(h, list) else []
    except Exception:
        return []


def save_history(wallet, hist, cap):
    os.makedirs(DATA_DIR, exist_ok=True)
    hist = hist[-cap:]
    with open(wallet_file(wallet), "w") as f:
        json.dump(hist, f, indent=1)
        f.write("\n")


def update_manifest():
    entries = []
    if os.path.isdir(DATA_DIR):
        for fn in sorted(os.listdir(DATA_DIR)):
            if not fn.endswith(".json"):
                continue
            addr = fn[:-5]
            h = load_history(addr)
            if not h:
                continue
            last = h[-1]
            entries.append({
                "address": addr,
                "first": h[0].get("date"),
                "last": last.get("date"),
                "snapshots": len(h),
                "lastTvl": last.get("totals", {}).get("combinedValue"),
                "lastPnl": last.get("totals", {}).get("combinedPnl"),
            })
    os.makedirs(os.path.dirname(INDEX_MANIFEST), exist_ok=True)
    with open(INDEX_MANIFEST, "w") as f:
        json.dump(entries, f, indent=1)
        f.write("\n")
    return entries


# ---------------------------------------------------------------------------
# Static page generation (data baked in — zero network at page load)
# ---------------------------------------------------------------------------

def esc(s):
    return (json.dumps(s) if not isinstance(s, str) else json.dumps(s))


def js_str(s):
    """JSON-encode and neutralize </script>."""
    return json.dumps(s).replace("</", "<\\/")


def delta_html(cur, prev, unit):
    if cur is None or prev is None:
        return ""
    d = float(cur) - float(prev)
    if abs(d) < 1e-9:
        return '<div class="delta flat">±0 vs yesterday</div>'
    cls = "up" if d > 0 else "down"
    arrow = "▲" if d > 0 else "▼"
    if unit == "$":
        return '<div class="delta %s">%s %s vs yesterday</div>' % (cls, arrow, fmt(abs(d), 4))
    return '<div class="delta %s">%s %spp vs yesterday</div>' % (cls, arrow, fmt(abs(d) * 100, 2))


def card(label, val, accent, sub, delta=""):
    return ('<div class="card"><div class="accent %s"></div><div class="label">%s</div>'
            '<div class="val">%s</div>%s<div class="delta" style="color:var(--muted)">%s</div></div>'
            % (accent, label, val, delta, sub))


def build_cards(snap, prev):
    t, pt = snap["totals"], (prev or {}).get("totals") or {}
    def g(k):
        return t.get(k)
    items = [
        ("Combined TVL", money(g("combinedValue")), "accent g",
         "LP value + vault shares", g("combinedValue"), pt.get("combinedValue"), "$"),
        ("Direct LP value", money(g("lpValue")), "accent b",
         "%d open positions, all chains" % g("openPositions"), g("lpValue"), pt.get("lpValue"), "$"),
        ("LP PnL", money(g("lpPnl"), 4, True), "accent g" if (g("lpPnl") or 0) >= 0 else "accent r",
         "cumulative, direct LP", g("lpPnl"), pt.get("lpPnl"), "$"),
        ("Vault share value", money(g("vaultUserValue"), 3), "accent p",
         "%d vaults held" % g("vaultCount"), g("vaultUserValue"), pt.get("vaultUserValue"), "$"),
        ("Vault PnL", money(g("vaultUserPnl"), 4, True), "accent g" if (g("vaultUserPnl") or 0) >= 0 else "accent r",
         "cumulative, user shares", g("vaultUserPnl"), pt.get("vaultUserPnl"), "$"),
        ("Combined PnL", money(g("combinedPnl"), 4, True), "accent g" if (g("combinedPnl") or 0) >= 0 else "accent r",
         "LP + vaults", g("combinedPnl"), pt.get("combinedPnl"), "$"),
        ("Deposited", money(g("lpDeposited") + (g("vaultDeposited") or 0)), "accent y",
         "LP %s + vaults %s" % (money(g("lpDeposited"), 0), money(g("vaultDeposited"), 0)), None, None, None),
        ("Unclaimed LP fees", money(g("lpFeesPending"), 4), "accent y",
         "pending across open positions", g("lpFeesPending"), pt.get("lpFeesPending"), "$"),
        ("24h earnings", money(g("lpEarning24h"), 4, True), "accent g",
         "LP fee earnings, trailing 24h", g("lpEarning24h"), pt.get("lpEarning24h"), "$"),
    ]
    if g("vaultUserValue"):
        items.append(("Vault APY", (fmt(g("vaultApy") * 100, 2) if g("vaultApy") is not None else "n/a") + "%",
                      "accent p", "Krystal vaults, blended", g("vaultApy"), pt.get("vaultApy"), "%"))
    out = []
    for label, val, accent, sub, c, pv, unit in items:
        dlt = delta_html(c, pv, unit) if unit else ""
        out.append(card(label, val, accent, sub, dlt))
    return "\n    ".join(out)


def build_chains(snap):
    out = []
    for c in snap["chains"]:
        pnl_cls = "up" if (c["pnl"] or 0) >= 0 else "down"
        prots = "".join('<span class="chip">%s</span>' % p for p in (c["protocols"] or [])[:5])
        out.append(
            '<div class="chain-card"><div class="chain-head"><img src="%s" alt="" '
            'onerror="this.style.display=\'none\'"><div><div class="chain-name">%s</div>'
            '<div class="chain-sub">%d open · %d closed</div></div>'
            '<div class="chain-val">%s</div></div>'
            '<div class="chain-row"><span class="k">PnL</span><span class="%s">%s</span></div>'
            '<div class="chain-row"><span class="k">Fees earned</span><span>%s</span></div>'
            '<div class="chain-row"><span class="k">24h</span><span>%s</span></div>'
            '<div class="chain-row"><span class="k">APR</span><span>%s</span></div>'
            '<div class="chain-row"><span class="k">ROI</span><span>%s</span></div>'
            '<div class="chain-prots">%s</div></div>'
            % (c["logo"], c["chainName"], c["open"], c["closed"], money(c["value"], 0),
               pnl_cls, money(c["pnl"], 2, True),
               money(c["totalFeeEarned"], 0), money(c["earning24h"], 2, True),
               (fmt(c["apr"] * 100, 2) if c["apr"] is not None else "n/a") + "%",
               (fmt(c["roi"], 2, True) if c["roi"] is not None else "n/a") + "%", prots))
    return "\n    ".join(out) if out else '<div class="empty">No LP positions found on any chain.</div>'


def build_position_rows(positions):
    rows = []
    for p in sorted(positions, key=lambda p: (p["pnl"] or 0)):
        logos = "".join('<img src="%s" onerror="this.style.display=\'none\'">' % l for l in p["logos"][:2] if l)
        pnl_cls = "up" if (p["pnl"] or 0) >= 0 else "down"
        roi_cls = "up" if (p["roi"] or 0) >= 0 else "down"
        st_cls = "st-out" if p["status"] == "OUT_RANGE" else "st-in"
        opened = time.strftime("%Y-%m-%d", time.gmtime(p["opened"])) if p.get("opened") else "?"
        vault_tag = '<span class="chip chip-v">vault</span>' if p["vault"] else ""
        rows.append(
            '<tr class="pos-row" data-chain="%d" data-status="%s" data-project="%s">'
            "<td><div class='pair'><span class='logos'>%s</span><span>%s %s</span></div></td>"
            '<td class="chain-td">%s</td><td><span class="%s">%s</span></td>'
            "<td>%s</td><td class='%s'>%s</td><td class='%s'>%s</td>"
            "<td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
            % (p["chainId"], p["status"], (p["project"] or "").lower(),
               logos, p["pair"], vault_tag,
               p["chainName"],
               st_cls, "IN" if p["status"] == "IN_RANGE" else "OUT",
               money(p["value"], 2), pnl_cls, money(p["pnl"], 2, True), roi_cls,
               (fmt(p["roi"], 2, True) if p["roi"] is not None else "n/a") + "%",
               money(p["il"], 4, True) if p["il"] is not None else "n/a",
               money(p["feePending"], 4),
               (fmt(p["apr"] * 100, 2) if p["apr"] is not None else "n/a") + "%",
               money(p["earning24h"], 4, True), opened))
    return "\n      ".join(rows)


def risk_class(risk):
    r = str(risk or "").upper()
    if r in ("HIGH", "CRITICAL", "VERY HIGH"):
        return "r-high"
    if r in ("ELEVATED", "MEDIUM", "MODERATE", "AVERAGE"):
        return "r-mid"
    return "r-low"


def build_vault_rows(vaults):
    rows = []
    for v in sorted(vaults, key=lambda v: (v["userPnl"] or 0)):
        pnl_cls = "up" if (v["userPnl"] or 0) >= 0 else "down"
        risk = v["risk"] if v["risk"] is not None else "?"
        apr = (fmt(v["apr"] * 100, 2) if v["apr"] is not None else "n/a") + "%"
        rows.append(
            '<tr class="vault-row"><td class="vname">%s<div class="vsub">%s</div></td>'
            "<td>%s</td><td>%s</td><td>%s</td><td class='%s'>%s</td>"
            "<td>%s</td><td><span class='risk %s'>%s</span></td></tr>"
            % (v["name"], (v["address"] or "")[:12] + "…", v["chainName"],
               money(v["tvl"], 0), money(v["userValue"], 2), pnl_cls,
               money(v["userPnl"], 2, True), apr, risk_class(v["risk"]), risk))
    return "\n      ".join(rows)


def slim_history(hist):
    """Chart-only series (totals per day) — keeps the embedded HTML tiny."""
    keys = ("lpValue", "lpPnl", "lpFeesPending", "lpEarning24h", "openPositions",
            "vaultUserValue", "vaultUserPnl", "vaultApy", "combinedValue", "combinedPnl")
    out = []
    for h in hist:
        t = h.get("totals") or {}
        rec = {"ts": h.get("ts"), "date": h.get("date")}
        for k in keys:
            rec[k] = t.get(k)
        out.append(rec)
    return out


def render_page(snap, hist, tracked, wallet_label):
    with open(TEMPLATE_FILE, encoding="utf-8") as f:
        html = f.read()
    prev = hist[-2] if len(hist) >= 2 else None
    html = html.replace("<!--__CARDS__-->", build_cards(snap, prev))
    html = html.replace("<!--__CHAINS__-->", build_chains(snap))
    html = html.replace("<!--__POSITIONS__-->", build_position_rows(snap["positions"]))
    html = html.replace("<!--__VAULTS__-->", build_vault_rows(snap["vaults"]))

    warn = ""
    if (snap["totals"].get("combinedPnl") or 0) < 0:
        warn += "Net position is currently at a loss. "
    if snap["errors"]:
        bad = ", ".join("chain %s: %s" % (c, CHAIN_NAMES.get(c, "?")) for c in snap["errors"])
        warn += "Fetch errors on %s — snapshot may be incomplete. " % bad
    html = html.replace("<!--__WARN__-->", warn.strip())

    upd = "updated %s UTC" % time.strftime("%Y-%m-%d %H:%M", time.gmtime(snap["ts"]))
    html = html.replace("<!--__UPDATED__-->", upd)
    html = html.replace("<!--__WALLET__-->", wallet_label)
    html = html.replace("<!--__CHAINLIST__-->",
                        "".join('<option value="%d">%s</option>' % (c, CHAIN_NAMES[c])
                                for c in sorted(CHAIN_NAMES)))
    html = html.replace("<!--__TRACKED__-->",
                        "".join('<option value="%s">%s · %s</option>' % (t["address"], t["address"], t["last"])
                                for t in tracked))
    html = html.replace("__SNAPSHOT_JSON__", js_str(snap))
    html = html.replace("__HISTORY_JSON__", js_str(slim_history(hist)))
    html = html.replace("__TRACKED_JSON__", js_str(tracked))
    t = snap["totals"]
    html = html.replace("<!--__CUR_COMB__-->", money(t.get("combinedValue")))
    html = html.replace("<!--__CUR_COMBPNL__-->", money(t.get("combinedPnl"), 4, True))
    html = html.replace("<!--__CUR_LP__-->", money(t.get("lpValue")))
    html = html.replace("<!--__CUR_VLT__-->", money(t.get("vaultUserValue"), 3))
    with open(INDEX_FILE, "w", encoding="utf-8") as f:
        f.write(html)


# ---------------------------------------------------------------------------
# Report / git
# ---------------------------------------------------------------------------

def build_report(snap, hist, push_status):
    t = snap["totals"]
    lines = []
    owner = "@%s" % snap["owner"] if snap.get("owner") else snap["wallet"][:10] + "…"
    lines.append("💧 Krystal LP PnL — %s (%s)" % (snap["date"], owner))
    lines.append("─" * 58)
    lines.append("Combined TVL:   %s    (LP %s + vaults %s)"
                 % (money(t["combinedValue"]), money(t["lpValue"]), money(t["vaultUserValue"])))
    lines.append("Combined PnL:   %s (LP %s + vaults %s)"
                 % (money(t["combinedPnl"], 4, True), money(t["lpPnl"], 4, True), money(t["vaultUserPnl"], 4, True)))
    lines.append("Open LP pos.:   %d        | unclaimed fees: %s"
                 % (t["openPositions"], money(t["lpFeesPending"], 4)))
    lines.append("Vaults held:    %d        | vault APY: %s%%"
                 % (t["vaultCount"], fmt(None if t["vaultApy"] is None else t["vaultApy"] * 100, 2)))
    for c in snap["chains"]:
        lines.append("  %-11s %2d open / %-4d %s  pnl %s"
                     % (c["chainName"], c["open"], c["closed"], money(c["value"], 0),
                        money(c["pnl"], 2, True)))
    if snap["errors"]:
        lines.append("WARN fetch errors: %s" % ", ".join(str(k) for k in snap["errors"]))
    lines.append("push: %s · fetch %.1fs" % (push_status, snap["fetchSeconds"]))
    return "\n".join(lines)


def git_push(date_str, warnings):
    try:
        subprocess.run(["git", "-C", REPO, "add", "-A"], check=True, capture_output=True)
        r = subprocess.run(["git", "-C", REPO, "commit", "-m",
                            "data: krystal pnl snapshot %s" % date_str],
                           capture_output=True, text=True)
        if r.returncode == 0:
            p = subprocess.run(["git", "-C", REPO, "push"], capture_output=True, text=True)
            if p.returncode != 0:
                warnings.append("push failed: %s" % (p.stderr.strip()[:200]))
                return "push-failed"
            return "pushed"
        return "no-change"
    except Exception as e:
        warnings.append("git: %s" % e)
        return "git-error"


def load_config():
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Serve mode: live render for any address (fallback when browser fetch is blocked)
# ---------------------------------------------------------------------------

def run_serve(port):
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import urlparse, parse_qs
    cfg = load_config()

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            q = parse_qs(urlparse(self.path).query)
            addr = (q.get("address") or [None])[0]
            if self.path.startswith("/favicon"):
                self.send_response(204); self.end_headers(); return
            try:
                if not addr:
                    addr = cfg.get("default_wallet")
                if not addr or not ADDR_RE.match(addr):
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(b"invalid address - need 0x + 40 hex chars")
                    return
                snap = build_snapshot(addr, cfg.get("chains", []))
                hist = load_history(addr)
                hist = [h for h in hist if h.get("date") != snap["date"]]
                hist.append(snap)
                hist.sort(key=lambda h: h.get("ts", 0))
                save_history(addr, hist, int(cfg.get("max_history", 730)))
                tracked = update_manifest()
                render_page(snap, hist, tracked, addr)
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                with open(INDEX_FILE, "rb") as f:
                    self.wfile.write(f.read())
                log("rendered %s (%.1fs)" % (addr, snap["fetchSeconds"]))
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(("error: %s" % e).encode())
        def log_message(self, *a):
            pass

    print("Serving on http://localhost:%d  (add ?address=0x... to the URL)" % port)
    HTTPServer(("127.0.0.1", port), H).serve_forever()


def main():
    ap = argparse.ArgumentParser(description="Krystal LP PnL dashboard collector")
    ap.add_argument("wallet", nargs="?", help="wallet address (default: config.json)")
    ap.add_argument("--no-push", action="store_true", help="render only, no git push")
    ap.add_argument("--serve", type=int, metavar="PORT", help="run local live-render server")
    args = ap.parse_args()

    cfg = load_config()
    if args.serve:
        run_serve(args.serve)
        return

    wallet = (args.wallet or cfg.get("default_wallet") or "").strip().lower()
    if not ADDR_RE.match(wallet):
        sys.exit("invalid wallet address: %r" % wallet)
    chains = cfg.get("chains", [1, 10, 56, 137, 42161, 8453])

    snap = build_snapshot(wallet, chains)
    hist = load_history(wallet)
    hist = [h for h in hist if h.get("date") != snap["date"]]
    hist.append(snap)
    hist.sort(key=lambda h: h.get("ts", 0))
    save_history(wallet, hist, int(cfg.get("max_history", 730)))
    tracked = update_manifest()

    warnings = []
    try:
        render_page(snap, hist, tracked, wallet)
    except Exception as e:
        warnings.append("render: %s" % e)
        log("render failed: %s" % e)

    push_status = "skipped" if args.no_push else git_push(snap["date"], warnings)
    print(build_report(snap, hist, push_status))
    if warnings:
        print("WARN:", "; ".join(warnings))


if __name__ == "__main__":
    main()
