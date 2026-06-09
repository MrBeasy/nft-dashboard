#!/usr/bin/env python3
"""
Update all known collections in the DB.

For each collection in the `collections` table:
  1. Re-fetch metadata (fees, contract address) and current prices.
  2. Forward-sync new sales since last sync.
  3. Backfill any historical sales not yet stored.

Usage:
    python update_all.py [--slug SLUG] [--prices-only]

Options:
    --slug SLUG      Only update this specific slug (can be repeated).
    --prices-only    Refresh prices/metadata only; skip fetching new sales.
"""

import argparse
import time

from dotenv import load_dotenv

import db as _db
from collection_ev import (
    compute_daily_avg_spread,
    compute_daily_volume,
    fetch_collection_events,
    fetch_market_prices,
    resolve_collection,
)

load_dotenv()


def update_collection(conn, slug: str, prices_only: bool) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {slug}")
    print(f"{'=' * 60}")

    print("  Refreshing metadata & prices...")
    try:
        collection = resolve_collection(slug)
    except SystemExit as e:
        print(f"  ERROR resolving collection: {e}")
        return
    except Exception as e:
        print(f"  ERROR resolving collection: {e}")
        return

    try:
        prices = fetch_market_prices(slug)
    except Exception as e:
        print(f"  WARNING: could not fetch prices: {e}")
        prices = {"floor": None, "best_offer": None, "mid": None}

    floor_disp = f"{prices['floor']:.4f}" if prices["floor"] is not None else "N/A"
    offer_disp = f"{prices['best_offer']:.4f}" if prices["best_offer"] is not None else "N/A"
    print(f"  floor: {floor_disp} ETH  |  best offer: {offer_disp} ETH  |  "
          f"fees: {collection['creator_fee_bps'] / 100:.2f}% + {collection['opensea_fee_bps'] / 100:.2f}% OS")

    _db.upsert_collection(conn, collection, prices)

    if prices_only:
        print("  (prices-only mode — skipping sales fetch)")
        return

    def make_checkpoint(label: str):
        def checkpoint(batch: list) -> None:
            saved = _db.insert_sales(conn, slug, batch)
            oldest_in_batch = min(e["timestamp"] for e in batch)
            _db.update_sync_state(conn, slug, oldest_in_batch)
            print(f"\n  [{label} checkpoint] saved {saved:,} new events "
                  f"(oldest: {time.strftime('%Y-%m-%d', time.localtime(oldest_in_batch))})...",
                  flush=True)
        return checkpoint

    sync = _db.get_sync_state(conn, slug)

    if sync is None:
        print("  No sync state — fetching full history...")
        new_events = fetch_collection_events(slug, since_ts=0, on_checkpoint=make_checkpoint("import"))
        if new_events:
            inserted = _db.insert_sales(conn, slug, new_events)
            oldest = min(e["timestamp"] for e in new_events)
            _db.update_sync_state(conn, slug, oldest)
            print(f"  stored {inserted:,} new events")
        else:
            print("  no events found")
        return

    # Forward sync: new events since last sync
    last_sync = sync["last_synced_at"]
    print(f"  Forward sync from {time.strftime('%Y-%m-%d', time.localtime(last_sync))}...")
    new_events = fetch_collection_events(slug, since_ts=last_sync, on_checkpoint=make_checkpoint("forward"))
    if new_events:
        inserted = _db.insert_sales(conn, slug, new_events)
        _db.update_sync_state(conn, slug, sync["oldest_ts_fetched"])
        print(f"  stored {inserted:,} new events")
    else:
        print("  no new events since last sync")

    # Backfill: historical events older than oldest stored
    oldest_ts = _db.get_sync_state(conn, slug)["oldest_ts_fetched"]
    print(f"  Backfilling history before {time.strftime('%Y-%m-%d', time.localtime(oldest_ts))}...")
    old_events = fetch_collection_events(
        slug, since_ts=0, occurred_before=oldest_ts,
        on_checkpoint=make_checkpoint("backfill"),
    )
    if old_events:
        inserted = _db.insert_sales(conn, slug, old_events)
        new_oldest = min(e["timestamp"] for e in old_events)
        _db.update_sync_state(conn, slug, new_oldest)
        print(f"  backfilled {inserted:,} historical events")
    else:
        print("  no additional historical events found")

    # Compute and persist spread + volume stats over all stored sales
    all_sales = _db.get_sales(conn, slug, 0)
    spread = compute_daily_avg_spread(all_sales, collection["total_fee_bps"])
    volume = compute_daily_volume(all_sales)
    if spread:
        _db.update_spread(conn, slug, {**spread, **volume})
        print(f"  spread: {spread['avg_gross_spread_eth']:+.4f} ETH gross / "
              f"{spread['avg_net_spread_eth']:+.4f} ETH net "
              f"({spread['pair_count']:,} days)  |  "
              f"vol: {volume['avg_daily_sales_alltime']:.1f}/d alltime  "
              f"{volume['avg_daily_sales_30d']:.1f}/d 30d")


def main():
    parser = argparse.ArgumentParser(description="Update all known NFT collections in the DB")
    parser.add_argument("--slug", action="append", dest="slugs", metavar="SLUG",
                        help="Only update this slug (repeatable). Default: all collections.")
    parser.add_argument("--prices-only", action="store_true",
                        help="Refresh prices/metadata only; skip fetching new sales.")
    args = parser.parse_args()

    conn = _db.get_conn()
    _db.init_db(conn)

    if args.slugs:
        slugs = args.slugs
    else:
        rows = conn.execute("SELECT slug FROM collections ORDER BY slug").fetchall()
        slugs = [r["slug"] for r in rows]

    if not slugs:
        print("No collections found in DB. Run collection_ev.py --collection <slug> first.")
        return

    print(f"Updating {len(slugs)} collection(s)...")
    start = time.time()

    for i, slug in enumerate(slugs, 1):
        print(f"\n[{i}/{len(slugs)}] {slug}")
        update_collection(conn, slug, prices_only=args.prices_only)

    elapsed = time.time() - start
    print(f"\nDone. Updated {len(slugs)} collection(s) in {elapsed:.0f}s.")
    conn.close()


if __name__ == "__main__":
    main()
