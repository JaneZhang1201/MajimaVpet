"""py2app 设置：把 program/vpet_mac.py 打包成独立的 MajimaVpet.app（自带 Python）

不要直接运行，用 packaging/build_mac.sh。
GIF 放在 App 的 Contents/Resources/ 根目录，字体放在 Contents/Resources/fonts/（vpet_mac.py 从 RESOURCEPATH 找它们）。
"""
import glob
import os
import sys

from setuptools import setup

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(root, 'program'))
from version import APP_VERSION  # noqa: E402

setup(
    name='MajimaVpet',
    version=APP_VERSION,
    app=[os.path.join(root, 'program', 'vpet_mac.py')],
    data_files=[
        ('', sorted(glob.glob(os.path.join(root, 'majima-*.gif')))),
        ('fonts', [os.path.join(root, 'fonts', 'Silkscreen-Bold.ttf'), os.path.join(root, 'fonts', 'OFL.txt')]),
    ],
    options={'py2app': {
        'iconfile': os.path.join(root, 'MajimaVpet.app', 'Contents', 'Resources', 'AppIcon.icns'),
        'includes': ['majima_db', 'notes_store', 'pomodoro_store', 'runtime_log', 'version'],
        'excludes': ['PySide6', 'tkinter'],
        'plist': {
            'CFBundleName': 'MajimaVpet',
            'CFBundleDisplayName': 'MajimaVpet',
            'CFBundleIdentifier': 'local.orange.majimavpet',
            'CFBundleShortVersionString': APP_VERSION,
            'CFBundleVersion': APP_VERSION,
            'LSUIElement': True,  # 只有菜单栏图标，不在 Dock 显示
            'NSHumanReadableCopyright': 'Unofficial fan work, not affiliated with SEGA.',
        },
    }},
)
