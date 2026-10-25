# Data sources and timestamp contracts

## Yahoo Finance through yfinance

[yfinance](https://github.com/ranaroussi/yfinance) is free and unofficial, intended for personal research/education; it is not affiliated with Yahoo. Its software license does not grant data rights. Review [Yahoo terms](https://legal.yahoo.com/us/en/yahoo/terms/otos/index.html), usage limits and personal-use restrictions. There is no guaranteed rate, availability, delay, realtime accuracy, exchange certification or redistribution entitlement. Rate limiting/authentication failure stops the batch; do not rotate identities, bypass controls or disable TLS. This deployment is local personal reporting only.

The M2 note describes approximately 15–20 minutes of delay. This is an operational caveat, not a provider SLA. This implementation never claims a measured latest-trade delay from a daily bar.

Exact series: `0050.TW`, `^TWII`, `2344.TW`, `2382.TW`, `2408.TW`, `3231.TW`, `2317.TW`, `2303.TW`, `2409.TW`, `3481.TW`, `2603.TW`, `2618.TW`, `2327.TW`, `3711.TW`, `2449.TW`, `3045.TW`, `2412.TW`, each `period=5d, interval=1d`; plus `2382.TW period=1mo, interval=1d`. `auto_adjust=True`, `prepost=False`, `repair=False`, 10-second library request timeout; 18 sequential history calls per uncached run, at most 420 seconds overall. Library cookie/crumb/timezone traffic can add HTTP requests, so 18 is not an HTTP request ceiling. The subprocess is killed on outer timeout. Library defaults were checked against version 1.4.1, pinned separately from the rest of FIE.

`bar_timestamp` / `source_as_of` retain the actual aware yfinance daily index (+08:00); `source_date` is that bar's Taipei date. This is a daily bar label, often midnight, **not a latest-trade timestamp**. `latest_trade_at` stays null. `fetched_at` and checkpoint age are separate; a cached read retains its original fetch time. Daily `Close` during an open session is an indicative bar observation, not the official final close. At 13:00 the session has not closed (regular close 13:30).

OHLC is TWD/share, except `^TWII` index points. Volume is retained as unconverted provider-reported volume, not asserted to be lots; no volume-derived trade signal is produced. Missing/nonpositive stock volume blocks the LIMITED report. `^TWII` missing/zero volume is tolerated as in M2; 0050 remains the useful market benchmark. Invalid/nonfinite OHLC, impossible ranges, naive/future/out-of-order timestamps, duplicate/missing series and oversized history fail closed.

Raw persistence means exact normalized worker JSON, including the yfinance version and hashes, not original Yahoo HTTP bodies. yfinance performs parsing and price adjustment before this boundary. Do not describe that artifact as wire-level raw response. Older versions and content hashes remain auditable. Separate 5d and 1mo series do not overwrite each other.

A provider bar on a verified closed date is retained as a labelled calendar conflict, never accepted as a session or used to change the calendar. October 23 2026 is the verified typhoon closure, October 24–25 weekend, October 26 holiday; October 27 is the next expected session. Sunday source reads are source-access evidence only. Calendar verification/evidence must be current; no calendar is inferred from Yahoo data.

## Other preserved sources

| Source | Contract / limits |
|---|---|
| TWSE daily prices, valuation, institutional | Official daily publication dates. Hourly fetch does not create intraday observations. Existing adapters and database tables retained. |
| DIGITIMES RSS | Three configured AI/technology/semiconductor feeds. Publisher pubDate and canonical URL; seven-day headline counts, not whole-market research or sentiment. Wealth/stale feeds remain disabled. |
| TWSE company OpenAPI | Monthly operating revenue and provider-labelled quarterly snapshots. Output date is not issuer filing time; quarter vs YTD not certified. No synthesized monthly earnings, TTM, targets or full M2 score. |
| FRED DGS10/DGS2/VIXCLS | Publication/series-specific US calendars and release grace. Taiwan closure never makes US observations current. |

See [scheduling](SCHEDULING.md) for recommendations and the distinction from the existing installed cadence.
