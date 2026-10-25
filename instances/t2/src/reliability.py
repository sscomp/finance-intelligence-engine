"""T2 calendar, dependency gates and local job functions; no delivery client."""
from __future__ import annotations
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import runpy
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parents[1]
TZ=ZoneInfo('Asia/Taipei'); US=ZoneInfo('America/Chicago'); UTC=dt.timezone.utc
GOOD={'CURRENT','MARKET_CLOSED_CURRENT','AWAITING_PUBLICATION'}
# Verified source-backed reports are gated at execution; quote-dependent schedules
# stay disabled in instance config. Existing closed-session semantics are retained.
BLOCKED={}
SCHEDULE_BLOCKERS={j:'STAGED: yfinance daily-bar adapter installed; actual trading-day source/report acceptance pending (no realtime quote claim)' for j in ['opening','tail','publisher']}

CRONS={
 'yfinance':'20 9-13 * * 1-5', 'research_rss':'10 */6 * * *','research_company':'20 7 * * *',
 'twse_valuation':'0 * * * *','twse_prices':'0 * * * *','twse_institutional':'0 * * * *','fred_macro':'0 * * * *',
 'monitor':'*/5 * * * *','intraday':'*/30 9-13 * * 1-5',
 'opening':'30 9 * * 1-5','tail':'0 13 * * 1-5','publisher':'*/10 9-13 * * 1-5',
 'macro_morning':'30 8 * * 1-6','industry_weekly':'0 8 * * 1','company_monthly':'0 8 12 * *',
 'constituents_review':'0 9 1 1,4,7,10 *','phase4_shadow':'0 9 * * 1-6',
 'watchdog_open':'45 9 * * 1-5','watchdog_tail':'15 13 * * 1-5'}


def runtime():
    return runpy.run_path(str(ROOT/'src/fie_t2.py'))


def load_calendar(root, day, now=None):
    """Missing, malformed, expired or unverified closure evidence fails closed."""
    try:
        p=root/'config/calendar.json'
        if p.is_symlink() or p.stat().st_mode & 0o077:raise ValueError()
        c=json.loads(p.read_text())
        if c['instance_id']!='T2' or not c['valid_from'] <= day.isoformat() <= c['valid_until']:raise ValueError()
        for d in c['closed_dates']+c.get('extra_closed_dates',[]):dt.date.fromisoformat(d)
        if c.get('schema')!=2:raise ValueError()
        if now:
            verified=dt.datetime.fromisoformat(c['verified_utc'])
            if not 0 <= (now-verified).total_seconds() <= c['verification_max_age_days']*86400:raise ValueError()
        for item in c['evidence']:
            path=root/item['path']
            if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest()!=item['sha256']:raise ValueError()
        return c
    except (OSError,ValueError,KeyError,TypeError):return None


def calendar_state(root,day,now=None):
    c=load_calendar(root,day,now)
    if not c:return 'CALENDAR_UNAVAILABLE_OR_EXPIRED'
    return 'CLOSED' if day.weekday()>=5 or day.isoformat() in c['closed_dates']+c.get('extra_closed_dates',[]) else 'SCHEDULED_SESSION'


def trading_day(root,day,direction,include=False,now=None):
    for i in range(0 if include else 1,32):
        d=day+dt.timedelta(days=i*direction)
        state=calendar_state(root,d,now)
        if state=='CALENDAR_UNAVAILABLE_OR_EXPIRED':return None
        if state=='SCHEDULED_SESSION':return d
    return None


def us_session(c,day,series=None):
    closed=c['us_equity_closed_dates'] if series=='vix' else c['us_closed_dates']
    return day.weekday()<5 and day.isoformat() not in closed


def freshness(root,source,source_date,now,series=None):
    local=now.astimezone(TZ);c=load_calendar(root,local.date(),now)
    out={'policy':'T2_CALENDAR_V2','calendar':('US_FRED_CBOE' if series=='vix' else 'US_FRED_TREASURY') if source=='fred_macro' else 'TWSE','source_date':source_date}
    try:observed=dt.date.fromisoformat(source_date)
    except (ValueError,TypeError):return {**out,'state':'UNAVAILABLE','reason':'SOURCE_DATE_MISSING_OR_INVALID'}
    if observed>local.date():return {**out,'state':'UNAVAILABLE','reason':'FUTURE_OBSERVATION'}
    if not c:return {**out,'state':'CALENDAR_UNAVAILABLE_OR_EXPIRED','reason':'NO_TRUSTED_CALENDAR_FALLBACK'}
    if source=='fred_macro':
        # FRED publishes previous US business-day observations, not TWSE days.
        # Conservative release deadline 18:00 Chicago; per-series snapshots
        # explicitly anchor the observed Oct23 release and Oct26 next release.
        us=now.astimezone(US);release=us.date()
        if us.hour<18:release-=dt.timedelta(days=1)
        while not us_session(c,release):release-=dt.timedelta(days=1)
        expected=release-dt.timedelta(days=1)
        while not us_session(c,expected,series):expected-=dt.timedelta(days=1)
        anchor=c['fred_release_evidence']
        next_release=dt.datetime.fromisoformat(anchor['next_release_deadline'])
        if dt.datetime.fromisoformat(anchor['verified_at'])<=now<next_release:
            expected=dt.date.fromisoformat(anchor['observation_date'])
        closed=not us_session(c,us.date(),series)
        state=('MARKET_CLOSED_CURRENT' if closed else 'AWAITING_PUBLICATION') if observed>=expected else 'PROVIDER_LATE'
        if observed<expected:
            gap=sum(us_session(c,expected-dt.timedelta(days=i),series) for i in range((expected-observed).days))
            if gap>=2:state='STALE'
    else:
        current=calendar_state(root,local.date(),now)
        closed=current=='CLOSED'
        # Daily endpoints cannot supply an opening quote. Current session daily
        # observations become due at 18:00, institutional flow at 20:00 TPE.
        deadline=20 if source=='twse_institutional' else 18
        expected=trading_day(root,local.date(),-1,include=(not closed and local.hour>=deadline),now=now)
        if expected is None:return {**out,'state':'CALENDAR_UNAVAILABLE_OR_EXPIRED'}
        state=('MARKET_CLOSED_CURRENT' if closed else 'AWAITING_PUBLICATION' if local.hour<deadline else 'CURRENT') if observed>=expected else 'PROVIDER_LATE'
        if observed<expected:
            gap=sum(calendar_state(root,expected-dt.timedelta(days=i),now)=='SCHEDULED_SESSION' for i in range((expected-observed).days))
            if gap>=2:state='STALE'
        out['next_trading_day']=(trading_day(root,local.date(),1,now=now) or local.date()).isoformat()
    return {**out,'state':state,'expected_source_date':expected.isoformat(),'market_closed':closed,
            'meaning':'Daily published observations; collection success and intraday coverage are separate'}


def source_status(root,source,now):
    t=runtime();p=root/'state'/('ingestion-'+source+'.json')
    try:item=json.loads(p.read_text())
    except (OSError,ValueError):item={'result':'NEVER_RUN'}
    stamp=t['parse_time'](item.get('last_success'));age=(now-stamp).total_seconds() if stamp else None
    pipeline='CURRENT' if item.get('result')=='PASS' and age is not None and 0<=age<7500 else 'FAILED' if item.get('result')=='FAILED' else 'STALE_OR_MISSING'
    dates=item.get('source_dates',[]); latest=max(dates) if dates else None
    quality=freshness(root,source,latest,now)
    # FRED is checked per series. A new DGS10 must not hide a stale VIX/DGS2.
    market=root/'state'/('market-'+source+'.json')
    try:
        view=json.loads(market.read_text())
        if item.get('batch_id') and view.get('batch_id')!=item['batch_id']:
            pipeline='RECEIPT_MISMATCH'
        if source!='fred_macro':
            dates=[x['source_date'] for x in view['observations']]
            if dates:
                quality=freshness(root,source,min(dates),now)
                quality['date_coverage']='oldest latest entity/field observation in refreshed feed'
    except (OSError,KeyError,ValueError):
        pipeline='MARKET_VIEW_UNAVAILABLE'
    if source=='fred_macro':
        try:
            rows=json.loads(market.read_text())['observations'];series={}
            for field in ['us10y','us2y','vix']:
                ds=[x['source_date'] for x in rows if x['signal_type']==field]
                series[field]=freshness(root,source,max(ds) if ds else None,now,field)
            quality['series']=series
            if any(v['state'] not in GOOD for v in series.values()):quality['state']='STALE' if any(v['state']=='STALE' for v in series.values()) else 'PROVIDER_LATE'
        except (OSError,KeyError,ValueError):quality={'state':'UNAVAILABLE','reason':'FRED_SERIES_RECEIPTS_MISSING'}
    return {**item,'success_age_seconds':round(age,1) if age is not None else None,'pipeline_state':pipeline,'source_freshness':quality['state'],'freshness':quality}


def dependency(root,job,now):
    t=runtime();sources=['fred_macro'] if job in {'macro_morning','phase4_shadow'} else list(t['COLLECTORS'])
    statuses={s:source_status(root,s,now) for s in sources}; reasons=[]
    market_job=job in {'opening','tail','publisher','watchdog_open','watchdog_tail'}
    cal=calendar_state(root,now.astimezone(TZ).date(),now)
    if cal=='CALENDAR_UNAVAILABLE_OR_EXPIRED':reasons.append(cal)
    elif market_job and cal=='CLOSED':return {'state':'CLOSED','reasons':['TWSE_CLOSED'],'sources':statuses}
    for s,x in statuses.items():
        if x['pipeline_state']!='CURRENT':reasons.append(s+':'+x['pipeline_state'])
        if x['source_freshness'] not in GOOD:reasons.append(s+':'+x['source_freshness'])
    if job in {'opening','tail','publisher'}:
        # Explicitly impossible with proven daily-only collector. Do not loosen
        # this gate by accepting yesterday's daily close as today's intraday.
        reasons.append('YFINANCE_DAILY_BAR_ONLY; no latest-trade timestamp; use explicit LIMITED daily-bar policy, actual trading-day acceptance pending')
    return {'state':'DEGRADED' if reasons else 'PASS','reasons':reasons,'sources':statuses,'calendar_state':cal}


def matches(expr,local):
    vals=[local.minute,local.hour,local.day,local.month,(local.weekday()+1)%7]
    for spec,value in zip(expr.split(),vals):
        if spec=='*':continue
        if spec.startswith('*/'):
            if value%int(spec[2:]):return False
        else:
            allowed=set()
            for part in spec.split(','):
                if '-' in part:
                    lo,hi=map(int,part.split('-'));allowed.update(range(lo,hi+1))
                else:allowed.add(int(part))
            if value not in allowed:return False
    return True


def due(root,now):
    local=now.astimezone(TZ).replace(second=0,microsecond=0);out=[]
    for job,expr in CRONS.items():
        if job in BLOCKED:continue
        # One current bucket for periodic jobs; fixed daily jobs get 60 min
        # catchup. No previous-day replay, no unbounded restart backlog.
        if job=='yfinance':
            slot=local.replace(minute=20)
            if slot<=local and matches(expr,slot) and calendar_state(root,local.date(),now)=='SCHEDULED_SESSION':out.append((job,slot.isoformat()))
        elif job in {'monitor','intraday','publisher','research_rss',*runtime()['COLLECTORS']}:
            width=360 if job=='research_rss' else 5 if job=='monitor' else 30 if job=='intraday' else 10 if job=='publisher' else 60
            slot=local.replace(hour=local.hour//6*6,minute=10) if job=='research_rss' else local-dt.timedelta(minutes=local.minute%width)
            if slot<=local and matches(expr,slot) and (job!='intraday' or calendar_state(root,local.date(),now)=='SCHEDULED_SESSION'):out.append((job,slot.isoformat()))
        else:
            for offset in range(61):
                slot=local-dt.timedelta(minutes=offset)
                if slot.date()==local.date() and matches(expr,slot):out.append((job,slot.isoformat()));break
    # Collectors first, independent monitor next, consumers last.
    order={j:i for i,j in enumerate(CRONS)}
    return sorted(out,key=lambda x:order[x[0]])


def schedules(root,now):
    t=runtime();cfg=t['load_config'](root);out=[]
    local=now.astimezone(TZ).replace(second=0,microsecond=0)+dt.timedelta(minutes=1)
    for job,expr in CRONS.items():
        next_slot=None
        # At most a year to cover quarterly boundaries, no external scheduler.
        # Iterate calendar days and only the allowed hours/minutes.
        for days in range(370):
            day=(local+dt.timedelta(days=days)).replace(hour=0,minute=0)
            for hour in [h for h in range(24) if matches('0 '+expr.split()[1]+' * * *', day.replace(hour=h,minute=0))]:
                for minute in range(60):
                    candidate=day.replace(hour=hour,minute=minute)
                    if candidate<local:continue
                    if matches(expr,candidate):next_slot=candidate;break
                if next_slot:break
            if next_slot:break
        out.append({'id':'t2-fie-'+job,'job':job,'cron':expr,'timezone':'Asia/Taipei','enabled':job in cfg['enabled_jobs'],
                    'blocked_reason':SCHEDULE_BLOCKERS.get(job,BLOCKED.get(job)),'next_run':next_slot.isoformat() if next_slot else None,
                    'executor':'com.sscomp.fie.t2.scheduler; 60s launchd tick',
                    'market_gate':'TWSE calendar and dependencies' if job in {'opening','tail','publisher','intraday','watchdog_open','watchdog_tail'} else 'independent US/research cadence'})
    return out


def receipt_path(root,job,slot):
    return root/'state'/('receipt-'+job+'-'+hashlib.sha256(slot.encode()).hexdigest()[:20]+'.json')


def execute(root,job,slot,now):
    t=runtime();atomic=t['atomic'];day=now.astimezone(TZ).date().isoformat()
    if job in BLOCKED:return {'result':'BLOCKED','reason':BLOCKED[job]}
    if job in {'industry_weekly','company_monthly'}:
        return runpy.run_path(str(root/'src/research.py'))['write_report'](job,now,root)
    if job=='constituents_review':
        text='# T2 quarterly constituents human review\n\nReview official constituents and T2 universe; no automatic changes or outbound delivery.\n'
        path=root/'reports'/('t2-constituents-review-'+day+'.md');atomic(path,text)
        return {'result':'PASS','artifact':str(path.relative_to(root)),'sha256':t['sha'](path),'human_review_required':True}
    if job in {'opening','tail','publisher'}:
        y=runpy.run_path(str(root/'src/yfinance_source.py'))
        return y['publish'](slot,now,root) if job=='publisher' else y['write_report'](job,slot,now,root)
    dep=dependency(root,job,now)
    if job.startswith('watchdog_'):
        target='opening' if job=='watchdog_open' else 'tail';hour,minute=(9,30) if target=='opening' else (13,0)
        target_slot=now.astimezone(TZ).replace(hour=hour,minute=minute,second=0,microsecond=0).isoformat()
        p=receipt_path(root,target,target_slot)
        prior=json.loads(p.read_text()) if p.exists() else {}
        healthy=dep['state']=='PASS' and prior.get('result')=='PASS' and all((root/a['path']).exists() and t['sha'](root/a['path'])==a['sha256'] for a in prior.get('artifacts',[])) and bool(prior.get('artifacts'))
        if healthy:
            y=runpy.run_path(str(root/'src/yfinance_source.py'));ys=y['status'](now,root)
            healthy=ys.get('usable',False) and y['assess'](ys.get('rows',[]),now,root)['status']=='LIMITED'
        return {'result':'CLOSED' if dep['state']=='CLOSED' else 'PASS' if healthy else 'DEGRADED','target_job':target,'target_receipt':prior.get('result','MISSING'),'dependency':dep,'delivery':'local-only; no retry delivery'}
    if dep['state']!='PASS':return {'result':dep['state'],'reason':'; '.join(dep['reasons']),'dependency':dep}
    if job=='macro_morning':
        market=t['market_data'](root,'fred_macro',now)
        fields={}
        for row in market.get('observations',[]):
            key=row['signal_type']
            if key not in fields or row['source_date']>fields[key]['source_date']:fields[key]=row
        if not {'us10y','us2y','vix','yield_spread'}<=set(fields):return {'result':'DEGRADED','reason':'FRED_REQUIRED_FIELDS_MISSING'}
        report={'schema_version':1,'instance_id':'T2','report_type':'macro_morning','applicable_date':day,'generated_at':now.isoformat(),'quality':'PARTIAL',
                'indicators':fields,'missing':['DXY','USD/TWD','30Y','full M2 macro scoring parity'],
                'conclusion':'INDETERMINATE; no trading signal','dependency':dep,'delivery':'local-only'}
        path=root/'reports'/(day+'.intelligence_report.json');md=path.with_suffix('.md')
        # This is an actual T2-local producer, never an imported M2 artifact.
        if not path.exists():atomic(path,report)
        actual=json.loads(path.read_text())
        if actual.get('instance_id')!='T2' or actual.get('applicable_date')!=day:return {'result':'DEGRADED','reason':'LOCAL_PRODUCER_IDENTITY_MISMATCH'}
        atomic(md,'# T2 macro morning — PARTIAL\n\n'+json.dumps(actual,ensure_ascii=False,indent=2)+'\n')
        return {'result':'PASS','quality':'PARTIAL','artifacts':[{'path':str(p.relative_to(root)),'sha256':t['sha'](p)} for p in [path,md]],'dependency':dep}
    if job=='phase4_shadow':
        path=root/'reports'/(day+'.intelligence_report.json')
        if not path.exists():return {'result':'DEGRADED','reason':'T2_LOCAL_INTELLIGENCE_REPORT_ABSENT; replay not performed'}
        report=json.loads(path.read_text())
        if report.get('schema_version')!=1 or report.get('instance_id')!='T2' or report.get('applicable_date')!=day:return {'result':'DEGRADED','reason':'T2_LOCAL_INTELLIGENCE_REPORT_INVALID'}
        source=t['parse_time'](report.get('generated_at'))
        if not source or not 0<=(now-source).total_seconds()<=7200:return {'result':'DEGRADED','reason':'LOCAL_PRODUCER_REPORT_EXPIRED'}
        replay=json.loads(json.dumps(report,sort_keys=True,ensure_ascii=False));sha=hashlib.sha256(t['canonical'](replay)).hexdigest()
        if replay!=report:return {'result':'FAILED','reason':'SHADOW_DETERMINISM_FAILURE'}
        return {'result':'PASS','quality':report['quality'],'source_artifact':str(path.relative_to(root)),'source_sha256':t['sha'](path),'deterministic_sha256':sha,'delivery':'local-only; structured-copy replay; no external writes'}
    return {'result':'DEGRADED','reason':'NO_VALID_CURRENT_SESSION_REPORT; no publication performed','dependency':dep}
