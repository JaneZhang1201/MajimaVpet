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

Jot down words you meet while reading:

1. **Double-click** Majima. He stops where he is and switches to note-taking.
2. **Click him again** within 10 seconds to open a notebook page above him: yellow paper with ruled lines. If you don't, he goes back to normal.
3. Write the word or note (**⌥↩** starts a new line, for example for its meaning) and choose:
   - **Save** (Enter): kept in your notebook, with the time and labels recorded automatically. Add `#labels` anywhere in the text.
   - **Quick**: shown as a yellow sticky until you close it. It isn't stored, but its **Save** button keeps it after all.
   - **Esc**: discard.

While you write, Majima plays his typing animation as if he's taking dictation. Majima and the notebook move together: drag either one and the other follows. Focus goes back to the app you were reading.

To read your notes, choose **Notebook…** from the menu bar icon. Notes are listed newest first; you can search them (English or Chinese), filter by label, and read the full note on the right.

**Delete** moves a note to **Recently Deleted** (the last item in the label menu). From there you can **Restore** it or **Delete Forever**, or **Empty Recently Deleted**. Notes left there are removed for good after 30 days, and erased notes are overwritten inside the file.

Notes are stored only on your Mac, in `~/Library/Application Support/MajimaVpet/majima.db` (SQLite, with full-text search that also works for Chinese), and never in this repository. Older versions used `notes.db`; it is renamed automatically on first start.

## Your notes

**Where they are.** All your notes are saved in one file on your Mac: `~/Library/Application Support/MajimaVpet/majima.db`. They are never uploaded or added to this repository. To open the folder, in Finder press **⇧⌘G** and paste `~/Library/Application Support/MajimaVpet`.

**Organizing them.** Each note gets the date and time automatically. Type `#labels` anywhere in a note (for example `#reading`), then use the label menu in the Notebook window to see only those notes. Search finds English and Chinese text.

**Deleting and recovering.**
1. Select a note in the Notebook window and click **Delete**. It moves to **Recently Deleted**, the last item in the label menu.
2. Changed your mind? Open Recently Deleted, select the note and click **Restore**.
3. To erase it now, click **Delete Forever**, or **Empty Recently Deleted** to erase them all. This cannot be undone.
4. Notes left in Recently Deleted are erased automatically after 30 days (change `trash_days` to adjust).

**Backing up.** Time Machine backs the file up automatically. To keep your own copy, quit Majima and copy `majima.db` somewhere safe; to restore, quit Majima and put the copy back in the same folder.

**Moving to a new Mac.** Copy `majima.db` into the same folder on the new Mac before starting Majima.

**Reading them without Majima.** `majima.db` is a standard SQLite file. Any SQLite viewer (for example DB Browser for SQLite) can open it.

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
