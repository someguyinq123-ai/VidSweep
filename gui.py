"""
Video Organizer — GUI (tkinter).
Tabs:
  1. Scan      — pick folders, run the fingerprint pipeline with live progress
  2. Duplicates— groups with thumbnails, keep/decide each file, act on groups
  3. Organize  — name-based category suggestions, preview moves, apply
"""

import json
import os
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from PIL import Image, ImageTk
import io

import core

APP_DIR = os.path.dirname(os.path.abspath(__file__))

SENSITIVITY_HELP_TEXT = """\
HOW PERCEPTUAL MATCH SENSITIVITY WORKS

The app compares 4 sampled frames from each video (at 10%, 35%, 60% and 85% of
the runtime). Each frame is reduced to a 64-bit "fingerprint" of its visual
structure. Two frames match if their fingerprints differ by at most N bits —
and N is exactly the number this slider sets.

What the slider changes:
  • The maximum allowed bit difference (N) between two frames' fingerprints.
    4 = nearly pixel-identical frames only. 14 = frames can look quite
    different and still count as a match.
  • How many of a video's 4 frames must match: at least 3 of 4 must match at
    any setting (75%), so one noisy frame (fade-in, black frame) won't break
    a true match.

What the slider does NOT change:
  • Duration check — videos must also be within 10% of the same length.
  • Exact duplicates — byte-identical files are always grouped regardless
    of this setting.

Level-by-level guide:

  4  Very strict. Only catches re-encodes that are visually almost identical
     to the original (high-bitrate copies, container changes like mp4→mkv).
     Misses heavily degraded copies. Essentially zero false matches.
  6  Strict. Catches most re-encodes, including modest resolution changes.
     Very few false matches. Good if you have clean rips of the same source.
  8  DEFAULT / BALANCED. Catches re-encodes, resolution drops, and moderate
     quality loss. Occasional false matches between visually similar videos
     of the same length (e.g. different episodes with identical intros).
 10  Loose. Catches heavily compressed or resized copies. More false
     matches — expect to un-mark some suggested pairs by hand.
 12+ Very loose. Groups anything vaguely similar with similar length.
     High false-match rate; only useful if you want maximum disk savings
     and are willing to review every group carefully.

Rule of thumb: start at 8. If true duplicates are being MISSED, raise it.
If unrelated videos are being GROUPED, lower it.
"""


class ThumbnailCache:
    """Loads JPEG blobs from db, decodes to PhotoImage at fixed size."""

    def __init__(self, organizer, size=(160, 90)):
        self.org = organizer
        self.size = size
        self._cache = {}
        self._missing = None  # 1x1 gray

    def get(self, path):
        if path in self._cache:
            return self._cache[path]
        blob = self.org.get_thumbnail(path)
        img = None
        if blob:
            try:
                pil = Image.open(io.BytesIO(blob))
                pil.thumbnail(self.size)
                img = ImageTk.PhotoImage(pil)
            except Exception:
                img = None
        if img is None:
            if self._missing is None:
                self._missing = ImageTk.PhotoImage(
                    Image.new('RGB', self.size, (60, 60, 60)))
            img = self._missing
        self._cache[path] = img
        return img


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('Video Organizer')
        self.geometry('1200x800')
        self.minsize(900, 600)

        self.org = core.VideoOrganizer()
        self.thumbs = ThumbnailCache(self.org)
        self.groups = []            # list of groups (lists of recs)
        self.decisions = {}         # path -> 'keep' | 'delete' | 'move'
        self._scan_thread = None
        self.privacy = self._load_privacy()
        self.protocol('WM_DELETE_WINDOW', self._on_close)
        self._build_ui()

    def _on_close(self):
        if self.privacy.get('wipe_db_on_exit'):
            try:
                self.org.close()
                for ext in ('', '-wal', '-shm'):
                    p = self.org.db_path + ext
                    if os.path.isfile(p):
                        os.remove(p)
            except Exception:
                pass
        self.destroy()

    # ------------------------------------------------------------- privacy
    PRIVACY_DEFAULTS = {
        'wipe_db_on_exit': False,       # 2: shred library.db when app closes
        'secure_delete': False,         # 3: overwrite bytes before removal
        'open_no_history': False,       # 4: launch player with history disabled
    }

    def _load_privacy(self):
        cfg = os.path.join(APP_DIR, 'privacy.json')
        vals = dict(self.PRIVACY_DEFAULTS)
        if os.path.isfile(cfg):
            try:
                with open(cfg) as fh:
                    vals.update(json.load(fh))
            except Exception:
                pass
        return vals

    def _save_privacy(self, vals):
        with open(os.path.join(APP_DIR, 'privacy.json'), 'w') as fh:
            json.dump(vals, fh)

    def open_privacy_settings(self):
        win = tk.Toplevel(self)
        win.title('Privacy settings')
        win.transient(self)
        win.grab_set()
        frm = ttk.Frame(win, padding=14)
        frm.pack(fill='both', expand=True)
        ttk.Label(frm, text='All options are OFF by default. Settings persist.',).grid(
            row=0, column=0, columnspan=2, sticky='w', pady=(0, 10))

        vars_ = {}
        rows = [
            ('wipe_db_on_exit',
             'Wipe library database on exit',
             'Deletes library.db (fingerprints + thumbnails) every time the app closes.\n'
             'Maximum privacy: nothing about your library stays on disk.\n'
             'Cost: every launch starts as a full rescan — slow on 30k files.'),
            ('secure_delete',
             'Secure delete (overwrite before removal)',
             'When EXECUTE deletes files: overwrites their bytes with random data first,\n'
             'so they cannot be recovered from the Recycle Bin or with recovery tools.\n'
             'Slower for large files. Files bypass the Recycle Bin entirely.'),
            ('open_no_history',
             'Open videos without leaving history traces',
             'The "Open" button launches your player with history/recents disabled\n'
             '(MPC-HC private mode, VLC never saves recent list, etc.).\n'
             'Also skips Windows Recent Items jump-list entries.'),
        ]
        for i, (key, label, desc) in enumerate(rows, start=1):
            var = tk.BooleanVar(value=self.privacy[key])
            vars_[key] = var
            ttk.Checkbutton(frm, text=label, variable=var).grid(
                row=i, column=0, columnspan=2, sticky='w')
            ttk.Label(frm, text=desc, foreground='gray40', wraplength=440,
                      justify='left').grid(row=i, column=0, columnspan=2, sticky='w', padx=(28, 0))

        def on_ok():
            vals = {k: v.get() for k, v in vars_.items()}
            self.privacy = vals
            self._save_privacy(vals)
            win.destroy()
        ttk.Button(frm, text='Save', command=on_ok).grid(row=len(rows) + 1, column=0, pady=(12, 0))
        ttk.Button(frm, text='Cancel', command=win.destroy).grid(
            row=len(rows) + 1, column=1, pady=(12, 0), padx=6)

    # ------------------------------------------------------------------ util
    def _build_ui(self):
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill='both', expand=True)
        self.tab_scan = ttk.Frame(self.nb)
        self.tab_dupes = ttk.Frame(self.nb)
        self.tab_org = ttk.Frame(self.nb)
        self.nb.add(self.tab_scan, text=' 1. Scan ')
        self.nb.add(self.tab_dupes, text=' 2. Duplicates ')
        self.nb.add(self.tab_org, text=' 3. Organize ')
        self._build_scan_tab()
        self._build_dupes_tab()
        self._build_org_tab()

    # --- Scan tab
    def _build_scan_tab(self):
        f = self.tab_scan
        pad = {'padx': 10, 'pady': 6}

        row = ttk.Frame(f); row.pack(fill='x', **pad)
        ttk.Label(row, text='Folders to scan:').pack(anchor='w')
        self.folder_list = tk.Listbox(row, height=5)
        self.folder_list.pack(fill='x', side='top')
        btns = ttk.Frame(row); btns.pack(fill='x', pady=4)
        ttk.Button(btns, text='Add folder…', command=self.add_folder).pack(side='left')
        ttk.Button(btns, text='Remove selected', command=self.remove_folder).pack(side='left', padx=4)
        self.recursive_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(btns, text='Include subfolders', variable=self.recursive_var).pack(side='left', padx=8)

        opts = ttk.Frame(f); opts.pack(fill='x', **pad)
        ttk.Label(opts, text='Perceptual match sensitivity:').pack(side='left')
        self.sens_var = tk.IntVar(value=core.HAMMING_THRESHOLD)
        ttk.Scale(opts, from_=4, to=14, variable=self.sens_var,
                  command=lambda v: self.sens_lbl.config(text=str(self.sens_var.get()))).pack(side='left', padx=6)
        self.sens_lbl = ttk.Label(opts, text=str(self.sens_var.get()))
        self.sens_lbl.pack(side='left')
        ttk.Label(opts, text='(lower = stricter, fewer false matches)').pack(side='left', padx=8)
        ttk.Button(opts, text='What do these levels mean?',
                   command=self.show_sensitivity_help).pack(side='left', padx=6)

        ffrow = ttk.Frame(f); ffrow.pack(fill='x', **pad)
        self._update_ffmpeg_status(f)
        ttk.Button(ffrow, text='Locate ffmpeg…', command=self.locate_ffmpeg).pack(side='left')

    def _update_ffmpeg_status(self, parent):
        ff = core.find_ffmpeg()
        fp = core.find_ffprobe()
        if ff and fp:
            self.ffmpeg_status = ttk.Label(
                parent, text=f'✓ ffmpeg found: {ff}', foreground='green')
        else:
            self.ffmpeg_status = ttk.Label(
                parent, text='✗ ffmpeg NOT found — scanning will fail. Click "Locate ffmpeg…".',
                foreground='red')
        self.ffmpeg_status.pack(anchor='w', padx=10, pady=2)

    def locate_ffmpeg(self):
        d = os.path.dirname(core.find_ffmpeg() or '') or 'C:\\'
        p = filedialog.askopenfilename(
            title='Select ffmpeg.exe', initialdir=d,
            filetypes=[('ffmpeg.exe', 'ffmpeg.exe'), ('All files', '*.*')])
        if not p:
            return
        core.set_ffmpeg_override(p)
        with open(os.path.join(APP_DIR, 'ffmpeg_path.txt'), 'w') as fh:
            fh.write(p)
        self.ffmpeg_status.config(
            text=f'✓ ffmpeg set: {p}', foreground='green')

        run = ttk.Frame(f); run.pack(fill='x', **pad)
        self.scan_btn = ttk.Button(run, text='Start scan', command=self.start_scan)
        self.scan_btn.pack(side='left')
        self.cancel_btn = ttk.Button(run, text='Cancel', command=self.cancel_scan, state='disabled')
        self.cancel_btn.pack(side='left', padx=6)
        self.pause_btn = ttk.Button(run, text='Pause', command=self.toggle_pause, state='disabled')
        self.pause_btn.pack(side='left')
        ttk.Button(run, text='Reset library (delete database)',
                   command=self.reset_library).pack(side='right')
        ttk.Button(run, text='Privacy settings…',
                   command=self.open_privacy_settings).pack(side='right', padx=6)

        self.progress = ttk.Progressbar(f, mode='determinate')
        self.progress.pack(fill='x', **pad)
        self.status_var = tk.StringVar(value='Ready. Add folders, then Start scan.')
        ttk.Label(f, textvariable=self.status_var).pack(anchor='w', **pad)
        self.log = tk.Text(f, height=10, state='disabled')
        self.log.pack(fill='both', expand=True, **pad)

        # restore saved folders
        cfg = os.path.join(APP_DIR, 'settings.json')
        if os.path.isfile(cfg):
            try:
                with open(cfg) as fh:
                    s = json.load(fh)
                for p in s.get('folders', []):
                    self.folder_list.insert('end', p)
            except Exception:
                pass

    def add_folder(self):
        d = filedialog.askdirectory(title='Choose folder containing videos')
        if d:
            self.folder_list.insert('end', os.path.normpath(d))
            self._save_settings()

    def remove_folder(self):
        sel = self.folder_list.curselection()
        if sel:
            self.folder_list.delete(sel[0])
            self._save_settings()

    def _save_settings(self):
        os.makedirs(APP_DIR, exist_ok=True)
        with open(os.path.join(APP_DIR, 'settings.json'), 'w') as fh:
            json.dump({'folders': list(self.folder_list.get(0, 'end'))}, fh)

    def log_line(self, s):
        self.log.config(state='normal')
        self.log.insert('end', s + '\n')
        self.log.see('end')
        self.log.config(state='disabled')

    def start_scan(self):
        folders = list(self.folder_list.get(0, 'end'))
        if not folders:
            messagebox.showwarning('Video Organizer', 'Add at least one folder first.')
            return
        self._save_settings()
        self.scan_btn.config(state='disabled')
        self.cancel_btn.config(state='normal')
        self.pause_btn.config(state='normal')
        self.progress.config(value=0)
        self.status_var.set('Scanning…')
        self._scan_thread = threading.Thread(
            target=self._scan_worker,
            args=(folders, self.recursive_var.get()), daemon=True)
        self._scan_thread.start()

    def cancel_scan(self):
        self.org.cancel()

    def toggle_pause(self):
        if self.org._pause.is_set():
            self.org.resume()
            self.pause_btn.config(text='Pause')
            self.status_var.set('Scan resumed…')
        else:
            self.org.pause()
            self.pause_btn.config(text='Resume')
            self.status_var.set('Scan PAUSED — click Resume to continue. (In-flight videos finish first.)')

    def show_sensitivity_help(self):
        messagebox.showinfo('How perceptual match sensitivity works', SENSITIVITY_HELP_TEXT)

    def reset_library(self):
        if self._scan_thread and self._scan_thread.is_alive():
            messagebox.showwarning('Video Organizer',
                                   'A scan is running. Cancel it (and wait for it to stop) before resetting.')
            return
        db_path = self.org.db_path
        size_mb = os.path.getsize(db_path) / 1e6 if os.path.isfile(db_path) else 0
        if not messagebox.askyesno(
                'Reset library',
                f'Delete the library database?\n\n{db_path}\n({size_mb:.1f} MB)\n\n'
                'This removes ALL cached fingerprints, thumbnails and scan results. '
                'Your video files are NOT touched. Next scan starts from zero.'):
            return
        if not messagebox.askyesno(
                'Reset library', 'Are you sure? This cannot be undone.'):
            return
        try:
            self.org.close()
            for ext in ('', '-wal', '-shm'):
                p = db_path + ext
                if os.path.isfile(p):
                    os.remove(p)
        except Exception as e:
            messagebox.showerror('Video Organizer', f'Could not delete database:\n{e}')
            return
        # rebuild a fresh, empty library
        self.org = core.VideoOrganizer(db_path=db_path)
        self.thumbs = ThumbnailCache(self.org)
        self.groups = []
        self.decisions.clear()
        for iid in self.group_tree.get_children():
            self.group_tree.delete(iid)
        self.dupe_summary.config(text='')
        self._update_marked_count()
        self.log_line('Library reset — database deleted and recreated empty.')
        self.status_var.set('Library reset. Ready for a fresh scan.')
        messagebox.showinfo('Video Organizer', 'Library deleted and recreated empty.')

    def _scan_worker(self, folders, recursive):
        def progress(phase, done, total, cur):
            pct = (done / total * 100) if total else 0
            self.after(0, self._update_progress, phase, done, total, pct, cur)
        try:
            stats = self.org.scan(folders, recursive=recursive, progress=progress)
        except core.Cancelled:
            self.after(0, lambda: self._scan_done(cancelled=True))
            return
        except Exception as e:
            self.after(0, lambda: self._scan_done(error=str(e)))
            return
        self.after(0, lambda: self._scan_done(stats=stats))

    def _update_progress(self, phase, done, total, pct, cur):
        labels = {'scan': 'Finding files', 'hashing': 'Exact hashing',
                  'perceptual': 'Frames & perceptual hashes'}
        self.progress.config(value=pct)
        self.status_var.set(f"{labels.get(phase, phase)}: {done}/{total}  {cur}")

    def _scan_done(self, stats=None, cancelled=False, error=None):
        self.scan_btn.config(state='normal')
        self.cancel_btn.config(state='disabled')
        self.pause_btn.config(state='disabled', text='Pause')
        self.org.resume()  # clear pause flag so a future scan isn't stuck
        if error:
            self.status_var.set('Scan failed.')
            self.log_line(f'ERROR: {error}')
            messagebox.showerror('Video Organizer', f'Scan failed:\n{error}')
            return
        if cancelled:
            self.status_var.set('Scan cancelled.')
            self.log_line('Scan cancelled by user.')
            return
        self.status_var.set(
            f"Done in {stats['elapsed']}s — {stats['scanned']} videos found, "
            f"{stats['processed']} processed, {stats['skipped_cached']} cached, "
            f"{stats['errors']} errors.")
        self.log_line(f"Scan complete: {stats}")
        self.load_groups()

    # --- Duplicates tab
    def _build_dupes_tab(self):
        f = self.tab_dupes
        top = ttk.Frame(f); top.pack(fill='x', padx=10, pady=6)
        ttk.Button(top, text='Refresh duplicate list', command=self.load_groups).pack(side='left')
        self.dupe_summary = ttk.Label(top, text='')
        self.dupe_summary.pack(side='left', padx=12)

        # --- action bar: delete/keep right here, at the top where it's obvious
        action = ttk.LabelFrame(f, text=' Act on marked files ', padding=(8, 4))
        action.pack(fill='x', padx=10, pady=(0, 6))
        ttk.Label(action, text='Files marked "Delete" →').pack(side='left')
        self.action_var = tk.StringVar(value='Recycle Bin')
        ttk.Combobox(action, textvariable=self.action_var, state='readonly',
                     values=['Recycle Bin', 'Move to quarantine folder', 'Delete permanently'],
                     width=24).pack(side='left', padx=6)
        self.quarantine_var = tk.StringVar(value=os.path.join(APP_DIR, 'quarantine'))
        ttk.Entry(action, textvariable=self.quarantine_var, width=34).pack(side='left', padx=4)
        apply_btn = ttk.Button(action, text='EXECUTE (delete marked, keep unmarked)',
                               command=self.apply_decisions)
        apply_btn.pack(side='left', padx=10)
        ttk.Button(action, text='Mark ALL groups: keep best, delete rest',
                   command=self.mark_all_keep_best).pack(side='left', padx=4)
        self.marked_label = ttk.Label(action, text='0 marked for deletion')
        self.marked_label.pack(side='left', padx=10)

        # groups list on left, group detail on right
        paned = ttk.Panedwindow(f, orient='horizontal')
        paned.pack(fill='both', expand=True, padx=10, pady=6)

        left = ttk.Frame(paned)
        self.group_tree = ttk.Treeview(left, columns=('files', 'wasted'), show='headings', height=28)
        self.group_tree.heading('files', text='Group (files)')
        self.group_tree.heading('wasted', text='Redundant MB')
        self.group_tree.column('files', width=110, anchor='e')
        self.group_tree.column('wasted', width=110, anchor='e')
        self.group_tree.pack(side='left', fill='y')
        sb = ttk.Scrollbar(left, orient='vertical', command=self.group_tree.yview)
        self.group_tree.config(yscrollcommand=sb.set)
        sb.pack(side='left', fill='y')
        self.group_tree.bind('<<TreeviewSelect>>', self._on_group_selected)
        paned.add(left, weight=1)

        right = ttk.Frame(paned)
        self.detail_info = ttk.Label(right, text='Select a duplicate group on the left.')
        self.detail_info.pack(anchor='w')
        ttk.Button(right, text='Keep best in THIS group, delete the rest',
                   command=self._keep_best_current_group).pack(anchor='w', pady=2)
        canvas_frame = ttk.Frame(right)
        canvas_frame.pack(fill='both', expand=True)
        self.detail_canvas = tk.Canvas(canvas_frame, highlightthickness=0)
        dsb = ttk.Scrollbar(canvas_frame, orient='vertical', command=self.detail_canvas.yview)
        self.detail_inner = ttk.Frame(self.detail_canvas)
        self.detail_inner.bind('<Configure>',
            lambda e: self.detail_canvas.configure(scrollregion=self.detail_canvas.bbox('all')))
        self.detail_canvas.create_window((0, 0), window=self.detail_inner, anchor='nw')
        self.detail_canvas.configure(yscrollcommand=dsb.set)
        self.detail_canvas.pack(side='left', fill='both', expand=True)
        dsb.pack(side='right', fill='y')
        self.detail_canvas.bind_all('<MouseWheel>',
            lambda e: self.detail_canvas.yview_scroll(-1 * (e.delta // 120), 'units'))
        paned.add(right, weight=3)

    def load_groups(self):
        for iid in self.group_tree.get_children():
            self.group_tree.delete(iid)
        self.groups = self.org.find_duplicates(threshold=self.sens_var.get())
        total_waste = 0
        for gi, g in enumerate(self.groups):
            waste = sum(r['size'] for r in g[1:])
            total_waste += waste
            self.group_tree.insert('', 'end', iid=str(gi),
                values=(f'Group {gi+1} ({len(g)} files)', f'{waste/1e6:,.1f}'))
        self.dupe_summary.config(
            text=f'{len(self.groups)} duplicate groups — {total_waste/1e9:.2f} GB redundant')
        self.decisions.clear()

    def _on_group_selected(self, _evt):
        sel = self.group_tree.selection()
        if not sel:
            return
        gi = int(sel[0])
        g = self.groups[gi]
        for w in self.detail_inner.winfo_children():
            w.destroy()
        best = g[0]
        self.detail_info.config(text=(
            f"Group {gi+1}: {len(g)} files. Best copy (auto-suggested keep): "
            f"{os.path.basename(best['path'])}  [{best['width']}x{best['height']}, "
            f"{best['size']/1e6:,.1f} MB, {best['vcodec']}]"))
        for i, r in enumerate(g):
            row = ttk.Frame(self.detail_inner)
            row.pack(fill='x', pady=4, padx=4)
            lbl = ttk.Label(row, image=self.thumbs.get(r['path']))
            lbl.image = None
            lbl.pack(side='left')
            info = ttk.Frame(row)
            info.pack(side='left', fill='x', expand=True, padx=8)
            name = r['path']
            ttk.Label(info, text=name, wraplength=520).pack(anchor='w')
            dur = f"{int(r['duration']//60)}:{int(r['duration']%60):02d}" if r['duration'] else '?'
            ttk.Label(info, text=(
                f"{r['width']}x{r['height']}  {r['size']/1e6:,.1f} MB  {dur}  "
                f"{r['vcodec'] or '?'}  {'EXACT COPY' if i > 0 and r['sha'] == best['sha'] else ''}"
                )).pack(anchor='w')
            # preserve a decision the user already made for this file
            existing = self.decisions.get(r['path'])
            default = 'keep' if i == 0 else 'undecided'
            v = tk.StringVar(value=existing.get() if existing else default)
            v.trace_add('write', lambda *a: self._update_marked_count())
            self.decisions[r['path']] = v
            rbf = ttk.Frame(info); rbf.pack(anchor='w')
            for val, txt in (('keep', 'Keep'), ('delete', 'Delete'), ('move', 'Move')):
                ttk.Radiobutton(rbf, text=txt, variable=v, value=val).pack(side='left', padx=2)
            ttk.Button(rbf, text='Open', width=5,
                       command=lambda p=r['path']: self._open_file(p)).pack(side='left', padx=6)
            if i == 0:
                ttk.Label(rbf, text='← suggested keep (best quality)').pack(side='left', padx=6)

    def _open_file(self, path):
        if self.privacy.get('open_no_history'):
            from shutil import which
            # VLC: --no-qt-recentplay stops it saving a recent-files list.
            # MPC-HC: launched directly (no Windows Recent Items entry);
            # disable "Keep history" in its own options for full privacy.
            candidates = [
                ('vlc.exe', ['--no-qt-recentplay'],
                 (r'C:\Program Files\VideoLAN\VLC',)),
                ('mpc-hc64.exe', [],
                 (r'C:\Program Files\MPC-HC', r'C:\Program Files (x86)\MPC-HC')),
                ('mpc-hc.exe', [],
                 (r'C:\Program Files\MPC-HC', r'C:\Program Files (x86)\MPC-HC')),
            ]
            for name, extra_args, dirs in candidates:
                exe = which(name)
                if not exe:
                    for d in dirs:
                        cand = os.path.join(d, name)
                        if os.path.isfile(cand):
                            exe = cand
                            break
                if exe:
                    try:
                        subprocess.Popen([exe, *extra_args, path])
                        return
                    except Exception:
                        pass
        try:
            os.startfile(path)  # noqa
        except Exception:
            pass

    def mark_all_keep_best(self):
        if not self.groups:
            messagebox.showinfo('Video Organizer', 'No groups loaded — scan first.')
            return
        n = 0
        for g in self.groups:
            for i, r in enumerate(g):
                if r['path'] in self.decisions:
                    self.decisions[r['path']].set('keep' if i == 0 else 'delete')
                    if i > 0:
                        n += 1
        self._update_marked_count()
        messagebox.showinfo('Video Organizer',
                            f'Marked {n} files for deletion (best copy kept per group).\n'
                            'Review if you like, then click EXECUTE.')

    def _keep_best_current_group(self):
        sel = self.group_tree.selection()
        if not sel:
            messagebox.showinfo('Video Organizer', 'Select a group first.')
            return
        g = self.groups[int(sel[0])]
        for i, r in enumerate(g):
            if r['path'] in self.decisions:
                self.decisions[r['path']].set('keep' if i == 0 else 'delete')
        self._update_marked_count()

    def _update_marked_count(self):
        if hasattr(self, 'marked_label'):
            n = sum(1 for v in self.decisions.values() if v.get() == 'delete')
            self.marked_label.config(text=f'{n} marked for deletion')

    @staticmethod
    def _secure_delete(path):
        """Overwrite file bytes with random data before removing, so recovery tools see nothing."""
        size = os.path.getsize(path)
        chunk = 4 * 1024 * 1024
        with open(path, 'r+b') as f:
            while True:
                block = os.urandom(min(chunk, size))
                if not block:
                    break
                f.write(block)
                size -= len(block)
                if size <= 0:
                    break
            f.flush()
            os.fsync(f.fileno())
        os.remove(path)

    def apply_decisions(self):
        to_delete = [p for p, v in self.decisions.items() if v.get() == 'delete']
        to_move = [p for p, v in self.decisions.items() if v.get() == 'move']
        if not to_delete and not to_move:
            messagebox.showinfo('Video Organizer', 'Nothing marked. Mark files with Keep/Delete/Move first.')
            return
        # transparent confirmation: list EVERY file, since marking accumulates across groups
        lines = []
        if to_delete:
            lines.append(f"DELETE {len(to_delete)} file(s) ({self.action_var.get()}):")
            lines += [f'  ✗ {p}' for p in to_delete[:15]]
            if len(to_delete) > 15:
                lines.append(f'  … and {len(to_delete) - 15} more')
        if to_move:
            lines.append(f"MOVE {len(to_move)} file(s):")
            lines += [f'  → {p}' for p in to_move[:15]]
            if len(to_move) > 15:
                lines.append(f'  … and {len(to_move) - 15} more')
        kept = sum(1 for v in self.decisions.values() if v.get() == 'keep')
        lines.append(f'\nAll other files already marked Keep ({kept}) are untouched.')
        lines.append('Marking accumulates across ALL groups you have visited — '
                     'the list above is everything that will happen.')
        msg = '\n'.join(lines)
        if not messagebox.askyesno('Confirm — review this list carefully', msg):
            return
        moved = deleted = failed = 0
        if self.action_var.get() == 'Recycle Bin':
            try:
                import send2trash
            except ImportError:
                messagebox.showerror('Video Organizer', 'send2trash not installed. Run: pip install send2trash')
                return
        for p in to_delete:
            try:
                mode = self.action_var.get()
                if mode == 'Recycle Bin' and not self.privacy.get('secure_delete'):
                    import send2trash
                    send2trash.send2trash(p)
                elif mode == 'Move to quarantine folder' and not self.privacy.get('secure_delete'):
                    q = self.quarantine_var.get()
                    os.makedirs(q, exist_ok=True)
                    dest = os.path.join(q, os.path.basename(p))
                    k = 1
                    while os.path.exists(dest):
                        base, ext = os.path.splitext(os.path.basename(p))
                        dest = os.path.join(q, f'{base}_{k}{ext}')
                        k += 1
                    shutil.move(p, dest)
                else:
                    # secure delete (privacy setting) or Delete permanently
                    if self.privacy.get('secure_delete'):
                        self._secure_delete(p)
                    else:
                        os.remove(p)
                self.org.db.execute('DELETE FROM files WHERE path=?', (p,))
                deleted += 1
            except Exception as e:
                failed += 1
                self.log_line(f'FAILED {p}: {e}')
        for p in to_move:
            dest_dir = filedialog.askdirectory(title=f'Choose destination for {os.path.basename(p)}')
            if not dest_dir:
                continue
            try:
                shutil.move(p, os.path.join(dest_dir, os.path.basename(p)))
                self.org.db.execute('UPDATE files SET path=? WHERE path=?',
                                    (os.path.join(dest_dir, os.path.basename(p)), p))
                self.org.db.commit()
                moved += 1
            except Exception as e:
                failed += 1
                self.log_line(f'FAILED move {p}: {e}')
        self.org.db.commit()
        messagebox.showinfo('Video Organizer',
                            f'Deleted: {deleted}, moved: {moved}, failed: {failed}')
        self.load_groups()

    # --- Organize tab
    def _build_org_tab(self):
        f = self.tab_org
        pad = {'padx': 10, 'pady': 6}
        top = ttk.Frame(f); top.pack(fill='x', **pad)
        ttk.Label(top, text='Organize remaining videos into category folders based on their names.').pack(anchor='w')
        row = ttk.Frame(f); row.pack(fill='x', **pad)
        ttk.Label(row, text='Source folder:').pack(side='left')
        self.org_source = tk.StringVar()
        ttk.Entry(row, textvariable=self.org_source, width=50).pack(side='left', padx=6)
        ttk.Button(row, text='Browse…', command=lambda: self.org_source.set(
            filedialog.askdirectory() or self.org_source.get())).pack(side='left')
        row2 = ttk.Frame(f); row2.pack(fill='x', **pad)
        ttk.Button(row2, text='Suggest categories', command=self.suggest_categories).pack(side='left')
        self.org_preview_btn = ttk.Button(row2, text='Preview moves', command=self.preview_moves, state='disabled')
        self.org_preview_btn.pack(side='left', padx=6)
        self.org_apply_btn = ttk.Button(row2, text='Apply moves', command=self.apply_moves, state='disabled')
        self.org_apply_btn.pack(side='left', padx=6)
        self.org_tree = ttk.Treeview(f, columns=('file', 'category', 'dest'), show='headings')
        for c, w in (('file', 380), ('category', 140), ('dest', 380)):
            self.org_tree.heading(c, text=c.title())
            self.org_tree.column(c, width=w)
        self.org_tree.pack(fill='both', expand=True, **pad)
        self.org_moves = []

    def suggest_categories(self):
        src = self.org_source.get()
        if not os.path.isdir(src):
            messagebox.showwarning('Video Organizer', 'Pick a valid source folder.')
            return
        paths = [r[0] for r in self.org.db.execute(
            'SELECT path FROM files')]
        # fall back to disk listing if db is empty for this folder
        if not paths or not any(p.startswith(os.path.normcase(src)) for p in paths):
            paths = []
            for dirpath, _dirs, files in os.walk(src):
                for fn in files:
                    if os.path.splitext(fn)[1].lower() in core.VIDEO_EXTS:
                        paths.append(os.path.join(dirpath, fn))
        sugg = self.org.suggest_folders(paths)
        self.org_moves = [(p, cat, os.path.join(src, cat, os.path.basename(p)))
                          for p, cat in sugg if os.path.dirname(p) != os.path.join(src, cat)]
        for iid in self.org_tree.get_children():
            self.org_tree.delete(iid)
        for p, cat, dest in self.org_moves:
            self.org_tree.insert('', 'end', values=(os.path.basename(p), cat, dest))
        self.org_preview_btn.config(state='normal')
        messagebox.showinfo('Video Organizer',
                            f'{len(sugg)} videos analyzed, {len(self.org_moves)} would move. '
                            'Review the list, then Preview moves.')

    def preview_moves(self):
        conflicts = [(p, d) for p, _c, d in self.org_moves if os.path.exists(d)]
        if conflicts:
            if not messagebox.askyesno(
                    'Video Organizer',
                    f'{len(conflicts)} destination name(s) already exist — '
                    'those files will be renamed with a suffix. Continue?'):
                return
        self.org_apply_btn.config(state='normal')
        messagebox.showinfo('Video Organizer',
                            f'Preview OK — {len(self.org_moves)} moves ready. Click "Apply moves".')

    def apply_moves(self):
        if not messagebox.askyesno('Confirm', f'Move {len(self.org_moves)} files into category folders?'):
            return
        done = failed = 0
        for p, _cat, dest in self.org_moves:
            try:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                if os.path.exists(dest):
                    base, ext = os.path.splitext(dest)
                    k = 1
                    while os.path.exists(dest):
                        dest = f'{base}_{k}{ext}'
                        k += 1
                shutil.move(p, dest)
                self.org.db.execute('UPDATE files SET path=? WHERE path=?', (dest, p))
                done += 1
            except Exception as e:
                failed += 1
                self.log_line(f'FAILED {p}: {e}')
        self.org.db.commit()
        messagebox.showinfo('Video Organizer', f'Moved {done}, failed {failed}.')
        self.org_apply_btn.config(state='disabled')


def main():
    try:
        app = App()
        app.mainloop()
    except Exception:
        import logging
        log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'crash.log')
        logging.basicConfig(filename=log_path, level=logging.ERROR)
        logging.exception('Video Organizer crashed')
        # also show it in a console if one is attached
        import traceback
        traceback.print_exc()
        try:
            import tkinter.messagebox as mb
            mb.showerror('Video Organizer — error',
                         f'Startup failed. Details written to:\n{log_path}\n\n'
                         f'{traceback.format_exc()[-800:]}')
        except Exception:
            pass
        raise


if __name__ == '__main__':
    main()
