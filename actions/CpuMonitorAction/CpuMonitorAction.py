import os
import re
import time
import glob
import shutil
import subprocess
import threading
from collections import deque
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

# hwmon "name" values that expose a CPU package/core temperature.
CPU_TEMP_HWMON = ("k10temp", "zenpower", "coretemp", "cpu_thermal", "k8temp")
# Preferred temperature-sensor labels, best first (whole-package readings).
TEMP_LABEL_PRIORITY = ("Tdie", "Tctl", "Package id 0", "Tccd1", "Tccd2")

RE_CPU_DIR = re.compile(r"^cpu[0-9]+$")


class CpuMonitorAction(ActionCore):
    # Class-level cache to share a single sysfs snapshot across every button instance
    _cached_data = None
    _cached_time = 0
    _cache_lock = threading.Lock()

    # Sensor discovery is done once and reused (paths do not change at runtime)
    _sources_discovered = False
    _temp_sensors = []        # list of {"label": str, "path": str}
    _power_source = None      # {"kind": "power_uw"|"energy_uj", "path": str, "max": int|None}
    _cpu_model = "CPU"

    # Rolling state needed to turn cumulative counters into rates
    _prev_stat = None         # (idle, total) from /proc/stat
    _prev_energy = None       # (microjoules, timestamp) for power delta

    # Class-level font cache to avoid reloading font files from disk on every render
    _font_cache = {}
    _font_lock = threading.Lock()

    # ------------------------------------------------------------------ #
    #  Data source: /proc and /sys, no external dependencies
    # ------------------------------------------------------------------ #
    @staticmethod
    def _read_file(path):
        """Read a sysfs/procfs file, falling back to the flatpak host if the
        sandbox cannot see it directly. Returns the stripped text or None."""
        try:
            with open(path, "r") as f:
                return f.read().strip()
        except Exception:
            pass
        if shutil.which("flatpak-spawn"):
            try:
                result = subprocess.run(
                    ["flatpak-spawn", "--host", "cat", path],
                    capture_output=True, text=True, timeout=1.0
                )
                if result.returncode == 0:
                    return result.stdout.strip()
            except Exception:
                pass
        return None

    @classmethod
    def _discover_sources(cls):
        """Locate the CPU temperature and power sensors exposed on this machine.
        Runs once; results are vendor-neutral (AMD k10temp/zenergy, Intel
        coretemp/RAPL, generic thermal zones all supported)."""
        if cls._sources_discovered:
            return
        cls._sources_discovered = True

        # CPU model name (best-effort, cosmetic)
        cpuinfo = cls._read_file("/proc/cpuinfo") or ""
        m = re.search(r"^model name\s*:\s*(.+)$", cpuinfo, re.MULTILINE)
        if m:
            cls._cpu_model = m.group(1).strip()

        # --- Temperature sensors via hwmon ---
        sensors = []
        for hwmon in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
            name = cls._read_file(os.path.join(hwmon, "name"))
            if name not in CPU_TEMP_HWMON:
                continue
            for inp in sorted(glob.glob(os.path.join(hwmon, "temp*_input"))):
                label = cls._read_file(inp.replace("_input", "_label"))
                if not label:
                    label = os.path.basename(inp).replace("_input", "")
                sensors.append({"label": label, "path": inp})

        # Fallback: generic ACPI thermal zones if no CPU hwmon was found
        if not sensors:
            for zone in sorted(glob.glob("/sys/class/thermal/thermal_zone*")):
                ztype = (cls._read_file(os.path.join(zone, "type")) or "").lower()
                if any(k in ztype for k in ("cpu", "pkg", "x86", "core", "soc")):
                    sensors.append({"label": ztype or "temp",
                                    "path": os.path.join(zone, "temp")})

        # Order by preference so the first entry is the best default
        def _rank(s):
            try:
                return TEMP_LABEL_PRIORITY.index(s["label"])
            except ValueError:
                return len(TEMP_LABEL_PRIORITY)
        cls._temp_sensors = sorted(sensors, key=_rank)

        # --- Power source ---
        cls._power_source = cls._discover_power_source()

    @classmethod
    def _discover_power_source(cls):
        # zenpower exposes instantaneous package power directly (microwatts)
        for hwmon in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
            if cls._read_file(os.path.join(hwmon, "name")) == "zenpower":
                p = os.path.join(hwmon, "power1_input")
                if os.path.exists(p):
                    return {"kind": "power_uw", "path": p, "max": None}

        # zenergy (AMD) exposes a cumulative package energy counter (microjoules)
        for hwmon in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
            if cls._read_file(os.path.join(hwmon, "name")) == "zenergy":
                for inp in sorted(glob.glob(os.path.join(hwmon, "energy*_input"))):
                    label = cls._read_file(inp.replace("_input", "_label")) or ""
                    if label.startswith(("Esocket", "Epackage", "Epkg")):
                        return {"kind": "energy_uj", "path": inp, "max": None}

        # powercap RAPL: works for Intel and many AMD Zen kernels
        rapl = "/sys/class/powercap/intel-rapl:0/energy_uj"
        if os.path.exists(rapl):
            max_range = cls._read_file(
                "/sys/class/powercap/intel-rapl:0/max_energy_range_uj")
            try:
                max_range = int(max_range) if max_range else None
            except ValueError:
                max_range = None
            return {"kind": "energy_uj", "path": rapl, "max": max_range}

        return None

    @classmethod
    def _sample_util(cls):
        """CPU utilisation percent from the /proc/stat delta between samples."""
        line = cls._read_file("/proc/stat")
        if not line:
            return 0.0
        first = line.splitlines()[0].split()
        try:
            nums = [int(x) for x in first[1:]]
        except ValueError:
            return 0.0
        idle = nums[3] + (nums[4] if len(nums) > 4 else 0)  # idle + iowait
        total = sum(nums)
        prev = cls._prev_stat
        cls._prev_stat = (idle, total)
        if prev is None:
            return 0.0
        d_total = total - prev[1]
        d_idle = idle - prev[0]
        if d_total <= 0:
            return 0.0
        return max(0.0, min(100.0, 100.0 * (1.0 - d_idle / d_total)))

    @classmethod
    def _sample_power(cls, now):
        """CPU package power in watts, or None if unavailable."""
        src = cls._power_source
        if not src:
            return None
        raw = cls._read_file(src["path"])
        if raw is None:
            return None
        try:
            value = int(raw)
        except ValueError:
            return None

        if src["kind"] == "power_uw":
            return value / 1_000_000.0  # microwatts -> watts

        # energy_uj: differentiate the cumulative joule counter over time
        prev = cls._prev_energy
        cls._prev_energy = (value, now)
        if prev is None:
            return None
        d_energy = value - prev[0]
        d_time = now - prev[1]
        if d_time <= 0:
            return None
        if d_energy < 0:  # counter wrapped
            if src["max"]:
                d_energy += src["max"]
            else:
                return None
        return (d_energy / 1_000_000.0) / d_time  # microjoules/s -> watts

    @classmethod
    def _sample_clock(cls):
        """Average current core frequency in MHz."""
        freqs = []
        for cpu in glob.glob("/sys/devices/system/cpu/cpu[0-9]*"):
            if not RE_CPU_DIR.match(os.path.basename(cpu)):
                continue
            raw = cls._read_file(os.path.join(cpu, "cpufreq", "scaling_cur_freq"))
            if raw:
                try:
                    freqs.append(int(raw) / 1000.0)  # kHz -> MHz
                except ValueError:
                    pass
        if freqs:
            return sum(freqs) / len(freqs)
        # Fallback: /proc/cpuinfo reported MHz
        info = cls._read_file("/proc/cpuinfo") or ""
        mhz = [float(x) for x in re.findall(r"cpu MHz\s*:\s*([0-9.]+)", info)]
        return sum(mhz) / len(mhz) if mhz else 0.0

    @classmethod
    def get_cpu_data(cls):
        now = time.time()
        with cls._cache_lock:
            # Return the shared snapshot if it is fresh (< 0.8s old)
            if cls._cached_data is not None and (now - cls._cached_time) < 0.8:
                return cls._cached_data

            cls._discover_sources()

            # Memory from /proc/meminfo (kB -> bytes)
            meminfo = cls._read_file("/proc/meminfo") or ""
            total_kb = _grep_int(meminfo, "MemTotal")
            avail_kb = _grep_int(meminfo, "MemAvailable")
            mem_total = total_kb * 1024 if total_kb else None
            mem_used = (total_kb - avail_kb) * 1024 if (total_kb and avail_kb is not None) else None

            # Temperatures (millidegrees -> Celsius) for every discovered sensor
            temps = {}
            for s in cls._temp_sensors:
                raw = cls._read_file(s["path"])
                if raw is None:
                    continue
                try:
                    temps[s["label"]] = int(raw) / 1000.0
                except ValueError:
                    pass

            data = {
                "cpu_model": cls._cpu_model,
                "cpu_util": cls._sample_util(),
                "mem_used_bytes": mem_used,
                "mem_total_bytes": mem_total,
                "temps": temps,
                "power": cls._sample_power(now),
                "clock": cls._sample_clock(),
            }
            cls._cached_data = data
            cls._cached_time = now
            return data

    @classmethod
    def get_temp_labels(cls):
        cls._discover_sources()
        return [s["label"] for s in cls._temp_sensors]

    # ------------------------------------------------------------------ #
    #  Action lifecycle
    # ------------------------------------------------------------------ #
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.has_configuration = True

        # Max points to display in sparkline graphs
        self.max_history = 20
        self.history = {
            'cpu_util': deque(maxlen=self.max_history),
            'mem_util': deque(maxlen=self.max_history),
            'temp': deque(maxlen=self.max_history),
            'power': deque(maxlen=self.max_history),
            'clock': deque(maxlen=self.max_history)
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

        labels = self.get_temp_labels()
        settings.setdefault("temp_sensor", labels[0] if labels else "")
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

        # Poll sysfs and redraw the button image
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

    def update_display(self, force=False) -> None:
        if self._updating:
            return
        self._updating = True

        try:
            data = self.get_cpu_data()
            if not data:
                self.draw_error_image("no data")
                return

            settings = self.get_settings()

            cpu_util = data.get("cpu_util") or 0.0

            used_bytes = data.get("mem_used_bytes")
            total_bytes = data.get("mem_total_bytes")
            if used_bytes is not None and total_bytes:
                mem_util = (used_bytes / total_bytes) * 100
            else:
                mem_util = 0.0

            # Pick the temperature sensor chosen in settings, else the best default
            temps = data.get("temps") or {}
            temp_label = settings.get("temp_sensor", "")
            if temp_label in temps:
                temp = temps[temp_label]
            elif temps:
                temp = next(iter(temps.values()))
            else:
                temp = 0.0

            power = data.get("power")  # may be None (unavailable)
            power_val = power if power is not None else 0.0

            clock = data.get("clock") or 0.0

            # Add to rolling history buffers (automatically bounded by deque maxlen)
            self.history['cpu_util'].append(cpu_util)
            self.history['mem_util'].append(mem_util)
            self.history['temp'].append(temp)
            self.history['power'].append(power_val)
            self.history['clock'].append(clock)

            # Determine view state
            view_mode = settings.get("view_mode", "Cycle on Press")
            if view_mode == "Cycle on Press":
                active_view = self.current_view_idx
            else:
                view_modes = ["CPU Util %", "RAM GB", "Temp °C", "Wattage W", "Clock Speed", "Summary"]
                if view_mode in view_modes:
                    active_view = view_modes.index(view_mode)
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
            img = self.render_key_image(scaled_size, active_view, cpu_util, mem_util,
                                        temp, power, clock, used_bytes, total_bytes)
            self.set_media(image=img)

        except Exception as e:
            log.error(f"CpuMonitorAction: Error rendering display: {e}")
            self.draw_error_image("draw error")
        finally:
            self._updating = False

    def get_font(self, size, bold=False):
        cache_key = (size, bold)
        with self._font_lock:
            if cache_key in self._font_cache:
                return self._font_cache[cache_key]

            font_paths = [
                "/usr/share/fonts/google-droid-sans-fonts/DroidSans-Bold.ttf" if bold else "/usr/share/fonts/google-droid-sans-fonts/DroidSans.ttf",
                "/usr/share/fonts/adwaita-sans-fonts/AdwaitaSans-Regular.ttf",
                "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/dejavu/DejaVuSans.ttf",
                "/usr/share/fonts/liberation/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/liberation/LiberationSans-Regular.ttf",
            ]
            for path in font_paths:
                if os.path.exists(path):
                    try:
                        font = ImageFont.try_load(path) if hasattr(ImageFont, "try_load") else ImageFont.truetype(path, size)
                        self._font_cache[cache_key] = font
                        return font
                    except Exception:
                        pass
            font = ImageFont.load_default()
            self._font_cache[cache_key] = font
            return font

    def render_key_image(self, size, view_idx, cpu_util, mem_util, temp, power, clock, used_bytes, total_bytes):
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

            pwr_str = f"{power:.0f}W" if power is not None else "--"
            self.draw_quadrant(draw, font_lbl, font_val, "CPU", f"{cpu_util:.0f}%", W // 4, cy_top, theme["dim"])
            self.draw_quadrant(draw, font_lbl, font_val, "MEM", f"{mem_util:.0f}%", 3 * W // 4, cy_top, theme["dim"])
            self.draw_quadrant(draw, font_lbl, font_val, "TMP", f"{temp:.0f}°", W // 4, cy_bottom, theme["dim"])
            self.draw_quadrant(draw, font_lbl, font_val, "PWR", pwr_str, 3 * W // 4, cy_bottom, theme["dim"])
        else:
            # SPARKLINE GRAPH VIEW
            hist_keys = ['cpu_util', 'mem_util', 'temp', 'power', 'clock']
            hist_key = hist_keys[view_idx]
            history_data = self.history[hist_key]

            labels = ["CPU UTIL", "RAM GB", "CPU TEMP", "CPU PWR", "CPU CLK"]

            # Dynamic / static graph scaling limits
            if view_idx in (0, 1):
                max_limit = 100.0
            elif view_idx == 2:
                max_limit = 100.0  # Celsius ceiling
            elif view_idx == 3:
                max_limit = max(max(history_data) if history_data else 50.0, 100.0)
            else:
                max_limit = max(max(history_data) if history_data else 4000.0, 5000.0)

            # Format primary display value string
            val_str = ""
            if view_idx == 0:
                val_str = f"{cpu_util:.0f}%"
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
                val_str = f"{power:.0f} W" if power is not None else "N/A"
            elif view_idx == 4:
                if clock >= 1000:
                    val_str = f"{clock/1000:.2f} GHz"
                else:
                    val_str = f"{clock:.0f} MHz"

            # Draw Label
            font_lbl = self.get_font(int(H * 0.14 * 1.1), bold=True)
            lbl_txt = labels[view_idx]
            bbox_lbl = draw.textbbox((0, 0), lbl_txt, font=font_lbl)
            w_lbl = bbox_lbl[2] - bbox_lbl[0]
            draw.text(((W - w_lbl) / 2, int(H * 0.1)), lbl_txt, fill=theme["dim"], font=font_lbl)

            # Draw Large Center Value
            if view_idx == 4:
                if clock >= 1000:
                    val_val = f"{clock/1000:.2f}"
                    val_unit = "GHz"
                else:
                    val_val = f"{clock:.0f}"
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
        bbox_title = draw.textbbox((0, 0), "CPU ERROR", font=font_title)
        w_title = bbox_title[2] - bbox_title[0]
        draw.text(((W - w_title) / 2, int(H * 0.2)), "CPU ERROR", fill=(255, 50, 50, 255), font=font_title)

        font_msg = self.get_font(int(H * 0.12 * 1.1), bold=False)
        bbox_msg = draw.textbbox((0, 0), message, font=font_msg)
        w_msg = bbox_msg[2] - bbox_msg[0]
        draw.text(((W - w_msg) / 2, int(H * 0.55)), message, fill=(255, 255, 255, 255), font=font_msg)

        self.set_media(image=img)

    # ------------------------------------------------------------------ #
    #  Configuration UI
    # ------------------------------------------------------------------ #
    def get_config_rows(self) -> "list[Adw.PreferencesRow]":
        settings = self.get_settings()

        # 1. Temperature-sensor selector
        self.temp_model = Gtk.StringList()
        self.temp_selector = Adw.ComboRow(
            model=self.temp_model,
            title="Temperature Sensor",
            subtitle="Choose which CPU sensor to display"
        )
        labels = self.get_temp_labels()
        if labels:
            for lbl in labels:
                self.temp_model.append(lbl)
        else:
            self.temp_model.append("No sensor found")

        current_sensor = settings.get("temp_sensor", "")
        if labels and current_sensor in labels:
            self.temp_selector.set_selected(labels.index(current_sensor))
        self.temp_selector.connect("notify::selected", self.on_change_temp_sensor)

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
        view_modes = ["Cycle on Press", "CPU Util %", "RAM GB", "Temp °C", "Wattage W", "Clock Speed", "Summary"]
        for v in view_modes:
            self.view_model_list.append(v)

        current_view = settings.get("view_mode", "Cycle on Press")
        if current_view in view_modes:
            self.view_selector.set_selected(view_modes.index(current_view))
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

        return [self.temp_selector, self.theme_selector, self.view_selector, self.interval_selector]

    def on_change_temp_sensor(self, combo, *args):
        item = combo.get_selected_item()
        if item is None:
            return
        settings = self.get_settings()
        settings["temp_sensor"] = item.get_string()
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

        view_modes = ["CPU Util %", "RAM GB", "Temp °C", "Wattage W", "Clock Speed", "Summary"]
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


def _grep_int(text, key):
    """Pull the first integer following `key:` in a /proc-style block."""
    m = re.search(rf"^{re.escape(key)}\s*:\s*([0-9]+)", text, re.MULTILINE)
    return int(m.group(1)) if m else None
