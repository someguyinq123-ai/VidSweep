"""Pause must survive closing the app AND a full PC restart.

Two-process simulation of the real scenario:
  Phase A (this process): start a scan, let the first files fingerprint
    completely, press Pause (the paused flag is persisted to the sessions
    table and the scan_state.json sidecar), then cancel+close exactly like
    the GUI's window-close handler does — as if the user quit or the PC
    shut down for the night.
  Phase B (fresh subprocess = fresh process, like relaunching after a
    reboot): open the same library.db, verify the session is still active
    AND marked paused with its fingerprinted progress intact, then resume
    it and verify ZERO re-hashing (checkpointed digests are reused).
"""
import os
import sys
import json
import shutil
import subprocess
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core

FF = core.find_ffmpeg() or 'ffmpeg'


def phase_a(db, folder):
    """Scan, fingerprint a few files, pause, close — like the GUI does."""
    org = core.VideoOrganizer(db_path=db)
    gate = threading.Event()
    calls = [0]
    real_ffprobe = org._ffprobe_info

    def gated_ffprobe(path):
        # first 3 files process fully; the rest stay blocked (in-flight) so
        # the scan is genuinely mid-work when the user pauses
        calls[0] += 1
        if calls[0] > 3:
            gate.wait(60)
        return real_ffprobe(path)
    org._ffprobe_info = gated_ffprobe

    result = {}

    def target():
        try:
            org.scan([folder], recursive=True)
        except core.Cancelled:
            result['cancelled'] = True
        except Exception as e:
            result['error'] = e
    t = threading.Thread(target=target, daemon=True)
    t.start()

    # wait until at least 3 files are fully fingerprinted (sha + phash)
    deadline = time.time() + 120
    while time.time() < deadline:
        n = org.db.execute(
            'SELECT COUNT(*) FROM files WHERE sha256 IS NOT NULL '
            'AND phash IS NOT NULL').fetchone()[0]
        if n >= 3:
            break
        time.sleep(0.1)
    else:
        raise AssertionError('files never finished fingerprinting')

    org.pause()  # the user presses Pause, then quits / the PC shuts down
    paused, status = org.db.execute(
        'SELECT paused,status FROM sessions ORDER BY id DESC LIMIT 1'
    ).fetchone()
    assert paused == 1 and status == 'active', (paused, status)

    org.cancel()          # what _on_close does before exiting
    gate.set()
    t.join(timeout=60)
    assert not t.is_alive(), 'scan thread still alive after cancel'
    assert result.get('cancelled'), result
    org.close()

    # the sidecar must mirror the paused, unfinished session
    with open(os.path.join(os.path.dirname(db), 'scan_state.json')) as fh:
        st = json.load(fh)
    assert st['paused'] and st['status'] == 'active', st
    assert st['done'] >= 1 and st['total'] == 8, st
    print('PHASE A OK: paused and closed with '
          f"{st['done']}/{st['total']} fingerprinted", flush=True)


def phase_b(db, folder):
    """Relaunch after 'reboot': resume the paused scan, zero re-hashing."""
    org = core.VideoOrganizer(db_path=db)
    sess = org.get_last_session()
    assert sess and sess['active'], f'session lost after restart: {sess}'
    assert sess['paused'], f'paused flag lost after restart: {sess}'
    assert sess['done'] >= 1, f'fingerprinted progress lost: {sess}'
    assert sess['total'] == 8, f'enumerated total lost: {sess}'

    sha_calls = [0]
    real_sha = org._sha256

    def counting_sha(path):
        sha_calls[0] += 1
        return real_sha(path)
    org._sha256 = counting_sha

    stats = org.scan([folder], recursive=True, session_id=sess['id'])
    assert stats['session_id'] == sess['id']
    assert sha_calls[0] == 0, f'resume re-hashed {sha_calls[0]} files'
    assert stats['hashed_exact'] == 0, stats
    assert stats['skipped_cached'] + stats['processed'] == 8, stats

    sess2 = org.get_last_session()
    assert not sess2['active'], 'session should be complete after resume'
    assert not sess2['paused'], 'paused flag should be cleared after resume'
    org.close()
    print('PHASE B OK: resumed after restart with zero re-hashing', flush=True)


def main():
    tmp = tempfile.mkdtemp(prefix='vs_resume_restart_')
    a = os.path.join(tmp, 'a')
    os.makedirs(a)
    import subprocess as sp
    for gi in range(8):
        v = os.path.join(a, f'v{gi}.mp4')
        sp.run([FF, '-y', '-v', 'error',
                '-f', 'lavfi', '-i', f'testsrc2=s=640x360:r=24:d={9 + gi}',
                '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt',
                'yuv420p', v], check=True, capture_output=True)

    db = os.path.join(tmp, 't.db')
    phase_a(db, a)
    # Phase B runs in a FRESH interpreter — a different process, exactly like
    # relaunching the app after closing it (or after rebooting the PC)
    r = subprocess.run(
        [sys.executable, os.path.abspath(__file__), 'phaseB', db, a],
        capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        print(r.stdout)
        print(r.stderr, file=sys.stderr)
        raise AssertionError('phase B (post-restart resume) failed')
    print(r.stdout.strip())
    shutil.rmtree(tmp, ignore_errors=True)
    print('RESUME-RESTART TEST PASS')


if __name__ == '__main__':
    if len(sys.argv) > 2 and sys.argv[1] == 'phaseB':
        phase_b(sys.argv[2], sys.argv[3])
    else:
        main()


def test_resume_restart():
    main()
