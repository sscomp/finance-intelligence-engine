# Calendar batch admission and checkpoint contract

The collector validates the entire provider response (series, timestamps, OHLCV)
before calendar admission. Only verified `CLOSED` dates are removed. An unknown,
expired, missing or unverified calendar rejects the entire batch. Volume never
determines whether a date is closed. Verified OPEN bars with zero volume can be
stored; the existing current-session strategy volume requirement still applies.

Before any P3 call, rejected normalized rows, their hashes and exact response
references are saved under `state/yfinance-calendar-rejections/<digest>.json`.
An evidence write failure blocks persistence. All-CLOSED batches retain this
evidence but fail with `NO_ACCEPTED_ROWS`, without calling persistence. Raw
provider response bodies remain intact. Only accepted rows enter P3 versions,
heads, and strategy inputs. Coverage reports accepted series/instruments and
the rejection manifest; partial coverage does not waive strategy coverage gates.
Read-back assessment continues to reject any rowset containing CLOSED rows.

Partial success means at least one accepted row, no provider/validation/calendar
failure, and a committed persistence receipt matching batch ID and accepted row
count. It is a collector PASS with explicit calendar rejection counts, not a
claim that every returned bar was accepted or that a report may be published.

The existing persistence boundary is unchanged: raw database commits first;
intelligence versions, heads and receipt commit together in its own transaction.
There is no distributed transaction. The local checkpoint advances atomically
only after the matching durable receipt returns. A persistence failure can leave
raw data without a checkpoint. A crash after the intelligence commit can leave
a durable batch without a fresh checkpoint; rerun completes this idempotently.
Zero accepted rows, provider failures, invalid data and evidence failures never
advance `last_success`. Failed attempts retain the previous successful checkpoint
fields but mark it FAILED, so consumers cannot use the old data as current.

Batch identity continues to depend on accepted row versions and failures, not
fetch time. Identical accepted data are deduplicated on refetch, with separate
rejection evidence for each distinct response reference. The original P3 raw
batch and receipt retain their first fetch metadata; the local checkpoint and
rejection evidence retain the latest verified fetch metadata. Cached reruns keep
the original `fetched_at`; they cannot refresh freshness by changing caller time.
The existing checkpoint expiry is strict: age must be less than the configured
limit (4500 seconds on T2). Bar dates remain exchange-local daily labels;
`latest_trade_at` stays null.

This technical change does not establish provider licensing, realtime rights,
guaranteed delay, exchange-certified units, or Gate A/B acceptance. It does not
modify existing historical P3 rows or enable opening, tail or publisher jobs.
