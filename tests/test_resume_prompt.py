"""The startup resume prompt: a paused/stopped scan must be offered for
resume right after relaunch (the "closed the app / rebooted the PC" case)."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gui, core, json, tempfile, shutil, time
import unittest.mock as mock

gui.messagebox.showinfo = lambda *a, **k: None
gui.messagebox.showwarning = lambda *a, **k: None
gui.messagebox.showerror = lambda *a, **k: print('SHOWERROR:', a, flush=True)

tmp = tempfile.mkdtemp(prefix='vs_resume_prompt_')
root = os.path.join(tmp, 'videos')
os.makedirs(root)

# ISOLATION: App() must never open the real library.db next to core.py
_iso_dir = tempfile.mkdtemp(prefix='vs_db_')
_iso_init = core.VideoOrganizer.__init__
core.VideoOrganizer.__init__ = (
    lambda self, db_path=None: _iso_init(
    self, db_path=db_path or os.path.join(_iso_dir, 't.db')))

with mock.patch.object(gui, 'APP_DIR', tmp):
    app = gui.App()
    app.folder_list.delete(0, 'end')
    app.org = core.VideoOrganizer(db_path=os.path.join(tmp, 't.db'))
    app.thumbs = gui.ThumbnailCache(app.org)

    # --- seed a PAUSED, half-finished scan session (5 of 8 videos done)
    with app.org._db_lock:
        cur = app.org.db.execute(
            'INSERT INTO sessions(created,updated,roots,recursive,status,'
            'paused,total) VALUES(?,?,?,?,?,?,?)',
            (time.time(), time.time(), json.dumps([root]), 1, 'active', 1, 8))
        sid = cur.lastrowid
        for i in range(5):
            app.org.db.execute(
                'INSERT INTO files(path,size,mtime,sha256,phash,duration,'
                'width,height,session_id) VALUES(?,?,?,?,?,?,?,?,?)',
                (os.path.join(root, f'v{i}.mp4'), 100 + i, 0.0,
                 f'sha{i}', '[]', 60.0, 640, 360, sid))
        app.org.db.commit()

    # --- the Resume button reflects the paused state and the progress
    app._refresh_resume_btn()
    label = app.resume_btn.cget('text')
    assert 'Resume paused scan' in label, label
    assert '5 of 8' in label, label
    print('button text:', label, flush=True)

    answers = []
    gui.messagebox.askyesno = lambda *a, **k: answers.pop(0)

    # --- declined: no scan starts, the offer stays available on the tab
    answers.append(False)
    app._offer_resume_on_startup()
    assert not (app._scan_thread and app._scan_thread.is_alive()), \
        'scan started even though the user declined'
    assert 'Unfinished scan' in app.status_var.get(), app.status_var.get()

    # --- accepted: the paused session is resumed with the SAME session id
    launched = []
    app._launch_scan = lambda folders, recursive, session_id=None: \
        launched.append((list(folders), recursive, session_id))
    answers.append(True)
    app._offer_resume_on_startup()
    assert launched == [([root], True, sid)], launched
    print('PASS: startup prompt resumes the paused session id', sid, flush=True)

    app.destroy()

shutil.rmtree(tmp, ignore_errors=True)
print('RESUME-PROMPT TEST PASS')
