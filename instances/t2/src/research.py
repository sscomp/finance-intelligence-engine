"""T2 source contracts v1. Bounded public reads, dedicated append-only history.
No M2 state, notification, migration ledger, current-quote fetch or daily collector edits.
"""
from __future__ import annotations
import datetime as dt
from email.utils import parsedate_to_datetime
import hashlib
import html
import json
from pathlib import Path
import re
import runpy
import time
import urllib.parse
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parents[1]
TZ=ZoneInfo('Asia/Taipei');UTC=dt.timezone.utc
VERSION='T2_RESEARCH_V1'
SOURCES={'industry_rss','company_fundamentals'}

class SourceError(Exception):pass

def ingestion():return runpy.run_path(str(ROOT/'src/ingestion.py'))
def policy():return runpy.run_path(str(ROOT/'src/reliability.py'))
def canonical(x):return json.dumps(x,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
def digest(x):return hashlib.sha256(canonical(x)).hexdigest()
def atomic(p,x):return ingestion()['atomic'](p,x)
def config(root):
    p=root/'config/research.json'
    if p.is_symlink() or p.stat().st_mode&0o077:raise SourceError('SOURCE_CONFIG_PRIVATE_REQUIRED')
    c=json.loads(p.read_text())
    if c.get('instance_id')!='T2' or c.get('provider_version')!=VERSION:raise SourceError('SOURCE_CONFIG_IDENTITY')
    return c

def fetch(url,root):
    c=config(root);allowed={f['url'] for f in c['rss_feeds']}|set(c['company_endpoints'].values())
    if url not in allowed:raise SourceError('SOURCE_URL_REFUSED')
    m=ingestion();body=m['command'](['/usr/bin/curl','-q','--fail','--silent','--show-error','--http1.1','--proto','=https','--connect-timeout','5','--max-time','15','--max-filesize','4000000',url],timeout=18)
    if not body or len(body)>4000000:raise SourceError('SOURCE_BODY_INVALID')
    sha=hashlib.sha256(body).hexdigest();p=root/'state/research-responses';p.mkdir(mode=0o700,exist_ok=True)
    dest=p/(sha+'.body')
    if not dest.exists():m['atomic'](dest,body)
    return body,{'url':url,'sha256':sha,'bytes':len(body),'fetched_at':dt.datetime.now(UTC).isoformat()}

def article_url(value):
    p=urllib.parse.urlsplit(html.unescape(value.strip()))
    if p.scheme not in {'http','https'} or p.hostname not in {'www.digitimes.com.tw','digitimes.com.tw','www.wealth.com.tw','wealth.com.tw'} or p.username or p.password or p.port not in {None,80,443}:raise SourceError('ARTICLE_URL_REFUSED')
    query=[(k,v) for k,v in urllib.parse.parse_qsl(p.query,keep_blank_values=True) if not k.lower().startswith('utm_') and k.lower() not in {'fbclid','gclid'}]
    return urllib.parse.urlunsplit(('https','www.'+p.hostname.removeprefix('www.'),p.path or '/',urllib.parse.urlencode(sorted(query)),''))

def matched(text,keywords):
    return [k for k in keywords if (re.search(r'(?<![A-Za-z0-9])'+re.escape(k)+r'(?![A-Za-z0-9])',text,re.I) if k.isascii() else k in text)]

def parse_rss(body,feed,now,c):
    if b'<!DOCTYPE' in body.upper() or b'<!ENTITY' in body.upper():raise SourceError('RSS_ENTITY_REFUSED')
    tree=ET.fromstring(body);items=tree.findall('.//item')
    if tree.tag!='rss' or not items or len(items)>250:raise SourceError('RSS_SCHEMA_INVALID')
    rows=[];rejected={};dates=[]
    for item in items:
        try:
            title=html.unescape(item.findtext('title','')).strip()
            if not title or len(title)>1000:raise SourceError('TITLE_INVALID')
            link=article_url(item.findtext('link',''))
            stamp=parsedate_to_datetime(item.findtext('pubDate',''))
            if not stamp.tzinfo:raise SourceError('PUBLISH_TIMEZONE_MISSING')
            if stamp>now:raise SourceError('FUTURE_PUBLISH_DATE')
            dates.append(stamp.isoformat())
            if (now-stamp).total_seconds()>c['rss_history_days']*86400:raise SourceError('OUTSIDE_HISTORY_WINDOW')
            topics={k:matched(title,v) for k,v in c['industry_keywords'].items()};topics={k:v for k,v in topics.items() if v}
            row={'key':digest(link),'entity_type':'article','entity_id':link,'canonical_url':link,'title':title,
                 'published_at':stamp.isoformat(),'source_as_of':stamp.isoformat(),'period':stamp.astimezone(TZ).date().isoformat(),
                 'industries':topics,'feed_urls':[feed['url']],'provider':'DIGITIMES','provider_version':VERSION,'upstream_version':'RSS 2.0',
                 'release_semantics':'RSS pubDate supplied by publisher; not fetch date; no full article copied'}
            rows.append(row)
        except (ValueError,TypeError,SourceError) as exc:
            key=str(exc) if isinstance(exc,SourceError) else 'PUBLISH_DATE_INVALID';rejected[key]=rejected.get(key,0)+1
    newest=max((dt.datetime.fromisoformat(x) for x in dates),default=None)
    healthy=bool(rows) and newest is not None and 0<=(now-newest).total_seconds()<=7*86400
    return rows,{'feed_url':feed['url'],'provider_feed_title':tree.findtext('channel/title'),'items':len(items),'accepted':len(rows),'rejected':rejected,'newest_published_at':newest.isoformat() if newest else None,'state':'PASS' if healthy else 'NO_DATA'}

def roc_date(value,now):
    v=str(value)
    if not re.fullmatch(r'\d{7}',v):raise SourceError('OUTPUT_DATE_INVALID')
    d=dt.date(int(v[:3])+1911,int(v[3:5]),int(v[5:]))
    if d>now.astimezone(TZ).date():raise SourceError('FUTURE_OUTPUT_DATE')
    return d.isoformat()

def number(x):
    try:return ingestion()['number'](x)
    except Exception:raise SourceError('NUMERIC_FIELD_INVALID') from None

def parse_company(body,kind,url,universe,now):
    data=json.loads(body)
    if not isinstance(data,list) or not data or len(data)>20000:raise SourceError('COMPANY_SCHEMA_INVALID')
    rows=[];seen=set()
    for item in data:
        code=str(item.get('公司代號',''))
        if code not in universe:continue
        if code in seen:raise SourceError('DUPLICATE_COMPANY_PERIOD')
        seen.add(code);name=str(item.get('公司名稱','')).strip();sheet=roc_date(item.get('出表日期'),now)
        if not name or len(name)>100:raise SourceError('COMPANY_NAME_INVALID')
        if kind=='revenue':
            p=str(item.get('資料年月',''))
            if not re.fullmatch(r'\d{5}',p):raise SourceError('REVENUE_PERIOD_INVALID')
            year,month=int(p[:3])+1911,int(p[3:]);period=dt.date(year,month,1).strftime('%Y-%m')
            # Completed month AND a provider output date later than that period.
            if period>=now.astimezone(TZ).strftime('%Y-%m') or period>=sheet[:7]:raise SourceError('UNRELEASED_REVENUE_PERIOD')
            fields={'revenue':number(item['營業收入-當月營收']),'prior_month_revenue':number(item['營業收入-上月營收']),
                    'prior_year_revenue':number(item['營業收入-去年當月營收']),'yoy_pct':number(item['營業收入-去年同月增減(%)']),
                    'mom_pct':number(item['營業收入-上月比較增減(%)']),'ytd_revenue':number(item['累計營業收入-當月累計營收'])}
            if fields['revenue'] is None or fields['revenue']<0:raise SourceError('REVENUE_VALUE_INVALID')
            sector=item.get('產業別');units='TWD thousand; growth percent (not ratio)'
            if not sector:raise SourceError('OFFICIAL_SECTOR_MISSING')
            # Check the published YoY against its same-period numerator/denominator.
            base=fields['prior_year_revenue'];growth=fields['yoy_pct']
            if base and growth is not None and abs((fields['revenue']/base-1)*100-growth)>.15:raise SourceError('REVENUE_GROWTH_INCONSISTENT')
            semantics='Monthly operating revenue, not monthly earnings; output date is not issuer filing timestamp'
        else:
            year=int(item['年度'])+1911;q=int(item['季別'])
            if not 1<=q<=4:raise SourceError('FINANCIAL_QUARTER_INVALID')
            period=f'{year}-Q{q}'
            end=dt.date(year,q*3,1).replace(day=28)+dt.timedelta(days=4);end=end.replace(day=1)-dt.timedelta(days=1)
            if end>=dt.date.fromisoformat(sheet) or end>=now.astimezone(TZ).date():raise SourceError('UNRELEASED_FINANCIAL_PERIOD')
            fields={'basic_eps_reported':number(item['基本每股盈餘（元）']),'net_income_reported':number(item.get('本期淨利（淨損）',item.get('本期稅後淨利（淨損）')))}
            if fields['basic_eps_reported'] is None:raise SourceError('FINANCIAL_EPS_MISSING')
            sector=None;units='EPS TWD/share; net income provider monetary units (not converted)'
            semantics='Provider annual/quarter label; single-quarter vs YTD basis not certified; no annualization, TTM, margin or target-price derivation'
        rows.append({'key':code+':'+kind+':'+period,'entity_type':'company','entity_id':code,'name':name,'sector':sector,
                     'period':period,'kind':kind,'fields':fields,'units':units,'provider':'TWSE OpenAPI','provider_version':VERSION,
                     'upstream_version':'TWSE OpenAPI 1.0; swagger SHA256 06e1cea82448361e733a0ad1ae16e52f5d5d6b71905acd078472f852c32c0eb0','source_url':url,'source_as_of':sheet,'provider_output_date':sheet,'issuer_released_at':None,'release_semantics':semantics})
    return rows

def collect(source,now,root=ROOT):
    c=config(root);responses=[];rows=[];coverage={};failures=[]
    if source=='industry_rss':
        feeds=[]
        for feed in c['rss_feeds']:
            try:
                body,meta=fetch(feed['url'],root);responses.append(meta);items,health=parse_rss(body,feed,now,c);rows+=items;feeds.append(health)
                if health['state']!='PASS':failures.append(feed['url']+':NO_DATED_RECENT_ITEMS')
            except Exception:feeds.append({'feed_url':feed['url'],'state':'FAILED'});failures.append(feed['url']+':SOURCE_READ_OR_PARSE_FAILED')
            time.sleep(.2)
        unique={}
        for row in rows:
            key=row['key']
            if key in unique:
                prior=unique[key]
                if any(prior[x]!=row[x] for x in ['title','published_at']):raise SourceError('SAME_ARTICLE_FEED_CONFLICT')
                prior['feed_urls']=sorted(set(prior['feed_urls']+row['feed_urls']))
            else:unique[key]=row
        rows=list(unique.values());coverage={'feeds':feeds,'scope':c['rss_scope'],'expected_industries':list(c['industry_keywords'])}
    elif source=='company_fundamentals':
        u={s['code']:s for s in json.loads((root/'config/universe.json').read_text())}
        for kind,url in c['company_endpoints'].items():
            try:
                body,meta=fetch(url,root);responses.append(meta);rows+=parse_company(body,kind,url,u,now)
            except Exception:failures.append(kind+':SOURCE_READ_OR_PARSE_FAILED')
            time.sleep(.2)
        revenue={x['entity_id']:x for x in rows if x['kind']=='revenue'}
        for x in rows:
            if x['kind']!='revenue':x['sector']=revenue.get(x['entity_id'],{}).get('sector')
        coverage={'expected_companies':len(u),'revenue_companies':len(revenue),'income_companies':len({x['entity_id'] for x in rows if x['kind']!='revenue'}),
                  'missing_revenue':sorted(set(u)-set(revenue)),'missing_income':sorted(set(u)-{x['entity_id'] for x in rows if x['kind']!='revenue'}),
                  'identity_policy':'TWSE company code and publisher company name; official sector from same-code monthly revenue row; existing universe unchanged'}
    else:raise SourceError('SOURCE_NOT_ALLOWED')
    if not rows:raise SourceError('NO_VERIFIED_SOURCE_ROWS')
    rows.sort(key=lambda x:x['key']);keys=[x['key'] for x in rows]
    if len(keys)!=len(set(keys)):raise SourceError('DUPLICATE_SOURCE_KEY')
    for row in rows:row['version']=digest(row)
    batch={'instance_id':'T2','source':source,'provider_version':VERSION,'fetched_at':now.isoformat(),'responses':responses,'rows':rows,'coverage':coverage,'failures':failures}
    # Body hashes belong to raw receipts; observation identity excludes mutable fetch/output date metadata only where explicit.
    batch['batch_id']=digest({'source':source,'provider_version':VERSION,'rows':rows,'failures':failures})
    return batch

def verify_target(root):
    m=ingestion();expected=json.loads((root/'config/ingestion.json').read_text())['container_identities']
    for container in [m['DB_CONTAINER'],m['APP_CONTAINER']]:
        actual=m['command']([m['DOCKER'],'inspect','--format','{{.Id}} {{.Image}}',container]).decode().split()
        if actual!=[expected[container]['id'],expected[container]['image']]:raise SourceError('CONTAINER_IDENTITY_REFUSED')

def persist(batch,root=ROOT):
    if batch.get('instance_id')!='T2' or batch['source'] not in SOURCES or batch.get('provider_version')!=VERSION:raise SourceError('BATCH_IDENTITY_REFUSED')
    for row in batch['rows']:
        if row['version']!=digest({k:v for k,v in row.items() if k!='version'}):raise SourceError('ROW_DIGEST_MISMATCH')
    if batch['batch_id']!=digest({k:batch[k] for k in ['source','provider_version','rows','failures']}):raise SourceError('BATCH_DIGEST_MISMATCH')
    verify_target(root);m=ingestion();sql=m['sql'];j=m['json_sql'];bid=batch['batch_id'];b=j(batch)
    sql('raw',f"SELECT pg_advisory_xact_lock(205102,1); INSERT INTO t2_research.batches SELECT b->>'batch_id',b->>'source',b FROM (SELECT {b} b) q ON CONFLICT(batch_id) DO NOTHING;")
    # Raw-first crash recovery: rerun inserts missing intelligence versions, then commits receipt.
    stmt=f"""SELECT pg_advisory_xact_lock(205102,1);
    WITH input AS (SELECT value r FROM jsonb_array_elements({b}->'rows')), inserted AS (
      INSERT INTO t2_research.versions(source,record_key,version,payload,first_batch,first_seen)
      SELECT {b}->>'source',r->>'key',r->>'version',r,'{bid}',({b}->>'fetched_at')::timestamptz FROM input
      ON CONFLICT(source,record_key,version) DO NOTHING RETURNING 1)
    SELECT count(*) FROM inserted;
    INSERT INTO t2_research.heads(source,record_key,version,observed_at)
      SELECT {b}->>'source',r->>'key',r->>'version',({b}->>'fetched_at')::timestamptz FROM jsonb_array_elements({b}->'rows') r
      ON CONFLICT(source,record_key) DO UPDATE SET version=EXCLUDED.version,observed_at=EXCLUDED.observed_at
      WHERE t2_research.heads.observed_at<=EXCLUDED.observed_at;
    INSERT INTO t2_research.receipts SELECT '{bid}',{b}->>'source',({b}->>'fetched_at')::timestamptz,jsonb_array_length({b}->'rows'),{b}->'coverage',{b}->'failures'
      ON CONFLICT(batch_id) DO NOTHING;
    SELECT json_build_object('batch_id',batch_id,'rows',rows,'committed_at',committed_at) FROM t2_research.receipts WHERE batch_id='{bid}';"""
    result=json.loads(sql('intelligence',stmt).decode().splitlines()[-1]);return result

def run(source,now,root=ROOT):
    if source not in SOURCES:raise SourceError('SOURCE_NOT_ALLOWED')
    p=root/'state'/('research-'+source+'.json');prior=json.loads(p.read_text()) if p.exists() else {}
    try:
        batch=collect(source,now,root);result=persist(batch,root)
        summary={'instance_id':'T2','source':source,'result':'PASS' if not batch['failures'] else 'DEGRADED','last_attempt':now.isoformat(),'last_success':now.isoformat(),
                 'provider_version':VERSION,**result,'coverage':batch['coverage'],'failures':batch['failures'],'responses':batch['responses'],
                 'source_as_of':sorted({x['source_as_of'] for x in batch['rows']}),'rowset_sha256':digest(batch['rows'])}
        atomic(p,summary);return summary
    except Exception as exc:
        reason=str(exc) if isinstance(exc,SourceError) else 'SOURCE_COLLECTION_OR_PERSISTENCE_FAILED'
        atomic(p,{**prior,'result':'FAILED','last_attempt':now.isoformat(),'error':reason});raise

def read_rows(source,root=ROOT):
    if source not in SOURCES:raise SourceError('SOURCE_NOT_ALLOWED')
    verify_target(root);m=ingestion()
    data=json.loads(m['sql']('intelligence',f"SELECT coalesce(json_agg(json_build_object('row',v.payload,'first_seen',v.first_seen,'first_batch',v.first_batch,'observed_at',h.observed_at) ORDER BY v.record_key),'[]'::json) FROM t2_research.heads h JOIN t2_research.versions v USING(source,record_key,version) WHERE h.source={m['json_sql'](source)}#>>'{{}}';").decode().splitlines()[-1])
    for x in data:
        r=x['row']
        if r.get('version')!=digest({k:v for k,v in r.items() if k!='version'}):raise SourceError('PERSISTED_ROW_DIGEST_MISMATCH')
    return data

def source_state(root,source,now):
    try:
        x=json.loads((root/'state'/('research-'+source+'.json')).read_text());age=(now-dt.datetime.fromisoformat(x['last_success'])).total_seconds()
        limit=30*3600 if source=='company_fundamentals' else 7*3600
        if x.get('result')!='PASS' or not 0<=age<limit:return {**x,'usable':False,'reason':'SOURCE_FAILED_DEGRADED_OR_CHECKPOINT_EXPIRED'}
        m=ingestion();receipt=json.loads(m['sql']('intelligence',f"SELECT coalesce(json_agg(r),'[]'::json) FROM t2_research.receipts r WHERE batch_id={m['json_sql'](x['batch_id'])}#>>'{{}}';").decode().splitlines()[-1])
        if len(receipt)!=1 or receipt[0]['source']!=source or receipt[0]['rows']!=x['rows'] or receipt[0]['failures']!=[]:raise SourceError('SOURCE_RECEIPT_MISMATCH')
        # Raw lineage is verified independently of intelligence and local checkpoints.
        raw=json.loads(m['sql']('raw',f"SELECT coalesce(json_agg(payload),'[]'::json) FROM t2_research.batches WHERE batch_id={m['json_sql'](x['batch_id'])}#>>'{{}}';").decode().splitlines()[-1])
        if len(raw)!=1 or digest(raw[0]['rows'])!=x['rowset_sha256'] or raw[0]['source']!=source:raise SourceError('RAW_LINEAGE_MISMATCH')
        for meta in raw[0]['responses']:
            p=root/'state/research-responses'/(meta['sha256']+'.body')
            if hashlib.sha256(p.read_bytes()).hexdigest()!=meta['sha256']:raise SourceError('SOURCE_BODY_HASH_MISMATCH')
        return {**x,'usable':True,'age_seconds':age}
    except Exception:return {'source':source,'usable':False,'reason':'SOURCE_LINEAGE_UNAVAILABLE_OR_INVALID'}

def monthly_target(now):
    d=now.astimezone(TZ).date().replace(day=1)-dt.timedelta(days=1)
    return d.strftime('%Y-%m')

def make_report(job,now,root=ROOT):
    if job not in {'industry_weekly','company_monthly'}:raise SourceError('REPORT_NOT_ALLOWED')
    source='industry_rss' if job=='industry_weekly' else 'company_fundamentals'
    dep=source_state(root,source,now);rows=[];reason=None
    if not policy()['load_calendar'](root,now.astimezone(TZ).date(),now):reason='CALENDAR_UNAVAILABLE_OR_EXPIRED'
    elif not dep['usable']:reason=dep['reason']
    else:rows=read_rows(source,root)
    report={'instance_id':'T2','schema_version':1,'provider_version':VERSION,'report_type':job,'generated_at':now.isoformat(),
            'delivery':'local-only','adapter_version':VERSION,'build_sha256':digest({p:hashlib.sha256((root/p).read_bytes()).hexdigest() for p in ['src/research.py','src/reliability.py','config/research.json']}),'dependency':dep,'quality':'NO_DATA','rows':[],'source_as_of':[],'scoring_policy':'T2_DESCRIPTIVE_V1; counts and observed YoY only; no investment recommendation'}
    if reason:report.update(quality='DEGRADED',reason=reason)
    elif job=='industry_weekly':
        cutoff=now-dt.timedelta(days=7)
        selected=[x for x in rows if cutoff<=dt.datetime.fromisoformat(x['row']['published_at'])<=now and x['row']['industries']]
        report['rows']=selected;report['source_as_of']=sorted({x['row']['published_at'] for x in selected})
        report['window']={'start_inclusive':cutoff.isoformat(),'end_inclusive':now.isoformat(),'basis':'article pubDate, never fetch date','bootstrap':'rolling 7 days; earlier RSS items retained, not a complete archive'}
        report['industry_scores']={k:{'article_count':sum(k in x['row']['industries'] for x in selected),'state':'OBSERVED' if any(k in x['row']['industries'] for x in selected) else 'NO_MATCHES_IN_SELECTED_FEEDS','meaning':'unique dated headline count; no sentiment or whole-sector return inference'} for k in config(root)['industry_keywords']}
        report['scope']=config(root)['rss_scope'];report['quality']='VERIFIED_BOUNDED' if selected else 'NO_DATA'
    else:
        target=monthly_target(now);universe={x['code'] for x in json.loads((root/'config/universe.json').read_text())}
        monthly={x['row']['entity_id']:x for x in rows if x['row']['kind']=='revenue' and x['row']['period']==target}
        financial={}
        for x in rows:
            r=x['row']
            if r['kind']=='revenue':continue
            code=r['entity_id']
            if code not in financial or r['period']>financial[code]['row']['period']:financial[code]=x
        # Quarterly inputs are deliberately historical, never relabeled as monthly earnings.
        report.update(target_revenue_period=target,revenue_period_status='RELEASED' if monthly else 'NOT_RELEASED',missing_companies=sorted(universe-set(monthly)),
                      missing_financials=sorted(universe-set(financial)),missing_modules=['TTM/forward EPS','ROE/ROA','certified single-quarter versus YTD basis','analyst targets','full M2 research parity'],
                      financial_period_semantics='Official provider year/quarter label only; output date does not establish issuer release timestamp; no monthly earnings claim')
        for code in sorted(monthly):
            x=monthly[code];f=financial.get(code)
            report['rows'].append({'company_id':code,'name':x['row']['name'],'official_sector':x['row']['sector'],'revenue':x,'reported_financials':f,
                                  'descriptive_score':{'observed_revenue_yoy_pct':x['row']['fields']['yoy_pct'],'rating':None}})
        report['source_as_of']=sorted({x['row']['source_as_of'] for x in monthly.values()}|{x['row']['source_as_of'] for x in financial.values()})
        report['quality']='NOT_RELEASED' if not monthly else 'DEGRADED' if report['missing_companies'] or report['missing_financials'] else 'VERIFIED_BOUNDED'
    report['row_count']=len(report['rows']);report['lineage_sha256']=digest(report['rows'])
    return report

def write_report(job,now,root=ROOT):
    report=make_report(job,now,root);rid=digest(report);report['report_id']=rid
    base=root/'reports'/('t2-'+job+'-'+rid[:20]);paths=[base.with_suffix('.json'),base.with_suffix('.md')]
    atomic(paths[0],report)
    # Actual readable source-linked report, not a plan or generic status template.
    lines=[f"# T2 {job} — {report['quality']}",f"Generated: {now.isoformat()}",f"Provider/build: {VERSION}",f"Source as of: {', '.join(report['source_as_of']) or 'unavailable'}",'Local only. Descriptive evidence; no trading rating.','']
    if job=='industry_weekly':
        lines += ['Rolling 7-day article publish-time window; fetch time does not establish publication.']
        for sector,s in report.get('industry_scores',{}).items():lines.append(f"- {sector}: {s['article_count']} unique headlines")
        for x in report['rows']:
            r=x['row'];lines.append(f"- {r['published_at']} [{r['title']}]({r['canonical_url']}) — {', '.join(r['industries'])}; version {r['version']}")
    else:
        lines += [f"Revenue period: {report.get('target_revenue_period','unavailable')} (operating revenue, not earnings).",report.get('financial_period_semantics',''),'', '| Company | Sector | Monthly revenue (TWD thousand) | YoY % | Reported financial period / EPS (TWD/share) |','|---|---|---:|---:|---|']
        for x in report['rows']:
            r=x['revenue']['row'];f=x['reported_financials'];financial=(f['row']['period']+' / '+str(f['row']['fields']['basic_eps_reported'])) if f else 'NO_DATA'
            lines.append(f"| {x['company_id']} {x['name']} | {x['official_sector']} | {r['fields']['revenue']} | {r['fields']['yoy_pct']} | {financial} |")
        lines+=['','Missing modules: '+', '.join(report.get('missing_modules',[]))]
        lines+=['Source URLs: '+', '.join(sorted({x['revenue']['row']['source_url'] for x in report['rows']}))]
    if report.get('reason'):lines+=['Reason: '+report['reason']]
    atomic(paths[1],('\n'.join(lines)+'\n').encode())
    m=ingestion();m['sql']('intelligence',f"INSERT INTO t2_research.reports(report_id,job,generated_at,payload) VALUES({m['json_sql'](rid)}#>>'{{}}',{m['json_sql'](job)}#>>'{{}}',{m['json_sql'](now.isoformat())}#>>'{{}}',{m['json_sql'](report)}) ON CONFLICT(report_id) DO NOTHING;")
    artifacts=[{'path':str(p.relative_to(root)),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in paths]
    pointer={'instance_id':'T2','report_id':rid,'job':job,'generated_at':now.isoformat(),'quality':report['quality'],'artifacts':artifacts}
    atomic(root/'state'/('research-report-'+job+'.json'),pointer)
    return {'result':'PASS' if report['quality']=='VERIFIED_BOUNDED' else report['quality'],'quality':report['quality'],'report_id':rid,'artifacts':artifacts,'source_as_of':report['source_as_of'],'row_count':report['row_count']}

def read_report(job,root=ROOT,now=None):
    now=now or dt.datetime.now(UTC)
    if job not in {'industry_weekly','company_monthly'}:raise SourceError('REPORT_NOT_ALLOWED')
    pointer=json.loads((root/'state'/('research-report-'+job+'.json')).read_text())
    if not re.fullmatch('[a-f0-9]{64}',pointer['report_id']):raise SourceError('REPORT_IDENTITY_INVALID')
    p=root/'reports'/('t2-'+job+'-'+pointer['report_id'][:20]+'.json')
    expected=next(x['sha256'] for x in pointer['artifacts'] if x['path']==str(p.relative_to(root)))
    if hashlib.sha256(p.read_bytes()).hexdigest()!=expected:raise SourceError('REPORT_HASH_MISMATCH')
    r=json.loads(p.read_text());r['read_at']=now.isoformat();r['read_source_state']=source_state(root,'industry_rss' if job=='industry_weekly' else 'company_fundamentals',now)
    r['report_age_seconds']=(now-dt.datetime.fromisoformat(r['generated_at'])).total_seconds()
    limit=8*86400 if job=='industry_weekly' else 32*86400
    r['report_observation']='CURRENT' if 0<=r['report_age_seconds']<limit else 'STALE_OR_INVALID'
    if not r['read_source_state']['usable'] or r['report_observation']!='CURRENT':r['quality']='DEGRADED'
    return r

def quote_gate(quotes,now,root=ROOT,dry_run=False):
    """Strict offline realtime-quote contract. The separate yfinance daily adapter
    never satisfies this realtime gate; use its explicitly LIMITED report policy.
    """
    cal=policy()['calendar_state'](root,now.astimezone(TZ).date(),now)
    if cal=='CLOSED':return {'status':'MARKET_CLOSED','live_report_allowed':False,'dry_run':dry_run}
    if cal!='SCHEDULED_SESSION':return {'status':'DEGRADED','reason':cal,'live_report_allowed':False,'dry_run':dry_run}
    errors=[];local=now.astimezone(TZ)
    if not 9<=local.hour<14:errors.append('OUTSIDE_SESSION_WINDOW')
    if not quotes:errors.append('NO_CURRENT_SESSION_QUOTES')
    seen=set()
    for q in quotes:
        try:
            stamp=dt.datetime.fromisoformat(q['source_as_of'])
            if not stamp.tzinfo or stamp.astimezone(TZ).date()!=local.date() or not 0<=(now-stamp).total_seconds()<=120:raise ValueError()
            if q['company_id'] not in {x['code'] for x in json.loads((root/'config/universe.json').read_text())} or q['company_id'] in seen:raise ValueError()
            if number(q['price']) is None or number(q['price'])<=0 or number(q['volume']) is None or number(q['volume'])<0:raise ValueError()
            if q.get('volume_unit') not in {'shares','lots_1000'} or q.get('quote_kind')!='current_session_trade':raise ValueError()
            seen.add(q['company_id'])
        except Exception:errors.append('QUOTE_TIMESTAMP_IDENTITY_PRICE_VOLUME_INVALID')
    if seen!={x['code'] for x in json.loads((root/'config/universe.json').read_text())}:errors.append('QUOTE_UNIVERSE_COVERAGE_INCOMPLETE')
    if not dry_run:errors.append('DAILY_BAR_ADAPTER_CANNOT_AUTHORIZE_REALTIME_QUOTES')
    return {'status':'DEGRADED' if errors else 'DRY_RUN_PASS','reasons':errors,'live_report_allowed':False,'dry_run':dry_run,'provider_fetch_enabled':False}

def read_status(root=ROOT,now=None):
    now=now or dt.datetime.now(UTC)
    c=config(root);enabled=json.loads((root/'config/instance.json').read_text())['enabled_jobs']
    return {'instance_id':'T2','provider_version':VERSION,'read_at':now.isoformat(),
            'sources':{s:source_state(root,s,now) for s in sorted(SOURCES)},
            'reports':{j:{'schedule_enabled':j in enabled,'checkpoint':json.loads((root/'state'/('research-report-'+j+'.json')).read_text()) if (root/'state'/('research-report-'+j+'.json')).exists() else {'quality':'NO_DATA'}} for j in ['industry_weekly','company_monthly']},
            'intraday':{'collector_enabled':'yfinance' in enabled,'opening_enabled':'opening' in enabled,'preclose_enabled':'tail' in enabled,'safe_status':quote_gate([],now,root),'source_checkpoint':json.loads((root/'state/research-yfinance_daily.json').read_text()) if (root/'state/research-yfinance_daily.json').exists() else {'result':'NEVER_RUN'},'blocker':'YFINANCE_DAILY_ONLY; ACTUAL_MARKET_DAY_ACCEPTANCE_PENDING'},
            'disabled_sources':c['disabled_sources'],'delivery':'local-only'}
