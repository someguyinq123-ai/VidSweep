"""Verify 1-process frame extraction == old 4-process output, and benchmark."""
import core, os, tempfile, shutil, subprocess, time, sys

FF = core.find_ffmpeg()
tmp = tempfile.mkdtemp(prefix='vs_perf_')

# realistic test file: 60s 1080p
src = os.path.join(tmp, 'src.mp4')
subprocess.run([FF, '-y', '-v', 'error',
                '-f', 'lavfi', '-i', 'testsrc2=size=1920x1080:rate=30:duration=60',
                '-c:v', 'libx264', '-preset', 'medium', '-b:v', '4000k',
                '-pix_fmt', 'yuv420p', src], check=True, capture_output=True)
print('test file:', os.path.getsize(src) // 1024, 'KB, 60s 1080p')

def old_extract(path, duration, run_dir):
    """The previous implementation: one ffmpeg per frame."""
    frames = []
    for i, frac in enumerate(core.SAMPLE_TIMES):
        out = os.path.join(run_dir, f'old{i}.jpg')
        subprocess.run([FF, '-y', '-v', 'error', '-ss', f'{duration*frac:.3f}',
                        '-i', path, '-frames:v', '1', '-vf', 'scale=256:-2', out],
                       capture_output=True, creationflags=core._NO_WINDOW)
        try:
            with open(out, 'rb') as f:
                data = f.read()
            if len(data) > 100:
                frames.append(data)
        except OSError:
            pass
    return frames

import imagehash
from PIL import Image
import io

def hashes_of(frames):
    return [str(imagehash.phash(Image.open(io.BytesIO(d)))) for d in frames]

d = os.path.join(tmp, 'old'); os.makedirs(d)
t0 = time.perf_counter()
old_frames = old_extract(src, 60.0, d)
old_t = time.perf_counter() - t0
old_h = hashes_of(old_frames)

d2 = os.path.join(tmp, 'new'); os.makedirs(d2)
t0 = time.perf_counter()
new_frames = core.VideoOrganizer.__dict__['_extract_frames'](None, src, 60.0, d2) \
    if False else core.VideoOrganizer.__new__(core.VideoOrganizer)._extract_frames(src, 60.0, d2)
new_t = time.perf_counter() - t0
new_h = hashes_of(new_frames)

print(f'\nold (4 processes): {old_t:.2f}s, {len(old_frames)} frames')
print(f'new (1 process):   {new_t:.2f}s, {len(new_frames)} frames')
print(f'speedup: {old_t/new_t:.1f}x')

assert len(old_frames) == len(new_frames) == 4, 'frame count mismatch!'
mismatches = [(a, b) for a, b in zip(old_h, new_h) if a != b]
if mismatches:
    # allow tiny hamming distance (seek rounding), but not identity changes
    import imagehash as ih
    dists = [ih.hex_to_hash(a) - ih.hex_to_hash(b) for a, b in mismatches]
    print('non-identical frames, hamming dists:', dists)
    assert all(dd <= 2 for dd in dists), f'OUTPUT CHANGED TOO MUCH: {dists}'
else:
    print('PASS: frames byte-for-frame-hash identical to old method')

# projection for 30k files (sequential equivalent; real scan also parallelizes)
per_file_new = new_t + 0.05  # + approx probe cost
print(f'\nprojected frame-extraction for 30k files: {per_file_new*30000/3600:.1f}h single-threaded')
shutil.rmtree(tmp, ignore_errors=True)
print('PERF/QUALITY TEST PASS')
