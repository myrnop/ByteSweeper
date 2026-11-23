#!/usr/bin/env python3
import io
import math
import random
import struct
import sys
import wave
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk

CSS = """
.grid-tile {
    font-family: monospace;
    font-weight: bold;
    font-size: 14px;
    min-width: 38px;
    min-height: 38px;
    padding: 0;
    border-radius: 6px;
}
.tile-hidden {
    background-color: alpha(@window_fg_color, 0.08);
}
.tile-hidden:hover {
    background-color: alpha(@window_fg_color, 0.15);
}
.tile-safe {
    background-color: alpha(@window_fg_color, 0.02);
    color: @window_fg_color;
}
.tile-flagged {
    background-color: alpha(@warning_color, 0.25);
    color: @warning_color;
}
.tile-glitch {
    background-color: alpha(@error_color, 0.28);
    color: @error_color;
}
.tile-warp {
    background-color: alpha(@accent_color, 0.2);
    color: @accent_color;
}
.tile-scanned {
    border: 2px solid @accent_color;
}
.tile-cursor {
    outline: 2px solid @accent_color;
    outline-offset: -2px;
}
.num-1 { color: #3584e4; }
.num-2 { color: #2ec27e; }
.num-3 { color: #e5a50a; }
.num-4 { color: #ff7800; }
.num-5, .num-6, .num-7, .num-8 { color: #e01b24; }
"""

LEVELS = [
    {"name": "Sector 01: Boot Cache", "rows": 6, "cols": 6, "glitches": 5},
    {"name": "Sector 02: User Space", "rows": 8, "cols": 8, "glitches": 10},
    {"name": "Sector 03: Data Stream", "rows": 9, "cols": 9, "glitches": 14},
    {"name": "Sector 04: Cloud Relay", "rows": 10, "cols": 10, "glitches": 18},
    {"name": "Sector 05: Core Matrix", "rows": 12, "cols": 12, "glitches": 26},
]


class ArcadeAudio:
    """Generates retro 8-bit sound effects in memory without external audio files."""
    def __init__(self):
        self.muted = False
        self.sounds = {
            "click": self._generate_tone(freq=520, duration=0.04, volume=0.25),
            "flag": self._generate_tone(freq=340, duration=0.06, volume=0.3),
            "warp": self._generate_sweep(start_freq=400, end_freq=880, duration=0.12),
            "ability": self._generate_sweep(start_freq=600, end_freq=1200, duration=0.15),
            "hit": self._generate_sweep(start_freq=220, end_freq=75, duration=0.28, square=True),
            "win": self._generate_sweep(start_freq=440, end_freq=980, duration=0.28),
        }
        self._active_players = []

    def play(self, name):
        if self.muted:
            return
        gbytes = self.sounds.get(name)
        if not gbytes:
            return
        stream = Gio.MemoryInputStream.new_from_bytes(gbytes)
        media = Gtk.MediaFile.new_for_input_stream(stream)
        media.play()

        self._active_players.append(media)
        media.connect("notify::ended", self._on_ended)

    def _on_ended(self, media, _param):
        if media in self._active_players:
            self._active_players.remove(media)

    def _generate_tone(self, freq, duration, volume=0.3, sample_rate=22050):
        n_samples = int(sample_rate * duration)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            for i in range(n_samples):
                env = 1.0 - (i / n_samples)
                val = math.sin(2 * math.pi * freq * (i / sample_rate))
                sample = int(val * 32767 * volume * env)
                wf.writeframesraw(struct.pack("<h", sample))
        return GLib.Bytes.new(buf.getvalue())

    def _generate_sweep(self, start_freq, end_freq, duration, volume=0.3, square=False, sample_rate=22050):
        n_samples = int(sample_rate * duration)
        buf = io.BytesIO()
        phase = 0.0
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            for i in range(n_samples):
                progress = i / n_samples
                freq = start_freq + (end_freq - start_freq) * progress
                phase += (2 * math.pi * freq) / sample_rate
                raw = 1.0 if (square and math.sin(phase) >= 0) else (-1.0 if square else math.sin(phase))
                sample = int(raw * 32767 * volume * (1.0 - progress * 0.5))
                wf.writeframesraw(struct.pack("<h", sample))
        return GLib.Bytes.new(buf.getvalue())


class Tile:
    def __init__(self, r, c):
        self.r = r
        self.c = c
        self.is_glitch = False
        self.revealed = False
        self.flagged = False
        self.adjacent_glitches = 0
        self.warp_target = None
        self.button = None


class ByteSweeperWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="ByteSweeper")
        self.set_default_size(520, 660)

        self.audio = ArcadeAudio()
        self.level_idx = 0

        # Game & cursor state
        self.rows = 6
        self.cols = 6
        self.num_glitches = 5
        self.shields = 3
        self.energy = 2
        self.active_tool = None
        self.first_click = True
        self.game_over = False
        self.level_cleared = False
        self.tiles = []
        self.cursor_r = 0
        self.cursor_c = 0

        self._setup_ui()
        self.load_level(0)

    def _setup_ui(self):
        key_controller = Gtk.EventControllerKey()
        key_controller.connect("key-pressed", self._on_key_pressed)
        self.add_controller(key_controller)

        toolbar_view = Adw.ToolbarView()
        self.set_content(toolbar_view)

        # HeaderBar
        header = Adw.HeaderBar()
        restart_btn = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Retry Sector (R)")
        restart_btn.set_can_focus(False)
        restart_btn.connect("clicked", lambda _: self.load_level(self.level_idx))
        header.pack_start(restart_btn)

        about_btn = Gtk.Button(icon_name="help-about-symbolic", tooltip_text="About ByteSweeper")
        about_btn.set_can_focus(False)
        about_btn.connect("clicked", self._show_about)
        header.pack_end(about_btn)

        self.mute_btn = Gtk.ToggleButton(
            icon_name="audio-volume-high-symbolic",
            tooltip_text="Mute Sound Effects",
        )
        self.mute_btn.set_can_focus(False)
        self.mute_btn.connect("toggled", self._on_mute_toggled)
        header.pack_end(self.mute_btn)

        self.next_btn = Gtk.Button(label="Next Sector →", css_classes=["suggested-action"])
        self.next_btn.set_tooltip_text("Advance to the next sector (N)")
        self.next_btn.set_can_focus(False)
        self.next_btn.set_visible(False)
        self.next_btn.connect("clicked", lambda _: self._advance_level())
        header.pack_end(self.next_btn)

        self.sector_label = Gtk.Label(label="Sector 01: Boot Cache", css_classes=["title"])
        header.set_title_widget(self.sector_label)
        toolbar_view.add_top_bar(header)

        # Main container
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        main_box.set_margin_top(16)
        main_box.set_margin_bottom(16)
        main_box.set_margin_start(16)
        main_box.set_margin_end(16)
        toolbar_view.set_content(main_box)

        # Status Bar
        status_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        status_box.set_halign(Gtk.Align.CENTER)

        self.shields_label = Gtk.Label(css_classes=["heading"])
        self.energy_label = Gtk.Label(css_classes=["heading"])
        self.progress_label = Gtk.Label(css_classes=["dim-label"])
        status_box.append(self.shields_label)
        status_box.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        status_box.append(self.energy_label)
        status_box.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        status_box.append(self.progress_label)
        main_box.append(status_box)

        # Grid Container
        grid_frame = Gtk.Frame()
        grid_frame.set_halign(Gtk.Align.CENTER)
        grid_frame.set_valign(Gtk.Align.CENTER)
        self.grid = Gtk.Grid(row_spacing=4, column_spacing=4)
        self.grid.set_margin_top(8)
        self.grid.set_margin_bottom(8)
        self.grid.set_margin_start(8)
        self.grid.set_margin_end(8)
        grid_frame.set_child(self.grid)
        main_box.append(grid_frame)

        # Power-Ups Bar
        tools_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        tools_box.set_halign(Gtk.Align.CENTER)

        self.patch_btn = Gtk.ToggleButton(label="[1] Patch Tile (2⚡)")
        self.patch_btn.set_can_focus(False)
        self.patch_btn.set_tooltip_text("Safely clear 1 tile even if a Glitch is hiding there")
        self.patch_btn.connect("toggled", self._on_tool_toggled, "patch")

        self.radar_btn = Gtk.ToggleButton(label="[2] Radar Scan (3⚡)")
        self.radar_btn.set_can_focus(False)
        self.radar_btn.set_tooltip_text("Reveal any Glitches in a 3x3 area")
        self.radar_btn.connect("toggled", self._on_tool_toggled, "radar")

        tools_box.append(self.patch_btn)
        tools_box.append(self.radar_btn)
        main_box.append(tools_box)

        # Status Feed
        self.status_feed = Gtk.Label(
            label="Ready to sweep! Click any tile to begin.",
            css_classes=["dim-label"],
            wrap=True,
        )
        main_box.append(self.status_feed)

        # Controls Hint
        hint_label = Gtk.Label(
            label="Arrows/WASD: Move • Space: Sweep • F: Flag • R: Retry",
            css_classes=["caption", "dim-label"],
        )
        main_box.append(hint_label)

        # Creator Credit Footer
        credit_label = Gtk.Label(
            label="Made with ❤️ by Faisal Almalahy",
            css_classes=["caption", "dim-label"],
        )
        credit_label.set_margin_top(4)
        main_box.append(credit_label)

    def _show_about(self, _button):
        about = Adw.AboutDialog(
            application_name="ByteSweeper",
            application_icon="io.github.myrnop.ByteSweeper",
            developer_name="Faisal Almalahy",
            version="1.0.0",
            comments="Clear digital sectors and outsmart hidden glitches.\nMade with ❤️ by Faisal Almalahy.",
            license_type=Gtk.License.GPL_3_0,
            developers=["Faisal Almalahy"],
        )
        about.present(self)

    def _on_mute_toggled(self, button):
        self.audio.muted = button.get_active()
        if self.audio.muted:
            button.set_icon_name("audio-volume-muted-symbolic")
            button.set_tooltip_text("Unmute Sound Effects")
        else:
            button.set_icon_name("audio-volume-high-symbolic")
            button.set_tooltip_text("Mute Sound Effects")
            self.audio.play("click")

    def load_level(self, idx):
        self.level_idx = idx % len(LEVELS)
        cfg = LEVELS[self.level_idx]
        self.rows = cfg["rows"]
        self.cols = cfg["cols"]
        self.num_glitches = cfg["glitches"]
        self.sector_label.set_text(cfg["name"])

        self.shields = 3
        self.energy = 2
        self.active_tool = None
        self.first_click = True
        self.game_over = False
        self.level_cleared = False
        self.cursor_r = 0
        self.cursor_c = 0
        self.patch_btn.set_active(False)
        self.radar_btn.set_active(False)
        self.next_btn.set_visible(False)

        while child := self.grid.get_first_child():
            self.grid.remove(child)

        self.tiles = [[Tile(r, c) for c in range(self.cols)] for r in range(self.rows)]

        for r in range(self.rows):
            for c in range(self.cols):
                btn = Gtk.Button(css_classes=["grid-tile", "tile-hidden"])
                btn.set_can_focus(False)
                btn.connect("clicked", self._on_tile_left_click, r, c)

                right_click = Gtk.GestureClick(button=3)
                right_click.connect("pressed", self._on_tile_right_click, r, c)
                btn.add_controller(right_click)

                self.tiles[r][c].button = btn
                self.grid.attach(btn, c, r, 1, 1)

        self.tiles[0][0].button.add_css_class("tile-cursor")
        self._update_hud()
        self._say(f"Loaded {cfg['name']} — {self.num_glitches} Glitches detected.")

    def _advance_level(self):
        if self.level_idx + 1 < len(LEVELS):
            self.load_level(self.level_idx + 1)
        else:
            self.load_level(0)

    def _move_cursor(self, dr, dc):
        self.tiles[self.cursor_r][self.cursor_c].button.remove_css_class("tile-cursor")
        self.cursor_r = (self.cursor_r + dr) % self.rows
        self.cursor_c = (self.cursor_c + dc) % self.cols
        self.tiles[self.cursor_r][self.cursor_c].button.add_css_class("tile-cursor")

    def _set_cursor(self, r, c):
        self.tiles[self.cursor_r][self.cursor_c].button.remove_css_class("tile-cursor")
        self.cursor_r = r
        self.cursor_c = c
        self.tiles[self.cursor_r][self.cursor_c].button.add_css_class("tile-cursor")

    def _on_key_pressed(self, _controller, keyval, _keycode, _state):
        if keyval in (Gdk.KEY_Up, Gdk.KEY_w, Gdk.KEY_W, Gdk.KEY_k, Gdk.KEY_K):
            self._move_cursor(-1, 0)
            return True
        elif keyval in (Gdk.KEY_Down, Gdk.KEY_s, Gdk.KEY_S, Gdk.KEY_j, Gdk.KEY_J):
            self._move_cursor(1, 0)
            return True
        elif keyval in (Gdk.KEY_Left, Gdk.KEY_a, Gdk.KEY_A, Gdk.KEY_h, Gdk.KEY_H):
            self._move_cursor(0, -1)
            return True
        elif keyval in (Gdk.KEY_Right, Gdk.KEY_d, Gdk.KEY_D, Gdk.KEY_l, Gdk.KEY_L):
            self._move_cursor(0, 1)
            return True
        elif keyval in (Gdk.KEY_space, Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self._on_tile_left_click(None, self.cursor_r, self.cursor_c)
            return True
        elif keyval in (Gdk.KEY_f, Gdk.KEY_F, Gdk.KEY_m, Gdk.KEY_M):
            self._on_tile_right_click(None, 1, 0, 0, self.cursor_r, self.cursor_c)
            return True
        elif keyval == Gdk.KEY_1:
            if self.patch_btn.get_sensitive():
                self.patch_btn.set_active(not self.patch_btn.get_active())
            return True
        elif keyval == Gdk.KEY_2:
            if self.radar_btn.get_sensitive():
                self.radar_btn.set_active(not self.radar_btn.get_active())
            return True
        elif keyval in (Gdk.KEY_r, Gdk.KEY_R):
            self.load_level(self.level_idx)
            return True
        elif keyval in (Gdk.KEY_n, Gdk.KEY_N) and self.level_cleared:
            self._advance_level()
            return True

        return False

    def _populate_board(self, safe_r, safe_c):
        forbidden = {
            (r, c)
            for r in range(safe_r - 1, safe_r + 2)
            for c in range(safe_c - 1, safe_c + 2)
            if 0 <= r < self.rows and 0 <= c < self.cols
        }
        candidates = [
            (r, c)
            for r in range(self.rows)
            for c in range(self.cols)
            if (r, c) not in forbidden
        ]

        for r, c in random.sample(candidates, self.num_glitches):
            self.tiles[r][c].is_glitch = True

        for r in range(self.rows):
            for c in range(self.cols):
                if not self.tiles[r][c].is_glitch:
                    count = sum(
                        1
                        for nr, nc in self._neighbors(r, c)
                        if self.tiles[nr][nc].is_glitch
                    )
                    self.tiles[r][c].adjacent_glitches = count

        safe_tiles = [
            (r, c)
            for r in range(self.rows)
            for c in range(self.cols)
            if not self.tiles[r][c].is_glitch and (r, c) != (safe_r, safe_c)
        ]
        if len(safe_tiles) >= 2:
            a, b = random.sample(safe_tiles, 2)
            self.tiles[a[0]][a[1]].warp_target = b
            self.tiles[b[0]][b[1]].warp_target = a

    def _neighbors(self, r, c):
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                if dr == 0 and dc == 0:
                    continue
                nr, nc = r + dr, c + dc
                if 0 <= nr < self.rows and 0 <= nc < self.cols:
                    yield nr, nc

    def _on_tool_toggled(self, button, tool_name):
        if button.get_active():
            if tool_name == "patch" and self.energy < 2:
                button.set_active(False)
                self._say("Need 2⚡ Energy to use Patch Tile.")
                return
            if tool_name == "radar" and self.energy < 3:
                button.set_active(False)
                self._say("Need 3⚡ Energy to use Radar Scan.")
                return

            self.active_tool = tool_name
            if tool_name == "patch":
                self.radar_btn.set_active(False)
                self._say("Patch active: click any hidden tile to safely clear it.")
            else:
                self.patch_btn.set_active(False)
                self._say("Radar active: click a center tile to scan a 3x3 area.")
        elif self.active_tool == tool_name:
            self.active_tool = None

    def _on_tile_left_click(self, _button, r, c):
        self._set_cursor(r, c)
        if self.game_over:
            return

        if self.first_click:
            self._populate_board(r, c)
            self.first_click = False

        if self.active_tool == "patch":
            self._execute_patch(r, c)
            return
        elif self.active_tool == "radar":
            self._execute_radar(r, c)
            return

        tile = self.tiles[r][c]
        if tile.revealed or tile.flagged:
            return

        self._reveal_tile(r, c, play_sound=True)
        self._check_win()

    def _on_tile_right_click(self, _gesture, _n_press, _x, _y, r, c):
        self._set_cursor(r, c)
        if self.game_over or self.first_click:
            return

        tile = self.tiles[r][c]
        if tile.revealed:
            return

        self.audio.play("flag")
        tile.flagged = not tile.flagged
        if tile.flagged:
            tile.button.set_label("🚩")
            tile.button.set_css_classes(["grid-tile", "tile-flagged", "tile-cursor"])
            self._say(f"Flagged tile ({r + 1}, {c + 1}).")
        else:
            tile.button.set_label("")
            tile.button.set_css_classes(["grid-tile", "tile-hidden", "tile-cursor"])
            self._say(f"Removed flag at ({r + 1}, {c + 1}).")

        self._check_win()

    def _execute_patch(self, r, c):
        tile = self.tiles[r][c]
        if tile.revealed:
            return

        self.audio.play("ability")
        self.energy -= 2
        self.active_tool = None
        self.patch_btn.set_active(False)

        if tile.is_glitch:
            tile.is_glitch = False
            tile.revealed = True
            tile.button.set_label("✓")
            tile.button.set_css_classes(["grid-tile", "tile-safe", "tile-cursor"])
            self._say(f"Patched a Glitch at ({r + 1}, {c + 1})!")
            for nr, nc in self._neighbors(r, c):
                nt = self.tiles[nr][nc]
                if not nt.is_glitch:
                    nt.adjacent_glitches = max(0, nt.adjacent_glitches - 1)
                    if nt.revealed and nt.adjacent_glitches > 0:
                        nt.button.set_label(str(nt.adjacent_glitches))
                    elif nt.revealed and nt.adjacent_glitches == 0:
                        nt.button.set_label("")
        else:
            self._say(f"Tile ({r + 1}, {c + 1}) was clean—sweeping normally.")
            self._reveal_tile(r, c, play_sound=False)

        self._update_hud()
        self._check_win()

    def _execute_radar(self, r, c):
        self.audio.play("ability")
        self.energy -= 3
        self.active_tool = None
        self.radar_btn.set_active(False)

        found = 0
        area = [(r, c)] + list(self._neighbors(r, c))
        for ar, ac in area:
            t = self.tiles[ar][ac]
            if not t.revealed:
                t.button.add_css_class("tile-scanned")
                if t.is_glitch:
                    found += 1
                    t.button.set_label("!")

        self._say(f"Radar detected {found} Glitch(es) around ({r + 1}, {c + 1}).")
        self._update_hud()

    def _reveal_tile(self, r, c, play_sound=False):
        tile = self.tiles[r][c]
        if tile.revealed or tile.flagged:
            return

        tile.revealed = True
        is_cursor = (r == self.cursor_r and c == self.cursor_c)

        if tile.is_glitch:
            self.audio.play("hit")
            self.shields -= 1
            tile.button.set_label("👾")
            classes = ["grid-tile", "tile-glitch"]
            if is_cursor:
                classes.append("tile-cursor")
            tile.button.set_css_classes(classes)
            self._say(f"Hit a Glitch at ({r + 1}, {c + 1})! {self.shields} Shield(s) left.")
            self._update_hud()
            if self.shields <= 0:
                self._end_game(won=False)
            return

        revealed_count = sum(
            1 for row in self.tiles for t in row if t.revealed and not t.is_glitch
        )
        if revealed_count % 5 == 0:
            self.energy += 1

        if tile.warp_target:
            if play_sound:
                self.audio.play("warp")
            tr, tc = tile.warp_target
            tile.button.set_label("⇄")
            classes = ["grid-tile", "tile-warp"]
            if is_cursor:
                classes.append("tile-cursor")
            tile.button.set_css_classes(classes)
            self._say(f"Warp Node jumped from ({r + 1}, {c + 1}) to ({tr + 1}, {tc + 1})!")
            if not self.tiles[tr][tc].revealed:
                self._reveal_tile(tr, tc, play_sound=False)
            self._update_hud()
            return

        if play_sound:
            self.audio.play("click")

        classes = ["grid-tile", "tile-safe"]
        if is_cursor:
            classes.append("tile-cursor")

        if tile.adjacent_glitches > 0:
            tile.button.set_label(str(tile.adjacent_glitches))
            classes.append(f"num-{tile.adjacent_glitches}")
        else:
            tile.button.set_label("")
            for nr, nc in self._neighbors(r, c):
                self._reveal_tile(nr, nc, play_sound=False)

        tile.button.set_css_classes(classes)
        self._update_hud()

    def _update_hud(self):
        shields_str = "🛡️ " * self.shields + "○ " * (3 - self.shields)
        self.shields_label.set_text(f"Shields: {shields_str.strip()}")
        self.energy_label.set_text(f"Energy: {self.energy}⚡")
        self.progress_label.set_text(f"Stage {self.level_idx + 1}/{len(LEVELS)}")
        self.patch_btn.set_sensitive(self.energy >= 2 and not self.game_over)
        self.radar_btn.set_sensitive(self.energy >= 3 and not self.game_over)

    def _say(self, message):
        self.status_feed.set_text(message)

    def _check_win(self):
        if self.game_over:
            return
        safe_unrevealed = any(
            not t.is_glitch and not t.revealed
            for row in self.tiles
            for t in row
        )
        if not safe_unrevealed:
            self._end_game(won=True)

    def _end_game(self, won):
        self.game_over = True
        self._update_hud()
        for r, row in enumerate(self.tiles):
            for c, t in enumerate(row):
                if t.is_glitch and not t.revealed:
                    t.button.set_label("👾")
                    classes = ["grid-tile", "tile-glitch"]
                    if r == self.cursor_r and c == self.cursor_c:
                        classes.append("tile-cursor")
                    t.button.set_css_classes(classes)

        if won:
            self.level_cleared = True
            self.audio.play("win")
            if self.level_idx + 1 < len(LEVELS):
                self.next_btn.set_label("Next Sector →")
                self.next_btn.set_visible(True)
                self._say("Sector Cleared! Click 'Next Sector →' or press N to continue.")
            else:
                self.next_btn.set_label("Play Again ↻")
                self.next_btn.set_visible(True)
                self._say("All 5 Sectors Cleared! You mastered ByteSweeper!")
        else:
            self._say("Out of Shields! Click the refresh icon or press R to try again.")


class ByteSweeperApp(Adw.Application):
    def __init__(self):
        super().__init__(
            application_id="io.github.myrnop.ByteSweeper",
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )

    def do_activate(self):
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS.encode())
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        win = ByteSweeperWindow(self)
        win.present()


if __name__ == "__main__":
    app = ByteSweeperApp()
    sys.exit(app.run(sys.argv))