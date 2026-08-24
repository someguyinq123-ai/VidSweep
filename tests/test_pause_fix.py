"""Verify pause truly stops workers, resume restarts them, cancel-during-pause works."""
import core, os, tempfile, shutil, subprocess, threading, time

FF = core.find_ffmpeg()
tmp = tempfile.mkdtemp(prefix='vs_pausefix_')
a = os.path.join(tmp, 'a'); os.makedirs(a)
for gi in range(12):
    v = os.path.join(a, f'v{gi}.mp4')
    subprocess.run([FF, '-y', '-v', 'error',
                    '-f', 'lavfi', '-i', 'testsrc2=s=1920x1080:r=30:d=240',
                    '-c:v', 'libx264', '-preset', 'fast', '-pix_fmt', 'yuv420p', v],
                   check=True, capture_output=True)

org = core.VideoOrganizer(db_path=os.path.join(tmp, 't.db'))
done_count = {'n': 0}
def progress(phase, done, total, cur):
    done_count['n'] = done

t = threading.Thread(target=lambda: org.scan([a], recursive=True, progress=progress), daemon=True)
t.start()

# wait for perceptual stage to be underway
while done_count['n'] < 3 or org._pause.is_set():
    time.sleep(0.2)
    if not t.is_alive():
        break
time.sleep(1.0)

# PAUSE — workers must actually stop
org.pause()
time.sleep(2.0)  # give in-flight workers time to hit the pause gate
n_at_pause = done_count['n']
# count how many files have fingerprints now
fp = lambda: org.db.execute('SELECT COUNT(*) FROM files WHERE phash IS NOT NULL').fetchone()[0]
fp_at_pause = fp()
time.sleep(4.0)
fp_after = fp()
print(f'pause: fingerprints {fp_at_pause} -> {fp_after} after 4s (stopped={fp_at_pause == fp_after})')
assert fp_at_pause == fp_after, 'WORKERS KEPT RUNNING DURING PAUSE'

# RESUME — must move again
org.resume()
time.sleep(4.0)
fp_resumed = fp()
print(f'resume: fingerprints {fp_after} -> {fp_resumed} after 4s (moving={fp_resumed > fp_after})')
assert fp_resumed > fp_after, 'RESUME DID NOT RESTART WORKERS'

# PAUSE again, then CANCEL during pause
org.pause()
time.sleep(1.0)
org.cancel()
t.join(timeout=15)
print(f'cancel during pause: thread ended={not t.is_alive()}')
assert not t.is_alive(), 'CANCEL DURING PAUSE HUNG'

shutil.rmtree(tmp, ignore_errors=True)
print('PAUSE/RESUME/CANCEL ALL PASS')
