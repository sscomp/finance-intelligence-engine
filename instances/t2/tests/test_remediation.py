"""Fail-closed boundary tests, all targets are private temporary fixtures."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import runpy
from unittest.mock import patch
import unittest
import test_yfinance as base
from test_yfinance import y, NOW


class RemediationTests(unittest.TestCase):
 setUp=base.YFinanceTests.setUp
 tearDown=base.YFinanceTests.tearDown
 save=base.YFinanceTests.save
 payload=base.YFinanceTests.payload
 rows=base.YFinanceTests.rows
 def source(self):
  return {'usable':True,'rows':self.rows(),'batch_id':'a'*64,'rowset_sha256':'b'*64,'last_success':NOW.isoformat()}
 def enable(self,simulation=False):
  c=self.c.copy();c['market_day_accepted']=True
  if simulation:
   c['DRY_RUN']=True;self.save('DRY_RUN.json',{'DRY_RUN':True,'production_authorization':False})
  self.save('config/yfinance.json',c)
 def test_stale_numeric_checkpoint_missing_future_and_cached_report(self):
  for state in [{'usable':False,'rows':104,'reason':'YFINANCE_CHECKPOINT_EXPIRED'}, {'usable':False}, {'usable':True,'rows':23}]:
   with patch.object(y,'status',return_value=state):
    report=y.make_report('opening',NOW,self.r)
   self.assertEqual(report['status'],'DATA_DELAYED');self.assertEqual(report['strategy']['status'],'BLOCKED')
  for stamp in [None,'invalid','2026-10-28T00:00:00+08:00']:
   rows=self.rows();rows[0]['bar_timestamp']=stamp
   self.assertEqual(y.assess(rows,NOW,self.r)['quality'],'DEGRADED')
 def test_status_expiry_and_cached_future_bar_fail_closed(self):
  c=self.c
  state={'usable':True,'rows':104,'last_success':(NOW-base.dt.timedelta(hours=2)).isoformat()}
  with patch.object(y,'research',return_value={'source_state':lambda *a:state}):
   result=y.status(NOW,self.r)
  self.assertFalse(result['usable']);self.assertEqual(result['status'],'DATA_STALE');self.assertEqual(result['rows'],[])
  body=json.dumps(self.payload('2026-10-28T00:00:00+08:00')).encode();sha=hashlib.sha256(body).hexdigest()
  (self.r/'state/research-responses').mkdir();(self.r/'state/research-responses'/(sha+'.body')).write_bytes(body)
  self.save('state/yfinance-fetch.json',{'sha256':sha,'fetched_at':NOW.isoformat()})
  with patch.object(base.subprocess,'run',side_effect=AssertionError('no network')):
   with self.assertRaises(y.DataError):y.fetch(self.r,NOW,c)
 def test_strategy_executes_source_admission_with_real_lineage(self):
  with patch.object(y,'status',return_value=self.source()):report=y.make_report('opening',NOW,self.r)
  s=report['strategy'];self.assertEqual(s['status'],'LIMITED');self.assertEqual(s['admission'],'BLOCKED')
  self.assertIsNone(s['macro_score']);self.assertEqual(s['batch_id'],'a'*64)
  self.assertEqual(len(s['observations']),17);self.assertAlmostEqual(s['observations'][0]['open_to_close_pct'],1)
 def test_simulation_cannot_target_installation_or_missing_marker(self):
  self.enable();before=list((self.r/'state').iterdir())
  self.assertEqual(y.simulate_publish('slot',NOW,self.r)['result'],'DEGRADED')
  self.enable(True)
  with patch.object(y,'ROOT',self.r):self.assertEqual(y.simulate_publish('slot',NOW,self.r)['result'],'DEGRADED')
  self.assertEqual(before,list((self.r/'state').iterdir()))
 def test_production_rejects_isolated_even_with_enabled_flags(self):
  self.enable(True)
  with patch.object(y,'status',return_value=self.source()):
   self.assertEqual(y.write_report('opening','slot',NOW,self.r)['result'],'PASS')
   self.assertEqual(y.publish('slot',NOW,self.r)['reason'],'PUBLICATION_MODE_OR_ROOT_REFUSED')
   self.assertEqual(y.simulate_publish('slot',NOW,self.r)['result'],'PASS')
 def test_production_provenance_and_adversarial_metadata_path(self):
  self.enable();original=runpy.run_path
  def routed(path,*a,**kw):
   if str(path)==str(self.r/'src/fie_t2.py'):return {'gate':lambda root:None}
   return original(path,*a,**kw)
  # Simulated production installation exists only in this temporary fixture.
  with patch.object(y,'ROOT',self.r),patch.object(y,'status',return_value=self.source()),patch.object(runpy,'run_path',side_effect=routed):
   result=y.write_report('opening','slot',NOW,self.r);self.assertEqual(result['result'],'PASS')
   self.assertEqual(y.publish('slot',NOW,self.r)['result'],'PASS')
   pointer=self.r/'state/local-publication.json';before=pointer.read_bytes()
   p=self.r/'state/yfinance-report-opening.json';ptr=json.loads(p.read_text());report_path=self.r/ptr['artifacts'][0]['path'];good=json.loads(report_path.read_text())
   for key,value in [('DRY_RUN',True),('fixture_only',True),('test',True),('mode','TEST'),('provenance',{**good['provenance'],'batch_id':'c'*64}),('applicable_date','2026-10-22'),('rows',[]),('strategy',{})]:
    report=copy.deepcopy(good);report[key]=value
    report_path.write_text(json.dumps(report));md=self.r/ptr['artifacts'][1]['path'];md.write_bytes(y.render_report('opening',report))
    for a in ptr['artifacts']:a['sha256']=hashlib.sha256((self.r/a['path']).read_bytes()).hexdigest()
    self.save(str(p.relative_to(self.r)),ptr)
    self.assertEqual(y.publish('slot',NOW,self.r)['result'],'DEGRADED',key)
    self.assertEqual(pointer.read_bytes(),before)
   report_path.write_text(json.dumps(good));md.write_bytes(y.render_report('opening',good))
   for a in ptr['artifacts']:a['sha256']=hashlib.sha256((self.r/a['path']).read_bytes()).hexdigest()
   saved=copy.deepcopy(ptr)
   for path in ['reports/../secrets/token','/etc/passwd','reports/sub/t2-yfinance-'+('a'*20)+'.json']:
    ptr=copy.deepcopy(saved);ptr['artifacts'][0]['path']=path;self.save('state/yfinance-report-opening.json',ptr)
    self.assertEqual(y.publish('slot',NOW,self.r)['result'],'DEGRADED');self.assertEqual(pointer.read_bytes(),before)
   self.save('state/yfinance-report-opening.json',saved)
   self.assertEqual(y.publish('DRY_RUN_disguised',NOW,self.r)['result'],'DEGRADED')
   # A symlink to an otherwise valid artifact must also fail.
   copyfile=self.r/'original.json';report_path.rename(copyfile);report_path.symlink_to(copyfile)
   self.assertEqual(y.publish('slot',NOW,self.r)['result'],'DEGRADED');self.assertEqual(pointer.read_bytes(),before)
 def test_hermes_pinned_namespace_and_all_denials(self):
  spec=importlib.util.spec_from_file_location('dryread',Path(__file__).resolve().parents[1]/'bin/read_dry_run.py');reader=importlib.util.module_from_spec(spec);spec.loader.exec_module(reader)
  folder=self.r/'evidence/run/DRY_RUN_fixture';(folder/'reports').mkdir(parents=True)
  name='reports/t2-yfinance-'+('a'*20)+'.json';artifact=folder/name
  report={'DRY_RUN':True,'provenance':{'mode':'DRY_RUN'},'strategy':{'status':'LIMITED'}};artifact.write_text(json.dumps(report));artifact.chmod(0o600)
  entry={'root':str(folder),'path':name,'sha256':hashlib.sha256(artifact.read_bytes()).hexdigest()}
  registry={'evidence_root':str(self.r/'evidence'),'namespaces':{'rehearsal':{'artifacts':{'opening':entry}}}}
  p=self.r/'registry.json';self.save('registry.json',registry)
  self.assertTrue(reader.read(['rehearsal','opening'],p)['DRY_RUN'])
  for args in [['../secrets','opening'],['rehearsal','/etc/passwd'],['production','latest'],['rehearsal','opening','--root'],[]]:
   with self.assertRaises((KeyError,ValueError)):reader.read(args,p)
  for path in ['../secrets/token','reports/../../secret','/etc/passwd','state/local-publication.json']:
   entry['path']=path;self.save('registry.json',registry)
   with self.assertRaises(ValueError):reader.read(['rehearsal','opening'],p)
  entry['path']=name;self.save('registry.json',registry)
  original=artifact.read_bytes();artifact.write_text('{}')
  with self.assertRaises(ValueError):reader.read(['rehearsal','opening'],p)
  artifact.write_bytes(original);outside=self.r/'secret';outside.write_bytes(original);outside.chmod(0o600);artifact.unlink();artifact.symlink_to(outside)
  with self.assertRaises(ValueError):reader.read(['rehearsal','opening'],p)
  artifact.unlink();artifact.write_bytes(original);artifact.chmod(0o644)
  with self.assertRaises(ValueError):reader.read(['rehearsal','opening'],p)
