"""Persist protected eval outcomes, including failure and interrupted starts."""
import argparse
import datetime as dt
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

from .controls import atomic_write
from .experiment import validate_manifest


def run(output, package=None):
    package=Path(package or Path(__file__).resolve().parents[1])
    output=Path(output)
    output.mkdir(parents=True,exist_ok=True)
    key=uuid.uuid4().hex
    path=output/(key+'.json')
    result={'id':key,'status':'running','started_at':dt.datetime.now(dt.timezone.utc).isoformat(),
            'synthetic':True,'actual_charge_usd':None,'paid_model_calls':0,'cases':[]}
    atomic_write(path,json.dumps(result).encode())
    started=time.monotonic()
    try:
        validate_manifest(package/'evals',package/'evals/freeze.json')
        # Each subprocess receives synthetic fixtures only; never loads sessions.
        for name in sorted((package/'tests').glob('test_*.py')):
            command=[sys.executable,'-m','unittest','discover','-s','tests','-p',name.name,'-v']
            attempt={'suite':name.stem,'status':'running'}
            result['cases'].append(attempt)
            atomic_write(path,json.dumps(result,indent=2).encode())
            proc=subprocess.run(command,cwd=package,capture_output=True,text=True,timeout=120)
            (output/(key+'-'+name.stem+'.log')).write_text(proc.stdout+proc.stderr)
            attempt.update(status='passed' if proc.returncode==0 else 'failed',returncode=proc.returncode)
            atomic_write(path,json.dumps(result,indent=2).encode())
        expected={c['test'] for c in json.loads((package/'evals/heldout.json').read_text())['cases'] if 'test' in c}
        actual={c['suite'] for c in result['cases']}
        if not expected.issubset(actual):
            raise ValueError('Required suites missing: '+str(sorted(expected-actual)))
        result['status']='passed' if all(c['status']=='passed' for c in result['cases']) else 'failed'
    except Exception as error:
        result.update(status='failed',error_type=type(error).__name__,error=str(error))
    result['elapsed_seconds']=time.monotonic()-started
    atomic_write(path,json.dumps(result,indent=2).encode())
    return result,path


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    result,path=run(args.output)
    print(json.dumps({'status':result['status'],'suites':len(result['cases']),'result':str(path)}))
    raise SystemExit(0 if result['status']=='passed' else 1)


if __name__=='__main__':
    main()
