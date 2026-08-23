"""Core-engine smoke test for CI: runs on Ubuntu, Windows, macOS runners.

Generates test videos with ffmpeg (same technique as local testing),
then verifies scan + exact + perceptual grouping works on that OS.
"""
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core


def make_video(path, w=640, h=360):
    ffmpeg = core.find_ffmpeg()
    if not ffmpeg:
        # CI runners have ffmpeg on PATH
        from shutil import which
        ffmpeg = which('ffmpeg')
    subprocess.run(
        [ffmpeg, '-y', '-v', 'error',
         '-f', 'lavfi', '-i', f'testsrc2=size={w}x{h}:rate=30:duration=8',
         '-f', 'lavfi', '-i', 'sine=frequency=440:duration=8',
         '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
         '-c:a', 'aac', path],
        check=True, capture_output=True)


def main():
    tmp = tempfile.mkdtemp(prefix='vidsweep_ci_')
    a = os.path.join(tmp, 'a')
    b = os.path.join(tmp, 'b')
    os.makedirs(a)
    os.makedirs(b)

    orig = os.path.join(a, 'original.mp4')
    make_video(orig)
    import shutil
    shutil.copy(orig, os.path.join(b, 'exact_copy.mp4'))
    reenc = os.path.join(b, 'reencoded.mkv')
    ffmpeg = core.find_ffmpeg() or 'ffmpeg'
    subprocess.run(
        [ffmpeg, '-y', '-v', 'error', '-i', orig,
         '-vf', 'scale=480x270', '-c:v', 'libx264', '-preset', 'medium',
         '-crf', '26', '-pix_fmt', 'yuv420p', reenc],
        check=True, capture_output=True)
    other = os.path.join(a, 'different.mp4')
    subprocess.run(
        [ffmpeg, '-y', '-v', 'error',
         '-f', 'lavfi', '-i', 'smptebars=size=640x360:rate=30:duration=8',
         '-f', 'lavfi', '-i', 'sine=frequency=220:duration=8',
         '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
         '-c:a', 'aac', other],
        check=True, capture_output=True)

    db = os.path.join(tmp, 'test.db')
    org = core.VideoOrganizer(db_path=db)
    stats = org.scan([a, b], recursive=True)
    print(f'[{sys.platform}] scan:', {k: v for k, v in stats.items() if k != 'error_details'})
    assert stats['errors'] == 0, f'errors: {stats.get("error_details")}'

    groups = org.find_duplicates()
    sizes = sorted(len(g) for g in groups)
    print(f'[{sys.platform}] groups:', sizes)
    # original + exact copy + re-encode = one group of 3; different.mp4 stays alone
    assert sizes == [3], f'expected one group of 3, got {sizes}'

    all_grouped = {r['path'] for g in groups for r in g}
    assert other not in all_grouped, 'false positive: unrelated video grouped'

    stats2 = org.scan([a, b], recursive=True)
    assert stats2['skipped_cached'] == 4 and stats2['processed'] == 0
    print(f'[{sys.platform}] ALL PASS')


if __name__ == '__main__':
    main()
