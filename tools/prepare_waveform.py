#!/usr/bin/env python3
"""
prepare_waveform.py

Turns one channel of a fungal electrical recording into:
  1. a figure showing the whole trace, the chosen segment and its spectrum
  2. waveform.h, the lookup table the ESP32-S3 sketch plays back

Intended data: Adamatzky A (2021), "Recordings of electrical activity of four
species of fungi", Zenodo, https://zenodo.org/records/5790768
(one sample per second, 8 electrode pairs per recording).

Examples
  python3 tools/prepare_waveform.py "Schizophyllum commune.txt" --list
  python3 tools/prepare_waveform.py "Schizophyllum commune.txt" --channel 0 --detrend-min 60
  python3 tools/prepare_waveform.py "Schizophyllum commune.txt" --channel 2 --start-h 45 --hours 3 --detrend-min 60

The automatic choice skips the first 10 hours (--skip-h), when electrodes are
still settling, and looks for a window with activity sustained across it.
--detrend-min subtracts a moving average so short events are not swamped by
slow drift; the moving-average length should be longer than one event.
  python3 tools/prepare_waveform.py --demo        (synthetic test signal, NOT fungal data)

Needs: numpy, matplotlib
"""

import argparse
import datetime as dt
import re
import sys
from pathlib import Path

import numpy as np
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

FULL_SCALE = 1023  # matches the 10-bit PWM in the sketch
DATASET_URL = "https://zenodo.org/records/5790768"

# Plot colours
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_SOFT = "#52514e"
GRID = "#e4e3df"
SERIES = "#2a78d6"
BAND = "#eb6834"


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------
def _split(line, delim):
    return re.split(r"\s+", line.strip()) if delim is None else line.strip().split(delim)


def _to_float(token):
    """Number in the token, or NaN if it is empty or text (a clock time, a label)."""
    try:
        return float(token.strip().strip('"'))
    except ValueError:
        return np.nan


def load_table(path):
    """Read a delimited text file of numbers.

    Returns (data, names, skipped): a 2-D float array (rows = samples),
    a list of column names, and the number of lines that were not numeric.
    The delimiter (tab, semicolon, comma or spaces) is detected from the file.
    A line counts as data when at least half of its fields are numbers, so a
    text column such as a clock time does not stop the file loading.
    """
    lines = Path(path).read_text(errors="replace").splitlines()
    lines = [ln for ln in lines if ln.strip()]
    if not lines:
        sys.exit(f"{path}: the file is empty")

    sample = lines[: min(len(lines), 200)]
    delim = None
    for candidate in ("\t", ";", ","):
        if sum(candidate in ln for ln in sample) > 0.8 * len(sample):
            delim = candidate
            break

    rows, header, skipped = [], None, 0
    for ln in lines:
        tokens = _split(ln, delim)
        values = [_to_float(t) for t in tokens]
        numeric = sum(not np.isnan(v) for v in values)
        if numeric > 0 and numeric >= len(values) / 2:
            rows.append(values)
        else:
            if not rows:  # the last text line before the data is the header
                header = [t.strip().strip('"') for t in tokens]
            skipped += 1

    if not rows:
        sys.exit(f"{path}: no numeric rows found. Open the file and check its layout.")

    width = max(len(r) for r in rows)
    data = np.full((len(rows), width), np.nan)
    for i, r in enumerate(rows):
        data[i, : len(r)] = r

    # Drop columns that are completely empty (for example from a trailing delimiter).
    keep = ~np.all(np.isnan(data), axis=0)
    data = data[:, keep]

    if header is not None and len(header) == width:
        names = [h for h, k in zip(header, keep) if k]
    else:
        names = [f"column {i}" for i in range(data.shape[1])]
    return data, names, skipped


def split_time_column(data, names):
    """If the first column is a steadily increasing time base, separate it.

    Returns (signals, signal_names, step) where step is the time between
    samples in the file's own time units, or None if there is no time column.
    """
    if data.shape[1] < 2:
        return data, names, None
    first = data[:, 0]
    if np.any(np.isnan(first)):
        return data, names, None
    steps = np.diff(first)
    if len(steps) and np.all(steps > 0) and np.allclose(steps, steps[0], rtol=1e-3, atol=1e-9):
        return data[:, 1:], names[1:], float(steps[0])
    return data, names, None


def fill_gaps(x):
    """Replace missing samples by straight-line interpolation."""
    x = np.asarray(x, dtype=float)
    bad = np.isnan(x)
    if bad.all():
        sys.exit("The chosen channel has no numeric samples.")
    if bad.any():
        idx = np.arange(len(x))
        x = x.copy()
        x[bad] = np.interp(idx[bad], idx[~bad], x[~bad])
    return x


# --------------------------------------------------------------------------
# Signal handling
# --------------------------------------------------------------------------
def detrend(x):
    """Remove the best-fit straight line (slow electrode drift)."""
    t = np.arange(len(x))
    slope, intercept = np.polyfit(t, x, 1)
    return x - (slope * t + intercept)


def moving_average(x, w):
    """Centred moving average over w samples (the window shrinks at the ends)."""
    x = np.asarray(x, dtype=float)
    half = max(1, int(w)) // 2
    csum = np.concatenate([[0.0], np.cumsum(x)])
    idx = np.arange(len(x))
    lo = np.clip(idx - half, 0, len(x))
    hi = np.clip(idx + half + 1, 0, len(x))
    return (csum[hi] - csum[lo]) / (hi - lo)


def pick_segment(x, win, skip, smooth):
    """Start index of the most active window after `skip` samples.

    Slow drift (a centred moving average over `smooth` samples) is subtracted
    first. Each candidate window is then split into 8 equal parts and scored by
    the median of their spreads, so activity must be sustained across the
    window: one step or one large artefact affects only one or two parts and
    does not win.
    """
    if win >= len(x):
        return 0
    if skip + win > len(x):
        skip = 0
    fast = x - moving_average(x, smooth)
    parts = 8
    part = win // parts
    step = max(1, win // 8)
    best_start, best_score = skip, -1.0
    for start in range(skip, len(x) - win + 1, step):
        blocks = fast[start : start + part * parts].reshape(parts, part)
        score = float(np.median(blocks.std(axis=1)))
        if score > best_score:
            best_start, best_score = start, score
    return best_start


def resample(x, n):
    """Reduce (or stretch) a segment to n points.

    When shrinking, each output point is the mean of the samples it replaces,
    so short spikes are averaged rather than skipped.
    """
    x = np.asarray(x, dtype=float)
    if len(x) == n:
        return x.copy()
    if len(x) > n:
        edges = np.linspace(0, len(x), n + 1)
        csum = np.concatenate([[0.0], np.cumsum(x)])
        lo = np.floor(edges[:-1]).astype(int)
        hi = np.maximum(np.ceil(edges[1:]).astype(int), lo + 1)
        return (csum[hi] - csum[lo]) / (hi - lo)
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x)


def spectrum(x, fs):
    """One-sided amplitude spectrum of a detrended, Hann-windowed segment."""
    y = detrend(x)
    window = np.hanning(len(y))
    amp = np.abs(np.fft.rfft(y * window)) * 2.0 / window.sum()
    freq = np.fft.rfftfreq(len(y), d=1.0 / fs)
    return freq[1:], amp[1:]  # drop the zero-frequency bin


def demo_signal(seconds=6 * 3600, seed=1):
    """Synthetic spike train for testing the pipeline. NOT fungal data."""
    rng = np.random.default_rng(seed)
    t = np.arange(seconds, dtype=float)
    x = 0.00002 * t + 0.02 * rng.standard_normal(seconds)  # drift + noise, in mV
    for _ in range(14):
        centre = rng.uniform(0.05, 0.95) * seconds
        width = rng.uniform(120, 600)
        height = rng.uniform(0.2, 1.2)
        x += height * np.exp(-0.5 * ((t - centre) / width) ** 2)
    return x


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------
def style_axis(ax):
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=INK_SOFT, labelsize=9)
    ax.grid(True, color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)


def make_figure(x, seg, fs, start, win, units, title, out_png, skip_h, seg_title):
    hours = np.arange(len(x)) / fs / 3600.0
    seg_hours = hours[start : start + win]
    freq, amp = spectrum(seg, fs)

    fig, axes = plt.subplots(3, 1, figsize=(9, 9), facecolor=SURFACE)
    fig.suptitle(title, color=INK, fontsize=12, x=0.07, ha="left")

    ax = axes[0]
    style_axis(ax)
    ax.plot(hours, x, color=SERIES, linewidth=0.8)
    ax.axvspan(seg_hours[0], seg_hours[-1], color=BAND, alpha=0.18, linewidth=0)
    whole_title = "Whole recording, raw (chosen segment shaded)"
    if skip_h > 0:
        ax.axvspan(hours[0], min(skip_h, hours[-1]), color=GRID, alpha=0.6, linewidth=0)
        whole_title += f"; first {skip_h:g} h greyed, not used for automatic choice"
    ax.set_title(whole_title, loc="left", fontsize=10, color=INK)
    ax.set_xlabel("Time (hours)", color=INK_SOFT, fontsize=9)
    ax.set_ylabel(f"Potential ({units})", color=INK_SOFT, fontsize=9)

    ax = axes[1]
    style_axis(ax)
    ax.plot(seg_hours, seg, color=SERIES, linewidth=1.2)
    ax.set_title(seg_title, loc="left", fontsize=10, color=INK)
    ax.set_xlabel("Time (hours)", color=INK_SOFT, fontsize=9)
    ax.set_ylabel(f"Potential ({units})", color=INK_SOFT, fontsize=9)

    ax = axes[2]
    style_axis(ax)
    ax.plot(freq * 1000.0, amp, color=SERIES, linewidth=1.2)
    ax.set_xscale("log")
    ax.set_title("Amplitude spectrum of the segment (trend removed, Hann window)",
                 loc="left", fontsize=10, color=INK)
    ax.set_xlabel("Frequency (mHz)", color=INK_SOFT, fontsize=9)
    ax.set_ylabel(f"Amplitude ({units})", color=INK_SOFT, fontsize=9)

    fig.tight_layout(rect=(0, 0, 1, 0.97))
    fig.savefig(out_png, dpi=150, facecolor=SURFACE)
    plt.close(fig)


def write_header(path, table, *, placeholder, source, src_seconds, src_min, src_max, units, notes):
    body = []
    for i in range(0, len(table), 12):
        body.append("  " + ", ".join(f"{v:4d}" for v in table[i : i + 12]))
    text = [
        "// waveform.h",
        "// Generated by tools/prepare_waveform.py. Do not edit by hand.",
    ]
    text += [f"// {n}" for n in notes]
    text += [
        "#pragma once",
        "#include <stdint.h>",
        "",
        f"#define WAVE_IS_PLACEHOLDER {1 if placeholder else 0}",
        "",
        f'const char WAVE_SOURCE[] = "{source}";',
        f'const char WAVE_SRC_UNITS[] = "{units}";',
        f"const float WAVE_SRC_SECONDS = {src_seconds:.1f}f;  // length of the original segment",
        f"const float WAVE_SRC_MIN = {src_min:.6f}f;  // original value mapped to 0",
        f"const float WAVE_SRC_MAX = {src_max:.6f}f;  // original value mapped to {FULL_SCALE}",
        f"const uint16_t WAVE_FULL_SCALE = {FULL_SCALE};",
        f"const uint32_t WAVE_LEN = {len(table)};",
        "",
        f"const uint16_t WAVE[{len(table)}] = {{",
        ",\n".join(body),
        "};",
        "",
    ]
    Path(path).write_text("\n".join(text))


# --------------------------------------------------------------------------
def main():
    root = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("recording", nargs="?", help="text file from the dataset (unzipped)")
    ap.add_argument("--demo", action="store_true", help="use a synthetic test signal (not fungal data)")
    ap.add_argument("--list", action="store_true", help="show the columns found in the file and stop")
    ap.add_argument("--channel", type=int, default=0, help="signal column to use, counting from 0 (default 0)")
    ap.add_argument("--fs", type=float, default=1.0, help="samples per second in the file (default 1, as the dataset states)")
    ap.add_argument("--start-h", type=float, default=None, help="segment start in hours (default: most active window)")
    ap.add_argument("--hours", type=float, default=4.0, help="segment length in hours (default 4)")
    ap.add_argument("--skip-h", type=float, default=10.0,
                    help="hours at the start ignored by the automatic choice, while electrodes settle (default 10)")
    ap.add_argument("--detrend-min", type=float, default=0.0,
                    help="remove slow drift by subtracting a centred moving average of this many minutes "
                         "before export (default 0 = off)")
    ap.add_argument("--points", type=int, default=2000, help="points in the lookup table (default 2000)")
    ap.add_argument("--units", default="mV", help="units of the values in the file (default mV: check the file)")
    ap.add_argument("--label", default=None, help="species or description for titles")
    ap.add_argument("--citation", default=f"Adamatzky A (2021), Zenodo, {DATASET_URL}",
                    help="where the recording came from; written into waveform.h")
    ap.add_argument("--source-tag", default="Adamatzky 2021, Zenodo record 5790768",
                    help="short source name shown by the sketch's ? command")
    ap.add_argument("--out", default=str(root / "waveform.h"), help="header to write")
    ap.add_argument("--plot", default=str(root / "plots" / "trace_and_spectrum.png"), help="figure to write")
    args = ap.parse_args()

    if args.demo:
        x = demo_signal()
        names = ["synthetic"]
        source_name = "SYNTHETIC TEST SIGNAL (not fungal data)"
        label = args.label or "Synthetic test signal (not fungal data)"
        channel_name = "synthetic"
    else:
        if not args.recording:
            ap.error("give a recording file, or use --demo")
        data, names, skipped = load_table(args.recording)
        signals, names, step = split_time_column(data, names)
        print(f"Read {signals.shape[0]} samples x {signals.shape[1]} signal columns "
              f"({skipped} non-numeric lines skipped)")
        if step is not None:
            print(f"First column looks like a time base with step {step:g}; it is not used as a signal. "
                  f"If that step is in seconds, --fs should be {1.0 / step:g}.")
        if args.list:
            for i, name in enumerate(names):
                col = signals[:, i]
                print(f"  channel {i}: {name:<20} min {np.nanmin(col):.4g}  max {np.nanmax(col):.4g}  "
                      f"missing {int(np.isnan(col).sum())}")
            return
        if not 0 <= args.channel < signals.shape[1]:
            sys.exit(f"--channel must be between 0 and {signals.shape[1] - 1}")
        x = fill_gaps(signals[:, args.channel])
        source_name = Path(args.recording).name
        label = args.label or Path(args.recording).stem
        channel_name = names[args.channel]

    fs = args.fs
    win = max(8, int(round(args.hours * 3600 * fs)))
    win = min(win, len(x))
    skip = int(round(args.skip_h * 3600 * fs))
    detrend_w = int(round(args.detrend_min * 60 * fs))
    smooth_w = detrend_w if detrend_w > 0 else int(round(30 * 60 * fs))
    if args.start_h is None:
        start = pick_segment(x, win, skip, smooth_w)
    else:
        start = int(round(args.start_h * 3600 * fs))
        if not 0 <= start <= len(x) - win:
            sys.exit(f"--start-h is outside the recording ({len(x) / fs / 3600:.2f} hours long)")

    # Drift is removed from the whole recording before cutting the segment,
    # so the moving average is not distorted at the segment's ends.
    xp = x - moving_average(x, detrend_w) if detrend_w > 0 else x
    seg = xp[start : start + win]

    lo, hi = float(seg.min()), float(seg.max())
    if hi - lo <= 0:
        sys.exit("The chosen segment is flat. Pick another channel or window.")
    table = np.rint((resample(seg, args.points) - lo) / (hi - lo) * FULL_SCALE)
    table = np.clip(table, 0, FULL_SCALE).astype(int)

    Path(args.plot).parent.mkdir(parents=True, exist_ok=True)
    title = f"{label}, channel {args.channel} ({channel_name})"
    if detrend_w > 0:
        seg_title = f"Chosen segment, as exported: slow drift removed ({args.detrend_min:g}-min moving average subtracted)"
    else:
        seg_title = "Chosen segment, as exported (raw)"
    make_figure(x, seg, fs, start, win, args.units, title, args.plot,
                args.skip_h if args.start_h is None else 0.0, seg_title)

    src_seconds = win / fs
    notes = [
        f"Created {dt.date.today().isoformat()}",
        f"Source file: {source_name}, channel {args.channel} ({channel_name})",
        f"Segment: starts {start / fs / 3600:.3f} h, lasts {src_seconds / 3600:.3f} h, "
        f"{win} samples at {fs:g} samples per second",
        f"Reduced to {args.points} points by block averaging, then scaled to 0..{FULL_SCALE}",
    ]
    if detrend_w > 0:
        notes.append(f"Slow drift removed: centred {args.detrend_min:g}-min moving average subtracted "
                     f"from the whole recording before the segment was cut")
    if args.demo:
        notes.append("PLACEHOLDER: synthetic spike train for testing. NOT fungal data.")
        source_text = "SYNTHETIC PLACEHOLDER (not fungal data)"
    else:
        notes.append(f"Dataset: {args.citation}")
        source_text = f"{source_name}, channel {args.channel} ({args.source_tag})"
        if detrend_w > 0:
            source_text += f", drift removed ({args.detrend_min:g}-min moving average)"
    write_header(args.out, table, placeholder=args.demo, source=source_text.replace('"', "'"),
                 src_seconds=src_seconds, src_min=lo, src_max=hi, units=args.units, notes=notes)

    print(f"Segment: start {start / fs / 3600:.2f} h, length {src_seconds / 3600:.2f} h, "
          f"range {lo:.4g} to {hi:.4g} {args.units}")
    print(f"Wrote {args.out} ({args.points} points)")
    print(f"Wrote {args.plot}")


if __name__ == "__main__":
    main()
