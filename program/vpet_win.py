import ctypes
import datetime
import functools
import json
import math
import os
import random
import re
import subprocess
import sys
import time
import wave
from ctypes import wintypes

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt, QPoint, QRect, QTimer
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPixmap

import majima_db
import notes_store
import pomodoro_store
import runtime_log


# Windows 版本：和 vpet_mac.py 的行为一样，但界面用 Qt (PySide6) 而不是 Cocoa
# 运行：双击 MajimaVpet.cmd，或 program\venv\Scripts\pythonw.exe program\vpet_win.py
# 退出：点击任务栏右下角（通知区域）的 Majima 小图标 → Quit（终端里也可以 Ctrl+C）
#
# 动作分类：
#   steady      （平稳）    idle, walk     —— 鼠标停下不动时：发呆、往任意方向散步
#   interactive （互动反应） shock, confuse —— 被鼠标拖动时：拎起来 "!"，放下后 "?"
#   showtime    （表演时间） change, dance  —— 鼠标在活动时：闪光登场，然后跳舞
#   typing      （打字）    typing         —— 键盘在使用时：头上冒出 "…" 对话框
#   notes       （记笔记）  notetake       —— 双击 Majima：停下准备记笔记；再单击一下弹出输入框
#
# 坐标：Qt 的原点在屏幕左上角、y 向下（macOS 是左下角、y 向上）。pos 是 Majima 窗口的左上角。

from version import APP_VERSION
packaged = bool(getattr(sys, 'frozen', False))  # 打包成独立程序（自带 Python）时为 True
if packaged:  # GIF 和字体放在 exe 旁边（或 PyInstaller 的临时文件夹）
    impath = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(sys.executable))) + '/'
else:         # 从源代码运行：GIF 在项目根目录
    impath = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + '/'
scale = 1  # 放大倍数（像素风格不会模糊）

tick = 0.05             # 主循环间隔（秒）
idle_tick = 0.5         # 暂停或隐藏时的检查间隔（秒），省电
still_after = 1.5       # 鼠标静止多少秒后停止表演、回到平稳
walk_speed = 1          # 每个 tick 走多少像素（任意方向）
walk_move_every = 2     # 走路时每隔几个 tick 才真正移动一次窗口（移动窗口比换帧贵）
walk_loops = (1, 2)     # 每次散步播放几遍走路动画（每遍约 20 像素）
dropped_loops = 2       # 放下后 "?" 动画播放几遍
typing_after = 0.5      # 最后一次按键后多少秒内算"正在打字"（太小会在两次按键之间闪烁）
notetake_timeout = 10.0 # 双击后多少秒内没再单击，就回到平常动作
drag_threshold = 3      # 按住后移动超过几像素才算拖动（否则算点击）
trash_days = 30         # 删除的笔记在"最近删除"里保留几天，之后自动永久删除
# 番茄钟
focus_minutes = 25          # 一个番茄的专注时间
short_break_minutes = 5     # 短休息
long_break_minutes = 30     # 长休息（每轮第 4 个番茄之后，代替短休息）
pomodoros_per_section = 4   # 每轮几个番茄
warning_seconds = 60        # 休息结束前多少秒提醒一次
alarm_repeat_every = 60     # 人不在时，每隔多少秒再响一次
alarm_repeat_max = 10       # 最多再响几次
alarm_walk_speed = 6        # 闹钟时走到屏幕中间的速度（像素 / tick，约 120 像素 / 秒）
alarm_act_seconds = 4       # 在屏幕中间表演几秒
alarm_sounds = {            # Windows 自带的声音（C:\Windows\Media）；sounds/ 文件夹里同名的文件（focus-end.m4a 等）会代替它
    'focus_end': 'Windows Notify Calendar.wav', 'break_end': 'Windows Notify Messaging.wav',
    'warning': 'Windows Ding.wav', 'task_done': 'tada.wav'}
sleep_pause_minutes = 5      # 电脑睡眠短于这个时间：专注暂停、醒来继续；更长：这个番茄算中断
pixel_font_file = 'Silkscreen-Bold.ttf'  # 头顶文字用的像素字体（fonts/Silkscreen-Bold.ttf）
topmost_every = 2.0          # 每隔几秒重新把 Majima 放到最上层（任务栏等也是"最上层"窗口，会盖住他）

# ---------------- Windows 接口 ----------------
user32 = ctypes.WinDLL('user32', use_last_error=True)
kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
winmm = ctypes.WinDLL('winmm')
LRESULT = ctypes.c_ssize_t

class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [('cbSize', wintypes.UINT), ('dwTime', wintypes.DWORD)]

class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [('vkCode', wintypes.DWORD), ('scanCode', wintypes.DWORD), ('flags', wintypes.DWORD),
                ('time', wintypes.DWORD), ('dwExtraInfo', ctypes.c_size_t)]

HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
user32.GetLastInputInfo.argtypes = [ctypes.POINTER(LASTINPUTINFO)]
kernel32.GetTickCount.restype = wintypes.DWORD
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
user32.SetWindowsHookExW.restype = ctypes.c_void_p
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
user32.CallNextHookEx.restype = LRESULT
user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, wintypes.UINT]
winmm.mciSendStringW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.UINT, wintypes.HANDLE]
winmm.mciSendStringW.restype = wintypes.DWORD

def seconds_since_input():
    """距离上一次鼠标 / 键盘操作过了多少秒"""
    info = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
    if not user32.GetLastInputInfo(ctypes.byref(info)):
        return 1e9
    return ((kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000

# 键盘：全局低级键盘钩子只记下"最后一次按键的时间"，不记按了什么键。
# 只按 Shift / Ctrl / Alt / Win 这些修饰键不算打字（和 macOS 版一样）。
modifier_keys = {0x10, 0x11, 0x12, 0x14, 0x5B, 0x5C, 0x90, 0x91, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5}
last_key_down = -1e9

@HOOKPROC
def _keyboard_hook(n, wparam, lparam):
    global last_key_down
    if n == 0 and wparam in (0x0100, 0x0104):  # WM_KEYDOWN / WM_SYSKEYDOWN
        if ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents.vkCode not in modifier_keys:
            last_key_down = time.monotonic()
    return user32.CallNextHookEx(None, n, wparam, lparam)

_hook = user32.SetWindowsHookExW(13, _keyboard_hook, kernel32.GetModuleHandleW(None), 0)  # WH_KEYBOARD_LL
if not _hook:
    print('keyboard hook unavailable, falling back to polling:', ctypes.get_last_error())

def poll_keys():
    """钩子装不上时：每个 tick 看一下有没有键正按着"""
    global last_key_down
    if _hook:
        return
    for vk in range(0x08, 0xFF):
        if vk not in modifier_keys and user32.GetAsyncKeyState(vk) & 0x8000:
            last_key_down = time.monotonic()
            return

def keep_topmost(widget):
    """重新放到最上层（不激活、不移动）"""
    try:
        user32.SetWindowPos(int(widget.winId()), -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)  # HWND_TOPMOST
    except Exception:
        pass

def call_later(seconds, fn, *args):
    QTimer.singleShot(int(seconds * 1000), functools.partial(fn, *args))

# ---------------- 应用 ----------------
QtWidgets.QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
app = QtWidgets.QApplication(sys.argv)
app.setQuitOnLastWindowClosed(False)  # 只有通知区域图标，没有"主窗口"
app.setApplicationName('MajimaVpet')
try:
    app.styleHints().setColorScheme(Qt.ColorScheme.Light)  # 纸张总用浅色，深色模式下也看得清
except AttributeError:
    pass
try:
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('local.orange.majimavpet')
except Exception:
    pass

# ---------------- 图片 ----------------
def rgba_bytes(img):
    img = img.convertToFormat(QImage.Format.Format_RGBA8888)
    return img, bytearray(img.constBits()), img.width(), img.height()

# 有些 GIF 带不透明的纯色背景：从四周边缘开始把同色像素变透明（被线框围住的部分，比如对话框里面，会保留）
def clear_background(img):
    img, buf, w, h = rgba_bytes(img)
    bg = bytes(buf[0:4])
    if bg[3] == 0:
        return img  # 本来就是透明背景
    stack = [i for i in range(w)] + [(h - 1) * w + i for i in range(w)] + \
            [r * w for r in range(h)] + [r * w + w - 1 for r in range(h)]
    seen = set()
    while stack:
        p = stack.pop()
        if p in seen or bytes(buf[p * 4:p * 4 + 4]) != bg:
            continue
        seen.add(p)
        buf[p * 4:p * 4 + 4] = bytes(4)
        x, y = p % w, p // w
        if x > 0: stack.append(p - 1)
        if x < w - 1: stack.append(p + 1)
        if y > 0: stack.append(p - w)
        if y < h - 1: stack.append(p + w)
    return QImage(bytes(buf), w, h, w * 4, QImage.Format.Format_RGBA8888).copy()

# 角色脚下的水平中心（用来在不同宽度的动画之间对齐，避免切换时跳位置）。
# 只看画面里最大的一块连在一起的像素 —— 也就是 Majima 本人，旁边的对话框、金币、时钟等道具不算
def body_center(img):
    img, buf, w, h = rgba_bytes(img)
    opaque = [a > 0 for a in buf[3::4]]
    seen, best = bytearray(w * h), []
    for start in range(w * h):
        if not opaque[start] or seen[start]:
            continue
        blob, stack = [], [start]
        seen[start] = 1
        while stack:
            p = stack.pop()
            blob.append(p)
            x, y = p % w, p // w
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    nx, ny = x + dx, y + dy
                    if 0 <= nx < w and 0 <= ny < h:
                        q = ny * w + nx
                        if opaque[q] and not seen[q]:
                            seen[q] = 1
                            stack.append(q)
        if len(blob) > len(best):
            best = blob
    feet = max(p // w for p in best)
    xs = [p % w for p in best if p // w > feet - 25]  # 只看身体下半部分（头发的形状每帧可能不同）
    return (min(xs) + max(xs)) / 2

# 加载 GIF 的每一帧（QImage）、每帧时长（秒）和角色中心
def load_gif(name):
    reader = QtGui.QImageReader(impath + 'majima-' + name + '.gif')
    frames, delays = [], []
    while reader.canRead():
        img = reader.read()
        if img.isNull():
            break
        delay = reader.nextImageDelay() / 1000
        frames.append(clear_background(img))
        delays.append(delay if delay >= 0.02 else 0.2)
    return frames, delays, body_center(frames[0])

categories = {
    'steady': ['idle', 'walk'],
    'interactive': ['shock', 'confuse'],
    'showtime': ['change', 'dance'],
    'typing': ['typing'],
    'notes': ['notetake'],
    'pomodoro': ['focus-1', 'focus-2', 'focus-3', 'focus-4', 'focus-5', 'focus-end',
                 'break', 'longbreak', 'alarm-work', 'alarm-done'],
}
anims = {name: load_gif(name) for names in categories.values() for name in names
         if os.path.exists(impath + 'majima-' + name + '.gif')}

# 番茄钟的画缺少时用的替代动画（"问是否完成"用 confuse，"跑到中间"用 walk）
art_fallback = {'focus-1': 'typing', 'focus-2': 'typing', 'focus-3': 'typing', 'focus-4': 'typing',
                'focus-5': 'typing', 'focus-end': 'dance', 'break': 'idle', 'longbreak': 'idle',
                'alarm-work': 'shock', 'alarm-done': 'dance', 'check': 'confuse', 'run': 'walk'}

def art(name):
    return name if name in anims else art_fallback.get(name, 'idle')

# ---------------- 状态 ----------------
mode = 'steady'      # steady / showtime / typing / dragging / dropped / notetake（等待单击）/ note（输入框打开）
anim = 'idle'        # 当前动画
frame = 0            # 当前帧
frame_time = 0.0     # 当前帧已显示的时间
loops_left = 1       # 当前动画还要播放几遍
walk_vec = (0, 0)    # 每个 tick 的位移 (dx, dy)
pos = [0.0, 0.0]     # 窗口左上角位置（浮点，斜着走时不会被取整吃掉）
shown_pos = None     # 窗口实际所在的整数像素位置
ticks = 0            # 主循环计数
paused = False
last_mouse = QtGui.QCursor.pos()
last_move = time.monotonic()
last_topmost = 0.0
notetake_since = 0.0       # 进入 notetake 的时间
typing_ignore_until = 0.0  # 刚在输入框里打完字时，暂时不触发打字动画

def play(name, loops=1, vec=(0, 0)):
    global anim, frame, frame_time, loops_left, walk_vec
    old = anim
    anim, frame, frame_time, loops_left, walk_vec = name, 0, 0.0, loops, vec
    resize_for(old, name)
    window.show_frame(anims[anim][0][0])

# 切换到不同尺寸的动画时调整窗口大小，并让角色保持在原地（脚的位置不变）
def resize_for(old, new):
    global width, height
    img = anims[new][0][0]
    w, h = img.width() * scale, img.height() * scale
    if (w, h) == (width, height):
        return
    x = pos[0] + (anims[old][2] - anims[new][2]) * scale
    y = pos[1] + height - h
    width, height = w, h
    window.setFixedSize(w, h)
    move_to(*clamp_origin(x, y, screen_area()))

def keyboard_typing():
    return time.monotonic() - last_key_down < typing_after

def cursor_moving():
    return time.monotonic() - last_move < still_after

def random_direction():
    angle = random.uniform(0, 2 * math.pi)
    return (math.cos(angle) * walk_speed, math.sin(angle) * walk_speed)

# 更新位置；只有落到新的整数像素上时才真正移动窗口（apply=False 时只记录，稍后再移动）
def move_to(x, y, apply=True):
    global shown_pos
    pos[0], pos[1] = x, y
    target = (round(x), round(y))
    if apply and target != shown_pos:
        shown_pos = target
        window.move(*target)
        if 'check_card' in globals():  # 头顶的标签和"任务完成了吗？"气泡和 Majima 同一时刻移动，拖动时不落后
            bubble.follow()
            check_card.follow()

# 当前动画播完后，根据模式决定下一个动画
def next_anim():
    global mode
    if mode == 'steady' and pomo.active:
        mode = 'pomo'
    if mode == 'pomo':
        play(pomo.current_art())
    elif mode == 'alarm':
        play(anim, loops=1000)  # 闹钟期间保持当前动画
    elif mode == 'steady' and paused:
        play('idle')  # 暂停中：站着不动
    elif mode == 'steady':
        if cursor_moving():
            mode = 'showtime'
            play('change')  # 闪光登场
        elif random.random() < 0.5:
            play('idle', loops=random.randint(1, 3))
        else:
            play('walk', loops=random.randint(*walk_loops), vec=random_direction())
    elif mode == 'showtime':
        if cursor_moving():
            play('dance')
        else:
            mode = 'steady'
            play('change')  # 闪光退场，再回到平稳
    elif mode == 'typing':
        if keyboard_typing():
            play('typing')
        else:
            mode = 'steady'
            next_anim()
    elif mode == 'dropped':
        mode = 'steady'
        next_anim()
    elif mode == 'dragging':
        play('shock')
    elif mode == 'notetake':
        play('notetake')
    elif mode == 'note':
        play('typing' if keyboard_typing() else 'notetake')  # 在笔记本里打字时像在"听写"

# 某一点所在屏幕的可用区域（不含任务栏）；默认是 Majima 脚下那一点
def screen_area(point=None):
    if point is None:
        point = QPoint(round(pos[0]) + 1, round(pos[1] + height) - 1)
    screen = QtGui.QGuiApplication.screenAt(point) or QtGui.QGuiApplication.primaryScreen()
    return screen.availableGeometry()

def clamp_origin(x, y, area):
    x = min(max(x, area.x()), area.x() + area.width() - width)
    y = min(max(y, area.y()), area.y() + area.height() - height)
    return x, y

# 暂停时：站着不动（平时的站姿，番茄钟进行中就是番茄钟的画），不走路、不跳舞；
# 但打字、被拖动、双击记笔记时照常有动作，做完再站回去
resting = False

def calm():
    """暂停中、而且现在没有需要动起来的事"""
    if mode in ('steady', 'showtime'):
        return not (keyboard_typing() and time.monotonic() > typing_ignore_until)
    return mode == 'pomo'

def rest():
    global mode
    if mode == 'showtime':
        mode = 'steady'
    still = pomo.current_art() if mode == 'pomo' else 'idle'
    if anim != still or frame != 0:
        play(still)

# 主循环：检测鼠标、推进动画帧、走路
def update():
    global mode, last_mouse, last_move, frame, frame_time, loops_left, walk_vec, ticks, last_topmost
    poll_keys()
    pomo.tick()
    alarm.tick()
    audio.tick()
    global resting
    resting = not window.isVisible() or (paused and calm())
    if window.isVisible() and time.monotonic() - last_topmost > topmost_every:
        last_topmost = time.monotonic()
        keep_topmost(window)
    if resting:
        if paused and window.isVisible():
            rest()  # 暂停：站着不动
        call_later(idle_tick, update)  # 看不到变化 / 暂停时降低频率，几乎不占 CPU
        return
    call_later(tick, update)
    ticks += 1

    if mode == 'notetake' and time.monotonic() - notetake_since > notetake_timeout:
        mode = 'steady'
        next_anim()  # 一直没点：回到平常动作

    if pomo.active and mode in ('steady', 'showtime', 'typing'):
        mode = 'pomo'  # 番茄钟进行中：不跳舞、不打字动画，显示番茄钟的画
        play(pomo.current_art())
    elif mode == 'pomo' and anim != pomo.current_art():
        play(pomo.current_art())  # 每 5 分钟换一张画
    if mode == 'alarm':
        alarm.walk_step()  # 走到屏幕中间 / 走回原位
    elif pomo.stage in ('check', 'interrupted') and check_card.panel is None:
        check_card.show(interrupted=pomo.stage == 'interrupted')  # 等你回答时，问题一定要在屏幕上
    bubble.follow()
    check_card.follow()

    if mode == 'note':  # 输入框打开时：打字就播打字动画，停下就回到记笔记姿势
        typing_now = keyboard_typing()
        if typing_now and anim != 'typing':
            play('typing')
        elif not typing_now and anim == 'typing':
            play('notetake')

    if mode in ('steady', 'showtime') and keyboard_typing() and time.monotonic() > typing_ignore_until:
        mode = 'typing'
        play('typing')  # 键盘在用：立刻切到打字
    elif mode == 'typing' and not keyboard_typing():
        mode = 'steady'
        next_anim()  # 键盘停了：立刻回到平常动作

    mouse = QtGui.QCursor.pos()
    if mouse != last_mouse:
        last_mouse = mouse
        last_move = time.monotonic()
        if mode == 'steady' and anim != 'change' and not paused:
            mode = 'showtime'
            play('change')  # 鼠标动了：立刻闪光登场
    elif mode == 'showtime' and anim == 'dance' and not cursor_moving():
        next_anim()  # 鼠标停了：结束跳舞

    if anim == 'walk' and mode == 'steady' and task_box is None and not paused:  # 输入任务名时不自己走开
        dx, dy = walk_vec
        x, y = clamp_origin(pos[0] + dx, pos[1] + dy, screen_area())
        # 碰到屏幕边缘就反弹
        if x != pos[0] + dx:
            dx = -dx
        if y != pos[1] + dy:
            dy = -dy
        walk_vec = (dx, dy)
        move_to(x, y, apply=ticks % walk_move_every == 0)

    frames, delays, _ = anims[anim]
    frame_time += tick
    if frame_time < delays[frame]:
        return
    frame_time = 0.0
    frame += 1
    if frame >= len(frames):
        frame = 0
        loops_left -= 1
        if loops_left <= 0:
            next_anim()
            return
    window.show_frame(frames[frame])

# 每一步都记下耗时（运行记录，见 runtime_log.py）
_update_step = update

def update():
    t = time.perf_counter()
    try:
        _update_step()
    except Exception as e:
        runtime_log.error(type(e).__name__)
        raise
    finally:
        visible = window.isVisible()
        runtime_log.step(time.perf_counter() - t, idle_tick if resting else tick,
                         mode, anim, visible, paused)

# ---------------- Majima 的窗口：无边框、透明、总在最上层，透明部分点击穿透 ----------------
# 不抢焦点：点他、拖他都不会让你正在打字的窗口失去焦点
class PetWindow(QtWidgets.QWidget):
    def __init__(self):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
                         | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus
                         | Qt.WindowType.NoDropShadowWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setWindowTitle('MajimaVpet')
        self.image = None
        self.clicks = 1

    def show_frame(self, img):
        self.image = img
        self.update()

    def paintEvent(self, event):
        if self.image is None:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)  # 最近邻缩放：像素清晰
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        p.drawImage(self.rect(), self.image)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicks = 1
            self.press()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicks = 2  # 双击：松开时再处理（和 macOS 的 clickCount == 2 一样）
            self.press()

    def press(self):
        global press_point, drag_started
        press_point = QtGui.QCursor.pos()
        drag_started = False

    def mouseMoveEvent(self, event):
        global mode, drag_offset, drag_started
        if not event.buttons() & Qt.MouseButton.LeftButton:
            return
        mouse = QtGui.QCursor.pos()
        if not drag_started:
            if math.hypot(mouse.x() - press_point.x(), mouse.y() - press_point.y()) < drag_threshold:
                return  # 还不算拖动
            drag_started = True
            if mode == 'alarm':
                alarm.dismiss()  # 闹钟表演时被拎起来：和点一下一样，先结束这次提醒
            if mode != 'note':  # 输入框打开时不换成 "!"，保持记笔记的样子
                mode = 'dragging'
                play('shock', loops=1)  # 被拎起来："!"（可能改变窗口大小，所以之后再算偏移）
            drag_offset = (mouse.x() - pos[0], mouse.y() - pos[1])
        x, y = clamp_origin(mouse.x() - drag_offset[0], mouse.y() - drag_offset[1], screen_area(mouse))
        if task_box is not None:
            task_box.move_together(x, y)  # 移动任务名输入页，Majima 一起走
        else:
            move_to(x, y)

    def mouseReleaseEvent(self, event):
        global mode, notetake_since
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if mode == 'note':
            return  # 输入框打开时：拖完保持原样，点击不做任何事
        if mode == 'alarm' and not drag_started:
            alarm.dismiss()  # 闹钟表演时点他一下：提前结束，走回原位
            return
        if drag_started:
            mode = 'dropped'
            play('confuse', loops=dropped_loops)  # 放下后："?"
        elif mode == 'notetake' and time.monotonic() - notetake_since > app.doubleClickInterval() / 1000:
            open_note_box()  # 再单击一下：弹出输入框
        elif self.clicks == 2 and mode in ('steady', 'showtime', 'typing', 'dropped', 'pomo'):
            mode = 'notetake'  # 双击：停在原地，准备记笔记
            notetake_since = time.monotonic()
            play('notetake')

press_point = QtGui.QCursor.pos()
drag_started = False
drag_offset = (0, 0)

# ---------------- 记笔记：输入框和便利贴 ----------------
db = majima_db.open_db()  # 共享数据库 majima.db（笔记本、番茄钟各用自己的表）
notes_store.setup(db)
pomodoro_store.setup(db)
stickies = []         # 屏幕上的便利贴（保持引用，避免被回收）

def make_button(title, rect, slot, parent):
    b = QtWidgets.QPushButton(title, parent)
    b.setGeometry(*rect)
    b.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # 点按钮后继续在纸上打字
    b.clicked.connect(slot)
    return b

def frame_size(panel):
    """面板加上 Windows 标题栏和边框后的大小"""
    panel.winId()  # 先创建原生窗口，才知道边框多大
    m = panel.windowHandle().frameMargins() if panel.windowHandle() else QtCore.QMargins()
    return panel.width() + m.left() + m.right(), panel.height() + m.top() + m.bottom()

# 把面板放在 Majima 头顶（放不下就放到下面），并保持在屏幕内
def place_above_majima(panel):
    fw, fh = frame_size(panel)
    area = screen_area()
    body_x = pos[0] + anims[anim][2] * scale
    x = min(max(body_x - fw / 2, area.x()), area.x() + area.width() - fw)
    y = pos[1] - fh - 4
    if y < area.y():
        y = min(pos[1] + height + 4, area.y() + area.height() - fh)
    panel.move(round(x), round(y))

# 笔记本纸张：浅黄纸 + 蓝色横线 + 红色页边线
paper_color = QColor.fromRgbF(1.0, 0.96, 0.66)
rule_color = QColor.fromRgbF(0.45, 0.62, 0.88, 0.55)
margin_color = QColor.fromRgbF(0.88, 0.3, 0.3, 0.6)
ink_color = QColor.fromRgbF(0.12, 0.12, 0.2)
hint_color = QColor.fromRgbF(0.35, 0.35, 0.35, 0.8)
ui_family = 'Segoe UI'

def ui_font(px, bold=False):
    f = QFont(ui_family)
    f.setPixelSize(px)
    f.setBold(bold)
    return f

note_font = ui_font(15)
line_h = 22              # 横线间距 = 行高
paper_inset = (24, 4)    # 文字离纸边的距离（左边留出页边线）

panel_flags = (Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.CustomizeWindowHint
               | Qt.WindowType.WindowTitleHint | Qt.WindowType.WindowCloseButtonHint)

def setup_panel(panel, title, color=None):
    panel.setWindowTitle(title)
    panel.setAutoFillBackground(True)
    pal = panel.palette()
    pal.setColor(QtGui.QPalette.ColorRole.Window, color or paper_color)
    pal.setColor(QtGui.QPalette.ColorRole.WindowText, ink_color)
    panel.setPalette(pal)

# 画着横线的文字区域（输入框、便利贴和笔记本共用）
class PaperEdit(QtWidgets.QTextEdit):
    def __init__(self, parent=None, color=None, placeholder='', editable=True):
        super().__init__(parent)
        self.color = color or paper_color
        self.on_enter = self.on_escape = None
        self.single_line = False
        self._fixing = False
        self.setAcceptRichText(False)
        self.setFrameStyle(QtWidgets.QFrame.Shape.NoFrame)
        self.setFont(note_font)
        self.setReadOnly(not editable)
        self.placeholder = placeholder  # 空白时的提示文字（自己画，和正文对齐）
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        pal = self.palette()
        pal.setColor(QtGui.QPalette.ColorRole.Base, QColor(0, 0, 0, 0))
        pal.setColor(QtGui.QPalette.ColorRole.Text, ink_color)
        self.setPalette(pal)
        self.viewport().setAutoFillBackground(False)
        self.document().setDocumentMargin(paper_inset[1])  # 上下右留一点；左边再加上页边线的位置（见 fix_line_height）
        self.textChanged.connect(self.fix_line_height)
        self.fix_line_height()

    def fix_line_height(self):
        """每一行都正好 line_h 高，文字就落在横线上，左边让出页边线（粘贴进来的新段落也一样）"""
        if self._fixing:
            return
        self._fixing = True
        try:
            block = self.document().begin()
            while block.isValid():
                if block.blockFormat().lineHeightType() != 2:  # FixedHeight
                    c = QtGui.QTextCursor(block)
                    fmt = QtGui.QTextBlockFormat()
                    fmt.setLineHeight(line_h, 2)
                    fmt.setLeftMargin(paper_inset[0] - paper_inset[1])
                    c.mergeBlockFormat(fmt)
                block = block.next()
        finally:
            self._fixing = False
        self.viewport().update()

    def setText(self, text):
        self.setPlainText(text)
        self.fix_line_height()

    def text(self):
        return self.toPlainText()

    def content_height(self, w):
        doc = self.document().clone()
        doc.setTextWidth(w)
        return doc.size().height()

    def keyPressEvent(self, event):
        key = event.key()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not self.isReadOnly():
            newline = event.modifiers() & (Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.AltModifier)
            if newline and not self.single_line:
                self.textCursor().insertText('\n')  # Shift+Enter / Alt+Enter：换行
            elif self.on_enter:
                self.on_enter()  # Enter：保存 / 开始
            return
        if key == Qt.Key.Key_Escape and self.on_escape:
            self.on_escape()
            return
        super().keyPressEvent(event)

    def paintEvent(self, event):
        p = QPainter(self.viewport())
        r = self.viewport().rect()
        p.fillRect(r, self.color)
        off = self.verticalScrollBar().value()
        p.setPen(QtGui.QPen(rule_color, 1))
        y = paper_inset[1] + line_h - 1 - off  # 横线在文字下方，不穿过 g / y 的下半部分
        while y < r.height():
            if y >= 0:
                p.drawLine(0, y, r.width(), y)
            y += line_h
        p.setPen(QtGui.QPen(margin_color, 1))
        p.drawLine(paper_inset[0] - 8, 0, paper_inset[0] - 8, r.height())
        if self.placeholder and not self.isReadOnly() and self.document().isEmpty():
            p.setFont(note_font)
            p.setPen(QColor(128, 128, 128, 160))
            p.drawText(QRect(paper_inset[0] + 1, paper_inset[1], r.width() - paper_inset[0] - 6, line_h),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self.placeholder)
        p.end()
        super().paintEvent(event)

class NotePanel(QtWidgets.QWidget):
    """笔记页的窗口：Esc 关掉，关掉时通知 NoteBox"""
    def __init__(self, owner):
        super().__init__(None, panel_flags)
        self.owner = owner

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event):
        self.owner.window_closed()
        super().closeEvent(event)

# ---------------- 笔记页：最多同时开 3 页，比如 摘录 / 生词 / 想法 ----------------
# 每页一种颜色，点标题可以改名；回车把这一条存进笔记本（标签 = 页名），页面留着继续写。
# 页面不跟着 Majima 走，可以各自拖到书或网页旁边。
page_slots = [  # 第 1–3 页：默认名字、纸的颜色、提示文字
    ('Notes', paper_color, 'Note from your reading…'),
    ('Words', QColor.fromRgbF(0.80, 0.95, 0.80), 'New word…'),
    ('Ideas', QColor.fromRgbF(1.0, 0.85, 0.88), 'An idea…'),
]
max_pages = len(page_slots)
note_pages = []  # 打开着的笔记页
settings_path = os.path.join(os.path.dirname(majima_db.DB_PATH), 'settings.json')

def load_settings():
    try:
        with open(settings_path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}

def save_setting(key, value):
    data = load_settings()
    data[key] = value
    try:
        with open(settings_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError:
        pass

def page_name(slot):
    names = load_settings().get('page_names', [])
    return names[slot] if slot < len(names) and names[slot] else page_slots[slot][0]

def page_label(name):
    """页名变成笔记的标签：'New words' → new_words"""
    return re.sub(r'\W+', '_', name.strip().lower()).strip('_') or 'notes'

def show_panel(panel, place, activate=True):
    """先放到屏幕外显示（这时才知道标题栏多高），再摆到该在的位置"""
    if not activate:
        panel.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    place()
    panel.show()
    place()
    if activate:
        panel.raise_()
        panel.activateWindow()

class NoteBox:
    def open(self, slot, beside=None):
        self.slot, self.saved, self.done = slot, 0, False
        default, color, placeholder = page_slots[slot]
        self.name = page_name(slot)
        w, paper_h, head_h, foot_h = 300, 4 * line_h + 2 * paper_inset[1], 30, 46
        h = foot_h + paper_h + head_h
        self.panel = NotePanel(self)
        self.panel.setFixedSize(w, h)
        setup_panel(self.panel, self.name, color)
        # 页名：点一下改名，回车确定
        self.title = QtWidgets.QLineEdit(self.name, self.panel)
        self.title.setGeometry(paper_inset[0] - 4, 4, w - 180, 24)
        self.title.setFrame(False)
        self.title.setFont(ui_font(15, bold=True))
        self.title.setStyleSheet('QLineEdit { background: transparent; color: %s; }' % ink_color.name())
        self.title.setToolTip('Click to rename this page')
        self.title.editingFinished.connect(self.rename)
        self.title.returnPressed.connect(lambda: self.paper.setFocus())
        keys = QtWidgets.QLabel('Enter save · Shift+Enter new line', self.panel)
        keys.setGeometry(w - 172, 8, 164, 16)
        keys.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        keys.setFont(ui_font(10))
        keys.setStyleSheet('color: rgba(89, 89, 89, 204);')
        self.paper = PaperEdit(self.panel, color, placeholder)
        self.paper.setGeometry(0, head_h, w, paper_h)
        self.paper.on_enter = self.save
        self.paper.on_escape = self.close
        fy = head_h + paper_h
        self.add_button = make_button('＋ Page', (8, fy + 8, 76, 30), self.add_page, self.panel)
        self.count = QtWidgets.QLabel('', self.panel)
        self.count.setGeometry(90, fy + 15, 60, 16)
        self.count.setFont(ui_font(11))
        self.count.setStyleSheet('color: rgba(89, 89, 89, 204);')
        make_button('Quick', (w - 156, fy + 8, 72, 30), self.quick, self.panel)
        make_button('Save', (w - 82, fy + 8, 72, 30), self.save, self.panel)
        if beside is None:
            show_panel(self.panel, lambda: place_above_majima(self.panel))
        else:
            show_panel(self.panel, lambda: place_beside(self.panel, beside))
        self.paper.setFocus()

    # 改页名：回车或点别处时确定；以后存的笔记用新名字做标签
    def rename(self):
        name = self.title.text().strip()
        if not name:
            self.title.setText(self.name)
            return
        if name == self.name:
            return
        self.name = name
        self.panel.setWindowTitle(name)
        names = load_settings().get('page_names', [])
        names += [''] * (max_pages - len(names))
        names[self.slot] = name
        save_setting('page_names', names)

    def label(self):
        return page_label(self.name)

    def save(self):
        global typing_ignore_until
        if self.done:
            return
        text = self.paper.text()
        if text.strip():
            notes_store.add_note(db, text, kind=self.label())
            notebook.reload()
            self.saved += 1
            self.count.setText(f'{self.saved} saved')
            self.paper.setText('')  # 页面留着，接着写下一条
            typing_ignore_until = time.monotonic() + typing_after + 0.2
            if mode in ('steady', 'showtime', 'typing'):
                play('change')  # 闪光：保存成功
        self.paper.setFocus()

    def quick(self):
        if self.done:
            return
        text = self.paper.text()
        if text.strip():
            sticky = Sticky()
            stickies.append(sticky)
            sticky.open(text, self.panel.frameGeometry(), page_slots[self.slot][1], self.label())
            self.paper.setText('')
        self.paper.setFocus()

    def add_page(self):
        open_note_page(beside=self.panel)

    def close(self):
        self.panel.close()  # 由 window_closed 收尾

    def window_closed(self):
        global typing_ignore_until
        if self.done:
            return
        self.done = True
        if self in note_pages:
            note_pages.remove(self)
        update_page_buttons()
        typing_ignore_until = time.monotonic() + typing_after + 0.2  # 刚才的按键不算"正在打字"

def place_beside(panel, other):
    """新的一页放在已开的页旁边：先试最右边那页的右边，放不下就放最左边那页的左边；顶部和点了"＋ Page"的那页对齐"""
    fw, fh = frame_size(panel)
    o = other.frameGeometry()
    area = screen_area(o.center())
    frames = [p.panel.frameGeometry() for p in note_pages if p.panel is not panel]
    right = max(r.x() + r.width() for r in frames) + 8
    left = min(r.x() for r in frames) - fw - 8
    if right + fw <= area.x() + area.width():
        x = right
    elif left >= area.x():
        x = left
    else:  # 两边都放不下（屏幕太窄）：错开一点叠在上面
        x = o.x() + 30
    x = min(max(x, area.x()), area.x() + area.width() - fw)
    panel.move(x, o.y())

def update_page_buttons():
    """开满 3 页时，"＋ Page" 变灰"""
    for p in note_pages:
        p.add_button.setEnabled(len(note_pages) < max_pages)

def open_note_page(beside=None):
    """打开下一张没开的页（最多 3 页）"""
    used = {p.slot for p in note_pages}
    free = [i for i in range(max_pages) if i not in used]
    if not free:
        return None
    page = NoteBox()
    note_pages.append(page)
    page.open(free[0], beside)
    update_page_buttons()
    return page

def open_note_box():
    """双击 Majima 再点一下：打开第一页；已经开着的话，把它们拿到最前面"""
    global mode
    mode = 'steady'
    next_anim()
    if note_pages:
        for p in note_pages:
            p.panel.raise_()
        note_pages[-1].panel.activateWindow()
        note_pages[-1].paper.setFocus()
        return
    open_note_page()

# 便利贴：和输入框同一张笔记纸，显示 Quick 的内容直到关闭；也可以之后再点 Save 存进笔记本
class StickyPanel(QtWidgets.QWidget):
    def __init__(self, owner):
        super().__init__(None, panel_flags)
        self.owner = owner

    def closeEvent(self, event):
        if self.owner in stickies:
            stickies.remove(self.owner)
        super().closeEvent(event)

class Sticky:
    def open(self, text, box_frame, color=None, label='notes'):
        self.text, self.label = text, label
        w = 300
        self.panel = StickyPanel(self)
        setup_panel(self.panel, 'Quick note', color)
        paper = PaperEdit(self.panel, color, editable=False)
        paper.setText(text)
        paper_h = max(line_h + 2 * paper_inset[1], round(paper.content_height(w - 2)))
        paper_h = min(paper_h, 16 * line_h)
        paper.setGeometry(0, 0, w, paper_h)
        self.panel.setFixedSize(w, paper_h + 46)
        make_button('Save', (w - 82, paper_h + 8, 72, 30), self.save, self.panel)
        show_panel(self.panel, lambda: self.place(box_frame), activate=False)

    def place(self, f):
        """便利贴放在这一页上面（上面放不下就放下面），不挡住正在写的页"""
        sw, sh = frame_size(self.panel)
        area = screen_area(f.center())
        y = f.y() - 8 - sh
        if y < area.y():
            y = min(f.y() + f.height() + 8, area.y() + area.height() - sh)
        self.panel.move(f.x(), y)

    def save(self):
        notes_store.add_note(db, self.text, kind=self.label)
        notebook.reload()
        self.panel.close()

# ---------------- 笔记本窗口：浏览、搜索、按标签筛选、删除已保存的笔记 ----------------
def fmt_time(iso, long=False):
    t = datetime.datetime.fromisoformat(iso)
    return t.strftime('%a %d %b %Y, %H:%M') if long else t.strftime('%d %b, %H:%M')

TRASH = 'Recently Deleted'  # 标签菜单里的最后一项（用户标签都以 # 开头，不会重名）

class Notebook:
    def __init__(self):
        self.window = None
        self.rows = []

    def open(self):
        if self.window is None:
            self.build()
        self.reload()
        if self.window.isMinimized():
            self.window.showNormal()
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()
        self.search.setFocus()

    def build(self):
        W, H = 760, 480
        self.window = w = QtWidgets.QWidget()
        w.setWindowTitle("Majima's Notebook")
        w.setWindowIcon(app_icon)
        w.resize(W, H)
        w.setMinimumSize(560, 340)
        outer = QtWidgets.QVBoxLayout(w)
        outer.setContentsMargins(12, 12, 12, 8)

        # 顶部：搜索框 + 标签筛选
        top = QtWidgets.QHBoxLayout()
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText('Search notes (English or 中文)')
        self.search.setClearButtonEnabled(True)
        self.search.setFixedWidth(280)
        self.search.textChanged.connect(lambda _: self.reload())
        top.addWidget(self.search)
        self.label_menu = QtWidgets.QComboBox()
        self.label_menu.setMinimumWidth(180)
        self.label_menu.currentIndexChanged.connect(lambda _: self.reload())
        top.addWidget(self.label_menu)
        top.addStretch(1)
        outer.addLayout(top)

        # 中间：左边列表，右边笔记内容
        split = QtWidgets.QSplitter(Qt.Orientation.Horizontal)
        self.table = QtWidgets.QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(['Date', 'Note', 'Labels'])
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setWordWrap(False)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Interactive)
        hh.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(0, 105)
        self.table.setColumnWidth(2, 90)
        self.table.verticalHeader().setDefaultSectionSize(24)
        self.table.itemSelectionChanged.connect(self.show_detail)
        split.addWidget(self.table)

        detail = QtWidgets.QWidget()
        dl = QtWidgets.QVBoxLayout(detail)
        dl.setContentsMargins(12, 0, 0, 0)
        self.header = QtWidgets.QLabel('')
        self.header.setStyleSheet('color: #6e6e73;')
        dl.addWidget(self.header)
        self.paper = PaperEdit(editable=False)
        dl.addWidget(self.paper, 1)
        buttons = QtWidgets.QHBoxLayout()
        buttons.addStretch(1)
        self.restore_button = QtWidgets.QPushButton('Restore')
        self.restore_button.clicked.connect(self.restore_note)
        self.forever_button = QtWidgets.QPushButton('Delete Forever')
        self.forever_button.clicked.connect(self.delete_forever)
        self.delete_button = QtWidgets.QPushButton('Delete')
        self.delete_button.clicked.connect(self.delete_note)
        for b in (self.restore_button, self.forever_button, self.delete_button):
            buttons.addWidget(b)
        dl.addLayout(buttons)
        split.addWidget(detail)
        split.setStretchFactor(0, 0)  # 窗口变大时，多出来的宽度给右边的笔记
        split.setStretchFactor(1, 1)
        split.setSizes([400, W - 400])
        outer.addWidget(split, 1)

        # 底部：数量
        bottom = QtWidgets.QHBoxLayout()
        self.count = QtWidgets.QLabel('')
        self.count.setStyleSheet('color: #6e6e73;')
        bottom.addWidget(self.count)
        bottom.addStretch(1)
        self.empty_button = QtWidgets.QPushButton('Empty Recently Deleted')
        self.empty_button.clicked.connect(self.empty_trash)
        bottom.addWidget(self.empty_button)
        outer.addLayout(bottom)
        QtGui.QShortcut(QtGui.QKeySequence.StandardKey.Find, w, activated=self.search.setFocus)
        QtGui.QShortcut(QtGui.QKeySequence('Delete'), self.table,
                        activated=lambda: self.delete_forever() if self.in_trash else self.delete_note())
        self.in_trash = False
        screen = QtGui.QGuiApplication.primaryScreen().availableGeometry()
        w.move(screen.center() - w.rect().center())

    # 重新读取数据（搜索、筛选、保存或删除后）
    def reload(self, keep_id=None):
        if self.window is None:
            return
        menu = self.label_menu
        chosen = menu.currentText()
        menu.blockSignals(True)
        menu.clear()
        menu.addItem('All labels')
        for label in notes_store.all_labels(db):
            menu.addItem('#' + label)
        menu.insertSeparator(menu.count())
        menu.addItem(self.window.style().standardIcon(QtWidgets.QStyle.StandardPixmap.SP_TrashIcon), TRASH)
        i = menu.findText(chosen) if chosen else -1
        menu.setCurrentIndex(i if i >= 0 else 0)
        menu.blockSignals(False)
        label = menu.currentText()
        self.in_trash = label == TRASH
        label = None if label in ('', 'All labels', TRASH) else label[1:]
        self.rows = notes_store.list_notes(db, self.search.text(), label, deleted=self.in_trash)
        self.table.setHorizontalHeaderItem(0, QtWidgets.QTableWidgetItem('Deleted' if self.in_trash else 'Date'))
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.rows))
        for r, (note_id, text, created, labels, deleted_at) in enumerate(self.rows):
            cells = (fmt_time(deleted_at if self.in_trash else created), text.split('\n')[0],
                     ', '.join(l for l in (labels or '').split(',') if l))
            for c, value in enumerate(cells):
                self.table.setItem(r, c, QtWidgets.QTableWidgetItem(value))
        self.table.blockSignals(False)
        total = len(notes_store.list_notes(db, deleted=self.in_trash))
        noun = 'deleted note' if self.in_trash else 'note'
        text = f'{len(self.rows)} of {total} {noun}s' if len(self.rows) != total \
            else f'{total} {noun}' + ('' if total == 1 else 's')
        if self.in_trash:
            text += f' · removed for good after {trash_days} days'
        self.count.setText(text)
        self.delete_button.setVisible(not self.in_trash)
        self.restore_button.setVisible(self.in_trash)
        self.forever_button.setVisible(self.in_trash)
        self.empty_button.setVisible(self.in_trash)
        self.empty_button.setEnabled(total > 0)
        row = next((i for i, r in enumerate(self.rows) if r[0] == keep_id), 0)
        self.table.blockSignals(True)
        if self.rows:
            self.table.selectRow(row)
            self.table.scrollToItem(self.table.item(row, 0))
        else:
            self.table.clearSelection()
        self.table.blockSignals(False)
        self.show_detail()

    def selected_row(self):
        rows = self.table.selectionModel().selectedRows()
        return rows[0].row() if rows else -1

    def show_detail(self):
        row = self.selected_row()
        if 0 <= row < len(self.rows):
            note_id, text, created, labels, deleted_at = self.rows[row]
            tags = '  '.join('#' + l for l in (labels or '').split(',') if l)
            if self.in_trash:
                ago = notes_store.days_in_trash(deleted_at)
                when = 'today' if ago == 0 else ('1 day ago' if ago == 1 else f'{ago} days ago')
                left = max(0, trash_days - ago)
                self.header.setText(f'Deleted {when} · {left} day' + ('' if left == 1 else 's')
                                    + f' left   (written {fmt_time(created)})')
            else:
                self.header.setText(fmt_time(created, long=True) + ('   ' + tags if tags else ''))
            self.paper.setText(text)
            for b in (self.delete_button, self.restore_button, self.forever_button):
                b.setEnabled(True)
        else:
            if self.in_trash:
                empty = 'Recently Deleted is empty.' if not self.search.text() else 'No deleted notes match.'
            elif not self.search.text() and self.label_menu.currentIndex() <= 0:
                empty = 'No notes yet. Double-click Majima to write one.'
            else:
                empty = 'No notes match.'
            self.header.setText(empty)
            self.paper.setText('')
            for b in (self.delete_button, self.restore_button, self.forever_button):
                b.setEnabled(False)

    # 处理选中的笔记，然后选中它下面（或上面）的一条
    def act_on_selected(self, action):
        row = self.selected_row()
        if not 0 <= row < len(self.rows):
            return
        action(self.rows[row][0])
        next_id = self.rows[row + 1][0] if row + 1 < len(self.rows) else (self.rows[row - 1][0] if row else None)
        self.reload(keep_id=next_id)

    def confirm(self, title, info, button):
        box = QtWidgets.QMessageBox(self.window)
        box.setIcon(QtWidgets.QMessageBox.Icon.Warning)
        box.setWindowTitle("Majima's Notebook")
        box.setText(title)
        box.setInformativeText(info)
        ok = box.addButton(button, QtWidgets.QMessageBox.ButtonRole.DestructiveRole)
        box.addButton('Cancel', QtWidgets.QMessageBox.ButtonRole.RejectRole)
        box.exec()
        return box.clickedButton() is ok

    def delete_note(self):  # 放进"最近删除"，不用确认
        self.act_on_selected(lambda i: notes_store.delete_note(db, i))

    def restore_note(self):
        self.act_on_selected(lambda i: notes_store.restore_note(db, i))

    def delete_forever(self):
        row = self.selected_row()
        if 0 <= row < len(self.rows) and self.confirm(
                'Delete this note forever?', self.rows[row][1].split('\n')[0][:80] + '\n\nThis cannot be undone.',
                'Delete Forever'):
            self.act_on_selected(lambda i: notes_store.delete_forever(db, [i]))

    def empty_trash(self):
        n = len(notes_store.list_notes(db, deleted=True))
        if n and self.confirm(f'Delete {n} note' + ('' if n == 1 else 's') + ' forever?',
                              'Everything in Recently Deleted will be erased. This cannot be undone.', 'Empty'):
            notes_store.empty_trash(db)
            self.reload()

notebook = Notebook()

# 每天检查一次：在"最近删除"里放了超过 trash_days 天的笔记永久删除
def purge_trash():
    if notes_store.purge_expired(db, trash_days):
        notebook.reload()
    call_later(24 * 3600, purge_trash)

# ---------------- 番茄钟 ----------------
# 流程：输入任务 → 专注 25 分钟 → 问"任务完成了吗？" → 短休息 5 分钟（×3）/ 第 4 个之后长休息 30 分钟 → 下一个番茄 …
# 每个阶段结束自动进入下一个阶段，并响闹钟（见 Alarm）。

def fmt_slept(seconds):
    """睡了多久：'12 min' / '3 h' """
    return f'{seconds / 3600:.0f} h' if seconds >= 3600 else f'{seconds / 60:.0f} min'

def fmt_left(seconds):
    seconds = max(0, int(seconds + 0.999))
    return f'{seconds // 60}:{seconds % 60:02d}'

class Pomodoro:
    def __init__(self):
        self.active = False
        self.stage = None       # focus / check（等你回答"任务完成了吗？"）/ short_break / long_break
        self.paused_left = None
        self.asleep_at = None   # 电脑开始睡眠的时间（醒来前不计时）
        self.last_tick = time.time()

    def start(self, task_name):
        if self.active:  # 已经有一个任务在进行：不再开第二个
            return
        self.task_id = pomodoro_store.start_task(db, task_name)
        runtime_log.event('task_start')
        self.task_name = task_name.strip()
        self.active, self.section, self.number = True, 1, 0
        self.start_focus()

    def start_focus(self):
        self.number += 1
        self.begin('focus', focus_minutes)

    def begin(self, stage, minutes):
        runtime_log.event('stage', stage=stage, minutes=minutes, section=self.section, number=self.number)
        self.stage, self.stage_start = stage, time.time()
        self.stage_length = minutes * 60
        self.stage_end = self.stage_start + self.stage_length
        self.paused_left, self.warned = None, False

    def left(self):
        return self.paused_left if self.paused_left is not None else self.stage_end - time.time()

    def current_art(self):
        if self.stage == 'focus':  # 专注时每 5 分钟（专注时长的 1/5）换一张：金币越堆越高
            k = min(5, int((self.stage_length - self.left()) / (self.stage_length / 5)) + 1)
            return art(f'focus-{k}')
        if self.stage == 'check':
            return art('focus-end')  # 等你回答时：站在堆满的金币旁边
        if self.stage == 'interrupted':
            return art('check')  # 睡醒后问要不要重新开始：一脸疑惑
        return art('longbreak' if self.stage == 'long_break' else 'break')

    def tick(self):
        now = time.time()
        gap, self.last_tick = now - self.last_tick, now
        if self.asleep_at is not None:
            if gap > 30:
                self.wake()  # 循环停了很久：刚睡醒
            elif now - self.asleep_at > 120:
                self.asleep_at = None  # 说要睡但没睡：照常继续
            return
        if gap > 60:
            self.wake(now - gap)  # 没收到睡眠通知，但循环停了很久：当作睡眠
        if not self.active or self.paused_left is not None or self.stage in ('check', 'interrupted'):
            return  # 等你回答时不计时
        left = self.left()
        if self.stage != 'focus' and not self.warned and left <= warning_seconds < self.stage_length:
            self.warned = True
            alarm.warn()
        warning_due = self.stage != 'focus' and not self.warned and warning_seconds < self.stage_length
        if left <= awake_lead or (warning_due and left - warning_seconds <= awake_lead):
            audio.keep(1)  # 快要响了：先打开声音输出（见 AudioKeeper）
        if left <= 0:
            self.end_stage()

    def end_stage(self):
        if self.stage == 'focus':
            pomodoro_store.add_pomodoro(db, self.task_id, self.stage_start, time.time(), 'completed',
                                        self.section, self.number)
            self.stage = 'check'  # 先问"任务完成了吗？"，回答之前不继续
            alarm.fire('focus_end')  # 先表演（不显示文字），表演完再出现"任务完成了吗？"气泡
        else:
            if self.stage == 'long_break':  # 一轮结束，开始新的一轮
                self.section, self.number = self.section + 1, 0
            check_card.close()
            self.start_focus()
            alarm.fire('break_end')

    def start_break(self):
        """回答"还没完成"之后：开始休息（每轮第 4 个之后是长休息）"""
        if self.stage != 'check':
            return
        long = self.number >= pomodoros_per_section
        self.begin('long_break' if long else 'short_break', long_break_minutes if long else short_break_minutes)
        bubble.show('Long break time!' if long else 'Break time!')

    def sleep(self):
        """电脑要睡眠了：醒来之前不计时、不响铃"""
        self.asleep_at = time.time()
        runtime_log.event('sleep', stage=self.stage)

    def wake(self, slept_from=None):
        """电脑醒了：短睡当作暂停；长睡中断专注；休息照算，结束了就安静地说一句"""
        start = slept_from if slept_from is not None else self.asleep_at
        self.asleep_at, self.last_tick = None, time.time()
        if start is None or not self.active or self.paused_left is not None:
            return
        slept = time.time() - start
        runtime_log.event('wake', slept_s=round(slept), stage=self.stage, notice=slept_from is None)
        if self.stage == 'focus':
            if slept < sleep_pause_minutes * 60:
                self.stage_end += slept  # 只合上一会儿：当作暂停
                return
            pomodoro_store.add_pomodoro(db, self.task_id, self.stage_start, start, 'interrupted',
                                        self.section, self.number)
            self.stage = 'interrupted'
            bubble.show(f'Slept {fmt_slept(slept)}, focus interrupted')
            check_card.show(interrupted=True)
        elif self.stage in ('short_break', 'long_break') and self.left() <= 0:
            if self.stage == 'long_break':  # 睡眠算休息；休息在睡觉时结束了：只响一声，不走到中间，不重复
                self.section, self.number = self.section + 1, 0
            self.start_focus()
            play_sound('break_end')
            bubble.show('Back to work!')

    def restart_focus(self):
        """睡眠中断后重新开始这个番茄"""
        if self.stage != 'interrupted':
            return
        self.number -= 1
        self.start_focus()
        bubble.show(f'Focus {focus_minutes:g} min — go!')

    def toggle_pause(self):
        if self.paused_left is None:
            self.paused_left = self.left()
        else:
            self.stage_end, self.paused_left = time.time() + self.paused_left, None

    def skip_break(self):
        if self.stage in ('short_break', 'long_break'):
            if self.stage == 'long_break':
                self.section, self.number = self.section + 1, 0
            check_card.close()
            self.start_focus()

    def record_partial(self):
        """专注到一半就结束：记为 stopped（不算一个完整的番茄）"""
        if self.stage == 'focus':
            pomodoro_store.add_pomodoro(db, self.task_id, self.stage_start, time.time(), 'stopped',
                                        self.section, self.number)

    def finish(self):
        """任务完成：结束整个番茄钟，并显示总结"""
        if not self.active:
            return
        self.record_partial()
        pomodoro_store.finish_task(db, self.task_id, 'done')
        count, minutes = pomodoro_store.task_summary(db, self.task_id)
        runtime_log.event('task_done', pomodoros=count, minutes=minutes, stage=self.stage)
        self.end_session()
        alarm.fire('task_done', f'Task complete! {count} pomodoro' + ('' if count == 1 else 's') + f' · {minutes} min')

    def stop(self):
        if not self.active:
            return
        self.record_partial()
        pomodoro_store.finish_task(db, self.task_id, 'stopped')
        runtime_log.event('task_stopped', stage=self.stage)
        self.end_session()
        bubble.show('Pomodoro stopped')

    def end_session(self):
        global mode
        self.active, self.stage = False, None
        check_card.close()
        alarm.stop_repeating()
        if mode == 'pomo':
            mode = 'steady'
            next_anim()

    def status(self):
        """菜单里显示的一行状态"""
        name = self.task_name if len(self.task_name) <= 24 else self.task_name[:23] + '…'
        if self.stage == 'check':
            return f'🍅 {name} — waiting: task finished?'
        if self.stage == 'interrupted':
            return f'🍅 {name} — interrupted by sleep'
        if self.stage == 'focus':
            what = f'Focus {self.number}/{pomodoros_per_section}'
        else:
            what = 'Long break' if self.stage == 'long_break' else 'Short break'
        pause = ' (paused)' if self.paused_left is not None else ''
        return f'🍅 {name} — {what} · {fmt_left(self.left())} left{pause}'

pomo = Pomodoro()

# ---------------- 声音（Windows 自带的 MCI，能放 wav / mp3 / m4a / wma）----------------
def mci(command):
    buf = ctypes.create_unicode_buffer(256)
    err = winmm.mciSendStringW(command, buf, 255, None)
    return err, buf.value

class Sound:
    count = 0

    def __init__(self, path, name=None):
        Sound.count += 1
        self.alias, self.path, self.name = f'majima{Sound.count}', path, name
        err, _ = mci(f'open "{path}" type mpegvideo alias {self.alias}')
        self.ok = err == 0
        self.length = 0.0
        if self.ok:
            mci(f'set {self.alias} time format milliseconds')
            err, value = mci(f'status {self.alias} length')
            self.length = int(value) / 1000 if not err and value.isdigit() else 2.0

    def duration(self):
        return self.length

    def play(self, loop=False):
        if self.ok:
            mci(f'play {self.alias} from 0' + (' repeat' if loop else ''))

    def stop(self):
        if self.ok:
            mci(f'stop {self.alias}')

    def close(self):
        if self.ok:
            mci(f'close {self.alias}')
            self.ok = False

system_media = os.path.join(os.environ.get('WINDIR', r'C:\Windows'), 'Media')
sound_dir = (os.path.join(majima_db.DATA_DIR, 'sounds', '') if packaged
             else impath + 'sounds/')  # 自己的闹钟声音（打包后的程序里不能放文件）
sound_files = {'focus_end': 'focus-end', 'break_end': 'break-end', 'warning': 'warning', 'task_done': 'task-done'}
alarm_art = {'focus_end': 'focus-end', 'break_end': 'alarm-work', 'task_done': 'alarm-done'}
alarm_text = {'focus_end': 'Pomodoro done!', 'break_end': 'Back to work!', 'task_done': 'Task complete!'}
muted = False

def play_sound(kind):
    if muted:
        return
    sound = None
    for ext in ('m4a', 'mp3', 'wav', 'aiff', 'wma'):  # sounds/ 文件夹里的同名文件优先
        path = f'{sound_dir}{sound_files[kind]}.{ext}'
        if os.path.exists(path):
            sound = Sound(path)
            break
    if sound is None or not sound.ok:
        path = os.path.join(system_media, alarm_sounds[kind])
        sound = Sound(path, alarm_sounds[kind]) if os.path.exists(path) else None
    if sound is None or not sound.ok:
        app.beep()
        return
    audio.keep(sound.duration() + 2.5)
    runtime_log.event('sound', kind=kind, custom=sound.name is None, output=runtime_log.audio_output(),
                      output_warm_s=round(time.time() - audio.since, 1), waited=not audio.ready())
    if audio.ready():
        start_sound(sound)
    else:  # 输出刚刚打开：等 1 秒再放，声音开头才不会丢
        call_later(1, start_sound, sound)

def start_sound(sound):
    for old in playing_sounds:
        if old is not sound:
            old.close()
    sound.play()
    playing_sounds[:] = [sound]  # 保持引用，下一个声音开始时再关掉

playing_sounds = []

# 蓝牙耳机没有声音时会关掉输出，下一个声音的开头（约 0.3–1 秒）就会丢失，短的声音会完全听不到。
# 所以响铃前先循环放几秒无声的声音，让输出一直开着，声音放完再关。
awake_lead = 3  # 响铃前多少秒开始放无声的声音
silence_path = os.path.join(majima_db.DATA_DIR, 'silence.wav')

def make_silence():
    if os.path.exists(silence_path):
        return
    os.makedirs(majima_db.DATA_DIR, exist_ok=True)
    with wave.open(silence_path, 'wb') as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(22050)
        f.writeframes(bytes(2 * 22050))  # 1 秒静音

class AudioKeeper:
    def __init__(self):
        self.silence = None   # 正在循环播放的无声声音
        self.since = None     # 从什么时候开始放
        self.until = 0        # 放到什么时候

    def keep(self, seconds):
        """让声音输出至少再开 seconds 秒"""
        if muted:
            return
        now = time.time()
        if self.silence is None:
            try:
                make_silence()
            except OSError:
                return
            s = Sound(silence_path)
            if not s.ok:
                return
            s.play(loop=True)
            self.silence, self.since = s, now
        self.until = max(self.until, now + seconds)

    def ready(self):
        """输出已经开了至少 1 秒（蓝牙耳机准备好了）"""
        return self.silence is not None and time.time() - self.since >= 1

    def tick(self):
        if self.silence is not None and time.time() >= self.until:
            self.silence.close()
            self.silence = None
            self.until = 0

audio = AudioKeeper()

# ---------------- 闹钟：声音 + Majima 走到屏幕中间表演 ----------------
class Alarm:
    def __init__(self):
        self.state = None        # None / to_centre / acting / waiting
        self.kind = None
        self.last_ring = None    # 最近一次响的时间（用来判断人在不在、要不要再响）
        self.repeats = 0

    def warn(self):
        """休息快结束：轻轻响一声，原地说一句"""
        runtime_log.event('alarm', kind='warning')
        play_sound('warning')
        n, unit = (warning_seconds // 60, 'minute') if warning_seconds >= 60 else (warning_seconds, 'second')
        bubble.show(f'{n} {unit}' + ('' if n == 1 else 's') + ' left')

    def fire(self, kind, text=None):
        """时间到（专注 / 休息结束）：响铃，Majima 走到屏幕中间表演；任务完成：原地庆祝"""
        self.kind, self.text = kind, text or alarm_text[kind]
        self.last_ring, self.repeats = time.time(), 0
        runtime_log.event('alarm', kind=kind)
        play_sound(kind)
        if kind == 'task_done':
            self.celebrate_here()
        else:
            self.go_to_centre()

    def celebrate_here(self):
        global mode
        if self.can_walk():
            mode = 'alarm'
            self.act()
        else:
            bubble.show(self.text)

    def can_walk(self):
        return window.isVisible() and not paused and mode not in ('note', 'notetake', 'dragging')

    def go_to_centre(self):
        global mode
        if self.state in ('acting', 'waiting') and mode == 'alarm':  # 已经在中间了：直接再表演一次
            self.act()
            return
        self.state = None  # 上一次提醒没正常结束（比如被拖走）：重新开始
        if not self.can_walk():  # 不方便走动（比如正在记笔记）：原地提醒
            if self.kind == 'focus_end':
                self.after_act()
            else:
                bubble.show(self.text)
            return
        mode, self.state = 'alarm', 'to_centre'
        play(art('run'), loops=1000)

    def act(self):
        self.state, self.act_until = 'acting', time.time() + alarm_act_seconds
        play(art(alarm_art[self.kind]), loops=1000)
        if self.kind != 'focus_end':  # 番茄结束只表演，不显示文字
            bubble.show(self.text, alarm_act_seconds + 1)

    def after_act(self):
        """番茄结束的表演做完：出现"任务完成了吗？"气泡"""
        if self.kind == 'focus_end' and pomo.stage == 'check' and check_card.panel is None:
            check_card.show()

    def dismiss(self):
        """点一下 Majima：不表演了，就在当前位置恢复平常的样子"""
        if self.state in ('to_centre', 'acting', 'waiting'):
            self.after_act()
            self.done()

    def done(self):
        """提醒结束：不走回原位，从现在的位置像平常一样继续"""
        global mode
        self.state = None
        mode = 'steady'
        next_anim()

    def stop_repeating(self):
        self.last_ring = None

    def user_back(self):
        """最近一次响铃之后，有没有动过鼠标 / 键盘"""
        return self.last_ring is not None and seconds_since_input() < time.time() - self.last_ring - 0.5

    def tick(self):
        """人不在时每分钟再响一次（最多 alarm_repeat_max 次）；人回来了就不再响"""
        if self.last_ring is None or self.kind == 'task_done':
            return
        if self.user_back():
            runtime_log.event('user_back', kind=self.kind, after_s=round(time.time() - self.last_ring),
                              repeats=self.repeats)
            self.last_ring = None
            if self.state == 'waiting':
                self.done()
        elif self.repeats < alarm_repeat_max and time.time() - self.last_ring >= alarm_repeat_every - awake_lead:
            audio.keep(1)  # 快要再响了：先打开声音输出
            if time.time() - self.last_ring < alarm_repeat_every:
                return
            self.repeats += 1
            runtime_log.event('alarm_repeat', kind=self.kind, n=self.repeats)
            self.last_ring = time.time()
            play_sound(self.kind)
            self.go_to_centre()

    def target(self):
        """屏幕中间（让 Majima 的身体在正中）"""
        area = screen_area()
        x = area.x() + area.width() / 2 - anims[anim][2] * scale
        y = area.y() + (area.height() - height) / 2
        return clamp_origin(x, y, area)

    def step_to(self, tx, ty):
        """朝目标走一步，到了返回 True"""
        dx, dy = tx - pos[0], ty - pos[1]
        dist = math.hypot(dx, dy)
        if dist <= alarm_walk_speed:
            move_to(tx, ty)
            return True
        move_to(pos[0] + dx / dist * alarm_walk_speed, pos[1] + dy / dist * alarm_walk_speed)
        return False

    def walk_step(self):
        if self.state == 'to_centre':
            if self.step_to(*self.target()):
                self.act()
        elif self.state == 'acting' and time.time() >= self.act_until:
            self.after_act()
            if self.kind != 'task_done' and self.last_ring is not None and not self.user_back():
                self.state = 'waiting'  # 人不在：留在中间等，到时间再响
            else:
                self.done()

alarm = Alarm()

# 像素字体：fonts/ 里的字体只给本程序用（不安装到系统）；缺少时用系统粗体
pixel_family = None
_font_id = QtGui.QFontDatabase.addApplicationFont(impath + 'fonts/' + pixel_font_file)
if _font_id >= 0 and QtGui.QFontDatabase.applicationFontFamilies(_font_id):
    pixel_family = QtGui.QFontDatabase.applicationFontFamilies(_font_id)[0]
else:
    print('could not load fonts:', impath + 'fonts/' + pixel_font_file)

def pixel_font(size):
    f = QFont(pixel_family) if pixel_family else ui_font(size, bold=True)
    f.setPixelSize(size)
    f.setStyleStrategy(QFont.StyleStrategy.NoAntialias)  # 像素字不要模糊的边
    return f

overlay_flags = (Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint
                 | Qt.WindowType.WindowDoesNotAcceptFocus | Qt.WindowType.NoDropShadowWindowHint)

# ---------------- 头顶的话：黄色小标签 + 像素字，几秒后消失 ----------------
class BubbleWindow(QtWidgets.QWidget):
    def __init__(self):
        super().__init__(None, overlay_flags | Qt.WindowType.WindowTransparentForInput)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.text = ''

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QtGui.QPen(ink_color, 2))
        p.setBrush(paper_color)
        p.drawRoundedRect(QtCore.QRectF(self.rect()).adjusted(1, 1, -1, -1), 9, 9)  # 黄色圆角标签，深色边框
        p.setFont(pixel_font(13))
        p.setPen(ink_color)
        p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.text)

class Bubble:
    def __init__(self):
        self.panel = None
        self.token = 0

    def show(self, text, seconds=4):
        if self.panel is None:
            self.panel = BubbleWindow()
        self.panel.text = text
        m = QtGui.QFontMetrics(pixel_font(13))
        self.panel.setFixedSize(m.horizontalAdvance(text) + 24, m.height() + 12)
        self.panel.update()
        check_card.suspend()  # 黄色标签和"任务完成了吗？"气泡不同时出现
        self.follow(force=True)
        self.panel.show()
        self.panel.raise_()
        keep_topmost(self.panel)
        self.token += 1
        call_later(seconds, self.hide, self.token)

    def hide(self, token):
        if token == self.token and self.panel is not None:
            self.panel.hide()
            check_card.resume()

    def visible(self):
        return self.panel is not None and self.panel.isVisible()

    def follow(self, force=False):
        """跟着 Majima：放在他头顶"""
        if self.panel is None or not (force or self.panel.isVisible()):
            return
        w, h = self.panel.width(), self.panel.height()
        area = screen_area()
        x = pos[0] + anims[anim][2] * scale - w / 2
        y = pos[1] + height - 90 * scale - h  # 头顶上方，不挡住头发
        x = min(max(x, area.x()), area.x() + area.width() - w)
        y = max(y, area.y())
        self.panel.move(round(x), round(y))

bubble = Bubble()

# ---------------- 番茄结束后 Majima 在头顶的像素对话气泡里问"任务完成了吗？"（休息期间一直可以回答）----------------
bubble_border = 3   # 像素气泡的边框粗细
bubble_tail = 9     # 气泡下面指向 Majima 的小尾巴高度

class SpeechBubbleView(QtWidgets.QWidget):
    """白底、粗黑边、直角的像素风对话框，下面有一个指向 Majima 的小尾巴"""
    def __init__(self):
        super().__init__(None, overlay_flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.tail_x = 30

    def paintEvent(self, event):
        p = QPainter(self)
        w, h, t, k = self.width(), self.height(), bubble_tail, bubble_border
        tx = round(self.tail_x)
        # 先画黑色的外形（框 + 尾巴），再在里面画白色
        p.fillRect(0, 0, w, h - t, ink_color)
        p.fillRect(tx - 6 - k, h - t - k, 12 + 2 * k, t + k, ink_color)
        p.fillRect(k, k, w - 2 * k, h - t - 2 * k, Qt.GlobalColor.white)
        p.fillRect(tx - 6, h - t - 2 * k, 12, t + k, Qt.GlobalColor.white)

def pixel_button(title, rect, slot, parent):
    """像素风按钮：白底、黑框、像素字"""
    b = QtWidgets.QPushButton(title, parent)
    b.setGeometry(*rect)
    b.setFont(pixel_font(11))
    b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    ink = ink_color.name()
    b.setStyleSheet(f'QPushButton {{ background: white; color: {ink}; border: 2px solid {ink}; }}'
                    f'QPushButton:hover {{ background: #fff6a8; }}'
                    f'QPushButton:pressed {{ background: {ink}; color: white; }}')
    b.clicked.connect(slot)
    return b

class CheckCard:
    def __init__(self):
        self.panel = None
        self.suspended = False  # 黄色标签显示时暂时让开，标签消失后再出现

    def suspend(self):
        if self.panel is not None and not self.suspended:
            self.suspended = True
            self.panel.hide()

    def resume(self):
        if self.panel is not None and self.suspended:
            self.suspended = False
            self.follow(force=True)
            self.panel.show()
            keep_topmost(self.panel)

    def show(self, interrupted=False):
        self.close()
        w, h = 196, 74 + bubble_tail
        question, left, right = (('Restart focus?', ('Restart', self.restart, 90), ('Stop', self.stop_task, 66))
                                 if interrupted
                                 else ('Task finished?', ('Done', self.done, 74), ('Not yet', self.keep_going, 86)))
        self.panel = p = SpeechBubbleView()
        p.setFixedSize(w, h)
        q = QtWidgets.QLabel(question, p)
        q.setFont(pixel_font(13))
        q.setStyleSheet(f'color: {ink_color.name()}; background: transparent;')
        q.setGeometry(14, 14, w - 28, 20)
        by = h - bubble_tail - 12 - 26
        pixel_button(left[0], (14, by, left[2], 26), left[1], p)
        pixel_button(right[0], (w - 14 - right[2], by, right[2], 26), right[1], p)
        self.follow(force=True)
        if bubble.visible():
            self.suspended = True  # 黄色标签还在：等它消失再出现
        else:
            p.show()
            keep_topmost(p)

    def close(self):
        if self.panel is not None:
            self.panel.hide()
            self.panel.deleteLater()
            self.panel = None
        self.suspended = False

    def follow(self, force=False):
        """放在 Majima 头顶，尾巴指向他；靠近屏幕边缘时气泡挪进来，尾巴仍对着他"""
        if self.panel is None:
            return
        w, h = self.panel.width(), self.panel.height()
        area = screen_area()
        body_x = pos[0] + anims[anim][2] * scale
        x = min(max(body_x - 30, area.x()), area.x() + area.width() - w)
        y = max(pos[1] + height - 84 * scale - h, area.y())
        tail = min(max(body_x - x, 6 + bubble_border + 4), w - 6 - bubble_border - 4)
        g = self.panel.pos()
        if force or (round(x), round(y)) != (g.x(), g.y()) or tail != self.panel.tail_x:
            self.panel.tail_x = tail
            self.panel.move(round(x), round(y))
            self.panel.update()

    def keep_going(self):
        self.close()
        pomo.start_break()

    def done(self):
        self.close()
        pomo.finish()

    def restart(self):
        self.close()
        pomo.restart_focus()

    def stop_task(self):
        self.close()
        pomo.stop()

check_card = CheckCard()

# ---------------- 开始番茄钟：输入任务名 ----------------
class TaskPanel(QtWidgets.QWidget):
    def __init__(self, owner):
        super().__init__(None, panel_flags)
        self.owner = owner

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.owner.cancel()
        else:
            super().keyPressEvent(event)

    def moveEvent(self, event):  # 输入页被拖动：Majima 跟着走
        super().moveEvent(event)
        self.owner.follow_panel()

    def closeEvent(self, event):
        if not self.owner.closed:
            self.owner.close()
        super().closeEvent(event)

class TaskBox:
    def open(self):
        self.closed, self.moving, self.offset = False, True, None
        w, h = 300, line_h + 2 * paper_inset[1] + 46
        self.panel = TaskPanel(self)
        self.panel.setFixedSize(w, h)
        setup_panel(self.panel, 'New Pomodoro')
        self.paper = PaperEdit(self.panel, placeholder='What will you work on?')
        self.paper.single_line = True
        self.paper.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.paper.setGeometry(0, 0, w, h - 46)
        self.paper.on_enter = self.start  # 任务名只有一行：回车 = 开始
        self.paper.on_escape = self.cancel
        make_button('Cancel', (w - 206, h - 38, 84, 30), self.cancel, self.panel)
        make_button(f'Start {focus_minutes:g} min', (w - 118, h - 38, 106, 30), self.start, self.panel)
        show_panel(self.panel, lambda: place_above_majima(self.panel))
        o = self.panel.pos()
        self.offset = (o.x() - (pos[0] + anims[anim][2] * scale), o.y() - pos[1])  # 相对 Majima 身体的位置
        self.moving = False
        self.paper.setFocus()

    # 拖 Majima → 把输入页移到对应位置
    def move_together(self, x, y):
        self.moving = True
        self.panel.move(round(x + anims[anim][2] * scale + self.offset[0]), round(y + self.offset[1]))
        self.moving = False
        move_to(x, y)

    # 输入页被拖动后，Majima 跟到输入页下面
    def follow_panel(self):
        if self.moving or self.offset is None or self.closed:
            return
        o = self.panel.pos()
        move_to(o.x() - self.offset[0] - anims[anim][2] * scale, o.y() - self.offset[1])

    def start(self):
        name = self.paper.text().strip()
        if not name:
            app.beep()
            return
        self.close()
        pomo.start(name)
        bubble.show(f'Focus {focus_minutes:g} min — go!')

    def cancel(self):
        self.close()

    def close(self):
        global task_box, typing_ignore_until
        if self.closed:
            return
        self.closed = True
        self.panel.hide()
        self.panel.deleteLater()
        move_to(*clamp_origin(pos[0], pos[1], screen_area()))
        task_box = None
        typing_ignore_until = time.monotonic() + typing_after + 0.2

task_box = None

def open_task_box():
    global task_box
    if task_box is None and not pomo.active:
        task_box = TaskBox()
        task_box.open()

# ---------------- 创建 Majima 的窗口 ----------------
idle_frame = anims['idle'][0][0]
width = idle_frame.width() * scale
height = idle_frame.height() * scale

def make_icon(img, size):
    """把 idle 第一帧裁掉透明边、放进正方形，最近邻放大成图标"""
    _, buf, w, h = rgba_bytes(img)
    xs = [i % w for i in range(w * h) if buf[i * 4 + 3]]
    ys = [i // w for i in range(w * h) if buf[i * 4 + 3]]
    crop = img.copy(min(xs), min(ys), max(xs) - min(xs) + 1, max(ys) - min(ys) + 1)
    side = max(crop.width(), crop.height())
    square = QImage(side, side, QImage.Format.Format_RGBA8888)
    square.fill(0)
    p = QPainter(square)
    p.drawImage((side - crop.width()) // 2, side - crop.height(), crop)
    p.end()
    return QPixmap.fromImage(square.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio,
                                           Qt.TransformationMode.FastTransformation))

app_icon = QtGui.QIcon()
for s in (16, 20, 24, 32, 48, 64, 256):
    app_icon.addPixmap(make_icon(idle_frame, s))
app.setWindowIcon(app_icon)

# 初始位置：主屏幕右下角，站在任务栏上方
area = QtGui.QGuiApplication.primaryScreen().availableGeometry()
start_x = area.x() + area.width() - width - 100
start_y = area.y() + area.height() - height

window = PetWindow()
window.setFixedSize(width, height)
pos[0], pos[1] = start_x, start_y
window.move(start_x, start_y)
window.show_frame(idle_frame)
window.show()
move_to(start_x, start_y)

purge_trash()

# ---------------- 通知区域图标的菜单：显示/隐藏、暂停/继续、退出 ----------------
def toggle_visible():
    if window.isVisible():
        window.hide()
        bubble.panel and bubble.panel.hide()
        check_card.suspend()
    else:
        window.show()
        keep_topmost(window)
        check_card.resume()

def toggle_pause():
    global paused
    paused = not paused

def toggle_mute():
    global muted
    muted = not muted

def restart_focus():
    check_card.close()
    pomo.restart_focus()

def screen_sizes():
    return [f'{s.size().width()}x{s.size().height()}@{s.devicePixelRatio():g}x' for s in app.screens()]

def export_runtime():
    path = runtime_log.export(APP_VERSION, screen_sizes())
    subprocess.Popen(['explorer', '/select,', os.path.normpath(path)])
    bubble.show('Saved to Desktop')

def quit_app():
    runtime_log.quit()
    tray.hide()
    app.quit()

tray = QtWidgets.QSystemTrayIcon(app_icon)
tray.setToolTip('MajimaVpet')
menu = QtWidgets.QMenu()

def build_menu():
    menu.clear()
    def item(title, action=None, enabled=True, checked=None):
        it = menu.addAction(title)
        if action:
            it.triggered.connect(action)
        it.setEnabled(enabled)
        if checked is not None:
            it.setCheckable(True)
            it.setChecked(checked)
        return it
    item('MajimaVpet', enabled=False)
    menu.addSeparator()
    if pomo.active:
        item(pomo.status(), enabled=False)
        if pomo.stage == 'interrupted':
            item('Restart Focus', restart_focus)
        elif pomo.stage != 'check':
            item('Resume Pomodoro' if pomo.paused_left is not None else 'Pause Pomodoro', pomo.toggle_pause)
        if pomo.stage in ('short_break', 'long_break'):
            item('Skip Break', pomo.skip_break)
        item('Task Finished ✓', pomo.finish)
        item('Stop Pomodoro', pomo.stop)
    else:
        item('Start Pomodoro…', open_task_box)
    item('Mute Alarm Sounds', toggle_mute, checked=muted)
    menu.addSeparator()
    item('Notebook…', notebook.open)
    menu.addSeparator()
    item('Show Majima' if not window.isVisible() else 'Hide Majima', toggle_visible)
    item('Resume' if paused else 'Pause', toggle_pause)
    menu.addSeparator()
    item('Export Runtime Data…', export_runtime)
    item('Quit MajimaVpet', quit_app)

menu.aboutToShow.connect(build_menu)
build_menu()
tray.setContextMenu(menu)

def tray_clicked(reason):
    if reason == QtWidgets.QSystemTrayIcon.ActivationReason.Trigger:  # 左键单击也弹出菜单（和 macOS 菜单栏一样）
        menu.popup(QtGui.QCursor.pos())
    elif reason == QtWidgets.QSystemTrayIcon.ActivationReason.DoubleClick:
        notebook.open()

tray.activated.connect(tray_clicked)
tray.show()

# ---------------- 电脑睡眠 / 醒来时通知番茄钟 ----------------
class PowerWatcher(QtCore.QAbstractNativeEventFilter):
    def nativeEventFilter(self, event_type, message):
        try:
            if bytes(event_type) == b'windows_generic_MSG':
                msg = wintypes.MSG.from_address(int(message))
                if msg.message == 0x0218:  # WM_POWERBROADCAST
                    if msg.wParam == 0x4:  # PBT_APMSUSPEND
                        pomo.sleep()
                    elif msg.wParam in (0x7, 0x12):  # PBT_APMRESUMESUSPEND / PBT_APMRESUMEAUTOMATIC
                        pomo.wake()
        except Exception as e:
            print('power event:', e)
        return False, 0

power_watcher = PowerWatcher()
app.installNativeEventFilter(power_watcher)

# Ctrl+C 在终端里也能退出
import signal
signal.signal(signal.SIGINT, lambda *_: quit_app())
_sigint_timer = QTimer()
_sigint_timer.timeout.connect(lambda: None)  # 让 Python 定期有机会处理 Ctrl+C
_sigint_timer.start(500)

# 启动循环
runtime_log.start(APP_VERSION, screen_sizes())
audio.keep(1)  # 启动时先把声音系统准备好，之后响铃不卡
play('idle')
call_later(tick, update)
app.aboutToQuit.connect(lambda: _hook and user32.UnhookWindowsHookEx(ctypes.c_void_p(_hook)))
sys.exit(app.exec())
