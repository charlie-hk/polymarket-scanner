#!/usr/bin/env python3
"""
polymarket_executor.py
Order-execution add-on for polymarket_scanner.py.

The scanner itself stays read-only. This is a separate script: it needs your
own wallet (private key, never anyone else's) and places real orders with
real funds on Polymarket's CLOB. Dry-run unless you pass --live.

Install
  pip install -r requirements-executor.txt

Setup (never commit these)
  export POLY_PRIVATE_KEY=0x...        # your wallet key
  export POLY_MAX_ORDER_USDC=50        # optional, per-order cap
  export POLY_MAX_EXPOSURE_USDC=300    # optional, total open-exposure cap

Usage
  python polymarket_executor.py --slug some-event --token-id 123 --side buy --price 0.42 --size 50
  python polymarket_executor.py --slug some-event --token-id 123 --side buy --price 0.42 --size 50 --live

Legal: trading on Polymarket is restricted or prohibited in a number of
countries. You are responsible for complying with your own jurisdiction's
laws and Polymarket's terms. Not financial advice.
"""

import argparse
import json
import os
import uuid
from datetime import datetime, timezone

STATE_FILE = os.getenv("POLY_STATE_FILE", "executor_state.json")
MAX_ORDER_USDC = float(os.getenv("POLY_MAX_ORDER_USDC", "50"))
MAX_EXPOSURE_USDC = float(os.getenv("POLY_MAX_EXPOSURE_USDC", "300"))


def stamp():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def load_state():
    if not os.path.exists(STATE_FILE):
        return {"orders": {}, "exposure_usdc": 0.0}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state):
    # write to a temp file then replace, so a crash mid-write never leaves a
    # half-written state file behind
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


def client_order_id(slug, token_id, side):
    return f"{slug}:{token_id}:{side}:{uuid.uuid4().hex[:10]}"


def get_client():
    """Authenticated CLOB client, built from env vars. Fails loudly if the key is missing
    instead of silently running in some half-authenticated state."""
    from py_clob_client.client import ClobClient

    key = os.getenv("POLY_PRIVATE_KEY")
    if not key:
        raise SystemExit("POLY_PRIVATE_KEY is not set. Use your own wallet key, never anyone else's.")
    client = ClobClient("https://clob.polymarket.com", key=key, chain_id=137)
    client.set_api_creds(client.create_or_derive_api_creds())
    return client


def reconcile(client, state):
    """On every run before a live order, check what the exchange actually has open
    instead of trusting whatever this script wrote last time. A crash between
    submitting and recording a fill should not turn into a duplicate order."""
    try:
        live = client.get_orders()
    except Exception as e:
        print(f"[reconcile] could not fetch live orders, keeping local state: {e}")
        return state
    live_ids = {o.get("id") for o in live}
    for coid, rec in state["orders"].items():
        if rec.get("exchange_id") and rec["status"] == "open" and rec["exchange_id"] not in live_ids:
            rec["status"] = "closed_or_filled_externally"
    return state


def check_limits(state, notional):
    if notional > MAX_ORDER_USDC:
        raise SystemExit(f"order notional ${notional:.2f} exceeds MAX_ORDER_USDC ${MAX_ORDER_USDC:.2f}")
    if state["exposure_usdc"] + notional > MAX_EXPOSURE_USDC:
        raise SystemExit(
            f"would exceed MAX_EXPOSURE_USDC (${state['exposure_usdc']:.2f} + "
            f"${notional:.2f} > ${MAX_EXPOSURE_USDC:.2f})"
        )


def place_order(args):
    from py_clob_client.clob_types import OrderArgs
    from py_clob_client.order_builder.constants import BUY, SELL

    state = load_state()
    notional = args.price * args.size
    check_limits(state, notional)

    coid = client_order_id(args.slug, args.token_id, args.side)
    state["orders"][coid] = {
        "time": stamp(), "slug": args.slug, "token_id": args.token_id,
        "side": args.side, "price": args.price, "size": args.size,
        "status": "dry_run" if not args.live else "submitting", "exchange_id": None,
    }
    save_state(state)  # record intent before sending anything

    if not args.live:
        print(f"[dry-run] would {args.side} {args.size} @ {args.price} on {args.slug} ({args.token_id})")
        return

    client = get_client()
    order = client.create_order(OrderArgs(
        token_id=args.token_id, price=args.price, size=args.size,
        side=BUY if args.side == "buy" else SELL,
    ))
    resp = client.post_order(order)
    state["orders"][coid]["status"] = resp.get("status", "submitted")
    state["orders"][coid]["exchange_id"] = resp.get("orderID") or resp.get("id")
    state["exposure_usdc"] += notional
    save_state(state)
    print(f"[live] order sent: {resp}")


def main():
    p = argparse.ArgumentParser(description="Order execution add-on for the Polymarket scanner. Dry-run unless --live.")
    p.add_argument("--slug", required=True, help="event slug, for logging and state only")
    p.add_argument("--token-id", required=True, help="CLOB token id for the outcome to trade")
    p.add_argument("--side", choices=["buy", "sell"], required=True)
    p.add_argument("--price", type=float, required=True)
    p.add_argument("--size", type=float, required=True, help="number of shares")
    p.add_argument("--live", action="store_true", help="actually submit the order; omit for a dry run")
    args = p.parse_args()

    if args.live:
        try:
            client = get_client()
            save_state(reconcile(client, load_state()))
        except SystemExit:
            raise
        except Exception as e:
            print(f"[warn] reconcile failed, continuing with local state: {e}")

    place_order(args)


if __name__ == "__main__":
    main()
