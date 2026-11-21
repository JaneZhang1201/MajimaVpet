"""从 majima-idle.gif 的第一帧做 Windows 图标（.ico）：裁掉透明边，放进正方形，最近邻放大保持像素清晰

用法：python packaging/make_icon.py build/MajimaVpet.ico
"""
import os
import sys

from PIL import Image

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
out = sys.argv[1]

img = Image.open(os.path.join(root, 'majima-idle.gif'))
img.seek(0)
img = img.convert('RGBA')
img = img.crop(img.getchannel('A').getbbox())
side = max(img.size)
square = Image.new('RGBA', (side, side), (0, 0, 0, 0))
square.paste(img, ((side - img.width) // 2, side - img.height))
big = square.resize((256, 256), Image.NEAREST)
os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
big.save(out, sizes=[(s, s) for s in (16, 20, 24, 32, 48, 64, 128, 256)])
print('icon:', out)
