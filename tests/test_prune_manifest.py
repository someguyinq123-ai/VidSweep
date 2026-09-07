"""Ghost-row pruning (cache rows for vanished files) + EXECUTE action manifest."""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core

FF = core.find_ffmpeg() or 'ffmpeg'
tmp = tempfile.mkdtemp(prefix='vs_prune_')
a = os.path.join(tmp, 'a')
b = os.path.join(tmp, 'b')
os.makedirs(a)
os.makedirs(b)


def make_video(path):
    subprocess.run([FF, '-y', '-v', 'error',
                    '-f', 'lavfi', '-i', 'testsrc2=size=320x240:rate=15:duration=4',
                    '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
                    path], check=True, capture_output=True)


v1, v2, v3 = (os.path.join(a, f'v{i}.mp4') for i in (1, 2, 3))
vb = os.path.join(b, 'other.mp4')
for v in (v1, v2, v3, vb):
    make_video(v)

org = core.VideoOrganizer(db_path=os.path.join(tmp, 't.db'))
org.scan([a, b], recursive=True)

# a file under a scanned root disappears -> its row must be pruned
os.remove(v3)
# a ghost row under an OFFLINE root (passed as a scan root but not a dir)
# must survive — never prune what we cannot see
offline_root = os.path.join(tmp, 'offline_drive')
ghost_offline = os.path.join(offline_root, 'ghost.mp4')
org.db.execute(
    'INSERT OR REPLACE INTO files(path,size,mtime,sha256,phash,thumbnail)'
    ' VALUES(?,?,?,?,?,?)', (ghost_offline, 1, 0, 'a' * 64, None, None))
org.db.commit()

stats = org.scan([a, b, offline_root], recursive=True)
assert stats['pruned'] == 1, f'expected 1 pruned row, got {stats["pruned"]}'
remaining = {r[0] for r in org.db.execute('SELECT path FROM files')}
assert v3 not in remaining, 'vanished file was not pruned'
assert ghost_offline in remaining, 'pruned a row under an offline root!'
assert v1 in remaining and vb in remaining, 'pruned live files!'
print('PASS: prune removes vanished files, spares offline roots', flush=True)

# --- manifest: EXECUTE writes a planned-then-outcome CSV log
gui_tmp = tempfile.mkdtemp(prefix='vs_manifest_')
import gui
gui.messagebox.showinfo = lambda *a, **k: None
gui.messagebox.askyesno = lambda *a, **k: True
gui.messagebox.showwarning = lambda *a, **k: None
with mock.patch.object(gui, 'APP_DIR', gui_tmp):
    # ISOLATION: App() must never open the real library.db next to core.py
    _iso_dir = tempfile.mkdtemp(prefix='vs_db_')
    _iso_init = core.VideoOrganizer.__init__
    core.VideoOrganizer.__init__ = (
        lambda self, db_path=None: _iso_init(
    self, db_path=db_path or os.path.join(_iso_dir, 't.db')))
    app = gui.App()
    app.org = core.VideoOrganizer(db_path=os.path.join(gui_tmp, 't.db'))
    app.thumbs = gui.ThumbnailCache(app.org)
    app.nb.select(app.tab_dupes)

    fa = os.path.join(gui_tmp, 'kill_a.mp4')
    fb = os.path.join(gui_tmp, 'kill_b.mp4')
    fc = os.path.join(gui_tmp, 'move_me.mp4')
    for p in (fa, fb, fc):
        with open(p, 'wb') as fh:
            fh.write(b'x' * 1024)
    for p in (fa, fb, fc):
        app.org.db.execute(
            'INSERT OR REPLACE INTO files(path,size,mtime,sha256,phash)'
            ' VALUES(?,?,?,?,?)', (p, 1024, time.time(), 'x' * 64, None))
    app.org.db.commit()

    app.action_var.set('Delete permanently')  # no Recycle Bin pollution
    app.decisions[fa] = __import__('tkinter').StringVar(value='delete')
    app.decisions[fb] = __import__('tkinter').StringVar(value='delete')
    app.decisions[fc] = __import__('tkinter').StringVar(value='move')

    dest_dir = os.path.join(gui_tmp, 'dest')
    os.makedirs(dest_dir)
    gui.filedialog.askdirectory = lambda **k: dest_dir
    app.apply_decisions()

    assert not os.path.exists(fa) and not os.path.exists(fb), 'delete failed'
    assert not os.path.exists(fc), 'move source still present'
    moved_dest = os.path.join(dest_dir, 'move_me.mp4')
    assert os.path.isfile(moved_dest), 'move failed'
    row = app.org.db.execute(
        'SELECT path FROM files WHERE sha256=?', ('x' * 64,)).fetchall()
    assert [r[0] for r in row] == [moved_dest], f'DB not updated for move: {row}'

    logs = os.listdir(os.path.join(gui_tmp, 'logs'))
    assert len(logs) == 1, f'expected one action log, got {logs}'
    import csv
    with open(os.path.join(gui_tmp, 'logs', logs[0]), newline='') as fh:
        rows = list(csv.reader(fh))
    assert rows[0][:3] == ['timestamp', 'action', 'path'], rows[0]
    by_path = {r[2]: r for r in rows[1:]}
    assert by_path[fa][5] == 'deleted', by_path[fa]
    assert by_path[fb][5] == 'deleted', by_path[fb]
    assert by_path[fc][4] == moved_dest and by_path[fc][5] == 'moved', by_path[fc]
    print('PASS: EXECUTE manifest logs planned->outcome per file', flush=True)

    # organize-tab moves get the same audit trail
    src = os.path.join(gui_tmp, 'org_me.mp4')
    with open(src, 'wb') as fh:
        fh.write(b'y' * 512)
    cat_dir = os.path.join(gui_tmp, 'Cats')
    app.org_moves = [(src, 'Cats', os.path.join(cat_dir, 'org_me.mp4'))]
    app.apply_moves()
    assert os.path.isfile(os.path.join(cat_dir, 'org_me.mp4')), 'organize move failed'
    logs = sorted(os.listdir(os.path.join(gui_tmp, 'logs')))
    assert len(logs) == 2, f'expected a second action log, got {logs}'
    with open(os.path.join(gui_tmp, 'logs', logs[-1]), newline='') as fh:
        rows = list(csv.reader(fh))
    assert rows[1][1] == 'move' and rows[1][5] == 'moved', rows[1]
    print('PASS: organize moves logged too', flush=True)

    app.destroy()

org.close()
shutil.rmtree(tmp, ignore_errors=True)
shutil.rmtree(gui_tmp, ignore_errors=True)
print('PRUNE + MANIFEST TESTS PASS')
