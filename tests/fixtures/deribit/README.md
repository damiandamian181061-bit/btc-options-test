# Deribit test fixtures

These files have the exact shape of Deribit API v2 messages recorded from the public WebSocket
feed on 12 November 2026 (field names, nesting, types, nulls, extra fields we ignore). The
values (prices, sizes, IVs, Greeks, ids, timestamps) are made up, because raw vendor data is
never committed (CLAUDE.md).

- `instruments.json`: `public/get_instruments` results keyed by `currency/kind`. Besides
  ordinary instruments it holds cases discovery must handle: an ETH_USDC option and an
  ETH_USDC perpetual (other base currency), an option combo, an inactive option, and an option
  named `BTC-ODDNAME` whose strike, expiry and type exist only in its metadata.
- `session.jsonl`: one server message per line, replayed by the fake server: tickers for an
  inverse option, a linear option (with an empty bid side), both perpetuals and a dated future;
  both price indexes; trades on the BTC and USDC option channels (including an ETH_USDC trade
  that the recorder must ignore); and heartbeat messages.
- `delivery_prices.json`: a `public/get_delivery_prices` result.
