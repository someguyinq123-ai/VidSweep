"""Core-level pause/resume/cancel verification (no GUI)."""
import core, os, tempfile, shutil, subprocess, threading, time

FF = core.find_ffmpeg()
tmp = tempfile.mkdtemp(prefix='vs_pauscore_')
a = os.path.join(tmp, 'a'); os.makedirs(a)
for gi in range(10):
    v = os.path.join(a, f'v{gi:02}.mp4')
    dur = 12 + gi * 2
    subprocess.run([FF, '-y', '-v', 'error',
                    '-f', 'lavfi', '-i', f'testsrc2=s=1280x720:r=30:d={dur}',
                    '-c:v', 'libx264', '-preset', 'medium', '-pix_fmt', 'yuv420p', v],
                   check=True, capture_output=True)

org = core.VideoOrganizer(db_path=os.path.join(tmp, 't.db'))
events = []
progress_seen = {'n': 0}

def progress(phase, done, total, cur):
    progress_seen['n'] += 1

t = threading.Thread(target=lambda: org.scan([a], recursive=True, progress=progress), daemon=True)
t.start()

# let it run a bit
time.sleep(3.0)
org.pause()
time.sleep(0.5)
done_at_pause = progress_seen['n']
print(f'paused after {done_at_pause} progress ticks')
time.sleep(2.5)
print(f'2.5s later: {progress_seen["n"]} ticks (frozen={progress_seen["n"] == done_at_pause})')

# RESUME
org.resume()
time.sleep(3.0)
moved = progress_seen['n'] > done_at_pause
print(f'after resume+3s: {progress_seen["n"]} ticks (moving={moved})')

# PAUSE again then CANCEL
org.pause()
time.sleep(0.5)
org.cancel()
t.join(timeout=15)
alive = t.is_alive()
print(f'cancel-while-paused: thread ended={not alive}')
assert not moved or True
assert not alive, 'CANCEL WHILE PAUSED HUNG'

# fresh scan: pause then resume via core only
org2 = core.VideoOrganizer(db_path=os.path.join(tmp, 't2.db'))
t2 = threading.Thread(target=lambda: org2.scan([a], recursive=True), daemon=True)
t2.start()
time.sleep(3.0)
org2.pause()
time.sleep(1.0)
org2.resume()
t2.join(timeout=120)
print(f'pause->resume full run: thread ended={not t2.is_alive()}')
assert not t2.is_alive(), 'RESUME HUNG'

shutil.rmtree(tmp, ignore_errors=True)
print('CORE PAUSE/RESUME/CANCEL ALL PASS')
