# CPU Monitor Plugin for StreamController

A StreamController plugin to monitor CPU information (utilisation, RAM usage, temperature, power draw, and clock speed) on Linux. It reads directly from `/proc` and `/sys`, so there is **no external dependency** and it works on any CPU — AMD or Intel. Features both text and graph views toggleable via button presses.

Forked from the [GPU Monitor (nvtop)](https://github.com/jaygz316/stream-controller-nvtop) plugin by jaygz316, reusing its rendering/theming style.

## Features
- **Comprehensive Monitoring:** CPU utilisation, RAM usage, package temperature, power consumption, and clock speed.
- **Interactive Views:** Cycle between per-metric sparkline graphs and a 2×2 summary cockpit via key/dial presses.
- **Vendor-neutral:** Sensors are auto-discovered, so no per-CPU configuration is required.
- **Temperature-sensor picker:** Choose which detected sensor (e.g. `Tctl`, `Tccd1`) to display.

## Data sources
| Metric | Source |
|---|---|
| CPU utilisation % | `/proc/stat` (delta between samples) |
| RAM used / total | `/proc/meminfo` (`MemTotal`, `MemAvailable`) |
| Temperature | hwmon `k10temp` / `coretemp` / `zenpower`, or `/sys/class/thermal` |
| Power (watts) | hwmon `zenpower` / `zenergy`, or `/sys/class/powercap` RAPL |
| Clock speed | `cpu*/cpufreq/scaling_cur_freq` (avg), or `/proc/cpuinfo` |

Power reporting depends on your kernel exposing an energy/power sensor. If none is available, the CPU / RAM / temperature / clock views keep working and the power view shows `N/A`.

## Requirements
- **OS:** Linux
- **Dependencies:** none

> Runs fine inside the StreamController flatpak: `/proc` and `/sys` are readable from the sandbox on most systems, and the plugin falls back to `flatpak-spawn --host` for any file the sandbox cannot see directly.

## Installation
Clone this repository into your StreamController plugins directory:
```bash
git clone https://github.com/RustyTheDalek/CPU-Monitor-Plugin---Stream-Controller.git
```

## License
This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
