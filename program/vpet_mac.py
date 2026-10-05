import math
import os
import random
import time

import AppKit
import Quartz
from Foundation import NSURL
from PyObjCTools import AppHelper

# macOS 版本：系统自带的 Tk 无法在透明窗口中显示图片，所以这里直接用 Cocoa (PyObjC) 绘制
# 运行：双击 MajimaVpet.app，或 program/venv/bin/python program/vpet_mac.py
# 退出：点击菜单栏的 Majima 小图标 → Quit（终端里也可以 Ctrl+C）
#
# 动作分类：
#   steady      （平稳）    idle, walk     —— 鼠标停下不动时：发呆、往任意方向散步
#   interactive （互动反应） shock, confuse —— 被鼠标拖动时：拎起来 "!"，放下后 "?"
#   showtime    （表演时间） change, dance  —— 鼠标在活动时：闪光登场，然后跳舞

impath = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + '/'  # GIF 在项目根目录
scale = 1  # 放大倍数（像素风格不会模糊）

tick = 0.05             # 主循环间隔（秒）
still_after = 1.5       # 鼠标静止多少秒后停止表演、回到平稳
walk_speed = 1          # 每个 tick 走多少像素（任意方向）
walk_loops = (1, 2)     # 每次散步播放几遍走路动画（每遍约 20 像素）
dropped_loops = 2       # 放下后 "?" 动画播放几遍

# 加载 GIF 的每一帧（CGImage）和每帧时长（秒）
def load_gif(name):
    src = Quartz.CGImageSourceCreateWithURL(NSURL.fileURLWithPath_(impath + 'majima-' + name + '.gif'), None)
    frames, delays = [], []
    for i in range(Quartz.CGImageSourceGetCount(src)):
        frames.append(Quartz.CGImageSourceCreateImageAtIndex(src, i, None))
        props = Quartz.CGImageSourceCopyPropertiesAtIndex(src, i, None)
        delays.append(props.get('{GIF}', {}).get('DelayTime') or 0.2)
    return frames, delays

categories = {
    'steady': ['idle', 'walk'],
    'interactive': ['shock', 'confuse'],
    'showtime': ['change', 'dance'],
}
anims = {name: load_gif(name) for names in categories.values() for name in names}

# ---------------- 状态 ----------------
mode = 'steady'      # steady / showtime / dragging / dropped
anim = 'idle'        # 当前动画
frame = 0            # 当前帧
frame_time = 0.0     # 当前帧已显示的时间
loops_left = 1       # 当前动画还要播放几遍
walk_vec = (0, 0)    # 每个 tick 的位移 (dx, dy)
pos = [0.0, 0.0]     # 窗口位置（浮点，斜着走时不会被取整吃掉）
paused = False
last_mouse = AppKit.NSEvent.mouseLocation()
last_move = time.monotonic()

def play(name, loops=1, vec=(0, 0)):
    global anim, frame, frame_time, loops_left, walk_vec
    anim, frame, frame_time, loops_left, walk_vec = name, 0, 0.0, loops, vec
    view.layer().setContents_(anims[anim][0][0])

def cursor_moving():
    return time.monotonic() - last_move < still_after

def random_direction():
    angle = random.uniform(0, 2 * math.pi)
    return (math.cos(angle) * walk_speed, math.sin(angle) * walk_speed)

def move_to(x, y):
    pos[0], pos[1] = x, y
    window.setFrameOrigin_((x, y))

# 当前动画播完后，根据模式决定下一个动画
def next_anim():
    global mode
    if mode == 'steady':
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
    elif mode == 'dropped':
        mode = 'steady'
        next_anim()
    elif mode == 'dragging':
        play('shock')

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
    global mode, last_mouse, last_move, frame, frame_time, loops_left, walk_vec
    AppHelper.callLater(tick, update)
    if paused:
        return

    mouse = AppKit.NSEvent.mouseLocation()
    if mouse.x != last_mouse.x or mouse.y != last_mouse.y:
        last_mouse = mouse
        last_move = time.monotonic()
        if mode == 'steady' and anim != 'change':
            mode = 'showtime'
            play('change')  # 鼠标动了：立刻闪光登场
    elif mode == 'showtime' and anim == 'dance' and not cursor_moving():
        next_anim()  # 鼠标停了：结束跳舞

    if anim == 'walk' and mode == 'steady':
        dx, dy = walk_vec
        x, y = clamp_origin(pos[0] + dx, pos[1] + dy, screen_area())
        # 碰到屏幕边缘就反弹
        if x != pos[0] + dx:
            dx = -dx
        if y != pos[1] + dy:
            dy = -dy
        walk_vec = (dx, dy)
        move_to(x, y)

    frames, delays = anims[anim]
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

# 可拖动的桌宠视图
class PetView(AppKit.NSView):
    def acceptsFirstMouse_(self, event):
        return True

    def mouseDown_(self, event):
        global mode, drag_offset
        mouse = AppKit.NSEvent.mouseLocation()
        origin = window.frame().origin
        drag_offset = (mouse.x - origin.x, mouse.y - origin.y)
        mode = 'dragging'
        play('shock', loops=1)  # 被拎起来："!"

    def mouseDragged_(self, event):
        mouse = AppKit.NSEvent.mouseLocation()
        move_to(*clamp_origin(mouse.x - drag_offset[0], mouse.y - drag_offset[1], screen_area(mouse)))

    def mouseUp_(self, event):
        global mode
        mode = 'dropped'
        play('confuse', loops=dropped_loops)  # 放下后："?"

drag_offset = (0, 0)

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
window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
    ((start_x, area.origin.y), (width, height)),
    AppKit.NSWindowStyleMaskBorderless,
    AppKit.NSBackingStoreBuffered,
    False,
)
window.setTitle_("MajimaVpet")
window.setOpaque_(False)
window.setBackgroundColor_(AppKit.NSColor.clearColor())
window.setHasShadow_(False)
window.setLevel_(AppKit.NSFloatingWindowLevel)
window.setIgnoresMouseEvents_(False)
window.setCollectionBehavior_(AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces)

# 用 layer 显示图片，最近邻缩放保持像素清晰
view = PetView.alloc().initWithFrame_(((0, 0), (width, height)))
view.setWantsLayer_(True)
view.layer().setMagnificationFilter_(Quartz.kCAFilterNearest)
window.setContentView_(view)
window.orderFrontRegardless()
move_to(start_x, area.origin.y)

# 菜单栏控制器：显示/隐藏、暂停/继续、退出
class MenuController(AppKit.NSObject):
    def toggleVisible_(self, sender):
        if window.isVisible():
            window.orderOut_(None)
            sender.setTitle_("Show Majima")
        else:
            window.orderFrontRegardless()
            sender.setTitle_("Hide Majima")

    def togglePause_(self, sender):
        global paused
        paused = not paused
        sender.setTitle_("Resume" if paused else "Pause")

    def quit_(self, sender):
        app.terminate_(None)

controller = MenuController.alloc().init()

# 菜单栏图标：用 idle 第一帧缩成 20pt 高的小像素人
icon_height = 20
icon = AppKit.NSImage.alloc().initWithCGImage_size_(
    idle_frame, (Quartz.CGImageGetWidth(idle_frame) * icon_height / Quartz.CGImageGetHeight(idle_frame), icon_height)
)

status_item = AppKit.NSStatusBar.systemStatusBar().statusItemWithLength_(AppKit.NSVariableStatusItemLength)
status_item.button().setImage_(icon)
status_item.button().setToolTip_("MajimaVpet")

menu = AppKit.NSMenu.alloc().init()
header = menu.addItemWithTitle_action_keyEquivalent_("MajimaVpet", None, "")
header.setEnabled_(False)
menu.addItem_(AppKit.NSMenuItem.separatorItem())
for title, action, key in [("Hide Majima", "toggleVisible:", "h"), ("Pause", "togglePause:", "p")]:
    menu.addItemWithTitle_action_keyEquivalent_(title, action, key).setTarget_(controller)
menu.addItem_(AppKit.NSMenuItem.separatorItem())
menu.addItemWithTitle_action_keyEquivalent_("Quit MajimaVpet", "quit:", "q").setTarget_(controller)
status_item.setMenu_(menu)

# 启动循环
play('idle')
AppHelper.callLater(tick, update)
AppHelper.runEventLoop(installInterrupt=True)
