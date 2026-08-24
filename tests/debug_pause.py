"""Pause DURING perceptual with many large files so the stage lasts >30s."""
import core, os, tempfile, shutil, subprocess, threading, time, sys

FF = core.find_ffmpeg()
tmp = tempfile.mkdtemp(prefix='vs_dbg4_')
a = os.path.join(tmp, 'a'); os.makedirs(a)
for gi in range(12):
    v = os.path.join(a, f'v{gi}.mp4')
    subprocess.run([FF, '-y', '-v', 'error',
                    '-f', 'lavfi', '-i', 'testsrc2=s=1920x1080:r=30:d=240',
                    '-c:v', 'libx264', '-preset', 'fast', '-pix_fmt', 'yuv420p', v],
                   check=True, capture_output=True)

org = core.VideoOrganizer(db_path=os.path.join(tmp, 't.db'))
orig_wait = core.VideoOrganizer._wait_if_paused
def traced_wait(self):
    if self._pause.is_set():
        print('[wait] loop BLOCKING', flush=True)
        orig_wait(self)
        print('[wait] loop UNBLOCKED', flush=True)
core.VideoOrganizer._wait_if_paused = traced_wait

def progress(phase, done, total, cur):
    print(f'[prog] {phase} {done}/{total}', flush=True)

t = threading.Thread(target=lambda: org.scan([a], recursive=True, progress=progress), daemon=True)
t.start()

# pause the moment perceptual stage starts
deadline = time.time() + 120
while time.time() < deadline:
    if org._pause.is_set():
        break
    # peek at progress by waiting for hashing to finish
    time.sleep(0.5)
    # crude: check db for first perceptual row
    row = org.db.execute('SELECT COUNT(*) FROM files WHERE phash IS NOT NULL').fetchone()[0]
    if row >= 2:
        break
print('>>> PAUSE (perceptual should be in flight)', flush=True)
org.pause()
time.sleep(3.0)
print('>>> RESUME', flush=True)
org.resume()
time.sleep(8.0)
print(f'>>> thread alive after resume+8s: {t.is_alive()}', flush=True)
if t.is_alive():
    org.cancel()
    t.join(timeout=15)
    print(f'after cancel: alive={t.is_alive()}', flush=True)
    import traceback
    # dump where the scan thread is stuck
    for tid, stack in sys._current_frames().items():
        if tid != threading.main_thread().ident:
            print('--- thread stack ---', flush=True)
            for line in traceback.format_stack(stack):
                print(line, flush=True)
shutil.rmtree(tmp, ignore_errors=True)
print('done')
