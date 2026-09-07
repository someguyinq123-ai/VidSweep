"""Cache-corruption recovery: a malformed library.db must not kill a scan.

The L:-drive bridge intermittently returns bad pages, surfacing as
sqlite3.DatabaseError ('database disk image is malformed') mid-scan and
killing hours-long scans. The cache is disposable, so:
  1. Corruption mid-scan      -> rebuild the cache and restart the scan
                                 automatically (fresh session, same roots).
  2. Corruption persists      -> relocate the cache off the failing drive
                                 (%LOCALAPPDATA%\\VidSweep), remember the
                                 location; only if even that fails, a
                                 clear error (never raw sqlite text).
  3. Corrupt file at launch   -> __init__ recreates (or relocates) instead
                                 of crashing.
  4. Drive mangles everything -> relocation happens mid-scan and the scan
                                 completes on the internal drive.
"""
import os
import sys
import shutil
import sqlite3
import contextlib
import subprocess
import tempfile
import time
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core

FF = core.find_ffmpeg() or 'ffmpeg'

tmp = tempfile.mkdtemp(prefix='vs_corrupt_')
a = os.path.join(tmp, 'a')
os.makedirs(a)
print('building 6 small test videos...', flush=True)
for gi in range(6):
    v = os.path.join(a, f'v{gi}.mp4')
    subprocess.run([FF, '-y', '-v', 'error',
                    '-f', 'lavfi', '-i', f'testsrc2=s=320x180:r=24:d={3+gi}',
                    '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt',
                    'yuv420p', v], check=True, capture_output=True)

db = os.path.join(tmp, 't.db')
# relocation targets: patched so the test never touches the real
# %LOCALAPPDATA% or the project dir
fake_local = os.path.join(tmp, 'localappdata')
fake_marker = os.path.join(tmp, 'db_location.txt')


def relocation_patches():
    return (mock.patch.dict(os.environ, {'LOCALAPPDATA': fake_local}),
            mock.patch.object(core.VideoOrganizer, '_marker_path',
                              staticmethod(lambda: fake_marker)))

# --- populate a healthy cache first (multi-page table incl. thumbnails)
org = core.VideoOrganizer(db_path=db)
stats0 = org.scan([a])
assert stats0['errors'] == 0, stats0
org.close()
print(f'healthy scan done: {stats0["processed"]} processed', flush=True)

# --- 1. corruption appears MID-SCAN -> automatic rebuild + restart
org2 = core.VideoOrganizer(db_path=db)
real_conn = org2.db


class CorruptConn:
    """Proxy that raises the drive's bad-page error at a real write inside
    the scan (the total update); everything before it works normally."""

    def __init__(self, real):
        self._real = real

    def execute(self, sql, *args, **kw):
        if 'SET total' in sql:
            raise sqlite3.DatabaseError('database disk image is malformed')
        return self._real.execute(sql, *args, **kw)

    def __getattr__(self, name):
        return getattr(self._real, name)


org2.db = CorruptConn(real_conn)
progress_notes = []
stats = org2.scan([a], progress=lambda ph, d, t, cur: progress_notes.append(cur))
assert stats['errors'] == 0, stats
assert stats['processed'] == 6, f'restart should re-fingerprint all: {stats}'
assert stats['skipped_cached'] == 0, stats
assert any('corrupt' in str(c) for c in progress_notes), progress_notes[-3:]
sess = org2.get_last_session()
assert sess and not sess['active'], f'restarted session should complete: {sess}'
groups = org2.find_duplicates()
assert isinstance(groups, list)
print('MID-SCAN CORRUPTION: rebuilt and restarted automatically, '
      f"{stats['processed']} re-fingerprinted", flush=True)
org2.db.close()

# --- 2. corruption persists -> cache relocated, then a clear failure
org3 = core.VideoOrganizer(db_path=db)


def always_corrupt(*args, **kw):
    raise sqlite3.DatabaseError('database disk image is malformed')


org3._scan_impl = always_corrupt
with contextlib.ExitStack() as stack:
    for p in relocation_patches():
        stack.enter_context(p)
    try:
        org3.scan([a])
        raise AssertionError('expected RuntimeError for persistent corruption')
    except RuntimeError as e:
        assert 'corrupted' in str(e) and 'chkdsk' in str(e), e
        assert org3.db_path.startswith(fake_local), org3.db_path
        print('PERSISTENT CORRUPTION: cache relocated, then friendly '
              'failure message shown', flush=True)
org3.db.close()

# --- 3. garbage file at launch -> __init__ recreates instead of crashing
with open(db, 'wb') as fh:  # scenario 2's migration removed it: recreate
    fh.write(b'THIS IS NOT A DATABASE' * 50)
org4 = core.VideoOrganizer(db_path=db)  # must not raise
n = org4.db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0]
assert n == 0
print('LAUNCH CORRUPTION: cache recreated automatically', flush=True)
org4.close()

# --- 4. corrupt cache drive -> auto-relocate mid-scan; scan completes on
#        the internal drive and future launches remember the location
bad_dir = os.path.join(tmp, 'baddrive')
os.makedirs(bad_dir)
bad_db = os.path.join(bad_dir, 'library.db')
org5 = core.VideoOrganizer(db_path=bad_db)
real_impl = core.VideoOrganizer._scan_impl


def corrupt_until_migrated(self, roots, recursive=True, progress=None,
                           session_id=None):
    # the 'bad drive' mangles every cache write until the cache is
    # relocated off it
    if self.db_path == bad_db:
        raise sqlite3.DatabaseError('database disk image is malformed')
    return real_impl(self, roots, recursive=recursive, progress=progress,
                     session_id=session_id)


p3 = mock.patch.object(core.VideoOrganizer, '_scan_impl',
                       corrupt_until_migrated)
with contextlib.ExitStack() as stack:
    for p in (*relocation_patches(), p3):
        stack.enter_context(p)
    stats4 = org5.scan([a])
assert stats4.get('cache_migrated') == org5.db_path, stats4
assert org5.db_path == os.path.join(fake_local, 'VidSweep', 'library.db')
assert stats4['processed'] == 6 and stats4['errors'] == 0, stats4
with open(fake_marker) as fh:
    assert fh.read().strip() == org5.db_path
sess5 = org5.get_last_session()
assert sess5 and not sess5['active'], sess5
print('DRIVE-FAILURE SIMULATION: cache relocated to the internal drive, '
      'scan completed, location remembered', flush=True)
org5.close()

shutil.rmtree(tmp, ignore_errors=True)
print('CORRUPT-RECOVERY TEST PASS')
