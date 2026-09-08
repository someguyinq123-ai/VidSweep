"""F-001 contract tests: persistent not-duplicates dismissal.

Covers the five TEST_CONTRACT.md items using exact-duplicate groups only
(use_perceptual=False), so no ffmpeg/imagehash dependency: groups come
purely from the sha256 map, which is the code path the dismissal filter
wraps.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core


@pytest.fixture()
def org(tmp_path):
    o = core.VideoOrganizer(db_path=str(tmp_path / "lib.db"))
    yield o
    o.close()


def _add_file(o, path, sha, size=100):
    with o._db_lock:
        o.db.execute(
            'INSERT INTO files(path,size,mtime,sha256,phash,duration,'
            'width,height,fps,vcodec,session_id) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (os.path.abspath(path), size, 0.0, sha, '[]', 10.0,
             640, 360, 30.0, 'h264', None))
        o._safe_commit()


def _group_paths(groups):
    return sorted(sorted(r['path'] for r in g) for g in groups)


def _make_groups(o, tmp_path):
    """Two exact-duplicate groups: A/B (sha1) and C/D (sha2)."""
    a = str(tmp_path / "a.mp4")
    b = str(tmp_path / "b.mp4")
    c = str(tmp_path / "c.mp4")
    d = str(tmp_path / "d.mp4")
    _add_file(o, a, "sha1")
    _add_file(o, b, "sha1")
    _add_file(o, c, "sha2")
    _add_file(o, d, "sha2")
    return a, b, c, d


def test_group_key_is_order_independent(tmp_path):
    a, b = str(tmp_path / "x" / "A file.mp4"), str(tmp_path / "y" / "B file.mp4")
    k1 = core.VideoOrganizer.group_key([a, b])
    k2 = core.VideoOrganizer.group_key([b, a])
    assert k1 == k2
    # case-insensitive on every OS (normcase + casefold)
    assert k1 == core.VideoOrganizer.group_key([a.upper(), b])
    # different member sets produce different keys
    assert k1 != core.VideoOrganizer.group_key([a])


def test_dismiss_persists_across_reopen(tmp_path, org):
    a, b, c, d = _make_groups(org, tmp_path)
    groups = org.find_duplicates(use_perceptual=False)
    assert _group_paths(groups) == [sorted([a, b]), sorted([c, d])]
    key = org.dismiss_group(paths=[a, b])
    org.close()

    org2 = core.VideoOrganizer(db_path=str(tmp_path / "lib.db"))
    try:
        groups = org2.find_duplicates(use_perceptual=False)
        # dismissed group is gone after full reopen; the other remains
        assert _group_paths(groups) == [sorted([c, d])]
        stored = org2.list_dismissed()
        assert len(stored) == 1 and stored[0]['group_key'] == key
    finally:
        org2.close()


def test_undismiss_restores_group(tmp_path, org):
    a, b, c, d = _make_groups(org, tmp_path)
    key = org.dismiss_group(paths=[a, b])
    assert _group_paths(org.find_duplicates(use_perceptual=False)) == [sorted([c, d])]
    assert org.undismiss_group(key) is True
    groups = org.find_duplicates(use_perceptual=False)
    assert _group_paths(groups) == [sorted([a, b]), sorted([c, d])]


def test_non_dismissed_groups_unaffected(tmp_path, org):
    a, b, c, d = _make_groups(org, tmp_path)
    org.dismiss_group(paths=[a, b])  # dismiss only the first group
    groups = org.find_duplicates(use_perceptual=False)
    assert _group_paths(groups) == [sorted([c, d])]


def test_matching_unchanged_before_dismissal(tmp_path, org):
    a, b, c, d = _make_groups(org, tmp_path)
    # no dismissals exist: grouping must be exactly the sha-map result
    assert org.list_dismissed() == []
    groups = org.find_duplicates(use_perceptual=False)
    assert _group_paths(groups) == [sorted([a, b]), sorted([c, d])]


def test_reset_dismissed_clears_everything(tmp_path, org):
    a, b, c, d = _make_groups(org, tmp_path)
    org.dismiss_group(paths=[a, b])
    org.dismiss_group(paths=[c, d])
    assert org.find_duplicates(use_perceptual=False) == []
    assert org.clear_dismissed() == 2
    groups = org.find_duplicates(use_perceptual=False)
    assert _group_paths(groups) == [sorted([a, b]), sorted([c, d])]
