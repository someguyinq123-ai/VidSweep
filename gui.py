"""
VidSweep — GUI (tkinter).
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
import time
from collections import OrderedDict
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from PIL import Image, ImageTk
import io

import core

APP_DIR = os.path.dirname(os.path.abspath(__file__))

# ---- theme palettes ------------------------------------------------------
# Every color used by custom-painted surfaces lives here; ttk styles and the
# dialogs read from one dict so light/dark stay consistent.
THEMES = {
    'light': {
        'bg': '#f8fafc', 'fg': '#1e293b', 'muted': '#64748b',
        'card': '#ffffff', 'card2': '#f1f5f9', 'hover': '#e2e8f0',
        'border': '#cbd5e1', 'select': '#dbeafe', 'select_fg': '#1e293b',
        'accent': '#2563eb', 'tab_fg': '#ffffff',
        'head_bg': '#0f172a', 'head_fg': '#ffffff', 'head_sub': '#94a3b8',
        'foot_bg': '#f1f5f9',
        'section_fg': '#334155', 'para_fg': '#1e293b',
        'card_border': '#e2e8f0', 'card_title': '#0f172a',
        'card_desc': '#475569',
        'tip_bg': '#fefce8', 'tip_border': '#fde047',
        'tip_title': '#713f12', 'tip_text': '#854d0e',
    },
    'dark': {
        'bg': '#0f172a', 'fg': '#e2e8f0', 'muted': '#94a3b8',
        'card': '#1e293b', 'card2': '#26334d', 'hover': '#3b4a63',
        'border': '#334155', 'select': '#2563eb', 'select_fg': '#ffffff',
        'accent': '#3b82f6', 'tab_fg': '#ffffff',
        'head_bg': '#020617', 'head_fg': '#ffffff', 'head_sub': '#94a3b8',
        'foot_bg': '#1e293b',
        'section_fg': '#cbd5e1', 'para_fg': '#e2e8f0',
        'card_border': '#334155', 'card_title': '#f1f5f9',
        'card_desc': '#94a3b8',
        'tip_bg': '#3a3010', 'tip_border': '#ca8a04',
        'tip_title': '#fde047', 'tip_text': '#fbbf24',
    },
}

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


class _SensitivityHelpDialog(tk.Toplevel):
    """Professional structured help dialog: sections + color-coded level table."""

    LEVELS = [
        ('4',  'Very strict',   '#16a34a',
         'Only catches re-encodes that are visually almost identical to the '
         'original (high-bitrate copies, container changes like mp4\u2192mkv). '
         'Misses heavily degraded copies. Essentially zero false matches.'),
        ('6',  'Strict',        '#65a30d',
         'Catches most re-encodes, including modest resolution changes. Very '
         'few false matches. Good if you have clean rips of the same source.'),
        ('8',  'Default',       '#0284c7',
         'Balanced. Catches re-encodes, resolution drops, and moderate quality '
         'loss. Occasional false matches between visually similar videos of '
         'the same length (e.g. different episodes with identical intros).'),
        ('10', 'Loose',         '#d97706',
         'Catches heavily compressed or resized copies. More false matches — '
         'expect to un-mark some suggested pairs by hand.'),
        ('12+', 'Very loose',   '#dc2626',
         'Groups anything vaguely similar with similar length. High false-match '
         'rate; only useful for maximum disk savings if you review every group.'),
    ]

    def __init__(self, master):
        super().__init__(master)
        self.title('How perceptual match sensitivity works')
        self.transient(master)
        self.geometry('640x640')
        self.minsize(560, 480)
        c = self._c = getattr(master, 'theme', THEMES['light'])
        bg = self._bg = c['bg']
        self.configure(bg=bg)

        # ---- header band -------------------------------------------------
        head = tk.Frame(self, bg=c['head_bg'], padx=20, pady=14)
        head.pack(fill='x')
        tk.Label(head, text='Perceptual Match Sensitivity',
                 font=('Segoe UI', 15, 'bold'), fg=c['head_fg'], bg=c['head_bg']
                 ).pack(anchor='w')
        tk.Label(head,
                 text='How strictly videos must look alike to be grouped as duplicates',
                 font=('Segoe UI', 9), fg=c['head_sub'], bg=c['head_bg']
                 ).pack(anchor='w')

        # ---- footer button (packed FIRST so it keeps its strip at the bottom)
        foot = tk.Frame(self, bg=c['foot_bg'], pady=10)
        foot.pack(fill='x', side='bottom')
        ok = ttk.Button(foot, text='Got it', command=self.destroy)
        ok.pack(padx=20)
        self.bind('<Return>', lambda e: self.destroy())
        self.bind('<Escape>', lambda e: self.destroy())

        # ---- scrollable body --------------------------------------------
        outer = tk.Frame(self, bg=bg)
        outer.pack(fill='both', expand=True)
        canvas = tk.Canvas(outer, bg=bg, highlightthickness=0)
        vsb = ttk.Scrollbar(outer, orient='vertical', command=canvas.yview)
        body = tk.Frame(canvas, bg=bg)
        body.bind('<Configure>',
                  lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
        win = canvas.create_window((0, 0), window=body, anchor='nw', width=600)
        # keep body width in sync when the dialog is resized
        canvas.bind('<Configure>',
                    lambda e: canvas.itemconfigure(win, width=e.width))
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side='left', fill='both', expand=True)
        vsb.pack(side='right', fill='y')
        # mouse-wheel scrolling
        canvas.bind_all('<MouseWheel>', lambda e: canvas.yview_scroll(
            -int(e.delta / 120), 'units'))
        self._wheel_canvas = canvas  # unbind on close

        pad = dict(padx=20)

        def section(title):
            f = tk.Frame(body, bg=bg)
            f.pack(fill='x', pady=(18, 4), **pad)
            tk.Label(f, text=title.upper(), font=('Segoe UI', 9, 'bold'),
                     fg=c['section_fg'], bg=bg).pack(anchor='w')

        def para(text, wl=550):
            tk.Label(body, text=text, font=('Segoe UI', 10), fg=c['para_fg'],
                     bg=bg, wraplength=wl, justify='left'
                     ).pack(anchor='w', pady=2, **pad)

        section('How it works')
        para('VidSweep samples 4 frames from each video (at 10%, 35%, 60% and '
             '85% of its runtime) and reduces each frame to a 64-bit '
             '\u201cfingerprint\u201d of its visual structure. Two frames match '
             'if their fingerprints differ by at most N bits — and N is exactly '
             'what this slider sets.')
        para('At any slider setting, at least 3 of the 4 frames must match, so '
             'a single noisy frame (fade-in, black frame) won\u2019t break a '
             'true match.')

        section('The slider does NOT change')
        para('\u2022  Duration check — videos must also be within 10% of the '
             'same length.\n'
             '\u2022  Exact duplicates — byte-identical files are always '
             'grouped regardless of this setting.')

        section('Level-by-level guide')
        for value, name, color, desc in self.LEVELS:
            row = tk.Frame(body, bg=c['card'], padx=12, pady=10,
                           highlightbackground=c['card_border'],
                           highlightthickness=1)
            row.pack(fill='x', pady=(0, 2), **pad)
            badge = tk.Label(row, text=value, font=('Consolas', 11, 'bold'),
                             fg='#ffffff', bg=color, width=4, pady=4)
            badge.pack(side='left', anchor='n', padx=(0, 12))
            right = tk.Frame(row, bg=c['card'])
            right.pack(side='left', fill='x', expand=True)
            tk.Label(right, text=name, font=('Segoe UI', 10, 'bold'),
                     fg=c['card_title'], bg=c['card']
                     ).pack(anchor='w')
            tk.Label(right, text=desc, font=('Segoe UI', 9), fg=c['card_desc'],
                     bg=c['card'], wraplength=480, justify='left'
                     ).pack(anchor='w')

        # rule of thumb callout
        tip = tk.Frame(body, bg=c['tip_bg'], padx=12, pady=10,
                       highlightbackground=c['tip_border'], highlightthickness=1)
        tip.pack(fill='x', pady=(16, 4), **pad)
        tk.Label(tip, text='\U0001f4a1  Rule of thumb', font=('Segoe UI', 9, 'bold'),
                 fg=c['tip_title'], bg=c['tip_bg']).pack(anchor='w')
        tk.Label(tip, text='Start at 8. If true duplicates are being MISSED, '
                           'raise it. If unrelated videos are being GROUPED, '
                           'lower it.', font=('Segoe UI', 9), fg=c['tip_text'],
                 bg=c['tip_bg'], wraplength=550, justify='left').pack(anchor='w')

        self.protocol('WM_DELETE_WINDOW', self._on_close)
        self.grab_set()
        self.focus_set()

    def _on_close(self):
        try:
            # stop intercepting mouse wheel app-wide
            self._wheel_canvas.unbind_all('<MouseWheel>')
        except Exception:
            pass
        self.destroy()


class ThumbnailCache:
    """Loads JPEG blobs from db, decodes to PhotoImage at fixed size."""

    def __init__(self, organizer, size=(160, 90), max_items=200):
        self.org = organizer
        self.size = size
        self._cache = OrderedDict()  # path -> PhotoImage, LRU order (oldest first)
        self._missing = None  # 1x1 gray
        self._max_items = max_items

    def get(self, path):
        if path in self._cache:
            self._cache.move_to_end(path)  # mark most-recently-used
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
        while len(self._cache) > self._max_items:
            self._cache.popitem(last=False)  # evict least-recently-used
        return img


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('VidSweep — Video Duplicate Finder & Organizer')
        self.geometry('1200x800')
        self.minsize(900, 600)

        self.org = core.VideoOrganizer()
        self.thumbs = ThumbnailCache(self.org)
        self.groups = []            # list of groups (lists of recs)
        self.decisions = {}         # path -> 'keep' | 'delete' | 'move'
        self._scan_thread = None
        self.privacy = self._load_privacy()
        self.theme_name = self._load_theme_name()
        self.theme = THEMES[self.theme_name]
        self.protocol('WM_DELETE_WINDOW', self._on_close)
        self._apply_theme()
        self._build_ui()

    # ------------------------------------------------------------- theme
    def _load_settings_full(self):
        cfg = os.path.join(APP_DIR, 'settings.json')
        if os.path.isfile(cfg):
            try:
                with open(cfg) as fh:
                    return json.load(fh)
            except Exception:
                pass
        return {}

    def _load_theme_name(self):
        s = self._load_settings_full()
        t = s.get('theme')
        return t if t in THEMES else 'light'

    def _save_theme(self):
        cfg = os.path.join(APP_DIR, 'settings.json')
        data = self._load_settings_full()
        data['theme'] = self.theme_name
        os.makedirs(APP_DIR, exist_ok=True)
        with open(cfg, 'w') as fh:
            json.dump(data, fh)

    def toggle_theme(self):
        self.set_theme('light' if self.theme_name == 'dark' else 'dark')

    def set_theme(self, name):
        if name not in THEMES or name == self.theme_name:
            return
        self.theme_name = name
        self.theme = THEMES[name]
        self._save_theme()
        self._apply_theme()
        btn = getattr(self, 'theme_btn', None)
        if btn is not None:
            btn.config(text='🌙 Dark mode' if name == 'light'
                       else '☀️ Light mode')
        self.update_idletasks()

    def _apply_theme(self):
        """(Re)paint every themed surface: ttk styles + classic tk widgets."""
        c = THEMES[self.theme_name]
        self.configure(bg=c['bg'])
        style = ttk.Style(self)
        try:
            style.theme_use('clam')
        except tk.TclError:
            pass  # keep whatever theme the platform provides
        style.configure('.', background=c['card'], foreground=c['fg'],
                        fieldbackground=c['card'], bordercolor=c['border'],
                        lightcolor=c['card'], darkcolor=c['card2'],
                        troughcolor=c['card2'])
        style.configure('TFrame', background=c['bg'])
        style.configure('TLabel', background=c['bg'], foreground=c['fg'])
        style.configure('TLabelframe', background=c['bg'],
                        foreground=c['fg'])
        style.configure('TLabelframe.Label', background=c['bg'],
                        foreground=c['fg'])
        style.configure('TNotebook', background=c['bg'],
                        bordercolor=c['border'])
        style.configure('TNotebook.Tab', background=c['card2'],
                        foreground=c['tab_fg'], padding=(14, 6))
        style.map('TNotebook.Tab',
                  background=[('selected', c['select'])],
                  foreground=[('selected', c['select_fg'])])
        style.configure('TButton', background=c['card2'],
                        foreground=c['fg'], bordercolor=c['border'])
        style.map('TButton', background=[('active', c['hover'])])
        style.configure('TCheckbutton', background=c['bg'],
                        foreground=c['fg'], focuscolor=c['accent'])
        style.map('TCheckbutton', background=[('active', c['hover'])])
        style.configure('TRadiobutton', background=c['bg'],
                        foreground=c['fg'], focuscolor=c['accent'])
        style.map('TRadiobutton', background=[('active', c['hover'])])
        style.configure('TEntry', fieldbackground=c['card'],
                        foreground=c['fg'], insertbackground=c['fg'])
        style.configure('TProgressbar', background=c['accent'],
                        troughcolor=c['card2'], bordercolor=c['bg'])
        style.configure('Vertical.TScrollbar', background=c['card2'],
                        troughcolor=c['bg'], bordercolor=c['bg'],
                        arrowcolor=c['muted'])
        style.configure('Horizontal.TScrollbar', background=c['card2'],
                        troughcolor=c['bg'], bordercolor=c['bg'],
                        arrowcolor=c['muted'])

        # classic (non-ttk) widgets created in _build_ui
        for attr, kind in (('log', 'text'), ('folder_list', 'listbox'),
                           ('detail_canvas', 'canvas')):
            w = getattr(self, attr, None)
            if w is None:
                continue  # not built yet (first apply) — builders use palette
            if kind == 'listbox':
                w.configure(bg=c['card'], fg=c['fg'],
                            selectbackground=c['select'],
                            selectforeground=c['select_fg'],
                            highlightbackground=c['border'],
                            highlightcolor=c['accent'])
            elif kind == 'text':
                w.configure(bg=c['card'], fg=c['fg'],
                            insertbackground=c['fg'])
            else:
                w.configure(bg=c['bg'], highlightbackground=c['border'])

    def _on_close(self):
        # A running scan holds the DB open; wiping/closing under it would
        # crash the worker and risk a half-written database.
        if self._scan_thread and self._scan_thread.is_alive():
            if not messagebox.askyesno(
                    'VidSweep',
                    'A scan is still running.\n\nCancel it and exit?'):
                return
            self.org.cancel()
            self._scan_thread.join(timeout=10)
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
        c = self.theme
        win = tk.Toplevel(self)
        win.title('Privacy settings')
        win.transient(self)
        win.grab_set()
        win.configure(bg=c['bg'])
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
        for i, (key, label, desc) in enumerate(rows):
            r = i * 2 + 1  # +1: row 0 is the header line (was colliding before)
            var = tk.BooleanVar(value=self.privacy[key])
            vars_[key] = var
            ttk.Checkbutton(frm, text=label, variable=var).grid(
                row=r, column=0, columnspan=2, sticky='w')
            ttk.Label(frm, text=desc, foreground=c['muted'], wraplength=460,
                      justify='left').grid(row=r + 1, column=0, columnspan=2,
                                           sticky='w', padx=(28, 0), pady=(0, 8))

        def on_ok():
            vals = {k: v.get() for k, v in vars_.items()}
            self.privacy = vals
            self._save_privacy(vals)
            win.destroy()
        btn_row = len(rows) * 2 + 1
        ttk.Button(frm, text='Save', command=on_ok).grid(row=btn_row, column=0, pady=(12, 0))
        ttk.Button(frm, text='Cancel', command=win.destroy).grid(
            row=btn_row, column=1, pady=(12, 0), padx=6)

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
        ttk.Label(row, text='Folders to scan: (Ctrl/Shift-click to select multiple; Delete key removes)').pack(anchor='w')
        c = self.theme
        self.folder_list = tk.Listbox(row, height=5, selectmode='extended',
                                      bg=c['card'], fg=c['fg'],
                                      selectbackground=c['select'],
                                      selectforeground=c['select_fg'],
                                      highlightbackground=c['border'],
                                      highlightcolor=c['accent'])
        self.folder_list.pack(fill='x', side='top')
        self.folder_list.bind('<Delete>', lambda e: self.remove_folder())
        self.folder_list.bind('<BackSpace>', lambda e: self.remove_folder())
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
        self._build_scan_tab_rest(f, pad)

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

    def _build_scan_tab_rest(self, f, pad):
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
        self.theme_btn = ttk.Button(
            run, text='🌙 Dark mode' if self.theme_name == 'light'
            else '☀️ Light mode',
            command=self.toggle_theme)
        self.theme_btn.pack(side='right', padx=6)

        self.progress = ttk.Progressbar(f, mode='determinate')
        self.progress.pack(fill='x', **pad)
        self.status_var = tk.StringVar(value='Ready. Add folders, then Start scan.')
        ttk.Label(f, textvariable=self.status_var).pack(anchor='w', **pad)
        c = self.theme
        self.log = tk.Text(f, height=10, state='disabled', bg=c['card'],
                           fg=c['fg'], insertbackground=c['fg'])
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
        # delete highest index first so lower indices stay valid during removal
        sel = sorted(self.folder_list.curselection(), reverse=True)
        if sel:
            for i in sel:
                self.folder_list.delete(i)
            self._save_settings()

    def _save_settings(self):
        os.makedirs(APP_DIR, exist_ok=True)
        # preserve other keys (e.g. 'theme') written elsewhere
        data = self._load_settings_full()
        data['folders'] = list(self.folder_list.get(0, 'end'))
        with open(os.path.join(APP_DIR, 'settings.json'), 'w') as fh:
            json.dump(data, fh)

    def log_line(self, s):
        self.log.config(state='normal')
        self.log.insert('end', s + '\n')
        self.log.see('end')
        self.log.config(state='disabled')

    def start_scan(self):
        folders = list(self.folder_list.get(0, 'end'))
        if not folders:
            messagebox.showwarning('VidSweep', 'Add at least one folder first.')
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
        # immediate feedback + stop double-clicks; _scan_done does final cleanup
        self.cancel_btn.config(state='disabled')
        self.pause_btn.config(state='disabled', text='Pause')
        # marching stripes + live counter so "cancelling" never looks like a hang
        self.progress.config(mode='indeterminate')
        self.progress.start(40)
        self._cancel_t0 = time.monotonic()
        self._poll_cancel()

    def _poll_cancel(self):
        th = self._scan_thread
        if th is None or not th.is_alive():
            return  # _scan_done fires separately and finalizes the UI
        elapsed = time.monotonic() - self._cancel_t0
        self.status_var.set(
            f'Cancelling… {elapsed:.0f}s (finishing in-flight files)')
        self._cancel_job = self.after(250, self._poll_cancel)

    def toggle_pause(self):
        if self.org._cancel.is_set():
            # cancel already requested — resuming a dead scan would look like a hang
            self.status_var.set('Cancel requested — scan is stopping.')
            return
        if self.org._pause.is_set():
            self.org.resume()
            self.pause_btn.config(text='Pause')
            self.status_var.set('Scan resumed…')
        else:
            self.org.pause()
            self.pause_btn.config(text='Resume')
            self.status_var.set('Scan PAUSED — click Resume to continue. (In-flight videos finish first.)')

    def show_sensitivity_help(self):
        _SensitivityHelpDialog(self)

    def reset_library(self):
        if self._scan_thread and self._scan_thread.is_alive():
            messagebox.showwarning('VidSweep',
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
            messagebox.showerror('VidSweep', f'Could not delete database:\n{e}')
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
        messagebox.showinfo('VidSweep', 'Library deleted and recreated empty.')

    def _scan_worker(self, folders, recursive):
        def progress(phase, done, total, cur):
            pct = (done / total * 100) if total else 0
            try:
                self.after(0, self._update_progress, phase, done, total, pct, cur)
            except (RuntimeError, tk.TclError):
                pass  # window closed mid-scan: keep scanning, skip UI updates
        try:
            stats = self.org.scan(folders, recursive=recursive, progress=progress)
        except core.Cancelled:
            try:
                self.after(0, lambda: self._scan_done(cancelled=True))
            except (RuntimeError, tk.TclError):
                pass
            return
        except Exception as e:
            try:
                self.after(0, lambda: self._scan_done(error=str(e)))
            except (RuntimeError, tk.TclError):
                pass
            return
        try:
            self.after(0, lambda: self._scan_done(stats=stats))
        except (RuntimeError, tk.TclError):
            pass

    def _update_progress(self, phase, done, total, pct, cur):
        labels = {'scan': 'Finding files', 'hashing': 'Exact hashing',
                  'perceptual': 'Frames & perceptual hashes'}
        try:
            self.progress.config(value=pct)
            now = time.monotonic()
            # throttle text updates: the bar moves every call, but the label
            # only refreshes ~10x/sec so a fast scan can't flood the mainloop
            if now - getattr(self, '_last_status_t', 0) > 0.1 or done >= total:
                self._last_status_t = now
                self.status_var.set(f"{labels.get(phase, phase)}: {done}/{total}  {cur}")
        except (RuntimeError, tk.TclError):
            pass  # window closed mid-scan

    def _scan_done(self, stats=None, cancelled=False, error=None):
        try:
            self.progress.stop()
            self.progress.config(mode='determinate', value=0)
            self.scan_btn.config(state='normal')
            self.cancel_btn.config(state='disabled')
            self.pause_btn.config(state='disabled', text='Pause')
            self.org.resume()  # clear pause flag so a future scan isn't stuck
        except (RuntimeError, tk.TclError):
            return  # window already closed; scan results are saved in the DB
        if error:
            self.status_var.set('Scan failed.')
            self.log_line(f'ERROR: {error}')
            messagebox.showerror('VidSweep', f'Scan failed:\n{error}')
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
        ttk.Button(top, text='Refresh duplicate list', command=self.load_groups)
        self.refresh_btn = top.winfo_children()[-1]
        self.refresh_btn.pack(side='left')
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
        self.detail_canvas = tk.Canvas(canvas_frame, highlightthickness=0,
                                       bg=self.theme['bg'])
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
        # Run the CPU-bound comparison in a background thread so the UI
        # stays responsive; results are applied back on the main thread.
        self.refresh_btn.config(state='disabled')
        self.status_var.set('Loading duplicate groups…')
        threshold = self.sens_var.get()  # read tk variable on main thread only
        self.update_idletasks()
        self._load_result = None  # set by worker, consumed by _poll_load_result

        def _worker():
            try:
                groups = self.org.find_duplicates(threshold=threshold)
                self._load_result = (groups, None)
            except Exception as e:
                self._load_result = (None, str(e))

        threading.Thread(target=_worker, daemon=True).start()
        self._poll_load_result()

    def _poll_load_result(self):
        res = getattr(self, '_load_result', None)
        if res is None:
            try:
                self.after(50, self._poll_load_result)
            except (RuntimeError, tk.TclError):
                pass  # window gone: worker result simply never gets applied
            return
        self._load_result = None
        groups, err = res
        self.refresh_btn.config(state='normal')
        if err:
            self.status_var.set(f'Error loading groups: {err}')
            messagebox.showerror('VidSweep', f'Failed to load duplicate groups:\n{err}')
            return
        self.groups = groups
        for iid in self.group_tree.get_children():
            self.group_tree.delete(iid)
        total_waste = 0
        for gi, g in enumerate(self.groups):
            waste = sum(r['size'] for r in g[1:])
            total_waste += waste
            self.group_tree.insert('', 'end', iid=str(gi),
                values=(f'Group {gi+1} ({len(g)} files)', f'{waste/1e6:,.1f}'))
        self.dupe_summary.config(
            text=f'{len(self.groups)} duplicate groups — {total_waste/1e9:.2f} GB redundant')
        self.decisions.clear()
        self.status_var.set(f'Loaded {len(self.groups)} duplicate groups.')
        self._update_marked_count()  # decisions were just cleared

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
            img = self.thumbs.get(r['path'])
            lbl = ttk.Label(row, image=img)
            lbl.image = img  # keep alive even if the LRU cache evicts it
            lbl.pack(side='left')
            info = ttk.Frame(row)
            info.pack(side='left', fill='x', expand=True, padx=8)
            name = r['path']
            ttk.Label(info, text=name, wraplength=520).pack(anchor='w')
            dur = f"{int(r['duration']//60)}:{int(r['duration']%60):02d}" if r['duration'] else '?'
            # bytes/sec * 8 = bits/sec; label honestly in megabits
            br = (r['size'] / r['duration']) * 8 if r.get('duration') else 0
            br_str = f"{br/1e6:.2f} Mbps" if br else '?'
            ttk.Label(info, text=(
                f"{r['width']}x{r['height']}  {r['size']/1e6:,.1f} MB  {br_str}  {dur}  "
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
        self._update_marked_count()  # refresh count when revisiting a group

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
            messagebox.showinfo('VidSweep', 'No groups loaded — scan first.')
            return
        n = 0
        for g in self.groups:
            for i, r in enumerate(g):
                v = self.decisions.get(r['path'])
                if v is None:
                    # group never opened: create the decision now so EXECUTE sees it
                    v = tk.StringVar(value='keep' if i == 0 else 'delete')
                    v.trace_add('write', lambda *a: self._update_marked_count())
                    self.decisions[r['path']] = v
                else:
                    v.set('keep' if i == 0 else 'delete')
                if i > 0:
                    n += 1
        self._update_marked_count()
        messagebox.showinfo('VidSweep',
                            f'Marked {n} files for deletion across ALL {len(self.groups)} groups '
                            '(best copy kept per group).\n'
                            'Review if you like, then click EXECUTE.')

    def _keep_best_current_group(self):
        sel = self.group_tree.selection()
        if not sel:
            messagebox.showinfo('VidSweep', 'Select a group first.')
            return
        g = self.groups[int(sel[0])]
        for i, r in enumerate(g):
            v = self.decisions.get(r['path'])
            if v is None:
                v = tk.StringVar(value='keep' if i == 0 else 'delete')
                v.trace_add('write', lambda *a: self._update_marked_count())
                self.decisions[r['path']] = v
            else:
                v.set('keep' if i == 0 else 'delete')
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
            messagebox.showinfo('VidSweep', 'Nothing marked. Mark files with Keep/Delete/Move first.')
            return
        # transparent confirmation: list EVERY file, since marking accumulates across groups
        lines = []
        secure = self.privacy.get('secure_delete')
        if to_delete:
            if secure:
                # honest label: secure delete overrides the dropdown entirely
                lines.append(f"DELETE {len(to_delete)} file(s) — SECURE DELETE "
                             "(permanent overwrite; Recycle Bin is bypassed):")
            else:
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
                messagebox.showerror('VidSweep', 'send2trash not installed. Run: pip install send2trash')
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
                base, ext = os.path.splitext(os.path.basename(p))
                dest = os.path.join(dest_dir, base + ext)
                k = 1
                # never overwrite: same-name collisions get a numbered suffix
                while os.path.exists(dest):
                    dest = os.path.join(dest_dir, f'{base}_{k}{ext}')
                    k += 1
                shutil.move(p, dest)
                self.org.db.execute('UPDATE files SET path=? WHERE path=?',
                                    (dest, p))
                self.org.db.commit()
                moved += 1
            except Exception as e:
                failed += 1
                self.log_line(f'FAILED move {p}: {e}')
        self.org.db.commit()
        messagebox.showinfo('VidSweep',
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
            messagebox.showwarning('VidSweep', 'Pick a valid source folder.')
            return
        src_norm = os.path.normcase(os.path.abspath(src))
        all_paths = [r[0] for r in self.org.db.execute(
            'SELECT path FROM files')]
        # only DB rows actually inside the chosen source folder (normalized
        # compare: case/separator-insensitive on Windows)
        paths = [p for p in all_paths
                 if os.path.normcase(os.path.abspath(p)).startswith(src_norm + os.sep)]
        # fall back to disk listing if db is empty for this folder
        if not paths:
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
        messagebox.showinfo('VidSweep',
                            f'{len(sugg)} videos analyzed, {len(self.org_moves)} would move. '
                            'Review the list, then Preview moves.')

    def preview_moves(self):
        conflicts = [(p, d) for p, _c, d in self.org_moves if os.path.exists(d)]
        if conflicts:
            if not messagebox.askyesno(
                    'VidSweep',
                    f'{len(conflicts)} destination name(s) already exist — '
                    'those files will be renamed with a suffix. Continue?'):
                return
        self.org_apply_btn.config(state='normal')
        messagebox.showinfo('VidSweep',
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
        messagebox.showinfo('VidSweep', f'Moved {done}, failed {failed}.')
        self.org_apply_btn.config(state='disabled')


def main():
    try:
        app = App()
        app.mainloop()
    except Exception:
        import logging
        log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'crash.log')
        logging.basicConfig(filename=log_path, level=logging.ERROR)
        logging.exception('VidSweep crashed')
        # also show it in a console if one is attached
        import traceback
        traceback.print_exc()
        try:
            import tkinter.messagebox as mb
            mb.showerror('VidSweep — error',
                         f'Startup failed. Details written to:\n{log_path}\n\n'
                         f'{traceback.format_exc()[-800:]}')
        except Exception:
            pass
        raise


if __name__ == '__main__':
    main()
