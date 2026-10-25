"""Independent public-source collectors and bounded P3-only writer.

HTTPS uses macOS curl's system trust, never insecure TLS. No M2 credentials,
runtime dependency install, migration, publication or backup runs in a tick.
"""
from __future__ import annotations
import concurrent.futures
import datetime as dt
import hashlib
from html.parser import HTMLParser
import json
import math
import os
from pathlib import Path
import re
import runpy
import subprocess
import tempfile
import time
import urllib.parse

ROOT = Path(__file__).resolve().parents[1]
_DEPLOY = runpy.run_path(str(ROOT/'src/runtime_config.py'))['load'](ROOT)
DOCKER = _DEPLOY['docker']
DB_CONTAINER = _DEPLOY['db_container']
APP_CONTAINER = _DEPLOY['app_container']
DATABASES = {'raw':_DEPLOY['raw_database'], 'intelligence':_DEPLOY['intelligence_database']}
ADMIN = _DEPLOY['admin_role']
ROLE = _DEPLOY['writer_role']
CLUSTER = _DEPLOY['cluster']
UTC = dt.timezone.utc
SOURCES = {"twse_valuation", "twse_prices", "twse_institutional", "fred_macro"}


class IngestionError(Exception):
    pass


def canonical(x):
    return json.dumps(x, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(x):
    return hashlib.sha256(canonical(x)).hexdigest()


def atomic(path, value):
    data = value if isinstance(value, bytes) else canonical(value) + b"\n"
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".pending-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data); f.flush(); os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)


def command(args, data=None, timeout=35):
    try:
        p = subprocess.run(args, input=data, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        raise IngestionError("PROCESS_UNAVAILABLE_OR_TIMEOUT") from None
    if p.returncode:
        raise IngestionError("PROCESS_FAILED")
    return p.stdout


def sql(layer, statements, writer=True):
    if layer not in DATABASES:
        raise IngestionError("DATABASE_NOT_ALLOWED")
    db = DATABASES[layer]
    # Guard inside the transaction and BEFORE any mutation/SET ROLE. Literal
    # identifiers are constants; all source data enter as hex encoded JSON.
    guard = f"""DO $guard$ BEGIN
      IF current_database() <> '{db}' OR (SELECT system_identifier::text FROM pg_control_system()) <> '{CLUSTER}'
      THEN RAISE EXCEPTION 'T2_TARGET_REFUSED'; END IF;
    END $guard$;"""
    script = "BEGIN; SET LOCAL statement_timeout='15000'; SET LOCAL lock_timeout='3000';\n" + guard
    if writer: script += "\nSET LOCAL ROLE " + ROLE + ";"
    script += "\n" + statements + "\nCOMMIT;\n"
    return command([DOCKER, "exec", "-i", "-e", "PGOPTIONS=-c default_transaction_read_only=off", DB_CONTAINER, "psql", "-XqAt", "-v", "ON_ERROR_STOP=1", "-U", ADMIN, "-d", db], script.encode())


def json_sql(value):
    return "convert_from(decode('" + canonical(value).hex() + "','hex'),'UTF8')::jsonb"


def fetch(url, root=ROOT):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in {"openapi.twse.com.tw", "www.twse.com.tw", "fred.stlouisfed.org"} or parsed.username or parsed.fragment:
        raise IngestionError("SOURCE_URL_REFUSED")
    # -q disables ambient curlrc; no redirects; no retry storm; HTTPS only.
    started = time.monotonic()
    body = command(["/usr/bin/curl", "-q", "--fail", "--silent", "--show-error", "--http1.1", "--proto", "=https", "--connect-timeout", "5", "--max-time", "15", "--max-filesize", "4000000", url], timeout=18)
    if not body or len(body) > 4000000:
        raise IngestionError("SOURCE_BODY_INVALID")
    sha = hashlib.sha256(body).hexdigest()
    archive = root / "state/source-responses"
    archive.mkdir(mode=0o700, exist_ok=True)
    dest = archive / (sha + ".body")
    if not dest.exists(): atomic(dest, body)
    return body, {"url": url, "sha256": sha, "bytes": len(body), "latency_ms": round((time.monotonic()-started)*1000, 1)}


def number(value):
    if isinstance(value, bool): raise IngestionError("BOOLEAN_VALUE_REFUSED")
    if value is None or str(value).strip() in {"", "--", "---", "N/A", "."}: return None
    try: n = float(str(value).replace(",", "").strip())
    except ValueError: raise IngestionError("NUMERIC_VALUE_INVALID") from None
    if not math.isfinite(n): raise IngestionError("NONFINITE_VALUE_REFUSED")
    return n


def date(value, today):
    v = str(value).replace("/", "").replace("-", "")
    try:
        if len(v) == 7: v = str(int(v[:3])+1911)+v[3:]
        day = dt.datetime.strptime(v, "%Y%m%d").date()
    except ValueError: raise IngestionError("SOURCE_DATE_INVALID") from None
    if day > today or (today-day).days > 45:
        raise IngestionError("SOURCE_DATE_OUTSIDE_WINDOW")
    return day.isoformat()


def observation(kind, entity, field, value, day, unit, ref):
    return {"entity_type": kind, "entity_id": entity, "signal_type": field,
            "value": value, "source_date": day, "unit": unit, "ref": ref}


def parse_twse(body, source, universe, today):
    rows = json.loads(body)
    if not isinstance(rows, list) or not rows or len(rows) > 20000:
        raise IngestionError("TWSE_SCHEMA_INVALID")
    out = []; found = set()
    fields = [("PEratio", "pe_ratio", "ratio"), ("PBratio", "pb_ratio", "ratio"), ("DividendYield", "dividend_yield", "pct")]
    if source == "twse_prices": fields = [("ClosingPrice", "price", "TWD")]
    for row in rows:
        code = str(row.get("Code", ""))
        if code not in universe: continue
        day = date(row.get("Date"), today)
        if code in found: raise IngestionError("DUPLICATE_SOURCE_ENTITY")
        found.add(code)
        for remote, field, unit in fields:
            if remote not in row: raise IngestionError("TWSE_FIELD_MISSING")
            value = number(row[remote])
            if value is None: continue
            if value < 0 or (field == "price" and value == 0): raise IngestionError("TWSE_VALUE_OUT_OF_RANGE")
            # TWSE publishes dividend yield as percent; the pinned scorer's
            # 0.04 threshold means a unit ratio (4%).
            if field == "dividend_yield": value /= 100; unit = "ratio"
            out.append(observation("company", code, field, value, day, unit, source))
    if len(found) < max(1, int(len(universe)*.9)) or not out:
        raise IngestionError("UNIVERSE_COVERAGE_LOW")
    return out, {"entities_seen": len(found), "entities_expected": len(universe), "missing_entities": sorted(set(universe)-found)}


def parse_flow(body, universe, today, requested):
    result = json.loads(body)
    if result.get("stat") != "OK":
        if result.get("total") == 0 or "沒有符合" in str(result.get("stat")):
            return [], {"no_data": True, "requested_date": requested}
        raise IngestionError("TWSE_FLOW_STATUS_INVALID")
    day = date(result.get("date"), today)
    if day != requested: raise IngestionError("TWSE_FLOW_DATE_MISMATCH")
    fields = result.get("fields", [])
    expected = {"code": "證券代號", "foreign_net": "外陸資買賣超股數(不含外資自營商)", "prop_net": "投信買賣超股數", "dealer_net": "自營商買賣超股數"}
    try: indexes = {k: fields.index(v) for k,v in expected.items()}
    except ValueError: raise IngestionError("TWSE_FLOW_SCHEMA_CHANGED") from None
    if "股" not in str(result.get("hints", "")): raise IngestionError("TWSE_FLOW_UNIT_UNKNOWN")
    out = []; found = set(); sectors = {}
    for row in result.get("data", []):
        code = str(row[indexes["code"]]).strip()
        if code not in universe: continue
        if code in found: raise IngestionError("DUPLICATE_SOURCE_ENTITY")
        found.add(code)
        sector = universe[code]["sector"]
        for field in ["foreign_net", "prop_net", "dealer_net"]:
            value = number(row[indexes[field]])
            if value is None: raise IngestionError("FLOW_VALUE_MISSING")
            # TWSE publishes shares; canonical T86 signal unit is 1000 shares.
            value /= 1000
            out.append(observation("company", code, field, value, day, "zhang", "twse_institutional"))
            sectors[(sector,field)] = sectors.get((sector,field),0)+value
    if len(found) < max(1,int(len(universe)*.9)): raise IngestionError("UNIVERSE_COVERAGE_LOW")
    for (sector,field), value in sorted(sectors.items()):
        out.append(observation("industry", sector, field, value, day, "zhang", "twse_institutional"))
    return out, {"entities_seen":len(found), "rollup":"sum over configured 50 companies by their preserved sector; not whole-market industry flow"}


class Text(HTMLParser):
    def __init__(self): super().__init__(); self.parts=[]
    def handle_data(self, data): self.parts.append(data)


def parse_fred(body, series, today):
    text = Text(); text.feed(body.decode("utf-8"))
    plain = " ".join(text.parts)
    # The publisher's observation table: date followed by colon, then value.
    matches = re.findall(r"(\d{4}-\d{2}-\d{2})\s*:\s*([+-]?\d+(?:\.\d+)?)", plain)
    values = {}
    for d,v in matches:
        d=date(d,today); n=number(v)
        if d in values and values[d] != n: raise IngestionError("FRED_OBSERVATION_CONFLICT")
        values[d]=n
    if series not in plain or not values: raise IngestionError("FRED_SCHEMA_INVALID")
    field,unit = {"DGS10":("us10y","pct"),"DGS2":("us2y","pct"),"VIXCLS":("vix","index")}[series]
    return [observation("macro","global",field,v,d,unit,"https://fred.stlouisfed.org/series/"+series) for d,v in sorted(values.items())]


def collect(source, now, root=ROOT):
    cfg=json.loads((root/"config/ingestion.json").read_text())
    universe={s["code"]:s for s in json.loads((root/"config/universe.json").read_text())}
    today=now.astimezone(dt.timezone(dt.timedelta(hours=8))).date()
    observations=[]; responses=[]; coverage={}
    if source in {"twse_valuation","twse_prices"}:
        path="BWIBBU_ALL" if source=="twse_valuation" else "STOCK_DAY_ALL"
        body,meta=fetch("https://openapi.twse.com.tw/v1/exchangeReport/"+path,root)
        observations,coverage=parse_twse(body,source,universe,today);responses.append(meta)
    elif source=="twse_institutional":
        policy=runpy.run_path(str(ROOT/"src/reliability.py"))
        calendar=policy["load_calendar"](root,today,now)
        if not calendar:raise IngestionError("CALENDAR_UNAVAILABLE_OR_EXPIRED")
        closed=set(calendar["closed_dates"]+calendar.get("extra_closed_dates",[]))
        for offset in range(cfg["rolling_days"]):
            d=today-dt.timedelta(days=offset)
            if d.weekday()>=5 or d.isoformat() in closed: continue
            url="https://www.twse.com.tw/rwd/zh/fund/T86?response=json&date="+d.strftime("%Y%m%d")+"&selectType=ALLBUT0999"
            body,meta=fetch(url,root);rows,c=parse_flow(body,universe,today,d.isoformat())
            responses.append(meta);observations.extend(rows);coverage[d.isoformat()]=c
            time.sleep(.2)
    elif source=="fred_macro":
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            futures={series:pool.submit(fetch,"https://fred.stlouisfed.org/series/"+series,root) for series in ["DGS10","DGS2","VIXCLS"]}
            for series,future in futures.items():
                body,meta=future.result(); observations.extend(parse_fred(body,series,today));responses.append(meta)
        paired={}
        for x in observations:paired.setdefault(x["source_date"],{})[x["signal_type"]]=x["value"]
        for d,v in paired.items():
            if "us10y" in v and "us2y" in v:
                observations.append(observation("macro","global","yield_spread",v["us10y"]-v["us2y"],d,"pp","FRED DGS10 minus DGS2, same source date"))
        coverage={"series":["DGS10","DGS2","VIXCLS"],"derived":"yield_spread = same-date DGS10 - DGS2"}
    else:raise IngestionError("SOURCE_NOT_ALLOWED")
    if not observations: raise IngestionError("NO_OBSERVATIONS_IN_ROLLING_WINDOW")
    observations.sort(key=lambda x:(x["source_date"],x["entity_type"],x["entity_id"],x["signal_type"]))
    batch_id=digest({"source":source,"observations":observations,"version":1})
    dates=sorted({x["source_date"] for x in observations})
    return {"instance_id":"T2","source":source,"batch_id":batch_id,"fetched_at":now.isoformat(),"observations":observations,
            "source_dates":dates,"responses":responses,"coverage":coverage,"universe":universe}


def signals(batch):
    out=[]
    for row in batch["observations"]:
        if row["signal_type"]=="price": continue
        identity={k:row[k] for k in ["entity_type","entity_id","signal_type","source_date"]}
        sid="t2-"+digest({"source":batch["source"],**identity,"value":row["value"],"unit":row["unit"]})[:32]
        metadata={"instance_id":"T2","source_date":row["source_date"],"batch_id":batch["batch_id"],"coverage":batch["coverage"]}
        out.append({"signal_id":sid,**{k:row[k] for k in ["entity_type","entity_id","signal_type","value","unit"]},
                    "direction":"neutral","timestamp":row["source_date"]+"T00:00:00Z","date_bucket":row["source_date"],
                    "source_id":"t2."+batch["source"],"source_type":"t2_"+batch["source"],"ref":row["ref"],
                    "fetched_at":batch["fetched_at"],"metadata":metadata,"metadata_json":json.dumps(metadata,ensure_ascii=False),"raw_payload":json.dumps(row,ensure_ascii=False)})
    return out


def persist(batch, root=ROOT):
    if batch["source"] not in SOURCES or batch.get("instance_id")!="T2" or not re.fullmatch("[a-f0-9]{64}",batch["batch_id"]):
        raise IngestionError("BATCH_IDENTITY_REFUSED")
    batch_id=batch["batch_id"]
    rows=signals(batch)
    prior_times=json.loads(sql("intelligence", "SELECT coalesce(json_object_agg(k.score_key,r.committed_at),'{}'::json) FROM t2_ingestion.score_keys k JOIN t2_ingestion.receipts r USING(batch_id);").decode().splitlines()[-1])
    worker=(root/"src/score_worker.py").read_text()
    payload={**batch,"signals":rows,"prior_score_times":prior_times}
    # Source data are a literal JSON string loaded as data, never evaluated.
    script=worker.split('if __name__ == "__main__":')[0]+"\nprint(json.dumps(score(json.loads("+repr(canonical(payload).decode())+")),ensure_ascii=False,allow_nan=False))\n"
    scored=json.loads(command([DOCKER,"exec","-i",APP_CONTAINER,"python","-B","-"],script.encode(),timeout=45)) if rows else {"scores":[],"nodes":[],"edges":[]}
    scores=scored.get("scores")
    if not isinstance(scores,list):raise IngestionError("SCORING_OUTPUT_INVALID")
    # Raw commits first. A crash before intelligence commit is reconciled by
    # replaying this content hash: raw ON CONFLICT; intel durable receipt.
    raw_sql=f"""SELECT pg_advisory_xact_lock(205101,1);
      INSERT INTO t2_ingestion.batches(batch_id,source,fetched_at,source_dates,responses,coverage,observations)
      SELECT b->>'batch_id',b->>'source',b->>'fetched_at',b->'source_dates',b->'responses',b->'coverage',b->'observations' FROM (SELECT {json_sql(batch)} b) q
      ON CONFLICT(batch_id) DO NOTHING;
      SELECT json_build_object('batch_id','{batch_id}','raw_present',true);"""
    sql("raw",raw_sql)
    row_type="signal_id text,entity_type text,entity_id text,signal_type text,value float8,unit text,direction text,timestamp text,date_bucket text,source_id text,source_type text,ref text,fetched_at text,metadata_json text,raw_payload text"
    score_type="scorer text,entity_type text,entity_id text,score float8,breakdown_json text,inputs_json text,notes text,computed_at text,score_key text"
    intel_sql=f"""SELECT pg_advisory_xact_lock(205101,1);
      WITH input AS (SELECT * FROM jsonb_to_recordset({json_sql(rows)}) AS x({row_type})), new_signals AS (
      INSERT INTO public.signal_log(signal_id,entity_type,entity_id,signal_type,value,unit,direction,timestamp,date_bucket,source_id,source_type,ref,fetched_at,metadata_json,raw_payload,ingested_at)
      SELECT *,{json_sql(batch['fetched_at'])}#>>'{{}}' FROM input
      WHERE NOT EXISTS(SELECT 1 FROM t2_ingestion.receipts WHERE batch_id='{batch_id}')
      ON CONFLICT(signal_id) DO NOTHING RETURNING signal_id),
      score_input AS (SELECT * FROM jsonb_to_recordset({json_sql(scores)}) AS x({score_type})), new_scores AS (
      INSERT INTO public.score_snapshot(scorer,entity_type,entity_id,score,breakdown_json,inputs_json,notes,computed_at)
      SELECT scorer,entity_type,entity_id,score,breakdown_json,inputs_json,notes,computed_at FROM score_input
      WHERE NOT EXISTS(SELECT 1 FROM t2_ingestion.score_keys k WHERE k.score_key=score_input.score_key) RETURNING snapshot_id), new_keys AS (
      INSERT INTO t2_ingestion.score_keys(score_key,batch_id)
      SELECT x.score_key,'{batch_id}' FROM jsonb_to_recordset({json_sql(scores)}) AS x(score_key text)
      ON CONFLICT(score_key) DO NOTHING RETURNING score_key)
      INSERT INTO t2_ingestion.receipts(batch_id,source,committed_at,signals,scores,source_dates)
      SELECT '{batch_id}',{json_sql(batch['source'])}#>>'{{}}',{json_sql(batch['fetched_at'])}#>>'{{}}',(SELECT count(*) FROM new_signals),(SELECT count(*) FROM new_scores),{json_sql(batch['source_dates'])}
      ON CONFLICT(batch_id) DO NOTHING;
      INSERT INTO public.graph_nodes(node_id,node_type,label,created_at,metadata_json,tags_json,schema_version)
      SELECT node_id,node_type,label,created_at,metadata_json,tags_json,'3.0' FROM jsonb_to_recordset({json_sql(scored['nodes'])}) AS x(node_id text,node_type text,label text,created_at text,metadata_json text,tags_json text)
      ON CONFLICT(node_id) DO NOTHING;
      INSERT INTO public.graph_edges(edge_id,edge_type,from_node_id,to_node_id,weight,metadata_json,created_at,schema_version)
      SELECT edge_id,edge_type,from_node_id,to_node_id,NULL,metadata_json,created_at,'3.0' FROM jsonb_to_recordset({json_sql(scored['edges'])}) AS x(edge_id text,edge_type text,from_node_id text,to_node_id text,metadata_json text,created_at text)
      ON CONFLICT DO NOTHING;
      SELECT json_build_object('batch_id',batch_id,'signals',signals,'scores',scores,'committed_at',committed_at) FROM t2_ingestion.receipts WHERE batch_id='{batch_id}';"""
    committed=json.loads(sql("intelligence",intel_sql).decode().splitlines()[-1])
    return {**committed,"source":batch["source"],"source_dates":batch["source_dates"],"responses":batch["responses"],"coverage":batch["coverage"],"observations":len(batch["observations"])}


def run(source, now, root=ROOT):
    expected=json.loads((root/"config/ingestion.json").read_text())["container_identities"]
    for container in [DB_CONTAINER,APP_CONTAINER]:
        actual=command([DOCKER,"inspect","--format","{{.Id}} {{.Image}}",container]).decode().strip().split()
        if actual != [expected[container]["id"],expected[container]["image"]]:
            raise IngestionError("CONTAINER_IDENTITY_REFUSED")
    batch = collect(source,now,root)
    result = persist(batch,root)
    latest = max(batch["source_dates"])
    latest_per_key = {}
    for row in batch["observations"]:
        key=(row["entity_type"],row["entity_id"],row["signal_type"])
        if key not in latest_per_key or row["source_date"]>latest_per_key[key]["source_date"]:latest_per_key[key]=row
    atomic(root/"state"/("market-"+source+".json"), {"instance_id":"T2","source":source,"fetched_at":now.isoformat(),
           "source_date":latest,"batch_id":batch["batch_id"],"coverage":batch["coverage"],
           "observations":list(latest_per_key.values())})
    return result
