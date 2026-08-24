"""Cancel latency test: cancel mid-scan of large files must finish in <10s."""
import core, os, tempfile, shutil, subprocess, threading, time

FF = core.find_ffmpeg()
tmp = tempfile.mkdtemp(prefix='vs_cancelfast_')
a = os.path.join(tmp, 'a'); os.makedirs(a)
# large-ish files: ~90s of 1080p each — big enough that old code would take ~30s+ per file
print('building 8 large test videos (~90s 1080p each)...', flush=True)
for gi in range(8):
    v = os.path.join(a, f'v{gi}.mp4')
    subprocess.run([FF, '-y', '-v', 'error',
                    '-f', 'lavfi', '-i', 'testsrc2=s=1920x1080:r=30:d=90',
                    '-c:v', 'libx264', '-preset', 'fast', '-pix_fmt', 'yuv420p', v],
                   check=True, capture_output=True)

org = core.VideoOrganizer(db_path=os.path.join(tmp, 't.db'))
t = threading.Thread(target=lambda: org.scan([a], recursive=True), daemon=True)
t.start()
time.sleep(6.0)  # let it get deep into hashing/decoding big files

t0 = time.monotonic()
org.cancel()      # <- what the Cancel button calls
t.join(timeout=60)
dt = time.monotonic() - t0
alive = t.is_alive()

print(f'cancel latency: {dt:.1f}s (thread ended={not alive})', flush=True)
assert not alive, f'scan thread STILL ALIVE {dt:.0f}s after cancel!'
assert dt < 10, f'too slow: cancel took {dt:.1f}s'

shutil.rmtree(tmp, ignore_errors=True)
print(f'FAST-CANCEL TEST PASS ({dt:.1f}s)')
