#!/usr/bin/env python3
"""
polymarket_scanner.py
Read-only Polymarket market scanner. It never places orders and needs no wallet or API key.

Detectors
  multi   : multi-outcome events where buying every "Yes" costs less than $1 per full set
  binary  : binary markets where ask(Yes) + ask(No) < $1
  movers  : sharp 1h / 24h price moves on liquid markets
  spreads : liquid markets with wide bid/ask spreads (market-making candidates)

Install
  pip install requests

Usage
  python polymarket_scanner.py                     # one scan, all detectors
  python polymarket_scanner.py --loop 120          # rescan every 2 min + Telegram alerts
  python polymarket_scanner.py --only multi movers

Telegram (optional): TELEGRAM_TOKEN , TELEGRAM_CHAT_ID
"""

import argparse
import csv
import json
import os
import time
from datetime import datetime, timezone

import requests

VERSION = "1.5"
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "polymarket-scanner/1.0"})
ALERTED = {}   # key -> time of last alert

# outcome labels that usually mean "everything else is covered"
CATCH_ALL = ("other", "none", "no one", "nobody", "neither", "not ", "no winner",
             "no deal", "no change", "unchanged", "field")


# ------------------------------------------------------------------
# helpers
# ------------------------------------------------------------------

def stamp():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def notify(text):
    token, chat = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return
    try:
        SESSION.post(f"https://api.telegram.org/bot{token}/sendMessage",
                     json={"chat_id": chat, "text": text, "disable_web_page_preview": True}, timeout=10)
    except requests.RequestException as e:
        print(f"[telegram error] {e}")


def alert_once(key, text, realert):
    now = time.time()
    if now - ALERTED.get(key, 0) < realert:
        return False
    ALERTED[key] = now
    notify(text)
    return True


def get_json(url, params=None, retries=3):
    for i in range(retries):
        try:
            r = SESSION.get(url, params=params, timeout=20)
            if r.status_code == 429:          # rate limited
                time.sleep(2 * (i + 1))
                continue
            r.raise_for_status()
            return r.json()
        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else 0
            if 400 <= code < 500 or i == retries - 1:
                raise                         # client errors will not fix themselves
            time.sleep(1 + i)
        except requests.RequestException:
            if i == retries - 1:
                raise
            time.sleep(1 + i)
    return None


def as_list(v):
    """Gamma returns some list fields as JSON-encoded strings."""
    if v is None:
        return []
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return []
    return list(v)


def fnum(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def short(text, n=58):
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "~"


def days_left(end_iso):
    if not end_iso:
        return None
    try:
        end = datetime.fromisoformat(str(end_iso).replace("Z", "+00:00"))
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        return max((end - datetime.now(timezone.utc)).total_seconds() / 86400, 0.0)
    except ValueError:
        return None


def log_rows(path, rows, fields):
    if not rows or not path:
        return
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if new:
            w.writeheader()
        for r in rows:
            w.writerow(r)


# ------------------------------------------------------------------
# data
# ------------------------------------------------------------------

def fetch_events(max_pages):
    """Active events, most traded first. The API caps how deep offset paging can go,
    so ordering by volume makes sure the pages we can reach are the liquid ones."""
    base = {"active": "true", "closed": "false", "archived": "false", "limit": 100}
    order = {"order": "volume24hr", "ascending": "false"}
    events = []
    for page in range(max_pages):
        params = dict(base, offset=page * 100, **order)
        try:
            batch = get_json(f"{GAMMA}/events", params)
        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else 0
            if page == 0 and order and 400 <= code < 500:
                order = {}                    # ordering not accepted: fall back to default order
                batch = get_json(f"{GAMMA}/events", dict(base, offset=0))
            elif 400 <= code < 500:
                break                         # reached the API's paging limit
            else:
                raise
        if not batch:
            break
        events.extend(batch)
        if len(batch) < 100:
            break
    return events


def price_change(value, price):
    """Keep a reported price change only if it is physically possible:
    prices live in [0, 1], so a change must be within [-1, 1] and the implied
    previous price (price - change) must also be within [0, 1]."""
    ch = fnum(value)
    if ch is None or abs(ch) > 1:
        return None
    if price is not None and not (-0.005 <= price - ch <= 1.005):
        return None
    return ch


def market_view(m, ev):
    liq = m.get("liquidityNum") if m.get("liquidityNum") is not None else m.get("liquidity")
    prices = [fnum(p) for p in as_list(m.get("outcomePrices"))]
    return {
        "event": ev.get("title") or "", "event_slug": ev.get("slug") or "",
        "question": m.get("question") or "", "slug": m.get("slug") or "",
        "label": m.get("groupItemTitle") or m.get("question") or "",
        "outcomes": as_list(m.get("outcomes")),
        "prices": prices,
        "tokens": [str(t) for t in as_list(m.get("clobTokenIds"))],
        "bid": fnum(m.get("bestBid")), "ask": fnum(m.get("bestAsk")),
        "vol24": fnum(m.get("volume24hr"), 0.0) or 0.0,
        "liq": fnum(liq, 0.0) or 0.0,
        "ch1h": price_change(m.get("oneHourPriceChange"), prices[0] if prices else None),
        "ch1d": price_change(m.get("oneDayPriceChange"), prices[0] if prices else None),
        "tradable": bool(m.get("active", True)) and not m.get("closed", False)
                    and m.get("acceptingOrders", True) is not False,
        "end": m.get("endDate") or ev.get("endDate"),
    }


def book(token_id):
    """(bids high->low, asks low->high) as [(price, size), ...]"""
    b = get_json(f"{CLOB}/book", {"token_id": token_id}) or {}
    asks = sorted(((fnum(x.get("price"), 0), fnum(x.get("size"), 0)) for x in b.get("asks", [])),
                  key=lambda t: t[0])
    bids = sorted(((fnum(x.get("price"), 0), fnum(x.get("size"), 0)) for x in b.get("bids", [])),
                  key=lambda t: -t[0])
    return bids, asks


def cost_for_shares(asks, shares):
    """USDC needed to buy `shares` walking the asks, or None if depth is insufficient."""
    got = cost = 0.0
    for price, size in asks:
        take = min(size, shares - got)
        cost += take * price
        got += take
        if got >= shares - 1e-9:
            return cost
    return None


def annualise(edge_pct, days):
    if edge_pct is None or days is None:
        return None
    return edge_pct * 365 / max(days, 1.0)


def has_catch_all(markets):
    labels = [str(m["label"]).lower() for m in markets]
    return any(k in lab for lab in labels for k in CATCH_ALL)


def event_url(slug):
    return f"https://polymarket.com/event/{slug}" if slug else ""


# ------------------------------------------------------------------
# detectors
# ------------------------------------------------------------------

def detect_multi(events, args):
    """Mutually exclusive outcomes (negRisk): one 'Yes' pays $1, so a full set of Yes
    bought for less than $1 locks in the difference at resolution."""
    rows = []
    for ev in events:
        if not ev.get("negRisk") and not ev.get("enableNegRisk"):
            continue
        ms = [market_view(m, ev) for m in ev.get("markets", [])]
        if len(ms) < 3 or not all(m["tradable"] for m in ms):
            continue   # an incomplete set is NOT an arbitrage
        if any(m["ask"] is None or m["ask"] <= 0 or not m["tokens"] for m in ms):
            continue
        total = sum(m["ask"] for m in ms)
        edge = 1.0 - total * (1 + args.fee)
        if edge * 100 < args.min_edge:
            continue
        d = days_left(ev.get("endDate"))
        if args.max_days and d is not None and d > args.max_days:
            continue
        rows.append({
            "time": stamp(), "type": "multi", "title": ev.get("title"), "slug": ev.get("slug"),
            "legs": len(ms), "sum_ask": total, "edge_pct": edge * 100, "days": d,
            "markets": ms, "book_edge_pct": None, "book_cost": None, "annual": None,
            "catch_all": has_catch_all(ms),
            "vol24": sum(m["vol24"] for m in ms),
        })
    rows.sort(key=lambda r: r["edge_pct"], reverse=True)

    for r in rows[: args.verify]:                 # re-check on the real order books
        try:
            cost = 0.0
            for m in r["markets"]:
                _, asks = book(m["tokens"][0])    # token 0 = "Yes"
                c = cost_for_shares(asks, args.shares)
                if c is None:
                    cost = None
                    break
                cost += c
            if cost is not None:
                cost *= 1 + args.fee
                r["book_cost"] = cost
                r["book_edge_pct"] = (args.shares - cost) / cost * 100
                r["annual"] = annualise(r["book_edge_pct"], r["days"])
        except requests.RequestException as e:
            print(f"  [book error] {short(r['title'])}: {e}")
    return rows


def detect_binary(markets, args):
    rows = []
    cands = [m for m in markets if m["tradable"] and len(m["tokens"]) == 2 and m["vol24"] >= args.min_volume]
    cands.sort(key=lambda m: m["vol24"], reverse=True)
    for m in cands[: args.binary_top]:
        try:
            _, asks_yes = book(m["tokens"][0])
            _, asks_no = book(m["tokens"][1])
        except requests.RequestException as e:
            print(f"  [book error] {short(m['question'])}: {e}")
            continue
        if not asks_yes or not asks_no:
            continue
        total = (asks_yes[0][0] + asks_no[0][0]) * (1 + args.fee)
        edge = (1.0 - total) * 100
        if edge < args.min_edge:
            continue
        c_yes, c_no = cost_for_shares(asks_yes, args.shares), cost_for_shares(asks_no, args.shares)
        book_edge = None
        if c_yes is not None and c_no is not None:
            cost = (c_yes + c_no) * (1 + args.fee)
            book_edge = (args.shares - cost) / cost * 100
        d = days_left(m["end"])
        if args.max_days and d is not None and d > args.max_days:
            continue
        rows.append({
            "time": stamp(), "type": "binary", "title": m["question"], "slug": m["event_slug"],
            "sum_ask": total, "edge_pct": edge, "book_edge_pct": book_edge,
            "days": d, "annual": annualise(book_edge, d), "vol24": m["vol24"],
        })
    rows.sort(key=lambda r: r["edge_pct"], reverse=True)
    return rows


def detect_movers(markets, args):
    rows = []
    for m in markets:
        if not m["tradable"] or m["vol24"] < args.min_volume:
            continue
        h, d = m["ch1h"], m["ch1d"]
        hit_h = h is not None and abs(h) >= args.move_1h
        hit_d = d is not None and abs(d) >= args.move_1d
        if not (hit_h or hit_d):
            continue
        price = m["prices"][0] if m["prices"] else None
        # skip markets that are effectively decided (game over, event already happened)
        if price is None or price <= args.decided or price >= 1 - args.decided:
            continue
        left = days_left(m["end"])
        if left is not None and left <= 0:
            continue
        rows.append({
            "time": stamp(), "type": "mover", "title": m["question"], "slug": m["event_slug"],
            "price": price, "ch1h": h, "ch1d": d, "vol24": m["vol24"],
            "score": max(abs(h or 0) / args.move_1h, abs(d or 0) / args.move_1d),
        })
    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows


def detect_spreads(markets, args):
    rows = []
    for m in markets:
        if not m["tradable"] or m["bid"] is None or m["ask"] is None or m["liq"] < args.min_liquidity:
            continue
        mid = (m["bid"] + m["ask"]) / 2
        spread = m["ask"] - m["bid"]
        if spread >= args.min_spread and 0.05 <= mid <= 0.95:
            rows.append({
                "time": stamp(), "type": "spread", "title": m["question"], "slug": m["event_slug"],
                "bid": m["bid"], "ask": m["ask"], "spread": spread, "liq": m["liq"], "vol24": m["vol24"],
            })
    rows.sort(key=lambda r: r["spread"], reverse=True)
    return rows


# ------------------------------------------------------------------
# output
# ------------------------------------------------------------------

def fmt(v, nd=2, pct=False):
    if v is None:
        return "-"
    return f"{v:.{nd}f}%" if pct else f"{v:.{nd}f}"


def report(results, args):
    print(f"\n[{stamp()}]")
    if "multi" in results:
        rows = results["multi"]
        print(f"\n== MULTI-OUTCOME: full set of 'Yes' below $1  ({len(rows)} found)")
        print(f"{'event':56}{'legs':>5}{'sumAsk':>8}{'edge':>8}{'book':>9}{'days':>6}{'annual':>9}  rules")
        for r in rows[: args.top]:
            rules = "ok" if r["catch_all"] else "CHECK"
            print(f"{short(r['title'], 55):56}{r['legs']:>5}{r['sum_ask']:>8.3f}{fmt(r['edge_pct'], pct=True):>8}"
                  f"{fmt(r['book_edge_pct'], pct=True):>9}{fmt(r['days'], 0):>6}"
                  f"{fmt(r['annual'], 0, pct=True):>9}  {rules}")
        if any(not r["catch_all"] for r in rows[: args.top]):
            print("  rules=CHECK: no 'Other/None' outcome found. All outcomes might resolve No. Read the market rules.")
    if "binary" in results:
        rows = results["binary"]
        print(f"\n== BINARY: ask(Yes) + ask(No) below $1  ({len(rows)} found)")
        print(f"{'market':60}{'sumAsk':>8}{'edge':>8}{'book':>9}{'days':>6}{'annual':>9}")
        for r in rows[: args.top]:
            print(f"{short(r['title'], 59):60}{r['sum_ask']:>8.3f}{fmt(r['edge_pct'], pct=True):>8}"
                  f"{fmt(r['book_edge_pct'], pct=True):>9}{fmt(r['days'], 0):>6}{fmt(r['annual'], 0, pct=True):>9}")
    if "movers" in results:
        rows = results["movers"]
        print(f"\n== MOVERS  ({len(rows)} found)")
        print(f"{'market':60}{'price':>7}{'1h':>8}{'24h':>8}{'vol24h$':>12}")
        for r in rows[: args.top]:
            print(f"{short(r['title'], 59):60}{fmt(r['price']):>7}{fmt(r['ch1h']):>8}{fmt(r['ch1d']):>8}"
                  f"{r['vol24']:>12,.0f}")
    if "spreads" in results:
        rows = results["spreads"]
        print(f"\n== WIDE SPREADS  ({len(rows)} found)")
        print(f"{'market':60}{'bid':>6}{'ask':>6}{'spread':>8}{'liq$':>12}")
        for r in rows[: args.top]:
            print(f"{short(r['title'], 59):60}{r['bid']:>6.2f}{r['ask']:>6.2f}{r['spread']:>8.2f}{r['liq']:>12,.0f}")


def send_alerts(results, args):
    def worth(r):
        return (r["book_edge_pct"] is not None and r["book_edge_pct"] >= args.alert_edge
                and r.get("annual") is not None and r["annual"] >= args.min_annual)

    for r in results.get("multi", []):
        if not worth(r):
            continue
        warn = "" if r["catch_all"] else "\nCHECK RULES: no 'Other/None' outcome - all could resolve No"
        if alert_once(("multi", r["slug"]), (
                f"POLYMARKET SET ARB: {r['title']}\n"
                f"{r['legs']} outcomes, sum of Yes asks {r['sum_ask']:.3f}\n"
                f"edge on {args.shares:.0f} sets: {r['book_edge_pct']:.2f}% | "
                f"resolves in {fmt(r['days'], 0)} days | annualised {r['annual']:.0f}%"
                f"{warn}\n{event_url(r['slug'])}"), args.realert):
            print(f"  -> alert sent: {short(r['title'])}")
    for r in results.get("binary", []):
        if not worth(r):
            continue
        if alert_once(("binary", r["title"]), (
                f"POLYMARKET YES+NO < $1: {r['title']}\n"
                f"sum of asks {r['sum_ask']:.3f}, edge on {args.shares:.0f} shares: {r['book_edge_pct']:.2f}% | "
                f"resolves in {fmt(r['days'], 0)} days | annualised {r['annual']:.0f}%\n"
                f"{event_url(r['slug'])}"), args.realert):
            print(f"  -> alert sent: {short(r['title'])}")
    for r in results.get("movers", [])[: args.alert_movers]:
        if alert_once(("mover", r["title"]), (
                f"POLYMARKET MOVE: {r['title']}\n"
                f"price {fmt(r['price'])} | 1h {fmt(r['ch1h'])} | 24h {fmt(r['ch1d'])}\n"
                f"vol 24h ${r['vol24']:,.0f}\n{event_url(r['slug'])}"), args.realert):
            print(f"  -> alert sent: {short(r['title'])}")


FIELDS = ["time", "type", "title", "slug", "legs", "sum_ask", "edge_pct", "book_edge_pct", "days",
          "annual", "catch_all",
          "price", "ch1h", "ch1d", "bid", "ask", "spread", "liq", "vol24"]


# ------------------------------------------------------------------
# main
# ------------------------------------------------------------------

def scan(args):
    events = fetch_events(args.pages)
    markets, seen = [], set()
    for ev in events:
        for m in ev.get("markets", []):
            mv = market_view(m, ev)
            key = mv["slug"] or (mv["tokens"][0] if mv["tokens"] else mv["question"])
            if key in seen:          # the same market can appear under more than one event
                continue
            seen.add(key)
            markets.append(mv)
    print(f"[{stamp()}] {len(events)} events, {len(markets)} markets loaded")
    results = {}
    if "multi" in args.only:
        results["multi"] = detect_multi(events, args)
    if "binary" in args.only:
        results["binary"] = detect_binary(markets, args)
    if "movers" in args.only:
        results["movers"] = detect_movers(markets, args)
    if "spreads" in args.only:
        results["spreads"] = detect_spreads(markets, args)
    return results


def main():
    p = argparse.ArgumentParser(description="Read-only Polymarket scanner (no trading)")
    p.add_argument("--only", nargs="+", default=["multi", "binary", "movers", "spreads"],
                   choices=["multi", "binary", "movers", "spreads"])
    p.add_argument("--pages", type=int, default=60, help="max event pages to load (100 events each); stops early when done")
    p.add_argument("--shares", type=float, default=100, help="set size used to verify on the order books")
    p.add_argument("--fee", type=float, default=0.0, help="taker fee as a fraction, if the market charges one")
    p.add_argument("--min-edge", type=float, default=0.5, help="min edge %% to list an arbitrage")
    p.add_argument("--verify", type=int, default=10, help="multi-outcome candidates to verify on the books")
    p.add_argument("--binary-top", type=int, default=40, help="binary markets (by volume) to check books for")
    p.add_argument("--min-volume", type=float, default=20_000, help="min 24h volume in USD")
    p.add_argument("--min-liquidity", type=float, default=5_000, help="min liquidity for the spread detector")
    p.add_argument("--min-spread", type=float, default=0.04, help="min bid/ask spread in $ (0.04 = 4 cents)")
    p.add_argument("--move-1h", type=float, default=0.08, help="alert on 1h move >= this (0.08 = 8 cents)")
    p.add_argument("--move-1d", type=float, default=0.20, help="alert on 24h move >= this")
    p.add_argument("--alert-edge", type=float, default=1.0, help="telegram alert when verified edge %% >= this")
    p.add_argument("--min-annual", type=float, default=15.0,
                   help="telegram alert only if the annualised edge %% >= this")
    p.add_argument("--max-days", type=float, default=0,
                   help="ignore arbitrages resolving later than N days (0 = no limit)")
    p.add_argument("--alert-movers", type=int, default=3, help="max mover alerts per scan (0 = none)")
    p.add_argument("--decided", type=float, default=0.03,
                   help="ignore movers priced within this of 0 or 1 (already decided)")
    p.add_argument("--realert", type=float, default=3600, help="seconds before alerting the same item again")
    p.add_argument("--top", type=int, default=15)
    p.add_argument("--loop", type=float, default=0, help="rescan every N seconds (0 = once)")
    p.add_argument("--log", default="polymarket_log.csv")
    args = p.parse_args()
    print(f"polymarket scanner v{VERSION}")

    if args.loop > 0:
        notify(f"polymarket scanner started, every {args.loop:.0f}s: {', '.join(args.only)}")
    try:
        while True:
            t0 = time.time()
            try:
                results = scan(args)
                report(results, args)
                send_alerts(results, args)
                log_rows(args.log, [r for rows in results.values() for r in rows[: args.top]], FIELDS)
            except requests.RequestException as e:
                print(f"[{stamp()}] network error: {e}")
            if args.loop <= 0:
                break
            time.sleep(max(0.0, args.loop - (time.time() - t0)))
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
