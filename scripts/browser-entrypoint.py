#!/usr/bin/env python3
"""Drop privilege, repairing legacy root-created files in the existing mount."""
import os
from pathlib import Path
import sys

if os.geteuid() == 0:
    root = Path('/config')
    root.mkdir(exist_ok=True)
    info = root.stat()
    uid, gid = (info.st_uid, info.st_gid) if info.st_uid else (1000, 1000)
    # Old images ran as root: retain the mount owner's identity and make its
    # SQLite/image files writable to that same nonroot runtime identity.
    for folder, dirs, files in os.walk(root, followlinks=False):
        for path in [Path(folder), *(Path(folder)/name for name in dirs+files)]:
            if not path.is_symlink() and path.stat().st_uid == 0:
                os.chown(path, uid, gid)
    home = Path(f'/tmp/agora-browser-{uid}')
    home.mkdir(mode=0o700, exist_ok=True)
    os.chown(home, uid, gid)
    os.environ['HOME'] = str(home)
    # The UID has no /etc/passwd entry; asyncssh reads the local user name from the environment instead.
    os.environ['USER'] = os.environ['LOGNAME'] = 'agora'
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)
if not sys.argv[1:]:
    raise SystemExit('Missing hub command')
os.execvp(sys.argv[1], sys.argv[1:])
