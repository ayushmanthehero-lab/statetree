"""Private credential preparation shared by the cloud chat and local worker."""

import json
import os
from pathlib import Path
import re
import secrets


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SECRET_PATTERN = re.compile(r'[A-Za-z0-9_-]{32,128}\Z')


def _write_private(path: Path, text: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as target:
        target.write(text)


def prepare(root: Path = PROJECT_ROOT) -> dict:
    """Create or validate the private demo credential file without rotating keys."""
    private = Path(root).resolve() / 'build/hybrid/private'
    private.mkdir(parents=True, mode=0o700, exist_ok=True)
    secret_file = private / 'secret.json'
    created = False
    if secret_file.exists():
        try:
            values = json.loads(secret_file.read_text(encoding='utf-8'))
            if not isinstance(values, dict) or not isinstance(values.get('demo_access_code'), str) or not SECRET_PATTERN.fullmatch(values['demo_access_code']):
                raise ValueError
        except (ValueError, UnicodeError):
            raise ValueError('The existing secret.json is invalid; it was not replaced.') from None
    else:
        values = {'demo_access_code': secrets.token_urlsafe(32)}
        _write_private(secret_file, json.dumps(values, indent=2) + '\n')
        created = True
    return {'secret_file': str(secret_file), 'created': created}