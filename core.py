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
            # a relocated cache (its old drive kept corrupting it) wins
            # over the default location next to core.py
            db_path = (self._saved_db_location() or
                       os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    'library.db'))
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.db_path = db_path
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        # NOTE: deliberately NOT WAL. WAL needs working shared-memory mmap on
        # the DB's filesystem; on some external-drive bridges (L:) it fails
        # with 'disk I/O error' at first write. Corruption is addressed by
        # batched commits (far fewer fsyncs) instead.
        # synchronous=OFF: this drive bridge also intermittently FAILS fsync
        # itself (SQLITE_IOERR at commit/DDL). The DB is a disposable
        # fingerprint cache (rescan is the recovery path), so skipping the
        # per-commit drive flush is the right trade-off here.
        try:
            self.db.execute('PRAGMA synchronous=OFF')
        except sqlite3.Error:
            pass
        # RLock: some paths nest (render_pil -> get_thumbnail); a plain Lock
        # would deadlock a thread against itself
        self._db_lock = threading.RLock()
        self._cancel = threading.Event()
        self._pause = threading.Event()
        # live registry of running ffmpeg/ffprobe processes so cancel can kill
        # them instead of waiting out long decodes of huge files
        self._procs = set()
        self._procs_lock = threading.Lock()
        # session currently being scanned (for pause-state persistence)
        self._active_session_id = None
        try:
            self._init_schema()
        except sqlite3.DatabaseError as e:
            if not self._is_db_corruption(e):
                raise
            # The cache file is in an unusable state (e.g. left behind by
            # an interrupted write on a flaky drive bridge). The DB is a
            # pure cache — worst case is a re-scan — so recreate it rather
            # than refuse to start; if the file can't even be deleted
            # (held open, failing drive), relocate the cache instead.
            print(f'VidSweep: library cache unusable ({e}); recreating it.',
                  flush=True)
            if not self._close_and_delete_db():
                self._migrate_db()
            else:
                self._fresh_connection()

    def _init_schema(self):
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
        self.db.execute("""CREATE TABLE IF NOT EXISTS sessions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created REAL,
            updated REAL,
            roots TEXT,
            recursive INTEGER,
            status TEXT)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS dismissed_groups(
            group_key TEXT PRIMARY KEY,
            paths TEXT NOT NULL,
            created REAL)""")
        # migration for caches created before session tracking existed
        cols = [r[1] for r in self.db.execute('PRAGMA table_info(files)')]
        if 'session_id' not in cols:
            self.db.execute('ALTER TABLE files ADD COLUMN session_id INTEGER')
        # pause state + enumerated total must survive app restarts / PC
        # reboots so a long scan can be resumed after either
        scols = [r[1] for r in self.db.execute('PRAGMA table_info(sessions)')]
        if 'paused' not in scols:
            self.db.execute(
                'ALTER TABLE sessions ADD COLUMN paused INTEGER DEFAULT 0')
        if 'total' not in scols:
            self.db.execute('ALTER TABLE sessions ADD COLUMN total INTEGER')
        self._safe_commit()

    # ------------------------------------------------------------------ util
    def _safe_commit(self, attempts=4):
        """Commit with bounded retries. Some USB drive bridges fail
        FlushFileBuffers intermittently under load (SQLITE_IOERR at commit);
        a short backoff turns a transient blip into a non-event instead of
        an error (or worse, a user thinking the cache is corrupt)."""
        for attempt in range(attempts):
            try:
                self.db.commit()  # the only direct commit — retries on blips
                return
            except sqlite3.OperationalError as e:
                if 'disk I/O' not in str(e) or attempt == attempts - 1:
                    raise
                time.sleep(0.25 * (attempt + 1))

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
        self._mark_session_paused(True)

    def resume(self):
        self._pause.clear()
        self._mark_session_paused(False)

    def _mark_session_paused(self, paused):
        """Persist the paused flag so a pause survives closing the app or a
        full PC restart — the startup resume prompt reads it from the DB."""
        sid = self._active_session_id
        if sid is None:
            return
        try:
            with self._db_lock:
                self.db.execute('UPDATE sessions SET paused=? WHERE id=?',
                                (1 if paused else 0, sid))
                self._safe_commit()
            self._write_scan_state(paused=paused)
        except sqlite3.Error:
            pass  # a failed pause-flag write must never break the pause itself

    def scan_state_path(self):
        return os.path.join(os.path.dirname(os.path.abspath(self.db_path)),
                            'scan_state.json')

    def _write_scan_state(self, status=None, paused=None):
        """Sidecar copy of the scan-session state. library.db is the source
        of truth, but its drive has shown it can come back unwritable after
        a hard shutdown (the recovery path then recreates it EMPTY, wiping
        the session record) — this tiny JSON keeps the 'unfinished scan over
        these folders' fact alive so the GUI can still offer a restart."""
        sid = self._active_session_id
        if sid is None:
            return
        try:
            with self._db_lock:
                row = self.db.execute(
                    'SELECT roots,recursive,status,paused,total FROM sessions '
                    'WHERE id=?', (sid,)).fetchone()
                if row is None:
                    return
                roots_json, recursive, st, pflag, total = row
                done = self.db.execute(
                    'SELECT COUNT(*) FROM files WHERE session_id=? AND '
                    'sha256 IS NOT NULL AND phash IS NOT NULL',
                    (sid,)).fetchone()[0]
            state = {'session_id': sid,
                     'roots': json.loads(roots_json) if roots_json else [],
                     'recursive': bool(recursive),
                     'status': status or st,
                     'paused': bool(pflag if paused is None else paused),
                     'total': total,
                     'done': done,
                     'updated': time.time()}
            with open(self.scan_state_path(), 'w') as fh:
                json.dump(state, fh)
        except Exception:
            pass  # best-effort sidecar; the DB remains the source of truth

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

    def _sha256(self, path):
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
        if not ffprobe:
            raise RuntimeError(
                'ffprobe not found — locate ffmpeg/ffprobe before scanning')
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
        output frames, identical quality. Two filter chains are tried: the
        plain scale filter first, then zscale (for codecs/colorspaces where
        scale misbehaves). If a chain fails (bad return code, timeout, or
        yields no readable frames), the next chain is attempted; only when
        every chain fails does the file count as unfingerprintable.
        """
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            raise RuntimeError(
                'ffmpeg not found — locate ffmpeg/ffprobe before scanning')
        frames = []
        times = [duration * frac for frac in SAMPLE_TIMES]
        import uuid
        run_dir = os.path.join(tmpdir, uuid.uuid4().hex)
        os.makedirs(run_dir, exist_ok=True)
        try:
            chains = ['scale=256:-2,format=yuvj420p',
                      'zscale=w=256:h=-2,format=yuvj420p']
            for chain in chains:
                frames = []
                cmd = [ffmpeg, '-y', '-v', 'error']
                for t in times:
                    cmd += ['-ss', f'{t:.3f}', '-i', path]
                outs = []
                for i in range(len(times)):
                    out = os.path.join(run_dir, f'f{i}.jpg')
                    outs.append(out)
                    cmd += ['-map', f'{i}:v', '-frames:v', '1',
                            '-vf', chain, out]
                try:
                    r = self._run_tracked(cmd, timeout=20,
                                          creationflags=_NO_WINDOW)
                    if r.returncode not in (0, 1):  # 1 can mean trailing garbage
                        continue  # this chain failed — try the next
                    for out in outs:
                        try:
                            with open(out, 'rb') as f:
                                data = f.read()
                            if len(data) > 100:
                                frames.append(data)
                        except OSError:
                            pass
                except subprocess.TimeoutExpired:
                    continue  # next chain
                except Cancelled:
                    raise  # propagate cancellation out of frame extraction
                if frames:
                    return frames
            return []
        finally:
            shutil.rmtree(run_dir, ignore_errors=True)

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

    # ------------------------------------------------------- cache relocation
    @staticmethod
    def _marker_path():
        return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'db_location.txt')

    @classmethod
    def _saved_db_location(cls):
        try:
            with open(cls._marker_path()) as fh:
                p = fh.read().strip()
            if p and os.path.isdir(os.path.dirname(p)):
                return p
        except OSError:
            pass
        return None

    def _close_and_delete_db(self):
        """Close the connection and remove the cache files. Returns False
        when the main file cannot be removed (another instance holds it
        open, or the drive refuses) — the caller must then relocate the
        cache instead of reconnecting to the same corrupt file."""
        with self._db_lock:
            try:
                self.db.close()
            except Exception:
                pass
            for ext in ('', '-journal', '-wal', '-shm'):
                try:
                    if os.path.isfile(self.db_path + ext):
                        os.remove(self.db_path + ext)
                except OSError:
                    pass
            return not os.path.isfile(self.db_path)

    def _fresh_connection(self):
        with self._db_lock:
            self.db = sqlite3.connect(self.db_path, check_same_thread=False)
            try:
                self.db.execute('PRAGMA synchronous=OFF')
            except sqlite3.Error:
                pass
            self._init_schema()

    def _rebuild_db(self):
        """The cache came back structurally corrupt: close it, delete it,
        start fresh. If the corrupt file cannot be deleted (another
        instance holds it open, or the drive refuses), relocate the cache
        to the local app-data drive instead of reconnecting to the same
        corrupt file. The DB is a disposable fingerprint cache — a re-scan
        is the recovery path. Everything it held (sessions included) is
        lost; the scan_state.json sidecar still remembers an unfinished
        scan, and the restarted scan writes a fresh session record."""
        if not self._close_and_delete_db():
            # the corrupt file is held open or undeletable — reconnecting
            # to it would corrupt again immediately; move instead
            return self._migrate_db()
        self._fresh_connection()
        return self.db_path

    def _migrate_db(self):
        """Relocate the cache to the local app-data drive (off the disk
        that keeps corrupting it) and remember the choice for future
        launches. Contents are NOT copied: the cache is disposable, and
        the old copy is the corrupt thing."""
        self._close_and_delete_db()  # best-effort: the old drive may refuse
        new_dir = os.path.join(os.environ.get('LOCALAPPDATA')
                               or os.path.expanduser('~'), 'VidSweep')
        os.makedirs(new_dir, exist_ok=True)
        self.db_path = os.path.join(new_dir, 'library.db')
        self._close_and_delete_db()  # stale leftovers from an older migration
        self._fresh_connection()
        try:
            with open(self._marker_path(), 'w') as fh:
                fh.write(self.db_path)
        except OSError:
            pass  # marker is best-effort; the scan works without it
        print(f'VidSweep: library cache relocated to {self.db_path}',
              flush=True)
        return self.db_path

    # ------------------------------------------------------------------- scan
    @staticmethod
    def _is_db_corruption(e):
        """The drive bridge under the cache is known to intermittently
        return bad pages ('database disk image is malformed') or fail
        mid-write ('disk I/O error'), and an interrupted write can leave
        'file is not a database'."""
        s = str(e).lower()
        return ('malformed' in s or 'not a database' in s
                or 'disk i/o' in s)

    def scan(self, roots, recursive=True, progress=None, session_id=None):
        """Full pipeline over given roots, with cache-corruption recovery.

        A malformed/dying cache used to kill the scan with 'database disk
        image is malformed'. The cache is disposable, so instead:
          1. rebuild it in place and restart the scan once;
          2. if the restart corrupts again, the rebuild itself fails (the
             bridge also fails DDL after corruption churn), or the corrupt
             file is undeletable — relocate the cache to the local
             app-data drive, remember the location, and restart once more;
          3. only if even the fresh location corrupts, fail — with an
             explanation pointing at the drive, not raw sqlite text.

        See _scan_impl for the session/progress contract.
        """
        try:
            return self._scan_impl(roots, recursive=recursive,
                                   progress=progress, session_id=session_id)
        except sqlite3.DatabaseError as e:
            if not self._is_db_corruption(e):
                raise
            return self._scan_recovering(roots, recursive, progress, e)

    def _scan_recovering(self, roots, recursive, progress, first_error):
        """Corruption-recovery ladder: rebuild in place, then relocate the
        cache off the failing drive. A recovery step ITSELF hitting a
        database error just moves the ladder down one rung — on the flaky
        bridge, the rebuild after a corruption event is exactly when DDL
        and commits tend to fail with 'disk I/O error'."""
        last = first_error
        migrated = False
        for action in ('rebuild', 'migrate'):
            try:
                if action == 'rebuild':
                    print(f'VidSweep: cache corrupt mid-scan ({last}); '
                          'rebuilding and restarting the scan.', flush=True)
                    if progress:
                        progress('scan', 0, 0,
                                 'library cache corrupted — rebuilding, '
                                 'scan restarts')
                    self._rebuild_db()
                else:
                    new_path = self._migrate_db()
                    migrated = True
                    if progress:
                        progress('scan', 0, 0,
                                 f'cache moved off the failing drive to '
                                 f'{new_path} — scan restarts')
                # the corrupt cache (and its session record) is gone — a
                # fresh session over the same roots is the only continuation
                stats = self._scan_impl(roots, recursive=recursive,
                                        progress=progress, session_id=None)
                if migrated:
                    stats['cache_migrated'] = self.db_path
                return stats
            except sqlite3.DatabaseError as e:
                if not self._is_db_corruption(e):
                    raise
                last = e
        raise RuntimeError(
            'The library cache keeps getting corrupted: it was rebuilt, '
            f'then moved to {self.db_path}, and corrupted again. Check that '
            'drive for errors (chkdsk or the manufacturer tool). Your video '
            'files are NOT affected — this only breaks the fingerprint '
            'cache.') from last

    def _scan_impl(self, roots, recursive=True, progress=None, session_id=None):
        """
        Full pipeline over given roots.

        session_id: None starts a new scan session (previous active ones are
        superseded); an existing id resumes that session, keeping the batch
        cumulative (files fingerprinted before a cancel are not redone).

        progress callback: progress(phase:str, done:int, total:int, current_path:str)
        Returns dict summary (includes 'session_id').
        """
        self._cancel.clear()
        self._pause.clear()
        t0 = time.time()
        roots = list(roots)

        # Resolve the scan session. A new scan supersedes still-active ones;
        # a resume reactivates the given session so its batch keeps growing.
        with self._db_lock:
            if session_id is None:
                self.db.execute(
                    "UPDATE sessions SET status='complete' WHERE status='active'")
                cur = self.db.execute(
                    'INSERT INTO sessions(created,updated,roots,recursive,status)'
                    ' VALUES(?,?,?,?,?)',
                    (t0, t0, json.dumps(roots), 1 if recursive else 0, 'active'))
                session_id = cur.lastrowid
            else:
                row = self.db.execute(
                    'SELECT id FROM sessions WHERE id=?', (session_id,)).fetchone()
                if row is None:
                    raise ValueError(f'unknown scan session id {session_id}')
                self.db.execute(
                    "UPDATE sessions SET updated=?, status='active', paused=0 "
                    'WHERE id=?', (t0, session_id))
            self._safe_commit()
        self._active_session_id = session_id
        self._write_scan_state(paused=False)

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
        skipped_paths = []
        # SHA digests recovered from the cache: a cancel during hashing used
        # to lose all Stage-A work; checkpointed rows let a resume pick the
        # digests back up and go straight to the perceptual stage.
        digests = {}
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
                skipped_paths.append(p)
                continue
            if rec and rec[3] and rec[4] is None \
                    and rec[1] == st.st_size and abs(rec[2] - st.st_mtime) < 1:
                digests[p] = rec[3]  # sha checkpointed, perceptual still pending
            to_process.append((p, st.st_size, st.st_mtime))

        # Stamp skipped files with this session so the batch covers everything
        # the scan saw, not just what it had to fingerprint.
        if skipped_paths:
            with self._db_lock:
                self.db.executemany(
                    'UPDATE files SET session_id=? WHERE path=?',
                    [(session_id, p) for p in skipped_paths])
                self._safe_commit()

        # remember the enumerated total: the startup resume prompt shows
        # 'N of total videos fingerprinted'
        with self._db_lock:
            self.db.execute('UPDATE sessions SET total=? WHERE id=?',
                            (total, session_id))
            self._safe_commit()
        self._write_scan_state()

        work_total = len(to_process)
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
                    with self._db_lock:
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
                    'INSERT OR REPLACE INTO files(path,size,mtime,sha256,phash,duration,width,height,fps,vcodec,thumbnail,session_id)'
                    ' VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                    (p, size, mtime, sha,
                     json.dumps(hashes) if hashes else None,
                     duration, width, height, fps, vcodec, thumb, session_id))
            return p

        # --- pipelined scan: hash and fingerprint run concurrently, no
        # Stage A/B barrier. Each SHA digest streams its file into the
        # perceptual pool immediately ('working' phase, done over 2x count).
        if progress:
            pipeline_total = max(1, 2 * work_total)
            pipeline_done = 0
            progress('working', 0, pipeline_total, '')

        checkpoint_buf = []
        last_checkpoint = time.time()

        def flush_checkpoint():
            """Persist SHA digests in batches (INSERT OR IGNORE — never
            clobbers a full row) so a resume never re-hashes checkpointed
            files. Runs on a size/time budget and at the end."""
            if not checkpoint_buf:
                return
            with self._db_lock:
                self.db.executemany(
                    'INSERT OR IGNORE INTO files(path,size,mtime,sha256)'
                    ' VALUES(?,?,?,?)',
                    checkpoint_buf)
                self._safe_commit()
            checkpoint_buf.clear()
            nonlocal last_checkpoint
            last_checkpoint = time.time()

        perceptual_pool = ThreadPoolExecutor(
            max_workers=max(2, (os.cpu_count() or 4) // 2))
        perceptual_futs = {}
        # files with a cached digest skip straight to the perceptual stage
        for item in to_process:
            if item[0] in digests:
                fut = perceptual_pool.submit(process_one, item)
                perceptual_futs[fut] = item

        try:
            with ThreadPoolExecutor(max_workers=6) as hash_pool:
                futures = {hash_pool.submit(self._sha256, p): (p, s, m)
                           for p, s, m in to_process if p not in digests}
                for fut in as_completed(futures):
                    self._wait_if_paused()
                    self._check_cancel()
                    p, s, m = futures[fut]
                    try:
                        digest = fut.result()
                        # NOTE: no full-row write here — new files have no
                        # row yet; the digest is checkpointed in batches and
                        # travels to the perceptual stage via `digests`.
                        digests[p] = digest
                        checkpoint_buf.append((p, s, m, digest))
                        if len(checkpoint_buf) >= 32 or \
                                time.time() - last_checkpoint > 3.0:
                            flush_checkpoint()
                        stats['hashed_exact'] += 1
                    except Cancelled:
                        for f2 in futures:
                            f2.cancel()
                        raise
                    except Exception as e:
                        errors.append((p, f'hash: {e}'))
                    if progress:
                        pipeline_done += 1
                        progress('working', pipeline_done, pipeline_total,
                                 os.path.basename(p))
                    # dispatch perceptual work now — no stage barrier
                    fut2 = perceptual_pool.submit(process_one, (p, s, m))
                    perceptual_futs[fut2] = (p, s, m)
            flush_checkpoint()  # also runs on cancel: keep every digest read

            # drain the perceptual side (some futures already finished during
            # the hashing phase and complete instantly here)
            checked = 0
            for fut in as_completed(perceptual_futs):
                self._wait_if_paused()
                self._check_cancel()
                item = perceptual_futs[fut]
                try:
                    fut.result()
                except Cancelled:
                    for f2 in perceptual_futs:
                        f2.cancel()
                    raise
                except Exception as e:
                    errors.append((item[0], str(e)))
                checked += 1
                if checked % 16 == 0:
                    with self._db_lock:
                        self._safe_commit()  # batch: keep resume progress durable
                if progress:
                    pipeline_done += 1
                    progress('working', pipeline_done, pipeline_total,
                             os.path.basename(item[0]))
            perceptual_pool.shutdown(wait=True)
            with self._db_lock:
                self._safe_commit()
        except Cancelled:
            # on cancel this returns quickly: workers hit their cancel gates
            # (ffmpeg was already killed by cancel()) instead of long decodes
            perceptual_pool.shutdown(wait=True)
            with self._db_lock:
                self._safe_commit()  # persist whatever completed, even on cancel
            self._write_scan_state()  # sidecar mirrors the stop/completion
            raise
        self._safe_commit()

        elapsed = time.time() - t0
        stats['scanned'] = total
        stats['processed'] = len(to_process)
        stats['errors'] = len(errors)
        stats['error_details'] = errors[:10]
        stats['elapsed'] = round(elapsed, 1)
        stats['session_id'] = session_id
        # scan ran to completion: drop cache rows whose files vanished under
        # the scanned roots (ghost rows would surface deleted videos as
        # duplicates forever). Only for recursive scans — a non-recursive
        # scan never enumerated subfolders, so pruning there would be wrong.
        stats['pruned'] = self._prune_missing(roots) if recursive else 0
        with self._db_lock:
            self.db.execute(
                "UPDATE sessions SET updated=?, status='complete' WHERE id=?",
                (time.time(), session_id))
            self._safe_commit()
        self._write_scan_state(status='complete')
        self._active_session_id = None
        return stats

    def _prune_missing(self, roots):
        """Remove cache rows for files that no longer exist under the scanned
        roots. Roots that have vanished themselves (offline / unplugged
        drives) are skipped entirely — never prune what we cannot see.
        Returns the number of rows removed."""
        prefixes = [os.path.normcase(os.path.abspath(r)).rstrip(os.sep) + os.sep
                    for r in roots if os.path.isdir(r)]
        if not prefixes:
            return 0
        gone = []
        with self._db_lock:
            rows = self.db.execute('SELECT path FROM files').fetchall()
        for (path,) in rows:
            k = os.path.normcase(os.path.abspath(path))
            if not any(k.startswith(p) for p in prefixes):
                continue  # row belongs to a different root — leave it alone
            if not os.path.exists(path):
                gone.append(path)
        if gone:
            with self._db_lock:
                self.db.executemany('DELETE FROM files WHERE path=?',
                                    [(p,) for p in gone])
                self._safe_commit()
        return len(gone)

    # ---------------------------------------------------------------- grouping
    def _load_records(self, session_id=None):
        with self._db_lock:  # safe to read even while a scan is writing
            q = ('SELECT path,size,sha256,phash,duration,width,height,vcodec '
                 'FROM files WHERE sha256 IS NOT NULL')
            params = ()
            if session_id is not None:
                q += ' AND session_id=?'
                params = (session_id,)
            rows = self.db.execute(q, params).fetchall()
        recs = []
        seen = set()
        for path, size, sha, phash, dur, w, h, vc in rows:
            # dedupe by case-insensitive path so two cache rows that differ
            # only in case can never be paired as duplicates of each other
            k = os.path.normcase(path)
            if k in seen:
                continue
            seen.add(k)
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

    # ------------------------------------------------- not-duplicates dismissals
    @staticmethod
    def group_key(paths):
        """Stable identity for a duplicate group.

        The key is the SHA-256 of the sorted, case-folded, absolute member
        paths. It is unaffected by scan order or by the order the caller
        lists the paths, so the same set of files dismisses reliably across
        rescans and restarts.
        """
        if isinstance(paths, (str, bytes, os.PathLike)):
            paths = [paths]
        canonical = sorted(
            os.path.normcase(os.path.abspath(p)).casefold()
            for p in paths
        )
        digest = hashlib.sha256()
        for p in canonical:
            # NUL cannot appear in file paths and keeps concatenated paths
            # unambiguous
            digest.update(p.encode('utf-8', 'surrogateescape'))
            digest.update(b'\0')
        return digest.hexdigest()

    @staticmethod
    def _stored_paths_json(paths):
        entries = []
        seen = set()
        for p in paths or ():
            ap = os.path.abspath(p)
            norm = os.path.normcase(ap).casefold()
            if norm in seen:
                continue
            seen.add(norm)
            entries.append((norm, str(ap)))
        entries.sort(key=lambda e: e[0])
        return json.dumps([ap for _, ap in entries])

    def dismiss_group(self, group_key=None, paths=None):
        """Persist a group dismissal.

        Accepts the precomputed stable group key, the member paths (which
        are used to compute it), or both. Returns the group key.
        """
        if paths is None and isinstance(group_key, (list, tuple, set, frozenset)):
            paths = list(group_key)
            group_key = None
        if paths is not None and isinstance(paths, (str, bytes, os.PathLike)):
            paths = [paths]
        path_list = list(paths) if paths is not None else None
        if group_key is None:
            if not path_list:
                raise ValueError(
                    'dismiss_group needs a group key or the member paths')
            group_key = self.group_key(path_list)
        stored = self._stored_paths_json(path_list) if path_list is not None else '[]'
        with self._db_lock:
            self.db.execute(
                'INSERT OR REPLACE INTO dismissed_groups(group_key, paths, created)'
                ' VALUES(?,?,?)',
                (group_key, stored, time.time()))
            self._safe_commit()
        return group_key

    def undismiss_group(self, group_key=None, paths=None):
        """Remove a previously stored dismissal, restoring the group.

        Accepts the stable group key or the member paths used to compute it.
        """
        if paths is None and isinstance(group_key, (list, tuple, set, frozenset)):
            paths = list(group_key)
            group_key = None
        if paths is not None:
            if isinstance(paths, (str, bytes, os.PathLike)):
                paths = [paths]
            group_key = self.group_key(list(paths))
        if group_key is None:
            raise ValueError('undismiss_group needs a group key or member paths')
        with self._db_lock:
            cur = self.db.execute(
                'DELETE FROM dismissed_groups WHERE group_key=?', (group_key,))
            self._safe_commit()
        return cur.rowcount > 0

    def list_dismissed(self):
        """Return stored dismissals as dicts with group_key/paths/created."""
        with self._db_lock:
            rows = self.db.execute(
                'SELECT group_key, paths, created FROM dismissed_groups '
                'ORDER BY created, group_key').fetchall()
        out = []
        for key, stored, created in rows:
            try:
                paths = json.loads(stored) if stored else []
            except (ValueError, TypeError):
                paths = []
            if not isinstance(paths, list):
                paths = []
            out.append({'group_key': key, 'paths': paths,
                        'created': created})
        return out

    def clear_dismissed(self):
        """Delete every stored dismissal; returns the number removed."""
        with self._db_lock:
            cur = self.db.execute('DELETE FROM dismissed_groups')
            self._safe_commit()
        return cur.rowcount

    def _dismissed_group_keys(self):
        with self._db_lock:
            rows = self.db.execute(
                'SELECT group_key FROM dismissed_groups').fetchall()
        return {row[0] for row in rows}

    def get_last_session(self):
        """Most recent scan session, for the GUI's Resume button.

        Returns dict(id, roots, recursive, status, done, active, paused, total)
        or None. 'done' counts files in the batch that are fully fingerprinted.
        """
        with self._db_lock:
            row = self.db.execute(
                'SELECT id,roots,recursive,status,paused,total FROM sessions '
                'ORDER BY id DESC LIMIT 1').fetchone()
            if not row:
                return None
            sid, roots_json, recursive, status, paused, total = row
            try:
                roots = json.loads(roots_json) if roots_json else []
            except ValueError:
                roots = []
            done = self.db.execute(
                'SELECT COUNT(*) FROM files '
                'WHERE session_id=? AND sha256 IS NOT NULL AND phash IS NOT NULL',
                (sid,)).fetchone()[0]
        return {'id': sid, 'roots': roots, 'recursive': bool(recursive),
                'status': status, 'done': done, 'active': status == 'active',
                'paused': bool(paused), 'total': total}

    def find_duplicates(self, threshold=HAMMING_THRESHOLD, min_duration_overlap=0.90,
                        use_perceptual=True, progress=None, session_id=None,
                        fast_match=True):
        """
        Build duplicate groups.
        session_id: None matches the entire library cache; an id restricts
        matching to the files of that scan session (a partial-scan batch).
        fast_match: candidate pairs come from 8x8-bit banding of the frame
        hashes instead of comparing every duration-compatible pair — orders
        of magnitude fewer comparisons on huge libraries. Banding is
        probabilistic: a true match is overwhelmingly likely (not provably
        guaranteed) to share an identical band, so a rare match may be
        missed. fast_match=False gives the exact exhaustive enumeration.
        Returns list of groups: each is list of record dicts, sorted by quality score.
        """
        recs = self._load_records(session_id)
        n = len(recs)
        if progress:
            progress('match', 0, n, '')
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
            for ri, r in enumerate(recs):
                if progress:
                    progress('match', ri, n, r['path'])
                hash_strs = r.get('hashes') or []
                if not hash_strs:
                    continue  # no usable frames: exclude from matching
                try:
                    ihs = [imagehash.hex_to_hash(h) for h in hash_strs]
                except (ValueError, TypeError):
                    continue  # malformed hash string: exclude, don't crash
                r['_ih'] = ihs  # kept for the no-numpy fallback in _similarity
                # packed per-frame ints for the banding candidate generator
                ints = []
                for ih in ihs:
                    v = 0
                    for bit in ih.hash.flatten():
                        v = (v << 1) | int(bit)
                    ints.append(v)
                r['_ints'] = ints
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

            def duration_ok(i, j):
                di, dj = recs[i]['duration'], recs[j]['duration']
                if not di or not dj:
                    return False
                lo, hi = min(di, dj), max(di, dj)
                return hi > 0 and lo / hi >= min_duration_overlap

            def pairs_banded():
                """LSH candidate generation: two records can only match if
                some 8-bit slice of some same-indexed frame hash is identical
                (a frame within the Hamming threshold keeps most slices
                intact). Buckets turn O(n^2) comparisons into near-linear."""
                buckets = {}
                for i, r in enumerate(recs):
                    for fi, v in enumerate(r.get('_ints') or ()):
                        for b in range(8):
                            key = (fi, b, (v >> (8 * b)) & 255)
                            buckets.setdefault(key, []).append(i)
                seen = set()
                for idxs in buckets.values():
                    if len(idxs) < 2:
                        continue
                    for x in range(len(idxs)):
                        i = idxs[x]
                        for y in range(x + 1, len(idxs)):
                            j = idxs[y]
                            key = (i, j) if i < j else (j, i)
                            if key not in seen:
                                seen.add(key)
                                yield key

            def pairs_window():
                """Exhaustive enumeration: every pair within the duration
                overlap window (the pre-banding exact behavior)."""
                order = sorted(range(n), key=lambda i: (recs[i]['duration'] or 0))
                for ai in range(len(order)):
                    i = order[ai]
                    if not recs[i]['duration']:
                        continue
                    for bi in range(ai + 1, len(order)):
                        j = order[bi]
                        if not recs[j]['duration']:
                            continue
                        if not duration_ok(i, j):
                            break  # sorted, so all later ones are even further apart
                        yield (i, j)

            if fast_match:
                # pairs_banded() already yields unique (i, j) pairs via its
                # own seen-set — materializing them here (sorted(set(...)))
                # would hold EVERY candidate pair in memory at once, which
                # explodes to O(n^2) tuples on duplicate-heavy libraries.
                # Iterate the generator directly; progress stays blind on the
                # fast path (total unknown), which is the point of fast match.
                cand = pairs_banded()
                total = -1
            else:
                cand = pairs_window()
                total = -1

            checked = 0
            for i, j in cand:
                checked += 1
                if progress and total > 0 and checked % 500 == 0:
                    progress('match', checked, total, '')
                if find(i) == find(j):
                    continue
                if not duration_ok(i, j):
                    continue
                if self._similarity(recs[i], recs[j], threshold):
                    union(i, j)

        # --- collect groups
        groups = {}
        for i in range(n):
            groups.setdefault(find(i), []).append(recs[i])
        dupes = [g for g in groups.values() if len(g) > 1]
        for g in dupes:
            g.sort(key=self._quality_key, reverse=True)
        dupes.sort(key=lambda g: -sum(r['size'] for r in g))
        dismissed = self._dismissed_group_keys()
        if dismissed and dupes:
            dupes = [g for g in dupes
                     if self.group_key([r['path'] for r in g]) not in dismissed]
        return dupes

    @staticmethod
    def _similarity(a, b, threshold):
        """Symmetric: the verdict must not depend on argument order. With
        equal frame counts both directions are evaluated and the weaker one
        wins — otherwise grouping could vary between scans of identical
        data depending on DB row order."""
        pa, pb = a.get('_packed'), b.get('_packed')
        if _have_popcount and pa is not None and pb is not None:
            # fast path: XOR + hardware popcount on packed uint64 hashes
            shorter, longer = (pa, pb) if len(pa) <= len(pb) else (pb, pa)
            ra, rb = shorter, longer
            dists = _np.bitwise_count(ra[:, None] ^ rb[None, :])
            matched = int(_np.sum(dists.min(axis=1) <= threshold))
            if len(pa) == len(pb):
                matched_rev = int(_np.sum(dists.min(axis=0) <= threshold))
                matched = min(matched, matched_rev)
            return matched >= max(2, int(0.75 * len(ra)))
        ha, hb = a.get('_bits'), b.get('_bits')
        if _have_numpy and ha is not None and hb is not None:
            # vectorized fallback: all frame-pair Hamming distances at once
            shorter, longer = (ha, hb) if len(ha) <= len(hb) else (hb, ha)
            ra, rb = shorter, longer
            dists = _np.count_nonzero(ra[:, None, :] != rb[None, :, :],
                                      axis=2)
            matched = int(_np.sum(dists.min(axis=1) <= threshold))
            if len(ha) == len(hb):
                matched_rev = int(_np.sum(dists.min(axis=0) <= threshold))
                matched = min(matched, matched_rev)
            return matched >= max(2, int(0.75 * len(ra)))
        # original imagehash path (no numpy at all)
        ia, ib = a.get('_ih'), b.get('_ih')
        if not ia or not ib:
            return False
        shorter = ia if len(ia) <= len(ib) else ib
        longer = ib if shorter is ia else ia
        matched = sum(1 for x in shorter
                      if min((x - y) for y in longer) <= threshold)
        if len(ia) == len(ib):
            matched_rev = sum(1 for y in longer
                              if min((y - x) for x in shorter) <= threshold)
            matched = min(matched, matched_rev)
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
        with self._db_lock:
            row = self.db.execute(
                'SELECT thumbnail FROM files WHERE path=?', (path,)).fetchone()
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


def group_key(paths):
    """Module-level convenience for VideoOrganizer.group_key."""
    return VideoOrganizer.group_key(paths)


if __name__ == '__main__':
    print('Core module OK. Import this from the GUI.')