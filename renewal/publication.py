"""Conservative public-tree inspection. Always also inspect fresh git history."""
import argparse
import json
import re
import subprocess
from pathlib import Path


def content_findings(text, label):
    # Build patterns in pieces so the scanner does not match its own fixtures.
    patterns=[('private local path',re.compile('/'+'Users'+r'/[^\s/]+/')),
              ('email address',re.compile(r'[\w.+-]+'+'@'+r'[\w.-]+\.[A-Za-z]{2,}')),
              ('credential-shaped value',re.compile(r'(?:'+'sk'+r'-[A-Za-z0-9_-]{24,}|'+'ghp'+r'_[A-Za-z0-9]{20,}|'+'github_pat'+r'_[A-Za-z0-9_]{20,})'))]
    return [{'path':label,'reason':reason} for reason,pattern in patterns if pattern.search(text)]


def scan(root):
    root=Path(root).resolve()
    findings=[]
    for path in root.rglob('*'):
        rel=path.relative_to(root)
        if any(p in {'.git','__pycache__'} for p in rel.parts):
            continue
        if path.is_symlink():
            if not path.exists() or not path.resolve().is_relative_to(root):
                findings.append({'path':str(rel),'reason':'external or broken symlink'})
            continue
        if not path.is_file():
            continue
        if path.name in {'.env','private-integration.json','baseline-private.json','calls.jsonl'} or any(p in {'state','raw','quarantine','archives'} for p in rel.parts):
            findings.append({'path':str(rel),'reason':'private operational artifact'})
        try:
            text=path.read_text()
        except (UnicodeError,OSError):
            findings.append({'path':str(rel),'reason':'binary requires explicit review'})
            continue
        findings.extend(content_findings(text,str(rel)))
    return findings


def scan_history(root):
    """Inspect every reachable revision in a focused repo, including deleted files."""
    root=Path(root).resolve()
    if not root.is_dir():
        raise ValueError('Focused repository root required')
    def git(*args):
        return subprocess.run(['git',*args],cwd=root,capture_output=True,check=True).stdout
    if Path(git('rev-parse','--show-toplevel').decode().strip()).resolve() != root:
        raise ValueError('Refusing to inspect a parent workspace repository')
    findings=[]
    for revision in git('rev-list','--all').decode().splitlines():
        findings.extend(content_findings(git('show','-s','--format=%B',revision).decode(),revision+':message'))
        entries=[]
        for item in git('ls-tree','-rz','--full-tree',revision).split(b'\x00'):
            if item:
                meta,name=item.split(b'\t',1)
                mode,kind,key=meta.decode().split()
                entries.append((mode,kind,key,name.decode()))
        names={item[3] for item in entries}
        for mode,kind,key,name in entries:
            label=revision[:12]+':'+name
            if kind != 'blob':
                findings.append({'path':label,'reason':'embedded repository requires explicit review'})
                continue
            raw=git('cat-file','blob',key)
            try:
                text=raw.decode()
            except UnicodeError:
                findings.append({'path':label,'reason':'binary requires explicit review'})
                continue
            findings.extend(content_findings(text,label))
            if mode=='120000':
                import posixpath
                target=posixpath.normpath(posixpath.join(posixpath.dirname(name),text))
                # Directories are valid targets if any versioned child exists.
                if text.startswith('/') or target.startswith('../') or not any(p==target or p.startswith(target+'/') for p in names):
                    findings.append({'path':label,'reason':'external or broken historical symlink'})
    return findings


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('root',type=Path)
    parser.add_argument('--history',action='store_true')
    args=parser.parse_args()
    findings=scan(args.root)
    if args.history:
        findings.extend(scan_history(args.root))
    print(json.dumps({'status':'passed' if not findings else 'failed','findings':findings},indent=2))
    raise SystemExit(bool(findings))


if __name__=='__main__':
    main()
