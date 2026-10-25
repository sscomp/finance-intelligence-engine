"""Independent T2 scheduler observer: stdlib, local reads/files, no runtime import.

Continues detecting stale heartbeats and digest failures when the scheduler's
code, API or collector execution is broken. Never starts/restarts another job.
"""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import tempfile
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parents[1];UTC=dt.timezone.utc


def observe(root=ROOT,now=None):
    now=now or dt.datetime.now(UTC);reasons=[];ages={}
    def read(name):
        try:return json.loads((root/name).read_text())
        except (OSError,ValueError):return {}
    def age(name,key,limit):
        value=read(name)
        try:seconds=(now-dt.datetime.fromisoformat(value[key].replace('Z','+00:00'))).total_seconds()
        except (ValueError,KeyError,TypeError):seconds=None
        ages[name]=round(seconds,1) if seconds is not None else None
        if seconds is None or not 0<=seconds<limit:reasons.append(name+':STALE_OR_UNAVAILABLE')
        return value
    age('state/heartbeat.json','at',180)
    monitor=age('state/monitor.json','generated_at',420)
    if monitor.get('alert_state') not in {'CURRENT_DAILY_OBSERVATIONS','CURRENT_WITHIN_THRESHOLD'}:
        reasons.append('MONITOR_DATA_HEALTH:'+str(monitor.get('alert_state','UNAVAILABLE')))
    for source in ['twse_prices','twse_valuation','twse_institutional','fred_macro']:
        value=age('state/ingestion-'+source+'.json','last_success',7500)
        if value.get('result')!='PASS':reasons.append(source+':COLLECTOR_FAILED_OR_MISSING')
    activation=read('config/activation.json')
    if activation.get('result')!='PASS':reasons.append('ACTIVATION_UNAVAILABLE')
    for name,expected in activation.get('files',{}).items():
        try:
            path=root/name
            if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest()!=expected:reasons.append('ACTIVATION_DIGEST_CHANGED:'+name)
        except OSError:reasons.append('ACTIVATION_FILE_MISSING:'+name)
    calendar=read('config/calendar.json');today=now.astimezone(ZoneInfo('Asia/Taipei')).date().isoformat()
    try:
        stamp=dt.datetime.fromisoformat(calendar['verified_utc'])
        if not calendar['valid_from']<=today<=calendar['valid_until'] or not 0<=(now-stamp).total_seconds()<=calendar['verification_max_age_days']*86400:raise ValueError()
    except (ValueError,KeyError,TypeError):reasons.append('CALENDAR_UNAVAILABLE_OR_EXPIRED')
    for evidence in calendar.get('evidence',[]):
        try:
            if hashlib.sha256((root/evidence['path']).read_bytes()).hexdigest()!=evidence['sha256']:reasons.append('CALENDAR_EVIDENCE_CHANGED')
        except (OSError,KeyError,TypeError):reasons.append('CALENDAR_EVIDENCE_UNAVAILABLE')
    return {'instance_id':'T2','at':now.isoformat(),'result':'DEGRADED' if reasons else 'PASS','reasons':reasons,'ages_seconds':ages,'pid':os.getpid(),'delivery':'local-only','scheduler_imported':False}


def main(root=ROOT):
    os.umask(0o077);result=observe(root)
    fd,name=tempfile.mkstemp(dir=root/'state',prefix='.health-')
    try:
        with os.fdopen(fd,'w') as file:
            json.dump(result,file,sort_keys=True);file.write('\n');file.flush();os.fsync(file.fileno())
        os.replace(name,root/'state/independent-health.json')
    finally:
        if os.path.exists(name):os.unlink(name)
    print(json.dumps(result,sort_keys=True))
    return 1 if result['result']=='DEGRADED' else 0

if __name__=='__main__':raise SystemExit(main())
