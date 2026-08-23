# Video Organizer

A portable, privacy-first Windows app for organizing large video collections:
finds duplicate videos (even re-encoded ones), lets you review them with
thumbnails, and categorizes what remains by name.

Built for big libraries — tested on the design target of 30,000+ files.

## Features

- **Two-level duplicate detection**
  - *Exact*: SHA-256 content hashing catches byte-identical copies instantly.
  - *Perceptual*: samples 4 frames per video and compares DCT perceptual
    hashes — catches the same video re-encoded at a different resolution,
    bitrate, or container (mp4 → mkv → avi).
- **Thumbnail review grid** — every duplicate group shows first-frame
  screenshots, resolution, size, codec, and duration side by side.
- **Keep-best auto-suggestion** — the highest-quality copy is pre-marked
  Keep; one click marks the rest for deletion across all groups.
- **Your choice of removal**: Recycle Bin (default), quarantine folder, or
  permanent delete — with an optional secure-delete mode that overwrites
  bytes first so nothing is recoverable.
- **Name-based organizer** — clusters remaining files into category folders
  from their filenames, with a full preview before anything moves.
- **Incremental scans** — everything is cached in a local SQLite database;
  rescans only touch new or changed files. Pause/Resume supported.
- **Privacy options (all off by default)**
  - Wipe the fingerprint database on exit
  - Secure delete (overwrite before removal)
  - Open videos without leaving player-history / Recent Items traces
- **Portable** — no installer, no registry, no telemetry, fully offline.
  Delete the folder and it's gone.

## Requirements

- Windows 10/11
- Python 3.11+ with: `pip install pillow imagehash send2trash`
- [ffmpeg/ffprobe](https://www.gyan.dev/ffmpeg/builds/) on disk
  (default path `C:\ffmpeg\...` is auto-detected; see `core.py` to change it)
- A display — tkinter GUI, no extra UI toolkit needed

## Usage

1. Double-click **`Video Organizer.bat`** (it locates a Python that has the
   dependencies, and offers one-time setup if none does).
2. **Scan** tab: add folders → Start scan. First scan decodes 4 frames per
   video, so a 30k-file library takes a few hours; after that, rescans are
   nearly instant for unchanged files.
3. **Duplicates** tab: review groups with thumbnails, mark Keep/Delete/Move
   (marking accumulates across groups — the EXECUTE dialog lists every file
   so you always see exactly what will happen), then Execute.
4. **Organize** tab (optional): preview and apply name-based category folders.

## How perceptual matching works

Each video contributes 4 frame fingerprints (64-bit pHash each). Two videos
group when at least 3 frames match within a bit-difference threshold AND
durations are within 10%. The sensitivity slider (default 8) sets the
threshold — see `How sensitivity works.txt` for a level-by-level guide.

## License

MIT — see [LICENSE](LICENSE).
