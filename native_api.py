"""OpenVR background connection and Windows process inventory. No pose writes.

Bindings follow the pinned Valve OpenVR header under reference/ (BSD license).
Only the VirtualDesktop blocked_by_safe_mode key can be changed by this module.
"""
import ctypes as C
from ctypes import wintypes as W
import json
import os
from pathlib import Path

SECTION = b'driver_VirtualDesktop'
KEY = b'blocked_by_safe_mode'
APPLICATION_BACKGROUND = 3


class ProcessEntry(C.Structure):
    _fields_ = [('size', W.DWORD), ('usage', W.DWORD), ('pid', W.DWORD),
                ('heap', C.c_size_t), ('module', W.DWORD), ('threads', W.DWORD),
                ('parent', W.DWORD), ('priority', W.LONG), ('flags', W.DWORD),
                ('exe', W.WCHAR * 260)]


def vr_processes():
    k = C.WinDLL('kernel32', use_last_error=True)
    k.CreateToolhelp32Snapshot.argtypes = [W.DWORD, W.DWORD]
    k.CreateToolhelp32Snapshot.restype = W.HANDLE
    for name in ('Process32FirstW', 'Process32NextW'):
        function = getattr(k, name)
        function.argtypes = [W.HANDLE, C.POINTER(ProcessEntry)]
        function.restype = W.BOOL
    k.CloseHandle.argtypes = [W.HANDLE]
    k.CloseHandle.restype = W.BOOL
    handle = k.CreateToolhelp32Snapshot(2, 0)
    if handle == C.c_void_p(-1).value:
        raise C.WinError(C.get_last_error())
    try:
        entry = ProcessEntry(); entry.size = C.sizeof(entry)
        found = {}
        success = k.Process32FirstW(handle, C.byref(entry))
        while success:
            name = entry.exe.lower()
            if name in ('vrserver.exe', 'vrmonitor.exe', 'vrcompositor.exe'):
                found[name] = entry.pid
            success = k.Process32NextW(handle, C.byref(entry))
        if C.get_last_error() not in (0, 18):
            raise C.WinError(C.get_last_error())
        return found
    finally:
        k.CloseHandle(handle)


def runtime_paths():
    path_file = Path(os.environ['LOCALAPPDATA']) / 'openvr/openvrpaths.vrpath'
    data = json.loads(path_file.read_text(encoding='utf-8-sig'))
    runtime, config = Path(data['runtime'][0]), Path(data['config'][0])
    return runtime, config / 'steamvr.vrsettings', Path(data['log'][0])


class Pose(C.Structure):
    _fields_ = [('matrix', C.c_float * 12), ('velocity', C.c_float * 3),
                ('angular', C.c_float * 3), ('result', C.c_int),
                ('valid', C.c_bool), ('connected', C.c_bool)]


assert C.sizeof(Pose) == 80


class NativeAPI:
    def __init__(self, runtime, expected_pid=None):
        current_pid = vr_processes().get('vrserver.exe')
        if not current_pid or (expected_pid is not None and current_pid != expected_pid):
            raise RuntimeError('SteamVRが起動していません。')
        self.pid = current_pid
        self.closed = True
        self.dll = C.CDLL(str(Path(runtime) / 'bin/win64/openvr_api.dll'))
        self.dll.VR_InitInternal.argtypes = [C.POINTER(C.c_int), C.c_int]
        self.dll.VR_InitInternal.restype = C.c_uint32
        self.dll.VR_GetGenericInterface.argtypes = [C.c_char_p, C.POINTER(C.c_int)]
        self.dll.VR_GetGenericInterface.restype = C.c_void_p
        self.dll.VR_ShutdownInternal.argtypes = []
        self.dll.VR_ShutdownInternal.restype = None
        error = C.c_int()
        # Background explicitly does not start or keep SteamVR running. Utility
        # only skips hardware loading; it CAN start vrserver (observed 23:37).
        self.dll.VR_InitInternal(C.byref(error), APPLICATION_BACKGROUND)
        if error.value:
            raise RuntimeError(f'SteamVRとの接続待ちです（{error.value}）。')
        self.closed = False
        try:
            if vr_processes().get('vrserver.exe') != self.pid:
                raise RuntimeError('接続中にSteamVRの起動状態が変わりました。')
            self.system = self._table(b'FnTable:IVRSystem_026')
            self.settings = self._table(b'FnTable:IVRSettings_003')
        except Exception:
            self.close(); raise

    def _table(self, name):
        error = C.c_int()
        pointer = self.dll.VR_GetGenericInterface(name, C.byref(error))
        if error.value or not pointer:
            raise RuntimeError(f'SteamVRの対応APIを取得できません（{error.value}）。')
        return C.cast(pointer, C.POINTER(C.c_void_p))

    def _function(self, table, index, result, *args):
        if self.closed:
            raise RuntimeError('SteamVR接続は終了しています。')
        return C.CFUNCTYPE(result, *args)(table[index])

    def blocked(self):
        error = C.c_int()
        value = self._function(self.settings, 5, C.c_bool, C.c_char_p, C.c_char_p,
                               C.POINTER(C.c_int))(SECTION, KEY, C.byref(error))
        if error.value == 5:  # UnsetSettingHasNoDefault.
            return None
        if error.value:
            raise RuntimeError(f'VD設定を読み取れません（{error.value}）。')
        return bool(value)

    def restore_block(self, present, value):
        error = C.c_int()
        if present:
            self._function(self.settings, 1, None, C.c_char_p, C.c_char_p, C.c_bool,
                           C.POINTER(C.c_int))(SECTION, KEY, bool(value), C.byref(error))
        else:
            self._function(self.settings, 10, None, C.c_char_p, C.c_char_p,
                           C.POINTER(C.c_int))(SECTION, KEY, C.byref(error))
        if error.value not in (0, 5):
            raise RuntimeError(f'VD設定を戻せません（{error.value}）。')
        actual = self.blocked()
        if (present and actual is not bool(value)) or (not present and actual is True):
            raise RuntimeError('VD設定の反映を確認できませんでした。')

    def unblock(self):
        self.restore_block(False, None)

    def devices(self):
        poses = (Pose * 64)()
        self._function(self.system, 12, None, C.c_int, C.c_float, C.POINTER(Pose),
                       C.c_uint32)(2, 0, poses, 64)
        device_class = self._function(self.system, 20, C.c_int, C.c_uint32)
        prop = self._function(self.system, 28, C.c_uint32, C.c_uint32, C.c_int,
                              C.c_void_p, C.c_uint32, C.POINTER(C.c_int))
        output = []
        for index, pose in enumerate(poses):
            kind = device_class(index)
            if kind not in (1, 2, 3):
                continue
            buffer = C.create_string_buffer(256); error = C.c_int()
            prop(index, 1000, buffer, 256, C.byref(error))
            output.append(dict(index=index, kind=kind, source=buffer.value.decode(errors='replace'),
                               valid=bool(pose.valid), connected=bool(pose.connected)))
        return output

    def close(self):
        if not self.closed:
            self.dll.VR_ShutdownInternal(); self.closed = True

    def __enter__(self): return self
    def __exit__(self, *_): self.close()
