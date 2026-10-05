import datetime
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time
from native_api import NativeAPI, runtime_paths, vr_processes

ROOT = Path(__file__).resolve().parent
STATE = ROOT / 'pending.json'
SECTION = 'driver_VirtualDesktop'
KEY = 'blocked_by_safe_mode'
WARMUP_SECONDS = 90
SETTLE_SECONDS = 5


class Cancelled(Exception): pass


def patch_block(text, present, value):
    pattern = re.compile(r'("driver_VirtualDesktop"\s*:\s*\{)([^{}]*)(\})')
    matches = list(pattern.finditer(text))
    if len(matches) != 1:
        raise RuntimeError('VD設定の形式が想定と異なるため、変更を停止しました。')
    match = matches[0]; body = match.group(2)
    fields = list(re.finditer(r'"blocked_by_safe_mode"\s*:\s*(true|false)', body))
    if len(fields) > 1:
        raise RuntimeError('VDのブロック設定が重複しています。')
    if fields:
        f = fields[0]
        if present:
            body = body[:f.start(1)] + json.dumps(bool(value)) + body[f.end(1):]
        else:
            after = re.match(r'\s*,', body[f.end():])
            before = re.search(r',\s*$', body[:f.start()])
            if after:
                body = body[:f.start()] + body[f.end()+after.end():]
            else:
                body = body[:before.start() if before else f.start()] + body[f.end():]
    elif present:
        indent = re.search(r'\n([ \t]+)"', body)
        indent = indent.group(1) if indent else '      '
        body = '\n' + indent + '"blocked_by_safe_mode" : ' + json.dumps(bool(value)) + (',' if body.strip() else '') + body
    result = text[:match.start(2)] + body + text[match.end(2):]
    expected = json.loads(text)
    if present: expected[SECTION][KEY] = bool(value)
    else: expected[SECTION].pop(KEY, None)
    if json.loads(result) != expected:
        raise RuntimeError('対象外の設定に差分が出たため、変更を停止しました。')
    return result


def atomic_write(path, text):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='',
                                        dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name); stream.write(text)
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists(): temporary.unlink()


def offline_block(config, present, value):
    if vr_processes():
        raise RuntimeError('SteamVRが動作中です。設定ファイルは変更していません。')
    original = config.read_text(encoding='utf-8-sig')
    changed = patch_block(original, present, value)
    if vr_processes() or config.read_text(encoding='utf-8-sig') != original:
        raise RuntimeError('起動または設定変更を検出したため、書き込みを停止しました。')
    atomic_write(config, changed)


def write_state(data): atomic_write(STATE, json.dumps(data, ensure_ascii=False, indent=2))


def restore_pending():
    if not STATE.exists(): return False
    state = json.loads(STATE.read_text(encoding='utf-8'))
    if not state.get('active'): return False
    if state.get('restart_requested'):
        # The intended final configuration is already unblocked. Never initiate
        # another restart or re-block a successful native session on recovery.
        state['active'] = False; write_state(state); return False
    # Restoring an originally unblocked key during startup recreates the crash.
    # Recovery must never connect to or unblock a starting/running runtime.
    offline_block(Path(state['config']), state['old_present'], state['old_value'])
    state['active'] = False; write_state(state); return True


def summarize(devices):
    active = [d for d in devices if d['valid'] and d['connected']]
    return dict(hmd=any(d['kind'] == 1 for d in active),
                controllers=sum(d['kind'] == 2 and d['source'].lower() == 'oculus' for d in active),
                native_trackers=sum(d['kind'] == 3 and d['source'].lower() == 'virtualdesktop' for d in active))


def current_session(log_dir, pid):
    text = (log_dir / 'vrserver.txt').read_text(encoding='utf-8', errors='replace')
    starts = list(re.finditer(r'vrserver [^\r\n]+ startup with PID=(\d+)', text))
    for index in range(len(starts) - 1, -1, -1):
        entry = starts[index]
        if int(entry.group(1)) == pid:
            end = starts[index + 1].start() if index + 1 < len(starts) else len(text)
            return text[entry.start():end]
    return ''


def hardware_ready_in_log(session):
    if 'VR server shutting down' in session or 'Graceful exit' in session:
        return False
    if 'Active HMD set to oculus.' not in session:
        return False
    return all(re.search(r"Driver 'oculus' finished adding tracked device with serial number '[^'\r\n]+_Controller_"
                         + side + "'", session) for side in ('Left', 'Right'))


class Runner:
    def __init__(self, update):
        self.update = update
        self.stop = threading.Event()
        self.ready_to_restart = threading.Event()
        self.restart_now = threading.Event()
        self.state = None
        self.run_dir = None
        self.runtime, self.config, self.logs = runtime_paths()

    def note(self, message):
        self.update(message)
        if self.run_dir:
            with (self.run_dir / 'progress.txt').open('a', encoding='utf-8') as stream:
                stream.write(datetime.datetime.now().isoformat(timespec='seconds') + ' ' + message + '\n')

    def snapshot(self, name):
        destination = self.run_dir / name; destination.mkdir(exist_ok=True)
        for source in (self.config, self.logs / 'vrserver.txt', self.logs / 'vrmonitor.txt'):
            try: shutil.copy2(source, destination / source.name)
            except OSError: pass

    def wait(self, seconds=1):
        if self.stop.wait(seconds): raise Cancelled()

    def can_restart(self):
        return self.ready_to_restart.is_set() and not self.stop.is_set() and not self.restart_now.is_set()

    def request_restart(self):
        if not self.can_restart(): return False
        self.restart_now.set()
        return True

    def prepare(self):
        if vr_processes():
            raise RuntimeError('SteamVRは既に起動しています。現在のセッションは変更しません。次回、SteamVR終了後に使用してください。')
        if STATE.exists() and json.loads(STATE.read_text(encoding='utf-8')).get('active'):
            restore_pending()
        settings = json.loads(self.config.read_text(encoding='utf-8-sig'))
        original = settings.get(SECTION)
        if original is None or original.get('enable') is False:
            raise RuntimeError('VD純正アドオンが無効、または設定が見つかりません。変更していません。')
        self.run_dir = ROOT / 'runs' / datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
        self.run_dir.mkdir(parents=True)
        self.snapshot('before')
        self.state = dict(active=True, old_present=KEY in original, old_value=original.get(KEY),
                          runtime=str(self.runtime), config=str(self.config), run=str(self.run_dir),
                          restart_requested=False)
        write_state(self.state)
        offline_block(self.config, True, True)
        self.note('準備できました。ヘッドセットのVDから「Launch SteamVR」を押してください。')

    def first_boot(self):
        deadline = time.monotonic() + 600
        pid = None; stable_since = None
        ready_since = None; api = None; last_display = None
        try:
            while time.monotonic() < deadline:
                self.wait(0)
                processes = vr_processes()
                current_pid = processes.get('vrserver.exe')
                if current_pid != pid:
                    self.ready_to_restart.clear(); self.restart_now.clear()
                    if api: api.close(); api = None
                    if pid:
                        self.note('SteamVRの起動処理の切り替わりを待っています。VDのブロックは維持します。')
                    pid = current_pid; ready_since = None; stable_since = None
                session = current_session(self.logs, pid) if pid else ''
                if 'Failed Watchdog' in session or 'Aborting.' in session:
                    raise RuntimeError('最初の起動でエラーを検出しました。')
                if 'Loaded server driver VirtualDesktop (' in session:
                    raise RuntimeError('最初の起動でVDが読み込まれました。予定した順序と異なるため停止します。')
                blocked_in_log = 'Not loading driver VirtualDesktop because it was blocked' in session
                settled = (blocked_in_log and hardware_ready_in_log(session) and
                           'vrmonitor.exe' in processes and 'vrcompositor.exe' in processes)
                if settled:
                    if stable_since is None: stable_since = time.monotonic()
                else:
                    stable_since = None
                    if api: api.close(); api = None
                ready = False
                # No OpenVR initialization during bootstrap, even for status.
                if stable_since is not None and time.monotonic() - stable_since >= SETTLE_SECONDS:
                    try:
                        if api is None: api = NativeAPI(self.runtime, expected_pid=pid)
                        devices = summarize(api.devices())
                        ready = api.blocked() is True and devices['hmd'] and devices['controllers'] >= 2
                    except RuntimeError:
                        if api: api.close(); api = None
                        stable_since = None
                if ready:
                    if ready_since is None: ready_since = time.monotonic()
                    self.ready_to_restart.set()
                    remaining = max(0, WARMUP_SECONDS - int(time.monotonic() - ready_since))
                    display = f'準備できました。「再起動開始」で今すぐ再起動できます。\n押さなければ約{remaining}秒後に自動で再起動します。'
                    manual = self.restart_now.is_set()
                    if manual or remaining == 0:
                        self.ready_to_restart.clear()
                        self.wait(0)
                        if vr_processes().get('vrserver.exe') != pid:
                            continue
                        self.snapshot('stage1-ready')
                        api.unblock()
                        self.state['restart_trigger'] = 'button' if manual else 'timeout'
                        self.state['warmup_seconds'] = round(time.monotonic() - ready_since, 1)
                        self.state['restart_requested'] = True; write_state(self.state)
                        api.close(); api = None
                        self.note('VDを有効にしました。SteamVR標準の再起動を実行します。')
                        # Same URI used by this installed SteamVR dashboard.
                        monitor = self.runtime / 'bin/win64/vrmonitor.exe'
                        subprocess.Popen([str(monitor), 'vrmonitor://restartsystem'],
                                         cwd=str(monitor.parent), creationflags=0x08000000,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        return pid
                else:
                    self.ready_to_restart.clear(); self.restart_now.clear()
                    ready_since = None
                    display = '最初の起動を待っています。ヘッドセットを装着し、左右のコントローラーを使える状態にしてください。'
                if display != last_display:
                    self.note(display); last_display = display
                self.wait()
            raise RuntimeError('VRの準備を確認できず、10分の待ち時間を超えました。')
        finally:
            self.ready_to_restart.clear(); self.restart_now.clear()
            if api: api.close()

    def verify_restart(self, old_pid):
        deadline = time.monotonic() + 150
        new_pid = None; seen_alive_since = None
        required = ['chest', 'left_arm_upper', 'left_arm_lower', 'right_arm_upper', 'right_arm_lower',
                    'HANDL', 'HANDR']
        while time.monotonic() < deadline:
            pid = vr_processes().get('vrserver.exe')
            if pid and pid != old_pid:
                if new_pid and pid != new_pid:
                    raise RuntimeError('2回目の起動中に追加の再起動を検出しました。繰り返し再起動はしません。')
                new_pid = pid
                session = current_session(self.logs, pid)
                if 'Failed Watchdog' in session or 'Aborting.' in session:
                    raise RuntimeError('再起動後にSteamVRのエラーを検出しました。')
                good = ('Loaded server driver VirtualDesktop (' in session and
                        all(f"Driver 'VirtualDesktop' finished adding tracked device with serial number '{name}'" in session
                            for name in required))
                if good:
                    if seen_alive_since is None: seen_alive_since = time.monotonic()
                    if time.monotonic() - seen_alive_since >= 20:
                        with NativeAPI(self.runtime, expected_pid=pid) as api:
                            status = summarize(api.devices())
                        if not (status['hmd'] and status['controllers'] >= 2 and status['native_trackers'] >= 5):
                            self.wait()
                            continue
                        (self.run_dir / 'native-status.json').write_text(json.dumps(status, indent=2), encoding='utf-8')
                        self.snapshot('stage2-result')
                        self.state['active'] = False; write_state(self.state)
                        self.note('起動完了。VD純正の5個のトラッカーと通常のコントローラー2個が有効です。位置とボタン操作を確認してください。')
                        return
            elif new_pid:
                raise RuntimeError('2回目のSteamVRが終了しました。繰り返し再起動はしません。')
            self.wait()
        raise RuntimeError('再起動の完了を確認できませんでした。SteamVRの画面を確認してください。')

    def run(self):
        try:
            self.prepare()
            old_pid = self.first_boot()
            self.verify_restart(old_pid)
        except (Exception, Cancelled) as error:
            if self.run_dir: self.snapshot('stopped')
            if self.state and not self.state.get('restart_requested'):
                if vr_processes():
                    suffix = '\n起動途中でVDを有効にしないため、一時ブロックを残しました。SteamVRを終了してから「設定を戻す」を押してください。'
                else:
                    try:
                        restore_pending()
                        suffix = '\nSteamVRの停止を確認し、一時ブロック設定を開始前に戻しました。'
                    except Exception as restore_error:
                        suffix = '\n一時設定が残っています。SteamVR終了後に「設定を戻す」を使ってください。\n' + str(restore_error)
            else:
                suffix = '\nVDはブロック解除済みです。追加の再起動は行いません。' if self.state else ''
                if self.state:
                    self.state['active'] = False; write_state(self.state)
            self.note(('処理を中断しました。' if isinstance(error, Cancelled) else str(error)) + suffix)
