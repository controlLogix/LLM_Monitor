# LLM Monitor

A transparent, always-on-top, draggable telemetry overlay for NVIDIA GPUs on
Windows, built around the numbers that actually matter when you run models
locally: VRAM headroom, memory bandwidth saturation, CPU-offload detection and
model residency.

Pure Python standard library. No pip installs, no Electron, no service. One
`.pyw` file and a JSON config.

Typical resident cost is around 48 MB and well under 1% of one core at the
default one-second poll: two short `nvidia-smi` calls plus one localhost HTTP
request per tick.

## Why not just use nvidia-smi

Most overlays lead with utilisation and temperature, which are the least useful
numbers for local inference. This one leads with:

- **VRAM headroom**, the figure that decides whether the next model loads onto
  the GPU or spills to system RAM.
- **Memory-controller utilisation**, shown separately from GPU utilisation.
  Token generation is memory-bandwidth bound rather than compute bound, so
  bandwidth pegged high while core utilisation sits mid-range is a healthy,
  fully-fed decode loop, not an idle card.
- **The GPU/CPU split of each loaded model.** A model reported as resident is
  not necessarily resident *on the GPU*. Even a few layers spilling to the CPU
  cost most of the throughput, so anything short of full GPU residency is
  flagged in amber or red.
- **Throttle reasons**, decoded from the driver's clock-event mask. A card
  capped by temperature or power quietly loses throughput with no other
  outward sign.

## Requirements

- Windows 10 or 11
- An NVIDIA GPU with `nvidia-smi` on `PATH`, installed with the driver
- Python 3.10 or newer, including tkinter, which ships with the standard
  Windows installer

Optional:

- [Ollama](https://ollama.com) for the model residency rows, read over its
  local HTTP API
- [PresentMon](https://github.com/GameTechDev/PresentMon) for the frame-rate
  metric, covered below

## Running it

    Start GPU Overlay.bat

That launches under `pythonw.exe`, so no console window appears.

To run it directly:

    pythonw gpu_overlay.pyw

### Starting at logon

Tick **Start with Windows** in the right-click menu. That places a shortcut in
the current user's Startup folder and records the choice as `start_on_login` in
`config.json`; unticking removes both. `Add to Startup.bat` and
`Remove from Startup.bat` do the same job from outside the overlay and update
the same key, so the menu always reflects the true state.

The key ships as `null`, meaning undecided. On first run the overlay adopts
whatever the Startup folder already holds, so a shortcut left by an earlier
version is kept rather than quietly deleted. From then on the key is
authoritative: set it to `true` or `false` by hand and the shortcut is created
or removed at the next launch.

The shortcut targets `pythonw.exe` with the script path rather than the .bat, so
nothing flashes a console at logon. It stores an absolute path, so after moving
the folder, retick the setting (or rerun `Add to Startup.bat`) to repoint it.

## Controls

| Action | How |
| --- | --- |
| Move | Left-click drag anywhere on the panel |
| Menu | Right-click the panel |
| Quit | Right-click then Quit, or press Esc |
| Toggle click-through | Ctrl+Shift+G, from anywhere |
| Temporarily grab the mouse back | Hold Ctrl+Shift |

Position, opacity, metric selection and every other setting persist to
`config.json` as soon as they change.

### Click-through

With click-through enabled the panel ignores the mouse entirely and clicks land
on whatever is behind it. That also means it cannot be right-clicked to turn the
mode off, so there are two ways back, neither of which needs the window to
receive input:

- **Ctrl+Shift+G** toggles the mode outright, from anywhere. Change the letter
  with `click_through_hotkey` in the config.
- **Holding Ctrl+Shift** makes the panel interactive for as long as the keys are
  held, which is enough to right-click and reach the menu.

Toggling the mode flashes a reminder of the hotkey on the panel for a few
seconds. The hotkey is polled rather than registered, so it needs no elevation
and cannot collide with another application's shortcut registration. It does
still fire while another application has focus.

## Metrics

Every row can be toggled from the right-click menu under Metrics.

### General

| Key | Shows |
| --- | --- |
| `gpu_name` | Card model and driver version |
| `util` | GPU utilisation, bar plus sparkline |
| `vram` | VRAM used against total, bar plus sparkline |
| `vram_free` | Free VRAM in GB |
| `vram_reserved` | VRAM held back by the driver |
| `temp` | Core temperature, plus memory-junction temperature where reported |
| `power` | Draw against limit, coloured as it approaches the cap |
| `fan` | Fan speed |
| `clocks` | SM and memory clocks, plus P-state |
| `pcie` | Current link generation and width |
| `throttle` | Decoded clock-event reasons |
| `fps` | Frame rate of the foreground application, described below |
| `ram` | System RAM, which is where a model spills when it will not fit |
| `procs` | Processes holding the GPU |

### Local inference

| Key | Shows |
| --- | --- |
| `mem_util` | Memory-controller utilisation, the usual generation bottleneck |
| `vram_free` | Headroom in GB |
| `ollama` | Each loaded model with its size, quantisation, context and expiry, plus a second line giving the GPU/CPU split |
| `ollama_fit` | Largest model that fits the free VRAM at 4-bit, 8-bit and 16-bit |
| `procs` | Filtered to inference runtimes by default, via `proc_filter` |

The fit estimate is deliberately rough: it divides free VRAM, less a reserve for
context, by typical bytes per parameter at each quantisation. Tune the reserve
with `fit_context_reserve_gb`.

## Frame rate

`nvidia-smi` cannot observe frame presentation at all; its only frame-rate field
is the NVENC encoder. Real per-application frame rate on Windows requires an ETW
present-tracing tool, so this metric is backed by PresentMon.

A binary is looked for in this order:

1. `presentmon_path` in the config, if set
2. A `presentmon` folder beside the script
3. `PresentMon.exe` on `PATH`
4. The copy shipped with the NVIDIA FrameView SDK, if installed

When one is found, present events are streamed and the frame rate of the
**foreground** window's process is reported, averaged over `fps_window_s`. When
none is found, or the one found produces no output, the row says so rather than
showing an invented number.

To enable it, download the PresentMon console release and drop the executable
into a `presentmon` folder beside the script. Nothing is installed system-wide.

Elevation is not required if the account belongs to the **Performance Log
Users** group, which grants the ETW rights PresentMon needs. Note that the copy
bundled with the NVIDIA FrameView SDK is inert unless that SDK's driver and
service are also installed; it exits immediately without output, which the
overlay reports rather than hiding.

Three details worth knowing:

- Frame rate is derived from each row's own inter-present interval rather than
  from when output lines arrive, because PresentMon block-buffers its output
  when redirected and arrival times would read as noise.
- Without elevation PresentMon cannot name processes owned by another account
  and reports them as unknown. Those are resolved from the process ID instead,
  and a process that could be named is preferred over one that could not,
  since otherwise the reading is usually the desktop compositor rather than a
  real application.
- PresentMon runs as a child process bound to a Windows job object with
  `KILL_ON_JOB_CLOSE`, so it exits with the overlay even if the overlay is
  killed outright rather than quit. Its trace session name includes the
  overlay's process ID so that two instances do not stop each other's session.

Set `fps_backend` to `presentmon`, `nvenc` or `off` to pin the behaviour. The
`nvenc` fallback reports the encoder rate, which is real but non-zero only while
something is actually encoding. It is labelled `encoder` so it cannot be
mistaken for render frame rate.

## Configuration

`config.json` is created next to the script on first run and rewritten whenever
a setting changes. The right-click menu offers *Edit config.json* and
*Reload config*, the latter applying changes without a restart. A malformed file
falls back to defaults rather than refusing to start.

Keys beyond the menu items:

    "gpu": "all",                   // or an index such as 0 on a multi-GPU host
    "bar_width": 12,                // characters in the inline bars
    "sparkline_points": 90,         // history depth
    "fit_context_reserve_gb": 1.5,  // VRAM withheld from the fit estimate
    "ollama_url": "http://localhost:11434",
    "click_through_hotkey": "G",
    "start_on_login": null,         // true | false | null to follow the folder
    "fps_backend": "auto",          // auto | presentmon | nvenc | off
    "fps_window_s": 1.5,
    "warn": { "temp_warn": 78, "temp_crit": 86 },
    "colors": { "bg": "#0b0f14", "good": "#6fcf7f" }

Note that `metrics` is a list, not a merged set. An existing config keeps the
list it has, so a metric added in a later version has to be enabled from the
menu or added to the list by hand.

### Custom metrics

Additional rows are defined in the `custom` array. Three types are supported.

**`nvidia_smi`** reads any field `nvidia-smi --query-gpu` accepts. It is merged
into the existing poll, so it costs nothing extra:

    { "label": "Enc FPS", "type": "nvidia_smi",
      "field": "encoder.stats.averageFps", "unit": " fps", "enabled": true }

**`command`** runs anything and optionally extracts a value with a regular
expression, using capture group 1 when present. Each entry runs on its own
interval so a slow command never stalls the display:

    { "label": "Requests", "type": "command",
      "cmd": ["cmd", "/c", "curl -s localhost:8000/metrics"],
      "regex": "num_requests_running ([0-9.]+)",
      "unit": " req", "interval_ms": 5000, "enabled": true }

**`http_json`** fetches a URL and walks a dotted path, where integers index into
arrays:

    { "label": "Loaded", "type": "http_json",
      "url": "http://localhost:11434/api/ps",
      "json_path": "models.0.name", "interval_ms": 3000, "enabled": true }

## Style

`card`, the default, draws a translucent dark panel. `ghost` makes the panel
background fully transparent so only the text floats over the desktop, which
reads better over a busy screen but is harder to grab, since only the text is
draggable.

## Notes and limitations

- **Do not set `WS_EX_LAYERED` by hand.** An early revision applied that bit
  from the click-through handler during construction, before the window had been
  mapped. The result was a window that reported itself visible, topmost and
  correctly sized, with a fully populated widget tree, and painted nothing at
  all. Tk owns that bit through its `-alpha` attribute; click-through needs only
  `WS_EX_TRANSPARENT`, and the extended style is now touched solely from the
  guard loop, well after the window is realised, with alpha re-asserted
  afterwards.
- **Per-process VRAM is unavailable on GeForce cards under WDDM.** The driver
  reports it as not applicable on consumer Windows. The `procs` row therefore
  shows process names rather than sizes, filtered to inference runtimes, and
  falls back to a size-ranked list only where the driver supplies real figures.
- **Memory-junction temperature** is not exposed on GeForce cards, so that
  reading is omitted rather than shown as zero.
- A `config.json` saved with a UTF-8 byte-order mark is handled, since the
  loader reads `utf-8-sig`. An unreadable config reverts to defaults rather than
  failing loudly.
- Exclusive-fullscreen applications cover the overlay. Borderless-windowed ones
  do not.

## Repository layout

    gpu_overlay.pyw          the application
    Start GPU Overlay.bat    launch without a console window
    Add to Startup.bat       run at logon
    Remove from Startup.bat  undo that
    config.json              generated on first run, not tracked
    presentmon/              optional PresentMon binary, not tracked
