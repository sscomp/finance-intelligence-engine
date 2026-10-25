"""Offline fixtures; never stored in production or sent to a provider."""
import copy
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('yf_source',ROOT/'src/yfinance_source.py');y=importlib.util.module_from_spec(spec);spec.loader.exec_module(y)
NOW=dt.datetime(2026,10,27,1,30,tzinfo=dt.timezone.utc)
class YFinanceTests(unittest.TestCase):
 def setUp(self):
  self.mask=os.umask(0o077);self.tmp=tempfile.TemporaryDirectory();self.r=Path(self.tmp.name).resolve()
  for d in ['src','config','state','reports','secrets']:(self.r/d).mkdir()
  for p in (ROOT/'src').glob('*.py'):shutil.copyfile(p,self.r/'src'/p.name)
  for p in (ROOT/'templates').glob('*.json.example'):
   dest=self.r/'config'/p.name.removesuffix('.example');shutil.copyfile(p,dest);dest.chmod(0o600)
  self.c=y.config(self.r)
  cal={'instance_id':'T2','schema':2,'valid_from':'2026-01-01','valid_until':'2026-12-31','verified_utc':'2026-10-22T00:00:00+00:00','verification_max_age_days':30,'evidence':[], 'closed_dates':['2026-10-23','2026-10-26']}
  self.save('config/calendar.json',cal)
 def tearDown(self):self.tmp.cleanup();os.umask(self.mask)
 def save(self,path,x):
  p=self.r/path;p.write_text(json.dumps(x));p.chmod(0o600)
 def payload(self,stamp='2026-10-27T00:00:00+08:00'):
  return {'yfinance_version':'1.4.1','series':[{'symbol':s,'period':p,'bars':[{'timestamp':stamp,'Open':100.,'High':102.,'Low':99.,'Close':101.,'Volume':1234.}]} for s,p in y.requests(self.c)]}
 def rows(self):return y.normalize(self.payload(),NOW,self.c)
 def test_invocation_universe_and_periods(self):
  self.assertEqual(len(y.requests(self.c)),18);self.assertIn(('2382.TW','1mo'),y.requests(self.c));self.assertEqual(self.c['interval'],'1d')
 def test_source_as_of_is_bar_time_not_fetch_time(self):
  r=self.rows()[0];self.assertEqual(r['source_as_of'],'2026-10-27T00:00:00+08:00');self.assertIsNone(r['latest_trade_at']);self.assertEqual(r['quote_kind'],'daily_bar')
 def test_naive_future_or_wrong_timezone_rejected(self):
  for stamp in ['2026-10-27T00:00:00','2026-10-28T00:00:00+08:00','2026-10-26T16:00:00+00:00']:
   with self.assertRaises(y.DataError):y.normalize(self.payload(stamp),NOW,self.c)
 def test_empty_missing_duplicate_and_out_of_order_rejected(self):
  for variant in ['empty','missing','duplicate','order']:
   p=self.payload()
   if variant=='empty':p['series'][0]['bars']=[]
   if variant=='missing':p['series'].pop()
   if variant=='duplicate':p['series'].append(p['series'][0])
   if variant=='order':p['series'][0]['bars']*=2
   with self.assertRaises(y.DataError):y.normalize(p,NOW,self.c)
 def test_invalid_prices_and_volume_rejected(self):
  for field,value in [('Close',float('nan')),('Low',200),('Open',0),('Volume',-1),('Volume',1.5)]:
   p=self.payload();p['series'][0]['bars'][0][field]=value
   with self.assertRaises(y.DataError):y.normalize(p,NOW,self.c)
 def test_current_day_daily_is_limited_never_live(self):
  a=y.assess(self.rows(),NOW,self.r);self.assertEqual(a['status'],'LIMITED');self.assertFalse(a['live_report_allowed']);self.assertEqual(len(a['rows']),17)
 def test_prior_close_not_substituted(self):
  rows=y.normalize(self.payload('2026-10-22T00:00:00+08:00'),NOW,self.c);a=y.assess(rows,NOW,self.r);self.assertEqual(a['status'],'DATA_DELAYED');self.assertEqual(a['rows'],[])
 def test_closed_oct23_24_25_26_and_next_expected_day(self):
  p=y.policy(self.r)
  for day in [23,24,25,26]:
   now=NOW.replace(day=day);self.assertEqual(y.assess([],now,self.r)['status'],'MARKET_CLOSED')
  self.assertEqual(p['trading_day'](self.r,dt.date(2026,10,22),1,now=NOW),dt.date(2026,10,27))
 def test_calendar_expired_missing_or_evidence_changed(self):
  self.assertEqual(y.assess(self.rows(),NOW+dt.timedelta(days=40),self.r)['quality'],'DEGRADED')
  (self.r/'config/calendar.json').unlink();self.assertEqual(y.assess([],NOW,self.r)['quality'],'DEGRADED')
 def test_outside_session(self):
  for hour,minute in [(0,30),(5,30),(6,0)]:self.assertEqual(y.assess(self.rows(),NOW.replace(hour=hour,minute=minute),self.r)['quality'],'DEGRADED')
 def test_missing_volume_blocks_except_index(self):
  rows=self.rows()
  for r in rows:
   if r['entity_id']=='^TWII':r['fields']['volume']=None
  self.assertEqual(y.assess(rows,NOW,self.r)['status'],'LIMITED')
  rows[0]['fields']['volume']=0;self.assertEqual(y.assess(rows,NOW,self.r)['status'],'DATA_DELAYED')
 def test_cache_keeps_original_fetch_timestamp_and_hash(self):
  m=y.research(self.r);payload=self.payload();body=json.dumps(payload).encode();sha=__import__('hashlib').sha256(body).hexdigest()
  (self.r/'state/research-responses').mkdir();(self.r/'state/research-responses'/(sha+'.body')).write_bytes(body)
  meta={'sha256':sha,'fetched_at':(NOW-dt.timedelta(seconds=60)).isoformat()};self.save('state/yfinance-fetch.json',meta)
  with patch.object(subprocess,'run',side_effect=AssertionError('cache must avoid network')):
   p,out=y.fetch(self.r,NOW,self.c)
  self.assertTrue(out['cached']);self.assertEqual(out['fetched_at'],meta['fetched_at']);self.assertEqual(p,payload)
 def test_timeout_and_no_stale_cache_fallback(self):
  self.save('state/yfinance-fetch.json',{'sha256':'a'*64,'fetched_at':(NOW-dt.timedelta(hours=3)).isoformat()})
  with patch.object(subprocess,'run',side_effect=subprocess.TimeoutExpired('worker',420)):
   with self.assertRaises(subprocess.TimeoutExpired):y.fetch(self.r,NOW,self.c)
 def test_batch_identity_does_not_change_on_refetch(self):
  meta={'fetched_at':NOW.isoformat(),'sha256':'a'*64}
  with patch.object(y,'fetch',return_value=(self.payload(),meta)):
   a=y.collect(NOW,self.r);b=y.collect(NOW+dt.timedelta(minutes=1),self.r)
  self.assertEqual(a['batch_id'],b['batch_id']);self.assertEqual(a['rows'],b['rows'])
 def test_report_and_publisher_disabled_until_actual_acceptance(self):
  with patch.object(y,'status',return_value={'usable':True,'rows':self.rows()}):
   self.assertEqual(y.write_report('opening','slot',NOW,self.r)['result'],'DEGRADED')
   self.assertEqual(y.publish('slot',NOW,self.r)['reason'],'PUBLICATION_MODE_OR_ROOT_REFUSED')
  self.assertEqual(list((self.r/'reports').iterdir()),[])
 def test_local_publication_duplicate_restart_tamper_and_source_failure(self):
  self.save('DRY_RUN.json',{'DRY_RUN':True,'production_authorization':False})
  c=self.c.copy();c.update(market_day_accepted=True,DRY_RUN=True);self.save('config/yfinance.json',c)
  with patch.object(y,'status',return_value={'usable':True,'rows':self.rows(),'batch_id':'a'*64,'rowset_sha256':'b'*64,'last_success':NOW.isoformat()}):
   out=y.write_report('opening','slot',NOW,self.r);self.assertEqual(out['result'],'PASS')
   a=y.simulate_publish('slot',NOW,self.r);self.assertFalse(a['duplicate']);self.assertEqual(a['delivery'],'local-only')
   self.assertTrue(y.simulate_publish('new-slot',NOW,self.r)['duplicate'])
   # Reloading code keeps durable publication markers.
   again=runpy.run_path(y.__file__)
   with patch.dict(again['simulate_publish'].__globals__,status=lambda *a:{'usable':True,'rows':self.rows(),'batch_id':'a'*64,'rowset_sha256':'b'*64,'last_success':NOW.isoformat()}):self.assertTrue(again['simulate_publish']('restart',NOW,self.r)['duplicate'])
   path=self.r/out['artifacts'][0]['path'];path.write_text('{}');self.assertEqual(y.simulate_publish('slot',NOW,self.r)['result'],'DEGRADED')
  with patch.object(y,'status',return_value={'usable':False}):self.assertEqual(y.simulate_publish('slot',NOW,self.r)['result'],'DEGRADED')
 def test_tail_requires_same_day_opening(self):
  c=self.c.copy();c['market_day_accepted']=True;self.save('config/yfinance.json',c)
  with patch.object(y,'status',return_value={'usable':True,'rows':self.rows()}):self.assertEqual(y.write_report('tail','tail',NOW.replace(hour=5,minute=0),self.r)['reason'],'SAME_DAY_OPENING_ABSENT')
 def test_latest_read_has_bounded_timeout_without_changing_health(self):
  t=runpy.run_path(str(self.r/'src/fie_t2.py'));self.save('secrets/api-token','unit-placeholder')
  # Client expects a plain private token, never a production credential.
  (self.r/'secrets/api-token').write_text('unit-placeholder')
  client=t['Client'](self.r);seen=[]
  class Response:
   status=200
   def __enter__(self):return self
   def __exit__(self,*args):pass
   def read(self,*args):return json.dumps({'status':'ok','schema_version':'6.5','principal_id':t['PRINCIPAL']}).encode()
  class Opener:
   def open(self,request,timeout):seen.append(timeout);return Response()
  client.opener=Opener();client.read('latest');client.read('health');self.assertEqual(seen,[20,8])
 def test_scheduler_source_session_hourly_no_holiday_backlog(self):
  p=y.policy(self.r)
  for day in [23,24,25,26]:self.assertFalse(any(j=='yfinance' for j,s in p['due'](self.r,NOW.replace(day=day,minute=25))))
  slots=[s for j,s in p['due'](self.r,NOW.replace(minute=25)) if j=='yfinance'];self.assertEqual(slots,['2026-10-27T09:20:00+08:00'])
if __name__=='__main__':unittest.main()
