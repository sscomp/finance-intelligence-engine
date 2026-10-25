"""T2-owned ingestion, monitoring and report execution plane."""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import runpy
import sqlite3
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
TZ = ZoneInfo("Asia/Taipei")
_DEPLOY = runpy.run_path(str(ROOT/'src/runtime_config.py'))['load'](ROOT)
BASE = _DEPLOY['api_base']
PRINCIPAL = _DEPLOY['api_principal']
COLLECTORS = ("twse_valuation", "twse_prices", "twse_institutional", "fred_macro")
RESEARCH_COLLECTORS = {"research_rss": "industry_rss", "research_company": "company_fundamentals", "yfinance": "yfinance_daily"}
PARITY = {"opening", "tail", "publisher", "macro_morning", "industry_weekly", "company_monthly", "constituents_review", "phase4_shadow", "watchdog_open", "watchdog_tail"}
JOBS = {"monitor", "intraday", *COLLECTORS, *PARITY, *RESEARCH_COLLECTORS}
AUDIT_FILES = {"src/runtime_config.py", "config/deployment.json", "src/yfinance_source.py", "config/yfinance.json", "src/research.py", "config/research.json", "src/fie_t2.py", "src/reliability.py", "src/health_watch.py", "src/ingestion.py", "src/score_worker.py", "config/instance.json", "config/calendar.json", "config/side-effects.json", "config/ingestion.json", "config/universe.json"}
DB_TARGETS = [_DEPLOY['raw_database'], _DEPLOY['intelligence_database']]
UTC = dt.timezone.utc


class Refused(Exception):
    """Safe category only; underlying exception values must not reach logs."""


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def protected(path):
    s = path.lstat()
    if not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid() or s.st_mode & 0o077:
        raise Refused("PRIVATE_FILE_REQUIRED")
    return path.read_bytes()


def atomic(path, value):
    """Same-filesystem replace; interrupted writes never publish partial JSON."""
    data = value.encode() if isinstance(value, str) else canonical(value) + b"\n"
    fd, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Client:
    def __init__(self, root=ROOT):
        self.token = protected(root / "secrets/api-token").decode().strip()
        if not self.token or "\n" in self.token or "\r" in self.token:
            raise Refused("CREDENTIAL_INVALID")
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def read(self, operation, *parts, limit=50):
        if operation in {"health", "ready", "live", "latest"} and not parts:
            path = {"health": "/v1/health", "ready": "/readyz", "live": "/healthz",
                    "latest": "/v1/intelligence/latest"}[operation]
            if operation == "latest":
                if not isinstance(limit, int) or not 1 <= limit <= 50:
                    raise Refused("LIMIT_INVALID")
                path += "?limit=" + str(limit)
        elif operation in {"entity", "freshness"} and len(parts) == 2:
            if parts[0] not in {"company", "industry", "macro", "portfolio"}:
                raise Refused("ENTITY_KIND_INVALID")
            path = ("/v1/intelligence/entity/" if operation == "entity" else "/v1/freshness/")
            path += "/".join(self.segment(p) for p in parts)
        elif operation == "evidence" and len(parts) == 1:
            path = "/v1/evidence/" + self.segment(parts[0])
        else:
            raise Refused("READ_OPERATION_REQUIRED")
        request = urllib.request.Request(BASE + path, method="GET", headers={
            "Authorization": "Bearer " + self.token, "Accept": "application/json"})
        try:
            with self.opener.open(request, timeout=20 if operation == "latest" else 8) as r:
                if r.status != 200:
                    raise Refused("API_UNAVAILABLE")
                body = r.read(8 * 1024 * 1024 + 1)
                if len(body) > 8 * 1024 * 1024:
                    raise Refused("API_RESPONSE_TOO_LARGE")
                result = json.loads(body)
        except urllib.error.HTTPError as e:
            reason = "API_AUTH_REFUSED" if e.code in {401, 403} else "API_UNAVAILABLE"
            e.close()
            raise Refused(reason) from None
        except (OSError, ValueError, TimeoutError):
            raise Refused("API_UNAVAILABLE") from None
        if not isinstance(result, dict) or result.get("status") != "ok":
            raise Refused("API_ENVELOPE_INVALID")
        if operation != "live" and (result.get("principal_id") != PRINCIPAL or result.get("schema_version") != "6.5"):
            raise Refused("API_IDENTITY_REFUSED")
        return result

    @staticmethod
    def segment(value):
        if not isinstance(value, str) or not value or len(value) > 256 or any(c in value for c in "/\\\r\n\x00?#") or value in {".", ".."}:
            raise Refused("IDENTIFIER_INVALID")
        return urllib.parse.quote(value, safe="")


def load_config(root):
    cfg = json.loads(protected(root / "config/instance.json"))
    if cfg.get("instance_id") != "T2" or cfg.get("api_base") != BASE or cfg.get("timezone") != "Asia/Taipei":
        raise Refused("INSTANCE_IDENTITY_REFUSED")
    if set(cfg.get("enabled_jobs", [])) - JOBS:
        raise Refused("JOB_NOT_AUDITED")
    if cfg.get("external_writes") is not False or cfg.get("database_writes") is not True or cfg.get("database_targets") != DB_TARGETS:
        raise Refused("WRITE_POLICY_REFUSED")
    if not isinstance(cfg.get("max_data_age_hours"), (int, float)) or not 1 <= cfg["max_data_age_hours"] <= 72:
        raise Refused("AGE_THRESHOLD_INVALID")
    return cfg


def gate(root):
    cfg = load_config(root)
    approval = json.loads(protected(root / "config/activation.json"))
    if approval.get("instance_id") != "T2" or approval.get("result") != "PASS":
        raise Refused("ACTIVATION_REQUIRED")
    expected = AUDIT_FILES
    if set(approval.get("files", {})) != expected:
        raise Refused("AUDIT_COVERAGE_REQUIRED")
    for p, digest in approval["files"].items():
        protected(root / p)
        if sha(root / p) != digest:
            raise Refused("AUDIT_DIGEST_CHANGED")
    ledger = json.loads(protected(root / "config/side-effects.json"))
    if ledger.get("instance_id") != "T2" or set(ledger.get("jobs", {})) != JOBS:
        raise Refused("SIDE_EFFECT_AUDIT_REQUIRED")
    for job in cfg["enabled_jobs"]:
        effects = ledger["jobs"][job]
        allowed = DB_TARGETS if job in COLLECTORS or job in RESEARCH_COLLECTORS else [DB_TARGETS[1]] if job in {"industry_weekly", "company_monthly"} else []
        if effects.get("external_writes") != [] or effects.get("database_writes") != allowed or effects.get("verdict") != "PASS":
            raise Refused("SHARED_WRITER_REFUSED")
    ingest_cfg = json.loads(protected(root / "config/ingestion.json"))
    if ingest_cfg.get("instance_id") != "T2" or set(ingest_cfg.get("enabled_sources", [])) != set(COLLECTORS) or ingest_cfg.get("rolling_days") != 7 or ingest_cfg.get("external_publishing") is not False:
        raise Refused("INGESTION_CONFIG_REFUSED")
    return cfg


def reliability():
    return runpy.run_path(str(ROOT / "src/reliability.py"))


def calendar_state(root, day):
    return reliability()["calendar_state"](root, day)


def due_jobs(root, now):
    return reliability()["due"](root, now)


def parse_time(value):
    try:
        p = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return p if p.tzinfo else p.replace(tzinfo=UTC)
    except (ValueError, AttributeError, TypeError):
        return None


def data_quality(items, now, max_hours):
    computed = [parse_time(x.get("computed_at")) for x in items]
    sources = [parse_time(x.get("score_source_date") or (x.get("freshness") or {}).get("source_date")) for x in items]
    if not items or any(x is None for x in computed + sources):
        return {"state": "UNAVAILABLE", "reason": "MISSING_OR_INVALID_DATA_TIME"}
    ages = [(now - x).total_seconds() / 3600 for x in computed + sources]
    if min(ages) < -0.1:
        return {"state": "UNAVAILABLE", "reason": "FUTURE_DATA_TIME"}
    oldest = max(ages)
    return {"state": "STALE" if oldest > max_hours else "CURRENT_WITHIN_THRESHOLD",
            "oldest_age_hours": round(oldest, 2), "threshold_hours": max_hours,
            "latest_computed_at": max(computed).isoformat(),
            "oldest_source_date": min(sources).date().isoformat(),
            "api_freshness_states": sorted({(x.get("freshness") or {}).get("state", "UNKNOWN") for x in items}),
            "meaning": "Age guard over returned score/source dates; API policy is reported separately. Independent daily source ingestion; no live quote feed."}


def collect(root, now, client):
    cfg = load_config(root)
    started = time.monotonic()
    client.read("ready")
    health = client.read("health")
    latest = client.read("latest", limit=50)
    items = latest.get("payload")
    if not isinstance(items, list) or any(not isinstance(x, dict) for x in items):
        raise Refused("API_PAYLOAD_INVALID")
    # API freshness examines the entity's latest signal, which can belong to
    # another historical provider. Our immutable score refs also carry the
    # exact observation date used by that score; preserve both dates.
    for item in items:
        for ref in item.get("evidence_refs", []):
            match = re.search(r":(\d{4}-\d{2}-\d{2}):[a-f0-9]{64}$", ref.get("ref", "")) if ref.get("kind") == "score" else None
            if match:
                item["score_source_date"] = match.group(1)
                item["score_coverage"] = "PARTIAL_T2_SOURCE; missing dimensions use pinned scorer defaults"
                break
    quality = data_quality(items, now, cfg["max_data_age_hours"])
    dep = reliability()["dependency"](root, "intraday", now)
    quality["legacy_age_guard"] = quality["state"]
    quality["state"] = "CURRENT_DAILY_OBSERVATIONS" if dep["state"] == "PASS" else "DEGRADED"
    quality["dependency"] = dep
    quality["meaning"] = "Authoritative T2 calendar/per-feed policy. Raw API FRESH is provenance only; daily data is not an intraday quote."
    row_states = []
    for item in items:
        feed = "fred_macro" if item.get("kind")=="macro" else "twse_valuation"
        observed = item.get("score_source_date") or (item.get("freshness") or {}).get("source_date")
        item["t2_freshness"] = reliability()["freshness"](root,feed,observed,now)
        row_states.append(item["t2_freshness"]["state"])
    if any(state not in reliability()["GOOD"] for state in row_states):
        quality["state"] = "DEGRADED"
        quality["reason"] = "SCORE_OBSERVATIONS_NOT_CURRENT_BY_FEED_CALENDAR"
    rows = []
    for x in items:
        score = x.get("score")
        if not isinstance(score, (int, float)) or not math.isfinite(score):
            raise Refused("API_SCORE_INVALID")
        rows.append({k: x.get(k) for k in ["kind", "entity_id", "score", "as_of", "computed_at", "freshness", "evidence_refs", "score_source_date", "score_coverage", "t2_freshness"]})
    return {"instance_id": "T2", "generated_at": now.isoformat(), "endpoint": BASE,
            "api_available": True, "api_health": health["payload"].get("status"),
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
            "data_quality": quality, "rows": rows, "row_count": len(rows),
            "data_origin": "T2 P3 with independent TWSE and FRED ingestion; historical M2 restore retained; partial source coverage",
            "ingestion": ingestion_status(root, now),
            "closing_prices": market_data(root, "twse_prices", now),
            "calendar_state": calendar_state(root, now.astimezone(TZ).date()),
            "external_writes": False, "database_writes": False}


def unavailable(now, reason):
    return {"instance_id": "T2", "generated_at": now.isoformat(), "api_available": False,
            "data_quality": {"state": "UNAVAILABLE", "reason": reason}, "rows": [], "row_count": 0,
            "external_writes": False, "database_writes": False}


def safe_text(value):
    return re.sub(r"[\r\n|<>`\\]", " ", str(value))[:256]


def render(report):
    q = report["data_quality"]
    lines = ["# T2 FIE intraday snapshot", "", "Generated: " + report["generated_at"],
             "", "Data status: **" + q["state"] + "**", "",
             "T2 P3 with independent TWSE/FRED daily observations and preserved historical data. Partial source coverage; no live quote feed.",
             "Source data may predate score computation. T2 calendar/per-feed freshness is authoritative; raw API labels are provenance only.",
             "", "Quality: " + json.dumps(q, ensure_ascii=False, sort_keys=True), "",
             "Ingestion: " + json.dumps(report.get("ingestion", {}), ensure_ascii=False, sort_keys=True), "",
             "| Kind | Entity | Score | Score computed | Source date | API freshness |",
             "| --- | --- | ---: | --- | --- | --- |"]
    for row in report["rows"]:
        f = row.get("freshness") or {}
        lines.append("| " + " | ".join(safe_text(x) for x in [row["kind"], row["entity_id"], row["score"], row["computed_at"], row.get("score_source_date") or f.get("source_date"), f.get("state")]) + " |")
    prices = report.get("closing_prices", {})
    lines += ["", "## Published daily closing prices", "", "Source observation status: " + prices.get("source_freshness", "UNAVAILABLE"), "", "| Company | Close TWD | Observation date |", "| --- | ---: | --- |"]
    for row in prices.get("observations", []):
        lines.append("| " + " | ".join(safe_text(row[k]) for k in ["entity_id", "value", "source_date"]) + " |")
    return "\n".join(lines) + "\n"


@contextlib.contextmanager
def connection(root):
    c = sqlite3.connect(root / "state/scheduler.sqlite3", timeout=5)
    c.execute("PRAGMA busy_timeout=5000")
    c.execute("CREATE TABLE IF NOT EXISTS jobs(instance TEXT, job TEXT, slot TEXT, status TEXT, attempts INTEGER, finished_at TEXT, error TEXT, PRIMARY KEY(instance,job,slot))")
    c.execute("CREATE TABLE IF NOT EXISTS alerts(id INTEGER PRIMARY KEY, at TEXT, state TEXT)")
    c.commit()
    try:
        yield c
    finally:
        c.close()


@contextlib.contextmanager
def instance_lock(root):
    with open(root / "state/scheduler.lock", "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused("INSTANCE_BUSY") from None
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def run_job(root, job, slot, now, client=None):
    cfg = gate(root)
    if job not in cfg["enabled_jobs"] or len(slot) > 80:
        raise Refused("JOB_DISABLED")
    with connection(root) as c:
        previous = c.execute("SELECT status,attempts,finished_at FROM jobs WHERE instance='T2' AND job=? AND slot=?", (job, slot)).fetchone()
        if previous and (previous[0] == "done" or previous[1] >= 3):
            return {"job": job, "result": "SKIPPED", "slot": slot}
        if previous and previous[0] in {"failed", "running"}:
            finished = parse_time(previous[2])
            wait = 60 * (2 ** (previous[1] - 1))
            if finished and (now-finished).total_seconds() < wait:
                return {"job":job,"slot":slot,"result":"BACKOFF","retry_after_seconds":wait}
        attempt = previous[1] + 1 if previous else 1
        c.execute("INSERT OR REPLACE INTO jobs VALUES('T2',?,?, 'running',?,?,NULL)", (job, slot, attempt, now.isoformat()))
        c.commit()
        error = None
        if job in RESEARCH_COLLECTORS:
            try:
                module=runpy.run_path(str(root/('src/yfinance_source.py' if job=='yfinance' else 'src/research.py')))
                detail=module['run'](now,root) if job=='yfinance' else module['run'](RESEARCH_COLLECTORS[job],now,root)
                result={'job':job,'slot':slot,'result':detail['result'],'batch_id':detail['batch_id'],'attempt':attempt}
            except Exception:
                result={'job':job,'slot':slot,'result':'FAILED','reason':'RESEARCH_COLLECTION_FAILED','attempt':attempt}
            c.execute("UPDATE jobs SET status=?,finished_at=?,error=? WHERE instance='T2' AND job=? AND slot=?", ('done' if result['result']=='PASS' else 'failed',now.isoformat(),result.get('reason'),job,slot))
            c.commit()
            return result
        if job in PARITY:
            try:
                result = reliability()["execute"](root, job, slot, now)
            except Exception:
                result = {"result":"FAILED", "reason":"LOCAL_JOB_EXECUTION_FAILED"}
            result = {"instance_id":"T2", "id":"t2-fie-"+job, "job":job, "slot":slot, "attempt":attempt, "at":now.isoformat(), **result}
            failed = result["result"] not in {"PASS", "CLOSED"}
            atomic(reliability()["receipt_path"](root,job,slot), result)
            atomic(root / "state" / ("job-"+job+".json"), result)
            c.execute("UPDATE jobs SET status=?,finished_at=?,error=? WHERE instance='T2' AND job=? AND slot=?", ("failed" if failed else "done", now.isoformat(), result.get("reason"), job,slot))
            c.commit()
            return result
        if job in COLLECTORS:
            p = root / "state" / ("ingestion-" + job + ".json")
            prior = json.loads(p.read_text()) if p.exists() else {}
            try:
                module = runpy.run_path(str(root / "src/ingestion.py"))
                result = module["run"](job, now, root)
                summary = {"instance_id": "T2", "source": job, "result": "PASS", "last_attempt": now.isoformat(), "last_success": now.isoformat(), "slot": slot, "attempt": attempt, **result}
            except Exception as exc:
                reason = str(exc)
                error = reason if re.fullmatch(r"[A-Z_]{3,100}", reason) else "SOURCE_INGESTION_FAILED"
                summary = {**prior, "instance_id": "T2", "source": job, "result": "FAILED", "last_attempt": now.isoformat(), "slot": slot, "attempt": attempt, "error": error}
            atomic(p, summary)
            c.execute("UPDATE jobs SET status=?,finished_at=?,error=? WHERE instance='T2' AND job=? AND slot=?", ("failed" if error else "done", now.isoformat(), error, job, slot))
            c.commit()
            return {"job": job, "slot": slot, "result": "FAILED" if error else "PASS", "attempt": attempt, "batch_id": summary.get("batch_id")}
        try:
            report = collect(root, now, client or Client(root))
        except Exception as e:
            error = str(e) if isinstance(e, Refused) else "COLLECTION_FAILED"
            report = unavailable(now, error)
        if job == "intraday":
            if report["data_quality"]["state"] not in {"CURRENT_DAILY_OBSERVATIONS"}:
                error = error or "DEPENDENCY_DEGRADED"
                report["rows"] = []
                report["row_count"] = 0
            report["coverage"] = "Daily-data snapshot only; no opening/tail intraday inference"
            name = "t2-intraday-" + hashlib.sha256(slot.encode()).hexdigest()[:20]
            atomic(root / "reports" / (name + ".json"), report)
            atomic(root / "reports" / (name + ".md"), render(report))
            # Published only after both immutable-by-slot artifacts exist.
            atomic(root / "state/latest-report.json", {"instance_id": "T2", "slot": slot, "file": name, "generated_at": now.isoformat(), "quality": report["data_quality"]["state"]})
        else:
            summary = {k: v for k, v in report.items() if k != "rows"}
            vfs = os.statvfs(root)
            summary["disk_free_bytes"] = vfs.f_bavail * vfs.f_frsize
            severity = "UNAVAILABLE" if error else report["data_quality"]["state"]
            ingestion = summary.get("ingestion", {})
            if not error and ingestion.get("pipeline_state") != "CURRENT":
                severity = "INGESTION_" + ingestion.get("pipeline_state", "UNAVAILABLE")
            if not error and summary.get("calendar_state") in {"CALENDAR_EXPIRED", "CALENDAR_UNAVAILABLE_OR_EXPIRED"}:
                severity = "CALENDAR_EXPIRED"
            if summary["disk_free_bytes"] < cfg["min_free_disk_bytes"]:
                severity = "DISK_LOW"
            prior = root / "state/monitor.json"
            old = json.loads(prior.read_text()).get("alert_state") if prior.exists() else None
            summary["alert_state"] = severity
            if old != severity:
                c.execute("INSERT INTO alerts(at,state) VALUES(?,?)", (now.isoformat(), severity))
                atomic(root / "state/latest-alert.json", {"instance_id": "T2", "at": now.isoformat(), "state": severity, "delivery": "local-only"})
            atomic(prior, summary)
        c.execute("UPDATE jobs SET status=?,finished_at=?,error=? WHERE instance='T2' AND job=? AND slot=?", ("failed" if error else "done", now.isoformat(), error, job, slot))
        c.commit()
    return {"job": job, "slot": slot, "result": "FAILED" if error else "PASS", "data_quality": report["data_quality"]["state"], "attempt": attempt}


def tick(root=ROOT, now=None):
    now = now or dt.datetime.now(UTC)
    cfg = gate(root)
    with instance_lock(root):
        atomic(root / "state/heartbeat.json", {"instance_id": "T2", "at": now.isoformat(), "pid": os.getpid(), "phase": "running", "results": []})
        results = []
        for j, slot in due_jobs(root, now):
            if j not in cfg["enabled_jobs"]: continue
            try:
                atomic(root / "state/heartbeat.json", {"instance_id":"T2","at":dt.datetime.now(UTC).isoformat() if root==ROOT else now.isoformat(),"phase":"running","job":j,"pid":os.getpid()})
                results.append(run_job(root, j, slot, dt.datetime.now(UTC) if root == ROOT else now))
            except Exception:
                results.append({"job":j,"slot":slot,"result":"FAILED","reason":"JOB_BOUNDARY_FAILURE"})
        finished = dt.datetime.now(UTC)
        atomic(root / "state/heartbeat.json", {"instance_id": "T2", "at": finished.isoformat(), "started_at": now.isoformat(), "phase": "finished", "pid": os.getpid(), "results": results, "calendar_state": calendar_state(root, now.astimezone(TZ).date())})
        return results


def market_data(root=ROOT, source="twse_prices", now=None):
    if source not in COLLECTORS:
        raise Refused("SOURCE_NOT_ALLOWED")
    now = now or dt.datetime.now(UTC)
    p = root / "state" / ("market-" + source + ".json")
    if not p.exists():
        return {"instance_id":"T2", "source":source, "source_freshness":"UNAVAILABLE", "observations":[]}
    result = json.loads(protected(p))
    result["read_at"] = now.isoformat()
    summary = reliability()["source_status"](root,source,now)
    result["source_freshness"] = summary["source_freshness"]
    result["freshness"] = summary["freshness"]
    result["pipeline_state"] = summary["pipeline_state"]
    stamp = parse_time(result.get("source_date"))
    result["source_age_hours"] = round((now-stamp).total_seconds()/3600,2) if stamp else None
    return result


def ingestion_status(root=ROOT, now=None):
    now = now or dt.datetime.now(UTC)
    sources = {source: reliability()["source_status"](root,source,now) for source in COLLECTORS}
    states = {v["pipeline_state"] for v in sources.values()}
    return {"instance_id":"T2", "pipeline_state":"CURRENT" if states=={"CURRENT"} else "FAILED" if "FAILED" in states else "STALE_OR_MISSING", "sources":sources,
            "freshness_policy":"T2_CALENDAR_V2; per-feed daily publication deadlines, TWSE closures and separate US/FRED releases; no absolute 36h freshness decision"}


def status(root=ROOT):
    out = {"instance_id": "T2"}
    now = dt.datetime.now(UTC)
    for key in ["heartbeat", "monitor", "latest-report", "latest-alert"]:
        p = root / "state" / (key + ".json")
        out[key] = json.loads(p.read_text()) if p.exists() else None
    heartbeat = out["heartbeat"]
    stamp = parse_time(heartbeat.get("at")) if heartbeat else None
    out["scheduler_state"] = "CURRENT" if stamp and 0 <= (now - stamp).total_seconds() < 180 else "STALE_OR_STOPPED"
    monitor = out["monitor"]
    stamp = parse_time(monitor.get("generated_at")) if monitor else None
    out["monitor_observation"] = "CURRENT" if stamp and 0 <= (now - stamp).total_seconds() < 420 else "STALE_OR_MISSING"
    out["ingestion"] = ingestion_status(root, now)
    out["jobs"] = {j: json.loads((root/"state"/("job-"+j+".json")).read_text()) if (root/"state"/("job-"+j+".json")).exists() else {"result":"BLOCKED" if j in reliability()["BLOCKED"] else "NEVER_RUN", "reason":reliability()["BLOCKED"].get(j)} for j in PARITY}
    out['research'] = runpy.run_path(str(root/'src/research.py'))['read_status'](root,now)
    out["calendar_state"] = reliability()["calendar_state"](root,now.astimezone(TZ).date(),now)
    out["next_trading_day"] = str(reliability()["trading_day"](root,now.astimezone(TZ).date(),1,now=now))
    independent=root/"state/independent-health.json"
    out["independent_monitor"] = json.loads(independent.read_text()) if independent.exists() else {"result":"UNAVAILABLE"}
    return out


def main(argv=None):
    os.umask(0o077)
    p = argparse.ArgumentParser(description="T2 FIE independent ingestion and reporting")
    p.add_argument("operation", choices=["research", "tick", "scheduled-job", "schedules", "independent-health", "ingest-once", "ingestion", "market", "report-once", "status", "report", "health", "ready", "latest", "entity", "freshness", "evidence"])
    p.add_argument("parts", nargs="*")
    a = p.parse_args(argv)
    try:
        if a.operation == "research":
            module=runpy.run_path(str(ROOT/'src/research.py'))
            if not a.parts or a.parts==['sources']:result=module['read_status'](ROOT,dt.datetime.now(UTC))
            elif len(a.parts)==1 and a.parts[0] in {'industry_weekly','company_monthly'}:result=module['read_report'](a.parts[0],ROOT)
            elif len(a.parts)==1 and a.parts[0] in {'opening','tail'}:
                result=runpy.run_path(str(ROOT/'src/yfinance_source.py'))['make_report'](a.parts[0],dt.datetime.now(UTC),ROOT)
            else:raise Refused('RESEARCH_READ_ARGUMENTS_INVALID')
        elif a.operation == "tick":
            result = tick()
        elif a.operation == "schedules":
            result = {"instance_id":"T2", "schedules":reliability()["schedules"](ROOT,dt.datetime.now(UTC))}
        elif a.operation == "independent-health":
            now=dt.datetime.now(UTC)
            result=status()
            result={"instance_id":"T2","at":now.isoformat(),"result":"PASS" if result["scheduler_state"]=="CURRENT" and result["monitor_observation"]=="CURRENT" and result["ingestion"]["pipeline_state"]=="CURRENT" and result["calendar_state"]!="CALENDAR_UNAVAILABLE_OR_EXPIRED" else "DEGRADED", "scheduler_state":result["scheduler_state"], "monitor_observation":result["monitor_observation"], "pipeline_state":result["ingestion"]["pipeline_state"], "calendar_state":result["calendar_state"], "delivery":"local-only"}
            atomic(ROOT/"state/independent-health.json",result)
        elif a.operation == "scheduled-job":
            if len(a.parts)!=1 or a.parts[0] not in PARITY:raise Refused("JOB_NOT_AUDITED")
            now=dt.datetime.now(UTC);job=a.parts[0]
            slots=[slot for j,slot in due_jobs(ROOT,now) if j==job]
            if not slots:result={"job":job,"result":"OUTSIDE_BOUNDED_SLOT"}
            else:
                with instance_lock(ROOT):result=run_job(ROOT,job,slots[0],now)
        elif a.operation == "ingest-once":
            if a.parts and (len(a.parts) != 1 or a.parts[0] not in COLLECTORS):
                raise Refused("SOURCE_NOT_ALLOWED")
            now = dt.datetime.now(UTC)
            with instance_lock(ROOT):
                result = [run_job(ROOT, j, "manual:" + now.isoformat(), now) for j in (a.parts or COLLECTORS)]
        elif a.operation == "ingestion":
            result = ingestion_status()
        elif a.operation == "market":
            if len(a.parts)>1: raise Refused("SOURCE_NOT_ALLOWED")
            result = market_data(ROOT, a.parts[0] if a.parts else "twse_prices")
        elif a.operation == "report-once":
            now = dt.datetime.now(UTC)
            with instance_lock(ROOT):
                result = run_job(ROOT, "intraday", "manual:" + now.isoformat(), now)
        elif a.operation == "status":
            result = status()
        elif a.operation == "report":
            pointer = json.loads((ROOT / "state/latest-report.json").read_text())
            name = pointer["file"]
            if not re.fullmatch(r"t2-intraday-[a-f0-9]{20}", name):
                raise Refused("REPORT_ID_INVALID")
            result = json.loads((ROOT / "reports" / (name + ".json")).read_text())
            read_at = dt.datetime.now(UTC)
            result["read_at"] = read_at.isoformat()
            generated = parse_time(result.get("generated_at"))
            age = (read_at - generated).total_seconds() if generated else None
            result["report_age_seconds"] = round(age, 1) if age is not None else None
            result["report_observation"] = "CURRENT" if age is not None and 0 <= age < 2700 else "STALE_OR_INVALID"
            result["read_dependency"] = reliability()["dependency"](ROOT,"intraday",read_at)
            if result["read_dependency"]["state"]!="PASS" or result["report_observation"]!="CURRENT":
                result["data_quality"]["state"]="DEGRADED"
        else:
            load_config(ROOT)
            result = Client().read(a.operation, *a.parts)
            result["raw_api_freshness_semantics"] = "Provider/API policy only; use t2_freshness for authoritative per-feed calendar status"
            result["t2_freshness"] = ingestion_status(ROOT)
            def normalize(value):
                if isinstance(value,dict):
                    if "freshness" in value and isinstance(value["freshness"],dict):
                        value["raw_api_freshness"]=value.pop("freshness")
                    for key,child in list(value.items()):
                        if key not in {"raw_api_freshness","t2_freshness"}:normalize(child)
                elif isinstance(value,list):
                    for child in value:normalize(child)
            normalize(result)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        failed = result.get("result") in {"FAILED", "DEGRADED", "BLOCKED"} if isinstance(result, dict) else any(x.get("result") in {"FAILED", "DEGRADED", "BLOCKED"} for x in result)
        return 1 if failed else 0
    except Exception as e:
        reason = str(e) if isinstance(e, Refused) else "LOCAL_OPERATION_FAILED"
        print(json.dumps({"instance_id": "T2", "status": "error", "reason": reason}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
