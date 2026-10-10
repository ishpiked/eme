"""Emy audio effects engine — FFmpeg-only, no heavy libraries.

Every preset is a single `-filter_complex` graph rendered in one ffmpeg pass
(no intermediate WAVs, no Python-side DSP, no extra copies of the audio):

  8d      rotational panning + stereo widening, bass kept centred
  360     slow stereo orbit + spacious side reflections, bass kept centred
  concert live-stage reverb: spacious wet tail + short early reflections
  slowed  slowed + reverb (0.85x, natural pitch drop, -14 LUFS / -1 dBTP)
  vinyl   warm record player: crackle, hiss, wow/flutter, gentle saturation
  lofi    dusty mellow: 22.05 kHz band-limit, ~13-bit quantisation, tape hiss

Design rules encoded here (from the preset spec):

* nothing is pushed past the safe band — all presets end in `alimiter`, and
  `slowed` additionally targets -14 LUFS with a -1 dBTP true-peak ceiling,
* mono input is promoted to stereo once, never "widened into" fake space,
* low end is protected: 8D splits bass off before panning so the bottom stays
  centred; vinyl/lo-fi use highpass + gentle saturation instead of raw drive,
* output is always stereo 44.1 kHz MP3, same codec/bitrate as the rest of Emy.

Use `render_bytes()` (async) from the bot; `render_file()` is the sync core.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"

SAMPLE_RATE = 44100
DEFAULT_BITRATE = "320k"

# ---------------------------------------------------------------- primitives

# One normalisation point: any input (mono, 48k, 22k, float) becomes stereo
# 44.1 kHz fltp before any effect touches it.
_STEREO = f"aformat=sample_rates={SAMPLE_RATE}:sample_fmts=fltp:channel_layouts=stereo"

# Never clip, whatever the wet path does.
_SAFE_TAIL = "alimiter=limit=0.94:level=false"

# Reverb taps (ms) used as early reflections + decay tail. Shared by the
# concert and slowed presets so the room character stays consistent.
_EARLY_REFLECTIONS_MS = (28, 41, 57, 73)  # 25-35 ms pre-delay band + spread
_DECAY_TAPS_MS = (120, 260, 480, 900, 1500, 2350)
_EARLY_GAINS = (0.20, 0.14, 0.10, 0.07)
_DECAY_GAINS = (0.12, 0.095, 0.075, 0.055, 0.035, 0.02)


def _aecho(delay_ms, gains, in_gain: float, out_gain: float) -> str:
    d = "|".join(str(int(x)) for x in delay_ms)
    g = "|".join(f"{x:.3f}" for x in gains)
    return f"aecho={in_gain:.3f}:{out_gain:.3f}:{d}:{g}"


_NOISE_SEED = 0x0E7B  # fixed seed -> deterministic renders, cache-friendly


def _hiss(amplitude: float, duration: float, color: str = "pink") -> str:
    return (
        f"anoisesrc=color={color}:amplitude={amplitude:.4f}:"
        f"sample_rate={SAMPLE_RATE}:duration={duration:.3f}:seed={_NOISE_SEED}"
    )


def _crackle(duration: float, rate: float, level: float) -> str:
    """Sparse vinyl pops as an expression source.

    `random(idx)` is evaluated per sample; the gate lets through roughly
    `1-rate` of samples as short bipolar spikes (surface noise), decorrelated
    between channels so the pops don't sit in the middle of the head.
    """
    gate = 1.0 - rate
    # `level` is the full span of a pop; each channel is decorrelated.
    left = f"if(gt(random(0),{gate:.6f}),(random(1)-0.5)*{level:.3f},0)"
    right = f"if(gt(random(2),{gate:.6f}),(random(3)-0.5)*{level:.3f},0)"
    return f"aevalsrc=exprs='{left}|{right}':s={SAMPLE_RATE}:d={duration:.3f}"


# ------------------------------------------------------------------ presets


def _graph_8d(duration: float | None) -> str:
    """8D: smooth left-right rotation, bass pinned to the centre.

    Rotation 0.125 Hz, a little extra width and a soft reflection trail make
    the movement read clearly without pulling the low end off-centre.
    """
    rot_hz = 0.125
    width = 1.35
    return (
        f"[0:a]{_STEREO},asplit=2[bass][air];"
        # Keep kick and bass anchored while the upper image does the moving.
        "[bass]lowpass=f=180,volume=0.94[low];"
        "[air]highpass=f=180,"
        f"apulsator=hz={rot_hz}:mode=sine:amount=0.7:level_out=1,"
        "stereowiden=delay=14:feedback=0.24:crossfeed=0.22:drymix=0.86,"
        "aecho=0.9:0.94:38|66|104:0.075|0.045|0.02,"
        f"extrastereo=m={width},"
        f"volume=volume='1+0.07*sin(2*PI*t*{rot_hz})':eval=frame[airw];"
        "[low][airw]amix=inputs=2:normalize=0:duration=first,"
        f"{_SAFE_TAIL}[out]"
    )


def _graph_360(duration: float | None) -> str:
    """360°: slow, wide stereo orbit with a gentle trail and centred bass."""
    del duration
    orbit_hz = 0.065
    return (
        f"[0:a]{_STEREO},asplit=2[center][orbit];"
        "[center]lowpass=f=160,volume=0.9[bass];"
        "[orbit]highpass=f=160,"
        f"apulsator=hz={orbit_hz}:mode=sine:amount=0.66:"
        "offset_l=0:offset_r=0.5:level_out=1,"
        "stereowiden=delay=18:feedback=0.20:crossfeed=0.22:drymix=0.86,"
        "aecho=0.9:0.94:32|58|91:0.07|0.045|0.022,"
        "extrastereo=m=1.22,"
        "volume=0.88[moving];"
        "[bass][moving]amix=inputs=2:normalize=0:duration=first,"
        f"{_SAFE_TAIL}[out]"
    )


def _graph_3d(duration: float | None) -> str:
    """3D depth: stable centre, short front-to-back reflections and soft width."""
    del duration
    return (
        f"[0:a]{_STEREO},asplit=3[directsrc][basssrc][space];"
        "[basssrc]lowpass=f=190,volume=0.22[bass];"
        "[space]highpass=f=190,"
        "aecho=0.92:0.94:22|46|78:0.075|0.045|0.022,"
        "stereowiden=delay=11:feedback=0.18:crossfeed=0.28:drymix=0.9,"
        "extrastereo=m=1.16,"
        "volume=0.30[depth];"
        "[directsrc]volume=0.78[direct];"
        "[direct][bass][depth]amix=inputs=3:normalize=0:duration=first,"
        f"{_SAFE_TAIL}[out]"
    )


def _graph_concert(duration: float | None, decay_s: float = 2.5) -> str:
    """Concert hall: a clear early-reflection bloom and a soft, wide decay."""
    del decay_s  # decay length is encoded in _DECAY_TAPS_MS
    taps = tuple(_EARLY_REFLECTIONS_MS) + tuple(_DECAY_TAPS_MS)
    gains = tuple(_EARLY_GAINS) + tuple(_DECAY_GAINS)
    early = _aecho(taps, gains, 0.8, 0.88)
    return (
        f"[0:a]{_STEREO},asplit=2[dry][room];"
        # Room: early reflections first (stage geometry), then damping of the
        # highs (damping=50% -> gentle -3 dB shelf above 3 kHz) so the tail
        # never turns to hiss, then a modest width lift.
        f"[room]{early},highpass=f=90,treble=g=-3:f=3000,lowpass=f=9500,"
        "stereowiden=delay=24:feedback=0.32:crossfeed=0.26:drymix=0.72,"
        "extrastereo=m=1.28,"
        "volume=0.27[wet];"
        "[dry]volume=1.0[dryw];"
        "[dryw][wet]amix=inputs=2:normalize=0:duration=first,"
        f"{_SAFE_TAIL}[out]"
    )


def _graph_slowed(duration: float | None) -> str:
    """Slowed + reverb: 0.85x with natural pitch drop, a warm 28% wet cloud,
    35 ms pre-delay, 1.3x width, 14 kHz low-pass, +1.5 dB low shelf,
    -14 LUFS / -1 dBTP."""
    stretch = 0.85
    stretched_rate = int(SAMPLE_RATE * stretch)
    taps = (35,) + tuple(_DECAY_TAPS_MS) + (int(3.0 * 1000),)
    gains = (0.22, 0.18, 0.14, 0.10, 0.07, 0.045, 0.03, 0.018)
    return (
        # asetrate/aresample is the pitch-off slow: playback drops with the
        # pitch (the "classic" sound), unlike rubberband which preserves it.
        f"[0:a]{_STEREO},aresample={SAMPLE_RATE},asetrate={stretched_rate},"
        f"aresample={SAMPLE_RATE},"
        "bass=g=1.5:f=80,"          # +1.5 dB low shelf
        "lowpass=f=14000,"          # 14 kHz ceiling
        "asplit=2[dry][verb];"
        f"[verb]{_aecho(taps, gains, 0.8, 0.85)},"
        "extrastereo=m=1.3,"
        "volume=0.28[wet];"
        "[dry]volume=1.0[dryw];"
        "[dryw][wet]amix=inputs=2:normalize=0:duration=first,"
        # Loudness target with a true-peak ceiling, then belt-and-braces limiter.
        "loudnorm=I=-14:TP=-1:LRA=11,"
        f"{_SAFE_TAIL}[out]"
    )


def _graph_vinyl(duration: float | None) -> str:
    """Vinyl: audible but soft crackle, restrained hiss and gentle wow/flutter."""
    dur = f"{max(duration or 0.0, 0.0):.3f}" if duration else "30"
    crackle = _crackle(float(dur), rate=0.00028, level=0.36)  # ~12 pops/s @ -35 dB
    hiss = _hiss(0.008, float(dur))
    return (
        f"[0:a]{_STEREO},"
        # Wow + flutter: gentle 0.7 Hz pitch wobble at 0.2% depth.
        "vibrato=f=0.7:d=0.002,"
        # 1.1x drive into a tanh soft clip = warm, not distorted.
        "volume=1.1,asoftclip=type=tanh:param=1.0:oversample=2,"
        "lowpass=f=13500,highpass=f=35,"
        "extrastereo=m=0.95[mus];"
        f"{crackle}[crk];"
        f"{hiss}[hs];"
        # Noise is mixed at its own level; music stays full scale.
        "[mus][crk][hs]amix=inputs=3:normalize=0:duration=first,"
        f"{_SAFE_TAIL}[out]"
    )


def _graph_lofi(duration: float | None) -> str:
    """Lo-fi: softly grainy 12-bit texture, softened top end and tape motion."""
    dur = f"{max(duration or 0.0, 0.0):.3f}" if duration else "30"
    hiss = _hiss(0.010, float(dur))
    # floor(x*2048)/2048 -> 12-bit steps; +0.5 == round-to-nearest.
    quant = (
        "aeval=exprs="
        "'floor(val(0)*2048+0.5)/2048|floor(val(1)*2048+0.5)/2048'"
    )
    return (
        f"[0:a]{_STEREO},"
        # 44.1 -> 22.05 -> 44.1: the 22.05 kHz band-limit a tape/digest chain
        # imposes, without the pitch shift naive resampling would cause.
        f"aresample=22050,aresample={SAMPLE_RATE},"
        f"{quant},"              # 12-bit grid
        "vibrato=f=0.35:d=0.0015,"
        "volume=1.1,asoftclip=type=tanh:param=1.0:oversample=2,"
        "lowpass=f=9500,highpass=f=60,"
        "extrastereo=m=0.9[mus];"
        f"{hiss}[hs];"
        "[mus][hs]amix=inputs=2:normalize=0:duration=first,"
        f"{_SAFE_TAIL}[out]"
    )


PRESETS: dict = {
    "8d": {
        "key": "8d",
        "label": "8D",
        "suffix": "(8D)",
        "build": _graph_8d,
        "notes": "clear 0.125 Hz orbit, wider stereo image, short reflection trail",
        "headphones": True,
    },
    "360": {
        "key": "360",
        "label": "360°",
        "suffix": "(360°)",
        "build": _graph_360,
        "notes": "slow full stereo orbit, spacious side reflections, bass centred",
        "headphones": True,
    },
    "3d": {
        "key": "3d",
        "label": "3D",
        "suffix": "(3D)",
        "build": _graph_3d,
        "notes": "gentle depth, short early reflections, soft width and centred bass",
        "headphones": True,
    },
    "concert": {
        "key": "concert",
        "label": "Concert",
        "suffix": "(Concert)",
        "build": _graph_concert,
        "notes": "spacious early reflections, soft wide decay, clear dry signal",
    },
    "slowed": {
        "key": "slowed",
        "label": "Slowed",
        "suffix": "(Slowed)",
        "build": _graph_slowed,
        "notes": "0.85x pitch-off, warm reverb cloud, -14 LUFS / -1 dBTP",
    },
    "vinyl": {
        "key": "vinyl",
        "label": "Vinyl",
        "suffix": "(Vinyl)",
        "build": _graph_vinyl,
        "notes": "surface crackle, warm rolloff, gentle wow/flutter and hiss",
    },
    "lofi": {
        "key": "lofi",
        "label": "LoFi",
        "suffix": "(LoFi)",
        "build": _graph_lofi,
        "notes": "soft 12-bit grain, softened top end, tape hiss and motion",
    },
}

def label(name: str) -> str:
    return PRESETS[name]["label"] if name in PRESETS else "Off"


async def probe_duration(path: Path | str) -> float | None:
    """Shared by the presets that need to size a synthetic noise source."""
    try:
        proc = await asyncio.create_subprocess_exec(
            FFPROBE, "-v", "error", "-show_entries", "format=duration",
            "-of", "default=nw=1:nk=1", str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        return float(out.decode().strip())
    except Exception:
        return None


def build_ffmpeg_args(
    src: Path | str,
    out: Path | str,
    effect: str,
    *,
    duration: float | None = None,
    bitrate: str = DEFAULT_BITRATE,
) -> list[str]:
    """Full argv for one effect render (exposed for tests/benchmarks)."""
    spec = PRESETS[effect]
    graph = spec["build"](duration)
    return [
        FFMPEG, "-hide_banner", "-nostdin", "-y", "-v", "error",
        "-i", str(src),
        "-filter_complex", graph,
        "-map", "[out]",
        "-c:a", "libmp3lame", "-b:a", bitrate,
        "-ar", str(SAMPLE_RATE), "-ac", "2",
        "-threads", "0",
        "-f", "mp3", str(out),
    ]


def render_file(
    src: Path | str,
    out: Path | str,
    effect: str,
    *,
    duration: float | None = None,
    bitrate: str = DEFAULT_BITRATE,
    timeout: float = 300.0,
) -> Path:
    """Sync single-pass render. Raises RuntimeError on failure."""
    if effect not in PRESETS:
        raise KeyError(f"unknown effect: {effect!r}")
    argv = build_ffmpeg_args(src, out, effect, duration=duration, bitrate=bitrate)
    proc = subprocess.run(argv, capture_output=True, timeout=timeout)
    if proc.returncode != 0 or not Path(out).exists() or Path(out).stat().st_size < 1024:
        raise RuntimeError(
            f"Effect {effect} failed: {proc.stderr.decode(errors='replace')[:300]}"
        )
    return Path(out)


async def render_to_path(
    src: Path | str,
    out: Path | str,
    effect: str,
    *,
    duration: float | None = None,
    bitrate: str = DEFAULT_BITRATE,
    timeout: float = 300.0,
) -> tuple[Path, float]:
    """Async render (no thread, no blocking) -> (path, elapsed_seconds)."""
    import time

    if effect not in PRESETS:
        raise KeyError(f"unknown effect: {effect!r}")
    if duration is None:
        duration = await probe_duration(src)
    argv = build_ffmpeg_args(src, out, effect, duration=duration, bitrate=bitrate)
    t0 = time.perf_counter()
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"Effect {effect} timed out after {timeout:.0f}s")
    elapsed = time.perf_counter() - t0
    if proc.returncode != 0 or not Path(out).exists() or Path(out).stat().st_size < 1024:
        raise RuntimeError(
            f"Effect {effect} failed: {err.decode(errors='replace')[:300]}"
        )
    return Path(out), elapsed


async def render_bytes(
    audio: bytes,
    effect: str,
    *,
    cover_bytes: bytes | None = None,
    bitrate: str = DEFAULT_BITRATE,
    postprocess: Callable[[Path], bool] | None = None,
) -> tuple[bytes, float]:
    """Render an effect on in-memory audio; returns (mp3 bytes, seconds).

    One temp dir, two files, both deleted here — the bot never sees disk.
    `postprocess` lets the caller attach cover art (stream-copy, no re-encode)
    without emyfx needing to know about bot.py.
    """
    out = None
    with tempfile.TemporaryDirectory(prefix="emyfx_") as tmp:
        src = Path(tmp) / "in.mp3"
        out = Path(tmp) / f"out_{effect}.mp3"
        src.write_bytes(audio)
        _, elapsed = await render_to_path(src, out, effect, bitrate=bitrate)
        if cover_bytes and postprocess is not None:
            try:
                postprocess(out)
            except Exception as e:  # art is cosmetic; never fail the render
                logger.warning("Cover re-attach failed: %s", str(e)[:150])
        data = out.read_bytes()
    return data, elapsed
