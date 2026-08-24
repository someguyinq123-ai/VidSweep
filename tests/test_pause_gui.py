"""Reproduce: GUI pause -> resume -> cancel sequence through real button commands."""
import gui, core, os, tempfile, shutil, subprocess, time

gui.messagebox.showinfo = lambda *a, **k: None
gui.messagebox.askyesno = lambda *a, **k: True
gui.messagebox.showwarning = lambda *a, **k: None

FF = 'ffmpeg'
tmp = tempfile.mkdtemp(prefix='vs_pause_')
a = os.path.join(tmp, 'a'); os.makedirs(a)
# enough videos that the scan runs for a while
for gi in range(12):
    v = os.path.join(a, f'v{gi:02}.mp4')
    dur = 10 + gi
    subprocess.run([FF, '-y', '-v', 'error',
                    '-f', 'lavfi', '-i', f'testsrc2=s=1280x720:r=30:d={dur}',
                    '-c:v', 'libx264', '-preset', 'medium', '-pix_fmt', 'yuv420p', v],
                   check=True, capture_output=True)

db = os.path.join(tmp, 't.db')
app = gui.App()
app.org = core.VideoOrganizer(db_path=db)
app.thumbs = gui.ThumbnailCache(app.org)

state = {'events': [], 'phase': 0}

def log(msg):
    print(msg, flush=True)

def run():
    ph = state['phase']
    t = time.monotonic()
    if ph == 0:
        log('phase0: starting scan')
        app.folder_list.insert('end', a)
        app.start_scan()
        state['t0'] = t
        state['phase'] = 1
    elif ph == 1:
        # wait until some progress, then click Pause (through the real command)
        if app.progress['value'] < 20:
            return
        log(f'phase1: pausing at {app.progress["value"]:.0f}%')
        app.toggle_pause()  # same as clicking the button
        state['events'].append(f'paused at {app.progress["value"]:.0f}%')
        state['paused_at'] = time.monotonic()
        state['phase'] = 2
    elif ph == 2:
        # confirm we're actually paused for 2s
        if time.monotonic() - state['paused_at'] < 2.0:
            return
        state['events'].append(f'2s later, progress={app.progress["value"]:.0f}% (should equal paused value)')
        state['frozen_value'] = app.progress['value']
        app.toggle_pause()  # click Resume
        state['events'].append('clicked Resume')
        state['resumed_at'] = time.monotonic()
        state['phase'] = 3
    elif ph == 3:
        # after resume, progress must move again
        if app.progress['value'] > state['frozen_value'] + 5:
            state['events'].append(f'RESUME OK: progress now {app.progress["value"]:.0f}%')
            state['phase'] = 4
            return
        if time.monotonic() - state['resumed_at'] > 10:
            state['events'].append(f'RESUME HANG: stuck at {app.progress["value"]:.0f}% for 10s')
            state['phase'] = 4
            return
    elif ph == 4:
        if 'RESUME HANG' in state['events'][-1]:
            # try cancel while in the bad state
            app.cancel_scan()
            state['cancel_at'] = time.monotonic()
            state['phase'] = 5
            return
        else:
            app.cancel_scan()
            state['cancel_at'] = time.monotonic()
            state['phase'] = 5
            return
    elif ph == 5:
        th = app._scan_thread
        if th is not None and th.is_alive():
            if time.monotonic() - state['cancel_at'] > 10:
                state['events'].append('CANCEL HANG: thread still alive after 10s')
                app.destroy()
            return
        state['events'].append('CANCEL OK: scan thread ended')
        app.destroy()
    else:
        return
    app.after(200, run)

app.after(300, run)
app.mainloop()
print('\n'.join(state['events']))
shutil.rmtree(tmp, ignore_errors=True)
