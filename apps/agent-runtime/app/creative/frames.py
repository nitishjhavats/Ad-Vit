"""Turn a video into what a vision model can look at.

The `judgement` class is Claude, which reads images and not video. So a video
becomes a handful of JPEG frames chosen to answer the rubric's questions - dense
across the first three seconds, where the hook lives, then sparse across the
rest - plus the metadata ffmpeg can read directly.

ffmpeg comes from `imageio-ffmpeg`, which ships a static binary for every
platform the runtime runs on. No system package, no apt line in the Dockerfile,
and the same binary on the developer's Windows machine and in the Linux image.

What this does NOT do, and says so: audio. There is no speech-to-text here, so
spoken claims are not analysed - only what is on screen. A creative whose only
claim is in the voiceover will score well on `sound_off` for the wrong reason
and be missed by the compliance pre-check. The rating carries this as a
limitation, and Platform Watch's job is not to hide limitations.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

# Where the frames are taken from, in seconds. The first three seconds decide
# the scroll, so they get four frames; the rest of a thirty-second ad gets one
# every few seconds. Twelve frames is enough to judge and few enough to keep a
# single model call under a reasonable image budget.
HOOK_SAMPLE_AT = (0.0, 1.0, 2.0, 3.0)
BODY_SAMPLES = 8
MAX_FRAMES = len(HOOK_SAMPLE_AT) + BODY_SAMPLES

FRAME_WIDTH = 540  # 9:16 at this width is 540x960 - readable text, small file


@dataclass(frozen=True, slots=True)
class Metadata:
    width: int | None
    height: int | None
    duration_s: float | None
    fps: float | None
    has_audio: bool
    codec: str | None


@dataclass(frozen=True, slots=True)
class Frame:
    at_s: float
    jpeg: bytes
    # "hook" for the first three seconds, "body" after. Carried so the prompt
    # can label the frames and the model knows which ones decide the scroll.
    phase: str


@dataclass(frozen=True, slots=True)
class Extracted:
    metadata: Metadata
    frames: tuple[Frame, ...]
    limitations: tuple[str, ...] = field(default_factory=tuple)


def _ffmpeg() -> str:
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def probe(path: Path) -> Metadata:
    """Read what the container says about itself.

    `ffmpeg -i` with no output prints the stream table to stderr and exits
    non-zero; that is the documented way to probe without ffprobe, which
    imageio-ffmpeg does not ship.
    """
    proc = subprocess.run(
        [_ffmpeg(), "-hide_banner", "-i", str(path)],
        capture_output=True, text=True, timeout=60,
    )
    info = proc.stderr

    import re

    width = height = None
    m = re.search(r"Video:.*?\b(\d{2,5})x(\d{2,5})\b", info)
    if m:
        width, height = int(m.group(1)), int(m.group(2))

    duration = None
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", info)
    if m:
        h, mi, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
        duration = h * 3600 + mi * 60 + s

    fps = None
    m = re.search(r"(\d+(?:\.\d+)?)\s*fps", info)
    if m:
        fps = float(m.group(1))

    codec = None
    m = re.search(r"Video:\s*(\w+)", info)
    if m:
        codec = m.group(1)

    return Metadata(
        width=width, height=height, duration_s=duration, fps=fps,
        has_audio="Audio:" in info, codec=codec,
    )


def sample_times(duration_s: float | None) -> list[tuple[float, str]]:
    """Where to take frames, and which phase each belongs to."""
    if duration_s is None or duration_s <= 0:
        return [(t, "hook") for t in HOOK_SAMPLE_AT]

    times: list[tuple[float, str]] = [
        (t, "hook") for t in HOOK_SAMPLE_AT if t < duration_s
    ]
    body_start = HOOK_SAMPLE_AT[-1]
    if duration_s > body_start + 1.0:
        span = duration_s - body_start
        step = span / (BODY_SAMPLES + 1)
        times += [
            (round(body_start + step * (i + 1), 2), "body") for i in range(BODY_SAMPLES)
        ]
    return times[:MAX_FRAMES]


def extract(path: Path) -> Extracted:
    """Frames at the sample times, as JPEG bytes, plus the metadata."""
    meta = probe(path)
    limitations: list[str] = []

    if meta.has_audio:
        limitations.append(
            "the audio track was not analysed: no speech-to-text is wired, so "
            "spoken claims are not checked and a message carried only by the "
            "voiceover is invisible to this rating"
        )
    else:
        limitations.append("the file carries no audio track")

    frames: list[Frame] = []
    with tempfile.TemporaryDirectory(prefix="advit-frames-") as tmp:
        for at, phase in sample_times(meta.duration_s):
            out = Path(tmp) / f"frame_{at:07.2f}.jpg"
            # `-ss` before `-i` seeks on keyframes and is fast; `-frames:v 1`
            # takes exactly one. Scaled to a fixed width with height following,
            # so a 1080x1920 upload and a 540x960 one produce the same input.
            proc = subprocess.run(
                [
                    _ffmpeg(), "-hide_banner", "-loglevel", "error",
                    "-ss", f"{at:.2f}", "-i", str(path),
                    "-frames:v", "1",
                    "-vf", f"scale={FRAME_WIDTH}:-2",
                    "-q:v", "4",
                    str(out),
                ],
                capture_output=True, text=True, timeout=120,
            )
            if proc.returncode != 0 or not out.exists():
                # Past the end, or a broken container. Recorded, not raised: a
                # 9-second video asked for a frame at 12s is not an error.
                continue
            frames.append(Frame(at_s=at, jpeg=out.read_bytes(), phase=phase))

    if not frames:
        limitations.append("no frames could be extracted; the file may not be a video")

    return Extracted(metadata=meta, frames=tuple(frames), limitations=tuple(limitations))


# ---------------------------------------------------------------------------
# A synthetic video, for tests and for a smoke check with no real ad to hand
# ---------------------------------------------------------------------------


def _font_file() -> str | None:
    """A TrueType font for drawtext, or None.

    imageio-ffmpeg's static build has no fontconfig fallback, so `drawtext`
    without an explicit fontfile exits non-zero on a machine that has no
    default font wired up (a stock CI runner). Set ADVIT_FONT_FILE to override.
    """
    candidates = [
        os.environ.get("ADVIT_FONT_FILE", ""),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ]
    for c in candidates:
        if c and Path(c).is_file():
            return c
    return None


def synthesize(path: Path, *, seconds: float = 6.0, width: int = 540, height: int = 960,
               with_audio: bool = False, text: str | None = None) -> Path:
    """Render a test-pattern video with ffmpeg's own generators.

    Deterministic, tiny, and needs no fixture file in the repository. `text`
    burns a caption in, which is enough to exercise the on-screen-text path of
    the analysis without a real creative.
    """
    vf = f"testsrc2=size={width}x{height}:rate=10"
    inputs = ["-f", "lavfi", "-i", f"{vf}:duration={seconds}"]
    if with_audio:
        inputs += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]

    filters = []
    if text:
        safe = text.replace("'", r"\'").replace(":", r"\:")
        font = _font_file()
        fontopt = ""
        if font:
            fontopt = "fontfile='" + font.replace("\\", "/").replace(":", r"\:") + "':"
        filters.append(
            f"drawtext={fontopt}text='{safe}':x=(w-text_w)/2:y=h-120:fontsize=40:"
            "fontcolor=white:box=1:boxcolor=black@0.6"
        )

    cmd = [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", *inputs]
    if filters:
        cmd += ["-vf", ",".join(filters)]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast"]
    if with_audio:
        cmd += ["-c:a", "aac", "-shortest"]
    cmd.append(str(path))

    subprocess.run(cmd, check=True, capture_output=True, timeout=120)
    return path


def describe(extracted: Extracted) -> dict:
    """A JSON-safe summary, for the rating record and for logs."""
    return {
        "metadata": {
            "width": extracted.metadata.width,
            "height": extracted.metadata.height,
            "duration_s": extracted.metadata.duration_s,
            "fps": extracted.metadata.fps,
            "has_audio": extracted.metadata.has_audio,
            "codec": extracted.metadata.codec,
        },
        "frames": [{"at_s": f.at_s, "phase": f.phase, "bytes": len(f.jpeg)} for f in extracted.frames],
        "limitations": list(extracted.limitations),
    }


__all__ = ["Extracted", "Frame", "Metadata", "describe", "extract", "probe", "sample_times", "synthesize"]

# json is imported for callers that serialise `describe()`; kept explicit.
_ = json
