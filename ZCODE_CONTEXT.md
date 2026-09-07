# VidSweep — Context File for AI Code Review & Development

## Project Overview
**VidSweep** is a portable Windows video duplicate finder/organizer with a tkinter GUI.
- **Core engine**: `core.py` — scanning, fingerprinting (SHA-256 + perceptual pHash), SQLite caching, grouping
- **GUI**: `gui.py` — three tabs (Scan, Duplicates, Organize), dark/light themes, privacy settings
- **Launcher**: `VidSweep.bat` — probes for Python with deps, installs if needed
- **Published**: Aug 2026 at github.com/someguyinq123-ai/VidSweep (GPL-3 dual license)
- **Author**: someguyinq123 / someguyinq123@gmail.com

---

## Architecture & Key Design Decisions

### Deduplication Pipeline
1. **Scan**: Walk folder tree → collect video files (by extension)
2. **Exact hash**: SHA-256 (streamed, 4MB chunks) → byte-identical files group instantly
3. **Perceptual hash (pHash)**: ffmpeg extracts 4 frames (10%, 35%, 60%, 85% of duration) → 64-bit DCT hash per frame
4. **Grouping**: Union-find merges files. Two videos match if:
   - **3 of 4 frames** agree (Hamming distance ≤ threshold, default 8)
   - **Duration overlap ≥ 10%** (min/max duration ratio ≥ 0.90)
5. **SQLite cache** (`library.db`): paths, size, mtime, sha256, phash (JSON), duration, width, height, fps, vcodec, thumbnail BLOB, session_id

### Scan Sessions / Partial Scans (chunked scanning of huge libraries)
- Every `scan()` runs in a **session** (table `sessions`: id, roots, recursive, status `active`/`complete`); each `files` row is stamped with `session_id`.
- **Stop anytime**: per-file DB writes commit immediately, so a stopped scan leaves a usable partial cache and the session stays `active`.
- **SHA checkpointing**: Stage A persists digests in batches (`INSERT OR IGNORE` — never clobbers an existing row). A resume never re-hashes checkpointed files; cached digests are also seeded for rows with sha but NULL phash (perceptual still pending).
- **Resume**: `scan(roots, recursive, session_id=X)` continues the same session — the batch stays **cumulative** across resumes (next-day chunk 2 adds to chunk 1). A new `scan()` (no id) supersedes old active sessions and re-batches everything it sees (skipped files are re-stamped to the new session).
- **Scoped duplicate check**: `find_duplicates(session_id=X)` matches only that batch; `session_id=None` (default) matches the entire library. Rows are deduped by case-insensitive path so a file can't pair with itself.
- `get_last_session()` → `{id, roots, recursive, status, done, active, paused, total}` — powers the Scan tab's "Resume last scan" button and the Duplicates tab's scope default.
- **Pause survives restart**: `pause()`/`resume()` persist the `paused` flag to the `sessions` table; `scan()` also stores the enumerated `total`. On launch `_offer_resume_on_startup()` (gui.py) finds an active session with progress and offers one-click resume; declining keeps the Resume button ("Resume paused scan (N of M videos)").
- **`scan_state.json` sidecar** (next to library.db, written by `core._write_scan_state` at scan start/total/pause/resume/stop/complete): mirrors the session state so the unfinished scan is still offered even if library.db gets recreated after a corruption event (the L:-drive bridge has done this). Deleted alongside the DB by `wipe_db_on_exit` and Reset library.
- **Mid-scan corruption recovery**: `core.scan()` wraps `_scan_impl`; on `sqlite3.DatabaseError` matching `_is_db_corruption` (malformed / not a database / disk I/O): (1) `_rebuild_db()` in place + restart once; (2) if it corrupts again OR the corrupt file is undeletable (another instance holds it) → `_migrate_db()` relocates the cache to `%LOCALAPPDATA%\VidSweep\library.db`, writes `db_location.txt` (read by `__init__` on every launch), restarts again, and `stats['cache_migrated']` makes the GUI log the move; (3) only a third corruption raises a RuntimeError naming chkdsk. `__init__` recovery uses the same predicate and falls back to relocation.
- GUI: Duplicates tab has a **Match: Current scan batch / Entire library** selector; a cancelled scan auto-selects batch scope and auto-loads partial results.

### Cache Hygiene, Audit Trail, Fast Matching, Keyboard Triage
- **Ghost-row pruning**: a completed recursive scan deletes cache rows whose files no longer exist under the scanned roots (`core.py:_prune_missing`, `stats['pruned']`). Offline/removed roots are never pruned.
- **Action manifest**: EXECUTE and Organize-Apply write `logs/actions_YYYYMMDD-HHMMSS.csv` (action, path, size, destination, status) BEFORE touching files (`planned`), rewritten with per-file outcomes after — a crash mid-execute still leaves the record.
- **Fast match**: `find_duplicates(fast_match=True)` (default, Duplicates-tab checkbox persisted in settings.json) generates candidate pairs via 8×8-bit banding of frame hashes — ~7x faster at 800 records, gap widens with scale. Probabilistic (rare matches possible to miss); `fast_match=False` = exact exhaustive enumeration.
- **Keyboard triage** (Duplicates tab only, never while typing in a text field): Up/Down move the ▶ file cursor, K/D/M decide + auto-advance, Enter opens, Left/Right switch groups.
- **CRITICAL threading rule**: never read Tk variables (`fast_var.get()` etc.) from worker threads — Tcl calls off the main thread can kill the interpreter with a silent `exit(1)`. Read them in `load_groups` before spawning the worker.

### Scan Pipeline & DB Reliability
- **Pipelined scan**: hashing and perceptual work run concurrently — each SHA digest streams its file into the perceptual pool immediately (`progress` phase `'working'`, done over 2×file count). No Stage A/B barrier.
- **No WAL**: `L:`'s drive bridge fails WAL shared-memory ops (`disk I/O error` at first write). Default rollback journal only.
- **Batched commits**: `process_one` does not commit per file; the scan loop commits every ~16 perceptual completions, at checkpoint flushes, at the end, and in the cancel `finally` (via `_safe_commit`, which retries transient `disk I/O` blips from the drive bridge).
- **`_db_lock` is an RLock** and covers every DB access (reads included). Nesting is legal (e.g. `render_pil` → `get_thumbnail`) — do not swap it for a plain `Lock`.
- **`_similarity` is symmetric**: with equal frame counts it requires the match criterion in BOTH directions. It used to depend on argument order (DB row order), which made grouping vary between scans of identical data.

### Keep-Best Ranking (CRITICAL — NEVER CHANGE)
**Bitrate-first**: `(size / duration)` is the primary signal. A sharp 720p at 6 Mbps **must beat** a blurry 1080p at 1 Mbps.
- Tie-breaker 1: Resolution (width × height)
- Tie-breaker 2: Modern codec bonus (h264, hevc, av1, vp9)
- Code: `core.py:_quality_key()` (lines 672-683)

### Matching Thresholds
| Slider Value | Label | Behavior |
|---|---|---|
| 4 | Very strict | Nearly pixel-identical only; zero false matches |
| 6 | Strict | Most re-encodes; very few false matches |
| **8** | **Default** | **Balanced; catches re-encodes + resolution drops** |
| 10 | Loose | Heavily compressed/resized; more false matches |
| 12+ | Very loose | Anything vaguely similar; high false-match rate |

---

## Environment & Dependencies

### Python Interpreter (CRITICAL)
**Must use**: `C:\Users\WY\AppData\Local\Python\bin\python.exe` — has Pillow, imagehash, send2trash
- Plain `python`/`pythonw` in PATH may resolve to dep-less interpreter (e.g., C:\Python314)
- Launchers must probe, not trust PATH
- Test deps: `python -c "import PIL, imagehash, tkinter, send2trash"`

### Required Packages
```
pip install pillow imagehash send2trash numpy
```
- NumPy optional but strongly recommended (SIMD popcount → several × faster matching)
- Falls back to pure Python if NumPy absent

### FFmpeg/FFprobe
Auto-detected from:
1. Session override (`core.set_ffmpeg_override`)
2. Saved `ffmpeg_path.txt` from previous session
3. Known default: `C:\ffmpeg\ffmpeg-8.1.1-essentials_build\bin\ffmpeg.exe`
4. Common install locations (WinGet, Program Files)
5. PATH
- `ffprobe.exe` expected beside `ffmpeg.exe`

---

## Core Module (`core.py`) — Key Classes & Functions

### `VideoOrganizer(db_path=None)`
Main engine class. Thread-safe with `_db_lock`, `_procs_lock`.

#### Public Methods
- `scan(roots, recursive=True, progress=None, session_id=None)` → stats dict
  - Phases: 'scan' (enumerate), 'hashing' (SHA-256 parallel, checkpointed), 'perceptual' (metadata + frames + pHash parallel)
  - `session_id=None` starts a new session; pass an active id to resume it
  - Progress callback: `progress(phase, done, total, current_path)`
  - Returns: `{'scanned', 'processed', 'hashed_exact', 'hashed_perceptual', 'skipped_cached', 'reused_identical', 'errors', 'elapsed', 'error_details', 'session_id'}`
- `find_duplicates(threshold=8, min_duration_overlap=0.90, use_perceptual=True, progress=None, session_id=None)` → list of groups
  - `session_id=None` = entire library; an id = only that scan batch
  - Each group: list of record dicts sorted by `_quality_key` (best first)
  - Groups sorted by total wasted space (descending)
- `get_last_session()` → `{'id', 'roots', 'recursive', 'status', 'done', 'active'}` or None
- `get_thumbnail(path)` → JPEG bytes or None
- `suggest_folders(paths)` → list of `(path, category)` for Organize tab
- `close()` — close DB connection

#### Internal Methods
- `_sha256(path, size)` — streaming hash with cancel/pause checks
- `_ffprobe_info(path)` — duration, width, height, fps, vcodec
- `_extract_frames(path, duration, tmpdir)` — 4 frames in ONE ffmpeg process (2 filter chains fallback)
- `_make_thumb_and_phashes(path, duration)` → `(thumb_bytes, [phash_str...])`
- `_similarity(a, b, threshold)` — 3 paths: NumPy popcount (fast), NumPy vectorized, pure Python (imagehash)
- `_quality_key(r)` — bitrate, resolution, codec

### `Cancelled` Exception
Raised on user cancel; propagates through all threads.

---

## GUI Module (`gui.py`) — Key Components

### `App(tk.Tk)` — Main Window
**Tabs**: Scan, Duplicates, Organize

#### State
- `org` — `core.VideoOrganizer()`
- `thumbs` — `ThumbnailCache(org)` (LRU, max 200, 160×90)
- `groups` — loaded duplicate groups
- `decisions` — `path → StringVar('keep'|'delete'|'move')`
- `privacy` — dict from `privacy.json` (all OFF by default)
- `theme_name` — 'light' or 'dark' (from `settings.json`)

#### Privacy Options (ALL OPT-IN, DEFAULT OFF)
| Key | Description |
|---|---|
| `wipe_db_on_exit` | Delete library.db on close (full rescan next launch) |
| `secure_delete` | Overwrite bytes before removal (bypasses Recycle Bin) |
| `open_no_history` | Launch player with history disabled (MPC-HC, VLC) |

#### Scan Tab
- Folder list (multi-select, Del/Backspace to remove)
- Perceptual sensitivity slider (4-14) + help dialog
- ffmpeg status + locate button
- Start/Cancel/Pause buttons + progress bar + log

#### Duplicates Tab
- **Left**: Treeview of groups (file count, redundant MB)
- **Right**: Thumbnail grid per group with Keep/Delete/Move radios + Open button
- **Action bar**: Execute (Recycle Bin / Quarantine / Permanent), "Mark ALL groups: keep best, delete rest"
- Marked count label updates live

#### Organize Tab
- Source folder → "Suggest categories" → name-based clustering (strips noise tokens: 1080p, x264, bluray, etc.)
- Preview moves (shows conflicts) → Apply moves (never overwrites, adds `_N` suffix)

### `_SensitivityHelpDialog` (lines 101-241)
Professional structured help with color-coded level table and rule-of-thumb callout.

---

## Test Suite (`tests/`)
Run from project root with the correct Python:
```bash
C:\Users\WY\AppData\Local\Python\bin\python.exe -m pytest tests/
```
| Test File | Purpose |
|---|---|
| `test_core.py` | CI smoke test: generates videos, verifies scan + exact + perceptual grouping |
| `test_markall.py` | "Mark all" covers ALL groups |
| `test_ux.py` | UX regression tests |
| `test_cancel_fast.py` | Cancel kills in-flight ffmpeg |
| `test_pause_core.py` | Pause/resume works in workers |
| `test_help_dialog.py` | Help dialog renders |
| `test_folders.py` | Folder add/remove persistence |
| `test_reuse.py` | Identical-file reuse (SHA dedup) |
| `test_partial_scan.py` | Partial scan: stop mid-way, batch-scoped dupes, resume with zero re-hashing, session supersede |
| `test_resume_restart.py` | Two-process simulation: pause → close → "reboot" (fresh interpreter) → resume with zero re-hashing |
| `test_resume_prompt.py` | Startup resume prompt: paused session offered, declines don't launch, accept resumes same session id |
| `test_perf.py` | Performance benchmarks |

---

## Common Tasks for z code

### 1. Review a PR / Change
- Check `core.py:_quality_key` — bitrate-first ranking must remain
- Verify privacy options stay opt-in default-OFF
- Ensure cancel/pause propagate to worker threads (`_wait_if_paused`, `_check_cancel`)
- Check temp file cleanup (`shutil.rmtree` in finally blocks)

### 2. Add a Feature
- **New dedup signal**: Add to `_quality_key` tuple (bitrate, res, codec, new_signal)
- **New privacy option**: Add to `PRIVACY_DEFAULTS`, `_load_privacy`, `_save_privacy`, settings dialog
- **New organize category**: Modify `suggest_folders` noise set / token logic

### 3. Debug a Bug
- **Scan hangs**: Check `_wait_if_paused` in workers, `CREATE_NO_WINDOW` flag
- **False matches**: Verify duration overlap check (`min_duration_overlap`), Hamming threshold
- **Missing thumbnails**: Check `_extract_frames` filter chain fallback, tmpdir cleanup
- **DB locked**: Ensure `close()` called before wipe, `_db_lock` used for all writes

### 4. Performance Optimization
- NumPy popcount path in `_similarity` (requires NumPy ≥ 2.0 with `bitwise_count`)
- Frame extraction: single ffmpeg process for all 4 timestamps (already done)
- Exact hash reuse: identical SHA → reuse perceptual data (already done in Stage B)

---

## File Manifest
```
L:\Video organizer custom\OX ALpha\
├── core.py              # 724 lines — dedup engine
├── gui.py               # 1282 lines — tkinter GUI
├── VidSweep.bat         # 26 lines — launcher
├── settings.json        # folders[], theme
├── privacy.json         # wipe_db_on_exit, secure_delete, open_no_history
├── library.db           # SQLite cache (299 KB)
├── ffmpeg_path.txt      # user-selected ffmpeg (optional)
├── README.md            # docs
├── LICENSE              # GPL-3
├── .hermes.md           # project rules for Hermes
├── tests/               # 11 test files
├── site/                # GitHub Pages site
└── docs/                # screenshots
```

---

## Git Workflow
- Branch: `main` → `origin/main`
- Identity: `someguyinq123` / `someguyinq123@gmail.com`
- Recent: CI bump to Node 24 actions, README updates, marketing screenshots

---

## Key Invariants to Preserve
1. **Bitrate-first quality ranking** — never switch to resolution-first
2. **Privacy opt-in** — never default any privacy feature to ON
3. **Cancel kills ffmpeg** — don't wait out long decodes
4. **Pause respected in workers** — not just UI freeze
5. **Temp files cleaned** — `finally: shutil.rmtree`
6. **Mark All = ALL groups** — not just visible/selected
7. **Safe moves** — never overwrite, always `_N` suffix
8. **Incremental scans** — skip unchanged files with valid sha+phash
9. **Session integrity** — resumes keep the same `session_id` (cumulative batch); `INSERT OR IGNORE` SHA checkpoints must never clobber a full row; a completed scan marks its session `complete`

---

## Useful Commands
```bash
# Run app (from project root)
C:\Users\WY\AppData\Local\Python\bin\pythonw.exe gui.py

# Run tests
C:\Users\WY\AppData\Local\Python\bin\python.exe -m pytest tests/ -v

# Run core smoke test directly
C:\Users\WY\AppData\Local\Python\bin\python.exe tests/test_core.py

# Check deps
C:\Users\WY\AppData\Local\Python\bin\python.exe -c "import PIL, imagehash, send2trash, numpy; print('OK')"
```