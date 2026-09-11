"""
VidSweep — GUI (tkinter).
Tabs:
  1. Scan      — pick folders, run the fingerprint pipeline with live progress
  2. Duplicates— groups with thumbnails, keep/decide each file, act on groups
  3. Organize  — name-based category suggestions, preview moves, apply
"""

import csv
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from PIL import Image, ImageTk
import io

import core

APP_DIR = os.path.dirname(os.path.abspath(__file__))


# ---- multi-select folder picker --------------------------------------------
def _pick_folders_win32(owner_hwnd):
    """Native Windows folder picker with multi-select, via the COM
    IFileOpenDialog (the same dialog Explorer uses, in folder mode).

    Returns a list of folder paths, [] if the user cancelled, or None when
    the COM dialog is unavailable or fails — the caller then falls back to
    tkinter's single-folder chooser. Pure stdlib (ctypes), no pywin32.
    """
    import ctypes
    from ctypes import POINTER, byref, cast, c_void_p, c_wchar_p
    from ctypes import c_ulong, c_ushort, c_ubyte, c_uint32

    class GUID(ctypes.Structure):
        _fields_ = [('Data1', c_ulong), ('Data2', c_ushort),
                    ('Data3', c_ushort), ('Data4', c_ubyte * 8)]

    def make_guid(s):
        p = s.strip('{}').split('-')
        return GUID(int(p[0], 16), int(p[1], 16), int(p[2], 16),
                    (c_ubyte * 8)(*bytes.fromhex(p[3] + p[4])))

    CLSID_FileOpenDialog = make_guid('DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7')
    IID_IFileOpenDialog = make_guid('D57C7288-D4AD-4768-BE02-9D969532D960')
    SIGDN_FILESYSPATH = 0x80058000
    # FOS_NOCHANGEDIR | FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM | FOS_ALLOWMULTISELECT
    FOS_OPTIONS = 0x8 | 0x20 | 0x40 | 0x200
    HR_CANCELLED = 0x800704C7  # HRESULT_FROM_WIN32(ERROR_CANCELLED)

    ole32 = ctypes.oledll.ole32

    def vtbl_method(obj, index, *argtypes, restype=ctypes.HRESULT):
        """COM method at vtable slot `index` of an interface pointer.
        The callable still expects the interface pointer as first arg."""
        table_addr = cast(obj, POINTER(c_void_p)).contents.value
        entry = cast(c_void_p(table_addr + index * ctypes.sizeof(c_void_p)),
                     POINTER(c_void_p)).contents.value
        return ctypes.WINFUNCTYPE(restype, *argtypes)(entry)

    initialized = False
    try:
        try:
            ole32.CoInitializeEx(None, 0x2)  # COINIT_APARTMENTTHREADED
            initialized = True
        except OSError:
            pass  # COM already initialized (possibly a different model) — usable

        # the dialog owner must be a TOP-LEVEL window; Tk's winfo_id() returns
        # an inner child, so walk up to the root ancestor
        user32 = ctypes.windll.user32
        top = user32.GetAncestor(owner_hwnd, 2)  # GA_ROOT
        if top:
            owner_hwnd = top

        dlg = c_void_p()
        ole32.CoCreateInstance(byref(CLSID_FileOpenDialog), None, 1,  # INPROC_SERVER
                               byref(IID_IFileOpenDialog), byref(dlg))
        # vtable: 0-2 IUnknown, 3 Show, 4-26 IFileDialog (9 SetOptions,
        # 17 SetTitle), 27 GetResults (IFileOpenDialog)
        vtbl_method(dlg, 9, c_void_p, c_uint32)(dlg, FOS_OPTIONS)
        vtbl_method(dlg, 17, c_void_p, c_wchar_p)(
            dlg, 'Choose one or more folders containing videos')
        try:
            hr = vtbl_method(dlg, 3, c_void_p, c_void_p)(dlg, owner_hwnd)
        except OSError as e:
            # oledll raises failed HRESULTs; a user cancel must return []
            if getattr(e, 'winerror', 0) & 0xFFFFFFFF == HR_CANCELLED:
                return []
            return None
        if hr != 0:
            return None  # dialog failed — let the caller fall back

        results = c_void_p()
        vtbl_method(dlg, 27, c_void_p, POINTER(c_void_p))(dlg, byref(results))
        count = c_uint32()
        vtbl_method(results, 7, c_void_p, POINTER(c_uint32))(results, byref(count))
        get_item_at = vtbl_method(results, 8, c_void_p, c_uint32, POINTER(c_void_p))

        paths = []
        for i in range(count.value):
            item = c_void_p()
            get_item_at(results, i, byref(item))
            buf = c_void_p()
            # 5 = IShellItem.GetDisplayName
            vtbl_method(item, 5, c_void_p, c_uint32, POINTER(c_void_p))(
                item, SIGDN_FILESYSPATH, byref(buf))
            if buf:
                paths.append(cast(buf, c_wchar_p).value)
                ole32.CoTaskMemFree(buf)
            vtbl_method(item, 2, c_void_p)(item)  # Release
        vtbl_method(results, 2, c_void_p)(results)   # Release
        vtbl_method(dlg, 2, c_void_p)(dlg)           # Release
        return paths
    except Exception:
        return None
    finally:
        if initialized:
            try:
                ole32.CoUninitialize()
            except Exception:
                pass

# ---- theme palettes ------------------------------------------------------
# Every color used by custom-painted surfaces lives here; ttk styles and the
# dialogs read from one dict so light/dark stay consistent.
THEMES = {
    'light': {
        'bg': '#f8fafc', 'fg': '#1e293b', 'muted': '#64748b',
        'card': '#ffffff', 'card2': '#f1f5f9', 'hover': '#e2e8f0',
        'border': '#cbd5e1', 'select': '#dbeafe', 'select_fg': '#1e293b',
        'accent': '#2563eb', 'tab_fg': '#334155',
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



def export_snapshot_to_csv(snapshot, *, ask_path, export, info, error):
    """Export an already-selected snapshot of groups to CSV.

    The SNAPSHOT is what gets exported and what the messages count: taking it once
    means the file and the reported counts cannot drift apart if the view reloads
    mid-export. Cancellation is a silent no-op BEFORE the exporter is called.
    A failure is surfaced with the exporter's own message and no claim is made about
    what state the destination file is in — the engine promises overwrite and a
    visible failure, not an atomic write.

    Returns a small result dict (used by the tests).
    """
    groups = list(snapshot or [])
    if not groups:
        info('VidSweep', 'No groups are currently shown — scan first.')
        return {'exported': False, 'reason': 'nothing_shown'}
    path = ask_path()
    if not path:                      # user cancelled: no file is touched
        return {'exported': False, 'reason': 'cancelled'}
    rows = sum(len(grp) for grp in groups)
    try:
        export(groups, path)
    except Exception as exc:          # surfaced, never swallowed
        error('VidSweep', f'Could not write the CSV:\n{exc}')
        return {'exported': False, 'reason': 'error', 'error': str(exc), 'path': path}
    info('VidSweep', f'Exported {len(groups)} group(s) ({rows} file row(s)) to:\n{path}')
    return {'exported': True, 'groups': len(groups), 'rows': rows, 'path': path}


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
    """Loads JPEG blobs from db, decodes to PhotoImage, LRU-cached per size."""

    def __init__(self, organizer, size=(160, 90), max_items=200):
        self.org = organizer
        self.size = size
        self._cache = OrderedDict()  # (path, height) -> PhotoImage, LRU order
        self._missing = {}  # height -> gray placeholder
        self._max_items = max_items

    def get(self, path, height=None):
        """Blocking one-shot: PhotoImage for `path` scaled to `height`
        (decodes synchronously — prefer cached()/placeholder()/render_pil()/
        store() so the UI thread never blocks on JPEG decodes)."""
        h = int(height) if height else self.size[1]
        img = self.cached(path, h)
        if img is not None:
            return img
        return self.store(path, h, self.render_pil(path, h))

    def cached(self, path, height):
        key = (path, int(height))
        if key in self._cache:
            self._cache.move_to_end(key)  # mark most-recently-used
            return self._cache[key]
        return None

    def placeholder(self, height):
        """Gray 16:9 box shown while a thumbnail decodes off-thread."""
        h = int(height)
        ph = self._missing.get(h)
        if ph is None:
            ph = ImageTk.PhotoImage(
                Image.new('RGB', (16 * h // 9, h), (60, 60, 60)))
            self._missing[h] = ph
        return ph

    def render_pil(self, path, height):
        """Decode + resize off the main thread. Returns PIL.Image or None.
        Thread-safe: touches only the DB read and PIL, never Tk."""
        try:
            blob = self.org.get_thumbnail(path)  # internally _db_lock-protected
        except Exception:
            return None
        if not blob:
            return None
        try:
            pil = Image.open(io.BytesIO(blob))
            pil.thumbnail((10000, int(height)))
            return pil
        except Exception:
            return None

    def store(self, path, height, pil):
        """Create the PhotoImage on the main thread (Tk requirement) and
        cache it. Returns None when there is nothing to show."""
        if pil is None:
            return None
        key = (path, int(height))
        img = self._cache.get(key)
        if img is not None:
            self._cache.move_to_end(key)
            return img
        try:
            img = ImageTk.PhotoImage(pil)
        except Exception:
            return None
        self._cache[key] = img
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
        self.current_session_id = None  # scan batch shown on the Duplicates tab
        self._thumb_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='thumb')
        self._detail_gen = 0  # bumped per group selection; stale decodes dropped
        self.privacy = self._load_privacy()
        self.theme_name = self._load_theme_name()
        self.theme = THEMES[self.theme_name]
        self.protocol('WM_DELETE_WINDOW', self._on_close)
        self._apply_theme()
        self._build_ui()
        # an unfinished (paused/stopped) scan must survive closing the app or
        # rebooting the PC: offer to continue it shortly after launch
        self.after(400, self._offer_resume_on_startup)

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
        style.map('TButton',
                  background=[('active', c['hover']), ('pressed', c['accent']), ('disabled', c['card'])],
                  foreground=[('active', c['fg']), ('pressed', c['fg']), ('disabled', c['muted'])])
        style.configure('TCheckbutton', background=c['bg'],
                        foreground=c['fg'], focuscolor=c['accent'])
        style.map('TCheckbutton',
                  background=[('active', c['hover']), ('pressed', c['hover']), ('disabled', c['bg'])],
                  foreground=[('active', c['fg']), ('pressed', c['fg']), ('disabled', c['muted'])])
        style.configure('TRadiobutton', background=c['bg'],
                        foreground=c['fg'], focuscolor=c['accent'])
        style.map('TRadiobutton',
                  background=[('active', c['hover']), ('pressed', c['hover']), ('disabled', c['bg'])],
                  foreground=[('active', c['fg']), ('pressed', c['fg']), ('disabled', c['muted'])])
        style.configure('TEntry', fieldbackground=c['card'],
                        foreground=c['fg'], insertbackground=c['fg'])
        # Treeview (duplicate groups list): clam keeps a white field by
        # default, which left palette text invisible — style it explicitly
        style.configure('Treeview', background=c['card'],
                        fieldbackground=c['card'], foreground=c['fg'],
                        bordercolor=c['border'], lightcolor=c['card'],
                        darkcolor=c['card'])
        style.configure('Treeview.Heading', background=c['card2'],
                        foreground=c['fg'], lightcolor=c['card2'],
                        darkcolor=c['card2'], bordercolor=c['border'])
        style.map('Treeview',
                  background=[('selected', c['select'])],
                  foreground=[('selected', c['select_fg'])])
        style.map('Treeview.Heading', lightcolor=[('active', c['hover'])],
                  darkcolor=[('active', c['hover'])])
        # Combobox (delete/execute box): the closed, readonly field also kept
        # clam's white background — map every state to the palette
        style.configure('TCombobox', fieldbackground=c['card'],
                        background=c['card2'], foreground=c['fg'],
                        arrowcolor=c['fg'], insertbackground=c['fg'],
                        bordercolor=c['border'], lightcolor=c['card'],
                        darkcolor=c['card'])
        style.map('TCombobox',
                  fieldbackground=[('readonly', c['card']), ('disabled', c['bg'])],
                  foreground=[('readonly', c['fg']), ('disabled', c['muted'])],
                  selectbackground=[('readonly', c['card'])],
                  selectforeground=[('readonly', c['fg'])])
        # the dropdown list of every combobox (separate classic listbox)
        self.option_add('*TCombobox*Listbox.background', c['card'])
        self.option_add('*TCombobox*Listbox.foreground', c['fg'])
        self.option_add('*TCombobox*Listbox.selectBackground', c['select'])
        self.option_add('*TCombobox*Listbox.selectForeground', c['select_fg'])
        style.configure('TProgressbar', background=c['accent'],
                        troughcolor=c['card2'], bordercolor=c['bg'])
        # scrollbars: clam paints the trough with lightcolor/darkcolor
        # gradients that ignore the palette and look like a pale stripe on
        # dark mode — pin every element to theme colors for a flat look
        for sb in ('Vertical.TScrollbar', 'Horizontal.TScrollbar'):
            style.configure(sb, background=c['card2'], troughcolor=c['card'],
                            bordercolor=c['border'], arrowcolor=c['fg'],
                            lightcolor=c['card2'], darkcolor=c['card2'])
            style.map(sb,
                      background=[('active', c['hover']), ('pressed', c['select'])],
                      lightcolor=[('active', c['hover'])],
                      darkcolor=[('active', c['hover'])])

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
                    'A scan is still running.\n\n'
                    'Its progress is saved — after you restart, VidSweep '
                    'will offer to resume it.\n\n'
                    'Stop it and exit now?'):
                return
            self.org.cancel()
            self._scan_thread.join(timeout=10)
        # Always close the DB connection so a relaunch doesn't hit
        # "database is locked" from the previous process's unclosed handle.
        try:
            self.org.close()
        except Exception:
            pass
        # Shut down the thumbnail worker pool so its non-daemon threads
        # don't keep the process alive after the window closes.
        try:
            self._thumb_pool.shutdown(wait=False)
        except Exception:
            pass
        if self.privacy.get('wipe_db_on_exit'):
            try:
                for ext in ('', '-wal', '-shm'):
                    p = self.org.db_path + ext
                    if os.path.isfile(p):
                        os.remove(p)
                # the sidecar remembers the unfinished scan — wiping the DB
                # must wipe it too (privacy: nothing about the library stays)
                sp = self.org.scan_state_path()
                if os.path.isfile(sp):
                    os.remove(sp)
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
             'The "Open" button (and clicking a thumbnail) launches MPC-HC with\n'
             'history disabled when it is installed. If MPC-HC is not found,\n'
             'your system default player opens the file instead (that player\'s\n'
             'own history behavior then applies).'),
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
        self.resume_btn = ttk.Button(run, text='Resume last scan',
                                     command=self.resume_scan)
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

        # an unfinished scan session (stopped mid-way) offers a one-click resume
        self._refresh_resume_btn()

    def _refresh_resume_btn(self):
        """Show the Resume button only while an active scan session exists."""
        try:
            sess = self.org.get_last_session()
        except Exception:
            sess = None
        if sess and sess.get('active') and sess.get('roots'):
            self.current_session_id = sess['id']
            total = sess.get('total') or 0
            if sess.get('paused'):
                label = (f"Resume paused scan "
                         f"({sess['done']} of {total or '?'} videos)")
            else:
                label = f"Resume last scan ({sess['done']} files fingerprinted)"
            self.resume_btn.config(text=label)
            try:
                self.resume_btn.pack(side='left', padx=6)
            except tk.TclError:
                pass
        else:
            try:
                self.resume_btn.pack_forget()
            except tk.TclError:
                pass

    def _offer_resume_on_startup(self):
        """A paused/stopped scan must survive closing the app or a full PC
        restart: shortly after launch, find the unfinished session and offer
        to continue it in one click."""
        try:
            sess = self.org.get_last_session()
        except Exception:
            sess = None
        if sess and sess.get('active') and sess.get('done'):
            done, total = sess['done'], (sess.get('total') or 0)
            state_txt = ('was PAUSED' if sess.get('paused')
                         else 'was stopped unfinished')
            preview = ', '.join(sess['roots'][:3])
            if len(sess['roots']) > 3:
                preview += f' … (+{len(sess["roots"]) - 3} more)'
            if messagebox.askyesno(
                    'VidSweep — resume scan?',
                    f'An unfinished scan was found (it {state_txt}).\n\n'
                    f'{done} of {total or "?"} videos fingerprinted so far.\n'
                    f'Folders: {preview}\n\n'
                    'Resume it now?'):
                self.resume_scan()
            else:
                self.log_line(
                    f'Unfinished scan kept: {done} of {total or "?"} files '
                    'done. Click "Resume" on the Scan tab to continue it.')
                self.status_var.set(
                    'Unfinished scan found — use the Resume button to continue.')
            return
        # library.db sometimes comes back unreadable after a hard shutdown on
        # the external-drive bridge (then gets recreated empty). The sidecar
        # JSON still remembers the unfinished scan — offer a restart over the
        # same folders so a long scan is never silently lost.
        state = None
        try:
            p = self.org.scan_state_path()
            if os.path.isfile(p):
                with open(p) as fh:
                    state = json.load(fh)
        except Exception:
            state = None
        if not (state and state.get('status') == 'active' and state.get('done')):
            return
        roots = [r for r in (state.get('roots') or []) if os.path.isdir(r)]
        if not roots:
            return
        if messagebox.askyesno(
                'VidSweep — scan interrupted',
                f"An unfinished scan ({state.get('done')} of "
                f"{state.get('total') or '?'} videos fingerprinted) was "
                'interrupted, and its fingerprint cache could not be '
                'recovered.\n\n'
                'Start the scan again over the same folders?\n'
                + ', '.join(roots[:3])):
            self.folder_list.delete(0, 'end')
            for r in roots:
                self.folder_list.insert('end', r)
            self.recursive_var.set(bool(state.get('recursive', True)))
            self._save_settings()
            self.start_scan()

    def add_folder(self):
        folders = self._pick_folders()
        if not folders:
            return
        existing = {os.path.normcase(p)
                    for p in self.folder_list.get(0, 'end')}
        added = 0
        for p in folders:
            p = os.path.normpath(p)
            if os.path.normcase(p) in existing:
                continue  # already in the list — don't add twice
            existing.add(os.path.normcase(p))
            self.folder_list.insert('end', p)
            added += 1
        if added:
            self._save_settings()
            self.status_var.set(f'Added {added} folder(s).')
        else:
            messagebox.showinfo('VidSweep', 'That folder is already in the list.')

    def _pick_folders(self):
        """Multi-select folder chooser. Uses the native Windows dialog when
        possible; falls back to tkinter's single-folder chooser elsewhere or
        if the COM dialog is unavailable."""
        if os.name == 'nt':
            folders = _pick_folders_win32(self.winfo_id())
            if folders is not None:
                return folders
        d = filedialog.askdirectory(title='Choose folder containing videos')
        return [d] if d else []

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
        if getattr(self, 'fast_var', None) is not None:
            data['fast_match'] = bool(self.fast_var.get())
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
        self._launch_scan(folders, self.recursive_var.get())

    def resume_scan(self):
        """Continue the last stopped scan: same folders, same batch."""
        try:
            sess = self.org.get_last_session()
        except Exception:
            sess = None
        if not sess or not sess.get('active'):
            messagebox.showinfo('VidSweep', 'No unfinished scan to resume.')
            self._refresh_resume_btn()
            return
        roots = [r for r in (sess.get('roots') or []) if os.path.isdir(r)]
        if not roots:
            messagebox.showwarning(
                'VidSweep',
                'The folders from the last scan no longer exist.\n'
                'Start a new scan instead.')
            return
        # reflect the resumed session's folders in the UI
        self.folder_list.delete(0, 'end')
        for r in sess['roots']:
            self.folder_list.insert('end', r)
        self.recursive_var.set(sess['recursive'])
        self._save_settings()
        self._launch_scan(roots, sess['recursive'], session_id=sess['id'])

    def _launch_scan(self, folders, recursive, session_id=None):
        self.current_session_id = session_id  # None: set from stats when done
        self.scan_btn.config(state='disabled')
        self.resume_btn.config(state='disabled')
        self.cancel_btn.config(state='normal')
        self.pause_btn.config(state='normal')
        self.progress.config(value=0)
        self.status_var.set('Scanning…')
        self._scan_thread = threading.Thread(
            target=self._scan_worker,
            args=(folders, recursive, session_id), daemon=True)
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
            sp = self.org.scan_state_path()
            if os.path.isfile(sp):
                os.remove(sp)
        except Exception as e:
            messagebox.showerror('VidSweep', f'Could not delete database:\n{e}')
            return
        # rebuild a fresh, empty library
        self.org = core.VideoOrganizer(db_path=db_path)
        self.thumbs = ThumbnailCache(self.org)
        self.groups = []
        self.current_session_id = None
        self._refresh_resume_btn()
        self.groups = []
        self.decisions.clear()
        for iid in self.group_tree.get_children():
            self.group_tree.delete(iid)
        self.dupe_summary.config(text='')
        self._update_marked_count()
        self.log_line('Library reset — database deleted and recreated empty.')
        self.status_var.set('Library reset. Ready for a fresh scan.')
        messagebox.showinfo('VidSweep', 'Library deleted and recreated empty.')

    def _scan_worker(self, folders, recursive, session_id=None):
        def post(fn):
            """Run fn on the main thread; fall back to direct call when no
            Tk main loop is running (headless tests / scripted drivers)."""
            try:
                self.after(0, fn)
            except (RuntimeError, tk.TclError):
                try:
                    fn()  # mainloop-less context: apply directly
                except (RuntimeError, tk.TclError):
                    pass  # window truly gone: drop the update
        def progress(phase, done, total, cur):
            pct = (done / total * 100) if total else 0
            try:
                self.after(0, self._update_progress, phase, done, total, pct, cur)
            except (RuntimeError, tk.TclError):
                try:
                    self._update_progress(phase, done, total, pct, cur)
                except (RuntimeError, tk.TclError):
                    pass  # window closed mid-scan: keep scanning, skip UI
        try:
            stats = self.org.scan(folders, recursive=recursive,
                                  progress=progress, session_id=session_id)
        except core.Cancelled:
            post(lambda: self._scan_done(cancelled=True))
            return
        except Exception as e:
            err = str(e)
            post(lambda: self._scan_done(error=err))
            return
        post(lambda: self._scan_done(stats=stats))

    def _update_progress(self, phase, done, total, pct, cur):
        labels = {'scan': 'Finding files', 'hashing': 'Exact hashing',
                  'perceptual': 'Frames & perceptual hashes',
                  'working': 'Hashing + fingerprints'}
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
            # Everything fingerprinted before the stop is already committed;
            # surface it immediately instead of waiting for a manual refresh.
            done = 0
            try:
                done = self.org.get_last_session()['done']
            except Exception:
                pass
            self.status_var.set(
                f'Scan stopped — {done} videos fingerprinted so far. '
                'Resume anytime from the Scan tab.')
            self.log_line(
                f'Scan stopped by user — {done} files fingerprinted; '
                'partial batch loaded on the Duplicates tab.')
            self._refresh_resume_btn()
            if getattr(self, 'dupes_scope_var', None) is not None:
                self.dupes_scope_var.set('batch')
            self.load_groups()
            return
        self.current_session_id = stats.get('session_id')
        if stats.get('cache_migrated'):
            self.log_line(
                'The library cache kept getting corrupted on its old drive '
                f'and has moved to: {stats["cache_migrated"]}\n'
                'Future scans use the new location automatically.')
        self.status_var.set(
            f"Done in {stats['elapsed']}s — {stats['scanned']} videos found, "
            f"{stats['processed']} processed, {stats['skipped_cached']} cached, "
            f"{stats.get('reused_identical', 0)} reused from identical files, "
            f"{stats.get('pruned', 0)} stale entries cleaned, "
            f"{stats['errors']} errors.")
        self.log_line(f"Scan complete: {stats}")
        self._refresh_resume_btn()
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
        # scope: match only the current scan batch (partial scans, resume) or
        # everything ever fingerprinted. Defaults to batch when a stopped scan
        # session exists.
        ttk.Label(top, text='Match:').pack(side='left', padx=(12, 2))
        self.dupes_scope_var = tk.StringVar(
            value='batch' if getattr(self, 'current_session_id', None) else 'library')
        ttk.Radiobutton(top, text='Current scan batch', value='batch',
                        variable=self.dupes_scope_var).pack(side='left')
        ttk.Radiobutton(top, text='Entire library', value='library',
                        variable=self.dupes_scope_var).pack(side='left', padx=(2, 6))
        # banding-based candidate generation: near-instant matching on huge
        # libraries; off = the exact exhaustive comparison (slower at scale)
        self.fast_var = tk.BooleanVar(value=bool(
            self._load_settings_full().get('fast_match', True)))
        ttk.Checkbutton(top, text='Fast match (large libraries)',
                        variable=self.fast_var).pack(side='left')
        ttk.Button(top, text='Dismiss all shown groups',
                   command=self.dismiss_all_shown).pack(side='left', padx=6)
        ttk.Button(top, text='Reset dismissed groups',
                   command=self.reset_dismissed_groups).pack(side='left', padx=6)
        ttk.Button(top, text='Export shown groups to CSV…',
                   command=self.export_shown_groups).pack(side='left', padx=6)

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
        ttk.Button(action, text='Keep ALL groups: mark all as keep',
                   command=self.mark_all_keep_all).pack(side='left', padx=4)
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
        ttk.Button(right, text='Dismiss (not duplicates)',
                   command=self.dismiss_current_group).pack(anchor='w', pady=2)
        canvas_frame = ttk.Frame(right)
        canvas_frame.pack(fill='both', expand=True)
        self.detail_canvas = tk.Canvas(canvas_frame, highlightthickness=0,
                                       bg=self.theme['bg'])
        dsb = ttk.Scrollbar(canvas_frame, orient='vertical', command=self.detail_canvas.yview)
        self.detail_inner = ttk.Frame(self.detail_canvas)
        self.detail_inner.bind('<Configure>',
            lambda e: self.detail_canvas.configure(scrollregion=self.detail_canvas.bbox('all')))
        self._detail_window = self.detail_canvas.create_window(
            (0, 0), window=self.detail_inner, anchor='nw')
        # keep the inner frame exactly as wide as the canvas: content always
        # spans the pane and the scroll range matches the real content height
        self.detail_canvas.bind('<Configure>', self._on_detail_configure)
        self.detail_canvas.configure(yscrollcommand=dsb.set)
        self.detail_canvas.pack(side='left', fill='both', expand=True)
        dsb.pack(side='right', fill='y')
        # wheel scrolls ONLY the detail pane (bound per-widget below), never
        # via bind_all — a global binding made every wheel tick anywhere in
        # the app scroll this box
        self.detail_canvas.bind('<MouseWheel>', self._on_detail_wheel)
        paned.add(right, weight=3)
        # keyboard triage: active only on this tab (gated in _on_review_key)
        self._detail_rows = []
        self._detail_index = -1
        for seq in ('<Key-k>', '<Key-d>', '<Key-m>',
                    '<Key-K>', '<Key-D>', '<Key-M>',
                    '<Return>', '<Up>', '<Down>', '<Left>', '<Right>'):
            self.bind(seq, self._on_review_key)

    def load_groups(self):
        # Run the CPU-bound comparison in a background thread so the UI
        # stays responsive; results are applied back on the main thread.
        self.refresh_btn.config(state='disabled')
        # read tk variables on the main thread ONLY — touching Tcl state from
        # the worker thread can kill the interpreter (silent exit(1))
        threshold = self.sens_var.get()
        fast = True
        if getattr(self, 'fast_var', None) is not None:
            fast = bool(self.fast_var.get())
        scope = getattr(self, 'dupes_scope_var', None)
        batch = scope is not None and scope.get() == 'batch'
        session_id = self.current_session_id if batch else None
        # an id of None means "no batch selected yet" — fall back to the
        # whole library so the tab is never silently empty
        self._load_scope_txt = ('current scan batch' if session_id is not None
                                else 'entire library')
        self.status_var.set('Loading duplicate groups…')
        self.update_idletasks()
        self._load_result = None  # set by worker, consumed by _poll_load_result
        self._load_progress = None  # live (done, total) from the match loop

        def _worker():
            def lprog(phase, done, total, cur):
                self._load_progress = (done, total)
            try:
                groups = self.org.find_duplicates(
                    threshold=threshold, session_id=session_id,
                    fast_match=fast, progress=lprog)
                self._load_result = (groups, None)
            except Exception as e:
                self._load_result = (None, str(e))

        threading.Thread(target=_worker, daemon=True).start()
        self._poll_load_result()

    def _poll_load_result(self):
        res = getattr(self, '_load_result', None)
        if res is None:
            lp = getattr(self, '_load_progress', None)
            if lp:
                self.status_var.set(f'Matching duplicates… {lp[0]}/{lp[1]}')
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
            text=f'{len(self.groups)} duplicate groups — '
                 f'{total_waste/1e9:.2f} GB redundant '
                 f'[{getattr(self, "_load_scope_txt", "entire library")}]')
        self.decisions.clear()
        self.status_var.set(f'Loaded {len(self.groups)} duplicate groups.')
        self._update_marked_count()  # decisions were just cleared

    def _detail_thumb_height(self, n_files):
        """Thumbnail height sized to the group: few files -> big thumbnails,
        many files -> smaller ones so more rows fit before scrolling starts.
        Heights are quantized to steps so re-renders reuse the cache and
        window resizes produce visible jumps instead of 1px changes. Above
        144 the stored 256x144 frames are upscaled for display only (nothing
        extra is stored) — big but slightly soft beyond that point."""
        pane_h = self.detail_canvas.winfo_height()
        if pane_h <= 1:
            pane_h = 500  # not laid out yet (headless / first render)
        # ~90px of text+controls per row plus padding; whatever is left of
        # the pane is the thumbnail.
        fit_h = int((pane_h - 90) / max(1, n_files)) - 10
        h = 54
        for step in (54, 66, 80, 96, 116, 144, 180, 240):
            if step <= fit_h:
                h = step
        return h

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
        thumb_h = self._detail_thumb_height(len(g))
        self._detail_thumb_rendered = thumb_h
        # decode thumbnails off the UI thread: rows appear instantly with a
        # gray placeholder, images pop in as they finish. A generation counter
        # drops results for a group the user has already clicked away from.
        self._detail_gen += 1
        gen = self._detail_gen
        rows = []
        for i, r in enumerate(g):
            row = ttk.Frame(self.detail_inner)
            row.pack(fill='x', pady=4, padx=4)
            marker = ttk.Label(row, text='', foreground=self.theme['accent'])
            marker.pack(side='left')
            img = self.thumbs.cached(r['path'], thumb_h)
            needs_decode = img is None
            if needs_decode:
                img = self.thumbs.placeholder(thumb_h)
            lbl = ttk.Label(row, image=img, cursor='hand2')
            lbl.image = img  # keep alive even if the LRU cache evicts it
            lbl.pack(side='left')
            # clicking the thumbnail opens the video, same as the Open button
            lbl.bind('<Button-1>',
                     lambda e, p=r['path']: self._open_file(p))
            if needs_decode:
                self._thumb_pool.submit(self._decode_thumb, r['path'],
                                        thumb_h, gen, lbl)
            rows.append((row, marker, r['path']))
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
        # wheel binding must cover the freshly built rows (each rebuild
        # replaces the children); scrollregion + view follow the new content
        self._bind_wheel_recursive(self.detail_inner)
        self.detail_inner.update_idletasks()
        self.detail_canvas.configure(scrollregion=self.detail_canvas.bbox('all'))
        self.detail_canvas.yview_moveto(0)  # every group starts at the top
        self._detail_rows = rows
        self._set_detail_index(0 if rows else -1)  # keyboard cursor at top
        self._update_marked_count()  # refresh count when revisiting a group

    def _set_detail_index(self, i):
        """Move the keyboard cursor (▶ marker) to file i, keeping it visible."""
        self._detail_index = i
        for idx, (row, marker, _path) in enumerate(self._detail_rows):
            marker.config(text='▶' if idx == i else '')
        if not (0 <= i < len(self._detail_rows)):
            return
        row = self._detail_rows[i][0]
        try:
            # winfo_y is relative to detail_inner = canvas content coordinates
            y = row.winfo_y()
            h = row.winfo_height()
            view_h = self.detail_canvas.winfo_height() or 1
            if h <= 1 or h >= view_h:
                return  # not laid out yet, or row taller than the pane
            if y < 0:  # above the viewport — scroll up to it
                box = self.detail_canvas.bbox('all')
                content_h = max(1, (box[3] - box[1]) if box else 1)
                self.detail_canvas.yview_moveto(max(0.0, y / content_h))
            elif y + h > view_h:  # below — scroll down to it
                box = self.detail_canvas.bbox('all')
                content_h = max(1, (box[3] - box[1]) if box else 1)
                frac = (y + h - view_h) / content_h
                self.detail_canvas.yview_moveto(min(1.0, max(0.0, frac)))
        except (RuntimeError, tk.TclError):
            pass  # window gone / not laid out yet

    def _review_keys_active(self):
        """Keyboard review keys only act on the Duplicates tab and never
        while typing in a text field (quarantine path box etc.)."""
        try:
            if self.nb.select() != str(self.tab_dupes):
                return False
            w = self.focus_get()
        except (RuntimeError, tk.TclError):
            return False
        return not isinstance(w, (tk.Entry, ttk.Entry, ttk.Combobox, tk.Text))

    def _on_review_key(self, e):
        if not self._review_keys_active():
            return
        key = e.keysym.lower()
        if key in ('up', 'down'):
            if self.focus_get() is self.group_tree:
                return  # tree has focus: its own Up/Down walks the groups
            if self._detail_rows:
                step = 1 if key == 'down' else -1
                self._set_detail_index(max(0, min(len(self._detail_rows) - 1,
                                                  self._detail_index + step)))
            return 'break'
        if key in ('left', 'right'):
            sel = self.group_tree.selection()
            if sel and self.groups:
                gi = int(sel[0]) + (1 if key == 'right' else -1)
                if 0 <= gi < len(self.groups):
                    self.group_tree.selection_set(str(gi))
            return 'break'
        if key == 'return':
            if 0 <= self._detail_index < len(self._detail_rows):
                self._open_file(self._detail_rows[self._detail_index][2])
            return 'break'
        if key in ('k', 'd', 'm') and self._detail_rows:
            i = self._detail_index
            if 0 <= i < len(self._detail_rows):
                _row, _marker, path = self._detail_rows[i]
                v = self.decisions.get(path)
                if v is not None:
                    v.set({'k': 'keep', 'd': 'delete', 'm': 'move'}[key])
                if i + 1 < len(self._detail_rows):
                    self._set_detail_index(i + 1)  # triage: auto-advance
            return 'break'

    def _bind_wheel_recursive(self, w):
        w.bind('<MouseWheel>', self._on_detail_wheel)
        for c in w.winfo_children():
            self._bind_wheel_recursive(c)

    def _decode_thumb(self, path, height, gen, lbl):
        """Worker (thumbnail pool): decode + resize away from the UI thread,
        then hand the PIL image back to the main loop."""
        pil = self.thumbs.render_pil(path, height)
        try:
            self.after(0, lambda: self._apply_thumb(path, height, gen, lbl, pil))
        except (RuntimeError, tk.TclError):
            pass  # window gone mid-decode: drop the result

    def _apply_thumb(self, path, height, gen, lbl, pil):
        if gen != self._detail_gen:
            return  # user already selected a different group
        try:
            if not lbl.winfo_exists():
                return  # rows were rebuilt since this decode started
        except (RuntimeError, tk.TclError):
            return
        img = self.thumbs.store(path, height, pil)
        if img is None:
            return  # no thumbnail for this file; placeholder stays
        lbl.configure(image=img)
        lbl.image = img

    def _on_detail_configure(self, e):
        # content always spans the pane width...
        self.detail_canvas.itemconfigure(self._detail_window, width=e.width)
        # ...and thumbnails re-adapt to the new pane height — debounced so
        # dragging the window border doesn't rebuild rows on every pixel
        if getattr(self, '_resize_job', None):
            try:
                self.after_cancel(self._resize_job)
            except Exception:
                pass
        self._resize_job = self.after(180, self._maybe_rerender_detail)

    def _maybe_rerender_detail(self):
        self._resize_job = None
        sel = self.group_tree.selection()
        if not sel or not getattr(self, 'groups', None):
            return
        g = self.groups[int(sel[0])]
        if self._detail_thumb_height(len(g)) != getattr(
                self, '_detail_thumb_rendered', None):
            self._on_group_selected(None)

    def _on_detail_wheel(self, e):
        # scroll only while the content actually overflows the pane; when it
        # fits, the view is pinned and the wheel is a no-op (no dead space)
        if self.detail_canvas.yview() == (0.0, 1.0):
            return
        self.detail_canvas.yview_scroll(-1 * (e.delta // 120), 'units')

    def _open_file(self, path):
        if self.privacy.get('open_no_history'):
            from shutil import which
            # MPC-HC: launched directly (no Windows Recent Items entry);
            # disable "Keep history" in its own options for full privacy.
            # If MPC-HC isn't installed we fall through to the OS default
            # handler — we never second-guess the user's default player.
            candidates = [
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
            # No MPC-HC installed: open with the SYSTEM DEFAULT player rather
            # than hijacking the click to VLC. (Recent-items traces may apply,
            # but the user's default choice wins over player guessing.)
        # Fall back to the OS default handler (whatever the user registered —
        # e.g. MPC-HC — via Windows file association).
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

    def mark_all_keep_all(self):
        """Mark every file in every group as 'keep' — zero deletions."""
        if not self.groups:
            messagebox.showinfo('VidSweep', 'No groups loaded — scan first.')
            return
        total_files = 0
        for g in self.groups:
            for i, r in enumerate(g):
                v = self.decisions.get(r['path'])
                if v is None:
                    v = tk.StringVar(value='keep')
                    v.trace_add('write', lambda *a: self._update_marked_count())
                    self.decisions[r['path']] = v
                else:
                    v.set('keep')
                total_files += 1
        self._update_marked_count()
        messagebox.showinfo('VidSweep',
                            f'All {total_files} files across ALL {len(self.groups)} groups '
                            'marked KEEP. No files will be deleted.')

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

    # ---------------------------------------------- not-duplicates dismissals
    def dismiss_current_group(self):
        """Persistently hide the selected duplicate group from future scans."""
        sel = self.group_tree.selection()
        if not sel:
            messagebox.showinfo('VidSweep', 'Select a duplicate group first.')
            return
        gi = int(sel[0])
        g = self.groups[gi]
        paths = [r['path'] for r in g]
        try:
            key = self.org.group_key(paths)
            self.org.dismiss_group(key, paths)
        except Exception as e:
            messagebox.showerror('VidSweep', f'Could not dismiss group:\n{e}')
            return
        self.log_line(
            f'Dismissed group {gi + 1} as "not duplicates" '
            f'({len(g)} files): {os.path.basename(paths[0])} …')
        self.status_var.set(
            f'Group {gi + 1} dismissed — hidden until "Reset dismissed groups".')
        self.load_groups()

    def export_shown_groups(self):
        """Write the groups currently shown in this view to a CSV the operator picks."""
        if not getattr(self, 'groups', None):
            messagebox.showinfo('VidSweep', 'No groups are currently shown — scan first.')
            return
        snapshot = list(self.groups)          # ONE snapshot: exports and counts agree
        export_snapshot_to_csv(
            snapshot,
            ask_path=lambda: filedialog.asksaveasfilename(
                parent=self, title=f'Export {len(snapshot)} shown group(s) to CSV',
                defaultextension='.csv', initialfile='vidsweep_duplicates.csv',
                filetypes=[('CSV file', '*.csv'), ('All files', '*.*')]),
            export=lambda groups, path: self.org.export_groups(groups, path),
            info=messagebox.showinfo, error=messagebox.showerror)


    def dismiss_all_shown(self):
        """Convenience: dismiss every currently displayed group as not duplicates."""
        if not self.groups:
            messagebox.showinfo('VidSweep',
                                'No groups loaded — scan first.')
            return
        n_files = sum(len(g) for g in self.groups)
        if not messagebox.askyesno(
                'Dismiss all shown groups',
                f'Dismiss all {len(self.groups)} currently shown group(s) '
                f'({n_files} files) as "not duplicates"?\n\n'
                'They will stay hidden from duplicate results until you click '
                '“Reset dismissed groups”.'):
            return
        try:
            for g in self.groups:
                paths = [r['path'] for r in g]
                self.org.dismiss_group(self.org.group_key(paths), paths)
        except Exception as e:
            messagebox.showerror('VidSweep', f'Could not dismiss groups:\n{e}')
            return
        self.log_line(
            f'Dismissed {len(self.groups)} group(s) as "not duplicates".')
        self.status_var.set(
            f'Dismissed {len(self.groups)} groups — hidden until reset.')
        self.load_groups()

    def reset_dismissed_groups(self):
        """Clear the persistent dismissal table, restoring hidden groups."""
        try:
            entries = self.org.list_dismissed()
        except Exception as e:
            messagebox.showerror('VidSweep',
                                 f'Could not read dismissed groups:\n{e}')
            return
        if not entries:
            messagebox.showinfo('VidSweep',
                                'No dismissed groups are stored — nothing to reset.')
            return
        if not messagebox.askyesno(
                'Reset dismissed groups',
                f'Restore {len(entries)} dismissed group(s) to the duplicate list?'):
            return
        try:
            n = self.org.clear_dismissed()
        except Exception as e:
            messagebox.showerror('VidSweep',
                                 f'Could not reset dismissed groups:\n{e}')
            return
        self.log_line(f'Reset dismissed groups — {n} group(s) restored.')
        self.status_var.set(f'Dismissed groups reset — {n} group(s) restored.')
        self.load_groups()

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

    def _write_action_log(self, rows, path=None):
        """Local audit trail for EXECUTE/Organize actions, under logs/ in the
        app folder — nothing leaves the machine. Written BEFORE the
        operations run (status 'planned', so a crash mid-execute still
        leaves a record of what was about to happen), then rewritten with
        the real per-file outcomes afterwards."""
        if path is None:
            log_dir = os.path.join(APP_DIR, 'logs')
            os.makedirs(log_dir, exist_ok=True)
            path = os.path.join(log_dir,
                                time.strftime('actions_%Y%m%d-%H%M%S.csv'))
            # rapid back-to-back actions share the second: never clobber
            n = 1
            while os.path.exists(path):
                path = os.path.join(log_dir, time.strftime(
                    f'actions_%Y%m%d-%H%M%S_{n}.csv'))
                n += 1
        stamp = time.strftime('%Y-%m-%d %H:%M:%S')
        with open(path, 'w', newline='', encoding='utf-8') as fh:
            w = csv.writer(fh)
            w.writerow(['timestamp', 'action', 'path', 'size',
                        'destination', 'status'])
            for action, p, size, dest, status in rows:
                w.writerow([stamp, action, p, size, dest, status])
        return path

    def _manifest_row(self, action, p):
        """One planned row for the action log; size is best-effort."""
        try:
            size = os.path.getsize(p)
        except OSError:
            size = '?'
        return [action, p, size, '', 'planned']

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
        # audit trail: planned actions hit disk before anything is touched
        manifest = ([self._manifest_row('delete', p) for p in to_delete]
                    + [self._manifest_row('move', p) for p in to_move])
        log_path = self._write_action_log(manifest)
        moved = deleted = failed = 0
        if self.action_var.get() == 'Recycle Bin':
            try:
                import send2trash
            except ImportError:
                messagebox.showerror('VidSweep', 'send2trash not installed. Run: pip install send2trash')
                return
        for i, p in enumerate(to_delete):
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
                    manifest[i][3] = dest
                else:
                    # secure delete (privacy setting) or Delete permanently
                    if self.privacy.get('secure_delete'):
                        self._secure_delete(p)
                    else:
                        os.remove(p)
                with self.org._db_lock:
                    self.org.db.execute('DELETE FROM files WHERE path=?', (p,))
                    self.org.db.commit()
                deleted += 1
                manifest[i][4] = 'deleted'
            except Exception as e:
                failed += 1
                manifest[i][4] = f'failed: {e}'
                self.log_line(f'FAILED {p}: {e}')
        for j, p in enumerate(to_move):
            mi = len(to_delete) + j
            dest_dir = filedialog.askdirectory(title=f'Choose destination for {os.path.basename(p)}')
            if not dest_dir:
                manifest[mi][4] = 'skipped (no destination chosen)'
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
                # update the DB only after the move succeeded; if this raises,
                # the file is safely at `dest` but the row still points at `p`,
                # so a later scan re-discovers it rather than losing track.
                with self.org._db_lock:
                    self.org.db.execute('UPDATE files SET path=? WHERE path=?',
                                        (dest, p))
                    self.org.db.commit()
                moved += 1
                manifest[mi][3] = dest
                manifest[mi][4] = 'moved'
            except Exception as e:
                failed += 1
                manifest[mi][4] = f'failed: {e}'
                self.log_line(f'FAILED move {p}: {e}')
        self.org.db.commit()
        self._write_action_log(manifest, log_path)  # rewrite with outcomes
        self.log_line(f'Action log: {log_path}')
        messagebox.showinfo('VidSweep',
                            f'Deleted: {deleted}, moved: {moved}, failed: {failed}\n\n'
                            f'Log: {log_path}')
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
        with self.org._db_lock:
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
        manifest = [self._manifest_row('move', p) for p, _cat, _dest in self.org_moves]
        log_path = self._write_action_log(manifest)
        done = failed = 0
        for i, (p, _cat, dest) in enumerate(self.org_moves):
            try:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                if os.path.exists(dest):
                    base, ext = os.path.splitext(dest)
                    k = 1
                    while os.path.exists(dest):
                        dest = f'{base}_{k}{ext}'
                        k += 1
                shutil.move(p, dest)
                # update the DB only after the move succeeded; if this raises,
                # the file is safely at `dest` but the row still points at `p`,
                # so a later scan re-discovers it rather than losing track.
                with self.org._db_lock:
                    self.org.db.execute('UPDATE files SET path=? WHERE path=?', (dest, p))
                done += 1
                manifest[i][3] = dest
                manifest[i][4] = 'moved'
            except Exception as e:
                failed += 1
                manifest[i][4] = f'failed: {e}'
                self.log_line(f'FAILED {p}: {e}')
        with self.org._db_lock:
            self.org.db.commit()
        self._write_action_log(manifest, log_path)
        self.log_line(f'Action log: {log_path}')
        messagebox.showinfo('VidSweep', f'Moved {done}, failed {failed}.\nLog: {log_path}')
        self.org_apply_btn.config(state='disabled')


def _acquire_single_instance_lock():
    """Windows named mutex: only one VidSweep may run at a time.

    The second launch exits immediately instead of piling up a second
    instance (three copies of the app were observed eating all RAM and
    crashing the PC — each instance opened the same library.db and could
    start its own scan). The mutex is kernel-held: if the process crashes
    or is killed, the OS releases it automatically, so no stale lock.
    Returns the mutex handle (keep it referenced for the app's lifetime)
    or None on non-Windows / when the mutex already exists.
    """
    if os.name != 'nt':
        return None
    import ctypes
    from ctypes import wintypes
    ERROR_ALREADY_EXISTS = 183
    # 'Global\' namespace: visible across sessions, same as most apps
    name = 'Global\\VidSweep_SingleInstance'
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, name)
    if not handle:
        return None  # couldn't create — don't block startup on this
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return False  # another instance is running
    return handle


def main():
    try:
        lock = _acquire_single_instance_lock()
        if lock is False:
            # already running: bring the existing window forward instead
            # of spawning a second copy
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(
                    None,
                    'VidSweep is already running.\n\n'
                    'Check your taskbar / system tray for the open window.',
                    'VidSweep', 0x40)  # MB_ICONINFORMATION
            except Exception:
                pass
            return
        app = App()
        app._instance_lock = lock  # keep the handle alive for app lifetime
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