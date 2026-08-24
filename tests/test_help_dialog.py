"""Verify the new sensitivity help dialog: structure + visual screenshot."""
import os, sys, tempfile, tkinter as tk
from tkinter import messagebox
sys.path.insert(0, r'L:\Video organizer custom\OX ALpha')
os.chdir(r'L:\Video organizer custom\OX ALpha')
import gui, core

# stub all dialogs
for fn in ('showinfo', 'showwarning', 'showerror', 'askyesno'):
    setattr(messagebox, fn, lambda *a, **k: True)

tmpdb = os.path.join(tempfile.mkdtemp(prefix='vs_helpdlg_'), 't.db')

app = gui.App()
app.folder_list.delete(0, 'end')          # isolate from real settings.json
app.org = core.VideoOrganizer(db_path=tmpdb)
app.thumbs = gui.ThumbnailCache(app.org)
app.update()

# open the dialog via the production path
app.show_sensitivity_help()
app.update()

wins = [w for w in app.winfo_children() if isinstance(w, tk.Toplevel)]
assert len(wins) == 1, f'expected 1 Toplevel, got {len(wins)}'
dlg = wins[0]
assert dlg.title() == 'How perceptual match sensitivity works'

# count structural elements
labels = []
def walk(w):
    labels.append(w)
    for c in w.winfo_children():
        walk(c)
walk(dlg)
texts = [str(w.cget('text')) for w in labels if isinstance(w, tk.Label)]
levels_found = [t for t in texts if t.strip() in ('4','6','8','10','12+')]
names_found = [t for t in texts if t in ('Very strict','Strict','Default','Loose','Very loose')]
sections = [t for t in texts if t.isupper() and len(t) > 8]
print('level badges:', sorted(levels_found))
print('level names:', len(names_found))
print('section headers:', sections)

assert len(levels_found) == 5
assert len(names_found) == 5
assert any('HOW IT WORKS' == s for s in sections)
assert any(s.startswith('LEVEL-BY-LEVEL') for s in sections)
assert any('Rule of thumb' in t for t in texts)

# screenshot the dialog for visual review — force it on top first
import subprocess
dlg.attributes('-topmost', True)
dlg.lift()
dlg.focus_force()
app.update()
import time as _t
_t.sleep(0.6)
app.update()
geo = dlg.geometry()
x, y = dlg.winfo_rootx(), dlg.winfo_rooty()
w, h = dlg.winfo_width(), dlg.winfo_height()
shot = os.path.join(tempfile.gettempdir(), 'helpdialog.png')
subprocess.run(['powershell', '-c',
    f'Add-Type -AssemblyName System.Drawing; Add-Type -AssemblyName System.Windows.Forms;'
    f'$b=New-Object Drawing.Bitmap {w},{h};'
    f'$g=[Drawing.Graphics]::FromImage($b);'
    f'$g.CopyFromScreen({x},{y},0,0,$b.Size);'
    f'$b.Save("{shot}")'], capture_output=True)
print('screenshot:', shot, 'exists:', os.path.exists(shot))

dlg.destroy()
canvas_unbind_ok = True
app.update()
app.destroy()
print('HELP DIALOG STRUCTURE PASS')
