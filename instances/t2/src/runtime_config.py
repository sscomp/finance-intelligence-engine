"""Private deployment pins; no credentials are read or logged here."""
import json,os,re,stat
from pathlib import Path

def load(root):
 p=root/'config/deployment.json';s=p.lstat()
 if not stat.S_ISREG(s.st_mode) or s.st_uid!=os.getuid() or s.st_mode&0o077:raise RuntimeError('PRIVATE_DEPLOYMENT_CONFIG_REQUIRED')
 c=json.loads(p.read_text())
 for name in ['db_container','app_container','raw_database','intelligence_database','admin_role','writer_role']:
  if not re.fullmatch('[a-zA-Z_][a-zA-Z_0-9-]{0,62}',c[name]):raise RuntimeError('INVALID_DEPLOYMENT_IDENTIFIER')
 if not re.fullmatch('[0-9]{1,20}',c['cluster']):raise RuntimeError('INVALID_CLUSTER_PIN')
 if not re.fullmatch('http://127[.]0[.]0[.]1:[0-9]{2,5}',c['api_base']):raise RuntimeError('LOOPBACK_API_REQUIRED')
 if not Path(c['docker']).is_absolute():raise RuntimeError('ABSOLUTE_DOCKER_PATH_REQUIRED')
 return c
