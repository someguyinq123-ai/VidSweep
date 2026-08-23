# VidSweep

![VidSweep logo](docs/logo.png)

A portable, privacy-first desktop app for organizing large video collections:
finds duplicate videos (even re-encoded ones), lets you review them side by
side with thumbnails, and categorizes what remains by name.

Built for big libraries — designed and tested around 30,000+ files.

## Why VidSweep

- **Finds re-encodes, not just copies.** Byte-identical files are the easy
  case. VidSweep also matches the same video when it's been re-encoded at a
  different resolution, bitrate, or container (mp4 → mkv → avi) using
  perceptual frame hashing.
- **You decide, quickly.** Every duplicate group is shown as a thumbnail grid
  with resolution, size, codec and duration. The best copy is pre-marked
  Keep; one click marks the rest, another click executes.
- **Nothing leaves your machine.** No telemetry, no network calls, no
  installer, no registry. Fully offline. Optional privacy hardening: secure
  delete (overwrite before removal), wipe the database on exit, open videos
  without leaving player-history traces.
- **Incremental scans.** All fingerprints live in a local SQLite cache, so
  the first scan is the slow one; every rescan only touches new or changed
  files. Pause and resume supported.

## Features

- Two-level duplicate detection: exact (SHA-256) + perceptual (4-frame pHash
  with duration cross-check)
- Thumbnail review grid with per-file Keep / Delete / Move
- "Keep best, delete rest" per group or across all groups
- Removal via Recycle Bin (default), quarantine folder, permanent delete, or
  optional secure delete
- Name-based category organizer with full move preview
- Tunable match sensitivity with a plain-language guide
- Cross-platform core engine (see Status below)

## Requirements

- Python 3.11+ — `pip install pillow imagehash send2trash`
- ffmpeg/ffprobe available (auto-detected from PATH or common install
  locations; a "Locate ffmpeg…" button is built in)
- A display — the GUI is tkinter, bundled with Python

## Usage

1. Windows: double-click **`Video Organizer.bat`** (auto-locates a suitable
   Python and offers one-time dependency setup). Other systems:
   `python3 gui.py`
2. **Scan** tab: add folders → Start scan. First scan decodes 4 frames per
   video (a 30k library takes a few hours); rescans are nearly instant.
3. **Duplicates** tab: review, mark, Execute — the confirmation dialog lists
   every file so nothing is ambiguous.
4. **Organize** tab (optional): preview and apply name-based category folders.

## Status

| Platform | Core engine | GUI |
|---|---|---|
| Windows | ✅ tested | ✅ tested |
| Linux / macOS | ✅ CI-verified | expected to work (tkinter); untested by hand |

CI runs the core-engine smoke test (scan → exact + perceptual grouping →
cache) on Ubuntu, Windows and macOS across Python 3.11/3.12 on every push.

## License

MIT — see [LICENSE](LICENSE).
