#!/usr/bin/env python3
"""
Collection Trading EV Calculator

Fetches all sales for an NFT collection and computes expected trading edge
based on consecutive buy-sell pairs per token.

Usage:
    python collection_ev.py --collection <CA_or_slug> [--days 365] [--market-share 1.0]
"""

import argparse
import os
import sys
import time
from collections import defaultdict
from statistics import mean, median, quantiles

import requests
from dotenv import load_dotenv
from tabulate import tabulate

import db as _db

load_dotenv()

OPENSEA_BASE = "https://api.opensea.io/api/v2"
OPENSEA_FEE_RECIPIENT = "0x0000a26b00c1f0df003000390027140000faa719"
ETH_TOKENS = {
    "0x0000000000000000000000000000000000000000",  # native ETH
    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",  # WETH
}


def _headers() -> dict:
    key = os.environ.get("OPENSEA_API_KEY", "")
    return {"accept": "application/json", "x-api-key": key}


def _get(url: str, params: dict, retries: int = 8) -> dict:
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, headers=_headers(), timeout=15)

            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                if retry_after:
                    try:
                        wait = float(retry_after)
                    except ValueError:
                        wait = min(2 ** attempt, 60)
                else:
                    wait = min(2 ** attempt, 60)
                print(f"\n  rate limited, waiting {wait:.0f}s (attempt {attempt + 1}/{retries})...", flush=True)
                time.sleep(wait)
                continue

            if resp.status_code in (500, 502, 503, 504):
                wait = min(2 ** attempt, 60)
                print(f"\n  server error {resp.status_code}, retrying in {wait:.0f}s (attempt {attempt + 1}/{retries})...", flush=True)
                time.sleep(wait)
                continue

            resp.raise_for_status()
            return resp.json()

        except requests.Timeout:
            wait = min(2 ** attempt, 60)
            print(f"\n  request timed out, retrying in {wait:.0f}s (attempt {attempt + 1}/{retries})...", flush=True)
            time.sleep(wait)
        except requests.ConnectionError:
            wait = min(2 ** attempt, 60)
            print(f"\n  connection error, retrying in {wait:.0f}s (attempt {attempt + 1}/{retries})...", flush=True)
            time.sleep(wait)
        except requests.RequestException:
            if attempt == retries - 1:
                raise
            time.sleep(min(2 ** attempt, 60))

    raise RuntimeError(f"Failed to GET {url} after {retries} attempts")


def resolve_collection(ca_or_slug: str) -> dict:
    """Resolve contract address or slug to {slug, name, creator_fee_bps, opensea_fee_bps, total_fee_bps}."""
    if ca_or_slug.startswith("0x"):
        data = _get(f"{OPENSEA_BASE}/chain/ethereum/contract/{ca_or_slug}", {})
        slug = data.get("collection", "")
        if not slug:
            sys.exit(f"Could not find collection for contract {ca_or_slug}")
    else:
        slug = ca_or_slug

    data = _get(f"{OPENSEA_BASE}/collections/{slug}", {})
    if not data:
        sys.exit(f"Collection '{slug}' not found on OpenSea")

    creator_fee_bps = 0
    opensea_fee_bps = 0
    for fee in data.get("fees", []):
        recipient = (fee.get("recipient") or "").lower()
        bps = int(round(float(fee.get("fee", 0)) * 100))
        if recipient == OPENSEA_FEE_RECIPIENT:
            opensea_fee_bps += bps
        else:
            creator_fee_bps += bps

    contract_address = ""
    for c in data.get("contracts", []):
        if c.get("chain", "").lower() == "ethereum":
            contract_address = c.get("address", "").lower()
            break

    return {
        "slug": slug,
        "name": data.get("name", slug),
        "contract_address": contract_address,
        "creator_fee_bps": creator_fee_bps,
        "opensea_fee_bps": opensea_fee_bps,
        "total_fee_bps": creator_fee_bps + opensea_fee_bps,
    }


def fetch_market_prices(slug: str) -> dict:
    """Fetch current floor and best offer, return {floor, best_offer, mid}."""
    floor = None
    best_offer = None

    try:
        stats = _get(f"{OPENSEA_BASE}/collections/{slug}/stats", {})
        raw = (stats.get("total") or {}).get("floor_price")
        if raw is not None:
            floor = float(raw)
    except Exception:
        pass

    try:
        offers_data = _get(f"{OPENSEA_BASE}/offers/collection/{slug}", {})
        offers = offers_data.get("offers") or []
        if offers:
            best_offer = int(offers[0]["price"]["current"]["value"]) / 1e18
    except Exception:
        pass

    if floor is not None and best_offer is not None:
        mid = (floor + best_offer) / 2
    elif floor is not None:
        mid = floor
    elif best_offer is not None:
        mid = best_offer
    else:
        mid = None

    return {"floor": floor, "best_offer": best_offer, "mid": mid}


def fetch_collection_events(
    slug: str,
    since_ts: int,
    occurred_before: int | None = None,
    on_checkpoint=None,
    checkpoint_every: int = 1000,
) -> list:
    """
    Paginate all ETH sale events for a collection since since_ts.
    If occurred_before is set, start pagination from that timestamp going backwards.
    If on_checkpoint is provided, it is called with each new batch of events once
    checkpoint_every events have accumulated, allowing callers to persist data mid-run.
    Returns list of {nft_id, price_eth, timestamp} dicts (all events, including
    already-checkpointed ones, so the caller can derive final sync state).
    """
    events = []
    last_checkpoint = 0
    cursor = None
    page = 0
    done = False

    while not done:
        params = {"event_type": "sale", "chain": "ethereum", "limit": 50}
        if cursor:
            params["next"] = cursor
        elif occurred_before:
            params["occurred_before"] = occurred_before

        data = _get(f"{OPENSEA_BASE}/events/collection/{slug}", params)
        raw = data.get("asset_events", [])
        next_cursor = data.get("next")
        page += 1

        if page == 1 or page % 10 == 0:
            print(f"  page {page}  ({len(events)} events so far)...", end="\r", flush=True)

        for ev in raw:
            ts = ev.get("closing_date") or 0
            if ts < since_ts:
                done = True
                break

            payment = ev.get("payment") or {}
            token_addr = (payment.get("token_address") or "").lower()
            symbol = (payment.get("symbol") or "").upper()
            if token_addr not in ETH_TOKENS and symbol not in ("ETH", "WETH"):
                continue

            try:
                price_eth = int(payment.get("quantity", "0")) / 1e18
            except (ValueError, TypeError):
                continue
            if price_eth <= 0:
                continue

            nft = ev.get("nft") or {}
            events.append({
                "tx_hash": (ev.get("transaction") or "").lower(),
                "nft_id": str(nft.get("identifier", "")),
                "timestamp": ts,
                "price_eth": price_eth,
                "payment_token": symbol,
                "sale_type": "bid" if symbol == "WETH" else "listing",
                "seller": (ev.get("seller") or "").lower(),
                "buyer": (ev.get("buyer") or "").lower(),
            })

        if on_checkpoint and len(events) - last_checkpoint >= checkpoint_every:
            batch = events[last_checkpoint:]
            on_checkpoint(batch)
            last_checkpoint = len(events)

        if not next_cursor or not raw:
            done = True
        elif not done:
            cursor = next_cursor
            time.sleep(0.25)

    print(f"  done - {len(events):,} ETH sale events collected across {page} pages.", flush=True)
    return events


def build_pairs(events: list) -> list:
    """
    Group by nft_id, sort chronologically, produce consecutive (buy, sell) pairs.
    Returns list of {buy_price, sell_price, holding_secs} dicts.
    """
    by_token = defaultdict(list)
    for ev in events:
        by_token[ev["nft_id"]].append(ev)

    pairs = []
    for token_events in by_token.values():
        token_events.sort(key=lambda e: e["timestamp"])
        for i in range(len(token_events) - 1):
            buy = token_events[i]
            sell = token_events[i + 1]
            holding_secs = sell["timestamp"] - buy["timestamp"]
            if holding_secs < 0:
                continue
            pairs.append({
                "buy_price": buy["price_eth"],
                "sell_price": sell["price_eth"],
                "holding_secs": holding_secs,
            })

    return pairs


def compute_ev(pairs: list, events: list, total_fee_bps: int, mid_price: float | None,
               market_share_pct: float, timeframe_days: int) -> dict:
    fee_rate = total_fee_bps / 10_000
    net_spreads, gross_spreads, fees_list, hold_times = [], [], [], []
    wins = 0

    for p in pairs:
        gross = p["sell_price"] - p["buy_price"]
        fees = p["sell_price"] * fee_rate
        net = gross - fees
        net_spreads.append(net)
        gross_spreads.append(gross)
        fees_list.append(fees)
        hold_times.append(p["holding_secs"])
        if net > 0:
            wins += 1

    n = len(pairs)
    avg_net = mean(net_spreads)
    annual_factor = 365 / timeframe_days
    annual_volume = n * annual_factor
    your_trades = annual_volume * (market_share_pct / 100)
    trades_per_day = len(events) / timeframe_days

    all_prices = [e["price_eth"] for e in events]
    listing_count = sum(1 for e in events if e["sale_type"] == "listing")
    bid_count = len(events) - listing_count

    if len(all_prices) >= 4:
        q1, _, q3 = quantiles(all_prices, n=4)
        iqr = q3 - q1
        lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
        clean_prices = [p for p in all_prices if lo <= p <= hi]
    else:
        clean_prices = all_prices

    return {
        "total_pairs": n,
        "timeframe_days": timeframe_days,
        "price_avg_eth": mean(clean_prices),
        "price_low_eth": min(clean_prices),
        "price_high_eth": max(clean_prices),
        "price_outliers_excluded": len(all_prices) - len(clean_prices),
        "listing_count": listing_count,
        "bid_count": bid_count,
        "avg_gross_spread_eth": mean(gross_spreads),
        "avg_fees_eth": mean(fees_list),
        "avg_net_spread_eth": avg_net,
        "roi_per_trade_pct": (avg_net / mid_price * 100) if mid_price else None,
        "win_rate": wins / n,
        "avg_holding_days": mean(hold_times) / 86_400,
        "median_holding_days": median(hold_times) / 86_400,
        "trades_per_day": trades_per_day,
        "your_trades_per_day": trades_per_day * (market_share_pct / 100),
        "annual_volume_pairs": annual_volume,
        "your_trades_per_year": your_trades,
        "market_share_pct": market_share_pct,
        "annual_ev_eth": your_trades * avg_net,
    }


def print_results(collection: dict, prices: dict, stats: dict, events: list, all_time: bool = False) -> None:
    floor_str = f"{prices['floor']:.4f} ETH" if prices["floor"] is not None else "N/A"
    offer_str = f"{prices['best_offer']:.4f} ETH" if prices["best_offer"] is not None else "N/A"
    mid_str = f"{prices['mid']:.4f} ETH" if prices["mid"] is not None else "N/A"

    fee_pct = collection["total_fee_bps"] / 100
    creator_pct = collection["creator_fee_bps"] / 100
    os_pct = collection["opensea_fee_bps"] / 100

    roi_tag = ""
    if stats["roi_per_trade_pct"] is not None:
        roi_tag = f"  ({stats['roi_per_trade_pct']:+.1f}% of mid)"

    rows = [
        ["Collection", f"{collection['name']} ({collection['slug']})"],
        ["Timeframe", f"{'all time' if all_time else f\"{stats['timeframe_days']} days\"}  |  {stats['total_pairs']:,} sale pairs analysed"],
        ["Fee rate", f"{fee_pct:.2f}%  ({creator_pct:.2f}% creator + {os_pct:.2f}% OpenSea)"],
        ["", ""],
        ["Current floor", floor_str],
        ["Best offer", offer_str],
        ["Mid price", mid_str],
        ["", ""],
        ["Avg sale price", f"{stats['price_avg_eth']:.4f} ETH  (excl. {stats['price_outliers_excluded']} outliers)"],
        ["Lowest sale", f"{stats['price_low_eth']:.4f} ETH"],
        ["Highest sale", f"{stats['price_high_eth']:.4f} ETH"],
        ["", ""],
        ["Avg gross spread", f"{stats['avg_gross_spread_eth']:+.4f} ETH"],
        ["Avg fees (on sell)", f"-{stats['avg_fees_eth']:.4f} ETH"],
        ["Avg net spread", f"{stats['avg_net_spread_eth']:+.4f} ETH{roi_tag}"],
        ["Win rate", f"{stats['win_rate'] * 100:.1f}%"],
        ["", ""],
        ["Listing buys", f"{stats['listing_count']}  ({stats['listing_count']/len(events)*100:.0f}%)"],
        ["Bid dumps", f"{stats['bid_count']}  ({stats['bid_count']/len(events)*100:.0f}%)"],
        ["", ""],
        ["Avg holding time", f"{stats['avg_holding_days']:.1f} days"],
        ["Median hold time", f"{stats['median_holding_days']:.1f} days"],
        ["", ""],
        ["Collection trades/24h", f"~{stats['trades_per_day']:.1f}"],
        ["Your trades/24h", f"~{stats['your_trades_per_day']:.2f}  (at {stats['market_share_pct']:.2f}% share)"],
        ["Annual volume (pairs)", f"~{stats['annual_volume_pairs']:,.0f} pairs/yr"],
        ["Your share", f"{stats['market_share_pct']:.2f}%  ->  ~{stats['your_trades_per_year']:.0f} trades/yr"],
        ["Annual EV", f"{stats['annual_ev_eth']:+.4f} ETH/yr"],
    ]

    print()
    print("=" * 62)
    print(tabulate(rows, tablefmt="plain"))
    print("=" * 62)


def main():
    parser = argparse.ArgumentParser(description="Compute trading EV for an NFT collection")
    parser.add_argument("--collection", required=True,
                        help="Contract address (0x...) or OpenSea slug")
    parser.add_argument("--days", type=int, default=0,
                        help="Lookback window for EV calculation in days (0 = all time, default: 0)")
    parser.add_argument("--market-share", type=float, default=10.0,
                        help="Your estimated market share %% (default: 10.0)")
    args = parser.parse_args()

    conn = _db.get_conn()
    _db.init_db(conn)

    print(f"\nResolving collection '{args.collection}'...")
    collection = resolve_collection(args.collection)
    slug = collection["slug"]
    print(f"  {collection['name']}  (slug: {slug})")
    print(f"  fees: {collection['creator_fee_bps']/100:.2f}% creator + {collection['opensea_fee_bps']/100:.2f}% OpenSea")

    print("\nFetching market prices...")
    prices = fetch_market_prices(slug)
    floor_disp = f"{prices['floor']:.4f}" if prices["floor"] else "N/A"
    offer_disp = f"{prices['best_offer']:.4f}" if prices["best_offer"] else "N/A"
    mid_disp = f"{prices['mid']:.4f}" if prices["mid"] else "N/A"
    print(f"  floor: {floor_disp} ETH  |  best offer: {offer_disp} ETH  |  mid: {mid_disp} ETH")

    _db.upsert_collection(conn, collection, prices)

    sync = _db.get_sync_state(conn, slug)

    def make_checkpoint(label: str):
        def checkpoint(batch: list) -> None:
            saved = _db.insert_sales(conn, slug, batch)
            oldest_in_batch = min(e["timestamp"] for e in batch)
            _db.update_sync_state(conn, slug, oldest_in_batch)
            print(f"\n  [{label} checkpoint] saved {saved:,} new events (oldest so far: "
                  f"{time.strftime('%Y-%m-%d', time.localtime(oldest_in_batch))})...", flush=True)
        return checkpoint

    if sync is None:
        # New collection — fetch full history
        print(f"\nNew collection — fetching full trade history (this may take a while)...")
        new_events = fetch_collection_events(slug, since_ts=0, on_checkpoint=make_checkpoint("import"))
        inserted = _db.insert_sales(conn, slug, new_events)
        oldest = min(e["timestamp"] for e in new_events) if new_events else int(time.time())
        _db.update_sync_state(conn, slug, oldest)
        print(f"  stored {inserted:,} new events to DB")
    else:
        # Known collection — fetch new events since last sync
        last_sync = sync["last_synced_at"]
        print(f"\nUpdating — fetching new events since last sync ({time.strftime('%Y-%m-%d', time.localtime(last_sync))})...")
        new_events = fetch_collection_events(slug, since_ts=last_sync, on_checkpoint=make_checkpoint("forward"))
        if new_events:
            inserted = _db.insert_sales(conn, slug, new_events)
            _db.update_sync_state(conn, slug, sync["oldest_ts_fetched"])
            print(f"  stored {inserted:,} new events")
        else:
            print("  no new events since last sync")

        # Backfill historical trades older than oldest stored event
        oldest_ts = _db.get_sync_state(conn, slug)["oldest_ts_fetched"]
        print(f"\nBackfilling history before {time.strftime('%Y-%m-%d', time.localtime(oldest_ts))}...")
        old_events = fetch_collection_events(slug, since_ts=0, occurred_before=oldest_ts,
                                             on_checkpoint=make_checkpoint("backfill"))
        if old_events:
            inserted = _db.insert_sales(conn, slug, old_events)
            new_oldest = min(e["timestamp"] for e in old_events)
            _db.update_sync_state(conn, slug, new_oldest)
            print(f"  backfilled {inserted:,} historical events")
        else:
            print("  no additional historical events found")

    # Load the EV calculation window from DB
    since_ts = 0 if args.days == 0 else int(time.time()) - args.days * 86_400
    events = _db.get_sales(conn, slug, since_ts)
    conn.close()

    timeframe_label = "all time" if args.days == 0 else f"past {args.days} days"
    if not events:
        print(f"No ETH-denominated sale events found ({timeframe_label}).")
        return

    print(f"\nLoaded {len(events):,} events from DB ({timeframe_label}).")

    print("Building ownership pairs...")
    pairs = build_pairs(events)
    unique_tokens = len({e["nft_id"] for e in events})
    print(f"  {len(pairs):,} consecutive buy-sell pairs across {unique_tokens:,} unique tokens")

    if not pairs:
        print("No consecutive pairs found — each token sold at most once in this period.")
        return

    if args.days == 0:
        span_days = max(1, round((max(e["timestamp"] for e in events) - min(e["timestamp"] for e in events)) / 86_400))
    else:
        span_days = args.days
    stats = compute_ev(pairs, events, collection["total_fee_bps"], prices["mid"], args.market_share, span_days)
    print_results(collection, prices, stats, events, all_time=args.days == 0)


if __name__ == "__main__":
    main()
