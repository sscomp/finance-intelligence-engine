"""Read pinned rehearsal reports only. No runtime, credentials or provider access.

Operator installs a private registry beside this helper. CLI accepts exactly a
namespace and alias, never a filesystem path. Registry changes require review.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys


def private_bytes(path,limit):
    path=Path(path)
    if path.resolve(strict=True)!=path:raise ValueError('SYMLINK_OR_NONCANONICAL_PATH')
    with os.fdopen(os.open(path,os.O_RDONLY|os.O_NOFOLLOW),'rb') as f:
        info=os.fstat(f.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o600:raise ValueError('PRIVATE_FILE_REQUIRED')
        data=f.read(limit+1)
    if len(data)>limit:raise ValueError('ARTIFACT_TOO_LARGE')
    return data


def read(args,registry_path):
    if len(args)!=2 or not all(re.fullmatch(r'[A-Za-z0-9_-]{1,80}',x) for x in args):raise ValueError('NAMESPACE_AND_ALIAS_REQUIRED')
    registry=json.loads(private_bytes(registry_path,65536))
    entry=registry['namespaces'][args[0]]['artifacts'][args[1]]
    base=Path(registry['evidence_root'])
    folder=Path(entry['root']);path=folder/entry['path']
    # Only direct immutable reports, under a DRY_RUN namespace in evidence.
    if base.name!='evidence' or folder.name!='DRY_RUN_fixture' or base not in folder.parents:raise ValueError('EVIDENCE_ROOT_REQUIRED')
    if not re.fullmatch(r'reports/t2-yfinance-[a-f0-9]{20}\.json',entry['path']):raise ValueError('REPORT_PATH_REFUSED')
    content=private_bytes(path,1000000)
    if hashlib.sha256(content).hexdigest()!=entry['sha256']:raise ValueError('ARTIFACT_HASH_MISMATCH')
    report=json.loads(content)
    if report.get('DRY_RUN') is not True or report.get('provenance',{}).get('mode')!='DRY_RUN':raise ValueError('DRY_RUN_LABEL_REQUIRED')
    return {'status':'ok','DRY_RUN':True,'namespace':args[0],'alias':args[1],
            'production_report':False,'source':str(path),'sha256':entry['sha256'],'report':report}


def main(args,registry_path=None):
    try:
        value=read(args,registry_path or Path(__file__).with_name('dry-run-allowlist.json'))
        print(json.dumps(value,ensure_ascii=False));return 0
    except (OSError,ValueError,KeyError,TypeError):
        print(json.dumps({'status':'error','reason':'DRY_RUN_READ_REFUSED'}));return 2


if __name__=='__main__':
    os.umask(0o077)
    raise SystemExit(main(sys.argv[1:]))
