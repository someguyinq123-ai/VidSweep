"""Behavioral tests for the reclaimable-space summary helpers (feature F-EXP-2).

Pins the module-level, Tk-free contract:

  * reclaimable_summary() — the exact four keys, group/file counts, the
    keeper-first waste math, the conservative "any member size unknown -> the
    whole group is not measurable" policy, empty groups, and non-mutation of
    the caller's data;
  * human_bytes() — 1024-based units, plain bytes below 1024, one decimal
    place above that, unit choice from the UNROUNDED value, TB as the cap,
    and ValueError on negative input;
  * reclaim_label_text() — the exact sentence, with the unknown-size suffix
    only when it applies.

The post-load composition (label + bracketed scope, crash-proof with unknown
sizes) is exercised against a stand-in app object, so nothing here creates a
Tk instance or opens the database.

Run directly with the pinned interpreter:
    python tests/test_reclaim_summary.py
or under pytest:
    pytest tests/test_reclaim_summary.py
"""
import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gui


def rec(size, path='movie.mp4'):
    """A scan record shaped like core._load_records() output."""
    return {'path': path, 'size': size}


# --------------------------------------------------------------- summary keys
def test_summary_has_exactly_the_four_contract_keys():
    s = gui.reclaimable_summary([[rec(10), rec(5), rec(3)]])
    assert set(s) == {'groups', 'files', 'wasted_bytes', 'unknown_groups'}
    assert len(s) == 4


def test_summary_counts_every_group_and_every_member():
    groups = [[rec(10), rec(5), rec(3)], [rec(1)], [rec(2), rec(None)]]
    s = gui.reclaimable_summary(groups)
    assert s['groups'] == 3
    assert s['files'] == 6  # 3 + 1 + 2, known or unknown


def test_summary_empty_input_is_all_zero():
    assert gui.reclaimable_summary([]) == {'groups': 0, 'files': 0,
                                           'wasted_bytes': 0,
                                           'unknown_groups': 0}


# ------------------------------------------------------------ waste arithmetic
def test_summary_waste_is_keeper_first():
    assert gui.reclaimable_summary([[rec(10), rec(5), rec(3)]])['wasted_bytes'] == 8
    # the FIRST member is the keeper, not the largest one
    s = gui.reclaimable_summary([[rec(3), rec(10), rec(5)]])
    assert s['wasted_bytes'] == 18 - 3


def test_summary_zero_size_is_a_known_size():
    groups = [[rec(0), rec(0)], [rec(0), rec(7)]]
    s = gui.reclaimable_summary(groups)
    assert s['unknown_groups'] == 0
    assert s['wasted_bytes'] == 0 + 7
    assert s['files'] == 4


def test_summary_single_member_group_is_measurable_and_wastes_nothing():
    s = gui.reclaimable_summary([[rec(9)]])
    assert s['wasted_bytes'] == 0
    assert s['unknown_groups'] == 0
    assert s['files'] == 1


# -------------------------------------------------- conservative measurability
def test_summary_none_size_excludes_the_whole_group():
    groups = [[rec(100), rec(None), rec(50)]]
    s = gui.reclaimable_summary(groups)
    assert s['wasted_bytes'] == 0          # not guessed at
    assert s['unknown_groups'] == 1
    assert s['groups'] == 1 and s['files'] == 3


def test_summary_absent_size_key_excludes_the_whole_group():
    groups = [[rec(100), {'path': 'no-size.mp4'}]]
    s = gui.reclaimable_summary(groups)
    assert s['wasted_bytes'] == 0
    assert s['unknown_groups'] == 1
    assert s['files'] == 2


def test_summary_empty_group_is_unknown():
    s = gui.reclaimable_summary([[], [rec(4), rec(1)]])
    assert s['groups'] == 2
    assert s['files'] == 2
    assert s['unknown_groups'] == 1       # the empty group has no keeper
    assert s['wasted_bytes'] == 1


def test_summary_mixed_groups():
    groups = [[rec(10), rec(5), rec(3)],   # measurable: 8
              [rec(100), rec(None)],       # unknown
              [],                          # empty: unknown
              [rec(2)]]                    # measurable: 0
    s = gui.reclaimable_summary(groups)
    assert s == {'groups': 4, 'files': 6, 'wasted_bytes': 8, 'unknown_groups': 2}


# ---------------------------------------------------------------- no mutation
def test_summary_does_not_mutate_its_input():
    groups = [[rec(10), rec(5), rec(3)], [rec(2), rec(None)], []]
    snapshot = copy.deepcopy(groups)
    gui.reclaimable_summary(groups)
    assert groups == snapshot
    assert len(groups) == 3
    assert len(groups[0]) == 3
    assert [r['size'] for r in groups[0]] == [10, 5, 3]


# ----------------------------------------------------------------- byte units
def test_human_bytes_plain_bytes_below_1024():
    assert gui.human_bytes(0) == '0 B'
    assert gui.human_bytes(1) == '1 B'
    assert gui.human_bytes(1023) == '1023 B'


def test_human_bytes_is_1024_based_through_tb():
    assert gui.human_bytes(1024) == '1.0 KB'
    assert gui.human_bytes(1536) == '1.5 KB'
    assert gui.human_bytes(1024 ** 2) == '1.0 MB'
    assert gui.human_bytes(1024 ** 2 + 512 * 1024) == '1.5 MB'
    assert gui.human_bytes(1024 ** 3) == '1.0 GB'
    assert gui.human_bytes(1073741824) == '1.0 GB'
    assert gui.human_bytes(1024 ** 4) == '1.0 TB'
    assert gui.human_bytes(1024 ** 5) == '1024.0 TB'   # TB is never exceeded


def test_human_bytes_unit_comes_from_the_unrounded_value():
    # 1048575 / 1024 = 1023.999... -> still KB, and it ROUNDS to 1024.0
    assert gui.human_bytes(1048575) == '1024.0 KB'
    assert gui.human_bytes(1048576) == '1.0 MB'
    assert gui.human_bytes(1024 ** 3 - 1) == '1024.0 MB'
    assert gui.human_bytes(1024 ** 3) == '1.0 GB'
    assert gui.human_bytes(1024 ** 4 - 1) == '1024.0 GB'
    assert gui.human_bytes(1024 ** 4) == '1.0 TB'


def test_human_bytes_negative_input_raises_value_error():
    for bad in (-1, -1024, -(1024 ** 4)):
        with pytest.raises(ValueError):
            gui.human_bytes(bad)


# --------------------------------------------------------------- label text
def test_label_text_without_unknown_suffix():
    s = {'wasted_bytes': 1073741824, 'groups': 3, 'unknown_groups': 0}
    assert gui.reclaim_label_text(s) == 'Reclaimable: 1.0 GB across 3 group(s)'


def test_label_text_with_unknown_suffix():
    s = {'wasted_bytes': 1073741824, 'groups': 3, 'unknown_groups': 2}
    assert gui.reclaim_label_text(s) == (
        'Reclaimable: 1.0 GB across 3 group(s) '
        '\u00b7 2 group(s) with unknown sizes')


def test_label_text_zero_summary():
    s = {'wasted_bytes': 0, 'groups': 0, 'unknown_groups': 0}
    assert gui.reclaim_label_text(s) == 'Reclaimable: 0 B across 0 group(s)'


def test_label_text_derived_from_a_summary():
    groups = [[rec(10), rec(5), rec(3)], [rec(100), rec(None)]]
    assert gui.reclaim_label_text(gui.reclaimable_summary(groups)) == (
        'Reclaimable: 8 B across 2 group(s) '
        '\u00b7 1 group(s) with unknown sizes')


# ------------------------------------------------- post-load label (no Tk)
class _Widget:
    def __init__(self):
        self.calls = []

    def config(self, **kw):
        self.calls.append(kw)


class _Tree:
    def __init__(self):
        self.rows = []

    def get_children(self):
        return []

    def delete(self, iid):
        pass

    def insert(self, parent, index, iid=None, values=None):
        self.rows.append((iid, values))


class _Status:
    def __init__(self):
        self.text = None

    def set(self, text):
        self.text = text


class _FakeApp:
    """Just enough of App's surface for App._poll_load_result to run with no Tk."""

    _update_marked_count = gui.App._update_marked_count

    def __init__(self, groups, scope):
        self.groups = []
        self.decisions = {}
        self.refresh_btn = _Widget()
        self.group_tree = _Tree()
        self.dupe_summary = _Widget()
        self.status_var = _Status()
        self._load_result = (groups, None)
        self._load_scope_txt = scope


def test_load_label_is_summary_plus_unchanged_scope():
    groups = [[rec(10), rec(5), rec(3)], [rec(100), rec(None)]]
    app = _FakeApp(groups, 'current scan batch')
    gui.App._poll_load_result(app)
    assert app.dupe_summary.calls[-1]['text'] == (
        'Reclaimable: 8 B across 2 group(s) '
        '\u00b7 1 group(s) with unknown sizes [current scan batch]')
    assert app.status_var.text == 'Loaded 2 duplicate groups.'


def test_load_with_unknown_sizes_does_not_raise():
    groups = [[rec(None), rec(5)], [{'path': 'b.mp4'}, rec(7)]]
    app = _FakeApp(groups, 'entire library')
    gui.App._poll_load_result(app)  # must complete without an exception
    assert app.dupe_summary.calls[-1]['text'].endswith('[entire library]')
    # both groups are unmeasurable, so the summary must say so
    assert app.dupe_summary.calls[-1]['text'].startswith(
        'Reclaimable: 0 B across 2 group(s) '
        '\u00b7 2 group(s) with unknown sizes')


def main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith('test_') and callable(v)]
    for t in tests:
        t()
        print('ok', t.__name__)
    print('ALL RECLAIM SUMMARY TESTS PASS')


if __name__ == '__main__':
    main()