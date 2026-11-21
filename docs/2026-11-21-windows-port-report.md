# Work report: Windows port and first release (21 Nov 2026)

## Summary
MajimaVpet now runs on Windows as well as macOS, and the first public download, **v1.0.0 (Windows)**, is on
[GitHub Releases](https://github.com/JaneZhang1201/MajimaVpet/releases/tag/v1.0.0). All work is on the
`windows-port` branch; `main` is unchanged.

## What was done
| Area | Result |
|---|---|
| Windows version | `program/vpet_win.py`: the macOS app rebuilt with Qt (PySide6). Same animations, notes, Notebook, Pomodoro and settings |
| Windows differences | Tray icon instead of menu bar; Shift+Enter for a new line; Windows system sounds; data in `%APPDATA%\MajimaVpet`; keyboard hook records only *when* a key is pressed |
| Shared code | `majima_db.py` and `runtime_log.py` work on both systems; one version number in `program/version.py` |
| Setup | `requirements.txt` installs the right libraries per system; `MajimaVpet.cmd` starts the Windows version from source |
| Packaging | `packaging/build_windows.ps1` (PyInstaller zip) and `packaging/build_mac.sh` (py2app zip); `READ ME FIRST.txt` for users; draft download page |
| Release | Tag `v1.0.0` on `windows-port`, with `MajimaVpet-1.0.0-windows.zip` (36 MB) and its SHA-256 |

## Testing
| Check | Result |
|---|---|
| Runs from source on Windows 11 (150% display) | Passed |
| Click-through on transparent areas, drag, double-click → note page, typing animation | Passed (simulated input) |
| Note pages, Quick sticky, Notebook window, tray menu | Passed (screenshots) |
| Full Pomodoro cycle with short timers, alarm sound | Passed |
| Downloaded release zip matches its checksum | Passed |
| Packaged `MajimaVpet.exe` starts | **Not tested yet**: this session wasn't allowed to start it. Testing by hand next |
| Mac build script, Mac app after shared-code changes | **Not tested**: needs a Mac |
| Switching Windows virtual desktops, real sleep/wake | Not tested |

## Decisions
- Same repo, separate branch; release files live on GitHub Releases, not in the repo history.
- Unsigned builds for now: users see a one-time "Run anyway" (Windows) / "Open Anyway" (Mac) step.
- Free, unofficial fan work with a "not affiliated with SEGA" note.

## Next steps
1. Download the release zip on Windows, run `MajimaVpet.exe`, check Majima, tray menu, notes and a Pomodoro.
2. On the Mac: run `sh packaging/build_mac.sh`, test the app, add the zip to the v1.0.0 release.
3. Fill in the links in `packaging/download-page/index.html` and publish it on the personal webpage.
4. Merge `windows-port` into `main` when both versions are confirmed.
