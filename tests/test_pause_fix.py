"""Verify pause truly stops workers, resume restarts them, cancel-during-pause works.

Pause semantics: in-flight decodes are allowed to finish (bounded by ffmpeg
timeouts); no NEW file may start while paused. So the assertion must drain
the in-flight wave first, then check the fingerprint count is stable.
"""
import core, os, tempfile, shutil, subprocess, threading, time

FF = core.find_ffmpeg()
tmp = tempfile.mkdtemp(prefix='vs_pausefix_')
a = os.path.join(tmp, 'a'); os.makedirs(a)
for gi in range(12):
    v = os.path.join(a, f'v{gi}.mp4')
    subprocess.run([FF, '-y', '-v', 'error',
                    '-f', 'lavfi', '-i', 'testsrc2=s=1920x1080:r=30:d=90',
                    '-c:v', 'libx264', '-preset', 'fast', '-pix_fmt', 'yuv420p', v],
                   check=True, capture_output=True)

org = core.VideoOrganizer(db_path=os.path.join(tmp, 't.db'))
state = {'phase': '', 'done': 0}
def progress(phase, done, total, cur):
    state['phase'] = phase
    state['done'] = done

fp = lambda: org.db.execute(
    'SELECT COUNT(*) FROM files WHERE phash IS NOT NULL').fetchone()[0]

t = threading.Thread(target=lambda: org.scan([a], recursive=True, progress=progress),
                     daemon=True)
t.start()

# pause once the pipeline has real work underway (hashing + perceptual run
# concurrently now; done counts completions over 2x the file count)
deadline = time.monotonic() + 600
while not (state['phase'] == 'working' and state['done'] >= 4):
    if not t.is_alive() or time.monotonic() > deadline:
        raise AssertionError(f'scan pipeline never got underway: {state}')
    time.sleep(0.2)
org.pause()
print(f'paused mid-pipeline at {state["done"]}/24', flush=True)

# drain: in-flight decodes may still write their rows; wait until the
# fingerprint count has been stable for 5s (all workers parked at the gate)
last, stable_since = fp(), time.monotonic()
while time.monotonic() - stable_since < 5.0:
    time.sleep(1.0)
    cur = fp()
    if cur != last:
        last, stable_since = cur, time.monotonic()
fp_at_pause = last

time.sleep(4.0)  # a real pause: nothing new may be fingerprinted
fp_after = fp()
print(f'pause: fingerprints stable at {fp_at_pause}, after 4s: {fp_after} '
      f'(stopped={fp_after == fp_at_pause})', flush=True)
assert fp_after == fp_at_pause, 'WORKERS KEPT RUNNING DURING PAUSE'

# RESUME — must move again (more fingerprints and/or the scan finishes)
org.resume()
t0r = time.monotonic()
moved = False
while time.monotonic() - t0r < 30:
    if fp() > fp_after or not t.is_alive():
        moved = True
        break
    time.sleep(0.5)
print(f'resume: workers restarted={moved}', flush=True)
assert moved, 'RESUME DID NOT RESTART WORKERS'

# PAUSE again, then CANCEL during pause
org.pause()
time.sleep(1.0)
org.cancel()
t.join(timeout=15)
print(f'cancel during pause: thread ended={not t.is_alive()}', flush=True)
assert not t.is_alive(), 'CANCEL DURING PAUSE HUNG'

shutil.rmtree(tmp, ignore_errors=True)
print('PAUSE/RESUME/CANCEL ALL PASS')
