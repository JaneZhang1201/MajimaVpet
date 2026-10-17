# MajimaVpet

A tiny pixel-art Majima that lives on your macOS desktop. He idles and wanders when you're still, dances when you move the mouse, chats along while you type, and reacts when you pick him up.

<p>
  <img src="majima-idle.gif" alt="idle" width="80">
  <img src="majima-walk.gif" alt="walk" width="80">
  <img src="majima-shock.gif" alt="shock" width="80">
  <img src="majima-confuse.gif" alt="confuse" width="80">
  <img src="majima-change.gif" alt="change" width="80">
  <img src="majima-dance.gif" alt="dance" width="80">
</p>

<p>
  <img src="majima-typing.gif" alt="typing" width="160">
  <img src="majima-notetake.gif" alt="notetake" width="160">
</p>

## Behaviour

| Category | Animations | Trigger |
|---|---|---|
| **Steady** | idle, walk | Cursor still for 1.5s: idles and takes short walks (20–40px) in any direction, bouncing off screen edges |
| **Interactive** | shock → confuse | Drag him anywhere on screen: "!" while held, "?" after you drop him |
| **Showtime** | change → dance | Cursor moving: sparkle entrance, then dances until the cursor stops |
| **Typing** | typing | Keyboard in use: a speech bubble fills with "…" until you stop typing (0.5s) |
| **Notes** | notetake | Double-click Majima: he stops and gets ready to take a note (see below) |

The pet window floats above other windows and stays in the same spot on every desktop (Space), including full-screen apps and Mission Control. It doesn't even flicker while desktops slide. Clicks pass through its transparent areas.

## Notebook

### Write a note

**Double-click Majima → click him again (within 10s) → write → choose:**

| Key / button | Result |
|---|---|
| **Enter** or **Save** | Saved to your notebook, with date, time and `#labels` |
| **⌥↩** | New line (e.g. the word's meaning) |
| **Quick** | Yellow sticky on screen until you close it, not saved unless you click its **Save** |
| **Esc** | Discarded |

### Read your notes

**Menu bar icon → Notebook…** (⌘B)

- Find text with search box
- Label menu → `#label` → show only those notes

## Your notes

### Delete and recover

| To… | Do this |
|---|---|
| Delete | Notebook → select a note → **Delete** → moved to Recently Deleted |
| Recover | Label menu → **Recently Deleted** → select the note → **Restore** |
| Erase one now | Recently Deleted → select the note → **Delete Forever** (cannot be undone) |
| Erase all now | Recently Deleted → **Empty Recently Deleted** (cannot be undone) |
| Erase automatically | Left in Recently Deleted for 30 days (change with `trash_days`) |

### Where they are saved

- File → `~/Library/Application Support/MajimaVpet/majima.db` (only on your Mac, never uploaded)
- Open the folder → Finder → **⇧⌘G** → paste `~/Library/Application Support/MajimaVpet` → Return

### Back up, restore, move

| To… | Do this |
|---|---|
| Back up | Time Machine (automatic), or quit Majima → copy `majima.db` somewhere safe |
| Restore a backup | Quit Majima → put the copy back in the folder → start Majima |
| Move to a new Mac | Copy `majima.db` to the same folder on the new Mac → start Majima |
| Read without Majima | Open `majima.db` in any SQLite viewer (e.g. DB Browser for SQLite) |

Upgrading from an older version → `notes.db` is renamed to `majima.db` automatically.

## Menu bar

Click the little Majima icon in the menu bar:

- **Notebook…** (⌘B): browse, search and delete saved notes
- **Hide / Show Majima** (⌘H)
- **Pause / Resume** (⌘P): freezes the animation
- **Quit MajimaVpet** (⌘Q)

> On macOS 26+, if the icon doesn't appear, enable the app under **System Settings → Menu Bar → Allow in the Menu Bar**.

## Setup

Requires macOS and Python 3.

```bash
python3 -m venv program/venv
program/venv/bin/pip install -r program/requirements.txt
```

## Run

Double-click `MajimaVpet.app`, or from a terminal:

```bash
program/venv/bin/python program/vpet_mac.py
```

## Tweaks

Settings at the top of `program/vpet_mac.py`:

| Setting | Default | Meaning |
|---|---|---|
| `scale` | `1` | Sprite scale (nearest-neighbour, stays crisp) |
| `still_after` | `1.5` | Seconds of cursor stillness before leaving Showtime |
| `walk_speed` | `1` | Pixels per tick while walking |
| `walk_loops` | `(1, 2)` | Walk cycles per stroll (~20px each) |
| `dropped_loops` | `2` | Times the "?" plays after a drop |
| `typing_after` | `0.5` | Seconds after the last key press that still count as typing |
| `notetake_timeout` | `10.0` | Seconds to click Majima again after double-clicking, before he goes back to normal |
| `drag_threshold` | `3` | Pixels the mouse must move before a press becomes a drag |
| `trash_days` | `30` | Days a deleted note stays in Recently Deleted before it is removed for good |
| `fixed_on_desktop_switch` | `True` | Keep Majima and his notebook fixed while desktops slide, like the cursor (uses a private macOS API; falls back to a short flicker if unavailable) |
| `idle_tick` | `0.5` | Loop interval while paused or hidden (saves CPU) |
| `walk_move_every` | `2` | Move the window every N ticks while walking (moving a window costs more than changing frames) |

## Project layout

```
MajimaVpet.app/        macOS launcher bundle (runs program/vpet_mac.py with the venv)
program/vpet_mac.py    the pet: Cocoa window, animation state machine, menu bar item, note box, Notebook window
program/majima_db.py   shared local database (majima.db); each feature keeps its own tables
program/notes_store.py notebook tables: notes, labels, full-text search
majima-*.gif           sprite animations (80×92; typing and notetake are 160×92)
```
