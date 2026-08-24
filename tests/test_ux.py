"""Verify cancel-during-pause/resume UX + privacy dialog layout."""
import gui, core, os, tempfile, shutil, subprocess, threading, time

gui.messagebox.showinfo = lambda *a, **k: None
gui.messagebox.askyesno = lambda *a, **k: True
gui.messagebox.showwarning = lambda *a, **k: None
gui.messagebox.showerror = lambda *a, **k: print('SHOWERROR:', a, flush=True)

FF = 'ffmpeg'
tmp = tempfile.mkdtemp(prefix='vs_ux_')
a = os.path.join(tmp, 'a'); os.makedirs(a)
print('building videos...', flush=True)
for gi in range(8):
    v = os.path.join(a, f'v{gi}.mp4')
    subprocess.run([FF, '-y', '-v', 'error',
                    '-f', 'lavfi', '-i', f'testsrc2=s=640x360:r=24:d={40+gi*15}',
                    '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', v],
                   check=True, capture_output=True)
print('videos built', flush=True)

app = gui.App()
# ISOLATION: the App loads the user's real folders from settings.json — clear them
app.folder_list.delete(0, 'end')
app.org = core.VideoOrganizer(db_path=os.path.join(tmp, 't.db'))
app.thumbs = gui.ThumbnailCache(app.org)
state = {'phase': 0, 'events': []}
print('gui built, starting loop', flush=True)

def log(m):
    state['events'].append(m)
    print(m, flush=True)

def run():
    try:
        ph = state['phase']
        if ph == 0:
            log(f'phase0 start; scan_thread alive={app._scan_thread and app._scan_thread.is_alive()}')
            app.folder_list.insert('end', a)
            app.start_scan()
            log('scan started')
            state['t0'] = time.monotonic()
            state['phase'] = 1
        elif ph == 1:
            prog = app.progress['value']
            alive = app._scan_thread and app._scan_thread.is_alive()
            if time.monotonic() - state['t0'] > 20:
                log(f'TIMEOUT waiting: prog={prog} alive={alive} status={app.status_var.get()}')
                state['phase'] = 99; app.destroy(); return
            if prog >= 10 or (not alive and prog > 0):
                app.toggle_pause(); log(f'paused at prog={prog}')
                state['at'] = time.monotonic(); state['phase'] = 2
        elif ph == 2:
            if time.monotonic() - state['at'] > 1.5:
                app.toggle_pause(); log('resumed'); state['at'] = time.monotonic(); state['phase'] = 3
        elif ph == 3:
            if time.monotonic() - state['at'] > 1.5:
                app.cancel_scan(); log(f'cancel clicked -> status: {app.status_var.get()}')
                state['at'] = time.monotonic(); state['phase'] = 4
        elif ph == 4:
            # simulate the user clicking Resume after Cancel (must NOT say resumed)
            app.toggle_pause()
            log(f'resume-after-cancel -> status: {app.status_var.get()}')
            assert 'esumed' not in app.status_var.get(), 'resume allowed after cancel!'
            state['at'] = time.monotonic(); state['phase'] = 5
        elif ph == 5:
            th = app._scan_thread
            alive = th is not None and th.is_alive()
            if not alive:
                log('scan thread ended cleanly after cancel')
                state['phase'] = 6
            elif time.monotonic() - state['at'] > 60:
                log('FAIL: thread alive 60s after cancel'); state['phase'] = 7; app.destroy(); return
            # keep waiting otherwise (don't reschedule below for this branch)
        elif ph == 6:
            # privacy dialog layout check
            app.open_privacy_settings()
            win = app.nametowidget(app.winfo_children()[-1])
            app.update()
            # checkboxes/desc-labels live inside the padded inner frame,
            # so walk all descendants, not just direct children
            def descendants(w):
                yield w
                for c in w.winfo_children():
                    yield from descendants(c)
            boxes = []
            desc_rows = set()
            for w in descendants(win):
                if isinstance(w, gui.tk.ttk.Checkbutton):
                    boxes.append(w)
                elif isinstance(w, gui.tk.ttk.Label) and w.cget('wraplength'):
                    desc_rows.add(w.grid_info()['row'])
            box_rows = [b.grid_info()['row'] for b in boxes]
            # every checkbox on its own row, none sharing a row with a
            # description label or the header (row 0)
            assert len(boxes) == 3 and len(desc_rows) >= 3, (len(boxes), len(desc_rows))
            assert len(set(box_rows)) == 3 and not (set(box_rows) & desc_rows) \
                and 0 not in box_rows, (box_rows, desc_rows)
            win.destroy()
            # don't leak the test folder into user's real settings.json
            app.folder_list.delete(0, 'end')
            app._save_settings()
            app.update()
            app.destroy()
            print('\n'.join(state['events']))
            print('UX TESTS PASS')
            return
        else:
            return
    except Exception as e:
        import traceback; traceback.print_exc()
        state['events'].append(f'FAIL {e}')
        app.destroy()
        return
    app.after(150, run)

app.after(200, run)
app.mainloop()
shutil.rmtree(tmp, ignore_errors=True)
