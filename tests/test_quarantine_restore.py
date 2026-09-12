"""Behavioral tests for VideoOrganizer.restore_quarantine (feature F-EXP-3).

Pins the manifest-authoritative contract:

  * candidates come only from `actions_*.csv` rows resolved by COLUMN NAME
    (`action == 'move'`, `status == 'moved'`, non-empty `path` and
    `destination`) — planned/failed/non-move rows, blank paths, non-manifest
    CSVs and unreadable CSVs contribute nothing and are never fatal;
  * the quarantine folder is never scanned (unlisted files stay put) and the
    database is never consulted (`VideoOrganizer.__new__(VideoOrganizer)` is
    enough to call the method);
  * files return to their recorded original path, with missing parent
    directories re-created, and never overwriting: an occupied original takes
    the house numbered suffix (`v.mp4` -> `v_1.mp4`) and the occupant is left
    byte-identical;
  * duplicate destination records resolve LAST-wins (manifest filename
    ascending, then row order within the file);
  * a listed source missing from disk reports `skipped_missing_source` and
    changes nothing;
  * a failed transfer reports `skipped_transfer_failed` and leaves the
    quarantined copy fully intact;
  * the return value has exactly the keys `restored`, `renamed`, `skipped`
    and `results`, every result has exactly `source`, `original`, `dest` and
    `outcome`, `results` is sorted by `source`, the counts partition the
    results, a missing logs directory raises ValueError, and no outcome
    string outside the four permitted ones is ever produced.

Run directly with the pinned interpreter:
    python tests/test_quarantine_restore.py
or under pytest:
    pytest tests/test_quarantine_restore.py
"""
import csv
import os
import shutil
import sys
import tempfile
from contextlib import contextmanager

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core


MANIFEST_COLUMNS = ('timestamp', 'action', 'path', 'size', 'destination',
                    'status')
OUTCOMES = ('restored', 'restored_renamed', 'skipped_missing_source',
            'skipped_transfer_failed')


@contextmanager
def workdir():
    d = tempfile.mkdtemp(prefix='vidsweep_restore_')
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def make_tree(d):
    """Standard layout: <d>/logs, <d>/quarantine and <d>/videos."""
    logs = os.path.join(d, 'logs')
    quarantine = os.path.join(d, 'quarantine')
    videos = os.path.join(d, 'videos')
    for path in (logs, quarantine, videos):
        os.makedirs(path)
    return logs, quarantine, videos


def write_bytes(path, data):
    with open(path, 'wb') as fh:
        fh.write(data)
    return path


def read_bytes(path):
    with open(path, 'rb') as fh:
        return fh.read()


def manifest_row(path, destination, action='move', status='moved'):
    return {'timestamp': '2024-05-01 10:00:00', 'action': action,
            'path': path, 'size': '123', 'destination': destination,
            'status': status}


def write_manifest(logs_dir, name, rows, columns=MANIFEST_COLUMNS):
    """Write one action manifest; rows are dicts keyed by column name."""
    full = os.path.join(logs_dir, name)
    with open(full, 'w', newline='', encoding='utf-8') as fh:
        writer = csv.writer(fh)
        writer.writerow(list(columns))
        for row in rows:
            writer.writerow([row.get(col, '') for col in columns])
    return full


def bare_organizer():
    """Restore needs no database connection and no Tk object at all."""
    org = core.VideoOrganizer.__new__(core.VideoOrganizer)
    assert not hasattr(org, 'db'), 'restore must run without a database'
    return org


def restore(logs_dir):
    return bare_organizer().restore_quarantine(logs_dir)


def one_result(source, original, dest, outcome):
    return {'source': source, 'original': original, 'dest': dest,
            'outcome': outcome}


# --------------------------------------------------------------- basic restore
def test_one_moved_row_restores_the_file_to_its_original_path():
    with workdir() as d:
        logs, quarantine, videos = make_tree(d)
        src = write_bytes(os.path.join(quarantine, 'v.mp4'), b'video-bytes')
        original = os.path.join(videos, 'v.mp4')
        write_manifest(logs, 'actions_20240501-100000.csv',
                       [manifest_row(original, src)])

        report = restore(logs)

        assert report == {'restored': 1, 'renamed': 0, 'skipped': 0,
                          'results': [one_result(src, original, original,
                                                 'restored')]}
        assert read_bytes(original) == b'video-bytes'
        assert not os.path.exists(src)
        assert os.listdir(quarantine) == []


def test_empty_logs_directory_reports_nothing():
    with workdir() as d:
        logs, _quarantine, _videos = make_tree(d)

        report = restore(logs)

        assert report == {'restored': 0, 'renamed': 0, 'skipped': 0,
                          'results': []}


# ------------------------------------------------------ manifest row selection
def test_only_moved_move_rows_become_candidates():
    with workdir() as d:
        logs, quarantine, videos = make_tree(d)
        rows = []
        kept = []
        for i, (action, status) in enumerate((
                ('move', 'planned'),
                ('move', 'failed: permission denied'),
                ('delete', 'moved'),
                ('recycle', 'moved'),
                ('move', 'Moved'),
                ('move', ''))):
            src = write_bytes(os.path.join(quarantine, f'q{i}.mp4'), b'x')
            kept.append(src)
            rows.append(manifest_row(os.path.join(videos, f'v{i}.mp4'), src,
                                     action=action, status=status))
        write_manifest(logs, 'actions_20240501-100000.csv', rows)

        report = restore(logs)

        assert report == {'restored': 0, 'renamed': 0, 'skipped': 0,
                          'results': []}
        for src in kept:
            assert os.path.exists(src)
        assert os.listdir(videos) == []


def test_blank_path_and_blank_destination_rows_are_ignored():
    with workdir() as d:
        logs, quarantine, videos = make_tree(d)
        src = write_bytes(os.path.join(quarantine, 'q.mp4'), b'x')
        write_manifest(logs, 'actions_20240501-100000.csv', [
            manifest_row('', src),
            manifest_row('   ', src),
            manifest_row(os.path.join(videos, 'v.mp4'), ''),
            manifest_row(os.path.join(videos, 'v.mp4'), '   '),
        ])

        report = restore(logs)

        assert report['results'] == []
        assert report['skipped'] == 0
        assert os.path.exists(src)
        assert os.listdir(videos) == []


def test_manifest_columns_are_resolved_by_name_not_position():
    with workdir() as d:
        logs, quarantine, videos = make_tree(d)
        src = write_bytes(os.path.join(quarantine, 'v.mp4'), b'bytes')
        original = os.path.join(videos, 'v.mp4')
        write_manifest(logs, 'actions_20240501-100000.csv',
                       [manifest_row(original, src)],
                       columns=('status', 'destination', 'timestamp',
                                'path', 'action', 'size'))

        report = restore(logs)

        assert report['restored'] == 1
        assert read_bytes(original) == b'bytes'
        assert not os.path.exists(src)


def test_unrelated_and_unreadable_csvs_are_ignored_without_failing():
    with workdir() as d:
        logs, quarantine, videos = make_tree(d)
        src = write_bytes(os.path.join(quarantine, 'v.mp4'), b'bytes')
        original = os.path.join(videos, 'v.mp4')
        # a real manifest — it must still be processed
        write_manifest(logs, 'actions_20240501-100000.csv',
                       [manifest_row(original, src)])
        # an actions_*.csv that does not carry the manifest columns
        with open(os.path.join(logs, 'actions_20240502-100000.csv'), 'w',
                  newline='', encoding='utf-8') as fh:
            fh.write('group_id,path,note\n1,whatever,hello\n')
        # manifest-shaped but NOT named actions_*.csv
        write_manifest(logs, 'notes.csv',
                       [manifest_row(os.path.join(videos, 'x.mp4'),
                                     os.path.join(quarantine, 'x.mp4'))])
        # unreadable junk
        write_bytes(os.path.join(logs, 'actions_20240503-100000.csv'),
                    b'\x00\xff\xfe garbage\nnot a manifest,,,\n')

        report = restore(logs)

        assert report['restored'] == 1
        assert report['skipped'] == 0
        assert os.path.exists(original)
        assert not os.path.exists(os.path.join(videos, 'x.mp4'))


def test_truncated_csv_rows_are_ignored_and_never_fatal():
    with workdir() as d:
        logs, quarantine, videos = make_tree(d)
        src = write_bytes(os.path.join(quarantine, 'v.mp4'), b'bytes')
        with open(os.path.join(logs, 'actions_20240501-100000.csv'), 'w',
                  newline='', encoding='utf-8') as fh:
            fh.write('timestamp,action,path,size,destination,status\n')
            # unterminated quote: the row cannot be paired with its columns
            fh.write('2024-05-01 10:00:00,move,"C:\\vids\\v.mp4,123,'
                     f'{src},moved\n')

        report = restore(logs)

        assert report == {'restored': 0, 'renamed': 0, 'skipped': 0,
                          'results': []}
        assert read_bytes(src) == b'bytes'
        assert os.listdir(videos) == []


def test_unlisted_quarantine_files_are_never_restored():
    with workdir() as d:
        logs, quarantine, videos = make_tree(d)
        orphan = write_bytes(os.path.join(quarantine, 'orphan.mp4'),
                             b'not-from-vidsweep')
        write_manifest(logs, 'actions_20240501-100000.csv', [])

        report = restore(logs)

        assert report == {'restored': 0, 'renamed': 0, 'skipped': 0,
                          'results': []}
        assert read_bytes(orphan) == b'not-from-vidsweep'
        assert os.listdir(videos) == []


# ------------------------------------------------------------- never overwrite
def test_occupied_original_takes_the_numbered_suffix():
    with workdir() as d:
        logs, quarantine, videos = make_tree(d)
        original = write_bytes(os.path.join(videos, 'v.mp4'),
                               b'occupant-bytes')
        src = write_bytes(os.path.join(quarantine, 'v.mp4'),
                          b'quarantined-bytes')
        write_manifest(logs, 'actions_20240501-100000.csv',
                       [manifest_row(original, src)])

        report = restore(logs)

        dest = os.path.join(videos, 'v_1.mp4')
        assert report == {'restored': 0, 'renamed': 1, 'skipped': 0,
                          'results': [one_result(src, original, dest,
                                                 'restored_renamed')]}
        assert read_bytes(original) == b'occupant-bytes'
        assert read_bytes(dest) == b'quarantined-bytes'
        assert not os.path.exists(src)


def test_suffix_keeps_counting_until_the_first_free_name():
    with workdir() as d:
        logs, quarantine, videos = make_tree(d)
        original = write_bytes(os.path.join(videos, 'v.mp4'), b'occupant')
        write_bytes(os.path.join(videos, 'v_1.mp4'), b'occupant-1')
        write_bytes(os.path.join(videos, 'v_2.mp4'), b'occupant-2')
        src = write_bytes(os.path.join(quarantine, 'v.mp4'), b'incoming')
        write_manifest(logs, 'actions_20240501-100000.csv',
                       [manifest_row(original, src)])

        report = restore(logs)

        dest = os.path.join(videos, 'v_3.mp4')
        assert report['results'] == [one_result(src, original, dest,
                                                'restored_renamed')]
        assert read_bytes(dest) == b'incoming'
        assert read_bytes(original) == b'occupant'
        assert read_bytes(os.path.join(videos, 'v_1.mp4')) == b'occupant-1'
        assert read_bytes(os.path.join(videos, 'v_2.mp4')) == b'occupant-2'


def test_suffix_without_an_extension():
    with workdir() as d:
        logs, quarantine, videos = make_tree(d)
        original = write_bytes(os.path.join(videos, 'clip'), b'occupant')
        src = write_bytes(os.path.join(quarantine, 'clip'), b'incoming')
        write_manifest(logs, 'actions_20240501-100000.csv',
                       [manifest_row(original, src)])

        report = restore(logs)

        dest = os.path.join(videos, 'clip_1')
        assert report['results'] == [one_result(src, original, dest,
                                                'restored_renamed')]
        assert read_bytes(dest) == b'incoming'
        assert read_bytes(original) == b'occupant'


# --------------------------------------------------- re-create the original tree
def test_missing_parent_directories_are_recreated():
    with workdir() as d:
        logs, quarantine, videos = make_tree(d)
        src = write_bytes(os.path.join(quarantine, 'v.mp4'), b'bytes')
        original = os.path.join(videos, 'gone', 'deeper', 'v.mp4')
        write_manifest(logs, 'actions_20240501-100000.csv',
                       [manifest_row(original, src)])

        report = restore(logs)

        assert report['restored'] == 1
        assert read_bytes(original) == b'bytes'
        assert os.path.isdir(os.path.join(videos, 'gone', 'deeper'))
        assert not os.path.exists(src)


# ------------------------------------------------------------------- skipped
def test_missing_source_is_reported_and_changes_nothing():
    with workdir() as d:
        logs, quarantine, videos = make_tree(d)
        src = os.path.join(quarantine, 'v.mp4')      # never created
        original = os.path.join(videos, 'gone', 'v.mp4')
        write_manifest(logs, 'actions_20240501-100000.csv',
                       [manifest_row(original, src)])

        report = restore(logs)

        assert report == {'restored': 0, 'renamed': 0, 'skipped': 1,
                          'results': [one_result(src, original, None,
                                                 'skipped_missing_source')]}
        assert not os.path.exists(os.path.join(videos, 'gone'))
        assert os.listdir(quarantine) == []


def test_blocked_parent_is_a_transfer_failure_with_the_copy_left_intact():
    with workdir() as d:
        logs, quarantine, videos = make_tree(d)
        blocker = write_bytes(os.path.join(videos, 'blocked'), b'i am a file')
        original = os.path.join(blocker, 'v.mp4')    # parent path is a FILE
        src = write_bytes(os.path.join(quarantine, 'v.mp4'), b'bytes')
        write_manifest(logs, 'actions_20240501-100000.csv',
                       [manifest_row(original, src)])

        report = restore(logs)

        assert report == {'restored': 0, 'renamed': 0, 'skipped': 1,
                          'results': [one_result(src, original, None,
                                                 'skipped_transfer_failed')]}
        assert read_bytes(blocker) == b'i am a file'
        assert read_bytes(src) == b'bytes'
        assert os.listdir(videos) == ['blocked']


# ------------------------------------------------------------ LAST-wins dedup
def test_duplicate_destination_resolves_last_wins_across_manifests():
    with workdir() as d:
        logs, quarantine, videos = make_tree(d)
        src = write_bytes(os.path.join(quarantine, 'v.mp4'), b'bytes')
        older = os.path.join(videos, 'old', 'v.mp4')
        newer = os.path.join(videos, 'new', 'v.mp4')
        write_manifest(logs, 'actions_20240501-100000.csv',
                       [manifest_row(older, src)])
        write_manifest(logs, 'actions_20240502-100000.csv',
                       [manifest_row(newer, src)])

        report = restore(logs)

        assert report == {'restored': 1, 'renamed': 0, 'skipped': 0,
                          'results': [one_result(src, newer, newer,
                                                 'restored')]}
        assert os.path.exists(newer)
        assert not os.path.exists(older)


def test_duplicate_destination_resolves_last_wins_within_one_manifest():
    with workdir() as d:
        logs, quarantine, videos = make_tree(d)
        src = write_bytes(os.path.join(quarantine, 'v.mp4'), b'bytes')
        first = os.path.join(videos, 'first.mp4')
        second = os.path.join(videos, 'second.mp4')
        write_manifest(logs, 'actions_20240501-100000.csv',
                       [manifest_row(first, src), manifest_row(second, src)])

        report = restore(logs)

        assert report['results'] == [one_result(src, second, second,
                                                'restored')]
        assert os.path.exists(second)
        assert not os.path.exists(first)


# ---------------------------------------------------------------- report shape
def test_results_are_sorted_by_source_and_counts_partition():
    with workdir() as d:
        logs, quarantine, videos = make_tree(d)
        a = write_bytes(os.path.join(quarantine, 'aaa.mp4'), b'a')
        z = write_bytes(os.path.join(quarantine, 'zzz.mp4'), b'z')
        oa = os.path.join(videos, 'a.mp4')
        oz = os.path.join(videos, 'z.mp4')
        # manifest order is deliberately the reverse of source order
        write_manifest(logs, 'actions_20240501-100000.csv',
                       [manifest_row(oz, z), manifest_row(oa, a)])

        report = restore(logs)

        assert set(report) == {'restored', 'renamed', 'skipped', 'results'}
        assert [r['source'] for r in report['results']] == [a, z]
        assert (report['restored'] + report['renamed'] + report['skipped']
                == len(report['results']) == 2)
        for entry in report['results']:
            assert set(entry) == {'source', 'original', 'dest', 'outcome'}
            assert entry['outcome'] in OUTCOMES


def test_all_four_outcomes_can_appear_together():
    with workdir() as d:
        logs, quarantine, videos = make_tree(d)
        # restored: original name free
        free_orig = os.path.join(videos, 'free.mp4')
        free_src = write_bytes(os.path.join(quarantine, 'free.mp4'), b'free')
        # restored_renamed: original name occupied
        renamed_orig = write_bytes(os.path.join(videos, 'taken.mp4'), b'occ')
        renamed_src = write_bytes(os.path.join(quarantine, 'taken.mp4'),
                                  b'new')
        # skipped_missing_source: the quarantined file is gone
        missing_src = os.path.join(quarantine, 'gone.mp4')
        missing_orig = os.path.join(videos, 'gone.mp4')
        # skipped_transfer_failed: the parent path is a file
        blocker = write_bytes(os.path.join(videos, 'blocked'), b'file')
        failed_orig = os.path.join(blocker, 'failed.mp4')
        failed_src = write_bytes(os.path.join(quarantine, 'failed.mp4'), b'x')
        write_manifest(logs, 'actions_20240501-100000.csv', [
            manifest_row(free_orig, free_src),
            manifest_row(renamed_orig, renamed_src),
            manifest_row(missing_orig, missing_src),
            manifest_row(failed_orig, failed_src),
        ])

        report = restore(logs)

        assert report['restored'] == 1
        assert report['renamed'] == 1
        assert report['skipped'] == 2
        assert (report['restored'] + report['renamed'] + report['skipped']
                == len(report['results']) == 4)
        assert [r['source'] for r in report['results']] == sorted(
            [free_src, renamed_src, missing_src, failed_src])
        outcomes = {r['source']: r['outcome'] for r in report['results']}
        assert outcomes[free_src] == 'restored'
        assert outcomes[renamed_src] == 'restored_renamed'
        assert outcomes[missing_src] == 'skipped_missing_source'
        assert outcomes[failed_src] == 'skipped_transfer_failed'
        for entry in report['results']:
            if entry['outcome'].startswith('skipped'):
                assert entry['dest'] is None
            else:
                assert entry['dest'] is not None
                assert os.path.exists(entry['dest'])
        assert read_bytes(renamed_orig) == b'occ'
        assert not os.path.exists(missing_src)
        assert read_bytes(failed_src) == b'x'
        assert read_bytes(blocker) == b'file'


def test_missing_logs_dir_raises_value_error():
    with workdir() as d:
        gone = os.path.join(d, 'no_such_logs')
        a_file = write_bytes(os.path.join(d, 'a_file.csv'), b'x')
        for bad in (gone, a_file, None, 42, ''):
            with pytest.raises(ValueError):
                bare_organizer().restore_quarantine(bad)


def main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith('test_') and callable(v)]
    for t in tests:
        t()
        print('ok', t.__name__)
    print('ALL QUARANTINE RESTORE TESTS PASS')


if __name__ == '__main__':
    main()
