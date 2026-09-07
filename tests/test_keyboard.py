"""Keyboard triage test: K/D/M decide + advance, arrows switch groups/files,
Enter opens, and keys never fire while typing in a text field."""
import os
import sys
import tempfile
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core
import gui
import tkinter as tk

gui.messagebox.showinfo = lambda *a, **k: None
gui.messagebox.askyesno = lambda *a, **k: True
gui.messagebox.showwarning = lambda *a, **k: None

gui.APP_DIR = tempfile.mkdtemp(prefix='vs_keys_')
# ISOLATION: App() must never open the real library.db next to core.py
_iso_dir = tempfile.mkdtemp(prefix='vs_db_')
_iso_init = core.VideoOrganizer.__init__
core.VideoOrganizer.__init__ = (
    lambda self, db_path=None: _iso_init(
    self, db_path=db_path or os.path.join(_iso_dir, 't.db')))
app = gui.App()
app.geometry('1100x700+40+30')
app.nb.select(app.tab_dupes)

rec = lambda i: {'path': rf'C:\fake\video{i}.mp4', 'size': 123_456_789 + i,
                 'sha': f'{i:064x}', 'hashes': [], 'duration': 100.0 + i,
                 'width': 1920, 'height': 1080, 'vcodec': 'h264'}
app.groups = [[rec(i) for i in range(6)], [rec(50), rec(51)]]
app.dupe_summary.config(text='2 duplicate groups [keyboard test]')
for gi, g in enumerate(app.groups):
    app.group_tree.insert('', 'end', iid=str(gi),
                          values=(f'Group {gi+1} ({len(g)} files)', '1.0'))

opened = []
app._open_file = lambda p, *a, **k: opened.append(p)


def press(sym):
    app.focus_set()
    app.event_generate(f'<Key-{sym}>', keysym=sym)
    app.update()


# select group 0 -> cursor starts at file 0
app.group_tree.selection_set('0')
app.update()
assert app._detail_index == 0, f'cursor should start at 0, got {app._detail_index}'
assert app._detail_rows[0][1].cget('text') == '▶', 'marker not shown on row 0'

# D on file 0 -> marked delete, cursor advances to file 1
press('d')
assert app.decisions[app.groups[0][0]['path']].get() == 'delete', 'D did not mark delete'
assert app._detail_index == 1, 'D did not advance the cursor'

# K on file 1 -> keep (cursor advances to 2)
press('k')
assert app.decisions[app.groups[0][1]['path']].get() == 'keep', 'K did not mark keep'
assert app._detail_index == 2, 'K did not advance the cursor'

# Up moves back without deciding
press('Up')
assert app._detail_index == 1, f'Up did not move cursor back: {app._detail_index}'

# M on file 1 -> move (advances to 2), then Up again for the Enter test
press('m')
assert app.decisions[app.groups[0][1]['path']].get() == 'move', 'M did not mark move'
press('Up')

# Enter opens the highlighted file
press('Return')
assert opened and opened[-1] == app.groups[0][1]['path'], f'Enter did not open: {opened}'

# Right -> next group; cursor resets to its first file
press('Right')
app.update()
assert app.group_tree.selection() == ('1',), f'Right did not switch group: {app.group_tree.selection()}'
assert app._detail_index == 0, 'cursor not reset after group switch'

# Left -> back to group 0
press('Left')
app.update()
assert app.group_tree.selection() == ('0',), 'Left did not switch group back'

# typing in an Entry must NOT trigger triage (gate: _review_keys_active)
# headless Tk can't really move focus, so stub what the gate consults
entry = tk.Entry(app)
entry.pack()
app._detail_index = 2
before = app.decisions[app.groups[0][2]['path']].get() if app.groups[0][2]['path'] in app.decisions else 'undecided'
app.focus_get = lambda: entry  # pretend the Entry has focus
assert not app._review_keys_active(), 'gate active while Entry has focus'
press('d')
after = app.decisions[app.groups[0][2]['path']].get() if app.groups[0][2]['path'] in app.decisions else 'undecided'
assert before == after, 'triage key fired while an Entry had focus'
del app.focus_get  # restore the real method

app.destroy()
import shutil
shutil.rmtree(gui.APP_DIR, ignore_errors=True)
print('KEYBOARD TRIAGE TESTS PASS')
