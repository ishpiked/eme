from __future__ import annotations

import array
import math
import os
import re
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import emyfx  # noqa: E402

FFMPEG = emyfx.FFMPEG
DECODE_RATE = 22050


# ------------------------------------------------------------------ helpers

def _run(argv) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True)


def levels(path: str) -> tuple[float, float]:
    """(peak dBFS, mean dBFS) via volumedetect."""
    p = _run([FFMPEG, "-hide_banner", "-nostats", "-i", path,
              "-af", "volumedetect", "-f", "null", "-"])
    t = p.stderr.decode(errors="replace")
    mx = re.search(r"max_volume: (-?[\d.]+)", t)
    mn = re.search(r"mean_volume: (-?[\d.]+)", t)
    return (float(mx.group(1)) if mx else float("-inf"),
            float(mn.group(1)) if mn else float("-inf"))


def decode_wav(path: str, af: str | None = None,
               rate: int = DECODE_RATE) -> tuple[array.array, array.array, int]:
    """Decode to 2-channel PCM and return (L, R, rate).

    Pass rate=None for a bit-exact decode (no resampling) — required when the
    measurement is a quantisation grid rather than a spectrum.
    """
    out = tempfile.mktemp(suffix=".wav")
    argv = [FFMPEG, "-v", "error", "-y", "-i", path]
    if af:
        argv += ["-af", af]
    argv += ["-ac", "2"]
    if rate:
        argv += ["-ar", str(rate)]
    argv += ["-f", "wav", out]
    _run(argv)
    with wave.open(out, "rb") as f:
        raw = f.readframes(f.getnframes())
    os.unlink(out)
    a = array.array("h")
    a.frombytes(raw)
    return a[0::2], a[1::2], DECODE_RATE


def balance_envelope(l: array.array, r: array.array, rate: int, win: float = 0.1):
    """Per-window (L-R)/(L+R) envelope — how far the image sits left/right."""
    step = max(1, int(rate * win))
    out = []
    for i in range(0, len(l) - step, step):
        sl, sr = l[i:i + step], r[i:i + step]
        rl = math.sqrt(sum(x * x for x in sl) / len(sl)) + 1e-9
        rr = math.sqrt(sum(x * x for x in sr) / len(sr)) + 1e-9
        out.append((rl - rr) / (rl + rr))
    return out


def dominant_hz(env: list[float], win: float) -> float:
    """Zero-crossing estimate of the L/R balance oscillation frequency."""
    signs = [1 if v > 0 else -1 for v in env]
    cross = sum(1 for i in range(1, len(signs)) if signs[i] != signs[i - 1])
    return cross / 2.0 / (len(env) * win) if env else 0.0


def levels_filtered(path: str, af: str) -> tuple[float, float]:
    p = _run([FFMPEG, "-hide_banner", "-nostats", "-i", path,
              "-af", f"{af},volumedetect", "-f", "null", "-"])
    t = p.stderr.decode(errors="replace")
    mx = re.search(r"max_volume: (-?[\d.]+)", t)
    mn = re.search(r"mean_volume: (-?[\d.]+)", t)
    return (float(mx.group(1)) if mx else float("-inf"),
            float(mn.group(1)) if mn else float("-inf"))


def correlation(l: array.array, r: array.array) -> float:
    num = sum(a * b for a, b in zip(l, r))
    den = math.sqrt(sum(a * a for a in l) * sum(b * b for b in r))
    return num / den if den else 0.0


def make_bed(path: str, seconds: float = 20.0) -> None:
    """Music-like bed: tilt-limited noise + bass, peak ~-6 dBFS."""
    _run([FFMPEG, "-y", "-v", "error",
          "-f", "lavfi", "-i", f"anoisesrc=color=pink:amplitude=0.7:d={seconds}",
          "-f", "lavfi", "-i", f"sine=frequency=55:duration={seconds}",
          "-filter_complex",
          "[0:a]treble=g=-7:f=1200,highpass=f=30[n];[1:a]volume=0.4[b];"
          "[n][b]amix=inputs=2:normalize=0,"
          "aformat=sample_rates=44100:channel_layouts=stereo,"
          "alimiter=limit=0.5:level=false",
          "-c:a", "libmp3lame", "-b:a", "320k", path])


def make_tone(path: str, freq: float = 1000.0, seconds: float = 12.0) -> None:
    """Clean 1 kHz stereo tone for pitch-shift / clip measurement."""
    _run([FFMPEG, "-y", "-v", "error", "-f", "lavfi",
          "-i", f"sine=frequency={freq}:duration={seconds}",
          "-af", "volume=0.5,aformat=sample_rates=44100:channel_layouts=stereo",
          "-c:a", "libmp3lame", "-b:a", "320k", path])


# ------------------------------------------------------------------- checks

class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str, str, bool]] = []

    def add(self, effect: str, check: str, measured: str, want: str, ok: bool) -> None:
        self.rows.append((effect, check, measured, want, ok))

    def render(self) -> int:
        width = max(len(r[1]) for r in self.rows) + 2
        bad = 0
        cur = None
        for fx, check, meas, want, ok in self.rows:
            if fx != cur:
                print(f"\n[{fx}]")
                cur = fx
            mark = "PASS" if ok else "FAIL"
            bad += 0 if ok else 1
            print(f"  {mark}  {check:<{width}} {meas:<34} want {want}")
        print(f"\n{'=' * 72}\n{len(self.rows)} checks, {bad} failed")
        return 1 if bad else 0


def check_common(rep: Report, fx: str, src: str, out: str, dur_src: float, expect_scale: float) -> None:
    sr = subprocess.run(
        [emyfx.FFPROBE, "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=codec_name,sample_rate,channels,duration",
         "-of", "default=nw=1:nk=1", out], capture_output=True).stdout.decode().split()
    codec, rate, ch, dur = (sr + ["?", "?", "?", "0"])[:4]
    ok_ch = codec == "mp3" and rate == "44100" and ch == "2"
    rep.add(fx, "container", f"{codec}/{rate}Hz/{ch}ch", "mp3/44100/2", bool(ok_ch))
    got = float(dur)
    rep.add(fx, "duration", f"{got:.2f}s (x{got / dur_src:.3f})",
            f"x{expect_scale:.3f}", abs(got / dur_src - expect_scale) < 0.03)

    src_peak, _ = levels(src)
    peak, mean = levels(out)
    rep.add(fx, "no clipping", f"peak {peak:.2f} dBFS", "<= -0.3 dBFS", peak <= -0.3)
    rep.add(fx, "level sanity", f"{peak - src_peak:+.2f} dB vs source",
            "-2.5..+1.5 dB", -2.5 <= (peak - src_peak) <= 1.5)


def check_8d(rep: Report, src: str, out: str) -> None:
    l, r, rate = decode_wav(out, "highpass=f=800")
    env = balance_envelope(l, r, rate)
    hz = dominant_hz(env, 0.1)
    swing = (min(env), max(env))
    half = (swing[1] - swing[0]) / 2.0  # oscillation amplitude of the balance
    rep.add("8d", "rotation rate", f"{hz:.3f} Hz",
            "0.08-0.15 Hz", 0.06 <= hz <= 0.16)
    rep.add("8d", "pan depth", f"{swing[0]:+.2f}..{swing[1]:+.2f} (amp {half:.2f})",
            "amplitude >= 0.30", half >= 0.30)
    lb, rb, _ = decode_wav(out, "lowpass=f=150")
    corr = correlation(lb, rb)
    rep.add("8d", "bass stays centred", f"L/R correlation {corr:.4f}",
            ">= 0.90", corr >= 0.90)


def tone_hz(path: str) -> float:
    """Zero-crossing frequency — exact for a pure tone, good enough to catch
    a wrong playback-rate factor."""
    l, _, rate = decode_wav(path, "lowpass=f=2000")
    crosses = sum(1 for i in range(1, len(l)) if (l[i - 1] < 0) != (l[i] < 0))
    return crosses / 2.0 / (len(l) / rate)


def check_slowed(rep: Report, src: str, out: str, tone: str) -> None:
    # A 1 kHz tone must come back at 850 Hz: pitch drops with playback speed.
    with tempfile.TemporaryDirectory() as tmp:
        o = os.path.join(tmp, "tone_slowed.mp3")
        _run(emyfx.build_ffmpeg_args(tone, o, "slowed", duration=12.0))
        hz = tone_hz(o) if os.path.exists(o) else 0.0
    rep.add("slowed", "pitch drop (1 kHz in)", f"{hz:.0f} Hz", "820-880 Hz", 800 <= hz <= 900)
    rep.add("slowed", "reference tone check", f"in {tone_hz(tone):.0f} Hz",
            "~1000 Hz", abs(tone_hz(tone) - 1000) < 60)


def check_vinyl(rep: Report, src: str, out: str) -> None:
    l, r, rate = decode_wav(out)
    # Measured on the raw crackle source so music cannot mask it.
    with tempfile.TemporaryDirectory() as tmp:
        crk = os.path.join(tmp, "crk.wav")
        graph = emyfx._crackle(20.0, rate=0.0012, level=0.5)
        _run([FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", graph,
              "-ac", "2", "-ar", "44100", crk])
        _, mean = levels(crk)
        l2, _, rate2 = decode_wav(crk)
        thr = 0.03 * 32768
        peaks = [abs(v) for v in l2]
        pops = sum(1 for i in range(1, len(peaks))
                   if peaks[i] > thr and peaks[i - 1] <= thr)
        rate_pops = pops / (len(peaks) / rate2)
    rep.add("vinyl", "crackle level", f"{mean:.1f} dBFS RMS",
            "-45..-32 dBFS", -45 <= mean <= -32)
    del l, r, rate  # measured on the crackle source, not the mix
    rep.add("vinyl", "crackle density", f"{rate_pops:.0f} pops/s",
            "10-120 pops/s", 10 <= rate_pops <= 120)
    _, top = levels_filtered(out, "highpass=f=13000")
    _, ref = levels_filtered(out, "bandpass=f=2000:width_type=h:w=800")
    rep.add("vinyl", "HF rolloff", f"13k+ is {top - ref:.1f} dB vs mids",
            "<= -35 dB", (top - ref) <= -35)


def check_lofi(rep: Report, src: str, out: str) -> None:
    _, top = levels_filtered(out, "highpass=f=11000")
    _, ref = levels_filtered(out, "bandpass=f=2000:width_type=h:w=800")
    rep.add("lofi", "band limit", f"11k+ is {top - ref:.1f} dB vs mids",
            "<= -30 dB", (top - ref) <= -30)
    # Quantisation is only observable before the lossy encoder: render the
    # graph to PCM and look for the 13-bit grid step.
    with tempfile.TemporaryDirectory() as tmp:
        wav_out = os.path.join(tmp, "lo.wav")
        graph = emyfx.PRESETS["lofi"]["build"](12.0)
        _run([FFMPEG, "-y", "-v", "error", "-i", src,
              "-filter_complex", graph, "-map", "[out]",
              "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le", wav_out])
        if os.path.exists(wav_out):
            l, _, rate = decode_wav(wav_out, rate=None)  # bit-exact: no resample
            m = max(abs(v) for v in l) or 1
            step = m / 4096.0
            vals = sorted({abs(v) for v in l if abs(v) > m * 0.1})
            deltas = [b - a for a, b in zip(vals, vals[1:])]
            med = sorted(deltas)[len(deltas) // 2] if deltas else 0
            rep.add("lofi", "13-bit grid", f"step {med:.0f} counts (ideal {step:.1f})",
                    f"<= {max(2.0, step * 3):.0f} counts", 0 < med <= max(2, step * 3))
        else:
            rep.add("lofi", "13-bit grid", "render failed", "step ~m/4096", False)


def check_concert(rep: Report, src: str, out: str) -> None:
    # Tail test: 1 s burst of noise, then 3 s of silence. A 2-3 s room keeps
    # ringing at -60..-25 dB in the 0.3-1.5 s window after the burst.
    with tempfile.TemporaryDirectory() as tmp:
        burst = os.path.join(tmp, "burst.wav")
        _run([FFMPEG, "-y", "-v", "error", "-f", "lavfi",
              "-i", "anoisesrc=color=pink:amplitude=0.6:d=1",
              "-af", "aformat=sample_rates=44100:channel_layouts=stereo,"
                     "apad=pad_dur=3.5", "-c:a", "pcm_s16le", burst])
        o = os.path.join(tmp, "burst_concert.wav")
        _run([FFMPEG, "-y", "-v", "error", "-i", burst,
              "-filter_complex", emyfx.PRESETS["concert"]["build"](4.5),
              "-map", "[out]", "-ac", "2", "-c:a", "pcm_s16le", o])
        if not os.path.exists(o):
            rep.add("concert", "decay tail", "render failed", "tail present", False)
            return
        # window 0.3..1.5 s after the burst ends
        l, r, rate = decode_wav(o)
        a, b = int(1.3 * rate), int(2.5 * rate)
        seg = list(l[a:b]) + list(r[a:b])
        tail = math.sqrt(sum(x * x for x in seg) / max(1, len(seg))) / 32768
        tail_db = 20 * math.log10(tail) if tail > 0 else -120
        rep.add("concert", "decay tail", f"{tail_db:.1f} dBFS at +0.3..1.5 s",
                "-70..-20 dBFS", -70 <= tail_db <= -20)
        l2, r2, _ = decode_wav(out, "highpass=f=900")
        # width: side/mid energy ratio of the wet signal should sit near 1.2x
        mid = [x + y for x, y in zip(l2, r2)]
        side = [x - y for x, y in zip(l2, r2)]
        e_mid = sum(x * x for x in mid) + 1e-9
        e_side = sum(x * x for x in side) + 1e-9
        wid = math.sqrt(e_side / e_mid)
        rep.add("concert", "stereo width", f"side/mid {wid:.2f}",
                "0.4-1.4", 0.4 <= wid <= 1.4)


CHECKS = {
    "8d": (check_8d, 1.0),
    "concert": (check_concert, 1.0),
    "slowed": (check_slowed, 1 / 0.85),
    "vinyl": (check_vinyl, 1.0),
    "lofi": (check_lofi, 1.0),
}


def main(argv: list[str]) -> int:
    wanted = [a for a in argv if a in CHECKS] or list(CHECKS)
    rep = Report()
    with tempfile.TemporaryDirectory(prefix="emyverify_") as tmp:
        src = os.path.join(tmp, "bed.mp3")
        tone = os.path.join(tmp, "tone.mp3")
        make_bed(src)
        make_tone(tone)
        print(f"bed: {levels(src)[0]:.2f} dBFS peak / {levels(src)[1]:.2f} dBFS mean")
        for fx in wanted:
            out = os.path.join(tmp, f"{fx}.mp3")
            args = emyfx.build_ffmpeg_args(src, out, fx, duration=20.0)
            r = _run(args)
            if r.returncode != 0 or not os.path.exists(out):
                rep.add(fx, "render", r.stderr.decode(errors="replace")[:80],
                        "rc=0 with output", False)
                continue
            check, scale = CHECKS[fx]
            check_common(rep, fx, src, out, 20.0, scale)
            if fx == "slowed":
                check(rep, src, out, tone)
            else:
                check(rep, src, out)
    return rep.render()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
