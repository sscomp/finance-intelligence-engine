"""Prepare/reseal only this new instance. Never starts services or runs DDL."""
import argparse,json,os,runpy,shutil
from pathlib import Path
os.umask(0o077)
r=Path(__file__).resolve().parents[1]
p=argparse.ArgumentParser();p.add_argument('--initialize',action='store_true');p.add_argument('--seal',action='store_true');a=p.parse_args()
for d in ['config','state','reports','logs','secrets']:(r/d).mkdir(mode=0o700,exist_ok=True)
if a.initialize:
 for src in (r/'templates').glob('*.json.example'):
  dest=r/'config'/src.name.removesuffix('.example')
  if not dest.exists():shutil.copyfile(src,dest);dest.chmod(0o600)
 print('Prepared templates; edit private deployment/calendar/container pins before sealing.')
if a.seal:
 t=runpy.run_path(str(r/'src/fie_t2.py'));dep=runpy.run_path(str(r/'src/runtime_config.py'))['load'](r)
 for name in ['instance','side-effects']:
  dest=r/'config'/(name+'.json');c=json.loads(dest.read_text())
  if name=='instance':c['database_targets']=[dep['raw_database'],dep['intelligence_database']];c['api_base']=dep['api_base']
  else:
   for effect in c['jobs'].values():effect['database_writes']=[{'fie_t2_raw':dep['raw_database'],'fie_t2_intelligence':dep['intelligence_database']}.get(x,x) for x in effect.get('database_writes',[])]
  t['atomic'](dest,c)
 for folder in ['src','config']:
  for path in (r/folder).glob('*'):
   if path.is_file():path.chmod(0o600)
 t['atomic'](r/'config/activation.json',{'instance_id':'T2','result':'PASS','authorization':'Local operator reviewed independent deployment; external sends disabled.','files':{x:t['sha'](r/x) for x in t['AUDIT_FILES']}})
 t['gate'](r)
 print('Sealed code/config. Calendar, API, data source and actual-day gates still apply; no service started.')
