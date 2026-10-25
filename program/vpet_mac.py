import ctypes
import datetime
import math
import os
import random
import sys
import time

import AppKit
import objc
import Quartz
from Foundation import NSURL, NSArray, NSNumber
from AppKit import NSBeep
from PyObjCTools import AppHelper

import majima_db
import notes_store
import pomodoro_store
import runtime_log


# macOS 版本：系统自带的 Tk 无法在透明窗口中显示图片，所以这里直接用 Cocoa (PyObjC) 绘制
# 运行：双击 MajimaVpet.app，或 program/venv/bin/python program/vpet_mac.py
# 退出：点击菜单栏的 Majima 小图标 → Quit（终端里也可以 Ctrl+C）
#
# 动作分类：
#   steady      （平稳）    idle, walk     —— 鼠标停下不动时：发呆、往任意方向散步
#   interactive （互动反应） shock, confuse —— 被鼠标拖动时：拎起来 "!"，放下后 "?"
#   showtime    （表演时间） change, dance  —— 鼠标在活动时：闪光登场，然后跳舞
#   typing      （打字）    typing         —— 键盘在使用时：头上冒出 "…" 对话框
#   notes       （记笔记）  notetake       —— 双击 Majima：停下准备记笔记；再单击一下弹出输入框

APP_VERSION = '1.0.0'
packaged = bool(getattr(sys, 'frozen', False))  # 打包成独立 App（自带 Python）时为 True
if packaged:  # GIF 和字体在 App 的 Contents/Resources 里
    impath = os.environ.get('RESOURCEPATH', os.path.dirname(os.path.abspath(__file__))) + '/'
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
alarm_sounds = {            # 系统自带的声音；sounds/ 文件夹里同名的文件（focus-end.m4a 等）会代替它
    'focus_end': 'Glass', 'break_end': 'Hero', 'warning': 'Tink', 'task_done': 'Funk'}
sleep_pause_minutes = 5      # Mac 睡眠短于这个时间：专注暂停、醒来继续；更长：这个番茄算中断
pixel_font_name = 'Silkscreen-Bold'  # 头顶文字用的像素字体（fonts/Silkscreen-Bold.ttf）
fixed_on_desktop_switch = True  # 切换桌面时像鼠标一样保持不动（用 macOS 私有接口；失败时自动退回普通行为）

# ---------------- 切换桌面时保持不动 ----------------
# 普通窗口在切换桌面的动画中都会消失约 1 秒。这里用 SkyLight 私有接口建一个
# 位于所有桌面之上的"空间"，放进去的窗口切换桌面时像鼠标一样留在原地。
_fixed_space = None
if fixed_on_desktop_switch:
    try:
        _sl = ctypes.cdll.LoadLibrary('/System/Library/PrivateFrameworks/SkyLight.framework/SkyLight')
        _sl.SLSMainConnectionID.restype = ctypes.c_int
        _sl.SLSSpaceCreate.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int]
        _sl.SLSSpaceCreate.restype = ctypes.c_uint64
        _sl.SLSSpaceSetAbsoluteLevel.argtypes = [ctypes.c_int, ctypes.c_uint64, ctypes.c_int]
        _sl.SLSShowSpaces.argtypes = [ctypes.c_int, ctypes.c_void_p]
        _sl.SLSSpaceAddWindowsAndRemoveFromSpaces.argtypes = [ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
        _cid = _sl.SLSMainConnectionID()
        _sid = _sl.SLSSpaceCreate(_cid, 1, 0)
        _sl.SLSSpaceSetAbsoluteLevel(_cid, _sid, 100)
        _sl.SLSShowSpaces(_cid, objc.pyobjc_id(NSArray.arrayWithObject_(NSNumber.numberWithUnsignedLongLong_(_sid))))
        _fixed_space = (_sl, _cid, _sid)
    except Exception as e:
        print('fixed desktop space unavailable:', e)

def pin_window(w):
    """把窗口放进固定空间（窗口显示出来之后调用）"""
    if _fixed_space is None:
        return
    sl, cid, sid = _fixed_space
    try:
        ids = NSArray.arrayWithObject_(NSNumber.numberWithInt_(w.windowNumber()))
        sl.SLSSpaceAddWindowsAndRemoveFromSpaces(cid, sid, objc.pyobjc_id(ids), 7)
    except Exception as e:
        print('could not pin window:', e)

# 把图片画进可写的 RGBA 位图，返回 (context, 像素 buffer, 宽, 高)
def bitmap(img):
    w, h = Quartz.CGImageGetWidth(img), Quartz.CGImageGetHeight(img)
    ctx = Quartz.CGBitmapContextCreate(None, w, h, 8, w * 4, Quartz.CGColorSpaceCreateDeviceRGB(),
                                       Quartz.kCGImageAlphaPremultipliedLast)
    Quartz.CGContextDrawImage(ctx, ((0, 0), (w, h)), img)
    return ctx, Quartz.CGBitmapContextGetData(ctx).as_buffer(w * h * 4), w, h

# 有些 GIF 带不透明的纯色背景：从四周边缘开始把同色像素变透明（被线框围住的部分，比如对话框里面，会保留）
def clear_background(img):
    ctx, buf, w, h = bitmap(img)
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
    return Quartz.CGBitmapContextCreateImage(ctx)

# 角色脚下的水平中心（用来在不同宽度的动画之间对齐，避免切换时跳位置）。
# 只看画面里最大的一块连在一起的像素 —— 也就是 Majima 本人，旁边的对话框、金币、时钟等道具不算
def body_center(img):
    ctx, buf, w, h = bitmap(img)
    opaque = [buf[i * 4 + 3] > 0 for i in range(w * h)]
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

# 加载 GIF 的每一帧（CGImage）、每帧时长（秒）和角色中心
def load_gif(name):
    src = Quartz.CGImageSourceCreateWithURL(NSURL.fileURLWithPath_(impath + 'majima-' + name + '.gif'), None)
    frames, delays = [], []
    for i in range(Quartz.CGImageSourceGetCount(src)):
        frames.append(clear_background(Quartz.CGImageSourceCreateImageAtIndex(src, i, None)))
        props = Quartz.CGImageSourceCopyPropertiesAtIndex(src, i, None)
        delays.append(props.get('{GIF}', {}).get('DelayTime') or 0.2)
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
pos = [0.0, 0.0]     # 窗口位置（浮点，斜着走时不会被取整吃掉）
shown_pos = None     # 窗口实际所在的整数像素位置
ticks = 0            # 主循环计数
paused = False
last_mouse = AppKit.NSEvent.mouseLocation()
last_move = time.monotonic()
notetake_since = 0.0       # 进入 notetake 的时间
typing_ignore_until = 0.0  # 刚在输入框里打完字时，暂时不触发打字动画

def play(name, loops=1, vec=(0, 0)):
    global anim, frame, frame_time, loops_left, walk_vec
    old = anim
    anim, frame, frame_time, loops_left, walk_vec = name, 0, 0.0, loops, vec
    resize_for(old, name)
    view.layer().setContents_(anims[anim][0][0])

# 切换到不同尺寸的动画时调整窗口大小，并让角色保持在原地
def resize_for(old, new):
    global width, height
    img = anims[new][0][0]
    w, h = Quartz.CGImageGetWidth(img) * scale, Quartz.CGImageGetHeight(img) * scale
    if (w, h) == (width, height):
        return
    x = pos[0] + (anims[old][2] - anims[new][2]) * scale
    width, height = w, h
    window.setContentSize_((w, h))
    view.setFrame_(((0, 0), (w, h)))
    move_to(*clamp_origin(x, pos[1], screen_area()))

def keyboard_typing():
    return Quartz.CGEventSourceSecondsSinceLastEventType(
        Quartz.kCGEventSourceStateCombinedSessionState, Quartz.kCGEventKeyDown) < typing_after

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
        window.setFrameOrigin_(target)
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

# 当前窗口所在屏幕的可用区域（不含菜单栏和 Dock）
def screen_area(point=None):
    if point is None:
        point = window.frame().origin
    for screen in AppKit.NSScreen.screens():
        if AppKit.NSPointInRect(point, screen.frame()):
            return screen.visibleFrame()
    return AppKit.NSScreen.mainScreen().visibleFrame()

def clamp_origin(x, y, area):
    x = min(max(x, area.origin.x), area.origin.x + area.size.width - width)
    y = min(max(y, area.origin.y), area.origin.y + area.size.height - height)
    return x, y

# 主循环：检测鼠标、推进动画帧、走路
def update():
    global mode, last_mouse, last_move, frame, frame_time, loops_left, walk_vec, ticks
    pomo.tick()
    alarm.tick()
    audio.tick()
    if paused or not window.isVisible():
        AppHelper.callLater(idle_tick, update)  # 看不到变化时降低频率，几乎不占 CPU
        return
    AppHelper.callLater(tick, update)
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

    if mode == 'note' and note_box is not None:
        note_box.follow_panel()  # 笔记本被拖走时，Majima 跟上
    if task_box is not None:
        task_box.follow_panel()  # 任务名输入页被拖走时，Majima 跟上

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

    mouse = AppKit.NSEvent.mouseLocation()
    if mouse.x != last_mouse.x or mouse.y != last_mouse.y:
        last_mouse = mouse
        last_move = time.monotonic()
        if mode == 'steady' and anim != 'change':
            mode = 'showtime'
            play('change')  # 鼠标动了：立刻闪光登场
    elif mode == 'showtime' and anim == 'dance' and not cursor_moving():
        next_anim()  # 鼠标停了：结束跳舞

    if anim == 'walk' and mode == 'steady' and task_box is None:  # 输入任务名时不自己走开
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
    view.layer().setContents_(frames[frame])

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
        runtime_log.step(time.perf_counter() - t, idle_tick if paused or not visible else tick,
                         mode, anim, visible, paused)

# 可拖动、可双击的桌宠视图
class PetView(AppKit.NSView):
    def acceptsFirstMouse_(self, event):
        return True

    def mouseDown_(self, event):
        global press_point, drag_started
        press_point = AppKit.NSEvent.mouseLocation()
        drag_started = False

    def mouseDragged_(self, event):
        global mode, drag_offset, drag_started
        mouse = AppKit.NSEvent.mouseLocation()
        if not drag_started:
            if math.hypot(mouse.x - press_point.x, mouse.y - press_point.y) < drag_threshold:
                return  # 还不算拖动
            drag_started = True
            if mode == 'alarm':
                alarm.dismiss()  # 闹钟表演时被拎起来：和点一下一样，先结束这次提醒
            if mode != 'note':  # 输入框打开时不换成 "!"，保持记笔记的样子
                mode = 'dragging'
                play('shock', loops=1)  # 被拎起来："!"（可能改变窗口大小，所以之后再算偏移）
            drag_offset = (mouse.x - pos[0], mouse.y - pos[1])
        x, y = clamp_origin(mouse.x - drag_offset[0], mouse.y - drag_offset[1], screen_area(mouse))
        if mode == 'note' and note_box is not None:
            note_box.move_together(x, y)  # 移动笔记本，Majima 作为子窗口一起走
        elif task_box is not None:
            task_box.move_together(x, y)  # 移动任务名输入页，Majima 一起走
        else:
            move_to(x, y)

    def mouseUp_(self, event):
        global mode, notetake_since
        if mode == 'note':
            return  # 输入框打开时：拖完保持原样，点击不做任何事
        if mode == 'alarm' and not drag_started:
            alarm.dismiss()  # 闹钟表演时点他一下：提前结束，走回原位
            return
        if drag_started:
            mode = 'dropped'
            play('confuse', loops=dropped_loops)  # 放下后："?"
        elif mode == 'notetake' and time.monotonic() - notetake_since > AppKit.NSEvent.doubleClickInterval():
            open_note_box()  # 再单击一下：弹出输入框
        elif event.clickCount() == 2 and mode in ('steady', 'showtime', 'typing', 'dropped', 'pomo'):
            mode = 'notetake'  # 双击：停在原地，准备记笔记
            notetake_since = time.monotonic()
            play('notetake')

press_point = AppKit.NSEvent.mouseLocation()
drag_started = False
drag_offset = (0, 0)

# ---------------- 记笔记：输入框和便利贴 ----------------
db = majima_db.open_db()  # 共享数据库 majima.db（笔记本、之后的番茄钟各用自己的表）
notes_store.setup(db)
pomodoro_store.setup(db)
note_box = None       # 当前打开的输入框控制器
stickies = []         # 屏幕上的便利贴（保持引用，避免被回收）

def make_button(title, frame, target, action):
    b = AppKit.NSButton.alloc().initWithFrame_(frame)
    b.setTitle_(title)
    b.setBezelStyle_(AppKit.NSBezelStyleRounded)
    b.setTarget_(target)
    b.setAction_(action)
    return b

# 把面板放在 Majima 头顶（放不下就放到下面），并保持在屏幕内
def place_above_majima(panel):
    f = panel.frame()
    area = screen_area()
    body_x = pos[0] + anims[anim][2] * scale
    x = min(max(body_x - f.size.width / 2, area.origin.x), area.origin.x + area.size.width - f.size.width)
    y = pos[1] + height + 4
    if y + f.size.height > area.origin.y + area.size.height:
        y = max(pos[1] - f.size.height - 4, area.origin.y)
    panel.setFrameOrigin_((x, y))

# 笔记本纸张：浅黄纸 + 蓝色横线 + 红色页边线
paper_color = AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(1.0, 0.96, 0.66, 1.0)
rule_color = AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(0.45, 0.62, 0.88, 0.55)
margin_color = AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(0.88, 0.3, 0.3, 0.6)
ink_color = AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(0.12, 0.12, 0.2, 1.0)
note_font = AppKit.NSFont.systemFontOfSize_(15)
line_h = 22              # 横线间距 = 行高
paper_inset = (24, 4)    # 文字离纸边的距离（左边留出页边线）

def setup_panel(panel, title):
    panel.setTitle_(title)
    panel.setLevel_(AppKit.NSStatusWindowLevel)
    panel.setHidesOnDeactivate_(False)
    panel.setReleasedWhenClosed_(False)
    panel.setCollectionBehavior_(AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
                                 | AppKit.NSWindowCollectionBehaviorStationary
                                 | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
                                 | AppKit.NSWindowCollectionBehaviorIgnoresCycle)
    panel.setAppearance_(AppKit.NSAppearance.appearanceNamed_(AppKit.NSAppearanceNameAqua))  # 纸张总用浅色，深色模式下也看得清
    panel.setBackgroundColor_(paper_color)

paper_placeholders = {}  # 每张纸自己的提示文字（默认是记笔记的提示）

# 画着横线的文字区域（输入框和便利贴共用）
class PaperTextView(AppKit.NSTextView):
    def drawViewBackgroundInRect_(self, rect):
        paper_color.setFill()
        AppKit.NSRectFill(rect)
        b = self.bounds()
        rule_color.setStroke()
        y = paper_inset[1] + line_h - 0.5  # 横线在文字下方，不穿过 g / y 的下半部分
        while y < b.size.height:
            path = AppKit.NSBezierPath.bezierPath()
            path.moveToPoint_((0, y))
            path.lineToPoint_((b.size.width, y))
            path.setLineWidth_(1)
            path.stroke()
            y += line_h
        margin_color.setStroke()
        path = AppKit.NSBezierPath.bezierPath()
        path.moveToPoint_((paper_inset[0] - 7.5, 0))
        path.lineToPoint_((paper_inset[0] - 7.5, b.size.height))
        path.stroke()
        if self.isEditable() and not self.string():  # 空白时显示提示文字
            AppKit.NSAttributedString.alloc().initWithString_attributes_(
                paper_placeholders.get(objc.pyobjc_id(self), 'Word or note… (#label optional)'),
                {AppKit.NSFontAttributeName: note_font,
                 AppKit.NSForegroundColorAttributeName: AppKit.NSColor.colorWithWhite_alpha_(0.5, 0.6)},
            ).drawAtPoint_((paper_inset[0] + 5, paper_inset[1] + 2))

paper_style = AppKit.NSMutableParagraphStyle.alloc().init()
paper_style.setMinimumLineHeight_(line_h)
paper_style.setMaximumLineHeight_(line_h)
paper_attrs = {AppKit.NSFontAttributeName: note_font, AppKit.NSParagraphStyleAttributeName: paper_style,
               AppKit.NSForegroundColorAttributeName: ink_color}

def paper_string(text):
    return AppKit.NSAttributedString.alloc().initWithString_attributes_(text, paper_attrs)

def make_paper(frame, text, editable):
    tv = PaperTextView.alloc().initWithFrame_(frame)
    tv.setRichText_(False)
    tv.setTextContainerInset_(paper_inset)
    tv.setDefaultParagraphStyle_(paper_style)
    tv.setTypingAttributes_(paper_attrs)
    tv.textStorage().setAttributedString_(paper_string(text))
    tv.setInsertionPointColor_(ink_color)
    tv.setEditable_(editable)
    tv.setSelectable_(True)
    tv.setVerticallyResizable_(True)
    tv.setHorizontallyResizable_(False)
    tv.setAutoresizingMask_(AppKit.NSViewWidthSizable)
    tv.setMinSize_(frame[1])
    tv.setMaxSize_((frame[1][0], 1e7))
    tv.textContainer().setWidthTracksTextView_(True)
    return tv

class NotePanel(AppKit.NSPanel):
    def canBecomeKeyWindow(self):
        return True

    def cancelOperation_(self, sender):  # Esc：丢弃
        if note_box is not None:
            note_box.discard_(None)

# 输入框：一页笔记纸。回车 = Save 保存到笔记本，⌥回车 = 换行，Quick 变成便利贴，Esc 丢弃
class NoteBox(AppKit.NSObject):
    @objc.python_method
    def open(self):
        w, paper_h = 300, 3 * line_h + 2 * paper_inset[1]
        h = paper_h + 46
        self.done = False
        self.panel = NotePanel.alloc().initWithContentRect_styleMask_backing_defer_(
            ((0, 0), (w, h)), AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskUtilityWindow
            | AppKit.NSWindowStyleMaskNonactivatingPanel,  # 能打字，但不把本 App 切到前台
            AppKit.NSBackingStoreBuffered, False)
        setup_panel(self.panel, "Majima's notebook")
        content = self.panel.contentView()
        scroll = AppKit.NSScrollView.alloc().initWithFrame_(((0, 46), (w, paper_h)))
        scroll.setHasVerticalScroller_(True)
        scroll.setAutohidesScrollers_(True)
        scroll.setDrawsBackground_(False)
        self.paper = make_paper(((0, 0), scroll.contentSize()), '', editable=True)
        self.paper.setDelegate_(self)
        scroll.setDocumentView_(self.paper)
        content.addSubview_(scroll)
        hint = AppKit.NSTextField.labelWithString_('Esc discard · ⌥↩ new line')
        hint.setFrame_(((10, 14), (136, 16)))
        hint.setTextColor_(AppKit.NSColor.colorWithWhite_alpha_(0.35, 0.8))
        hint.setFont_(AppKit.NSFont.systemFontOfSize_(10))
        content.addSubview_(hint)
        content.addSubview_(make_button('Quick', ((w - 156, 8), (72, 30)), self, 'quick:'))
        save = make_button('Save', ((w - 82, 8), (72, 30)), self, 'save:')
        save.setKeyEquivalent_('\r')
        content.addSubview_(save)
        place_above_majima(self.panel)
        o = self.panel.frame().origin
        self.offset = (o.x - pos[0], o.y - pos[1])  # 笔记本相对 Majima 的位置，拖动时保持不变
        self.panel.setDelegate_(self)
        self.panel.makeKeyAndOrderFront_(None)
        pin_window(self.panel)
        self.panel.addChildWindow_ordered_(window, AppKit.NSWindowAbove)  # Majima 跟着笔记本移动
        self.panel.makeFirstResponder_(self.paper)

    # Majima 是笔记本的子窗口：系统会让他们一起移动，拖笔记本标题栏时也没有延迟。
    # 拖 Majima 时，把笔记本移到对应位置，Majima 自然跟着走
    @objc.python_method
    def move_together(self, x, y):
        if self.done:
            return
        self.panel.setFrameOrigin_((x + self.offset[0], y + self.offset[1]))
        self.follow_panel()

    # 根据 Majima 窗口的实际位置更新 pos（笔记本被拖动后）
    @objc.python_method
    def follow_panel(self):
        global shown_pos
        if self.done:
            return
        f = window.frame().origin
        if (round(f.x), round(f.y)) != shown_pos:
            pos[0], pos[1] = f.x, f.y
            shown_pos = (round(f.x), round(f.y))

    def windowDidMove_(self, notification):
        self.follow_panel()

    # 回车保存、⌥回车换行、Esc 丢弃
    def textView_doCommandBySelector_(self, text_view, selector):
        selector = selector.decode() if isinstance(selector, bytes) else str(selector)
        if selector == 'insertNewline:':
            self.save_(None)
            return True
        if selector in ('cancelOperation:', 'complete:'):
            self.discard_(None)
            return True
        return False

    def textDidChange_(self, notification):
        self.paper.setNeedsDisplay_(True)  # 让提示文字及时消失/出现

    def save_(self, sender):
        if self.done:
            return
        text = self.paper.string()
        if text.strip():
            notes_store.add_note(db, text, kind='word')
            notebook.reload()
            self.finish(saved=True)
        else:
            self.finish()

    def quick_(self, sender):
        if self.done:
            return
        text = self.paper.string()
        if text.strip():
            sticky = Sticky.alloc().init()
            sticky.open(text, self.panel.frame())
            stickies.append(sticky)
        self.finish()

    def discard_(self, sender):
        self.finish()

    @objc.python_method
    def finish(self, saved=False):
        global mode, note_box, typing_ignore_until
        if self.done:
            return
        self.follow_panel()
        self.done = True
        self.panel.removeChildWindow_(window)
        self.panel.orderOut_(None)
        move_to(*clamp_origin(pos[0], pos[1], screen_area()))
        note_box = None
        typing_ignore_until = time.monotonic() + typing_after + 0.2  # 刚才的按键不算"正在打字"
        mode = 'steady'
        if saved:
            play('change')  # 闪光：保存成功
        else:
            next_anim()

def open_note_box():
    global mode, note_box
    mode = 'note'
    play('notetake')
    note_box = NoteBox.alloc().init()
    note_box.open()

# 便利贴：和输入框同一张笔记纸，显示 Quick 的内容直到关闭；也可以之后再点 Save 存进笔记本
class Sticky(AppKit.NSObject):
    @objc.python_method
    def open(self, text, box_frame):
        self.text = text
        w = 300
        paper = make_paper(((0, 0), (w, line_h)), text, editable=False)
        paper.layoutManager().ensureLayoutForTextContainer_(paper.textContainer())
        used = paper.layoutManager().usedRectForTextContainer_(paper.textContainer())
        paper_h = max(line_h, used.size.height) + 2 * paper_inset[1]
        h = paper_h + 46
        self.panel = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            ((0, 0), (w, h)),
            AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskClosable
            | AppKit.NSWindowStyleMaskUtilityWindow | AppKit.NSWindowStyleMaskNonactivatingPanel,
            AppKit.NSBackingStoreBuffered, False)
        setup_panel(self.panel, 'Quick note')
        self.panel.setDelegate_(self)
        content = self.panel.contentView()
        paper.setFrame_(((0, 46), (w, paper_h)))
        content.addSubview_(paper)
        content.addSubview_(make_button('Save', ((w - 82, 8), (72, 30)), self, 'save:'))
        # 和输入框顶部对齐，放在原来的位置
        top = box_frame.origin.y + box_frame.size.height
        self.panel.setFrameTopLeftPoint_((box_frame.origin.x, top))
        self.panel.orderFrontRegardless()
        pin_window(self.panel)

    def save_(self, sender):
        notes_store.add_note(db, self.text, kind='word')
        notebook.reload()
        self.panel.close()

    def windowWillClose_(self, notification):
        if self in stickies:
            stickies.remove(self)

# ---------------- 笔记本窗口：浏览、搜索、按标签筛选、删除已保存的笔记 ----------------
def fmt_time(iso, long=False):
    t = datetime.datetime.fromisoformat(iso)
    return t.strftime('%a %d %b %Y, %H:%M') if long else t.strftime('%d %b, %H:%M')

def label_view(text, frame, size=12, color=None):
    v = AppKit.NSTextField.labelWithString_(text)
    v.setFrame_(frame)
    v.setFont_(AppKit.NSFont.systemFontOfSize_(size))
    if color is not None:
        v.setTextColor_(color)
    return v

TRASH = 'Recently Deleted'  # 标签菜单里的最后一项（用户标签都以 # 开头，不会重名）

class Notebook(AppKit.NSObject):
    def init(self):
        self = objc.super(Notebook, self).init()
        self.window = None
        self.rows = []
        return self

    @objc.python_method
    def open(self):
        if self.window is None:
            self.build()
        self.reload()
        app.activateIgnoringOtherApps_(True)
        self.window.makeKeyAndOrderFront_(None)
        self.window.makeFirstResponder_(self.search)

    @objc.python_method
    def build(self):
        W, H = 760, 480
        R = AppKit  # 简写
        self.window = R.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            ((0, 0), (W, H)),
            R.NSWindowStyleMaskTitled | R.NSWindowStyleMaskClosable | R.NSWindowStyleMaskResizable
            | R.NSWindowStyleMaskMiniaturizable, R.NSBackingStoreBuffered, False)
        self.window.setTitle_("Majima's Notebook")
        self.window.setReleasedWhenClosed_(False)
        self.window.setMinSize_((560, 340))
        self.window.setCollectionBehavior_(R.NSWindowCollectionBehaviorMoveToActiveSpace)
        self.window.center()
        content = self.window.contentView()
        top = R.NSViewMinYMargin  # 贴着顶部

        # 顶部：搜索框 + 标签筛选
        self.search = R.NSSearchField.alloc().initWithFrame_(((12, H - 40), (280, 26)))
        self.search.setPlaceholderString_('Search notes (English or 中文)')
        self.search.setSendsSearchStringImmediately_(True)
        self.search.setTarget_(self)
        self.search.setAction_('filter:')
        self.search.setAutoresizingMask_(top)
        content.addSubview_(self.search)
        self.label_menu = R.NSPopUpButton.alloc().initWithFrame_pullsDown_(((302, H - 41), (180, 28)), False)
        self.label_menu.setTarget_(self)
        self.label_menu.setAction_('filter:')
        self.label_menu.setAutoresizingMask_(top)
        content.addSubview_(self.label_menu)

        # 中间：左边列表，右边笔记内容
        split = R.NSSplitView.alloc().initWithFrame_(((0, 30), (W, H - 80)))
        split.setVertical_(True)
        split.setDividerStyle_(R.NSSplitViewDividerStyleThin)
        split.setAutoresizingMask_(R.NSViewWidthSizable | R.NSViewHeightSizable)

        list_scroll = R.NSScrollView.alloc().initWithFrame_(((0, 0), (400, H - 80)))
        list_scroll.setHasVerticalScroller_(True)
        self.table = R.NSTableView.alloc().initWithFrame_(((0, 0), (400, H - 80)))
        for ident, title, width in [('date', 'Date', 95), ('note', 'Note', 150), ('labels', 'Labels', 90)]:
            col = R.NSTableColumn.alloc().initWithIdentifier_(ident)
            col.setTitle_(title)
            col.setWidth_(width)
            col.setEditable_(False)
            self.table.addTableColumn_(col)
        self.table.setUsesAlternatingRowBackgroundColors_(True)
        self.table.setColumnAutoresizingStyle_(R.NSTableViewUniformColumnAutoresizingStyle)
        self.table.setDataSource_(self)
        self.table.setDelegate_(self)
        list_scroll.setDocumentView_(self.table)
        split.addSubview_(list_scroll)

        DW, DH = W - 401, H - 80
        detail = R.NSView.alloc().initWithFrame_(((0, 0), (DW, DH)))
        self.header = label_view('', ((12, DH - 28), (DW - 24, 18)), 12, R.NSColor.secondaryLabelColor())
        self.header.setAutoresizingMask_(R.NSViewWidthSizable | top)
        detail.addSubview_(self.header)
        paper_scroll = R.NSScrollView.alloc().initWithFrame_(((0, 46), (DW, DH - 80)))
        paper_scroll.setHasVerticalScroller_(True)
        paper_scroll.setAutohidesScrollers_(True)
        paper_scroll.setDrawsBackground_(False)
        paper_scroll.setAutoresizingMask_(R.NSViewWidthSizable | R.NSViewHeightSizable)
        self.paper = make_paper(((0, 0), paper_scroll.contentSize()), '', editable=False)
        paper_scroll.setDocumentView_(self.paper)
        detail.addSubview_(paper_scroll)
        self.delete_button = make_button('Delete', ((DW - 92, 8), (80, 30)), self, 'deleteNote:')
        self.restore_button = make_button('Restore', ((DW - 232, 8), (92, 30)), self, 'restoreNote:')
        self.forever_button = make_button('Delete Forever', ((DW - 136, 8), (124, 30)), self, 'deleteForever:')
        for b in (self.delete_button, self.restore_button, self.forever_button):
            b.setAutoresizingMask_(R.NSViewMinXMargin | R.NSViewMaxYMargin)
            detail.addSubview_(b)
        split.addSubview_(detail)
        content.addSubview_(split)
        split.setHoldingPriority_forSubviewAtIndex_(260, 0)  # 窗口变大时，多出来的宽度给右边的笔记
        split.adjustSubviews()
        split.setPosition_ofDividerAtIndex_(400, 0)
        self.table.sizeLastColumnToFit()

        # 底部：数量
        self.count = label_view('', ((12, 8), (420, 16)), 11, R.NSColor.secondaryLabelColor())
        content.addSubview_(self.count)
        self.empty_button = make_button('Empty Recently Deleted', ((W - 212, 1), (200, 28)), self, 'emptyTrash:')
        self.empty_button.setAutoresizingMask_(R.NSViewMinXMargin | R.NSViewMaxYMargin)
        content.addSubview_(self.empty_button)
        self.in_trash = False

    # 重新读取数据（搜索、筛选、保存或删除后）
    @objc.python_method
    def reload(self, keep_id=None):
        if self.window is None:
            return
        chosen = self.label_menu.titleOfSelectedItem()
        self.label_menu.removeAllItems()
        self.label_menu.addItemWithTitle_('All labels')
        for label in notes_store.all_labels(db):
            self.label_menu.addItemWithTitle_('#' + label)
        self.label_menu.menu().addItem_(AppKit.NSMenuItem.separatorItem())
        self.label_menu.addItemWithTitle_(TRASH)
        self.label_menu.lastItem().setImage_(
            AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_('trash', 'Recently Deleted'))
        if chosen and self.label_menu.itemWithTitle_(chosen) is not None:
            self.label_menu.selectItemWithTitle_(chosen)
        label = self.label_menu.titleOfSelectedItem()
        self.in_trash = label == TRASH
        label = None if label in (None, 'All labels', TRASH) else label[1:]
        self.rows = notes_store.list_notes(db, self.search.stringValue(), label, deleted=self.in_trash)
        self.table.tableColumnWithIdentifier_('date').setTitle_('Deleted' if self.in_trash else 'Date')
        self.table.reloadData()
        total = len(notes_store.list_notes(db, deleted=self.in_trash))
        noun = 'deleted note' if self.in_trash else 'note'
        text = f'{len(self.rows)} of {total} {noun}s' if len(self.rows) != total \
            else f'{total} {noun}' + ('' if total == 1 else 's')
        if self.in_trash:
            text += f' · removed for good after {trash_days} days'
        self.count.setStringValue_(text)
        self.delete_button.setHidden_(self.in_trash)
        self.restore_button.setHidden_(not self.in_trash)
        self.forever_button.setHidden_(not self.in_trash)
        self.empty_button.setHidden_(not self.in_trash)
        self.empty_button.setEnabled_(total > 0)
        row = next((i for i, r in enumerate(self.rows) if r[0] == keep_id), 0)
        if self.rows:
            self.table.selectRowIndexes_byExtendingSelection_(AppKit.NSIndexSet.indexSetWithIndex_(row), False)
            self.table.scrollRowToVisible_(row)
        self.show_detail()

    @objc.python_method
    def show_detail(self):
        row = self.table.selectedRow()
        if 0 <= row < len(self.rows):
            note_id, text, created, labels, deleted_at = self.rows[row]
            tags = '  '.join('#' + l for l in (labels or '').split(',') if l)
            if self.in_trash:
                ago = notes_store.days_in_trash(deleted_at)
                when = 'today' if ago == 0 else ('1 day ago' if ago == 1 else f'{ago} days ago')
                left = max(0, trash_days - ago)
                self.header.setStringValue_(f'Deleted {when} · {left} day' + ('' if left == 1 else 's')
                                            + f' left   (written {fmt_time(created)})')
            else:
                self.header.setStringValue_(fmt_time(created, long=True) + ('   ' + tags if tags else ''))
            self.paper.textStorage().setAttributedString_(paper_string(text))
            for b in (self.delete_button, self.restore_button, self.forever_button):
                b.setEnabled_(True)
        else:
            if self.in_trash:
                empty = 'Recently Deleted is empty.' if not self.search.stringValue() else 'No deleted notes match.'
            elif not self.search.stringValue() and self.label_menu.indexOfSelectedItem() <= 0:
                empty = 'No notes yet. Double-click Majima to write one.'
            else:
                empty = 'No notes match.'
            self.header.setStringValue_(empty)
            self.paper.textStorage().setAttributedString_(paper_string(''))
            for b in (self.delete_button, self.restore_button, self.forever_button):
                b.setEnabled_(False)

    def filter_(self, sender):
        self.reload()

    # 处理选中的笔记，然后选中它下面（或上面）的一条
    @objc.python_method
    def act_on_selected(self, action):
        row = self.table.selectedRow()
        if not 0 <= row < len(self.rows):
            return
        action(self.rows[row][0])
        next_id = self.rows[row + 1][0] if row + 1 < len(self.rows) else (self.rows[row - 1][0] if row else None)
        self.reload(keep_id=next_id)

    @objc.python_method
    def confirm(self, title, info, button):
        alert = AppKit.NSAlert.alloc().init()
        alert.setMessageText_(title)
        alert.setInformativeText_(info)
        alert.addButtonWithTitle_(button)
        alert.addButtonWithTitle_('Cancel')
        return alert.runModal() == AppKit.NSAlertFirstButtonReturn

    def deleteNote_(self, sender):  # 放进"最近删除"，不用确认
        self.act_on_selected(lambda i: notes_store.delete_note(db, i))

    def restoreNote_(self, sender):
        self.act_on_selected(lambda i: notes_store.restore_note(db, i))

    def deleteForever_(self, sender):
        row = self.table.selectedRow()
        if 0 <= row < len(self.rows) and self.confirm(
                'Delete this note forever?', self.rows[row][1].split('\n')[0][:80] + '\n\nThis cannot be undone.',
                'Delete Forever'):
            self.act_on_selected(lambda i: notes_store.delete_forever(db, [i]))

    def emptyTrash_(self, sender):
        n = len(notes_store.list_notes(db, deleted=True))
        if n and self.confirm(f'Delete {n} note' + ('' if n == 1 else 's') + ' forever?',
                              'Everything in Recently Deleted will be erased. This cannot be undone.', 'Empty'):
            notes_store.empty_trash(db)
            self.reload()

    # 列表的数据源和选择
    def numberOfRowsInTableView_(self, table):
        return len(self.rows)

    def tableView_objectValueForTableColumn_row_(self, table, column, row):
        note_id, text, created, labels, deleted_at = self.rows[row]
        ident = column.identifier()
        if ident == 'date':
            return fmt_time(deleted_at if self.in_trash else created)
        if ident == 'note':
            return text.split('\n')[0]
        return ', '.join(l for l in (labels or '').split(',') if l)

    def tableViewSelectionDidChange_(self, notification):
        self.show_detail()

notebook = Notebook.alloc().init()

# 每天检查一次：在"最近删除"里放了超过 trash_days 天的笔记永久删除
def purge_trash():
    if notes_store.purge_expired(db, trash_days):
        notebook.reload()
    AppHelper.callLater(24 * 3600, purge_trash)

purge_trash()

# ---------------- 番茄钟 ----------------
# 流程：输入任务 → 专注 25 分钟 → 问"任务完成了吗？" → 短休息 5 分钟（×3）/ 第 4 个之后长休息 30 分钟 → 下一个番茄 …
# 每个阶段结束自动进入下一个阶段，并响闹钟（见 Alarm）。

def seconds_since_input():
    """距离上一次鼠标 / 键盘操作过了多少秒"""
    return Quartz.CGEventSourceSecondsSinceLastEventType(
        Quartz.kCGEventSourceStateCombinedSessionState, Quartz.kCGAnyInputEventType)

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
        self.asleep_at = None   # Mac 开始睡眠的时间（醒来前不计时）
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
            return art('focus-end')
        if self.stage == 'interrupted':
            return art('check')  # 睡醒后问要不要重新开始：一脸疑惑  # 等你回答时：站在堆满的金币旁边
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
        """Mac 要睡眠了：醒来之前不计时、不响铃"""
        self.asleep_at = time.time()
        runtime_log.event('sleep', stage=self.stage)

    def wake(self, slept_from=None):
        """Mac 醒了：短睡当作暂停；长睡中断专注；休息照算，结束了就安静地说一句"""
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

# ---------------- 闹钟：声音 + Majima 走到屏幕中间表演，然后走回原位 ----------------
sound_dir = (os.path.expanduser('~/Library/Application Support/MajimaVpet/sounds/') if packaged
             else impath + 'sounds/')  # 自己的闹钟声音（打包后的 App 里不能放文件）
sound_files = {'focus_end': 'focus-end', 'break_end': 'break-end', 'warning': 'warning', 'task_done': 'task-done'}
alarm_art = {'focus_end': 'focus-end', 'break_end': 'alarm-work', 'task_done': 'alarm-done'}
alarm_text = {'focus_end': 'Pomodoro done!', 'break_end': 'Back to work!', 'task_done': 'Task complete!'}
muted = False

def play_sound(kind):
    if muted:
        return
    sound = None
    for ext in ('m4a', 'mp3', 'wav', 'aiff'):  # sounds/ 文件夹里的同名文件优先
        path = f'{sound_dir}{sound_files[kind]}.{ext}'
        if os.path.exists(path):
            sound = AppKit.NSSound.alloc().initWithContentsOfFile_byReference_(path, True)
            break
    if sound is None:
        sound = AppKit.NSSound.soundNamed_(alarm_sounds[kind])
    if sound is None:
        return
    audio.keep(sound.duration() + 2.5)
    runtime_log.event('sound', kind=kind, custom=sound.name() is None, output=runtime_log.audio_output(),
                      output_warm_s=round(time.time() - audio.since, 1), waited=not audio.ready())
    if audio.ready():
        start_sound(sound)
    else:  # 输出刚刚打开：等 1 秒再放，声音开头才不会丢
        AppHelper.callLater(1, start_sound, sound)

def start_sound(sound):
    sound.stop()
    sound.play()
    playing_sounds[:] = [sound]  # 保持引用，播放完之前不被回收

playing_sounds = []

# 蓝牙耳机（比如 AirPods）没有声音时会关掉输出，下一个声音的开头（约 0.3–1 秒）就会丢失，短的声音会完全听不到。
# 所以响铃前先放几秒无声的声音，让输出一直开着，声音放完再关。
awake_lead = 3  # 响铃前多少秒开始放无声的声音

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
            s = AppKit.NSSound.soundNamed_(alarm_sounds['warning']).copy()
            s.setVolume_(0)
            s.setLoops_(True)
            s.play()
            self.silence, self.since = s, now
        self.until = max(self.until, now + seconds)

    def ready(self):
        """输出已经开了至少 1 秒（蓝牙耳机准备好了）"""
        return self.silence is not None and time.time() - self.since >= 1

    def tick(self):
        if self.silence is not None and time.time() >= self.until:
            self.silence.stop()
            self.silence = None
            self.until = 0

audio = AudioKeeper()

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
        x = area.origin.x + area.size.width / 2 - anims[anim][2] * scale
        y = area.origin.y + (area.size.height - height) / 2
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
        global mode
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
def load_fonts():
    try:
        ct = ctypes.cdll.LoadLibrary('/System/Library/Frameworks/CoreText.framework/CoreText')
        ct.CTFontManagerRegisterFontsForURL.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]
        ct.CTFontManagerRegisterFontsForURL.restype = ctypes.c_bool
        folder = impath + 'fonts/'
        for name in os.listdir(folder) if os.path.isdir(folder) else []:
            if name.endswith(('.ttf', '.otf')):
                ct.CTFontManagerRegisterFontsForURL(objc.pyobjc_id(NSURL.fileURLWithPath_(folder + name)), 1, None)
    except Exception as e:
        print('could not load fonts:', e)

load_fonts()

def pixel_font(size):
    return AppKit.NSFont.fontWithName_size_(pixel_font_name, size) or AppKit.NSFont.boldSystemFontOfSize_(size)

# ---------------- 头顶的话：黄色小标签 + 像素字，几秒后消失 ----------------
class Bubble:
    def __init__(self):
        self.panel = None
        self.token = 0

    def build(self):
        self.panel = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            ((0, 0), (100, 30)), AppKit.NSWindowStyleMaskBorderless | AppKit.NSWindowStyleMaskNonactivatingPanel,
            AppKit.NSBackingStoreBuffered, False)
        p = self.panel
        p.setOpaque_(False)
        p.setBackgroundColor_(AppKit.NSColor.clearColor())
        p.setHasShadow_(True)
        p.setIgnoresMouseEvents_(True)
        p.setLevel_(AppKit.NSStatusWindowLevel)
        p.setHidesOnDeactivate_(False)
        p.setCollectionBehavior_(window.collectionBehavior())
        box = AppKit.NSView.alloc().initWithFrame_(((0, 0), (100, 30)))  # 黄色圆角标签，深色边框
        box.setWantsLayer_(True)
        box.layer().setBackgroundColor_(paper_color.CGColor())
        box.layer().setCornerRadius_(9)
        box.layer().setBorderWidth_(2)
        box.layer().setBorderColor_(ink_color.CGColor())
        self.label = AppKit.NSTextField.labelWithString_('')
        box.addSubview_(self.label)
        p.setContentView_(box)

    def show(self, text, seconds=4):
        if self.panel is None:
            self.build()
        self.label.setAttributedStringValue_(AppKit.NSAttributedString.alloc().initWithString_attributes_(text, {
            AppKit.NSFontAttributeName: pixel_font(13),
            AppKit.NSForegroundColorAttributeName: ink_color}))
        size = self.label.fittingSize()
        w, h = size.width + 24, size.height + 12
        self.panel.setContentSize_((w, h))
        self.panel.contentView().setFrame_(((0, 0), (w, h)))
        self.label.setFrame_(((12, 6), size))
        check_card.suspend()  # 黄色标签和"任务完成了吗？"气泡不同时出现
        self.follow(force=True)
        self.panel.orderFrontRegardless()
        pin_window(self.panel)
        self.token += 1
        AppHelper.callLater(seconds, self.hide, self.token)

    def hide(self, token):
        if token == self.token and self.panel is not None:
            self.panel.orderOut_(None)
            check_card.resume()

    def visible(self):
        return self.panel is not None and self.panel.isVisible()

    def follow(self, force=False):
        """跟着 Majima：放在他头顶"""
        if self.panel is None or not (force or self.panel.isVisible()):
            return
        f = self.panel.frame()
        area = screen_area()
        x = pos[0] + anims[anim][2] * scale - f.size.width / 2
        y = pos[1] + 90 * scale  # 头顶上方，不挡住头发
        x = min(max(x, area.origin.x), area.origin.x + area.size.width - f.size.width)
        y = min(y, area.origin.y + area.size.height - f.size.height)
        self.panel.setFrameOrigin_((x, y))

bubble = Bubble()

# ---------------- 番茄结束后 Majima 在头顶的像素对话气泡里问"任务完成了吗？"（休息期间一直可以回答）----------------
bubble_border = 3   # 像素气泡的边框粗细
bubble_tail = 9     # 气泡下面指向 Majima 的小尾巴高度

class SpeechBubbleView(AppKit.NSView):
    """白底、粗黑边、直角的像素风对话框，下面有一个指向 Majima 的小尾巴"""
    def drawRect_(self, rect):
        b = self.bounds()
        w, h, t, k = b.size.width, b.size.height, bubble_tail, bubble_border
        tail_x = speech_tail_x[0]
        AppKit.NSColor.blackColor().colorWithAlphaComponent_(0).set()
        # 先画黑色的外形（框 + 尾巴），再在里面画白色
        ink_color.setFill()
        AppKit.NSBezierPath.fillRect_(((0, t), (w, h - t)))
        AppKit.NSBezierPath.fillRect_(((tail_x - 6 - k, 0), (12 + 2 * k, t + k)))
        AppKit.NSColor.whiteColor().setFill()
        AppKit.NSBezierPath.fillRect_(((k, t + k), (w - 2 * k, h - t - 2 * k)))
        AppKit.NSBezierPath.fillRect_(((tail_x - 6, k), (12, t + k)))

speech_tail_x = [30]

def pixel_button(title, frame, target, action):
    """像素风按钮：白底、黑框、像素字"""
    b = AppKit.NSButton.alloc().initWithFrame_(frame)
    b.setBordered_(False)
    b.setWantsLayer_(True)
    b.layer().setBackgroundColor_(AppKit.NSColor.whiteColor().CGColor())
    b.layer().setBorderWidth_(2)
    b.layer().setBorderColor_(ink_color.CGColor())
    b.setAttributedTitle_(AppKit.NSAttributedString.alloc().initWithString_attributes_(
        title, {AppKit.NSFontAttributeName: pixel_font(11), AppKit.NSForegroundColorAttributeName: ink_color}))
    b.setTarget_(target)
    b.setAction_(action)
    return b

class CheckCard(AppKit.NSObject):
    def init(self):
        self = objc.super(CheckCard, self).init()
        self.panel = None
        self.suspended = False  # 黄色标签显示时暂时让开，标签消失后再出现
        return self

    @objc.python_method
    def suspend(self):
        if self.panel is not None and not self.suspended:
            self.suspended = True
            self.panel.orderOut_(None)

    @objc.python_method
    def resume(self):
        if self.panel is not None and self.suspended:
            self.suspended = False
            self.follow(force=True)
            self.panel.orderFrontRegardless()
            pin_window(self.panel)

    @objc.python_method
    def show(self, interrupted=False):
        self.close()
        w, h = 196, 74 + bubble_tail
        question, left, right = (('Restart focus?', ('Restart', 'restart:', 90), ('Stop', 'stopTask:', 66)) if interrupted
                                 else ('Task finished?', ('Done', 'done:', 74), ('Not yet', 'keepGoing:', 86)))
        self.panel = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            ((0, 0), (w, h)), AppKit.NSWindowStyleMaskBorderless | AppKit.NSWindowStyleMaskNonactivatingPanel,
            AppKit.NSBackingStoreBuffered, False)
        p = self.panel
        p.setOpaque_(False)
        p.setBackgroundColor_(AppKit.NSColor.clearColor())
        p.setHasShadow_(True)
        p.setLevel_(AppKit.NSStatusWindowLevel)
        p.setHidesOnDeactivate_(False)
        p.setReleasedWhenClosed_(False)
        p.setBecomesKeyOnlyIfNeeded_(True)
        p.setCollectionBehavior_(window.collectionBehavior())
        view = SpeechBubbleView.alloc().initWithFrame_(((0, 0), (w, h)))
        q = AppKit.NSTextField.labelWithString_('')
        q.setAttributedStringValue_(AppKit.NSAttributedString.alloc().initWithString_attributes_(
            question, {AppKit.NSFontAttributeName: pixel_font(13), AppKit.NSForegroundColorAttributeName: ink_color}))
        q.setFrame_(((14, h - 34), (w - 28, 20)))
        view.addSubview_(q)
        view.addSubview_(pixel_button(left[0], ((14, bubble_tail + 12), (left[2], 26)), self, left[1]))
        view.addSubview_(pixel_button(right[0], ((w - 14 - right[2], bubble_tail + 12), (right[2], 26)), self, right[1]))
        p.setContentView_(view)
        self.follow(force=True)
        if bubble.visible():
            self.suspended = True  # 黄色标签还在：等它消失再出现
        else:
            p.orderFrontRegardless()
            pin_window(p)

    @objc.python_method
    def close(self):
        if self.panel is not None:
            self.panel.orderOut_(None)
            self.panel = None
        self.suspended = False

    @objc.python_method
    def follow(self, force=False):
        """放在 Majima 头顶，尾巴指向他；靠近屏幕边缘时气泡挪进来，尾巴仍对着他"""
        if self.panel is None:
            return
        f = self.panel.frame()
        area = screen_area()
        body_x = pos[0] + anims[anim][2] * scale
        x = min(max(body_x - 30, area.origin.x), area.origin.x + area.size.width - f.size.width)
        y = min(pos[1] + 84 * scale, area.origin.y + area.size.height - f.size.height)
        tail = min(max(body_x - x, 6 + bubble_border + 4), f.size.width - 6 - bubble_border - 4)
        if force or (round(x), round(y)) != (round(f.origin.x), round(f.origin.y)) or tail != speech_tail_x[0]:
            speech_tail_x[0] = tail
            self.panel.setFrameOrigin_((x, y))
            self.panel.contentView().setNeedsDisplay_(True)

    def keepGoing_(self, sender):
        self.close()
        pomo.start_break()

    def done_(self, sender):
        self.close()
        pomo.finish()

    def restart_(self, sender):
        self.close()
        pomo.restart_focus()

    def stopTask_(self, sender):
        self.close()
        pomo.stop()

check_card = CheckCard.alloc().init()

# ---------------- 开始番茄钟：输入任务名 ----------------
class TaskPanel(AppKit.NSPanel):
    def canBecomeKeyWindow(self):
        return True

    def cancelOperation_(self, sender):  # Esc：取消
        if task_box is not None:
            task_box.cancel_(None)

class TaskBox(AppKit.NSObject):
    @objc.python_method
    def open(self):
        w, h = 300, line_h + 2 * paper_inset[1] + 46
        self.panel = TaskPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            ((0, 0), (w, h)), AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskUtilityWindow
            | AppKit.NSWindowStyleMaskNonactivatingPanel, AppKit.NSBackingStoreBuffered, False)
        setup_panel(self.panel, 'New Pomodoro')
        content = self.panel.contentView()
        self.paper = make_paper(((0, 46), (w, h - 46)), '', editable=True)
        paper_placeholders[objc.pyobjc_id(self.paper)] = 'What will you work on?'
        self.paper.setDelegate_(self)
        content.addSubview_(self.paper)
        content.addSubview_(make_button('Cancel', ((w - 206, 8), (84, 30)), self, 'cancel:'))
        start = make_button(f'Start {focus_minutes:g} min', ((w - 118, 8), (106, 30)), self, 'start:')
        start.setKeyEquivalent_('\r')
        content.addSubview_(start)
        place_above_majima(self.panel)
        o = self.panel.frame().origin
        self.offset = (o.x - (pos[0] + anims[anim][2] * scale), o.y - pos[1])  # 相对 Majima 身体的位置
        self.panel.setDelegate_(self)
        self.panel.makeKeyAndOrderFront_(None)
        pin_window(self.panel)
        self.panel.addChildWindow_ordered_(window, AppKit.NSWindowAbove)  # 拖输入页时 Majima 跟着走
        self.panel.makeFirstResponder_(self.paper)

    # 拖 Majima → 把输入页移到对应位置，Majima 作为子窗口一起走
    @objc.python_method
    def move_together(self, x, y):
        self.panel.setFrameOrigin_((x + anims[anim][2] * scale + self.offset[0], y + self.offset[1]))
        self.follow_panel()

    # 输入页被拖动后，根据 Majima 窗口的实际位置更新 pos
    @objc.python_method
    def follow_panel(self):
        global shown_pos
        f = window.frame().origin
        if (round(f.x), round(f.y)) != shown_pos:
            pos[0], pos[1] = f.x, f.y
            shown_pos = (round(f.x), round(f.y))

    def windowDidMove_(self, notification):
        self.follow_panel()

    def textView_doCommandBySelector_(self, text_view, selector):
        selector = selector.decode() if isinstance(selector, bytes) else str(selector)
        if selector in ('insertNewline:', 'insertNewlineIgnoringFieldEditor:'):
            self.start_(None)  # 任务名只有一行：回车 = 开始
            return True
        if selector in ('cancelOperation:', 'complete:'):
            self.cancel_(None)
            return True
        return False

    def textDidChange_(self, notification):
        self.paper.setNeedsDisplay_(True)

    def start_(self, sender):
        name = self.paper.string().strip()
        if not name:
            NSBeep()
            return
        self.close()
        pomo.start(name)
        bubble.show(f'Focus {focus_minutes:g} min — go!')

    def cancel_(self, sender):
        self.close()

    @objc.python_method
    def close(self):
        global task_box, typing_ignore_until
        paper_placeholders.pop(objc.pyobjc_id(self.paper), None)
        self.follow_panel()
        self.panel.removeChildWindow_(window)  # 先解除父子关系，否则隐藏输入页会把 Majima 一起隐藏
        self.panel.orderOut_(None)
        move_to(*clamp_origin(pos[0], pos[1], screen_area()))
        task_box = None
        typing_ignore_until = time.monotonic() + typing_after + 0.2

task_box = None

def open_task_box():
    global task_box
    if task_box is None and not pomo.active:
        task_box = TaskBox.alloc().init()
        task_box.open()

# 创建应用（不在 Dock 显示图标）
app = AppKit.NSApplication.sharedApplication()
app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)

idle_frame = anims['idle'][0][0]
width = Quartz.CGImageGetWidth(idle_frame) * scale
height = Quartz.CGImageGetHeight(idle_frame) * scale

# 初始位置：主屏幕右下角，站在 Dock 上方
area = AppKit.NSScreen.mainScreen().visibleFrame()
start_x = area.origin.x + area.size.width - width - 100

# 无边框、透明、总在最上层的窗口（透明部分点击穿透，角色本身可以拖动）
# 用"不激活 App"的面板：点他、在笔记本里打字都不会把本 App 切到前台，
# 这样切换桌面时系统不会先隐藏再显示他（前台 App 的窗口切桌面时会闪一下）
window = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
    ((start_x, area.origin.y), (width, height)),
    AppKit.NSWindowStyleMaskBorderless | AppKit.NSWindowStyleMaskNonactivatingPanel,
    AppKit.NSBackingStoreBuffered,
    False,
)
window.setHidesOnDeactivate_(False)
window.setBecomesKeyOnlyIfNeeded_(True)
window.setTitle_("MajimaVpet")
window.setOpaque_(False)
window.setBackgroundColor_(AppKit.NSColor.clearColor())
window.setHasShadow_(False)
window.setLevel_(AppKit.NSStatusWindowLevel)  # 比普通窗口和浮动面板都高
window.setIgnoresMouseEvents_(False)
# 在所有桌面（Space）同一位置显示，切换桌面 / 调度中心 / 全屏 App 时都不消失
window.setCollectionBehavior_(
    AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces     # 每个桌面都显示
    | AppKit.NSWindowCollectionBehaviorStationary         # 调度中心、显示桌面时不被移走
    | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary  # 全屏 App 上方也能显示
    | AppKit.NSWindowCollectionBehaviorIgnoresCycle       # 不参与 ⌘` 窗口切换
)

# 用 layer 显示图片，最近邻缩放保持像素清晰
view = PetView.alloc().initWithFrame_(((0, 0), (width, height)))
view.setWantsLayer_(True)
view.layer().setMagnificationFilter_(Quartz.kCAFilterNearest)
window.setContentView_(view)
window.orderFrontRegardless()
pin_window(window)
move_to(start_x, area.origin.y)

# 菜单栏控制器：显示/隐藏、暂停/继续、退出
class MenuController(AppKit.NSObject):
    def toggleVisible_(self, sender):
        if window.isVisible():
            window.orderOut_(None)
            sender.setTitle_("Show Majima")
        else:
            window.orderFrontRegardless()
            pin_window(window)
            sender.setTitle_("Hide Majima")

    def togglePause_(self, sender):
        global paused
        paused = not paused
        sender.setTitle_("Resume" if paused else "Pause")

    def openNotebook_(self, sender):
        notebook.open()

    def startPomodoro_(self, sender):
        open_task_box()

    def pausePomodoro_(self, sender):
        pomo.toggle_pause()

    def restartFocus_(self, sender):
        check_card.close()
        pomo.restart_focus()

    def skipBreak_(self, sender):
        pomo.skip_break()

    def finishTask_(self, sender):
        pomo.finish()

    def stopPomodoro_(self, sender):
        pomo.stop()

    def toggleMute_(self, sender):
        global muted
        muted = not muted

    def menuNeedsUpdate_(self, m):
        build_menu(m)

    def exportRuntime_(self, sender):
        path = runtime_log.export(APP_VERSION, screen_sizes())
        AppKit.NSWorkspace.sharedWorkspace().activateFileViewerSelectingURLs_([NSURL.fileURLWithPath_(path)])
        bubble.show('Saved to Desktop')

    def quit_(self, sender):
        runtime_log.quit()
        app.terminate_(None)

controller = MenuController.alloc().init()

class SleepWatcher(AppKit.NSObject):
    """Mac 睡眠 / 醒来时通知番茄钟"""
    def willSleep_(self, note):
        pomo.sleep()

    def didWake_(self, note):
        pomo.wake()

sleep_watcher = SleepWatcher.alloc().init()
_ws_center = AppKit.NSWorkspace.sharedWorkspace().notificationCenter()
_ws_center.addObserver_selector_name_object_(sleep_watcher, 'willSleep:', AppKit.NSWorkspaceWillSleepNotification, None)
_ws_center.addObserver_selector_name_object_(sleep_watcher, 'didWake:', AppKit.NSWorkspaceDidWakeNotification, None)

# 菜单栏图标：用 idle 第一帧缩成 20pt 高的小像素人
icon_height = 20
icon = AppKit.NSImage.alloc().initWithCGImage_size_(
    idle_frame, (Quartz.CGImageGetWidth(idle_frame) * icon_height / Quartz.CGImageGetHeight(idle_frame), icon_height)
)

status_item = AppKit.NSStatusBar.systemStatusBar().statusItemWithLength_(AppKit.NSVariableStatusItemLength)
status_item.button().setImage_(icon)
status_item.button().setToolTip_("MajimaVpet")

def build_menu(m):
    m.removeAllItems()
    def item(title, action=None, key='', enabled=True, checked=False):
        it = m.addItemWithTitle_action_keyEquivalent_(title, action, key)
        if action:
            it.setTarget_(controller)
        it.setEnabled_(enabled)
        it.setState_(AppKit.NSControlStateValueOn if checked else AppKit.NSControlStateValueOff)
        return it
    item('MajimaVpet', enabled=False)
    m.addItem_(AppKit.NSMenuItem.separatorItem())
    if pomo.active:
        item(pomo.status(), enabled=False)
        if pomo.stage == 'interrupted':
            item('Restart Focus', 'restartFocus:')
        elif pomo.stage != 'check':
            item('Resume Pomodoro' if pomo.paused_left is not None else 'Pause Pomodoro', 'pausePomodoro:')
        if pomo.stage in ('short_break', 'long_break'):
            item('Skip Break', 'skipBreak:')
        item('Task Finished ✓', 'finishTask:')
        item('Stop Pomodoro', 'stopPomodoro:')
    else:
        item('Start Pomodoro…', 'startPomodoro:', 't')
    item('Mute Alarm Sounds', 'toggleMute:', checked=muted)
    m.addItem_(AppKit.NSMenuItem.separatorItem())
    item('Notebook…', 'openNotebook:', 'b')
    m.addItem_(AppKit.NSMenuItem.separatorItem())
    item('Show Majima' if not window.isVisible() else 'Hide Majima', 'toggleVisible:', 'h')
    item('Resume' if paused else 'Pause', 'togglePause:', 'p')
    m.addItem_(AppKit.NSMenuItem.separatorItem())
    item('Export Runtime Data…', 'exportRuntime:')
    item('Quit MajimaVpet', 'quit:', 'q')

menu = AppKit.NSMenu.alloc().init()
menu.setAutoenablesItems_(False)
menu.setDelegate_(controller)
build_menu(menu)
status_item.setMenu_(menu)

# 启动循环
def screen_sizes():
    return [f'{s.frame().size.width:.0f}x{s.frame().size.height:.0f}@{s.backingScaleFactor():.0f}x'
            for s in AppKit.NSScreen.screens()]

runtime_log.start(APP_VERSION, screen_sizes())
audio.keep(1)  # 启动时先把声音系统准备好（第一次要约 0.3 秒），之后响铃不卡
play('idle')
AppHelper.callLater(tick, update)
AppHelper.runEventLoop(installInterrupt=True)
