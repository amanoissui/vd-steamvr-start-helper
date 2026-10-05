"""User-invoked warm startup helper. No service, task, driver or auto-start install."""
import ctypes as C
import json
from pathlib import Path
import queue
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from helper_core import Runner, restore_pending, summarize
from native_api import NativeAPI, runtime_paths, vr_processes


def status():
    processes = vr_processes()
    result = dict(processes=processes, read_only=True)
    if 'vrserver.exe' in processes:
        runtime, _, _ = runtime_paths()
        with NativeAPI(runtime) as api:
            result.update(blocked=api.blocked(), devices=summarize(api.devices()))
    print(json.dumps(result, ensure_ascii=False, indent=2))


def main():
    k = C.WinDLL('kernel32', use_last_error=True)
    k.CreateMutexW.argtypes = [C.c_void_p, C.c_bool, C.c_wchar_p]
    k.CreateMutexW.restype = C.c_void_p
    k.CloseHandle.argtypes = [C.c_void_p]
    handle = k.CreateMutexW(None, False, 'Local\\VDNativeStartupHelper')
    if not handle: raise C.WinError(C.get_last_error())
    if C.get_last_error() == 183:
        messagebox.showinfo('VD 起動補助', '起動補助は既に開いています。')
        k.CloseHandle(handle); return
    root = tk.Tk(); root.title('VD 起動補助 v2.1 — 純正トラッカー')
    root.geometry('650x410'); root.minsize(610,390)
    style = ttk.Style(); style.configure('TButton', font=('Yu Gothic UI', 11), padding=8)
    frame = ttk.Frame(root, padding=22); frame.pack(fill='both', expand=True)
    ttk.Label(frame, text='VD 純正トラッカーの起動補助 v2.1', font=('Yu Gothic UI', 17, 'bold')).pack(anchor='w')
    ttk.Label(frame, text='開始 → VDからSteamVRを起動 → 準備完了後に再起動',
              font=('Yu Gothic UI',11), wraplength=590).pack(anchor='w', pady=(10,7))
    ttk.Label(frame, text='VD設定：Forward tracking／Vive trackersはON、Index controllers／Hand trackingはOFF。\n'
              '準備完了後は「再起動開始」で待ち時間を省略できます。押さなければ90秒後に自動再起動。\n'
              'ゲームは起動完了の表示後に開始してください。',
              wraplength=590, font=('Yu Gothic UI',10)).pack(anchor='w')
    message = tk.StringVar(value='SteamVRが終了した状態で「開始」を押してください。\n現在動いているSteamVRには変更を加えません。')
    ttk.Label(frame, textvariable=message, wraplength=590, font=('Yu Gothic UI',12),
              justify='left').pack(anchor='w', pady=22, fill='x')
    events=queue.Queue(); worker=None; runner=None; closing=False
    buttons=ttk.Frame(frame); buttons.pack(side='bottom', fill='x')

    def begin():
        nonlocal worker, runner
        if worker and worker.is_alive(): return
        try:
            runner=Runner(events.put)
        except Exception as error:
            messagebox.showerror('起動補助',str(error)); return
        start.config(state='disabled'); restore.config(state='disabled'); cancel.config(state='normal')
        worker=threading.Thread(target=runner.run, daemon=True); worker.start()

    def cancel_work():
        if runner: runner.stop.set()
        message.set('中断処理中です。設定の確認が終わるまでお待ちください。')

    def restart_early():
        if runner and runner.request_restart():
            restart.config(state='disabled')
            message.set('再起動を開始します。しばらくお待ちください。')

    def restore_settings():
        try:
            message.set('一時設定を戻しました。' if restore_pending() else '戻す必要のある一時設定はありません。')
        except Exception as error: messagebox.showerror('設定の復元', str(error))

    def close():
        nonlocal closing
        if worker and worker.is_alive(): closing=True; cancel_work()
        else: root.destroy()

    def pump():
        try:
            while True: message.set(events.get_nowait())
        except queue.Empty: pass
        restart.config(state='normal' if worker and worker.is_alive() and runner.can_restart() else 'disabled')
        if worker and not worker.is_alive():
            start.config(state='normal'); restore.config(state='normal'); cancel.config(state='disabled')
            if closing: root.destroy(); return
        root.after(150, pump)

    start=ttk.Button(buttons,text='開始',command=begin); start.pack(side='left')
    cancel=ttk.Button(buttons,text='中断',command=cancel_work,state='disabled'); cancel.pack(side='left',padx=8)
    restart=ttk.Button(buttons,text='再起動開始',command=restart_early,state='disabled'); restart.pack(side='left')
    restore=ttk.Button(buttons,text='設定を戻す',command=restore_settings); restore.pack(side='right')
    root.protocol('WM_DELETE_WINDOW', close); root.after(150,pump)
    try: root.mainloop()
    finally: k.CloseHandle(handle)


if __name__ == '__main__':
    if '--status' in sys.argv: status()
    else: main()
