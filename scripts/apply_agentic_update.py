"""Apply the source-only Agent tasks update without touching .statetree/.venv.

Stop StateTree first. Refuses unknown local source edits rather than overwrite
someone's work. Backups contain only the source files replaced by this update.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import tempfile
from uuid import uuid4
import zipfile


def checksum(value):
    return hashlib.sha256(value).hexdigest()


def safe_relative(value):
    if type(value) is not str or '\\' in value or ':' in value:
        raise ValueError('Invalid update path')
    p=PurePosixPath(value)
    if p.is_absolute() or '..' in p.parts or not p.parts or str(p)!=value:
        raise ValueError('Unsafe update path')
    if any(part.lower() in ('.git','.statetree','.venv','__pycache__') for part in p.parts):
        raise ValueError('Update must not touch private state')
    if p.parts[0] not in ('statetree','scripts','docs','tests') and value not in ('README.md',):
        raise ValueError('Unexpected source path')
    return p


def reject_link(path):
    for item in (path,*path.parents):
        if item.is_symlink() or (hasattr(item,'is_junction') and item.is_junction()):
            raise ValueError('Refusing a symlink/junction source path: '+str(item))


def apply_update(project,archive,*,backup_root=None):
    repo=Path(project).absolute(); reject_link(repo)
    if not (repo/'pyproject.toml').is_file() or not (repo/'statetree/project.py').is_file():
        raise ValueError('Choose the extracted StateTree project folder containing pyproject.toml')
    changes=[]
    with zipfile.ZipFile(archive) as z:
        infos=z.infolist()
        if len(infos)>200 or sum(i.file_size for i in infos)>30*1024*1024:
            raise ValueError('Update ZIP is unexpectedly large')
        names=[i.filename for i in infos]
        if len(set(names))!=len(names): raise ValueError('Duplicate ZIP entries')
        manifest=json.loads(z.read('UPDATE_MANIFEST.json'))
        if manifest.get('format')!='statetree-agentic-update-v1' or type(manifest.get('files')) is not list:
            raise ValueError('Unsupported update manifest')
        expected={'UPDATE_MANIFEST.json'}
        for row in manifest['files']:
            relative=safe_relative(row['path']); member='payload/'+str(relative)
            if member in expected: raise ValueError('Duplicate manifest path')
            expected.add(member); data=z.read(member)
            if len(data)!=row['size'] or checksum(data)!=row['sha256']:
                raise ValueError('Checksum mismatch: '+str(relative))
            dest=repo.joinpath(*relative.parts); reject_link(dest)
            if dest.exists() and not dest.is_file(): raise ValueError('Source destination is not a regular file')
            current=dest.read_bytes() if dest.exists() else None
            if current is not None and checksum(current)==row['sha256']: continue
            accepted=row.get('before_sha256',[])
            if current is not None and checksum(current) not in accepted:
                raise ValueError('Locally modified source detected; nothing was changed: '+str(relative))
            if current is None and accepted:
                raise ValueError('Expected source file is missing: '+str(relative))
            changes.append((str(relative),dest,current,data))
        if set(names)!=expected: raise ValueError('Unexpected ZIP entries')
    if not changes: return {'changed':0,'backup':None,'message':'This update is already installed.'}
    if backup_root is None:
        cache=Path(os.environ.get('LOCALAPPDATA',str(Path.home()/'.cache')))
        backup_root=cache/'StateTree'/'source-update-backups'
    backup_root=Path(backup_root).absolute(); reject_link(backup_root)
    try: backup_root.relative_to(repo)
    except ValueError: pass
    else: raise ValueError('Backups must be outside the project and its checkpoint snapshots')
    backup_root.mkdir(parents=True,exist_ok=True)
    backup=backup_root/('agentic-'+datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')+'-'+uuid4().hex[:8]+'.zip')
    with zipfile.ZipFile(backup,'w',zipfile.ZIP_DEFLATED) as out:
        for name,dest,current,data in changes:
            if current is not None: out.writestr(name,current)
        out.writestr('UPDATE_RECEIPT.json',json.dumps({'project':str(repo),'new_files':[n for n,d,c,b in changes if c is None]},indent=2))
    applied=[]
    try:
        for name,dest,current,data in changes:
            dest.parent.mkdir(parents=True,exist_ok=True)
            fd,tmp=tempfile.mkstemp(prefix='.statetree-update-',dir=dest.parent)
            try:
                with os.fdopen(fd,'wb') as f: f.write(data); f.flush(); os.fsync(f.fileno())
                os.replace(tmp,dest)
            finally:
                if os.path.exists(tmp): os.unlink(tmp)
            applied.append((dest,current))
    except Exception:
        # Best-effort rollback of changed source, never user runtime directories.
        for dest,current in reversed(applied):
            try:
                if current is None: dest.unlink(missing_ok=True)
                else: dest.write_bytes(current)
            except OSError: pass
        raise RuntimeError('Update interrupted. Original source backup: '+str(backup))
    return {'changed':len(changes),'backup':str(backup),'message':'Agent tasks update installed. Runtime state, virtual environment and model cache were not changed.'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project',required=True,type=Path)
    parser.add_argument('--zip',required=True,type=Path,dest='archive')
    args=parser.parse_args()
    try:
        result=apply_update(args.project,args.archive)
        print(result['message'])
        print('Source files updated:',result['changed'])
        if result['backup']: print('Source backup:',result['backup'])
        return 0
    except (OSError,ValueError,KeyError,zipfile.BadZipFile,RuntimeError) as error:
        print('Update stopped:',error)
        return 1


if __name__=='__main__': raise SystemExit(main())
