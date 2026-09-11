"""The export UI wiring: selection, cancellation, success and failure handling.

The flow is exercised through its injected dialogs so the assertions are about the
contract the button relies on, not about Tk itself.
"""
import pytest

from gui import export_snapshot_to_csv

SHOWN = [[{"path": "a.mp4", "size": 10}, {"path": "b.mp4", "size": 5}],
         [{"path": "c.mp4", "size": 3}]]


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, *a, **k):
        self.calls.append((a, k))


def flow(snapshot, *, path="out.csv", boom=None):
    seen = {}
    info, error = Recorder(), Recorder()

    def export(groups, dest):
        seen["groups"] = groups
        seen["dest"] = dest
        if boom:
            raise boom

    result = export_snapshot_to_csv(snapshot, ask_path=lambda: path, export=export,
                                    info=info, error=error)
    return result, seen, info, error


def test_exports_exactly_the_shown_snapshot_and_counts_from_it():
    snapshot = list(SHOWN)
    result, seen, info, error = flow(snapshot)
    assert result == {"exported": True, "groups": 2, "rows": 3, "path": "out.csv"}
    assert seen["groups"] == SHOWN and seen["dest"] == "out.csv"
    assert len(info.calls) == 1 and not error.calls
    message = info.calls[0][0][1]
    assert "2 group(s)" in message and "3 file row(s)" in message and "out.csv" in message


def test_the_exported_snapshot_is_immune_to_a_later_reload():
    shown = list(SHOWN)
    snapshot = list(shown)
    shown.clear()                     # the view reloads mid-export
    result, seen, _, _ = flow(snapshot)
    assert result["exported"] is True
    assert seen["groups"] == SHOWN    # the snapshot, not the emptied live list


def test_cancellation_is_a_silent_noop_before_the_exporter_runs():
    result, seen, info, error = flow(SHOWN, path="")
    assert result == {"exported": False, "reason": "cancelled"}
    assert seen == {}                 # the exporter was never called
    assert not info.calls and not error.calls


def test_nothing_shown_is_reported_without_calling_the_exporter():
    result, seen, info, error = flow([])
    assert result == {"exported": False, "reason": "nothing_shown"}
    assert seen == {} and len(info.calls) == 1 and not error.calls


def test_a_write_failure_is_surfaced_and_claims_nothing_about_the_file():
    result, seen, info, error = flow(SHOWN, boom=OSError("disk full"))
    assert result["exported"] is False and result["reason"] == "error"
    assert result["error"] == "disk full"
    assert len(error.calls) == 1 and not info.calls
    message = error.calls[0][0][1]
    assert "disk full" in message
    # the engine promises overwrite + a visible failure, NOT an atomic write
    assert "no file" not in message.lower() and "unchanged" not in message.lower()
