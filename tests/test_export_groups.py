"""Behavioral tests for VideoOrganizer.export_groups (feature F-EXP-1).

Pins the CSV projection contract: exact column names/values and order, keeper
marking by position, duration/bitrate formatting (true kbps from the unrounded
duration), missing-value blanks, resolution/codec mapping, conservative
wasted-byte calculation, encoding and newline rules, failure propagation, and
the absence of any database access.

Run directly with the pinned interpreter:
    python tests/test_export_groups.py
or under pytest:
    pytest tests/test_export_groups.py
"""
import copy
import csv
import os
import shutil
import sys
import tempfile
from contextlib import contextmanager

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core


# The frozen column order for export_groups().
HEADER = ['group_id', 'index_in_group', 'is_keep_best', 'path', 'size_bytes',
          'duration_seconds', 'bitrate_kbps', 'resolution', 'codec',
          'wasted_bytes_in_group']


@contextmanager
def workdir():
    d = tempfile.mkdtemp(prefix='vidsweep_export_')
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def make_org(d):
    return core.VideoOrganizer(db_path=os.path.join(d, 'library.db'))


def record(path, size=None, duration=None, width=None, height=None,
           vcodec=None):
    """A scan record shaped like core._load_records() output."""
    return {'path': path, 'size': size, 'sha': 'sha:' + str(path),
            'hashes': [], 'duration': duration, 'width': width,
            'height': height, 'vcodec': vcodec}


def parse(path):
    with open(path, 'r', encoding='utf-8', newline='') as fh:
        return list(csv.reader(fh))


def raw_bytes(path):
    with open(path, 'rb') as fh:
        return fh.read()


def test_header_is_exact_and_empty_input_writes_header_only():
    with workdir() as d:
        org = make_org(d)
        dest = os.path.join(d, 'out.csv')
        org.export_groups([], dest)
        rows = parse(dest)
        assert rows == [HEADER], rows
        assert list(core.VideoOrganizer.EXPORT_HEADER) == HEADER
        data = raw_bytes(dest)
        assert data == (','.join(HEADER) + '\n').encode('utf-8'), data
        assert not data.startswith(b'\xef\xbb\xbf'), 'BOM must not be written'
        assert b'\r' not in data, 'records must end with LF only'
        org.close()


def test_exact_values_two_groups_and_no_mutation():
    with workdir() as d:
        org = make_org(d)
        awkward = 'C:\\vids\\a "quoted", comma\nnewline.mp4'
        m0 = record('C:\\vids\\keeper.mp4', size=100_000_000, duration=20.0,
                    width=1920, height=1080, vcodec='h264')
        m1 = record('C:\\vids\\copy.mp4', size=50_000_000, duration=8.0,
                    width=1280, height=720, vcodec='hevc')
        n0 = record(awkward, size=500_000, duration=4.25,
                    width=640, height=480, vcodec='vp9')
        n1 = record('C:\\vids\\bare.mp4')
        groups = [[m0, m1], [n0, n1]]
        snapshot = copy.deepcopy(groups)

        dest = os.path.join(d, 'out.csv')
        org.export_groups(groups, dest)

        rows = parse(dest)
        assert rows == [
            HEADER,
            # bitrate_kbps: 100000000 * 8 / 20.0 / 1000 = 40000
            ['0', '0', '1', 'C:\\vids\\keeper.mp4', '100000000', '20.00',
             '40000', '1920x1080', 'h264', '50000000'],
            # bitrate_kbps: 50000000 * 8 / 8.0 / 1000 = 50000
            ['0', '1', '0', 'C:\\vids\\copy.mp4', '50000000', '8.00',
             '50000', '1280x720', 'hevc', '50000000'],
            # bitrate_kbps: 500000 * 8 / 4.25 / 1000 = 941.18 -> 941
            ['1', '0', '1', awkward, '500000', '4.25', '941', '640x480',
             'vp9', ''],
            ['1', '1', '0', 'C:\\vids\\bare.mp4', '', '', '', '', '', ''],
        ], rows

        # no regrouping, reordering, reranking, or mutation of the input
        assert groups == snapshot
        assert groups[0][0] is m0 and groups[0][1] is m1
        assert groups[1][0] is n0 and groups[1][1] is n1
        org.close()


def test_keeper_is_index_zero_not_the_best_copy():
    with workdir() as d:
        org = make_org(d)
        # index 0 is deliberately the worst copy by every quality signal
        worst = record('keeper-at-index-0.mp4', size=100, duration=10.0,
                       width=320, height=240, vcodec='mpeg4')
        best = record('better-second.mp4', size=9_000_000, duration=10.0,
                      width=3840, height=2160, vcodec='av1')
        dest = os.path.join(d, 'out.csv')
        org.export_groups([[worst, best]], dest)
        rows = parse(dest)[1:]
        assert [r[2] for r in rows] == ['1', '0']
        assert [r[1] for r in rows] == ['0', '1']
        assert [r[3] for r in rows] == ['keeper-at-index-0.mp4',
                                        'better-second.mp4']
        org.close()


def test_duration_formatting_and_bitrate_from_unrounded_duration():
    with workdir() as d:
        org = make_org(d)
        recs = [record('a.mp4', size=100_000, duration=2.6),
                record('b.mp4', size=1_000_000, duration=8.5),
                record('c.mp4', size=2_500_000, duration=12.0),
                record('d.mp4', size=1_000_000, duration=7.777)]
        dest = os.path.join(d, 'out.csv')
        org.export_groups([recs], dest)
        rows = parse(dest)[1:]
        assert [r[5] for r in rows] == ['2.60', '8.50', '12.00', '7.78']
        # true kbps, computed from the ORIGINAL duration:
        #   100_000 * 8 / 2.6   / 1000 = 307.69  -> 308
        #   1_000_000 * 8 / 8.5 / 1000 = 941.18  -> 941
        #   2_500_000 * 8 / 12  / 1000 = 1666.67 -> 1667
        #   1_000_000 * 8 / 7.777 / 1000 = 1028.67 -> 1029
        # (using the DISPLAYED 7.78 instead of 7.777 would give 1028)
        assert [r[6] for r in rows] == ['308', '941', '1667', '1029']
        org.close()


def test_bitrate_blank_rules_and_zero_duration_preserved():
    with workdir() as d:
        org = make_org(d)
        recs = [record('missing_dur.mp4', size=1000),
                record('zero_dur.mp4', size=1000, duration=0.0),
                record('zero_size.mp4', size=0, duration=10.0),
                record('missing_size.mp4', duration=10.0)]
        dest = os.path.join(d, 'out.csv')
        org.export_groups([recs], dest)
        rows = parse(dest)[1:]
        # missing duration: both duration and bitrate blank
        assert rows[0][5] == '' and rows[0][6] == ''
        # zero duration is legitimate data ("0.00") but never divides: blank bitrate
        assert rows[1][5] == '0.00' and rows[1][6] == ''
        # zero-byte file with positive duration: integer bitrate 0, not blank
        assert rows[2][4] == '0' and rows[2][5] == '10.00' and rows[2][6] == '0'
        # missing size: blank size and blank bitrate
        assert rows[3][4] == '' and rows[3][6] == ''
        org.close()


def test_missing_metadata_blanks_and_resolution_codec():
    with workdir() as d:
        org = make_org(d)
        recs = [record('full.mp4', size=0, duration=5.0,
                       width=3840, height=2160, vcodec='av1'),
                record('no_width.mp4', size=10, duration=5.0,
                       height=2160, vcodec='h264'),
                record('no_height.mp4', size=10, duration=5.0, width=1920),
                record('nothing.mp4')]
        dest = os.path.join(d, 'out.csv')
        org.export_groups([recs], dest)
        rows = parse(dest)[1:]
        assert rows[0][4] == '0'  # legitimate zero size preserved
        assert rows[0][7] == '3840x2160' and rows[0][8] == 'av1'
        assert rows[1][7] == '' and rows[1][8] == 'h264'
        assert rows[2][7] == '' and rows[2][8] == ''
        assert rows[3][4:] == ['', '', '', '', '', ''], rows[3]
        assert [r[2] for r in rows] == ['1', '0', '0', '0']
        org.close()


def test_wasted_bytes_repeated_and_blank_when_any_size_unknown():
    with workdir() as d:
        org = make_org(d)
        known = [record('k0.mp4', size=300, duration=10.0),
                 record('k1.mp4', size=700, duration=10.0),
                 record('k2.mp4', size=500, duration=10.0)]
        unknown = [record('u0.mp4', size=300, duration=10.0),
                   record('u1.mp4', duration=10.0)]
        dest = os.path.join(d, 'out.csv')
        org.export_groups([known, unknown], dest)
        rows = parse(dest)[1:]
        # group 0: (300 + 700 + 500) - 300 (index-zero keeper) = 1200 on EVERY row
        assert [r[9] for r in rows[:3]] == ['1200', '1200', '1200']
        # group 1: one member size unknown -> blank on EVERY row
        assert [r[9] for r in rows[3:]] == ['', '']
        org.close()


def test_roundtrip_utf8_no_bom_lf_only():
    with workdir() as d:
        org = make_org(d)
        awkward = 'C:\\vidéos\\naïve, "quoted"\nsecond line.mp4'
        groups = [[record(awkward, size=1, duration=1.0,
                          width=2, height=3, vcodec='h264')]]
        dest = os.path.join(d, 'out.csv')
        org.export_groups(groups, dest)
        data = raw_bytes(dest)
        assert not data.startswith(b'\xef\xbb\xbf'), 'BOM must not be written'
        assert b'\r' not in data, 'records must end with LF only'
        assert data.endswith(b'\n')
        data.decode('utf-8')  # strict decode proves valid UTF-8
        rows = parse(dest)
        assert rows[1][3] == awkward
        assert rows[1][7] == '2x3'
        assert rows[1][8] == 'h264'
        org.close()


def test_existing_destination_is_replaced():
    with workdir() as d:
        org = make_org(d)
        dest = os.path.join(d, 'out.csv')
        with open(dest, 'w', encoding='utf-8') as fh:
            fh.write('stale,garbage\nmore junk that must disappear\n')
        org.export_groups([[record('a.mp4', size=1, duration=1.0)]], dest)
        rows = parse(dest)
        assert rows[0] == HEADER
        assert len(rows) == 2
        assert 'stale' not in raw_bytes(dest).decode('utf-8')
        org.close()


def test_missing_parent_dir_raises_and_is_not_created():
    with workdir() as d:
        org = make_org(d)
        missing = os.path.join(d, 'no_such_dir', 'out.csv')
        raised = False
        try:
            org.export_groups([[record('a.mp4')]], missing)
        except OSError:
            raised = True
        assert raised, 'export must propagate the open() failure'
        assert not os.path.exists(os.path.dirname(missing)), \
            'export must not create missing parent directories'
        org.close()


def test_write_failure_propagates():
    with workdir() as d:
        org = make_org(d)
        target_dir = os.path.join(d, 'a_directory')
        os.makedirs(target_dir)
        raised = False
        try:
            org.export_groups([[record('a.mp4')]], target_dir)
        except OSError:
            raised = True
        assert raised, 'export must propagate write failures unchanged'
        org.close()


def test_export_does_not_touch_database_state():
    with workdir() as d:
        db_path = os.path.join(d, 'library.db')
        org = make_org(d)
        with open(db_path, 'rb') as fh:
            before = fh.read()

        real_db = org.db

        class NoDB:
            def __getattr__(self, name):
                raise AssertionError(
                    'export_groups touched the database: ' + name)

        org.db = NoDB()
        dest = os.path.join(d, 'out.csv')
        org.export_groups([[record('a.mp4', size=5, duration=1.0)]], dest)
        assert parse(dest)[1][4] == '5'

        org.db = real_db
        with open(db_path, 'rb') as fh:
            after = fh.read()
        assert after == before, 'library cache must be untouched by export'
        org.close()


def main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith('test_') and callable(v)]
    for t in tests:
        t()
        print('ok', t.__name__)
    print('ALL EXPORT TESTS PASS')


if __name__ == '__main__':
    main()