"""运行记录：在这台电脑（Mac / Windows）上记录 Majima 运行得怎么样，供以后分析；完全离线，从菜单导出成一个 zip 文件手动发送

记在数据文件夹（见 majima_db.DATA_DIR）的 runtime/ 里：
  events-YYYY-MM-DD.csv  事件：启动 / 退出、番茄阶段、闹钟、声音、睡眠 / 醒来、卡顿、错误
  perf-YYYY-MM-DD.csv    每分钟一行：主循环耗时、卡顿次数、CPU、内存、空闲唤醒
  errors.log             程序出错时的报错信息（打包后的 App 才写这里）

不记录笔记内容、任务名、电脑名、用户名、耳机名字。超过 KEEP_DAYS 天的记录自动删除。
"""
import collections
import csv
import ctypes
import datetime
import json
import os
import platform
import subprocess
import sys
import time
import uuid
import zipfile

import majima_db

WINDOWS = sys.platform == 'win32'

DIR = os.path.join(majima_db.DATA_DIR, 'runtime', '')
KEEP_DAYS = 60
FREEZE_MS = 200      # 主循环一步超过这么久：记一次"卡顿"
STALL_MS = 400       # 两步之间比预期多停了这么久（主线程被别的事占住）：记一次"停顿"
EVENTS_PER_MINUTE = 20  # 每分钟最多单独记这么多次卡顿 / 停顿（更多的只算在每分钟的次数里）

session = uuid.uuid4().hex[:8]   # 这次运行的编号（每次启动不同）
_minute = None                   # 正在统计的这一分钟


def _now():
    return datetime.datetime.now().astimezone()


def _write(kind, header, row):
    """追加一行到今天的 CSV（新文件先写表头）"""
    path = f'{DIR}{kind}-{_now():%Y-%m-%d}.csv'
    new = not os.path.exists(path)
    with open(path, 'a', newline='') as f:
        w = csv.writer(f)
        if new:
            w.writerow(header)
        w.writerow(row)


def event(name, **detail):
    """记一个事件；detail 只放数字和简单的词，不放用户写的文字"""
    try:
        _write('events', ['time', 'session', 'event', 'detail'],
               [_now().isoformat(timespec='milliseconds'), session, name,
                json.dumps(detail, ensure_ascii=False) if detail else ''])
    except OSError:
        pass


# ---------------- 系统信息 ----------------
def _sysctl(key):
    if WINDOWS:
        return ''
    try:
        return subprocess.run(['sysctl', '-n', key], capture_output=True, text=True, timeout=2).stdout.strip()
    except Exception:
        return ''


def _windows_memory_gb():
    class Status(ctypes.Structure):
        _fields_ = [('length', ctypes.c_uint32), ('load', ctypes.c_uint32)] +                    [(n, ctypes.c_uint64) for n in ('total', 'avail', 'total_page', 'avail_page',
                                                   'total_virtual', 'avail_virtual', 'avail_ext')]
    st = Status()
    st.length = ctypes.sizeof(Status)
    try:
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
    except Exception:
        return 0
    return round(st.total / 2 ** 30, 1)


def system_info(app_version, screens=()):
    if WINDOWS:
        return {
            'app_version': app_version,
            'packaged': bool(getattr(sys, 'frozen', False)),
            'windows': platform.version(),
            'windows_release': platform.release(),
            'arch': platform.machine(),
            'cpu': platform.processor(),
            'memory_gb': _windows_memory_gb(),
            'python': platform.python_version(),
            'screens': list(screens),
            'audio_output': audio_output(),
        }
    return {
        'app_version': app_version,
        'packaged': bool(getattr(sys, 'frozen', False)),
        'macos': platform.mac_ver()[0],
        'arch': platform.machine(),
        'mac_model': _sysctl('hw.model'),
        'cpu': _sysctl('machdep.cpu.brand_string'),
        'memory_gb': round(int(_sysctl('hw.memsize') or 0) / 2 ** 30, 1),
        'python': platform.python_version(),
        'screens': list(screens),
        'audio_output': audio_output(),
    }


# 声音输出设备的类型（内置扬声器 / 蓝牙耳机 / USB …），不记设备名字（可能包含人名）
_TRANSPORT = {'bltn': 'built-in', 'blue': 'bluetooth', 'blea': 'bluetooth-le', 'usb ': 'usb', 'hdmi': 'hdmi',
              'dprt': 'displayport', 'airp': 'airplay', 'virt': 'virtual', 'thun': 'thunderbolt', 'aggr': 'aggregate'}
try:
    _ca = ctypes.cdll.LoadLibrary('/System/Library/Frameworks/CoreAudio.framework/CoreAudio')

    class _Address(ctypes.Structure):
        _fields_ = [('selector', ctypes.c_uint32), ('scope', ctypes.c_uint32), ('element', ctypes.c_uint32)]
except OSError:
    _ca = None


def _fourcc(s):
    return int.from_bytes(s.encode(), 'big')


def _audio_u32(obj, selector):
    value, size = ctypes.c_uint32(0), ctypes.c_uint32(4)
    addr = _Address(_fourcc(selector), _fourcc('glob'), 0)
    err = _ca.AudioObjectGetPropertyData(obj, ctypes.byref(addr), 0, None, ctypes.byref(size), ctypes.byref(value))
    return None if err else value.value


def audio_output():
    """当前默认声音输出的类型，比如 'bluetooth'"""
    if _ca is None:
        return 'unknown'
    try:
        device = _audio_u32(1, 'dOut')  # 1 = 整个系统
        if not device:
            return 'none'
        t = _audio_u32(device, 'tran')
        code = t.to_bytes(4, 'big').decode('latin-1') if t else ''
        return _TRANSPORT.get(code, code.strip() or 'unknown')
    except Exception:
        return 'unknown'


# ---------------- 内存 / CPU ----------------
class _RusageV2(ctypes.Structure):
    _fields_ = [('uuid', ctypes.c_uint8 * 16)] + [(n, ctypes.c_uint64) for n in (
        'user_time', 'system_time', 'pkg_idle_wkups', 'interrupt_wkups', 'pageins', 'wired_size',
        'resident_size', 'phys_footprint', 'proc_start_abstime', 'proc_exit_abstime', 'child_user_time',
        'child_system_time', 'child_pkg_idle_wkups', 'child_interrupt_wkups', 'child_pageins',
        'child_elapsed_abstime', 'diskio_bytesread', 'diskio_byteswritten')]

try:
    _libproc = ctypes.cdll.LoadLibrary('/usr/lib/libproc.dylib')
except OSError:
    _libproc = None


class _MemCounters(ctypes.Structure):
    _fields_ = [('cb', ctypes.c_uint32), ('page_faults', ctypes.c_uint32)] + [(n, ctypes.c_size_t) for n in (
        'peak_working_set', 'working_set', 'quota_peak_paged', 'quota_paged', 'quota_peak_nonpaged',
        'quota_nonpaged', 'pagefile', 'peak_pagefile', 'private')]


def _rusage_windows():
    """Windows：(私有内存, 工作集, 0)；没有"空闲唤醒"这个数"""
    try:
        c = _MemCounters()
        c.cb = ctypes.sizeof(c)
        k32 = ctypes.windll.kernel32
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        if not ctypes.windll.psapi.GetProcessMemoryInfo(ctypes.c_void_p(k32.GetCurrentProcess()),
                                                        ctypes.byref(c), c.cb):
            return None
        return c.private, c.working_set, 0
    except Exception:
        return None


def _rusage():
    """(物理内存 footprint, 常驻内存, 空闲唤醒次数)；拿不到时返回 None"""
    if WINDOWS:
        return _rusage_windows()
    if _libproc is None:
        return None
    r = _RusageV2()
    if _libproc.proc_pid_rusage(os.getpid(), 2, ctypes.byref(r)) != 0:
        return None
    return r.phys_footprint, r.resident_size, r.pkg_idle_wkups


def _cpu_seconds():
    return time.process_time()  # 本进程用掉的 CPU 时间（用户 + 系统）


# ---------------- 主循环统计 ----------------
class _Minute:
    def __init__(self, now):
        self.start = now
        self.cpu = _cpu_seconds()
        u = _rusage()
        self.wakeups = u[2] if u else 0
        self.steps = 0
        self.total = 0.0
        self.longest = 0.0
        self.gap = 0.0
        self.freezes = 0
        self.stalls = 0
        self.modes = collections.Counter()


_last_step = None


def step(seconds, interval, mode, anim, visible, paused):
    """每次主循环结束时调用：seconds = 这一步用了多久，interval = 预期的下一步间隔"""
    global _minute, _last_step
    now = time.time()
    m = _minute or _Minute(now)
    _minute = m
    if _last_step is not None:
        gap = now - seconds - _last_step[0]   # 这一步开始前停了多久
        extra = gap - _last_step[1]
        m.gap = max(m.gap, gap)
        if 1000 * extra >= STALL_MS and gap < 30:  # 超过 30 秒是睡眠，另外记
            m.stalls += 1
            if m.stalls <= EVENTS_PER_MINUTE:
                event('stall', ms=round(1000 * gap), mode=mode, anim=anim)
    _last_step = (now, interval)
    m.steps += 1
    m.total += seconds
    m.longest = max(m.longest, seconds)
    m.modes[mode] += 1
    if 1000 * seconds >= FREEZE_MS:
        m.freezes += 1
        if m.freezes <= EVENTS_PER_MINUTE:
            event('freeze', ms=round(1000 * seconds), mode=mode, anim=anim)
    if now - m.start >= 60:
        _flush(now, visible, paused)


def _flush(now, visible, paused):
    global _minute
    m = _minute
    elapsed = max(now - m.start, 1e-6)
    u = _rusage()
    footprint, resident, wakeups = (u if u else (0, 0, m.wakeups))
    try:
        _write('perf', ['time', 'session', 'seconds', 'steps', 'step_ms_mean', 'step_ms_max', 'gap_ms_max',
                        'freezes', 'stalls', 'cpu_pct', 'footprint_mb', 'resident_mb', 'idle_wakeups_per_s',
                        'visible', 'paused', 'modes'],
               [_now().isoformat(timespec='seconds'), session, round(elapsed), m.steps,
                round(1000 * m.total / max(m.steps, 1), 2), round(1000 * m.longest, 1), round(1000 * m.gap),
                m.freezes, m.stalls, round(100 * (_cpu_seconds() - m.cpu) / elapsed, 2),
                round(footprint / 2 ** 20, 1), round(resident / 2 ** 20, 1),
                round((wakeups - m.wakeups) / elapsed, 1), int(visible), int(paused),
                ' '.join(f'{k}:{v}' for k, v in m.modes.most_common())])
    except OSError:
        pass
    _minute = _Minute(now)


# ---------------- 启动 / 错误 / 清理 ----------------
def start(app_version, screens=()):
    os.makedirs(DIR, exist_ok=True)
    _purge_old()
    if getattr(sys, 'frozen', False) or sys.stderr is None:  # 打包后的 App / pythonw 没有终端：报错写进 errors.log
        sys.stderr = open(DIR + 'errors.log', 'a', buffering=1)
        sys.stderr.write(f'\n--- {_now().isoformat(timespec="seconds")} session {session} ---\n')
    old_hook = sys.excepthook

    def hook(kind, value, tb):
        event('error', type=kind.__name__)
        old_hook(kind, value, tb)
    sys.excepthook = hook
    event('launch', **system_info(app_version, screens))


def error(kind):
    """PyObjC 回调里的异常不经过 excepthook，由调用方记一下"""
    event('error', type=kind)


def quit():
    if _minute is not None:
        _flush(time.time(), True, False)
    event('quit')


def _purge_old():
    cutoff = time.time() - KEEP_DAYS * 86400
    for name in os.listdir(DIR):
        if name.endswith('.csv') and os.path.getmtime(DIR + name) < cutoff:
            os.remove(DIR + name)


README = """MajimaVpet runtime data
=======================
This zip was made with Majima's menu: Export Runtime Data.
It only contains how Majima ran on this computer. There are no notes, task names, computer name,
user name or device names.

events-*.csv   launch / quit, Pomodoro stages, alarms, sounds (with the type of audio output),
               sleep / wake, freezes and errors
perf-*.csv     one row per minute: loop timing, freezes, CPU %, memory, idle wakeups
errors.log     error messages, if there were any
system.json    OS version, computer model, chip, memory, screen sizes
"""


def desktop_dir():
    """桌面文件夹（Windows 上可能被 OneDrive 挪走，所以问系统）"""
    if WINDOWS:
        try:
            buf = ctypes.create_unicode_buffer(260)
            if ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buf) == 0:  # CSIDL_DESKTOPDIRECTORY
                return os.path.join(buf.value, '')
        except Exception:
            pass
    return os.path.expanduser('~/Desktop/')


def export(app_version, screens=()):
    """把所有记录打包到桌面上的一个 zip，返回它的路径"""
    if _minute is not None:
        _flush(time.time(), True, False)
    event('export')
    desktop = desktop_dir()
    path = f'{desktop}MajimaVpet-runtime-{_now():%Y%m%d-%H%M}.zip'
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as z:
        for name in sorted(os.listdir(DIR)):
            if name.endswith(('.csv', '.log')):
                z.write(DIR + name, name)
        z.writestr('system.json', json.dumps(system_info(app_version, screens), indent=2))
        z.writestr('README.txt', README)
    return path
