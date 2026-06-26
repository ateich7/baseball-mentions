"""
Kalshi API client — RSA-PSS signed requests.

All Kalshi HTTP calls go through here. The client handles auth signing,
pagination, and order placement. check_results.py uses the portfolio methods;
bet_today / monitor use market discovery and order placement.

Usage:
    from kalshi import KalshiClient
    k = KalshiClient(key_id='...', private_key_path='~/.ssh/kalshi.pem')
    markets = k.get_markets_for_event('KXMLBMENTION-26MAY07TBBOS')
"""

import base64, json, time, urllib.request, urllib.parse, urllib.error
from pathlib import Path
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


BASE_URL_PROD = 'https://api.elections.kalshi.com/trade-api/v2'
BASE_URL_DEMO = 'https://demo-api.kalshi.co/trade-api/v2'


class KalshiClient:
    def __init__(self, key_id: str, private_key_path: str, demo: bool = False):
        self.key_id   = key_id
        self.base_url = BASE_URL_DEMO if demo else BASE_URL_PROD
        pem = Path(private_key_path).expanduser().read_bytes()
        self._key = serialization.load_pem_private_key(pem, password=None)

    # ── Auth ──────────────────────────────────────────────────────────────────

    def _sign(self, method: str, path: str) -> dict:
        """Return auth headers for a Kalshi API request.

        Kalshi uses RSA-PSS (SHA-256) over the concatenation of:
            timestamp_ms + HTTP_METHOD_UPPERCASE + /trade-api/v2 + path
        The signature is base64-encoded and sent in KALSHI-ACCESS-SIGNATURE.
        Note: path must NOT include query string — sign the bare path only.
        """
        ts       = str(int(time.time() * 1000))
        full_path = '/trade-api/v2' + path
        msg      = (ts + method.upper() + full_path).encode()
        sig  = self._key.sign(msg, padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ), hashes.SHA256())
        return {
            'KALSHI-ACCESS-KEY':       self.key_id,
            'KALSHI-ACCESS-TIMESTAMP': ts,
            'KALSHI-ACCESS-SIGNATURE': base64.b64encode(sig).decode(),
            'Content-Type':            'application/json',
        }

    # ── HTTP helpers ──────────────────────────────────────────────────────────

    def _get(self, path: str, params=None) -> dict:
        """Signed GET request. Signs bare path; appends query string separately."""
        url_path = path
        if params:
            url_path += '?' + urllib.parse.urlencode(params)
        headers = self._sign('GET', path)   # sign without query string
        req  = urllib.request.Request(self.base_url + url_path, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors='ignore')
            raise RuntimeError(f'GET {path} → {e.code}: {body}') from e

    def _post(self, path: str, body: dict) -> dict:
        data    = json.dumps(body).encode()
        headers = self._sign('POST', path)
        req     = urllib.request.Request(
            self.base_url + path, data=data, headers=headers, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors='ignore')
            raise RuntimeError(f'POST {path} → {e.code}: {body}') from e

    # ── Account ───────────────────────────────────────────────────────────────

    def get_balance(self) -> float:
        """Return available balance in dollars. Kalshi returns cents as an integer."""
        data = self._get('/portfolio/balance')
        return int(data.get('balance', 0)) / 100   # cents → dollars

    # ── Market discovery ─────────────────────────────────────────────────────

    def get_series(self, category: str = 'sports') -> list[dict]:
        """List all series in a category."""
        out, cursor = [], None
        while True:
            params = {'category': category, 'limit': 200}
            if cursor:
                params['cursor'] = cursor
            data   = self._get('/series', params)
            series = data.get('series', [])
            out.extend(series)
            cursor = data.get('cursor')
            if not cursor or not series:
                break
        return out

    def get_events(self, series_ticker: str, status: str = 'open') -> list[dict]:
        """List events for a series (default: open only)."""
        out, cursor = [], None
        while True:
            params = {'series_ticker': series_ticker, 'status': status, 'limit': 200}
            if cursor:
                params['cursor'] = cursor
            data   = self._get('/events', params)
            events = data.get('events', [])
            out.extend(events)
            cursor = data.get('cursor')
            if not cursor or not events:
                break
        return out

    def get_markets_for_event(self, event_ticker: str) -> list[dict]:
        """List all markets within an event."""
        out, cursor = [], None
        while True:
            params = {'event_ticker': event_ticker, 'limit': 200}
            if cursor:
                params['cursor'] = cursor
            data    = self._get('/markets', params)
            markets = data.get('markets', [])
            out.extend(markets)
            cursor  = data.get('cursor')
            if not cursor or not markets:
                break
        return out

    def search_markets(self, keyword: str, series_ticker=None,
                       status: str = 'open') -> list[dict]:
        """
        Find open markets whose title contains keyword (case-insensitive).
        Optionally filter to a single series.
        """
        kw = keyword.lower()
        results = []
        if series_ticker:
            for event in self.get_events(series_ticker, status=status):
                for mkt in self.get_markets_for_event(event['event_ticker']):
                    if kw in mkt.get('title', '').lower():
                        results.append(mkt)
        else:
            # Scan all sports series — slow but thorough
            for s in self.get_series('sports'):
                for event in self.get_events(s['ticker'], status=status):
                    for mkt in self.get_markets_for_event(event['event_ticker']):
                        if kw in mkt.get('title', '').lower():
                            results.append(mkt)
        return results

    def get_market(self, ticker: str) -> dict:
        """Fetch a single market by ticker."""
        return self._get(f'/markets/{ticker}').get('market', {})

    # ── Pricing ───────────────────────────────────────────────────────────────

    @staticmethod
    def implied_prob(market: dict):
        """
        Best-estimate implied YES probability from market bid/ask.

        Priority:
          1. Midpoint of yes_bid_dollars / yes_ask_dollars — tightest estimate
          2. last_price_dollars — last traded price, lags the book
          3. Integer cent fallbacks (yes_bid, yes_ask) — older API fields

        The midpoint avoids favoring either buyer or seller and is the
        most accurate pre-trade estimate of consensus probability.
        Returns None if no price data at all.
        """
        def to_f(v):
            try:
                return float(v) if v not in (None, '', '0.0000') else None
            except (TypeError, ValueError):
                return None

        bid = to_f(market.get('yes_bid_dollars'))
        ask = to_f(market.get('yes_ask_dollars'))
        if bid is not None and ask is not None and ask > 0:
            return (bid + ask) / 2
        lp = to_f(market.get('last_price_dollars'))
        if lp is not None:
            return lp
        # Integer cent fallbacks
        bid_c = market.get('yes_bid')
        ask_c = market.get('yes_ask')
        if bid_c is not None and ask_c is not None and ask_c > 0:
            return (bid_c + ask_c) / 2 / 100
        lp_c = market.get('last_price')
        if lp_c is not None:
            return lp_c / 100
        return None

    # ── Orders ────────────────────────────────────────────────────────────────

    def place_order(self, ticker: str, side: str, yes_price_cents: int,
                    count: int, dry_run: bool = True) -> dict:
        """
        Place a limit order.

        ticker          — market ticker
        side            — 'yes' or 'no'
        yes_price_cents — the YES-side price in cents (1–99), regardless of side.
                          For a NO order at 35¢ NO, pass yes_price_cents=65
                          (because 35¢ NO implies 65¢ YES on the same binary market).
        count           — number of contracts (whole number)
        dry_run         — if True, print without sending (safe default)
        """
        body = {
            'ticker':           ticker,
            'side':             side,
            'action':           'buy',
            'count':            count,
            'yes_price':        yes_price_cents,
            'type':             'limit',
            'client_order_id':  f'bm_{ticker}_{int(time.time())}',
        }
        if dry_run:
            no_price = 100 - yes_price_cents
            display_price = no_price if side == 'no' else yes_price_cents
            print(f'[DRY RUN] {side.upper():>3} {ticker}  price={display_price}¢  count={count}')
            return {'dry_run': True, 'body': body}
        return self._post('/portfolio/orders', body)

    def get_order(self, order_id: str) -> dict:
        return self._get(f'/portfolio/orders/{order_id}').get('order', {})

    def get_live_orders(self, event_ticker: str = None) -> dict:
        """Return resting orders as {order_id: order_dict}, optionally filtered
        to a specific event_ticker."""
        params = {'status': 'resting'}
        if event_ticker:
            params['event_ticker'] = event_ticker
        data = self._get('/portfolio/orders', params)
        return {o['order_id']: o for o in data.get('orders', [])}

    # ── Portfolio — used by check_results.py ─────────────────────────────────

    def _get_all_pages(self, path: str, result_key: str, params=None) -> list:
        """Generic paginated GET — collects all pages into one list."""
        out, cursor = [], None
        while True:
            p = dict(params or {})
            p['limit'] = 200
            if cursor:
                p['cursor'] = cursor
            data  = self._get(path, p)
            items = data.get(result_key, []) or []
            out.extend(items)
            cursor = data.get('cursor')
            if not cursor or not items:
                break
        return out

    def get_positions(self) -> list[dict]:
        """All open market positions."""
        return self._get_all_pages('/portfolio/positions', 'market_positions')

    def get_settlements(self) -> list[dict]:
        """All settled market positions."""
        return self._get_all_pages('/portfolio/settlements', 'settlements')

    def get_fills(self) -> list[dict]:
        """All fill records (every executed trade)."""
        return self._get_all_pages('/portfolio/fills', 'fills')

    def cancel_order(self, order_id: str) -> dict:
        data    = json.dumps({}).encode()
        headers = self._sign('DELETE', f'/portfolio/orders/{order_id}')
        req     = urllib.request.Request(
            self.base_url + f'/portfolio/orders/{order_id}',
            data=data, headers=headers, method='DELETE')
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors='ignore')
            raise RuntimeError(f'DELETE /portfolio/orders/{order_id} → {e.code}: {body}') from e
