"""Verify mark-all-beyond-40 groups and bitrate-first quality ranking."""
import gui, core, os, tempfile, shutil, subprocess, sys
gui.messagebox.showinfo = lambda *a, **k: None
gui.messagebox.askyesno = lambda *a, **k: True
gui.messagebox.showwarning = lambda *a, **k: None

FF = 'ffmpeg'
tmp = tempfile.mkdtemp(prefix='vs_markall_')
a = os.path.join(tmp, 'a'); os.makedirs(a)

for gi in range(45):
    v = os.path.join(a, f'vid{gi:02}.mp4')
    dur = 4 + gi * 0.6  # distinct durations -> distinct groups (10% overlap rule)
    subprocess.run([FF, '-y', '-v', 'error',
                    '-f', 'lavfi', '-i', f'testsrc2=s=320x180:r=10:d={dur}',
                    '-f', 'lavfi', '-i', f'sine=frequency={200 + gi * 10}:duration={dur}',
                    '-filter_complex',
                    f'[0:v]hue=h={gi * 8},drawtext=text={gi}:x=20:y=20:fontsize=48[v]',
                    '-map', '[v]', '-map', '1:a',
                    '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
                    '-c:a', 'aac', v], check=True, capture_output=True)
    shutil.copy(v, os.path.join(a, f'vid{gi:02}_copy.mp4'))
print('built 90 test videos')

app = gui.App()
app.org = core.VideoOrganizer(db_path=os.path.join(tmp, 't.db'))  # ISOLATED
app.thumbs = gui.ThumbnailCache(app.org)
stats = app.org.scan([a], recursive=True)
assert stats['errors'] == 0, stats.get('error_details')
app.load_groups()
# load_groups is now asynchronous (worker thread + event-loop poll):
# pump Tk events until the summary label shows the loaded count.
import time as _t
_deadline = _t.time() + 180
while 'duplicate group' not in app.dupe_summary.cget('text'):
    assert _t.time() < _deadline, 'load_groups never completed'
    app.update()
    _t.sleep(0.05)
print('groups loaded:', len(app.groups))
assert len(app.groups) == 45, f'expected 45 groups, got {len(app.groups)}'

# FIX 1: mark all WITHOUT visiting any group detail
app.mark_all_keep_best()
marked = sum(1 for v in app.decisions.values() if v.get() == 'delete')
print('marked for deletion:', marked)
assert marked == 45 and len(app.decisions) == 90
print('PASS: mark-all covers all 45 groups (no 40 limit)')

# FIX 2: bitrate-first quality ranking
g = os.path.join(tmp, 'q'); os.makedirs(g, exist_ok=True)
sharp720 = os.path.join(g, 'sharp720.mp4')
subprocess.run([FF, '-y', '-v', 'error', '-f', 'lavfi', '-i',
                'testsrc2=size=1280x720:rate=30:duration=6',
                '-c:v', 'libx264', '-preset', 'medium', '-b:v', '6000k',
                '-pix_fmt', 'yuv420p', sharp720], check=True, capture_output=True)
blurry1080 = os.path.join(g, 'blurry1080.mp4')
subprocess.run([FF, '-y', '-v', 'error', '-f', 'lavfi', '-i',
                'testsrc2=size=1920x1080:rate=30:duration=6',
                '-c:v', 'libx264', '-preset', 'medium', '-b:v', '500k',
                '-pix_fmt', 'yuv420p', blurry1080], check=True, capture_output=True)
org2 = core.VideoOrganizer(db_path=os.path.join(tmp, 'q.db'))
org2.scan([g], recursive=True)
groups2 = org2.find_duplicates()
assert len(groups2) == 1, f'expected 1 group, got {len(groups2)}'
best = os.path.basename(groups2[0][0]['path'])
print('best pick:', best)
assert best == 'sharp720.mp4', f'ranking still resolution-first! picked {best}'
print('PASS: sharp 720p (high bitrate) beats blurry 1080p (low bitrate)')

app.destroy()
shutil.rmtree(tmp, ignore_errors=True)
print('ALL MARK-ALL + QUALITY TESTS PASS')
