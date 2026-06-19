import os
import time
import subprocess
import json
import threading
import shutil
from PIL import Image, ImageDraw, ImageFont
from loguru import logger as log

# Import StreamController base classes
from src.backend.PluginManager.ActionCore import ActionCore
from src.backend.PluginManager.EventAssigner import EventAssigner
from src.backend.DeckManagement.InputIdentifier import Input

# Import GTK modules
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw

# Preset cyber-neon themes
THEMES = {
    "Cyan":   {"line": (0, 240, 255, 255), "fill": (0, 240, 255, 40), "dim": (0, 160, 180, 255)},
    "Green":  {"line": (118, 185, 0, 255),   "fill": (118, 185, 0, 40),   "dim": (90, 140, 0, 255)},
    "Red":    {"line": (255, 59, 48, 255),   "fill": (255, 59, 48, 40),   "dim": (200, 40, 30, 255)},
    "Blue":   {"line": (0, 122, 255, 255),   "fill": (0, 122, 255, 40),   "dim": (0, 90, 200, 255)},
    "Purple": {"line": (175, 82, 222, 255),  "fill": (175, 82, 222, 40),  "dim": (130, 60, 170, 255)},
    "Orange": {"line": (255, 149, 0, 255),   "fill": (255, 149, 0, 40),   "dim": (200, 110, 0, 255)}
}

class NvtopAction(ActionCore):
    # Class-level cache to share nvtop data across multiple button instances
    _cached_data = None
    _cached_time = 0
    _cache_lock = threading.Lock()
    _nvtop_cmd = None

    @classmethod
    def get_gpu_data(cls):
        now = time.time()
        with cls._cache_lock:
            # If cache is valid (less than 0.8 seconds old), return it
            if cls._cached_data is not None and (now - cls._cached_time) < 0.8:
                return cls._cached_data
            
            # Determine the appropriate command to run nvtop
            if cls._nvtop_cmd is None:
                if shutil.which("nvtop"):
                    cls._nvtop_cmd = ["nvtop", "-s"]
                    log.info("NvtopAction: Found native 'nvtop'")
                elif shutil.which("flatpak-spawn"):
                    cls._nvtop_cmd = ["flatpak-spawn", "--host", "--directory=/", "nvtop", "-s"]
                    log.info("NvtopAction: 'nvtop' not found, falling back to 'flatpak-spawn --host --directory=/ nvtop -s'")
                else:
                    cls._nvtop_cmd = ["nvtop", "-s"]
                    log.warning("NvtopAction: Neither 'nvtop' nor 'flatpak-spawn' found in PATH. Using default fallback.")

            # Fetch fresh data from nvtop snapshot
            try:
                result = subprocess.run(cls._nvtop_cmd, capture_output=True, text=True, timeout=1.5)
                if result.returncode == 0:
                    data = json.loads(result.stdout)
                    cls._cached_data = data
                    cls._cached_time = now
                    return data
                else:
                    log.error(f"NvtopAction: Command {cls._nvtop_cmd} failed with exit code {result.returncode}. Stderr: {result.stderr.strip()}")
            except Exception as e:
                log.error(f"NvtopAction: Failed to run {cls._nvtop_cmd}: {e}")
        return cls._cached_data

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.has_configuration = True
        
        # Max points to display in sparkline graphs
        self.max_history = 20
        self.history = {
            'gpu_util': [],
            'mem_util': [],
            'temp': [],
            'power_draw': [],
            'gpu_clock': []
        }
        self.current_view_idx = 0
        self.last_update_time = 0
        self._updating = False

        # Register event assigners for key/dial up events to cycle views
        self.add_event_assigner(EventAssigner(
            id="Key Up",
            ui_label="Key Up",
            default_events=[Input.Key.Events.UP],
            callback=self.on_key_up
        ))
        self.add_event_assigner(EventAssigner(
            id="Dial Up",
            ui_label="Dial Up",
            default_events=[Input.Dial.Events.UP],
            callback=self.on_key_up
        ))

    def on_ready(self) -> None:
        settings = self.get_settings()
        if settings is None:
            settings = {}
            
        settings.setdefault("selected_gpu_index", 0)
        settings.setdefault("color_theme", "Cyan")
        settings.setdefault("view_mode", "Cycle on Press")
        settings.setdefault("update_interval", 1)
        settings.setdefault("current_view_idx", 0)
        self.set_settings(settings)
        
        self.current_view_idx = settings.get("current_view_idx", 0)
        self.update_display(force=True)

    def on_tick(self) -> None:
        settings = self.get_settings()
        interval = int(settings.get("update_interval", 1))
        now = time.time()
        
        # Poll nvtop and redraw the button image
        if now - self.last_update_time >= interval - 0.1:
            self.update_display()
            self.last_update_time = now

    def on_key_up(self, *args, **kwargs) -> None:
        settings = self.get_settings()
        view_mode = settings.get("view_mode", "Cycle on Press")
        if view_mode == "Cycle on Press":
            # Switch views (5 individual metrics + 1 summary cockpit view)
            self.current_view_idx = (self.current_view_idx + 1) % 6
            settings["current_view_idx"] = self.current_view_idx
            self.set_settings(settings)
            self.update_display(force=True)

    def parse_nvtop_val(self, val_str, suffix):
        if val_str is None:
            return None
        val_str = str(val_str).strip()
        if val_str.lower() in ("null", "none", ""):
            return None
        val_str = val_str.replace(suffix, "").strip()
        try:
            return float(val_str)
        except ValueError:
            import re
            match = re.match(r"^([0-9.]+)", val_str)
            if match:
                try:
                    return float(match.group(1))
                except ValueError:
                    pass
            return None

    def parse_mem_to_bytes(self, val_str):
        if val_str is None:
            return None
        val_str = str(val_str).strip()
        if val_str.lower() in ("null", "none", ""):
            return None
        import re
        match = re.match(r"^([0-9.]+)\s*([a-zA-Z]*)$", val_str)
        if not match:
            return None
        num_str, unit = match.groups()
        try:
            val = float(num_str)
        except ValueError:
            return None
        unit = unit.upper()
        if unit == "GB":
            return val * (1024 ** 3)
        elif unit == "MB":
            return val * (1024 ** 2)
        elif unit == "KB":
            return val * 1024
        return val

    def update_display(self, force=False) -> None:
        if self._updating:
            return
        self._updating = True
        
        try:
            gpu_data = self.get_gpu_data()
            if not gpu_data:
                self.draw_error_image("nvtop error")
                return

            settings = self.get_settings()
            gpu_idx = int(settings.get("selected_gpu_index", 0))
            if gpu_idx >= len(gpu_data):
                gpu_idx = 0
                
            gpu = gpu_data[gpu_idx]

            # Parse stats
            gpu_util = self.parse_nvtop_val(gpu.get("gpu_util"), "%")
            if gpu_util is None:
                gpu_util = 0.0

            used_bytes = self.parse_mem_to_bytes(gpu.get("mem_used"))
            total_bytes = self.parse_mem_to_bytes(gpu.get("mem_total"))
            if used_bytes is not None and total_bytes is not None and total_bytes > 0:
                mem_util = (used_bytes / total_bytes) * 100
            else:
                mem_util = self.parse_nvtop_val(gpu.get("mem_util"), "%")
                if mem_util is None:
                    mem_util = 0.0

            temp = self.parse_nvtop_val(gpu.get("temp"), "C")
            if temp is None:
                temp = self.parse_nvtop_val(gpu.get("temp"), "F")
                if temp is None:
                    temp = 0.0

            power_draw = self.parse_nvtop_val(gpu.get("power_draw"), "W")
            if power_draw is None:
                power_draw = 0.0

            gpu_clock = self.parse_nvtop_val(gpu.get("gpu_clock"), "MHz")
            if gpu_clock is None:
                gpu_clock = 0.0

            # Add to rolling history buffers
            self.history['gpu_util'].append(gpu_util)
            self.history['mem_util'].append(mem_util)
            self.history['temp'].append(temp)
            self.history['power_draw'].append(power_draw)
            self.history['gpu_clock'].append(gpu_clock)

            for key in self.history:
                if len(self.history[key]) > self.max_history:
                    self.history[key].pop(0)

            # Determine view state
            view_mode = settings.get("view_mode", "Cycle on Press")
            if view_mode == "Cycle on Press":
                active_view = self.current_view_idx
            else:
                view_modes = ["GPU Util %", "Mem GB", "Temp °C", "Wattage W", "Clock Speed", "Summary"]
                if view_mode in view_modes:
                    active_view = view_modes.index(view_mode)
                elif view_mode in ("Memory %", "Memory GB"):
                    active_view = 1
                else:
                    active_view = 0

            # Get deck dimensions
            try:
                size = self.deck_controller.deck.key_image_format()["size"]
            except Exception:
                size = (72, 72)

            # Make the entire view 10% larger
            W = int(size[0] * 1.1)
            H = int(size[1] * 1.1)
            scaled_size = (W, H)

            # Render key image
            img = self.render_key_image(scaled_size, active_view, gpu_util, mem_util, temp, power_draw, gpu_clock, used_bytes, total_bytes)
            self.set_media(image=img)
            
        except Exception as e:
            log.error(f"NvtopAction: Error rendering display: {e}")
            self.draw_error_image("draw error")
        finally:
            self._updating = False

    def get_font(self, size, bold=False):
        font_paths = [
            "/usr/share/fonts/google-droid-sans-fonts/DroidSans-Bold.ttf" if bold else "/usr/share/fonts/google-droid-sans-fonts/DroidSans.ttf",
            "/usr/share/fonts/adwaita-sans-fonts/AdwaitaSans-Regular.ttf",
            "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/liberation/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/liberation/LiberationSans-Regular.ttf",
        ]
        for path in font_paths:
            if os.path.exists(path):
                try:
                    return ImageFont.try_load(path) if hasattr(ImageFont, "try_load") else ImageFont.truetype(path, size)
                except Exception:
                    pass
        return ImageFont.load_default()

    def render_key_image(self, size, view_idx, gpu_util, mem_util, temp, power_draw, gpu_clock, used_bytes, total_bytes):
        W, H = size
        img = Image.new("RGBA", (W, H), (18, 18, 20, 255))
        draw = ImageDraw.Draw(img)

        settings = self.get_settings()
        theme_name = settings.get("color_theme", "Cyan")
        theme = THEMES.get(theme_name, THEMES["Cyan"])

        # Border
        draw.rounded_rectangle([1, 1, W - 2, H - 2], radius=6, outline=(42, 45, 53, 255), width=1)

        if view_idx == 5:
            # SUMMARY VIEW: 2x2 instrument panel
            # Move the summary view mode higher so it aligns with the top
            shift = int(H * 0.08)
            divider_y = H // 2 - shift
            cy_top = H // 4 - shift
            cy_bottom = 3 * H // 4 - shift

            draw.line([W // 2, int(4 * 1.1), W // 2, H - int(4 * 1.1)], fill=(30, 32, 38, 255), width=1)
            draw.line([int(4 * 1.1), divider_y, W - int(4 * 1.1), divider_y], fill=(30, 32, 38, 255), width=1)

            font_lbl = self.get_font(int(H * 0.12 * 1.1), bold=False)
            font_val = self.get_font(int(H * 0.18 * 1.1), bold=True)

            self.draw_quadrant(draw, font_lbl, font_val, "GPU", f"{gpu_util:.0f}%", W // 4, cy_top, theme["dim"])
            self.draw_quadrant(draw, font_lbl, font_val, "MEM", f"{mem_util:.0f}%", 3 * W // 4, cy_top, theme["dim"])
            self.draw_quadrant(draw, font_lbl, font_val, "TMP", f"{temp:.0f}°", W // 4, cy_bottom, theme["dim"])
            self.draw_quadrant(draw, font_lbl, font_val, "PWR", f"{power_draw:.0f}W", 3 * W // 4, cy_bottom, theme["dim"])
        else:
            # SPARKLINE GRAPH VIEW
            hist_keys = ['gpu_util', 'mem_util', 'temp', 'power_draw', 'gpu_clock']
            hist_key = hist_keys[view_idx]
            history_data = self.history[hist_key]

            labels = ["GPU UTIL", "MEM GB", "GPU TEMP", "GPU PWR", "GPU CLK"]
            
            # Dynamic / static graph scaling limits
            if view_idx in (0, 1):
                max_limit = 100.0
            elif view_idx == 2:
                max_limit = 100.0  # Celsius ceiling
            elif view_idx == 3:
                max_limit = max(max(history_data) if history_data else 50.0, 100.0)
            else:
                max_limit = max(max(history_data) if history_data else 1000.0, 2000.0)

            # Format primary display value string
            val_str = ""
            if view_idx == 0:
                val_str = f"{gpu_util:.0f}%"
            elif view_idx == 1:
                if used_bytes is not None and total_bytes is not None:
                    used_gb = used_bytes / (1024 ** 3)
                    total_gb = total_bytes / (1024 ** 3)
                    val_str = f"{used_gb:.1f}/{total_gb:.0f}G"
                else:
                    val_str = f"{mem_util:.0f}%"
            elif view_idx == 2:
                val_str = f"{temp:.0f}°C"
            elif view_idx == 3:
                val_str = f"{power_draw:.0f} W"
            elif view_idx == 4:
                if gpu_clock >= 1000:
                    val_str = f"{gpu_clock/1000:.2f} GHz"
                else:
                    val_str = f"{gpu_clock:.0f} MHz"

            # Draw Label
            font_lbl = self.get_font(int(H * 0.14 * 1.1), bold=True)
            lbl_txt = labels[view_idx]
            bbox_lbl = draw.textbbox((0, 0), lbl_txt, font=font_lbl)
            w_lbl = bbox_lbl[2] - bbox_lbl[0]
            draw.text(((W - w_lbl) / 2, int(H * 0.1)), lbl_txt, fill=theme["dim"], font=font_lbl)

            # Draw Large Center Value
            if view_idx == 4:
                if gpu_clock >= 1000:
                    val_val = f"{gpu_clock/1000:.2f}"
                    val_unit = "GHz"
                else:
                    val_val = f"{gpu_clock:.0f}"
                    val_unit = "MHz"

                # Draw Value
                font_val = self.get_font(int(H * 0.24 * 1.1), bold=True)
                bbox_val = draw.textbbox((0, 0), val_val, font=font_val)
                w_val = bbox_val[2] - bbox_val[0]
                h_val = bbox_val[3] - bbox_val[1]
                gy_start = H - int(24 * 1.1)
                val_y = gy_start // 2 - h_val // 2 + 1
                draw.text(((W - w_val) / 2, val_y), val_val, fill=(255, 255, 255, 255), font=font_val)

                # Draw Unit
                font_unit = self.get_font(int(H * 0.14 * 1.1), bold=True)
                bbox_unit = draw.textbbox((0, 0), val_unit, font=font_unit)
                w_unit = bbox_unit[2] - bbox_unit[0]
                h_unit = bbox_unit[3] - bbox_unit[1]
                unit_y = val_y + h_val + int(8 * 1.1)
                draw.text(((W - w_unit) / 2, unit_y), val_unit, fill=theme["dim"], font=font_unit)
            else:
                font_size = int(H * 0.24 * 1.1)
                if view_idx == 1:
                    font_size = int(H * 0.19 * 1.1)
                font_val = self.get_font(font_size, bold=True)
                bbox_val = draw.textbbox((0, 0), val_str, font=font_val)
                w_val = bbox_val[2] - bbox_val[0]
                h_val = bbox_val[3] - bbox_val[1]
                draw.text(((W - w_val) / 2, (H - h_val) / 2 - int(H * 0.05)), val_str, fill=(255, 255, 255, 255), font=font_val)

            # Draw Sparkline
            self.draw_sparkline(draw, W, H, history_data, max_limit, theme)

        return img

    def draw_quadrant(self, draw, font_lbl, font_val, label, value, cx, cy, label_color):
        bbox_lbl = draw.textbbox((0, 0), label, font=font_lbl)
        w_lbl = bbox_lbl[2] - bbox_lbl[0]
        h_lbl = bbox_lbl[3] - bbox_lbl[1]
        draw.text((cx - w_lbl / 2, cy - h_lbl - 2), label, fill=label_color, font=font_lbl)

        bbox_val = draw.textbbox((0, 0), value, font=font_val)
        w_val = bbox_val[2] - bbox_val[0]
        draw.text((cx - w_val / 2, cy + 2), value, fill=(255, 255, 255, 255), font=font_val)

    def draw_sparkline(self, draw, W, H, history, max_limit, theme):
        if not history:
            return

        gx_start = int(4 * 1.1)
        gx_end = W - int(4 * 1.1)
        gy_start = H - int(24 * 1.1)
        gy_end = H - int(4 * 1.1)
        graph_w = gx_end - gx_start
        graph_h = gy_end - gy_start

        points_to_draw = list(history)
        if len(points_to_draw) < self.max_history:
            points_to_draw = [0.0] * (self.max_history - len(points_to_draw)) + points_to_draw

        line_points = []
        for i, val in enumerate(points_to_draw):
            x = gx_start + (i / (self.max_history - 1)) * graph_w
            val = max(0.0, min(float(val), float(max_limit)))
            y = gy_end - (val / max_limit) * graph_h
            line_points.append((x, y))

        area_points = [(gx_start, gy_end)] + line_points + [(gx_end, gy_end)]
        draw.polygon(area_points, fill=theme["fill"])
        draw.line(line_points, fill=theme["line"], width=2)

    def draw_error_image(self, message):
        try:
            size = self.deck_controller.deck.key_image_format()["size"]
        except Exception:
            size = (72, 72)
        W = int(size[0] * 1.1)
        H = int(size[1] * 1.1)
        img = Image.new("RGBA", (W, H), (30, 10, 10, 255))
        draw = ImageDraw.Draw(img)

        draw.rounded_rectangle([1, 1, W - 2, H - 2], radius=6, outline=(255, 50, 50, 255), width=1)

        font_title = self.get_font(int(H * 0.16 * 1.1), bold=True)
        bbox_title = draw.textbbox((0, 0), "GPU ERROR", font=font_title)
        w_title = bbox_title[2] - bbox_title[0]
        draw.text(((W - w_title) / 2, int(H * 0.2)), "GPU ERROR", fill=(255, 50, 50, 255), font=font_title)

        font_msg = self.get_font(int(H * 0.12 * 1.1), bold=False)
        bbox_msg = draw.textbbox((0, 0), message, font=font_msg)
        w_msg = bbox_msg[2] - bbox_msg[0]
        draw.text(((W - w_msg) / 2, int(H * 0.55)), message, fill=(255, 255, 255, 255), font=font_msg)

        self.set_media(image=img)

    def get_config_rows(self) -> "list[Adw.PreferencesRow]":
        settings = self.get_settings()
        
        # 1. GPU selector
        self.gpu_model = Gtk.StringList()
        self.gpu_selector = Adw.ComboRow(
            model=self.gpu_model,
            title="Select GPU Device",
            subtitle="Choose which GPU to monitor"
        )
        
        gpu_data = self.get_gpu_data()
        if gpu_data:
            for i, gpu in enumerate(gpu_data):
                self.gpu_model.append(gpu.get("device_name", f"GPU {i}"))
        else:
            self.gpu_model.append("GPU 0 (No device found)")
            
        selected_gpu = settings.get("selected_gpu_index", 0)
        if selected_gpu < self.gpu_model.get_n_items():
            self.gpu_selector.set_selected(selected_gpu)
        self.gpu_selector.connect("notify::selected", self.on_change_gpu)

        # 2. Theme selector
        self.theme_model = Gtk.StringList()
        self.theme_selector = Adw.ComboRow(
            model=self.theme_model,
            title="Color Theme",
            subtitle="Select the color scheme for sparklines and labels"
        )
        themes = ["Cyan", "Green", "Red", "Blue", "Purple", "Orange"]
        for t in themes:
            self.theme_model.append(t)
            
        current_theme = settings.get("color_theme", "Cyan")
        if current_theme in themes:
            self.theme_selector.set_selected(themes.index(current_theme))
        self.theme_selector.connect("notify::selected", self.on_change_theme)

        # 3. View Mode selector
        self.view_model_list = Gtk.StringList()
        self.view_selector = Adw.ComboRow(
            model=self.view_model_list,
            title="View Mode",
            subtitle="Select which statistics to display"
        )
        view_modes = ["Cycle on Press", "GPU Util %", "Mem GB", "Temp °C", "Wattage W", "Clock Speed", "Summary"]
        for v in view_modes:
            self.view_model_list.append(v)
            
        current_view = settings.get("view_mode", "Cycle on Press")
        if current_view in view_modes:
            self.view_selector.set_selected(view_modes.index(current_view))
        elif current_view in ("Memory %", "Memory GB"):
            self.view_selector.set_selected(view_modes.index("Mem GB"))
        self.view_selector.connect("notify::selected", self.on_change_view_mode)

        # 4. Update Interval selector
        self.interval_model = Gtk.StringList()
        self.interval_selector = Adw.ComboRow(
            model=self.interval_model,
            title="Update Interval",
            subtitle="Choose statistics polling frequency"
        )
        intervals = ["1 second", "2 seconds", "5 seconds", "10 seconds"]
        for i in intervals:
            self.interval_model.append(i)
            
        current_interval = int(settings.get("update_interval", 1))
        interval_map = {1: 0, 2: 1, 5: 2, 10: 3}
        self.interval_selector.set_selected(interval_map.get(current_interval, 0))
        self.interval_selector.connect("notify::selected", self.on_change_interval)

        return [self.gpu_selector, self.theme_selector, self.view_selector, self.interval_selector]

    def on_change_gpu(self, combo, *args):
        settings = self.get_settings()
        settings["selected_gpu_index"] = combo.get_selected()
        self.set_settings(settings)
        self.update_display(force=True)

    def on_change_theme(self, combo, *args):
        settings = self.get_settings()
        selected_str = combo.get_selected_item().get_string()
        settings["color_theme"] = selected_str
        self.set_settings(settings)
        self.update_display(force=True)

    def on_change_view_mode(self, combo, *args):
        settings = self.get_settings()
        selected_str = combo.get_selected_item().get_string()
        settings["view_mode"] = selected_str
        
        view_modes = ["GPU Util %", "Mem GB", "Temp °C", "Wattage W", "Clock Speed", "Summary"]
        if selected_str in view_modes:
            self.current_view_idx = view_modes.index(selected_str)
            settings["current_view_idx"] = self.current_view_idx
            
        self.set_settings(settings)
        self.update_display(force=True)

    def on_change_interval(self, combo, *args):
        settings = self.get_settings()
        selected_str = combo.get_selected_item().get_string()
        try:
            seconds = int(selected_str.split()[0])
        except Exception:
            seconds = 1
        settings["update_interval"] = seconds
        self.set_settings(settings)
