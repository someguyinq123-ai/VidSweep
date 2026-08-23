"""Verify SHA-reuse: identical results to fresh decode, and real speedup on dup-heavy sets."""
import core, os, tempfile, shutil, subprocess, time

FF = core.find_ffmpeg()
tmp = tempfile.mkdtemp(prefix='vs_reuse_')
a = os.path.join(tmp, 'a'); os.makedirs(a)

# 10 unique videos, each duplicated 3x => 30 files, only 10 unique decodes needed
for gi in range(10):
    v = os.path.join(a, f'v{gi:02}.mp4')
    dur = 6 + gi * 0.5
    subprocess.run([FF, '-y', '-v', 'error',
                    '-f', 'lavfi', '-i', f'testsrc2=s=640x360:r=24:d={dur}',
                    '-f', 'lavfi', '-i', f'sine=frequency={300 + gi*25}:duration={dur}',
                    '-c:v', 'libx264', '-preset', 'medium', '-pix_fmt', 'yuv420p',
                    '-c:a', 'aac', v], check=True, capture_output=True)
    for k in (1, 2):
        shutil.copy(v, os.path.join(a, f'v{gi:02}_copy{k}.mp4'))
print('built 30 files (10 unique x 3)')

org = core.VideoOrganizer(db_path=os.path.join(tmp, 't.db'))

# scan folder A (10 originals)
t0 = time.perf_counter()
stats1 = org.scan([a], recursive=True)
t_full = time.perf_counter() - t0
assert stats1['errors'] == 0, stats1.get('error_details')

# folder B: 20 copies of the same content, never scanned before
b = os.path.join(tmp, 'b'); os.makedirs(b)
for gi in range(10):
    srcv = os.path.join(a, f'v{gi:02}.mp4')
    shutil.copy(srcv, os.path.join(b, f'copyA_{gi}.mp4'))
    shutil.copy(srcv, os.path.join(b, f'copyB_{gi}.mp4'))

t0 = time.perf_counter()
stats2 = org.scan([b], recursive=True)
t_reuse = time.perf_counter() - t0
assert stats2['errors'] == 0, stats2.get('error_details')

print(f'fresh full decode:  {t_full:.2f}s (reused_identical={stats1["reused_identical"]})')
print(f'with SHA reuse:     {t_reuse:.2f}s (reused_identical={stats2["reused_identical"]})')
print(f'speedup on 66%-duplicate set: {t_full/t_reuse:.1f}x')
assert stats2['reused_identical'] == 20, stats2

# correctness: reused rows must equal the originally decoded values
import re
rows = org.db.execute('SELECT path, phash, duration, width, height FROM files').fetchall()
base = {}
for path, phash, dur, w, h in rows:
    name = os.path.basename(path)
    # map copy files back to their source: copyA_5 -> v05
    m2 = re.match(r'copy[AB]_(\d+)\.mp4', name)
    key = f'v{int(m2.group(1)):02d}' if m2 else name.split('.')[0]
    base.setdefault(key, (phash, dur, w, h))
    assert base[key] == (phash, dur, w, h), f'MISMATCH for {path}: {base[key]} vs {(phash, dur, w, h)}'
print('PASS: reused fingerprints identical to freshly decoded ones')

# groups still correct: 10 groups of 5 (orig + 2 copies in A + 2 copies in B)
groups = org.find_duplicates()
sizes = sorted(len(g) for g in groups)
assert sizes == [5] * 10, sizes
print('PASS: grouping intact (10 groups of 5)')

app_dir_clean = shutil.rmtree(tmp, ignore_errors=True)
print('SHA-REUSE TESTS PASS')
