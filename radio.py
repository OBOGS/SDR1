#!/usr/bin/env python3
"""
SDR1 - pocket scanner for Pi Zero + RTL-SDR Blog V4

Press a button  -> tunes that station, audio out the speaker
Press it again  -> stops (silence)
OLED shows: frequency, station name, an icon, and live audio bars.

Pipeline:  rtl_fm  ->  this script (FFT for the bars)  ->  aplay
"""

import os
import queue
import signal
import subprocess
import sys
import threading
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFont

# ----------------------------------------------------------------------------
# CONFIG - tweak these
# ----------------------------------------------------------------------------
STATIONS = [
    # pin = BCM GPIO number (not header pin). mode: "am" for airband, "fm" for narrow FM.
    dict(pin=17, mhz="128.250", name="Scott Tower",   mode="am", icon="tower"),
    dict(pin=27, mhz="125.200", name="STL Approach",  mode="am", icon="plane"),
    dict(pin=22, mhz="128.700", name="ATIS",          mode="am", icon="atis"),
    dict(pin=23, mhz="160.500", name="Railroad",      mode="fm", icon="train"),
    dict(pin=24, mhz="162.550", name="NOAA Weather",  mode="fm", icon="weather"),
]

RTL_FM = "rtl_fm"          # full path if it isn't on PATH, e.g. /usr/local/bin/rtl_fm
GAIN = None                # None = tuner auto-gain; or a number like 30 (dB)
SQUELCH = 0                # 0 = open (hiss between calls). Try ~50 and adjust to quiet the hiss.
AUDIO_DEVICE = None        # None = ALSA default; or e.g. "plughw:1,0" (see `aplay -l`)
SAMPLE_RATE = 24000        # audio rate out of rtl_fm
LOG_FILE = os.path.expanduser("~/SDR1/rtl_fm.log")

OLED_ADDRESS = 0x3C
OLED_ROTATE = 0            # 2 = upside down
FPS = 15

# Bar scaling (dB). Raise FLOOR if idle noise makes the bars jump; lower CEIL if bars are too short.
DB_FLOOR = 30
DB_CEIL = 75

# ----------------------------------------------------------------------------
# DISPLAY LAYOUT
# ----------------------------------------------------------------------------
W, H = 128, 64
CHUNK = 1024                       # samples per FFT frame
CHUNK_BYTES = CHUNK * 2            # 16-bit mono
NBARS = 32                         # 32 bars x 4 px = 128 px
BAR_BASE = 63                      # bottom row of bars
BAR_MAX_H = 27                     # tallest bar in px

FONT_DIR = "/usr/share/fonts/truetype/dejavu/"


def load_font(name, size):
    try:
        return ImageFont.truetype(FONT_DIR + name, size)
    except OSError:
        try:
            return ImageFont.load_default(size=size)
        except TypeError:
            return ImageFont.load_default()


FONT_BIG = load_font("DejaVuSansMono-Bold.ttf", 16)
FONT_MED = load_font("DejaVuSans.ttf", 11)
FONT_SMALL = load_font("DejaVuSans.ttf", 9)


# ----------------------------------------------------------------------------
# ICONS (24x24, drawn at offset ox, oy)
# ----------------------------------------------------------------------------
def _sym(points):
    """Mirror a right-half outline (x from 12 up) into a full symmetric polygon."""
    return points + [(24 - x, y) for x, y in reversed(points)]


def icon_plane(d, ox, oy):
    right = [(12, 0), (14, 3), (14, 9), (23, 15), (23, 17), (14, 14),
             (14, 19), (18, 22), (18, 23), (12, 21)]
    d.polygon([(ox + x, oy + y) for x, y in _sym(right)], fill=1)


def icon_tower(d, ox, oy):
    d.line((ox + 12, oy, ox + 12, oy + 4), fill=1)
    d.polygon([(ox + 2, oy + 5), (ox + 22, oy + 5), (ox + 18, oy + 11), (ox + 6, oy + 11)], fill=1)
    d.rectangle((ox + 5, oy + 6, ox + 19, oy + 8), fill=0)          # cab windows
    d.rectangle((ox + 10, oy + 11, ox + 14, oy + 22), fill=1)       # shaft
    d.rectangle((ox + 6, oy + 22, ox + 18, oy + 23), fill=1)        # base


def icon_atis(d, ox, oy):
    d.rounded_rectangle((ox + 1, oy + 2, ox + 22, oy + 16), radius=3, outline=1)
    d.polygon([(ox + 5, oy + 16), (ox + 5, oy + 21), (ox + 10, oy + 16)], fill=1)
    d.line((ox + 5, oy + 6, ox + 18, oy + 6), fill=1)
    d.line((ox + 5, oy + 9, ox + 18, oy + 9), fill=1)
    d.line((ox + 5, oy + 12, ox + 13, oy + 12), fill=1)


def icon_train(d, ox, oy):
    d.rounded_rectangle((ox + 4, oy + 1, ox + 19, oy + 19), radius=3, outline=1)
    d.rectangle((ox + 7, oy + 4, ox + 16, oy + 10), outline=1)
    d.ellipse((ox + 7, oy + 13, ox + 9, oy + 15), fill=1)
    d.ellipse((ox + 14, oy + 13, ox + 16, oy + 15), fill=1)
    d.line((ox + 8, oy + 20, ox + 5, oy + 23), fill=1)              # rails
    d.line((ox + 15, oy + 20, ox + 18, oy + 23), fill=1)
    d.line((ox + 2, oy + 23, ox + 21, oy + 23), fill=1)


def icon_weather(d, ox, oy):
    d.ellipse((ox + 1, oy + 9, ox + 11, oy + 19), fill=1)
    d.ellipse((ox + 6, oy + 3, ox + 18, oy + 15), fill=1)
    d.ellipse((ox + 13, oy + 8, ox + 23, oy + 18), fill=1)
    d.rectangle((ox + 6, oy + 14, ox + 18, oy + 19), fill=1)
    bolt = [(14, 8), (9, 15), (12, 15), (10, 23), (17, 13), (13, 13)]
    d.polygon([(ox + x, oy + y) for x, y in bolt], fill=0, outline=1)


ICONS = dict(plane=icon_plane, tower=icon_tower, atis=icon_atis,
             train=icon_train, weather=icon_weather)


# ----------------------------------------------------------------------------
# SCREENS
# ----------------------------------------------------------------------------
def new_canvas():
    img = Image.new("1", (W, H), 0)
    return img, ImageDraw.Draw(img)


def draw_header(d, st):
    ICONS[st["icon"]](d, 2, 3)
    d.text((32, -1), st["mhz"], font=FONT_BIG, fill=1)
    x = 32 + d.textlength(st["mhz"], font=FONT_BIG) + 2
    d.text((x, 6), "MHz", font=FONT_SMALL, fill=1)
    d.text((32, 17), st["name"], font=FONT_MED, fill=1)
    d.line((0, 31, W - 1, 31), fill=1)


def draw_bars(d, levels, peaks):
    for i in range(NBARS):
        x = i * 4
        h = int(round(levels[i] * BAR_MAX_H))
        if h < 1:
            h = 1
        d.rectangle((x, BAR_BASE - h + 1, x + 2, BAR_BASE), fill=1)
        ph = int(round(peaks[i] * BAR_MAX_H))
        if ph > h + 1:
            d.line((x, BAR_BASE - ph, x + 2, BAR_BASE - ph), fill=1)


def render_station(st, levels, peaks, tuning=False):
    img, d = new_canvas()
    draw_header(d, st)
    if tuning:
        d.text((34, 42), "Tuning...", font=FONT_MED, fill=1)
    else:
        draw_bars(d, levels, peaks)
    return img


def render_idle():
    img, d = new_canvas()
    d.text((20, -1), "SDR1 RADIO", font=FONT_BIG, fill=1)
    d.text((26, 18), "Press a button", font=FONT_MED, fill=1)
    n = len(STATIONS)
    gap = (W - n * 24) // (n + 1)
    for i, st in enumerate(STATIONS):
        ICONS[st["icon"]](d, gap + i * (24 + gap), 38)
    return img


def render_message(line1, line2=""):
    img, d = new_canvas()
    d.text((4, 10), line1, font=FONT_MED, fill=1)
    d.text((4, 28), line2, font=FONT_SMALL, fill=1)
    return img


# ----------------------------------------------------------------------------
# AUDIO ANALYSIS
# ----------------------------------------------------------------------------
_window = np.hanning(CHUNK).astype(np.float32)
_bin_hz = SAMPLE_RATE / CHUNK
_edges = np.geomspace(100 / _bin_hz, 4000 / _bin_hz, NBARS + 1)   # 100 Hz - 4 kHz, log spaced
_bands = [(int(_edges[i]), max(int(_edges[i + 1]), int(_edges[i]) + 1)) for i in range(NBARS)]


def chunk_to_levels(raw):
    s = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
    spec = np.abs(np.fft.rfft(s * _window)) / (CHUNK / 4)
    out = np.empty(NBARS, dtype=np.float32)
    for i, (lo, hi) in enumerate(_bands):
        out[i] = spec[lo:hi].max()
    db = 20.0 * np.log10(out + 1.0)
    return np.clip((db - DB_FLOOR) / (DB_CEIL - DB_FLOOR), 0.0, 1.0)


# ----------------------------------------------------------------------------
# RADIO (rtl_fm -> python -> aplay)
# ----------------------------------------------------------------------------
class Radio:
    def __init__(self):
        self.rtl = None
        self.aplay = None
        self.thread = None
        self.stop_evt = threading.Event()
        self.last_chunk = None
        self.running = False

    def start(self, st):
        self.stop()
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        log = open(LOG_FILE, "ab")

        rtl_cmd = [RTL_FM, "-f", st["mhz"] + "M", "-M", st["mode"],
                   "-s", str(SAMPLE_RATE), "-r", str(SAMPLE_RATE), "-l", str(SQUELCH)]
        if st["mode"] == "fm":
            rtl_cmd += ["-E", "deemp"]
        if GAIN is not None:
            rtl_cmd += ["-g", str(GAIN)]
        rtl_cmd.append("-")

        aplay_cmd = ["aplay", "-q", "-t", "raw", "-f", "S16_LE",
                     "-r", str(SAMPLE_RATE), "-c", "1", "-B", "300000"]
        if AUDIO_DEVICE:
            aplay_cmd += ["-D", AUDIO_DEVICE]

        self.last_chunk = None
        self.stop_evt.clear()
        self.aplay = subprocess.Popen(aplay_cmd, stdin=subprocess.PIPE, stderr=log)
        self.rtl = subprocess.Popen(rtl_cmd, stdout=subprocess.PIPE, stderr=log)
        self.running = True
        self.thread = threading.Thread(target=self._pump, daemon=True)
        self.thread.start()

    def _pump(self):
        try:
            while not self.stop_evt.is_set():
                data = self.rtl.stdout.read(CHUNK_BYTES)
                if len(data) < CHUNK_BYTES:
                    break
                self.last_chunk = data
                self.aplay.stdin.write(data)
                self.aplay.stdin.flush()
        except (BrokenPipeError, ValueError, OSError):
            pass
        finally:
            self.running = False

    def stop(self):
        self.stop_evt.set()
        for p in (self.rtl, self.aplay):
            if p is None:
                continue
            try:
                p.terminate()
                p.wait(timeout=2)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
            except OSError:
                pass
        if self.thread is not None:
            self.thread.join(timeout=2)
        self.rtl = self.aplay = self.thread = None
        self.last_chunk = None
        self.running = False


# ----------------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------------
def main():
    # Hardware imports live here so the drawing code can be tested on any computer.
    from gpiozero import Button
    from luma.core.interface.serial import i2c
    from luma.oled.device import ssd1306

    device = ssd1306(i2c(port=1, address=OLED_ADDRESS), width=W, height=H, rotate=OLED_ROTATE)
    radio = Radio()
    presses = queue.Queue()

    buttons = []
    for idx, st in enumerate(STATIONS):
        b = Button(st["pin"], pull_up=True, bounce_time=0.05)
        b.when_pressed = lambda i=idx: presses.put(i)
        buttons.append(b)

    def shutdown(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, shutdown)

    current = None          # index of station playing, or None
    levels = np.zeros(NBARS, dtype=np.float32)
    peaks = np.zeros(NBARS, dtype=np.float32)
    error_until = 0.0
    frame = 1.0 / FPS

    device.display(render_idle())
    try:
        while True:
            t0 = time.time()

            # --- handle button presses (use only the most recent one) ---
            idx = None
            while True:
                try:
                    idx = presses.get_nowait()
                except queue.Empty:
                    break
            if idx is not None:
                if idx == current:
                    radio.stop()
                    current = None
                    levels[:] = 0
                    peaks[:] = 0
                else:
                    try:
                        radio.start(STATIONS[idx])
                        current = idx
                        levels[:] = 0
                        peaks[:] = 0
                    except OSError as e:
                        radio.stop()
                        current = None
                        error_until = time.time() + 4
                        device.display(render_message("Start failed", str(e)[:24]))

            # --- draw ---
            if current is None:
                if time.time() >= error_until:
                    device.display(render_idle())
            elif not radio.running:
                # rtl_fm died on us
                radio.stop()
                current = None
                error_until = time.time() + 5
                device.display(render_message("SDR error", "see rtl_fm.log"))
            else:
                chunk = radio.last_chunk
                if chunk is None:
                    device.display(render_station(STATIONS[current], levels, peaks, tuning=True))
                else:
                    new = chunk_to_levels(chunk)
                    levels = np.where(new > levels, new, levels * 0.75 + new * 0.25)
                    peaks = np.maximum(peaks - 0.04, levels)
                    device.display(render_station(STATIONS[current], levels, peaks))

            time.sleep(max(0.0, frame - (time.time() - t0)))
    except KeyboardInterrupt:
        pass
    finally:
        radio.stop()
        device.clear()


if __name__ == "__main__":
    sys.exit(main())
