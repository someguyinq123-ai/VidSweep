"""Regression: detail-pane wheel scroll must be scoped, clamped, content-sized.

1. Wheel over the groups tree (or anywhere else) must NOT scroll the video box.
2. Wheel on the detail pane scrolls, clamps at top and bottom (no overscroll).
3. The inner frame spans the canvas width (dynamic sizing).
4. A group whose content fits the pane cannot be scrolled at all (no dead space),
   and every group starts scrolled to the top.
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core
import gui

gui.messagebox.showinfo = lambda *a, **k: None
gui.APP_DIR = tempfile.mkdtemp(prefix='vs_scroll_')
# ISOLATION: App() must never open the real library.db next to core.py
_iso_dir = tempfile.mkdtemp(prefix='vs_db_')
_iso_init = core.VideoOrganizer.__init__
core.VideoOrganizer.__init__ = (
    lambda self, db_path=None: _iso_init(
    self, db_path=db_path or os.path.join(_iso_dir, 't.db')))
app = gui.App()
app.geometry('1100x700+40+30')
app.nb.select(app.tab_dupes)


def rec(i):
    return {'path': rf'C:\fake\video{i}.mp4', 'size': 123_456_789 + i, 'sha': f'{i:064x}',
            'hashes': [], 'duration': 634.0 + i, 'width': 1920, 'height': 1080,
            'vcodec': 'h264'}


app.groups = [[rec(i) for i in range(12)],   # 12 rows: definitely overflows
              [rec(0)],                      # 1 row: definitely fits
              [rec(i) for i in range(5)]]    # 5 rows: thumb size reacts to resize
app.dupe_summary.config(text='2 duplicate groups [scroll test]')
for gi, g in enumerate(app.groups):
    app.group_tree.insert('', 'end', iid=str(gi),
                          values=(f'Group {gi+1} ({len(g)} files)', '1.0'))

yview = lambda: tuple(round(v, 4) for v in app.detail_canvas.yview())
wheel = lambda w, d=-120: (w.event_generate('<MouseWheel>', delta=d), app.update())

# --- big group: select and let the pane build
app.group_tree.selection_set('0')
app.update()
app.detail_canvas.update()
assert yview()[0] == 0.0, f'group must start at the top, got {yview()}'

# 1. wheel over the groups tree must not move the detail pane
app.group_tree.focus_set()
app.group_tree.event_generate('<MouseWheel>', delta=-120)
app.update()
v = yview()
assert v[0] == 0.0, f'detail pane scrolled from a wheel event on the groups tree: {v}'

# 2. wheel on the pane itself scrolls...
wheel(app.detail_canvas)
v = yview()
assert v[0] > 0.0, f'wheel on detail canvas did not scroll: {v}'

# ...and a wheel over a content row reaches the pane too
first_row = app.detail_inner.winfo_children()[1]
app.detail_canvas.yview_moveto(0)
app.update()
wheel(first_row)
assert yview()[0] > 0.0, 'wheel over a video row did not scroll the pane'

# 3. clamped at both ends — no scrolling past the content
for _ in range(80):
    wheel(app.detail_canvas)
assert yview()[1] == 1.0, f'not pinned to bottom after 80 wheel-downs: {yview()}'
for _ in range(80):
    wheel(app.detail_canvas, +120)
assert yview()[0] == 0.0, f'not pinned to top after 80 wheel-ups: {yview()}'

# 4. dynamic width: inner frame spans the canvas exactly
inner_w = int(app.detail_canvas.itemcget(app._detail_window, 'width'))
canvas_w = app.detail_canvas.winfo_width()
assert inner_w == canvas_w, f'inner frame width {inner_w} != canvas width {canvas_w}'

# 5. adaptive thumbnails: big group -> small thumbs, tiny group -> big thumbs
def first_thumb_h():
    row = app.detail_inner.winfo_children()[0]
    return row.winfo_children()[1].image.height()  # [0] is the ▶ marker

h_big = first_thumb_h()
app.group_tree.selection_set('1')
app.update()
h_small = first_thumb_h()
assert h_big == 54, f'12-row group should use minimum thumbs (54), got {h_big}'
assert h_small == 240, f'1-row group should use max thumbs (240), got {h_small}'

# 6. resizing the window re-adapts thumbnails (debounced re-render)
app.group_tree.selection_set('2')
app.update()
h_before = first_thumb_h()
app.geometry('1100x600')
# pump the event loop until the debounced re-render lands (configure fires,
# 180ms timer runs, rows rebuild) — can't just wait on _resize_job, which is
# still None until the first <Configure> of the shrink is delivered
deadline = time.time() + 5
while time.time() < deadline:
    app.update()
    time.sleep(0.05)
    if getattr(app, '_resize_job', None) is None and first_thumb_h() != h_before:
        break
app.update()
h_after = first_thumb_h()
assert h_after < h_before, \
    f'thumbnails did not shrink after window shrink: {h_before} -> {h_after}'
assert h_after == app._detail_thumb_height(5), \
    f'rendered thumb {h_after} != adapted size {app._detail_thumb_height(5)}'

# 7. clicking a thumbnail opens the file (same as the Open button)
opened = []
app._open_file = lambda p, *a, **k: opened.append(p)
app.group_tree.selection_set('0')
app.update()
row0 = app.detail_inner.winfo_children()[0]
thumb_lbl = row0.winfo_children()[1]  # [0] is the ▶ marker
assert str(thumb_lbl.cget('cursor')) == 'hand2', 'thumbnail cursor not hand2'
thumb_lbl.event_generate('<Button-1>')
app.update()
assert opened == [app.groups[0][0]['path']], \
    f'clicking the thumbnail did not open the file: {opened}'

# 8. a real thumbnail blob decodes off-thread and swaps into the row
import io
from PIL import Image
blob_buf = io.BytesIO()
Image.new('RGB', (256, 144), (30, 120, 220)).save(blob_buf, 'JPEG')
with app.org._db_lock:
    app.org.db.execute(
        'INSERT OR REPLACE INTO files(path,size,mtime,sha256,phash,thumbnail)'
        ' VALUES(?,?,?,?,?,?)',
        (app.groups[0][0]['path'], 1, 0, 'f' * 64, None, blob_buf.getvalue()))
    app.org.db.commit()
app.thumbs._cache.clear()  # force a fresh async decode
app.group_tree.selection_set('0')
app.update()
deadline = time.time() + 5
w = 0
while time.time() < deadline:
    app.update()
    time.sleep(0.05)
    row0 = app.detail_inner.winfo_children()[0]
    w = row0.winfo_children()[1].image.width()  # [0] is the ▶ marker
    if w == 96:  # 54px-high 16:9 thumb from the 256x144 source
        break
assert w == 96, f'real thumbnail never swapped in (width {w})'

# --- small group: content fits -> wheel must do nothing (no dead space)
app.group_tree.selection_set('1')
app.update()
app.detail_canvas.update()
v = yview()
assert v == (0.0, 1.0), f'small group should not scroll at all: {v}'
wheel(app.detail_canvas)
wheel(app.detail_inner)
assert yview() == (0.0, 1.0), f'small group scrolled although content fits: {yview()}'

app.destroy()
import shutil
shutil.rmtree(gui.APP_DIR, ignore_errors=True)
print('DETAIL-SCROLL TESTS PASS')
