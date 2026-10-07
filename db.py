#!/usr/bin/env python3
"""
歷史資料庫模組 — raw layer（SQLite / PostgreSQL 後端感知）
儲存每日總體指標、每月個股基本面、法人動向
讓未來可以算「外資連續買超天數」「NIM 季度趨勢圖」「本益比歷史百分位」

後端感知（WO C2 — PostgreSQL Production Readiness）
---------------------------------------------------
raw 層遵循與 Phase 3B 持久層同一條規則：specifier 開頭為
``postgres://`` / ``postgresql://`` 即選用 PostgreSQL 後端，
其餘視為 SQLite 檔案路徑。specifier 來源：

* 環境變數 ``FIE_DB_PATH``（經 :func:`phase3.paths.macro_history_db_path`
  一併納入 — cutover 時 ops 將其指向 DSN，raw 層隨之切換）；
* 模組屬性 ``db.DB_PATH``（保留供呼叫端/測試覆寫）。

PostgreSQL DSN 絕不會被當成檔案路徑誤存成 SQLite 檔；連不上即擲出
例外（fail-closed），不做靜默 SQLite fallback。

使用方式：
  from db import save_macro_daily, save_stock_monthly, save_institutional_daily

  save_macro_daily(data, verdict, score)           # macro_daily.py 呼叫
  save_stock_monthly(code, name, sector, data)      # company_monthly.py 呼叫
  save_institutional_daily(date_str, daily_data)   # institutional.py 呼叫
  get_foreign_streak(code, days=20)                # 查連續買超天數
  raw_layer_backend()                              # 'sqlite' | 'postgres'
"""

import os
import sqlite3
from datetime import datetime, timedelta, timezone

# Phase 6.8A (Runtime Contract Normalization): the raw layer resolves its
# target through the one canonical contract
# (phase3.runtime_contract — FIE_DB_TARGET_RAW > legacy FIE_DB_PATH) and
# FAILS CLOSED when no target contract exists. The historical behaviors —
# the ImportError stub (import failure silently selecting a CWD-relative
# SQLite file) and the <FIE_DATA_DIR or CWD>/macro_history.db default —
# are abolished (WO C-3/C-5): import or resolution failure can never
# change the backend or invent a path.
from phase3.paths import macro_history_db_spec  # noqa: F401 (re-export parity)
from phase3.runtime_contract import (
    FailClosedTarget,
    assert_rehearsal_target_safe,
    classify_service_env,
    resolve_raw_target,
)

TZ_TAIPEI = timezone(timedelta(hours=8))

# The DB_PATH module attribute is kept (callers/tests patch it). When no
# raw-target contract exists at import time it is None — every use raises
# fail-closed with the recorded reason; there is NO path default.
try:
    _DEFAULT_DB_PATH: "str | None" = resolve_raw_target().value
except FailClosedTarget as _exc:  # no contract at import: fail closed at use
    _DEFAULT_DB_PATH = None
    _DEFAULT_DB_PATH_FAILURE = _exc

DB_PATH = _DEFAULT_DB_PATH

# ---------------------------------------------------------------------------
# Backend selection (WO C2) — one selection rule, shared with the bridge.

PG_URL_PREFIXES = ("postgres://", "postgresql://")


def _is_pg_spec(specifier: object) -> bool:
    return bool(specifier) and str(specifier).strip().lower().startswith(PG_URL_PREFIXES)


def raw_db_spec() -> str:
    """The raw-layer specifier currently in force (DSN or SQLite path).

    Fail closed when no raw-target contract exists — never a CWD default.
    """
    if DB_PATH is None:
        raise FailClosedTarget(
            getattr(globals().get("_DEFAULT_DB_PATH_FAILURE"), "reason",
                    "FAIL_CLOSED_DB_TARGET_REQUIRED"),
            "no raw-layer target contract (FIE_DB_TARGET_RAW / FIE_DB_PATH "
            "unset at import; no implicit default)")
    return DB_PATH


def raw_layer_backend() -> str:
    """'postgres' when the raw layer specifier selects PostgreSQL;
    'unresolved' when no target contract exists (fail closed at use)."""
    if DB_PATH is None:
        return "unresolved"
    return "postgres" if _is_pg_spec(raw_db_spec()) else "sqlite"


class _PGCursor:
    """sqlite3-cursor-shaped facade over a PostgresStore connection.

    Supports exactly the operations the raw layer uses: execute with
    positional params (``?`` placeholders translated to ``%s``), fetchone/
    fetchall with mapping-style rows (PostgresStore._Row is a dict
    subclass), and close(). commit() is a no-op: the store runs in
    autocommit mode — every write is immediately durable or it raised.
    """

    def __init__(self, store) -> None:
        self._store = store
        self._last = None

    def execute(self, sql, params=None):
        self._last = self._store.execute(
            _pg_sql(sql), tuple(params) if params else None
        )
        return self

    def fetchall(self):
        if self._last is None:
            return []
        return self._last.fetchall()

    def fetchone(self):
        if self._last is None:
            return None
        return self._last.fetchone()

    def close(self):
        pass


class _PGConnection:
    """sqlite3-connection-shaped facade for the PostgreSQL raw layer."""

    def __init__(self, dsn: str) -> None:
        from phase3.persistence.postgres import PostgresStore

        self._store = PostgresStore(dsn)

    def cursor(self):
        return _PGCursor(self._store)

    def commit(self):  # autocommit store; kept for API parity
        pass

    def close(self):
        self._store.close()


def _pg_sql(sql: str) -> str:
    """Translate a raw-layer statement for PostgreSQL.

    Plain ``?``→``%s`` for the DDL/queries; the INSERT-OR-REPLACE saves
    carry their own explicit ``ON CONFLICT`` variants (see _STMT) since
    they need per-table upsert columns that a generic rewrite cannot
    infer.
    """
    return sql.replace("?", "%s")


def get_db():
    """Open the raw layer with the backend the specifier selects.

    Phase 6.8A fail-closed semantics: no raw-target contract -> raise
    (never a CWD SQLite default). In rehearsal/test mode
    (FIE_SERVICE_ENV=test|staging) the resolved target must be
    PROVEN non-Production against the authoritative contract-file
    identities BEFORE the writer opens anything (Task F raw-layer
    boundary): disposable intelligence targets never make the raw layer
    disposable, and vice versa.
    """
    if DB_PATH is None:
        raise FailClosedTarget(
            getattr(globals().get("_DEFAULT_DB_PATH_FAILURE"), "reason",
                    "FAIL_CLOSED_DB_TARGET_REQUIRED"),
            "no raw-layer target contract; refusing to resolve an implicit "
            "database (value withheld)")
    _assert_raw_layer_rehearsal_boundary()
    if raw_layer_backend() == "postgres":
        return _PGConnection(str(raw_db_spec()).strip())
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _assert_raw_layer_rehearsal_boundary() -> None:
    """Task F: the raw layer carries the same fail-closed boundary.

    Rehearsal/test mode (FIE_SERVICE_ENV=test|staging) must not open a
    Production-equivalent raw target. Production-mode execution
    (accepted business writes) is unaffected.
    """
    mode = os.environ.get("FIE_SERVICE_ENV", "")
    if classify_service_env(mode) != "rehearsal":
        return
    from phase3.runtime_contract import parse_target
    # DB_PATH is the resolved target value (DSN or path); re-parse it for
    # identity comparison only — never re-resolve via the ambient env, so
    # a caller-patched module attribute is what gets checked.
    assert_rehearsal_target_safe(parse_target(str(DB_PATH)))


# ---------------------------------------------------------------------------
# Phase 6.3 — single-owner DDL. These three CREATE TABLE strings are the
# ONLY copies of the legacy history-table schema; phase3/backfill.py
# imports them (previously it carried a byte-duplicate of the same DDL
# that could drift out of sync with this file). The DDL is dialect-safe
# for both SQLite and PostgreSQL (TEXT/REAL/INTEGER exist in both).
# ---------------------------------------------------------------------------
DDL_MACRO_DAILY = """
    CREATE TABLE IF NOT EXISTS macro_daily (
        date TEXT PRIMARY KEY,
        us10y REAL, us2y REAL, us13w REAL,
        dxy REAL, vix REAL, usdtwd REAL,
        yield_spread REAL,
        score INTEGER,
        verdict TEXT,
        signals_json TEXT,
        created_at TEXT
    )
"""

DDL_STOCK_MONTHLY = """
    CREATE TABLE IF NOT EXISTS stock_monthly (
        date TEXT,
        code TEXT,
        name TEXT,
        sector TEXT,
        price REAL,
        eps_ttm REAL, pe_trailing REAL, pe_forward REAL,
        roe REAL, roa REAL,
        gross_margin REAL, operating_margin REAL, profit_margin REAL,
        dividend_rate REAL, dividend_yield REAL, payout_ratio REAL,
        pb_ratio REAL, revenue_growth REAL, earnings_growth REAL,
        nim_growth REAL, interest_spread REAL,
        high_52 REAL, low_52 REAL, dist_from_high REAL,
        target_mean REAL, peg_ratio REAL, market_cap REAL,
        PRIMARY KEY (date, code)
    )
"""

DDL_INSTITUTIONAL_DAILY = """
    CREATE TABLE IF NOT EXISTS institutional_daily (
        date TEXT,
        code TEXT,
        foreign_net INTEGER,
        prop_net INTEGER,
        total_net INTEGER,
        trading_days INTEGER,
        PRIMARY KEY (date, code)
    )
"""


# Per-backend statements (WO C2). The SQLite copy is the historical
# statement; the PostgreSQL copy is its dialect twin — PostgreSQL has no
# "INSERT OR REPLACE" (it would DELETE+re-INSERT); the twin is a true
# UPSERT that keeps rows stable and is the documented equivalent.
_STMT = {
    "save_macro": {
        "postgres": """
        INSERT INTO macro_daily
        (date, us10y, us2y, us13w, dxy, vix, usdtwd, yield_spread, score, verdict, signals_json, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (date) DO UPDATE SET
            us10y = EXCLUDED.us10y, us2y = EXCLUDED.us2y,
            us13w = EXCLUDED.us13w, dxy = EXCLUDED.dxy,
            vix = EXCLUDED.vix, usdtwd = EXCLUDED.usdtwd,
            yield_spread = EXCLUDED.yield_spread,
            score = EXCLUDED.score, verdict = EXCLUDED.verdict,
            signals_json = EXCLUDED.signals_json,
            created_at = EXCLUDED.created_at
        """,
    },
    "save_stock": {
        "postgres": """
        INSERT INTO stock_monthly
        (date, code, name, sector, price, eps_ttm, pe_trailing, pe_forward,
         roe, roa, gross_margin, operating_margin, profit_margin,
         dividend_rate, dividend_yield, payout_ratio, pb_ratio,
         revenue_growth, earnings_growth, nim_growth, interest_spread,
         high_52, low_52, dist_from_high, target_mean, peg_ratio, market_cap)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (date, code) DO UPDATE SET
            name = EXCLUDED.name, sector = EXCLUDED.sector,
            price = EXCLUDED.price, eps_ttm = EXCLUDED.eps_ttm,
            pe_trailing = EXCLUDED.pe_trailing, pe_forward = EXCLUDED.pe_forward,
            roe = EXCLUDED.roe, roa = EXCLUDED.roa,
            gross_margin = EXCLUDED.gross_margin,
            operating_margin = EXCLUDED.operating_margin,
            profit_margin = EXCLUDED.profit_margin,
            dividend_rate = EXCLUDED.dividend_rate,
            dividend_yield = EXCLUDED.dividend_yield,
            payout_ratio = EXCLUDED.payout_ratio,
            pb_ratio = EXCLUDED.pb_ratio,
            revenue_growth = EXCLUDED.revenue_growth,
            earnings_growth = EXCLUDED.earnings_growth,
            nim_growth = EXCLUDED.nim_growth,
            interest_spread = EXCLUDED.interest_spread,
            high_52 = EXCLUDED.high_52, low_52 = EXCLUDED.low_52,
            dist_from_high = EXCLUDED.dist_from_high,
            target_mean = EXCLUDED.target_mean,
            peg_ratio = EXCLUDED.peg_ratio, market_cap = EXCLUDED.market_cap
        """,
    },
    "save_inst": {
        "postgres": """
        INSERT INTO institutional_daily
        (date, code, foreign_net, prop_net, total_net, trading_days)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (date, code) DO UPDATE SET
            foreign_net = EXCLUDED.foreign_net,
            prop_net = EXCLUDED.prop_net,
            total_net = EXCLUDED.total_net,
            trading_days = EXCLUDED.trading_days
        """,
    },
    # SQLite uses date('now', -N days); PostgreSQL subtracts the same
    # parameterized interval text ('-60 days' is a valid interval cast) and
    # re-renders the resulting date as TEXT to compare with the TEXT column.
    "streak": {
        "postgres": """
        SELECT date, foreign_net FROM institutional_daily
        WHERE code = %s
        AND date >= to_char(CURRENT_DATE + (%s)::interval, 'YYYY-MM-DD')
        ORDER BY date DESC
        """,
    },
    "pe_percentile": {
        "postgres": """
        SELECT pe_trailing FROM stock_monthly
        WHERE code = %s AND pe_trailing IS NOT NULL AND pe_trailing > 0
        AND date >= to_char(CURRENT_DATE + (%s)::interval, 'YYYY-MM-DD')
        ORDER BY date DESC
        """,
    },
    "macro_history": {
        "postgres": """
        SELECT * FROM macro_daily
        WHERE date >= to_char(CURRENT_DATE + (%s)::interval, 'YYYY-MM-DD')
        ORDER BY date DESC
        """,
    },
}


def init_db():
    """Create tables if not exist (either backend).

    Phase 6.9B-R3: implicit raw-schema DDL is migration authority, not
    runtime authority. On a production-shaped target (fingerprint match
    against the R1 production contract) the create-if-missing call
    requires ``FIE_MIGRATION_AUTHORITY=1``; runtime authority instead
    proves the raw tables are already present (read-only) and fails
    closed with ``RAW_SCHEMA_NOT_INITIALIZED`` if they are not — no
    silent repair of Production schema.
    """
    from phase3.persistence.migration_authority import (
        auto_ddl_allowed,
        migration_authority_granted,
    )
    from phase3.runtime_contract import resolve_raw_target

    allow = migration_authority_granted()
    if not allow:
        try:
            # The guard verdicts on the specifier ACTUALLY in force —
            # the resolved raw-target contract or the caller/test
            # override on ``db.DB_PATH`` (raw_db_spec raises
            # FailClosedTarget when no target contract exists).
            allow = auto_ddl_allowed(raw_db_spec())
        except Exception:  # noqa: BLE001 - unresolvable target
            # Unprovable target: no production identity to protect (see
            # migration_authority.is_production_shaped) — the historical
            # create-if-missing behavior applies; any subsequent
            # connection failure still fails closed at use as before.
            allow = True
    if not allow:
        _assert_raw_tables_present()
        return
    conn = get_db()
    c = conn.cursor()

    # 每日總體指標 / 每月個股基本面 / 法人動向
    # (Phase 6.3: DDL 單一所有權 — 字串常數見本檔上方 DDL_*，backfill.py 引用同一份)
    c.execute(DDL_MACRO_DAILY)
    c.execute(DDL_STOCK_MONTHLY)
    c.execute(DDL_INSTITUTIONAL_DAILY)

    conn.commit()
    conn.close()


class RawSchemaNotInitialized(RuntimeError):
    """Fail-closed refusal: raw tables are absent and runtime authority
    forbids creating them (raise the stable code, never repair)."""

    code = "RAW_SCHEMA_NOT_INITIALIZED"


def _assert_raw_tables_present():
    conn = get_db()
    c = conn.cursor()
    try:
        for table in ("macro_daily", "stock_monthly", "institutional_daily"):
            try:
                c.execute(f"SELECT 1 FROM {table} LIMIT 1")
                c.fetchone()
            except RawSchemaNotInitialized:
                raise
            except Exception:  # noqa: BLE001
                raise RawSchemaNotInitialized(
                    "raw production DB target lacks required table "
                    f"{table!r}; runtime authority forbids implicit DDL — "
                    "run the migration/bootstrap context "
                    "(FIE_MIGRATION_AUTHORITY=1) to apply the raw schema"
                ) from None
    finally:
        conn.close()


def save_macro_daily(data, verdict, score, signals):
    """Save daily macro indicators to DB."""
    init_db()
    conn = get_db()
    c = conn.cursor()

    import json
    now = datetime.now(TZ_TAIPEI).isoformat()

    # Use the date from US10Y (market date), fallback to today
    date_str = data.get("US10Y", {}).get("date")
    if not date_str:
        date_str = datetime.now(TZ_TAIPEI).strftime("%Y-%m-%d")

    if raw_layer_backend() == "postgres":
        c.execute(_STMT["save_macro"]["postgres"], (
            date_str,
            data.get("US10Y", {}).get("value"),
            data.get("US2Y", {}).get("value"),
            data.get("US13W", {}).get("value"),
            data.get("DXY", {}).get("value"),
            data.get("VIX", {}).get("value"),
            data.get("USDTWD", {}).get("value"),
            data.get("YIELD_SPREAD", {}).get("value"),
            score,
            verdict,
            json.dumps(signals, ensure_ascii=False),
            now,
        ))
    else:
        c.execute("""
            INSERT OR REPLACE INTO macro_daily
            (date, us10y, us2y, us13w, dxy, vix, usdtwd, yield_spread, score, verdict, signals_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            date_str,
            data.get("US10Y", {}).get("value"),
            data.get("US2Y", {}).get("value"),
            data.get("US13W", {}).get("value"),
            data.get("DXY", {}).get("value"),
            data.get("VIX", {}).get("value"),
            data.get("USDTWD", {}).get("value"),
            data.get("YIELD_SPREAD", {}).get("value"),
            score,
            verdict,
            json.dumps(signals, ensure_ascii=False),
            now,
        ))
    conn.commit()
    conn.close()

def save_stock_monthly(code, name, sector, data):
    """Save monthly stock fundamentals to DB."""
    init_db()
    conn = get_db()
    c = conn.cursor()

    date_str = datetime.now(TZ_TAIPEI).strftime("%Y-%m-%d")

    params = (
        date_str, code, name, sector,
        data.get("price"), data.get("eps_ttm"), data.get("pe_trailing"), data.get("pe_forward"),
        data.get("roe"), data.get("roa"), data.get("gross_margin"), data.get("operating_margin"), data.get("profit_margin"),
        data.get("dividend_rate"), data.get("dividend_yield"), data.get("payout_ratio"), data.get("pb_ratio"),
        data.get("revenue_growth"), data.get("earnings_growth"),
        data.get("nim_growth"), data.get("interest_spread"),
        data.get("52w_high"), data.get("52w_low"), data.get("dist_from_high"),
        data.get("target_mean"), data.get("peg_ratio"), data.get("market_cap"),
    )
    if raw_layer_backend() == "postgres":
        c.execute(_STMT["save_stock"]["postgres"], params)
    else:
        c.execute("""
            INSERT OR REPLACE INTO stock_monthly
            (date, code, name, sector, price, eps_ttm, pe_trailing, pe_forward,
             roe, roa, gross_margin, operating_margin, profit_margin,
             dividend_rate, dividend_yield, payout_ratio, pb_ratio,
             revenue_growth, earnings_growth, nim_growth, interest_spread,
             high_52, low_52, dist_from_high, target_mean, peg_ratio, market_cap)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, params)
    conn.commit()
    conn.close()

def save_institutional_snapshot(code, foreign_net, prop_net, total_net, trading_days):
    """Save institutional data snapshot (cumulative for the period)."""
    init_db()
    conn = get_db()
    c = conn.cursor()

    date_str = datetime.now(TZ_TAIPEI).strftime("%Y-%m-%d")

    if raw_layer_backend() == "postgres":
        c.execute(_STMT["save_inst"]["postgres"], (
            date_str, code, foreign_net, prop_net, total_net, trading_days))
    else:
        c.execute("""
            INSERT OR REPLACE INTO institutional_daily
            (date, code, foreign_net, prop_net, total_net, trading_days)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (date_str, code, foreign_net, prop_net, total_net, trading_days))
    conn.commit()
    conn.close()

# === 查詢函數 ===

def get_foreign_streak(code, days=60):
    """查詢某檔股票外資連續買超/賣超天數（基於每日 snapshot 差值）。

    注意：institutional_daily 存的是「累積近月」值，
    所以連續天數 = snapshot 遞增的天數（每天新增買超 = 累積值增加）。

    Returns: (streak_days, direction) — 正=連續買超, 負=連續賣超
    """
    init_db()
    conn = get_db()
    c = conn.cursor()

    window = f"-{days} days"
    if raw_layer_backend() == "postgres":
        rows = c.execute(_STMT["streak"]["postgres"], (code, window)).fetchall()
    else:
        rows = c.execute("""
            SELECT date, foreign_net FROM institutional_daily
            WHERE code = ? AND date >= date('now', ?)
            ORDER BY date DESC
        """, (code, window)).fetchall()

    if len(rows) < 2:
        return 0, "N/A"

    # 計算每日 delta（最新 - 前一天）
    streak = 0
    direction = "N/A"
    for i in range(len(rows) - 1):
        delta = rows[i]["foreign_net"] - rows[i + 1]["foreign_net"]
        if i == 0:
            direction = "買超" if delta > 0 else ("賣超" if delta < 0 else "持平")
        if direction == "買超" and delta > 0:
            streak += 1
        elif direction == "賣超" and delta < 0:
            streak += 1
        else:
            break

    return streak, direction

def get_pe_percentile(code, months=12):
    """查詢某檔股票本益比歷史百分位。"""
    init_db()
    conn = get_db()
    c = conn.cursor()

    window = f"-{months * 30} days"
    if raw_layer_backend() == "postgres":
        rows = c.execute(_STMT["pe_percentile"]["postgres"], (code, window)).fetchall()
    else:
        rows = c.execute("""
            SELECT pe_trailing FROM stock_monthly
            WHERE code = ? AND pe_trailing IS NOT NULL AND pe_trailing > 0
            AND date >= date('now', ?)
            ORDER BY date DESC
        """, (code, window)).fetchall()

    if len(rows) < 3:
        return None

    pes = [r["pe_trailing"] for r in rows]
    current = pes[0]
    sorted_pes = sorted(pes)
    rank = sum(1 for p in sorted_pes if p <= current)
    percentile = rank / len(sorted_pes) * 100

    return {
        "current_pe": current,
        "percentile": percentile,
        "min_pe": min(pes),
        "max_pe": max(pes),
        "data_points": len(pes),
    }

def get_macro_history(days=30):
    """取得近 N 天總體指標歷史。"""
    init_db()
    conn = get_db()
    c = conn.cursor()

    window = f"-{days} days"
    if raw_layer_backend() == "postgres":
        rows = c.execute(_STMT["macro_history"]["postgres"], (window,)).fetchall()
    else:
        rows = c.execute("""
            SELECT * FROM macro_daily
            WHERE date >= date('now', ?)
            ORDER BY date DESC
        """, (window,)).fetchall()

    return [dict(r) for r in rows]


if __name__ == "__main__":
    init_db()
    print("✅ DB initialized at", DB_PATH, f"(backend={raw_layer_backend()})")

    # Show table stats
    conn = get_db()
    c = conn.cursor()
    for table in ["macro_daily", "stock_monthly", "institutional_daily"]:
        count = c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"  {table}: {count} rows")
    conn.close()