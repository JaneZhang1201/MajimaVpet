import datetime
import math
import os
import random
import time

import AppKit
import objc
import Quartz
from Foundation import NSURL
from PyObjCTools import AppHelper

import notes_store

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

impath = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + '/'  # GIF 在项目根目录
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

# 角色脚下的水平中心（用来在不同宽度的动画之间对齐，避免切换时跳位置）
def body_center(img):
    ctx, buf, w, h = bitmap(img)
    rows = [y for y in range(h) if any(buf[(y * w + x) * 4 + 3] for x in range(w))]
    feet = range(max(rows) - 25, max(rows) + 1)  # 只看最下面的身体部分，忽略头顶的对话框/特效
    xs = [x for y in feet for x in range(w) if buf[(y * w + x) * 4 + 3]]
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
}
anims = {name: load_gif(name) for names in categories.values() for name in names}

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
    if paused or not window.isVisible():
        AppHelper.callLater(idle_tick, update)  # 看不到变化时降低频率，几乎不占 CPU
        return
    AppHelper.callLater(tick, update)
    ticks += 1

    if mode == 'notetake' and time.monotonic() - notetake_since > notetake_timeout:
        mode = 'steady'
        next_anim()  # 一直没点：回到平常动作

    if mode == 'note' and note_box is not None:
        note_box.follow_panel()  # 笔记本被拖走时，Majima 跟上

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

    if anim == 'walk' and mode == 'steady':
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
            if mode != 'note':  # 输入框打开时不换成 "!"，保持记笔记的样子
                mode = 'dragging'
                play('shock', loops=1)  # 被拎起来："!"（可能改变窗口大小，所以之后再算偏移）
            drag_offset = (mouse.x - pos[0], mouse.y - pos[1])
        move_to(*clamp_origin(mouse.x - drag_offset[0], mouse.y - drag_offset[1], screen_area(mouse)))
        if mode == 'note' and note_box is not None:
            note_box.follow_majima()  # 笔记本跟着 Majima 走

    def mouseUp_(self, event):
        global mode, notetake_since
        if mode == 'note':
            return  # 输入框打开时：拖完保持原样，点击不做任何事
        if drag_started:
            mode = 'dropped'
            play('confuse', loops=dropped_loops)  # 放下后："?"
        elif mode == 'notetake' and time.monotonic() - notetake_since > AppKit.NSEvent.doubleClickInterval():
            open_note_box()  # 再单击一下：弹出输入框
        elif event.clickCount() == 2 and mode in ('steady', 'showtime', 'typing', 'dropped'):
            mode = 'notetake'  # 双击：停在原地，准备记笔记
            notetake_since = time.monotonic()
            play('notetake')

press_point = AppKit.NSEvent.mouseLocation()
drag_started = False
drag_offset = (0, 0)

# ---------------- 记笔记：输入框和便利贴 ----------------
notes_db = notes_store.connect()
note_box = None       # 当前打开的输入框控制器
stickies = []         # 屏幕上的便利贴（保持引用，避免被回收）
previous_app = None   # 打开输入框前正在用的 App，关闭后把焦点还给它

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
                                 | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary)
    panel.setAppearance_(AppKit.NSAppearance.appearanceNamed_(AppKit.NSAppearanceNameAqua))  # 纸张总用浅色，深色模式下也看得清
    panel.setBackgroundColor_(paper_color)

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
                'Word or note… (#label optional)',
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
            ((0, 0), (w, h)), AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskUtilityWindow,
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
        self.last_origin = (o.x, o.y)
        self.syncing = False
        self.panel.setDelegate_(self)
        app.activateIgnoringOtherApps_(True)
        self.panel.makeKeyAndOrderFront_(None)
        self.panel.makeFirstResponder_(self.paper)

    # Majima 被拖动 → 笔记本跟着移动
    @objc.python_method
    def follow_majima(self):
        if self.done:
            return
        origin = (pos[0] + self.offset[0], pos[1] + self.offset[1])
        self.syncing = True
        self.panel.setFrameOrigin_(origin)
        self.syncing = False
        o = self.panel.frame().origin  # 系统可能把窗口限制在屏幕内
        self.last_origin = (o.x, o.y)

    # 笔记本被拖动 → Majima 跟着移动（同时被主循环定时检查，防止漏掉）
    @objc.python_method
    def follow_panel(self):
        if self.done or self.syncing:
            return
        o = self.panel.frame().origin
        if (o.x, o.y) == self.last_origin:
            return
        self.last_origin = (o.x, o.y)
        x, y = clamp_origin(o.x - self.offset[0], o.y - self.offset[1], screen_area(o))
        move_to(x, y)  # Majima 碰到屏幕边缘时停在边缘；笔记本拖回来后他会回到原来的相对位置

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
            notes_store.add_note(notes_db, text, kind='word')
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
        self.done = True
        self.panel.orderOut_(None)
        note_box = None
        typing_ignore_until = time.monotonic() + typing_after + 0.2  # 刚才的按键不算"正在打字"
        mode = 'steady'
        if saved:
            play('change')  # 闪光：保存成功
        else:
            next_anim()
        if previous_app is not None:
            previous_app.activateWithOptions_(0)  # 把焦点还给刚才在用的 App

def open_note_box():
    global mode, note_box, previous_app
    mode = 'note'
    play('notetake')
    front = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
    previous_app = None if front is None or front.processIdentifier() == os.getpid() else front
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

    def save_(self, sender):
        notes_store.add_note(notes_db, self.text, kind='word')
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
        self.delete_button.setAutoresizingMask_(R.NSViewMinXMargin | R.NSViewMaxYMargin)
        detail.addSubview_(self.delete_button)
        split.addSubview_(detail)
        content.addSubview_(split)
        split.setHoldingPriority_forSubviewAtIndex_(260, 0)  # 窗口变大时，多出来的宽度给右边的笔记
        split.adjustSubviews()
        split.setPosition_ofDividerAtIndex_(400, 0)
        self.table.sizeLastColumnToFit()

        # 底部：数量
        self.count = label_view('', ((12, 8), (300, 16)), 11, R.NSColor.secondaryLabelColor())
        content.addSubview_(self.count)

    # 重新读取数据（搜索、筛选、保存或删除后）
    @objc.python_method
    def reload(self, keep_id=None):
        if self.window is None:
            return
        chosen = self.label_menu.titleOfSelectedItem()
        self.label_menu.removeAllItems()
        self.label_menu.addItemWithTitle_('All labels')
        for label in notes_store.all_labels(notes_db):
            self.label_menu.addItemWithTitle_('#' + label)
        if chosen and self.label_menu.itemWithTitle_(chosen) is not None:
            self.label_menu.selectItemWithTitle_(chosen)
        label = self.label_menu.titleOfSelectedItem()
        label = None if label in (None, 'All labels') else label[1:]
        self.rows = notes_store.list_notes(notes_db, self.search.stringValue(), label)
        self.table.reloadData()
        total = len(notes_store.list_notes(notes_db))
        self.count.setStringValue_(f'{len(self.rows)} of {total} notes' if len(self.rows) != total
                                   else f'{total} note' + ('' if total == 1 else 's'))
        row = next((i for i, r in enumerate(self.rows) if r[0] == keep_id), 0)
        if self.rows:
            self.table.selectRowIndexes_byExtendingSelection_(AppKit.NSIndexSet.indexSetWithIndex_(row), False)
            self.table.scrollRowToVisible_(row)
        self.show_detail()

    @objc.python_method
    def show_detail(self):
        row = self.table.selectedRow()
        if 0 <= row < len(self.rows):
            note_id, text, created, labels = self.rows[row]
            tags = '  '.join('#' + l for l in (labels or '').split(',') if l)
            self.header.setStringValue_(fmt_time(created, long=True) + ('   ' + tags if tags else ''))
            self.paper.textStorage().setAttributedString_(paper_string(text))
            self.delete_button.setEnabled_(True)
        else:
            empty = 'No notes yet. Double-click Majima to write one.' if not self.search.stringValue() \
                and self.label_menu.indexOfSelectedItem() <= 0 else 'No notes match.'
            self.header.setStringValue_(empty)
            self.paper.textStorage().setAttributedString_(paper_string(''))
            self.delete_button.setEnabled_(False)

    def filter_(self, sender):
        self.reload()

    def deleteNote_(self, sender):
        row = self.table.selectedRow()
        if not 0 <= row < len(self.rows):
            return
        alert = AppKit.NSAlert.alloc().init()
        alert.setMessageText_('Delete this note?')
        alert.setInformativeText_(self.rows[row][1].split('\n')[0][:80])
        alert.addButtonWithTitle_('Delete')
        alert.addButtonWithTitle_('Cancel')
        if alert.runModal() == AppKit.NSAlertFirstButtonReturn:
            notes_store.delete_note(notes_db, self.rows[row][0])
            next_id = self.rows[row + 1][0] if row + 1 < len(self.rows) else (self.rows[row - 1][0] if row else None)
            self.reload(keep_id=next_id)

    # 列表的数据源和选择
    def numberOfRowsInTableView_(self, table):
        return len(self.rows)

    def tableView_objectValueForTableColumn_row_(self, table, column, row):
        note_id, text, created, labels = self.rows[row]
        ident = column.identifier()
        if ident == 'date':
            return fmt_time(created)
        if ident == 'note':
            return text.split('\n')[0]
        return ', '.join(l for l in (labels or '').split(',') if l)

    def tableViewSelectionDidChange_(self, notification):
        self.show_detail()

notebook = Notebook.alloc().init()

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

    def openNotebook_(self, sender):
        notebook.open()

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
menu.addItemWithTitle_action_keyEquivalent_("Notebook…", "openNotebook:", "b").setTarget_(controller)
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
