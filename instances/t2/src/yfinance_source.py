"""T2 personal research: M2 daily-bar mechanism, never a realtime quote feed.
Network runs in a killable subprocess; no M2 credentials, caches or state.
"""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import runpy
import subprocess
import sys
from zoneinfo import ZoneInfo
ROOT=Path(__file__).resolve().parents[1]
TZ=ZoneInfo('Asia/Taipei'); UTC=dt.timezone.utc
SOURCE='yfinance_daily'; VERSION='T2_RESEARCH_V1'
SYMBOLS=['0050.TW','^TWII','2344.TW','2382.TW','2408.TW','3231.TW','2317.TW','2303.TW','2409.TW','3481.TW','2603.TW','2618.TW','2327.TW','3711.TW','2449.TW','3045.TW','2412.TW']
NOTICE='Yahoo Finance via free unofficial yfinance; delayed/indicative daily bars. No guaranteed realtime accuracy or exchange-certified quotes. Personal research only.'
class DataError(Exception):pass

def research(root):
    m=runpy.run_path(str(root/'src/research.py'));m['SOURCES'].add(SOURCE);return m

def policy(root):return runpy.run_path(str(root/'src/reliability.py'))
def config(root):
    p=root/'config/yfinance.json'
    if p.is_symlink() or p.stat().st_mode&0o077:raise DataError('PRIVATE_CONFIG_REQUIRED')
    c=json.loads(p.read_text())
    if c['instance_id']!='T2' or c['symbols']!=SYMBOLS or c['interval']!='1d' or c['period']!='5d' or c['tail_extra']!={'symbol':'2382.TW','period':'1mo'} or c['delivery']!='local-only':raise DataError('YFINANCE_CONFIG_REFUSED')
    if not 0<c['cache_seconds']<=900 or not 0<c['checkpoint_max_seconds']<=4500 or not 0<c['run_timeout_seconds']<=420 or not 0<c['per_symbol_timeout_seconds']<=20:raise DataError('YFINANCE_LIMIT_REFUSED')
    if not isinstance(c['market_day_accepted'],bool) or not isinstance(c['allow_limited_daily'],bool):raise DataError('YFINANCE_ACCEPTANCE_FLAG_INVALID')
    return c

def requests(c):return [(s,'5d') for s in c['symbols']]+[('2382.TW','1mo')]

def normalize(payload,now,c):
    """Preserve yfinance's aware daily index; it is not a latest-trade time."""
    rows=[];seen=set();expected=set(requests(c))
    if payload.get('yfinance_version')!='1.4.1':raise DataError('YFINANCE_VERSION_MISMATCH')
    for item in payload['series']:
        pair=(item['symbol'],item['period'])
        if pair not in expected or pair in seen:raise DataError('DUPLICATE_OR_UNKNOWN_SERIES')
        seen.add(pair);prior=None
        if not item['bars'] or len(item['bars'])>32:raise DataError('EMPTY_OR_OVERSIZED_HISTORY')
        for bar in item['bars']:
            stamp=dt.datetime.fromisoformat(bar['timestamp'])
            if stamp.tzinfo is None or stamp.utcoffset() is None or stamp>now or (prior and stamp<=prior):raise DataError('INVALID_SOURCE_TIMESTAMP')
            prior=stamp;day=stamp.astimezone(TZ).date()
            if stamp.utcoffset()!=dt.timedelta(hours=8):raise DataError('EXCHANGE_TIMEZONE_MISMATCH')
            if (now.astimezone(TZ).date()-day).days>45:raise DataError('HISTORY_TOO_OLD')
            fields={}
            for name in ['Open','High','Low','Close','Volume']:
                value=bar.get(name)
                if value is not None and (isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value)):raise DataError('INVALID_BAR_VALUE')
                if name!='Volume' and (value is None or value<=0):raise DataError('INVALID_BAR_PRICE')
                if name=='Volume' and value is not None and (value<0 or value!=int(value)):raise DataError('INVALID_BAR_VOLUME')
                fields[name.lower()]=value
            if fields['low']>min(fields['open'],fields['close']) or fields['high']<max(fields['open'],fields['close']) or fields['high']<fields['low']:raise DataError('INVALID_OHLC_RANGE')
            key=':'.join([item['symbol'],item['period'],stamp.isoformat()])
            row={'key':key,'entity_type':'market_instrument','entity_id':item['symbol'],'period':item['period'],'interval':'1d',
                 'source_as_of':stamp.isoformat(),'source_date':day.isoformat(),'bar_timestamp':stamp.isoformat(),
                 'source_timezone':'Asia/Taipei','timestamp_semantics':'daily_bar_label_not_latest_trade','latest_trade_at':None,
                 'quote_kind':'daily_bar','fields':fields,'units':{'price':'index_points' if item['symbol']=='^TWII' else 'TWD/share','volume':'provider_reported_volume_unconverted'},
                 'provider':'Yahoo Finance via yfinance','provider_version':VERSION,'yfinance_version':payload['yfinance_version'],
                 'source_url':'https://query2.finance.yahoo.com/v8/finance/chart/'+item['symbol'],'notice':NOTICE}
            rows.append(row)
    if seen!=expected:raise DataError('MISSING_SERIES')
    if len({r['key'] for r in rows})!=len(rows):raise DataError('DUPLICATE_BAR')
    return sorted(rows,key=lambda r:r['key'])

def worker(root):
    """One bounded sequential batch. Library may perform cookie/crumb requests.
    No manual HTTP, identity rotation, rate-limit workaround or shared session.
    """
    import yfinance as yf
    c=config(root)
    yf.set_tz_cache_location(str(root/'state/yfinance-library-cache'))
    series=[]
    for symbol,period in requests(c):
        frame=yf.Ticker(symbol).history(period=period,interval='1d',auto_adjust=True,prepost=False,repair=False,timeout=10,raise_errors=True)
        bars=[]
        for stamp,row in frame.iterrows():
            bar={'timestamp':stamp.isoformat()}
            for name in ['Open','High','Low','Close','Volume']:
                value=float(row[name]);bar[name]=value if math.isfinite(value) else None
            bars.append(bar)
        series.append({'symbol':symbol,'period':period,'bars':bars})
    print(json.dumps({'yfinance_version':yf.__version__,'series':series},allow_nan=False,separators=(',',':')))

def fetch(root,now,c):
    m=research(root);p=root/'state/yfinance-fetch.json';cached=False
    if p.exists():
        try:
            old=json.loads(p.read_text());age=(now-dt.datetime.fromisoformat(old['fetched_at'])).total_seconds()
            body=root/'state/research-responses'/(old['sha256']+'.body')
            if 0<=age<c['cache_seconds'] and hashlib.sha256(body.read_bytes()).hexdigest()==old['sha256']:
                return json.loads(body.read_bytes()),{**old,'cached':True}
        except (OSError,ValueError,KeyError):pass
    # Outer wall-clock bound also limits yfinance internal handshake/retry time.
    proc=subprocess.run([c['python'],'-I','-B',str(root/'src/yfinance_source.py'),'--worker',str(root)],capture_output=True,timeout=c['run_timeout_seconds'],env={k:v for k,v in os.environ.items() if k not in {'PYTHONPATH','PYTHONHOME'}})
    if proc.returncode or not proc.stdout or len(proc.stdout)>2_000_000:raise DataError('YFINANCE_FETCH_FAILED_OR_OVERSIZED')
    payload=json.loads(proc.stdout);normalize(payload,now,c)
    sha=hashlib.sha256(proc.stdout).hexdigest();folder=root/'state/research-responses';folder.mkdir(mode=0o700,exist_ok=True)
    m['atomic'](folder/(sha+'.body'),proc.stdout)
    meta={'url':'yfinance.Ticker.history; 17 x 5d + 2382.TW 1mo; interval=1d','sha256':sha,'bytes':len(proc.stdout),'fetched_at':now.isoformat(),'cached':cached,
          'raw_semantics':'exact normalized worker stdout, not original Yahoo HTTP response; yfinance performs upstream parsing/adjustment'}
    m['atomic'](p,meta);return payload,meta

def collect(now,root=ROOT):
    c=config(root);m=research(root);payload,meta=fetch(root,now,c);rows=normalize(payload,now,c)
    for row in rows:
        row['calendar_session']=policy(root)['calendar_state'](root,dt.date.fromisoformat(row['source_date']),now)
        row['calendar_note']='PROVIDER_BAR_ON_VERIFIED_CLOSED_DATE' if row['calendar_session']=='CLOSED' else 'provider label retained; not proof of a trade'
        row['version']=m['digest'](row)
    batch={'instance_id':'T2','source':SOURCE,'provider_version':VERSION,'fetched_at':meta['fetched_at'],'responses':[meta],'rows':rows,
           'coverage':{'series':18,'instruments':17,'interval':'1d','notice':NOTICE,'provider_calendar_conflict_dates':sorted({r['source_date'] for r in rows if r['calendar_session']=='CLOSED'})},'failures':[]}
    batch['batch_id']=m['digest']({k:batch[k] for k in ['source','provider_version','rows','failures']});return batch

def run(now,root=ROOT):
    m=research(root);p=root/'state/research-yfinance_daily.json'
    prior=json.loads(p.read_text()) if p.exists() else {}
    try:
        batch=collect(now,root);receipt=m['persist'](batch,root)
        result={'instance_id':'T2','source':SOURCE,'result':'PASS','last_attempt':now.isoformat(),'last_success':batch['fetched_at'],
                **receipt,'rowset_sha256':m['digest'](batch['rows']),'responses':batch['responses'],'coverage':batch['coverage'],
                'source_as_of':sorted({r['source_as_of'] for r in batch['rows']}),'notice':NOTICE}
        m['atomic'](p,result);return result
    except Exception:
        m['atomic'](p,{**prior,'result':'FAILED','last_attempt':now.isoformat(),'error':'YFINANCE_COLLECTION_OR_PERSISTENCE_FAILED'});raise

def status(now,root=ROOT):
    try:
        c=config(root);m=research(root);s=m['source_state'](root,SOURCE,now)
        if not s.get('usable'):return s
        age=(now-dt.datetime.fromisoformat(s['last_success'])).total_seconds()
        if not 0<=age<c['checkpoint_max_seconds']:return {**s,'usable':False,'reason':'YFINANCE_CHECKPOINT_EXPIRED'}
            # Use precisely the committed batch, not mixed latest heads from older runs.
        bid=s['batch_id'];ing=m['ingestion']()
        batch=json.loads(ing['sql']('raw',f"SELECT payload FROM t2_research.batches WHERE batch_id={ing['json_sql'](bid)}#>>'{{}}';").decode().splitlines()[-1])
        if m['digest'](batch['rows'])!=s['rowset_sha256']:raise DataError('BATCH_HASH_MISMATCH')
        return {**s,'rows':batch['rows']}
    except Exception:return {'usable':False,'reason':'YFINANCE_LINEAGE_UNAVAILABLE_OR_INVALID'}

def assess(rows,now,root=ROOT):
    p=policy(root);local=now.astimezone(TZ);cal=p['calendar_state'](root,local.date(),now)
    out={'status':'DATA_DELAYED','live_report_allowed':False,'quality':'LIMITED','notice':NOTICE,'source_as_of':[], 'rows':[]}
    if cal=='CLOSED':return {**out,'status':'MARKET_CLOSED','quality':'MARKET_CLOSED'}
    if cal!='SCHEDULED_SESSION':return {**out,'quality':'DEGRADED','reason':cal}
    if not dt.time(9,0)<=local.time().replace(tzinfo=None)<dt.time(13,30):return {**out,'quality':'DEGRADED','reason':'OUTSIDE_MARKET_SESSION'}
    latest={}
    for row in rows:
        symbol=row['entity_id']
        if row['period']=='5d' and (symbol not in latest or row['bar_timestamp']>latest[symbol]['bar_timestamp']):latest[symbol]=row
    if set(latest)!=set(SYMBOLS):return {**out,'quality':'DEGRADED','reason':'INCOMPLETE_SYMBOL_COVERAGE'}
    if any(r['source_date']!=local.date().isoformat() for r in latest.values()):return {**out,'reason':'CURRENT_SESSION_BAR_ABSENT; previous close not substituted'}
    if any(r['fields']['volume'] is None or r['fields']['volume']<=0 for s,r in latest.items() if s!='^TWII'):return {**out,'reason':'CURRENT_SESSION_VOLUME_ABSENT'}
    if any(r['timestamp_semantics']!='daily_bar_label_not_latest_trade' or r['latest_trade_at'] is not None for r in latest.values()):return {**out,'quality':'DEGRADED','reason':'DAILY_TIMESTAMP_CONTRACT_INVALID'}
    return {**out,'status':'LIMITED','reason':'CURRENT_DAY_DAILY_BAR; latest trade timestamp unknown; no realtime freshness assertion',
            'rows':list(latest.values()),'source_as_of':sorted({r['source_as_of'] for r in latest.values()})}

def make_report(job,now,root=ROOT):
    if job not in {'opening','tail'}:raise DataError('REPORT_NOT_ALLOWED')
    s=status(now,root);quality=assess(s.get('rows',[]),now,root)
    if quality['status']!='MARKET_CLOSED' and not s.get('usable'):quality.update(status='DATA_DELAYED',quality='DEGRADED',reason=s.get('reason'),rows=[])
    return {'instance_id':'T2','report_type':job,'applicable_date':now.astimezone(TZ).date().isoformat(),'generated_at':now.isoformat(),
            **quality,'dependency':{k:v for k,v in s.items() if k!='rows'},'delivery':'local-only','scoring_policy':'Factual daily OHLCV only; full M2 discretionary strategy and macro score parity unavailable; no recommendations',
            'market_day_accepted':config(root)['market_day_accepted'],'schedule_enabled':job in json.loads((root/'config/instance.json').read_text())['enabled_jobs']}

def write_report(job,slot,now,root=ROOT):
    m=research(root);report=make_report(job,now,root)
    if report['status']=='MARKET_CLOSED':return {'result':'CLOSED',**report}
    if not config(root)['market_day_accepted'] or not config(root)['allow_limited_daily'] or report['status']!='LIMITED':return {'result':'DEGRADED',**report,'reason':report.get('reason','ACTUAL_MARKET_DAY_ACCEPTANCE_PENDING')}
    if job=='tail':
        opening=root/'state/yfinance-report-opening.json'
        if not opening.exists():return {'result':'DEGRADED','reason':'SAME_DAY_OPENING_ABSENT'}
        try:
            ptr=json.loads(opening.read_text())
            if ptr['applicable_date']!=report['applicable_date'] or ptr['quality']!='LIMITED' or len(ptr['artifacts'])!=2:raise ValueError()
            for a in ptr['artifacts']:
                if not a['path'].startswith('reports/t2-yfinance-') or '..' in Path(a['path']).parts or hashlib.sha256((root/a['path']).read_bytes()).hexdigest()!=a['sha256']:raise ValueError()
        except (OSError,ValueError,KeyError):return {'result':'DEGRADED','reason':'SAME_DAY_OPENING_INVALID'}
    rid=m['digest']({'job':job,'slot':slot});base=root/'reports'/('t2-yfinance-'+rid[:20]);artifacts=[]
    for suffix,value in [('.json',report),('.md',('# T2 '+job+' — LIMITED\n\n'+NOTICE+'\n\n'+json.dumps(report,ensure_ascii=False,indent=2)+'\n').encode())]:
        path=base.with_suffix(suffix)
        if path.exists() and path.read_bytes()!=(value if isinstance(value,bytes) else m['canonical'](value)+b'\n'):return {'result':'DEGRADED','reason':'IMMUTABLE_SLOT_CONFLICT'}
        if not path.exists():m['atomic'](path,value)
        artifacts.append({'path':str(path.relative_to(root)),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
    pointer={'applicable_date':report['applicable_date'],'generated_at':now.isoformat(),'quality':'LIMITED','artifacts':artifacts,'slot':slot}
    m['atomic'](root/'state'/('yfinance-report-'+job+'.json'),pointer)
    return {'result':'PASS','quality':'LIMITED','live_report_allowed':False,'artifacts':artifacts}

def publish(slot,now,root=ROOT):
    """Local pointer only. Recheck source/calendar/artifact hashes each attempt."""
    c=config(root);p=policy(root);m=research(root);day=now.astimezone(TZ).date()
    if p['calendar_state'](root,day,now)=='CLOSED':return {'result':'CLOSED'}
    if not c['market_day_accepted'] or not c['allow_limited_daily']:return {'result':'DEGRADED','reason':'ACTUAL_MARKET_DAY_ACCEPTANCE_PENDING'}
    s=status(now,root)
    if not s.get('usable') or assess(s.get('rows',[]),now,root)['status']!='LIMITED':return {'result':'DEGRADED','reason':'INVALID_OR_STALE_SOURCE'}
    target='tail' if now.astimezone(TZ).hour>=13 else 'opening';path=root/'state'/('yfinance-report-'+target+'.json')
    try:
        ptr=json.loads(path.read_text());age=(now-dt.datetime.fromisoformat(ptr['generated_at'])).total_seconds()
        if ptr['applicable_date']!=day.isoformat() or not 0<=age<=4500 or ptr['quality']!='LIMITED':raise ValueError()
        if len(ptr['artifacts'])!=2:raise ValueError()
        for a in ptr['artifacts']:
            if not a['path'].startswith('reports/t2-yfinance-') or Path(a['path']).name!=a['path'].split('/')[-1] or '..' in Path(a['path']).parts:raise ValueError()
            if hashlib.sha256((root/a['path']).read_bytes()).hexdigest()!=a['sha256']:raise ValueError()
        marker=root/'state'/('local-published-'+m['digest'](ptr['artifacts'])+'.json')
        if marker.exists():return {'result':'PASS','duplicate':True,'delivery':'local-only'}
        m['atomic'](root/'state/local-publication.json',ptr);m['atomic'](marker,ptr)
        return {'result':'PASS','duplicate':False,'delivery':'local-only','artifacts':ptr['artifacts']}
    except (OSError,ValueError,KeyError):return {'result':'DEGRADED','reason':'VALID_REPORT_ABSENT_OR_EXPIRED'}

if __name__=='__main__':
    os.umask(0o077)
    if len(sys.argv)==3 and sys.argv[1]=='--worker':worker(Path(sys.argv[2]).resolve())
    else:raise SystemExit('internal bounded worker only')
