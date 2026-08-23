"""Regression: multi-select folder removal (button + Delete key) + settings persistence."""
import gui, os, tempfile, shutil

gui.messagebox.showinfo = lambda *a, **k: None
gui.messagebox.askyesno = lambda *a, **k: True
gui.messagebox.showwarning = lambda *a, **k: None

tmp = tempfile.mkdtemp(prefix='vs_folders_')
# keep the app's settings.json away from the real one during the test
import unittest.mock as mock
with mock.patch.object(gui, 'APP_DIR', tmp):
    app = gui.App()
    app.update()

    # add 6 folders
    for i in range(6):
        app.folder_list.insert('end', f'C:/fake/folder{i}')

    # 1) remove a non-contiguous selection (0, 2, 4) via the button path
    app.folder_list.selection_clear(0, 'end')
    for i in (0, 2, 4):
        app.folder_list.selection_set(i)
    app.remove_folder()
    items = list(app.folder_list.get(0, 'end'))
    print('after removing 0,2,4:', items)
    assert items == ['C:/fake/folder1', 'C:/fake/folder3', 'C:/fake/folder5'], items

    # 2) settings.json reflects the removal
    import json
    with open(os.path.join(tmp, 'settings.json')) as fh:
        saved = json.load(fh)['folders']
    assert saved == items, saved
    print('PASS: settings.json updated after multi-remove')

    # 3) Delete-key binding removes the current selection (list must be focused,
    #    as it always is when a user clicks a folder then presses Delete)
    app.folder_list.selection_clear(0, 'end')
    app.folder_list.selection_set(1)          # 'folder3'
    app.folder_list.focus_set()
    app.folder_list.focus()
    app.update()
    app.folder_list.event_generate('<Delete>')
    app.update()
    items = list(app.folder_list.get(0, 'end'))
    print('after Delete key on folder3:', items)
    assert items == ['C:/fake/folder1', 'C:/fake/folder5'], items

    # 4) select-all then remove -> empty list, no crash
    app.folder_list.selection_set(0, 'end')
    app.remove_folder()
    assert app.folder_list.size() == 0
    print('PASS: select-all + remove clears list')

    # 5) remove with nothing selected: no crash, no settings change
    app.remove_folder()
    print('PASS: remove with empty selection is a safe no-op')

    # 6) re-add and verify settings save/load roundtrip via _save_settings
    for i in range(3):
        app.add_folder_silent = None
        app.folder_list.insert('end', f'D:/new{i}')
    app._save_settings()
    with open(os.path.join(tmp, 'settings.json')) as fh:
        saved = json.load(fh)['folders']
    assert saved == ['D:/new0', 'D:/new1', 'D:/new2'], saved
    print('PASS: settings roundtrip')

    app.destroy()

shutil.rmtree(tmp, ignore_errors=True)
print('ALL MULTI-SELECT TESTS PASS')
