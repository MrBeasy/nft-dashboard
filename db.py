"""SQLite persistence for collection sale events."""

import sqlite3
import time

DB_PATH = "collection_trades.db"


def get_conn(path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS collections (
            slug                TEXT PRIMARY KEY,
            name                TEXT,
            contract_address    TEXT,
            creator_fee_bps     INTEGER,
            opensea_fee_bps     INTEGER,
            total_fee_bps       INTEGER,
            floor_price_eth     REAL,
            best_offer_eth      REAL,
            updated_at          INTEGER
        );

        CREATE TABLE IF NOT EXISTS sales (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            collection_slug TEXT    NOT NULL,
            tx_hash         TEXT    NOT NULL,
            nft_id          TEXT    NOT NULL,
            timestamp       INTEGER NOT NULL,
            price_eth       REAL    NOT NULL,
            payment_token   TEXT,
            sale_type       TEXT,
            seller          TEXT,
            buyer           TEXT,
            UNIQUE(tx_hash, nft_id)
        );

        CREATE INDEX IF NOT EXISTS idx_sales_slug_ts
            ON sales(collection_slug, timestamp);

        CREATE TABLE IF NOT EXISTS sync_state (
            collection_slug     TEXT PRIMARY KEY,
            oldest_ts_fetched   INTEGER,
            last_synced_at      INTEGER
        );
    """)
    # Migrate: add spread columns if they don't exist yet
    for col, typ in [
        ("avg_gross_spread_eth", "REAL"),
        ("avg_net_spread_eth", "REAL"),
        ("avg_gross_spread_pct", "REAL"),
        ("avg_net_spread_pct", "REAL"),
        ("spread_pair_count", "INTEGER"),
        ("spread_updated_at", "INTEGER"),
        ("avg_daily_sales_alltime", "REAL"),
        ("avg_daily_sales_30d", "REAL"),
    ]:
        try:
            conn.execute(f"ALTER TABLE collections ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError:
            pass  # column already exists
    conn.commit()


def upsert_collection(conn: sqlite3.Connection, collection: dict, prices: dict) -> None:
    conn.execute("""
        INSERT INTO collections
            (slug, name, contract_address, creator_fee_bps, opensea_fee_bps,
             total_fee_bps, floor_price_eth, best_offer_eth, updated_at)
        VALUES (:slug, :name, :contract_address, :creator_fee_bps, :opensea_fee_bps,
                :total_fee_bps, :floor_price_eth, :best_offer_eth, :updated_at)
        ON CONFLICT(slug) DO UPDATE SET
            name             = excluded.name,
            contract_address = excluded.contract_address,
            creator_fee_bps  = excluded.creator_fee_bps,
            opensea_fee_bps  = excluded.opensea_fee_bps,
            total_fee_bps    = excluded.total_fee_bps,
            floor_price_eth  = excluded.floor_price_eth,
            best_offer_eth   = excluded.best_offer_eth,
            updated_at       = excluded.updated_at
    """, {
        **collection,
        "floor_price_eth": prices.get("floor"),
        "best_offer_eth": prices.get("best_offer"),
        "updated_at": int(time.time()),
    })
    conn.commit()


def update_spread(conn: sqlite3.Connection, slug: str, spread: dict) -> None:
    """Persist computed daily-avg spread stats for a collection."""
    conn.execute("""
        UPDATE collections
        SET avg_gross_spread_eth    = :avg_gross_spread_eth,
            avg_net_spread_eth      = :avg_net_spread_eth,
            avg_gross_spread_pct    = :avg_gross_spread_pct,
            avg_net_spread_pct      = :avg_net_spread_pct,
            spread_pair_count       = :pair_count,
            spread_updated_at       = :updated_at,
            avg_daily_sales_alltime = :avg_daily_sales_alltime,
            avg_daily_sales_30d     = :avg_daily_sales_30d
        WHERE slug = :slug
    """, {**spread, "slug": slug, "updated_at": int(time.time())})
    conn.commit()


def insert_sales(conn: sqlite3.Connection, slug: str, events: list) -> int:
    """Insert events, skipping duplicates. Returns number of new rows inserted."""
    rows = [
        (slug, e["tx_hash"], e["nft_id"], e["timestamp"], e["price_eth"],
         e["payment_token"], e["sale_type"], e["seller"], e["buyer"])
        for e in events
    ]
    cur = conn.executemany("""
        INSERT OR IGNORE INTO sales
            (collection_slug, tx_hash, nft_id, timestamp, price_eth,
             payment_token, sale_type, seller, buyer)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, rows)
    conn.commit()
    return cur.rowcount


def get_sales(conn: sqlite3.Connection, slug: str, since_ts: int) -> list:
    """Load all sales for a collection since since_ts, newest first."""
    rows = conn.execute("""
        SELECT nft_id, tx_hash, timestamp, price_eth, payment_token, sale_type, seller, buyer
        FROM sales
        WHERE collection_slug = ? AND timestamp >= ?
        ORDER BY timestamp DESC
    """, (slug, since_ts)).fetchall()
    return [dict(r) for r in rows]


def get_sync_state(conn: sqlite3.Connection, slug: str) -> dict | None:
    row = conn.execute(
        "SELECT oldest_ts_fetched, last_synced_at FROM sync_state WHERE collection_slug = ?",
        (slug,)
    ).fetchone()
    return dict(row) if row else None


def update_sync_state(conn: sqlite3.Connection, slug: str, oldest_ts: int) -> None:
    conn.execute("""
        INSERT INTO sync_state (collection_slug, oldest_ts_fetched, last_synced_at)
        VALUES (?, ?, ?)
        ON CONFLICT(collection_slug) DO UPDATE SET
            oldest_ts_fetched = MIN(oldest_ts_fetched, excluded.oldest_ts_fetched),
            last_synced_at    = excluded.last_synced_at
    """, (slug, oldest_ts, int(time.time())))
    conn.commit()
