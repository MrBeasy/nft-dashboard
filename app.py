from flask import Flask, jsonify, request, send_from_directory
from pathlib import Path
import sqlite3
from datetime import datetime, timedelta
from collections import defaultdict

BASE = Path(__file__).parent
DB = BASE / 'collection_trades.db'

app = Flask(__name__, static_folder=str(BASE / 'static'))


def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


@app.route('/')
def index():
    return send_from_directory(app.static_folder, 'index.html')


@app.route('/api/collections')
def api_collections():
    conn = get_db()
    rows = conn.execute(
        'SELECT slug, name, floor_price_eth, best_offer_eth FROM collections ORDER BY name'
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/chart-data')
def api_chart_data():
    slugs = [s.strip() for s in request.args.get('collections', '').split(',') if s.strip()]
    if not slugs:
        return jsonify({})

    conn = get_db()
    result = {}

    for slug in slugs:
        rows = conn.execute(
            'SELECT timestamp, price_eth, sale_type FROM sales '
            'WHERE collection_slug = ? ORDER BY timestamp',
            (slug,)
        ).fetchall()

        if not rows:
            continue

        daily = defaultdict(lambda: {
            'bids': 0, 'listings': 0,
            'bid_vol': 0.0, 'listing_vol': 0.0,
            'total_price': 0.0, 'sale_count': 0,
        })

        for row in rows:
            day = datetime.utcfromtimestamp(row['timestamp']).strftime('%Y-%m-%d')
            daily[day]['total_price'] += row['price_eth']
            daily[day]['sale_count'] += 1
            if row['sale_type'] == 'bid':
                daily[day]['bids'] += 1
                daily[day]['bid_vol'] += row['price_eth']
            else:
                daily[day]['listings'] += 1
                daily[day]['listing_vol'] += row['price_eth']

        min_ts = min(r['timestamp'] for r in rows)
        max_ts = max(r['timestamp'] for r in rows)
        start = datetime.utcfromtimestamp(min_ts).date()
        end = datetime.utcfromtimestamp(max_ts).date()

        all_days = []
        d = start
        while d <= end:
            all_days.append(d.isoformat())
            d += timedelta(days=1)

        result[slug] = {
            'days': all_days,
            'bids': [daily[d]['bids'] for d in all_days],
            'listings': [daily[d]['listings'] for d in all_days],
            'bid_vol': [round(daily[d]['bid_vol'], 4) for d in all_days],
            'listing_vol': [round(daily[d]['listing_vol'], 4) for d in all_days],
            'total_price': [round(daily[d]['total_price'], 4) for d in all_days],
            'sale_count': [daily[d]['sale_count'] for d in all_days],
        }

    conn.close()
    return jsonify(result)


if __name__ == '__main__':
    print('Dashboard: http://localhost:5001')
    app.run(debug=True, port=5001)
