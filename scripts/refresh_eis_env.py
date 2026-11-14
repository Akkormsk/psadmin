"""Read the new key from encrypted SSH stdin, never from arguments or logs."""
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path

key = sys.stdin.read().strip()
uuid.UUID(key)
paths = [Path('/home/deploy/psadmin/deploy/vds/.env'), Path('/home/deploy/psadmin-test.env')]
if not all(path.is_file() for path in paths):
    raise SystemExit('Expected runtime env files are missing; no file changed')
for path in paths:
    lines = [line for line in path.read_text().splitlines()
             if not line.lstrip().startswith(('EIS_TOKEN=', 'export EIS_TOKEN='))]
    lines.append('EIS_TOKEN=' + json.dumps(key))
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
        temporary = stream.name
        os.chmod(temporary, 0o600)
        stream.write('\n'.join(lines) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
print('EIS runtime key updated in PROD and TEST; value suppressed')
