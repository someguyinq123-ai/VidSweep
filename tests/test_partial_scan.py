"""Partial-scan test: stop a scan mid-way, check dupes on the batch, resume.

Covers the session feature end-to-end:
  1. Cancel after hashing completes (mid perceptual stage) -> session stays
     active, SHA digests are checkpointed in the cache.
  2. find_duplicates(session_id=...) works on the partial batch.
  3. Resuming the same session redoes NO hashing (digests come from the
     checkpoint) and completes the batch; grouping matches the full scan.
  4. Whole-library scope still sees everything.
  5. A brand-new scan supersedes the old session and re-batches the files.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core

FF = core.find_ffmpeg() or 'ffmpeg'
tmp = tempfile.mkdtemp(prefix='vs_partial_')
a = os.path.join(tmp, 'a')
os.makedirs(a)


def make_video(path, dur, src='testsrc2', hue=None):
    vf = f'hue=h={hue}' if hue is not None else None
    cmd = [FF, '-y', '-v', 'error',
           '-f', 'lavfi', '-i', f'{src}=size=640x360:rate=30:duration={dur}',
           '-f', 'lavfi', '-i', f'sine=frequency=440:duration={dur}',
           '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
           '-c:a', 'aac']
    if vf:
        cmd += ['-vf', vf]
    cmd.append(path)
    subprocess.run(cmd, check=True, capture_output=True)


print('building 8 test videos...', flush=True)
orig = os.path.join(a, 'original.mp4')
make_video(orig, 8)
shutil.copy(orig, os.path.join(a, 'exact_copy.mp4'))
reenc = os.path.join(a, 'reencoded.mkv')
subprocess.run([FF, '-y', '-v', 'error', '-i', orig,
                '-vf', 'scale=480x270', '-c:v', 'libx264', '-preset', 'medium',
                '-crf', '26', '-pix_fmt', 'yuv420p', reenc],
               check=True, capture_output=True)
# fillers: distinct durations (>10% apart) so the overlap filter keeps them apart
for i, dur in enumerate((11, 13, 15, 17, 19)):
    make_video(os.path.join(a, f'filler{i}.mp4'), dur, hue=i * 40)

db = os.path.join(tmp, 't.db')
org = core.VideoOrganizer(db_path=db)

# --- instrument: count SHA calls; gate ffprobe so we can cancel deterministically
sha_calls = [0]
real_sha = org._sha256
def counting_sha(path):
    sha_calls[0] += 1
    return real_sha(path)
org._sha256 = counting_sha

gate = threading.Event()
real_ffprobe = org._ffprobe_info
def gated_ffprobe(path):
    # hold Stage B until the test has verified the SHA checkpoint, then let
    # cancel take effect (real _ffprobe_info raises Cancelled on entry)
    gate.wait(60)
    return real_ffprobe(path)
org._ffprobe_info = gated_ffprobe

roots = [a]
scan_result = {}
def run_scan(session_id=None):
    def target():
        try:
            scan_result['stats'] = org.scan(roots, recursive=True,
                                            session_id=session_id)
        except core.Cancelled:
            scan_result['cancelled'] = True
        except Exception as e:
            scan_result['error'] = e
    t = threading.Thread(target=target, daemon=True)
    t.start()
    return t

t = run_scan()

# wait until every file's SHA digest is checkpointed (Stage A fully flushed)
deadline = time.time() + 120
while time.time() < deadline:
    n = org.db.execute(
        'SELECT COUNT(*) FROM files WHERE sha256 IS NOT NULL').fetchone()[0]
    if n == 8:
        break
    time.sleep(0.2)
else:
    raise AssertionError(f'Stage A never checkpointed all 8 digests (got {n})')

sid = org.db.execute('SELECT MAX(id) FROM sessions').fetchone()[0]
org.cancel()          # user pressed Stop after the hashing phase
gate.set()
t.join(timeout=60)
assert not t.is_alive(), 'scan thread still alive after cancel'
assert scan_result.get('cancelled'), f'expected Cancelled, got {scan_result}'
print('scan cancelled after Stage A checkpoint', flush=True)

sess = org.get_last_session()
assert sess and sess['id'] == sid and sess['active'], f'session not active: {sess}'

# --- 2. duplicates on the partial batch must work and stay in-session
partial_groups = org.find_duplicates(session_id=sid)
partial_recs = org._load_records(session_id=sid)
assert all(os.path.dirname(r['path']) == a for r in partial_recs), \
    'scoped batch contains files outside the session'
print(f'partial batch: {len(partial_recs)} fingerprinted, '
      f'{len(partial_groups)} groups so far', flush=True)

# --- 3. resume: no re-hashing, batch completes, grouping matches full scan
del org._sha256          # back to real hashing for the resume
sha_calls[0] = 0
del org._ffprobe_info
stats2 = org.scan(roots, recursive=True, session_id=sid)
assert 'error' not in scan_result, scan_result
assert stats2['hashed_exact'] == 0, \
    f'resume re-hashed {stats2["hashed_exact"]} files — checkpoint broken'
assert sha_calls[0] == 0, f'resume ran {sha_calls[0]} SHA calls — checkpoint broken'
assert stats2['skipped_cached'] + stats2['processed'] == 8, stats2
assert stats2['session_id'] == sid

groups = org.find_duplicates(session_id=sid)
sizes = sorted(len(g) for g in groups)
assert sizes == [3], f'expected one group of 3 after resume, got {sizes}'
all_grouped = {r['path'] for g in groups for r in g}
assert os.path.join(a, 'filler0.mp4') not in all_grouped, 'false positive'

sess = org.get_last_session()
assert not sess['active'], 'session should be complete after a full scan'
print(f'resume complete: {stats2["processed"]} processed, '
      f'{stats2["skipped_cached"]} skipped, 0 re-hashed', flush=True)

# --- 4. whole-library scope is unaffected
lib_recs = org._load_records()
assert len(lib_recs) == 8, f'library scope lost files: {len(lib_recs)}'
lib_groups = org.find_duplicates()
assert sorted(len(g) for g in lib_groups) == [3]

# --- 5. a new scan supersedes the old session and re-batches what it sees
stats3 = org.scan(roots, recursive=True)   # everything cached: fast
assert stats3['skipped_cached'] == 8 and stats3['processed'] == 0
new_sid = stats3['session_id']
assert new_sid != sid
old_status = org.db.execute(
    'SELECT status FROM sessions WHERE id=?', (sid,)).fetchone()[0]
assert old_status == 'complete', f'old session not superseded: {old_status}'
assert org._load_records(session_id=new_sid), 'new batch is empty'
assert not org._load_records(session_id=sid), \
    'old batch still holds files after re-scan re-batched them'
print('new scan superseded old session', flush=True)

org.close()
shutil.rmtree(tmp, ignore_errors=True)
print('PARTIAL-SCAN TEST PASS')
