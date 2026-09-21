import hashlib
import importlib.util
import json
from pathlib import Path
import zipfile
import pytest


def installer():
    path=Path(__file__).resolve().parents[1]/'scripts/apply_agentic_update.py'
    assert path.exists(),'Safe source-only updater is missing'
    spec=importlib.util.spec_from_file_location('agentic_installer',path)
    m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def h(b): return hashlib.sha256(b).hexdigest()


def make_zip(tmp_path,files,extra=None):
    path=tmp_path/'update.zip'; rows=[]
    with zipfile.ZipFile(path,'w') as z:
        for name,before,after in files:
            rows.append({'path':name,'sha256':h(after),'before_sha256':[] if before is None else [h(before)],'size':len(after)})
            z.writestr('payload/'+name,after)
        z.writestr('UPDATE_MANIFEST.json',json.dumps({'format':'statetree-agentic-update-v1','files':rows}))
        if extra: z.writestr(extra,b'bad')
    return path


def target(tmp_path):
    repo=tmp_path/'project'; (repo/'statetree').mkdir(parents=True)
    (repo/'pyproject.toml').write_text('[project]\nname="statetree"\n')
    (repo/'statetree/project.py').write_text('# old')
    return repo


def test_source_update_preserves_user_state_and_creates_backup(tmp_path):
    m=installer(); repo=target(tmp_path)
    (repo/'.statetree').mkdir(); (repo/'.statetree/KEEP').write_text('memory')
    (repo/'.venv').mkdir(); (repo/'.venv/KEEP').write_text('python')
    z=make_zip(tmp_path,[('statetree/project.py',b'# old',b'# updated'),('statetree/agentic/new.py',None,b'# new')])
    value=m.apply_update(repo,z,backup_root=tmp_path/'backups')
    assert (repo/'statetree/project.py').read_bytes()==b'# updated'
    assert (repo/'.statetree/KEEP').read_text()=='memory'
    assert (repo/'.venv/KEEP').read_text()=='python'
    with zipfile.ZipFile(value['backup']) as backup: assert backup.read('statetree/project.py')==b'# old'
    assert m.apply_update(repo,z,backup_root=tmp_path/'backups')['changed']==0


def test_modified_source_refused_before_any_write(tmp_path):
    m=installer(); repo=target(tmp_path); (repo/'statetree/project.py').write_text('# my edits')
    z=make_zip(tmp_path,[('statetree/agentic/new.py',None,b'# new'),('statetree/project.py',b'# old',b'# replacement')])
    with pytest.raises(ValueError,match='modified'): m.apply_update(repo,z,backup_root=tmp_path/'backups')
    assert not (repo/'statetree/agentic/new.py').exists()
    assert (repo/'statetree/project.py').read_text()=='# my edits'


@pytest.mark.parametrize('name',['../escape','.statetree/state.json','.venv/python','.git/config','statetree/../escape'])
def test_zip_does_not_touch_private_or_escaping_paths(tmp_path,name):
    m=installer(); repo=target(tmp_path)
    z=make_zip(tmp_path,[(name,None,b'bad')])
    with pytest.raises(ValueError): m.apply_update(repo,z,backup_root=tmp_path/'backups')


def test_payload_tampering_refused(tmp_path):
    m=installer(); repo=target(tmp_path)
    z=make_zip(tmp_path,[('statetree/project.py',b'# old',b'# updated')])
    raw=zipfile.ZipFile(z).read('UPDATE_MANIFEST.json')
    with zipfile.ZipFile(z,'w') as out:
        out.writestr('UPDATE_MANIFEST.json',raw); out.writestr('payload/statetree/project.py',b'TAMPERED!')
    with pytest.raises(ValueError,match='Checksum'): m.apply_update(repo,z,backup_root=tmp_path/'backups')
