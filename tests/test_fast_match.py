"""Fast-match (banded LSH) test: same groups as the exhaustive path, way faster.

1. Real corpus (ffmpeg): fast_match=True and False produce identical groups.
2. Synthetic 800-record library injected straight into the DB: identical
   grouping on both paths, and the fast path is dramatically quicker.
"""
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core

FF = core.find_ffmpeg() or 'ffmpeg'
tmp = tempfile.mkdtemp(prefix='vs_fastmatch_')

# ---------- 1. real corpus: equivalence on ffmpeg-generated re-encodes
a = os.path.join(tmp, 'a')
os.makedirs(a)
orig = os.path.join(a, 'original.mp4')
subprocess.run([FF, '-y', '-v', 'error',
                '-f', 'lavfi', '-i', 'testsrc2=size=640x360:rate=30:duration=8',
                '-f', 'lavfi', '-i', 'sine=frequency=440:duration=8',
                '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
                '-c:a', 'aac', orig], check=True, capture_output=True)
shutil.copy(orig, os.path.join(a, 'copy.mp4'))
subprocess.run([FF, '-y', '-v', 'error', '-i', orig,
                '-vf', 'scale=480x270', '-c:v', 'libx264', '-preset', 'medium',
                '-crf', '26', '-pix_fmt', 'yuv420p',
                os.path.join(a, 'reenc.mkv')], check=True, capture_output=True)
subprocess.run([FF, '-y', '-v', 'error',
                '-f', 'lavfi', '-i', 'smptebars=size=640x360:rate=30:duration=8',
                '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
                os.path.join(a, 'other.mp4')], check=True, capture_output=True)

db = os.path.join(tmp, 'real.db')
org = core.VideoOrganizer(db_path=db)
org.scan([a], recursive=True)
groups_fast = org.find_duplicates(fast_match=True)
groups_slow = org.find_duplicates(fast_match=False)


def as_sets(groups):
    return sorted(frozenset(r['path'] for r in g) for g in groups)


assert as_sets(groups_fast) == as_sets(groups_slow), \
    'fast and exhaustive disagree on the real corpus'
assert sorted(len(g) for g in groups_fast) == [3], \
    f'real corpus grouping wrong: {[len(g) for g in groups_fast]}'
print('PASS: fast == exhaustive on real corpus', flush=True)
org.close()
shutil.rmtree(tmp, ignore_errors=True)

# ---------- 2. synthetic 800-record library: equivalence + speed
random.seed(42)
tmp2 = tempfile.mkdtemp(prefix='vs_fastmatch2_')
org = core.VideoOrganizer(db_path=os.path.join(tmp2, 'synthetic.db'))

N_CLUSTERS, CLUSTER_SIZE, N_NOISE = 30, 20, 200


def hexhash(v):
    return f'{v:016x}'


def mutate(v, flips):
    for _ in range(flips):
        v ^= 1 << random.randrange(64)
    return v


rows = []
for c in range(N_CLUSTERS):
    base = random.getrandbits(64)
    dur = random.uniform(100, 110)
    for m in range(CLUSTER_SIZE):
        # 3 frames mutated a little, 1 frame identical: comfortably a match
        frames = [hexhash(mutate(base, random.randrange(0, 6))),
                  hexhash(mutate(base, random.randrange(0, 6))),
                  hexhash(mutate(base, random.randrange(0, 6))),
                  hexhash(base)]
        p = os.path.join(tmp2, f'c{c}_m{m}.mp4')
        rows.append((p, random.randrange(10 ** 6, 10 ** 9),
                     f'{c:064x}', json.dumps(frames),
                     dur * random.uniform(0.97, 1.03)))
for i in range(N_NOISE):
    frames = [hexhash(random.getrandbits(64)) for _ in range(4)]
    p = os.path.join(tmp2, f'noise{i}.mp4')
    rows.append((p, random.randrange(10 ** 6, 10 ** 9),
                 f'noise{i:060x}', json.dumps(frames),
                 random.uniform(100, 110)))

with org._db_lock:
    org.db.executemany(
        'INSERT OR REPLACE INTO files(path,size,mtime,sha256,phash,duration)'
        ' VALUES(?,?,?,?,?,?)',
        [(p, s, 0, sha, phash, d) for p, s, sha, phash, d in rows])
    org.db.commit()

t0 = time.perf_counter()
fast = org.find_duplicates(fast_match=True)
t_fast = time.perf_counter() - t0
t0 = time.perf_counter()
slow = org.find_duplicates(fast_match=False)
t_slow = time.perf_counter() - t0

assert as_sets(fast) == as_sets(slow), \
    f'fast and exhaustive disagree on synthetic library ' \
    f'({len(fast)} vs {len(slow)} groups)'
assert len(fast) >= N_CLUSTERS - 2, \
    f'far fewer groups than expected: {len(fast)}'
speedup = t_slow / max(t_fast, 1e-9)
print(f'groups={len(fast)}  fast={t_fast:.2f}s  exhaustive={t_slow:.2f}s  '
      f'speedup={speedup:.1f}x', flush=True)
assert speedup >= 2, f'expected a solid speedup, got {speedup:.1f}x'

org.close()
shutil.rmtree(tmp2, ignore_errors=True)
print('FAST-MATCH TESTS PASS')
