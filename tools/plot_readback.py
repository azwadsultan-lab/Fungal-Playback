#!/usr/bin/env python3
"""
plot_readback.py

Plots what the ESP32-S3 sketch asked for (set_mV) against what the ADC
measured after the RC filter (read_mV).

Two ways to use it
  Live from the board (needs pyserial: pip install pyserial):
      python3 tools/plot_readback.py --port /dev/cu.usbmodem101 --seconds 25
  From text copied out of the Serial Monitor into a file:
      python3 tools/plot_readback.py --file readback.txt

Close the Arduino Serial Monitor before using --port; only one program can
hold the port. The sketch prints 50 lines a second.

Needs: numpy, matplotlib (and pyserial for --port)
"""

import argparse
import re
import sys
import time
from pathlib import Path

import numpy as np
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

LINE = re.compile(r"set_mV:\s*(-?\d+(?:\.\d+)?)\s*,\s*read_mV:\s*(-?\d+(?:\.\d+)?)")
LINES_PER_SECOND = 50.0  # REPORT_PERIOD_MS = 20 in the sketch

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_SOFT = "#52514e"
GRID = "#e4e3df"
SET_COLOUR = "#2a78d6"
READ_COLOUR = "#eb6834"


def read_port(port, seconds, baud):
    try:
        import serial
    except ImportError:
        sys.exit("pyserial is not installed. Run: pip install pyserial")
    lines = []
    with serial.Serial(port, baud, timeout=1) as ser:
        ser.reset_input_buffer()
        end = time.time() + seconds
        while time.time() < end:
            raw = ser.readline().decode("ascii", errors="replace").strip()
            if raw:
                lines.append(raw)
    return lines


def parse(lines):
    pairs = [LINE.search(ln) for ln in lines]
    pairs = [(float(m.group(1)), float(m.group(2))) for m in pairs if m]
    if len(pairs) < 10:
        sys.exit("Fewer than 10 data lines found. Is the stream on (send q) and the baud rate 115200?")
    arr = np.array(pairs)
    return arr[:, 0], arr[:, 1]


def main():
    root = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--port", help="serial port of the board")
    src.add_argument("--file", help="text file of serial output")
    ap.add_argument("--seconds", type=float, default=25.0, help="capture length with --port (default 25)")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--title", default="ESP32-S3 playback: requested and measured output")
    ap.add_argument("--out", default=str(root / "plots" / "readback.png"))
    ap.add_argument("--save-log", default=None, help="also save the raw serial lines to this file")
    args = ap.parse_args()

    if args.port:
        lines = read_port(args.port, args.seconds, args.baud)
    else:
        lines = Path(args.file).read_text(errors="replace").splitlines()
    if args.save_log:
        Path(args.save_log).write_text("\n".join(lines) + "\n")

    set_mv, read_mv = parse(lines)
    t = np.arange(len(set_mv)) / LINES_PER_SECOND

    diff = read_mv - set_mv
    print(f"{len(set_mv)} samples, about {t[-1]:.1f} s")
    print(f"Mean difference (measured - requested): {diff.mean():.0f} mV, "
          f"RMS difference: {np.sqrt(np.mean(diff ** 2)):.0f} mV")

    fig, ax = plt.subplots(figsize=(9, 4.2), facecolor=SURFACE)
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=INK_SOFT, labelsize=9)
    ax.grid(True, color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)

    ax.plot(t, set_mv, color=SET_COLOUR, linewidth=1.6, label="Requested (PWM duty)")
    ax.plot(t, read_mv, color=READ_COLOUR, linewidth=1.2, label="Measured (ADC after RC filter)")
    ax.set_xlabel("Time (s), assuming 50 lines per second", color=INK_SOFT, fontsize=9)
    ax.set_ylabel("Voltage (mV)", color=INK_SOFT, fontsize=9)
    ax.set_title(args.title, loc="left", fontsize=11, color=INK)
    legend = ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.16), ncol=2, frameon=False, fontsize=9)
    for text in legend.get_texts():
        text.set_color(INK)

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(args.out, dpi=150, facecolor=SURFACE)
    plt.close(fig)
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
