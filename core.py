"""
VidSweep — core engine.
Scans folders, fingerprints videos, groups exact and perceptual duplicates.

Pipeline:
  1. Scan: walk folder tree, collect video files (extension + magic check optional)
  2. Exact hash: SHA-256 of file content (streamed). Byte-identical files group instantly.
  3. Perceptual hash (pHash): ffmpeg extracts frames at several timestamps; each frame
     gets a 64-bit DCT perceptual hash. Two videos are "visually same" if their
     frame-hash sets match closely. This catches re-encodes, container changes,
     resolution changes.
  4. Grouping: union-find merges files into duplicate groups.

Everything is cached in SQLite so re-scans only fingerprint new/changed files.
"""

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import imagehash
    from PIL import Image
    HAVE_IMAGEHASH = True
except ImportError:
    HAVE_IMAGEHASH = False

try:
    import numpy as _np
    _have_numpy = True
    # numpy >= 2.0: hardware popcount enables a much faster Hamming path
    _have_popcount = hasattr(_np, 'bitwise_count')
except ImportError:
    _have_numpy = False
    _have_popcount = False

VIDEO_EXTS = {'.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm',
              '.m4v', '.mpg', '.mpeg', '.ts', '.mts', '.m2ts', '.vob',
              '.3gp', '.ogv', '.rm', '.rmvb', '.asf', '.divx', '.f4v'}

DEFAULT_FFMPEG = r'C:\ffmpeg\ffmpeg-8.1.1-essentials_build\bin\ffmpeg.exe'
DEFAULT_FFPROBE = r'C:\ffmpeg\ffmpeg-8.1.1-essentials_build\bin\ffprobe.exe'

# user-saved override (set by the GUI when the user browses for ffmpeg.exe)
_ffmpeg_override = None


def set_ffmpeg_override(ffmpeg_exe):
    """Point the app at a specific ffmpeg.exe; ffprobe is expected beside it."""
    global _ffmpeg_override
    _ffmpeg_override = ffmpeg_exe


def find_ffmpeg():
    # 0) CI/testing hook: skip machine-specific paths, use PATH only
    if os.environ.get('VIDSWEEP_SKIP_DEFAULT_FFMPEG'):
        from shutil import which
        return which('ffmpeg')
    # 1) explicit override for this session
    if _ffmpeg_override and os.path.isfile(_ffmpeg_override):
        return _ffmpeg_override
    # 2) path saved by a previous session
    saved = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ffmpeg_path.txt')
    if os.path.isfile(saved):
        try:
            with open(saved) as fh:
                p = fh.read().strip()
            if p and os.path.isfile(p):
                return p
        except OSError:
            pass
    # 3) known default + common install locations
    import glob
    candidates = [DEFAULT_FFMPEG]
    for base in (r'C:\ffmpeg', r'C:\Program Files\ffmpeg', r'C:\Program Files (x86)\ffmpeg',
                 os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Microsoft', 'WinGet', 'Packages')):
        if base and os.path.isdir(base):
            candidates += glob.glob(os.path.join(base, '**', 'bin', 'ffmpeg.exe'), recursive=True)
    for c in candidates:
        if os.path.isfile(c):
            return c
    # 4) whatever is on PATH
    from shutil import which
    return which('ffmpeg')

SAMPLE_TIMES = [0.10, 0.35, 0.60, 0.85]   # fractions of duration to sample
HAMMING_THRESHOLD = 8                      # per-frame max bit difference

# On Windows, prevent ffmpeg/ffprobe console windows from popping up
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0


def find_ffprobe():
    # prefer sitting beside whatever ffmpeg was chosen
    ff = find_ffmpeg()
    if ff:
        cand = os.path.join(os.path.dirname(ff), 'ffprobe.exe')
        if os.path.isfile(cand):
            return cand
    for p in (DEFAULT_FFPROBE, ):
        if os.path.isfile(p):
            return p
    from shutil import which
    return which('ffprobe')


class Cancelled(Exception):
    pass


class VideoOrganizer:
    def __init__(self, db_path=None):
        if db_path is None:
            app_dir = os.path.dirname(os.path.abspath(__file__))
        else:
            app_dir = os.path.dirname(db_path)
        if not db_path:
            db_path = os.path.join(app_dir, 'library.db')
        os.makedirs(app_dir, exist_ok=True)
        self.db_path = db_path
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self._db_lock = threading.Lock()
        self._cancel = threading.Event()
        self._pause = threading.Event()
        # live registry of running ffmpeg/ffprobe processes so cancel can kill
        # them instead of waiting out long decodes of huge files
        self._procs = set()
        self._procs_lock = threading.Lock()
        self.db.execute("""CREATE TABLE IF NOT EXISTS files(
            path TEXT PRIMARY KEY,
            size INTEGER,
            mtime REAL,
            sha256 TEXT,
            phash TEXT,
            duration REAL,
            width INTEGER,
            height INTEGER,
            fps REAL,
            vcodec TEXT,
            thumbnail BLOB)""")
        self.db.execute(
            'CREATE INDEX IF NOT EXISTS idx_files_sha ON files(sha256)')
        self.db.commit()

    # ------------------------------------------------------------------ util
    def cancel(self):
        self._cancel.set()
        # kill any in-flight ffmpeg/ffprobe immediately — don't wait out the decode
        with self._procs_lock:
            procs = list(self._procs)
        for p in procs:
            try:
                p.kill()
            except Exception:
                pass

    def pause(self):
        self._pause.set()

    def resume(self):
        self._pause.clear()

    def _wait_if_paused(self):
        """Blocks while paused; raises Cancelled if cancel pressed during pause."""
        while self._pause.is_set() and not self._cancel.is_set():
            time.sleep(0.2)
        self._check_cancel()

    def _check_cancel(self):
        if self._cancel.is_set():
            raise Cancelled()

    @staticmethod
    def _iter_videos(roots, recursive=True):
        seen = set()
        for root in roots:
            root = os.path.abspath(root)
            if not os.path.isdir(root):
                continue
            if recursive:
                for dirpath, dirnames, filenames in os.walk(root):
                    dirnames[:] = [d for d in dirnames if d != '$RECYCLE.BIN']
                    for fn in filenames:
                        ext = os.path.splitext(fn)[1].lower()
                        if ext in VIDEO_EXTS:
                            p = os.path.join(dirpath, fn)
                            rp = os.path.normcase(p)
                            if rp not in seen:
                                seen.add(rp)
                                yield p
            else:
                for fn in os.listdir(root):
                    ext = os.path.splitext(fn)[1].lower()
                    if ext in VIDEO_EXTS:
                        yield os.path.join(root, fn)

    def _sha256(self, path, size):
        h = hashlib.sha256()
        with open(path, 'rb') as f:
            while True:
                chunk = f.read(4 * 1024 * 1024)
                if not chunk:
                    break
                h.update(chunk)
                # abort promptly on cancel/pause instead of reading GBs first
                self._check_cancel()
                self._wait_if_paused()
        return h.hexdigest()

    def _run_tracked(self, cmd, timeout, **kw):
        """Run a subprocess registered for kill-on-cancel.

        On timeout the process is killed (not left orphaned) and
        TimeoutExpired re-raised.
        """
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, **kw)
        with self._procs_lock:
            self._procs.add(proc)
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass
            raise
        finally:
            with self._procs_lock:
                self._procs.discard(proc)
        return type('R', (), {'returncode': proc.returncode,
                              'stdout': out, 'stderr': err})()

    def _ffprobe_info(self, path):
        ffprobe = find_ffprobe()
        try:
            self._check_cancel()
            out = self._run_tracked(
                [ffprobe, '-v', 'error', '-print_format', 'json',
                 '-show_entries',
                 'format=duration:stream=codec_type,width,height,r_frame_rate,codec_name',
                 path],
                timeout=30, text=True,
                creationflags=_NO_WINDOW)
            info = json.loads(out.stdout or '{}')
        except Cancelled:
            raise  # never swallow cancellation inside broad error handling
        except Exception:
            return {}
        dur = None
        fmt = info.get('format', {})
        if fmt.get('duration'):
            dur = float(fmt['duration'])
        streams = info.get('streams') or []
        vs = next((s for s in streams if s.get('codec_type') == 'video'), {})
        fps = None
        if vs.get('r_frame_rate'):
            num, _, den = str(vs['r_frame_rate']).partition('/')
            try:
                fps = float(num) / float(den or 1)
            except (ValueError, ZeroDivisionError):
                fps = None
        return {'duration': dur,
                'width': vs.get('width'),
                'height': vs.get('height'),
                'fps': fps,
                'vcodec': vs.get('codec_name')}

    # ------------------------------------------------------------- thumbnails
    def _extract_frames(self, path, duration, tmpdir):
        """Extract sample frames as JPEG bytes list.

        All 4 timestamps are extracted in ONE ffmpeg process: the same file is
        passed as 4 inputs, each with its own fast input-side seek. This cuts
        process-spawn overhead 4x vs one ffmpeg call per frame — identical
        output frames, identical quality.
        """
        ffmpeg = find_ffmpeg()
        frames = []
        times = [duration * frac for frac in SAMPLE_TIMES]
        import uuid
        run_dir = os.path.join(tmpdir, uuid.uuid4().hex)
        os.makedirs(run_dir, exist_ok=True)
        try:
            cmd = [ffmpeg, '-y', '-v', 'error']
            for t in times:
                cmd += ['-ss', f'{t:.3f}', '-i', path]
            outs = []
            for i in range(len(times)):
                out = os.path.join(run_dir, f'f{i}.jpg')
                outs.append(out)
                cmd += ['-map', f'{i}:v', '-frames:v', '1',
                        '-vf', 'scale=256:-2', out]
            try:
                r = self._run_tracked(cmd, timeout=120,
                                      creationflags=_NO_WINDOW)
                if r.returncode not in (0, 1):  # 1 can mean trailing garbage on some files
                    raise RuntimeError(r.stderr.decode(errors='replace')[:300])
                for out in outs:
                    try:
                        with open(out, 'rb') as f:
                            data = f.read()
                        if len(data) > 100:
                            frames.append(data)
                    except OSError:
                        pass
            except subprocess.TimeoutExpired:
                pass
            except Cancelled:
                raise  # propagate cancellation out of frame extraction
        finally:
            shutil.rmtree(run_dir, ignore_errors=True)
        return frames

    def _make_thumb_and_phashes(self, path, duration):
        """Returns (thumbnail_jpeg_bytes, [phash_str, ...])."""
        tmpdir = os.path.join(os.environ.get('TEMP', '/tmp'), 'vidorg_cache')
        os.makedirs(tmpdir, exist_ok=True)
        frames = self._extract_frames(path, duration or 0, tmpdir)
        if not frames:
            return None, []
        thumb = frames[0]
        hashes = []
        if HAVE_IMAGEHASH:
            for data in frames:
                try:
                    import io
                    img = Image.open(io.BytesIO(data))
                    ph = imagehash.phash(img)
                    hashes.append(str(ph))
                except Exception:
                    pass
        return thumb, hashes

    # ------------------------------------------------------------------- scan
    def scan(self, roots, recursive=True, progress=None):
        """
        Full pipeline over given roots.
        progress callback: progress(phase:str, done:int, total:int, current_path:str)
        Returns dict summary.
        """
        self._cancel.clear()
        t0 = time.time()

        # Phase 0: enumerate
        paths = list(self._iter_videos(roots, recursive))
        total = len(paths)
        if progress:
            progress('scan', 0, total, '')
        stats = {'scanned': 0, 'hashed_exact': 0, 'hashed_perceptual': 0,
                 'skipped_cached': 0, 'errors': 0, 'reused_identical': 0}

        cur = self.db.cursor()
        # require phash too: a row with sha but NULL phash means perceptual
        # fingerprinting failed last time (ffprobe/ffmpeg error) — reprocess
        # it instead of skipping it forever.
        known = {os.path.normcase(r[0]): r for r in cur.execute(
            'SELECT path,size,mtime,sha256,phash FROM files')}

        to_process = []
        for p in paths:
            try:
                st = os.stat(p)
            except OSError:
                continue
            k = os.path.normcase(p)
            rec = known.get(k)
            # skip only if unchanged AND fully fingerprinted (sha + phash)
            if rec and rec[3] and rec[4] is not None \
                    and rec[1] == st.st_size and abs(rec[2] - st.st_mtime) < 1:
                stats['skipped_cached'] += 1
                continue
            to_process.append((p, st.st_size, st.st_mtime))

        work_total = len(to_process)
        done = 0
        errors = []

        def process_one(item):
            p, size, mtime = item
            # Workers MUST honor pause/cancel too — otherwise "pause" only
            # freezes the progress display while decoding continues silently,
            # and resume floods the UI with a backlog of updates.
            self._wait_if_paused()
            sha = digests.get(p)
            duration = width = height = fps = vcodec = None
            thumb = None
            hashes = []
            # Byte-identical content => identical frames/metadata. If another
            # file with the same SHA was already fingerprinted (this scan or a
            # previous one), reuse its result instead of decoding again.
            reused = False
            if sha:
                with self._db_lock:
                    row = self.db.execute(
                        'SELECT phash,duration,width,height,fps,vcodec,thumbnail '
                        'FROM files WHERE sha256=? AND phash IS NOT NULL AND path<>? '
                        'LIMIT 1', (sha, p)).fetchone()
                if row:
                    phash_json, duration, width, height, fps, vcodec, thumb = row
                    hashes = json.loads(phash_json) if phash_json else []
                    reused = True
                    stats['reused_identical'] += 1
            if not reused:
                try:
                    info = self._ffprobe_info(p)
                    duration = info.get('duration')
                    width = info.get('width')
                    height = info.get('height')
                    fps = info.get('fps')
                    vcodec = info.get('vcodec')
                    if duration and HAVE_IMAGEHASH:
                        thumb, hashes = self._make_thumb_and_phashes(p, duration)
                except Exception as e:
                    errors.append((p, f'ffprobe/frames: {e}'))
            cur2 = self.db.cursor()
            with self._db_lock:
                cur2.execute(
                    'INSERT OR REPLACE INTO files(path,size,mtime,sha256,phash,duration,width,height,fps,vcodec,thumbnail)'
                    ' VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                    (p, size, mtime, sha,
                     json.dumps(hashes) if hashes else None,
                     duration, width, height, fps, vcodec, thumb))
                self.db.commit()
            return p

        lock = threading.Lock()

        def guarded_progress(kind):
            nonlocal done
            with lock:
                done += 1
                if progress:
                    progress(kind, done, work_total, '')

        # Stage A: exact hashing (parallel, I/O bound); keep digests for Stage B
        if progress:
            progress('hashing', 0, work_total, '')
        digests = {}
        stageA_done = 0
        with ThreadPoolExecutor(max_workers=6) as ex:
            futures = {ex.submit(self._sha256, p, s): (p, s, m)
                       for p, s, m in to_process}
            for fut in as_completed(futures):
                self._wait_if_paused()
                self._check_cancel()
                p, s, m = futures[fut]
                try:
                    digest = fut.result()
                    # NOTE: no DB write here — new files have no row yet
                    # (rows are created in Stage B); a bare UPDATE matched
                    # nothing. The digest travels in `digests` instead.
                    digests[p] = digest
                except Cancelled:
                    for f2 in futures:
                        f2.cancel()
                    raise
                except Exception as e:
                    errors.append((p, f'hash: {e}'))
                stageA_done += 1
                if progress:
                    progress('hashing', stageA_done, work_total, os.path.basename(p))
        self.db.commit()

        # Stage B: metadata + perceptual hashes + thumbnails (parallel decode)
        if progress:
            progress('perceptual', 0, work_total, '')
        stageB_done = 0
        with ThreadPoolExecutor(max_workers=max(2, (os.cpu_count() or 4) // 2)) as ex:
            futures = {ex.submit(process_one, item): item for item in to_process}
            for fut in as_completed(futures):
                self._wait_if_paused()
                self._check_cancel()
                try:
                    fut.result()
                except Cancelled:
                    for f2 in futures:
                        f2.cancel()
                    raise
                except Exception as e:
                    errors.append((futures[fut][0], str(e)))
                stageB_done += 1
                if progress:
                    progress('perceptual', stageB_done, work_total,
                             os.path.basename(futures[fut][0]))
        self.db.commit()

        elapsed = time.time() - t0
        stats['scanned'] = total
        stats['processed'] = len(to_process)
        stats['errors'] = len(errors)
        stats['error_details'] = errors[:10]
        stats['elapsed'] = round(elapsed, 1)
        return stats

    # ---------------------------------------------------------------- grouping
    def _load_records(self):
        rows = self.db.execute(
            'SELECT path,size,sha256,phash,duration,width,height,vcodec '
            'FROM files WHERE sha256 IS NOT NULL').fetchall()
        recs = []
        for path, size, sha, phash, dur, w, h, vc in rows:
            try:
                hashes = json.loads(phash) if phash else []
            except ValueError:
                hashes = []  # malformed JSON (e.g. interrupted write): skip
            if not isinstance(hashes, list):
                hashes = []
            recs.append({'path': path, 'size': size, 'sha': sha,
                         'hashes': hashes, 'duration': dur,
                         'width': w, 'height': h, 'vcodec': vc})
        return recs

    def find_duplicates(self, threshold=HAMMING_THRESHOLD, min_duration_overlap=0.90,
                        use_perceptual=True, progress=None):
        """
        Build duplicate groups.
        Returns list of groups: each is list of record dicts, sorted by quality score.
        """
        recs = self._load_records()
        n = len(recs)
        parent = list(range(n))

        def find(a):
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        # --- exact duplicates via sha map
        by_sha = {}
        for i, r in enumerate(recs):
            by_sha.setdefault(r['sha'], []).append(i)
        for idxs in by_sha.values():
            for j in idxs[1:]:
                union(idxs[0], j)

        # --- perceptual matching
        if use_perceptual and HAVE_IMAGEHASH:
            # Decode frame hashes once; _similarity works on the packed bits
            # (numpy path) or ImageHash objects (fallback). A cancelled scan
            # can leave phash = '[]' (empty list) or malformed JSON — treat
            # both as unfingerprinted and exclude from matching.
            for r in recs:
                hash_strs = r.get('hashes') or []
                if not hash_strs:
                    continue  # no usable frames: exclude from matching
                try:
                    ihs = [imagehash.hex_to_hash(h) for h in hash_strs]
                except (ValueError, TypeError):
                    continue  # malformed hash string: exclude, don't crash
                r['_ih'] = ihs  # kept for the no-numpy fallback in _similarity
                if _have_numpy:
                    import numpy as np
                    bits = np.array([list(ih.hash) for ih in ihs],
                                    dtype=bool).reshape(len(ihs), -1)
                    if _have_popcount:
                        # pack each 64-bit hash into one uint64 for the
                        # hardware-popcount fast path in _similarity
                        r['_packed'] = (np.packbits(bits, axis=1)
                                        .view('>u8').ravel()
                                        .astype(np.uint64))
                    r['_bits'] = bits
            # Pairwise comparison restricted to similar durations to keep O manageable.
            # Sort by duration and only compare neighbors within overlap window.
            order = sorted(range(n), key=lambda i: (recs[i]['duration'] or 0))
            compared = set()
            pairs_checked = 0
            for ai in range(len(order)):
                i = order[ai]
                di = recs[i]['duration']
                if not di:
                    continue
                for bi in range(ai + 1, len(order)):
                    j = order[bi]
                    dj = recs[j]['duration']
                    if not dj:
                        continue
                    lo, hi = min(di, dj), max(di, dj)
                    if hi == 0:
                        continue
                    if lo / hi < min_duration_overlap:
                        break  # sorted, so all later ones are even further apart
                    pairs_checked += 1
                    key = (min(i, j), max(i, j))
                    if key in compared or find(i) == find(j):
                        continue
                    compared.add(key)
                    sim = self._similarity(recs[i], recs[j], threshold)
                    if sim:
                        union(i, j)

        # --- collect groups
        groups = {}
        for i in range(n):
            groups.setdefault(find(i), []).append(recs[i])
        dupes = [g for g in groups.values() if len(g) > 1]
        for g in dupes:
            g.sort(key=self._quality_key, reverse=True)
        dupes.sort(key=lambda g: -(sum(r['size'] for r in g)))
        return dupes

    @staticmethod
    def _similarity(a, b, threshold):
        pa, pb = a.get('_packed'), b.get('_packed')
        if _have_popcount and pa is not None and pb is not None:
            # fast path: XOR + hardware popcount on packed uint64 hashes
            shorter, longer = (pa, pb) if len(pa) <= len(pb) else (pb, pa)
            dists = _np.bitwise_count(shorter[:, None] ^ longer[None, :])
            matched = int(_np.sum(dists.min(axis=1) <= threshold))
            return matched >= max(2, int(0.75 * len(shorter)))
        ha, hb = a.get('_bits'), b.get('_bits')
        if _have_numpy and ha is not None and hb is not None:
            # vectorized fallback: all frame-pair Hamming distances at once
            shorter, longer = (ha, hb) if len(ha) <= len(hb) else (hb, ha)
            dists = _np.count_nonzero(shorter[:, None, :] != longer[None, :, :],
                                      axis=2)
            matched = int(_np.sum(dists.min(axis=1) <= threshold))
            return matched >= max(2, int(0.75 * len(shorter)))
        # original imagehash path (no numpy at all)
        ia, ib = a.get('_ih'), b.get('_ih')
        if not ia or not ib:
            return False
        shorter = ia if len(ia) <= len(ib) else ib
        longer = ib if shorter is ia else ia
        matched = sum(1 for x in shorter
                      if min((x - y) for y in longer) <= threshold)
        return matched >= max(2, int(0.75 * len(shorter)))

    @staticmethod
    def _quality_key(r):
        """
        Higher = better copy. Bitrate (size/duration) is the primary signal:
        it captures actual detail much better than resolution alone — a sharp
        720p at 6 Mbps beats a blurry 1080p at 1 Mbps. Resolution is the tie-
        breaker, then a modern-codec bonus.
        """
        dur = r.get('duration') or 0
        bitrate = (r['size'] / dur) if dur > 0 else 0
        res = (r.get('height') or 0) * (r.get('width') or 0)
        good_codec = 1 if (r.get('vcodec') or '') in ('h264', 'hevc', 'av1', 'vp9') else 0
        return (bitrate, res, good_codec)

    # ------------------------------------------------------------ thumbnails
    def get_thumbnail(self, path):
        row = self.db.execute('SELECT thumbnail FROM files WHERE path=?', (path,)).fetchone()
        return row[0] if row else None

    # ---------------------------------------------------------------- organize
    def suggest_folders(self, paths):
        """Name-based category suggestion: strip noise tokens, cluster on shared prefix words."""
        from collections import Counter
        noise = {'1080p', '720p', '480p', '2160p', '4k', 'x264', 'x265', 'h264',
                 'hevc', 'aac', 'bluray', 'brrip', 'dvdrip', 'webrip', 'web-dl',
                 'webdl', 'hdrip', 'hd', 'cam', 'xvid', '10bit', '8bit', 'hdr',
                 'yify', 'rarbg', 'ettv', 'mp4', 'mkv', 'avi'}
        word_counts = Counter()
        tokenized = {}
        for p in paths:
            name = os.path.splitext(os.path.basename(p))[0]
            tokens = re.findall(r'[a-z0-9]+', name.lower())
            clean = [t for t in tokens if t not in noise and len(t) > 1]
            tokenized[p] = clean
            for t in clean[:3]:   # leading words matter most
                word_counts[t] += 1
        suggestions = []
        for p, toks in tokenized.items():
            cat = None
            for t in toks[:3]:
                if word_counts[t] >= 5:
                    cat = t.title()
                    break
            if cat is None:
                cat = 'Misc'
            suggestions.append((p, cat))
        return suggestions

    def close(self):
        self.db.close()


if __name__ == '__main__':
    print('Core module OK. Import this from the GUI.')
