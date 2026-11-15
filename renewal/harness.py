"""One bounded harness invocation with durable pre-call accounting. No model defaults."""
import argparse
import json
import os
import signal
import subprocess
import time
from pathlib import Path

from .experiment import Ledger


def execute(command, *, cwd, prompt, metadata, ledger, output, timeout=90):
    output=Path(output)
    output.mkdir(parents=True,exist_ok=True)
    key=ledger.begin(dict(metadata,command_executable=Path(command[0]).name,retries=0))
    started=time.monotonic()
    stdout=stderr=''
    row={'status':'failed','usage':{},'accepted':False,'actual_charge_usd':None,
         'estimated_usd':None,'pricing_source':None,'pricing_date':None}
    try:
        process=subprocess.Popen(command,cwd=cwd,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE,text=True,start_new_session=True)
        try:
            stdout,stderr=process.communicate(prompt,timeout=timeout)
            row.update(status='completed' if process.returncode==0 else 'failed',returncode=process.returncode)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid,signal.SIGTERM)
            try:
                stdout,stderr=process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid,signal.SIGKILL)
                stdout,stderr=process.communicate()
            row.update(status='timeout',returncode=process.returncode)
    except OSError as error:
        row.update(status='blocked',error_type=type(error).__name__)
    # Raw output is private evidence. Do not copy this directory into public releases.
    for suffix,value in [('stdout',stdout),('stderr',stderr)]:
        path=output/(key+'.'+suffix)
        path.write_text(value)
        path.chmod(0o600)
    for line in stdout.splitlines():
        try:
            event=json.loads(line)
        except ValueError:
            continue
        if not isinstance(event,dict):
            continue
        usage=event.get('usage') or event.get('modelUsage')
        if event.get('type')=='turn.completed' and isinstance(usage,dict):
            row['usage']=usage
        elif event.get('type')=='result' and isinstance(usage,dict):
            row['usage']=usage
            row['provider_reported_cost_usd']=event.get('total_cost_usd')
            models = event.get('modelUsage', {})
            if isinstance(models, dict) and len(models) == 1:
                row['observed_model'] = next(iter(models))
            elif isinstance(models, dict) and models:
                row['observed_models'] = sorted(models)
        if event.get('model'):
            row['observed_model']=event['model']
    row['elapsed_seconds']=time.monotonic()-started
    ledger.finish(key,**row)
    return dict(row,id=key)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--command-json',required=True,help='A JSON argv list. No shell expansion.')
    parser.add_argument('--cwd',type=Path,required=True)
    parser.add_argument('--prompt-file',type=Path,required=True)
    parser.add_argument('--metadata-file',type=Path,required=True)
    parser.add_argument('--ledger',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--timeout',type=float,default=90)
    args=parser.parse_args()
    command=json.loads(args.command_json)
    if not isinstance(command,list) or not command or not all(isinstance(x,str) for x in command):
        parser.error('command-json must contain a nonempty argv list')
    row=execute(command,cwd=args.cwd,prompt=args.prompt_file.read_text(),
                metadata=json.loads(args.metadata_file.read_text()),ledger=Ledger(args.ledger),output=args.output,timeout=args.timeout)
    print(json.dumps(row))
    raise SystemExit(0 if row['status']=='completed' else 1)


if __name__=='__main__':
    main()
