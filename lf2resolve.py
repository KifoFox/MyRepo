#!/usr/bin/env python3
"""Convert a LumaFusion project package (.lfpackage) into a timeline DaVinci
Resolve can import (Final Cut Pro 7 XML, "xmeml").

A .lfpackage is a zip holding the project's media plus a JSON timeline
(<title>.lfarchive). This tool extracts the media, decodes the timeline and
writes an XML file that Resolve opens via File > Import > Timeline.

Usage:
    python3 lf2resolve.py MyProject.lfpackage [-o OUTPUT_DIR]
    python3 lf2resolve.py MyProject.lfpackage --inspect

Only the Python 3 standard library is required.
"""

import argparse
import json
import math
import os
import shutil
import struct
import sys
import zipfile
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from urllib.parse import quote, unquote, urlparse
from xml.dom import minidom
from xml.etree import ElementTree as ET

__version__ = "0.1.0"

# LumaFusion asset / stream codes observed in project files.
ASSET_TRANSITION = 3
ASSET_TITLE = 4
STREAM_VIDEO = 0
STREAM_AUDIO = 1
STREAM_TITLE = 3
TRACK_AUDIO_ONLY = 1

# --------------------------------------------------------------------------
# Transform conversion ASSUMPTIONS
#
# Neither app documents these conventions, and none of them has been checked
# with the two apps side by side yet. If zoom, position, rotation, crop or
# keyframe timing comes out wrong in Resolve, change the setting below.
# --------------------------------------------------------------------------

# LumaFusion zoom 1.0 = the clip fitted to the frame. FCP7 XML "scale" is a
# percentage of the clip's native pixel size, so 1080p media in a UHD
# timeline at LumaFusion zoom 1.0 is written as 200%. Set False to write the
# LumaFusion zoom as-is (1.0 -> 100%).
SCALE_RELATIVE_TO_NATIVE = True

# LumaFusion position is the offset of the clip's centre from the frame
# centre, as a fraction of the frame's width and height. True: +y is up
# (Core Image style). FCP7 "center" uses the same fractions with +y down.
LF_POSITION_Y_UP = True

# LumaFusion rotation is in radians. True: positive = counter-clockwise.
# FCP7 rotation is in degrees, positive = clockwise.
LF_ROTATION_CCW = True

# LumaFusion crop is [[x, y], [width, height]] of the part of the source
# that stays visible, as fractions. True: y is measured from the bottom.
LF_CROP_Y_UP = True

# LumaFusion "videoFitMode" codes treated as "fill" (cover the frame);
# every other code is treated as "fit" (letterbox). Only 0 has been seen.
LF_FILL_MODES = {1}

# FCP7 keyframe <when>: True = a frame number in the source media (the
# clip's in point plus the offset); False = frames from the clip's start.
KEYFRAME_WHEN_IS_SOURCE_FRAME = True


def fcp_scale(lf_scale, fit):
    return lf_scale * (fit if SCALE_RELATIVE_TO_NATIVE else 1) * 100


def fcp_center(point):
    x, y = (float(c) for c in point)
    return (x, -y if LF_POSITION_Y_UP else y)


def fcp_rotation(radians):
    deg = math.degrees(radians)
    return -deg if LF_ROTATION_CCW else deg


def fcp_crop(rect):
    """LumaFusion crop rect -> FCP7 crop (left, right, top, bottom) in percent."""
    (x, y), (w, h) = ((float(a), float(b)) for a, b in rect)
    low, high = y * 100, (1 - y - h) * 100
    top, bottom = (high, low) if LF_CROP_Y_UP else (low, high)
    clamp = lambda v: round(max(0.0, min(100.0, v)), 4)
    return clamp(x * 100), clamp((1 - x - w) * 100), clamp(top), clamp(bottom)


def fit_factor(media_w, media_h, frame_w, frame_h, fit_mode):
    if not media_w or not media_h:
        return 1.0
    sx, sy = frame_w / media_w, frame_h / media_h
    return max(sx, sy) if fit_mode in LF_FILL_MODES else min(sx, sy)


IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff", ".bmp", ".gif", ".webp"}
AUDIO_EXTS = {".m4a", ".mp3", ".wav", ".aif", ".aiff", ".caf", ".aac", ".flac"}


# Attributes converted into FCP7 filters (anything else keyframed gets a marker).
HANDLED_ATTRS = {"videoScale", "videoTransformRotation", "videoRotation", "videoTranslation",
                 "videoCrop", "videoAlpha", "audioVolume", "audioPan"}


def cmtime(t):
    """Convert a serialized CMTime dict to an exact Fraction of seconds."""
    if not t or not t.get("timescale"):
        return Fraction(0)
    return Fraction(t["value"], t["timescale"])


def attr(stream, name, default=None):
    a = stream.get("streamAttributes", {}).get(name)
    if not a:
        return default
    return a.get("value", {}).get("value", default)


def has_keyframes(stream, name):
    a = stream.get("streamAttributes", {}).get(name)
    return bool(a and a.get("keyframes"))


def db(gain):
    return -math.inf if gain <= 0 else 20 * math.log10(gain)


def color_hex(rgba):
    """'1.0 1.0 0.0 1.0' -> '#FFFF00'."""
    try:
        r, g, b = (float(x) for x in str(rgba).split()[:3])
    except ValueError:
        return str(rgba)
    return "#" + "".join(f"{round(max(0, min(1, c)) * 255):02X}" for c in (r, g, b))


def fmt_time(sec):
    sec = float(sec)
    return f"{int(sec // 60)}:{sec % 60:06.3f}"


# --------------------------------------------------------------------------
# Rates
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Rate:
    fps: Fraction

    @classmethod
    def from_step(cls, step):
        s = cmtime(step)
        return cls(Fraction(1) / s if s else Fraction(30))

    @property
    def timebase(self):
        return round(self.fps)

    @property
    def ntsc(self):
        return self.fps.denominator == 1001

    def frames(self, seconds):
        return int(round(seconds * self.fps))

    def xml(self, parent):
        r = ET.SubElement(parent, "rate")
        ET.SubElement(r, "timebase").text = str(self.timebase)
        ET.SubElement(r, "ntsc").text = "TRUE" if self.ntsc else "FALSE"
        return r


# --------------------------------------------------------------------------
# Media probing (just enough MP4/MOV parsing for durations)
# --------------------------------------------------------------------------

def mp4_duration(path):
    """Return the movie duration in seconds from the mvhd atom, or None."""
    try:
        with open(path, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            return _find_mvhd(f, 0, size)
    except OSError:
        return None


def _find_mvhd(f, start, end):
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        hdr = f.read(8)
        if len(hdr) < 8:
            return None
        size, kind = struct.unpack(">I4s", hdr)
        header = 8
        if size == 1:
            size = struct.unpack(">Q", f.read(8))[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header:
            return None
        if kind == b"moov":
            return _find_mvhd(f, pos + header, pos + size)
        if kind == b"mvhd":
            version = f.read(1)[0]
            f.read(3)
            if version == 1:
                _, _, timescale, duration = struct.unpack(">QQIQ", f.read(28))
            else:
                _, _, timescale, duration = struct.unpack(">IIII", f.read(16))
            return Fraction(duration, timescale) if timescale else None
        pos += size
    return None


# --------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------

@dataclass
class Media:
    name: str
    path: Path
    kind: str  # "video", "audio" or "image"
    width: int = 0
    height: int = 0
    samplerate: int = 48000
    rate: Rate = None
    duration: Fraction = None  # seconds; None if unknown
    id: str = ""


@dataclass
class Item:
    """One media clip placed on the timeline (times in seconds)."""
    media: Media
    title: str
    start: Fraction
    end: Fraction
    src_in: Fraction
    has_video: bool
    has_audio: bool
    volume: float = 1.0
    pan: float = 0.0
    opacity: float = 1.0
    enabled: bool = True
    speed: Fraction = Fraction(1)
    reversed: bool = False
    notes: list = field(default_factory=list)  # things to rebuild by hand
    lf_start: Fraction = Fraction(0)  # LumaFusion's start; keyframes count from here
    anim: dict = field(default_factory=dict)  # property -> Anim, in FCP7 units

    def src_len(self, timeline_len):
        return timeline_len * self.speed

    @property
    def src_out(self):
        return self.src_in + self.src_len(self.end - self.start)

    def file_range(self):
        """Source (in, out) in the original media file's own timeline.

        LumaFusion plays reversed clips from a pre-rendered reversed copy and
        measures sourceStart in that copy, so mirror it back onto the original.
        """
        if self.reversed and self.media.duration is not None:
            d = self.media.duration
            return d - self.src_out, d - self.src_in
        return self.src_in, self.src_out


@dataclass
class Anim:
    """A clip property: its static value, keyframes and whether it was changed.

    Keyframe times are seconds from the clip's LumaFusion start (Item.lf_start).
    """
    value: object
    keys: list
    changed: bool


@dataclass
class Marker:
    start: Fraction
    end: Fraction
    name: str
    comment: str = ""


@dataclass
class Transition:
    name: str
    start: Fraction
    end: Fraction
    alignment: str  # center, start-black, end-black
    prev: "Item" = None
    nxt: "Item" = None


@dataclass
class Track:
    name: str
    offset: int
    audio_only: bool
    enabled: bool = True
    locked: bool = False
    items: list = field(default_factory=list)
    transitions: list = field(default_factory=list)


@dataclass
class Project:
    title: str
    width: int
    height: int
    rate: Rate
    duration: Fraction
    tracks: list
    media: dict
    warnings: list
    markers: list = field(default_factory=list)


# --------------------------------------------------------------------------
# Decoding the LumaFusion JSON
# --------------------------------------------------------------------------

class Decoder:
    def __init__(self, archive, media_dir):
        self.a = archive
        self.media_dir = Path(media_dir)
        self.media = {}
        self.warnings = []
        self.markers = []
        self.rate = Rate.from_step(archive.get("stepTime"))
        res = archive.get("resolution") or [1920, 1080]
        self.width, self.height = int(res[0]), int(res[1])

    def warn(self, msg):
        if msg not in self.warnings:
            self.warnings.append(msg)

    def decode(self):
        a = self.a
        res = a.get("resolution") or [1920, 1080]
        tracks = [self.track(t) for t in a.get("tracks", [])]
        tracks = [t for t in tracks if t.items]
        self.check_unsupported()
        return Project(
            title=a.get("title") or "LumaFusion Project",
            width=int(res[0]),
            height=int(res[1]),
            rate=self.rate,
            duration=cmtime(a.get("duration")),
            tracks=tracks,
            media=self.media,
            warnings=self.warnings,
            markers=self.markers,
        )

    def check_unsupported(self):
        attrs = self.a.get("attributes", {})
        n = len(attrs.get("compositionMarkers") or [])
        n += sum(len(v) for v in (attrs.get("trackMarkers") or {}).values())
        n += sum(len(v) for v in (attrs.get("assetMarkers") or {}).values())
        if n:
            self.warn(f"{n} marker(s) were not converted.")
        if self.a.get("lutDataDictionary"):
            self.warn("LUTs used in the project were not converted.")

    def track(self, t):
        offset = int(t.get("trackOffset", 0))
        audio_only = t.get("trackType") == TRACK_AUDIO_ONLY
        track = Track(
            name=t.get("title") or "",
            offset=offset,
            audio_only=audio_only,
            enabled=not t.get("hidden", False),
            locked=bool(t.get("locked", False)),
        )
        track_volume = float(t.get("trackVolume", 1.0))
        clips = sorted(t.get("clips", []), key=lambda c: cmtime(c.get("trackStart")))
        trans_idx = []
        for c in clips:
            if c.get("assetType") == ASSET_TRANSITION:
                trans_idx.append(len(track.items))
                track.items.append(c)  # resolved below
                continue
            item = self.item(c, track_volume)
            if item is not None:
                if not audio_only and item.has_audio is False and item.has_video is False:
                    continue
                track.items.append(item)
        # Resolve transitions now that their neighbours are known.
        for i in reversed(trans_idx):
            c = track.items.pop(i)
            prev = track.items[i - 1] if i > 0 and isinstance(track.items[i - 1], Item) else None
            nxt = track.items[i] if i < len(track.items) and isinstance(track.items[i], Item) else None
            tr = self.transition(c, prev, nxt)
            if tr:
                track.transitions.append(tr)
        track.transitions.sort(key=lambda tr: tr.start)
        return track

    def lookup_media(self, c):
        attrs = c.get("attributes", {})
        url = c.get("assetURL") or ""
        names = []
        if attrs.get("originalFilename"):
            names.append(attrs["originalFilename"])
        if url:
            names.append(unquote(urlparse(url).path).split("/")[-1])
        mmap = self.a.get("attributes", {}).get("mediaMap", {})
        aid = attrs.get("assetID")
        names += [n for n, v in mmap.items() if v == aid]
        for n in names:
            if n in self.media:
                return self.media[n]
        name = next((n for n in names if n), None)
        if not name:
            return None
        path = self.media_dir / name
        if not path.exists():
            self.warn(f"Media '{name}' isn't in the package; Resolve will ask you to relink it.")
        ext = path.suffix.lower()
        kind = "image" if ext in IMAGE_EXTS else "audio" if ext in AUDIO_EXTS else "video"
        size = attrs.get("naturalSize") or [0, 0]
        streams = c.get("streams", [])
        vstream = next((s for s in streams if s.get("mediaType") == STREAM_VIDEO), None)
        rate = Rate.from_step(vstream["originalStepTime"]) if vstream and vstream.get("originalStepTime") else self.rate
        m = Media(
            name=name,
            path=path,
            kind=kind,
            width=int(size[0] or 0),
            height=int(size[1] or 0),
            samplerate=int(attrs.get("audioSamplerate") or 48000),
            rate=rate,
            duration=mp4_duration(path) if kind != "image" and path.exists() else None,
            id=f"file-{len(self.media) + 1}",
        )
        self.media[name] = m
        return m

    def item(self, c, track_volume=1.0):
        title = c.get("title") or c.get("attributes", {}).get("title") or "clip"
        start = cmtime(c.get("trackStart"))
        end = start + cmtime(c.get("trackDuration"))
        where = f"'{title}' at {fmt_time(start)}"
        if c.get("assetIsBlank"):
            return None
        streams = c.get("streams", [])
        if c.get("assetType") == ASSET_TITLE or c.get("runtimeTitle") or any(
                s.get("mediaType") == STREAM_TITLE for s in streams):
            self.title_marker(c, title, start, end)
            return None
        if c.get("subComp"):
            self.warn(f"{where}: nested/multicam clips aren't supported; skipped.")
            return None
        v = next((s for s in streams if s.get("mediaType") == STREAM_VIDEO), None)
        au = next((s for s in streams if s.get("mediaType") == STREAM_AUDIO), None)
        if not v and not au:
            self.warn(f"{where}: unsupported clip type; skipped.")
            return None
        media = self.lookup_media(c)
        if media is None:
            self.warn(f"{where}: no media file referenced; skipped.")
            return None
        src = v or au
        speed = Fraction(float(src.get("sourceSpeed", 1.0))).limit_denominator(10000)
        if speed <= 0:
            speed = Fraction(1)
        if has_keyframes(src, "speed"):
            self.warn(f"{where}: variable speed (keyframed) became a constant {float(speed):g}x.")
        reversed_ = bool(c.get("reversed"))
        if reversed_ and media.duration is None:
            self.warn(f"{where}: reversed clip but media length unknown; source range may be wrong.")

        anim = {}
        if v:
            anim.update(self.video_anim(v, media))
        if au:
            anim["volume"] = self.anim(au, "audioVolume", 1.0, lambda g: float(g) * track_volume)
            anim["volume"].changed |= abs(track_volume - 1) > 1e-4
            anim["pan"] = self.anim(au, "audioPan", 0.0, float)

        notes = self.look_notes(v) if v else []
        for s in streams:
            for e in s.get("effects") or []:
                notes.append("Effect: " + self.effect_desc(e))
            handled = HANDLED_ATTRS | {"speed"}
            kf = sorted(k for k, a in s.get("streamAttributes", {}).items() if a.get("keyframes") and k not in handled)
            if kf:
                notes.append(f"Keyframed {', '.join(kf)}")
        if notes:
            self.warn(f"{where}: {'; '.join(notes)}. Added as a timeline marker to rebuild.")

        return Item(
            media=media,
            title=title,
            start=start,
            end=end,
            src_in=cmtime(src.get("sourceStart")),
            has_video=v is not None and media.kind != "audio",
            has_audio=au is not None,
            volume=anim["volume"].value if au else 1.0,
            pan=anim["pan"].value if au else 0.0,
            opacity=anim["opacity"].value / 100 if v else 1.0,
            speed=speed,
            reversed=reversed_,
            notes=notes,
            lf_start=start,
            anim=anim,
        )

    @staticmethod
    def anim(stream, name, default, conv, get=lambda v: v):
        """Read a LumaFusion attribute (static value + keyframes) into an Anim."""
        a = stream.get("streamAttributes", {}).get(name) or {}
        raw = get((a.get("value") or {}).get("value", default))
        keys = [(cmtime(k.get("clipTime")), get((k.get("value") or {}).get("value")))
                for k in a.get("keyframes") or []]
        keys = [(t, conv(val)) for t, val in keys if val is not None]
        changed = bool(keys) or raw != default
        return Anim(conv(raw if raw is not None else default), keys, changed)

    def video_anim(self, v, media):
        size = attr(v, "videoONSize") or [0, 0]
        mw, mh = (size if size and size[0] else [media.width, media.height])
        fit = fit_factor(mw, mh, self.width, self.height, attr(v, "videoFitMode", 0))
        point = lambda t: (t or {}).get("point", [0.0, 0.0]) if isinstance(t, dict) else [0.0, 0.0]
        uniform = lambda sc: float((sc or [1.0])[0])
        base_rot = float(attr(v, "videoRotation", 0.0) or 0)
        anim = {
            "scale": self.anim(v, "videoScale", 1.0, lambda s: fcp_scale(s, fit), uniform),
            "rotation": self.anim(v, "videoTransformRotation", 0.0,
                                  lambda r: fcp_rotation(float(r) + base_rot), lambda r: float(r or 0)),
            "center": self.anim(v, "videoTranslation", (0.0, 0.0), fcp_center,
                                lambda t: tuple(float(c) for c in point(t))),
            "crop": self.anim(v, "videoCrop", ((0.0, 0.0), (1.0, 1.0)), fcp_crop,
                              lambda r: tuple(tuple(float(c) for c in p) for p in (r or [[0, 0], [1, 1]]))),
            "opacity": self.anim(v, "videoAlpha", 1.0, lambda a: float(a) * 100, float),
        }
        anim["rotation"].changed |= abs(base_rot) > 1e-6
        # Rotation of exactly -0.0 is still "unchanged".
        if not anim["rotation"].keys and abs(anim["rotation"].value) < 1e-6 and not base_rot:
            anim["rotation"].changed = False
        return anim

    def title_marker(self, c, title, start, end):
        rt = c.get("runtimeTitle") or {}
        texts, styles = [], []
        for layer in rt.get("layers", []):
            text = (layer.get("text") or {}).get("string")
            if not text:
                continue
            texts.append(text)
            for run in (layer.get("text") or {}).get("runs", [])[:1]:
                a = run.get("attributes", {})
                font = a.get("font", {})
                style = [font.get("fontName", "")]
                if a.get("foregroundColor"):
                    style.append(color_hex(a["foregroundColor"]))
                if a.get("strokeColor") and a.get("strokeWidth"):
                    style.append(f"outline {color_hex(a['strokeColor'])}")
                styles.append(", ".join(x for x in style if x))
        label = " / ".join(texts) or title
        self.markers.append(Marker(start, end, f"Title: {label}",
                                   f"LumaFusion title '{title}'" + (f" ({'; '.join(styles)})" if styles else "")))
        self.warn(f"Title '{label}' at {fmt_time(start)}: added as a timeline marker to rebuild.")

    @staticmethod
    def look_notes(v):
        """Describe video settings that have no FCP7 XML equivalent."""
        notes = []
        scale = attr(v, "videoScale", [1.0, 1.0]) or [1.0, 1.0]
        if abs(float(scale[0]) - float(scale[-1])) > 1e-4:
            notes.append(f"Non-uniform zoom X {float(scale[0]):.3f} Y {float(scale[-1]):.3f} (X used for both)")
        if attr(v, "videoFlipH", False):
            notes.append("Flip horizontal")
        if attr(v, "videoFlipV", False):
            notes.append("Flip vertical")
        if attr(v, "videoBlendMode", 0):
            notes.append(f"Blend mode #{attr(v, 'videoBlendMode', 0)}")
        if float(attr(v, "videoCropCorner", 0.0) or 0) or float(attr(v, "videoCropSoftness", 0.0) or 0):
            notes.append("Crop corner rounding / softness")
        return notes

    @staticmethod
    def effect_desc(e):
        name = (e.get("displayName") or e.get("effectName") or "effect").replace("\u273c", "").strip()
        parms = []
        for k, p in (e.get("parmValues") or {}).items():
            val = (p.get("value") or {}).get("value")
            ident = (p.get("identityValue") or {}).get("value")
            if ident is not None and val != ident and isinstance(val, (int, float)):
                parms.append(f"{k[5:] if k.startswith('input') else k} {val:g}")
        desc = f"{name} [{e.get('effectName', '')}]"
        if parms:
            desc += " " + ", ".join(parms)
        if e.get("enabled") is False:
            desc += " (disabled)"
        return desc

    def transition(self, c, prev, nxt):
        name = c.get("title") or "Transition"
        start = cmtime(c.get("trackStart"))
        dur = cmtime(c.get("trackDuration"))
        end = start + dur
        tol = Fraction(1, 2) / self.rate.fps
        joins_prev = prev is not None and abs(prev.end - start) <= tol
        joins_next = nxt is not None and abs(nxt.start - end) <= tol
        where = f"Transition '{name}' at {fmt_time(start)}"
        if joins_prev and joins_next:
            # LumaFusion stores the transition as its own slot between the two
            # clips. In Resolve it straddles the cut: the outgoing clip runs on
            # to the midpoint and the incoming one starts there, each using
            # half the transition's length from its handles.
            cut = start + dur / 2
            prev.end = cut
            nxt.src_in -= nxt.src_len(nxt.start - cut)
            nxt.start = cut
            self.check_handles(prev, nxt, start, end, where)
            return Transition(name, start, end, "center", prev, nxt)
        if joins_next:
            nxt.src_in -= nxt.src_len(nxt.start - start)
            nxt.start = start
            self.check_handles(None, nxt, start, end, where)
            return Transition(name, start, end, "start-black", None, nxt)
        if joins_prev:
            prev.end = end
            self.check_handles(prev, None, start, end, where)
            return Transition(name, start, end, "end-black", prev, None)
        self.warn(f"{where}: not attached to a clip; skipped.")
        return None

    def check_handles(self, prev, nxt, start, end, where):
        if nxt is not None and nxt.src_in < 0:
            self.warn(f"{where}: not enough media before '{nxt.title}' for the full transition.")
        if prev is not None and prev.media.duration is not None:
            if prev.src_in + prev.src_len(end - prev.start) > prev.media.duration + Fraction(1, 100):
                self.warn(f"{where}: not enough media after '{prev.title}' for the full transition.")


# --------------------------------------------------------------------------
# FCP7 XML writer
# --------------------------------------------------------------------------

def path_url(path):
    p = Path(path).resolve().as_posix()
    if not p.startswith("/"):
        p = "/" + p  # Windows drive path
    return "file://localhost" + quote(p, safe="/:")


def param_value(parent, value):
    v = ET.SubElement(parent, "value")
    if isinstance(value, tuple):  # a point
        ET.SubElement(v, "horiz").text = f"{value[0] + 0.0:.6g}"  # + 0.0 turns -0.0 into 0
        ET.SubElement(v, "vert").text = f"{value[1] + 0.0:.6g}"
    else:
        v.text = f"{value + 0.0:.6g}"
    return v


def transition_effect(mediatype):
    eff = ET.Element("effect")
    if mediatype == "video":
        values = ("Cross Dissolve", "Cross Dissolve", "Dissolve")
    else:
        values = ("Cross Fade (+3dB)", "KGAudioTransCrossFade3dB", "crossfade")
    for tag, text in zip(("name", "effectid", "effectcategory"), values):
        ET.SubElement(eff, tag).text = text
    ET.SubElement(eff, "effecttype").text = "transition"
    ET.SubElement(eff, "mediatype").text = mediatype
    return eff


class XmemlWriter:
    def __init__(self, project):
        self.p = project
        self.rate = project.rate
        self.files_written = set()
        self.clip_n = 0

    def write(self, out_path):
        root = ET.Element("xmeml", version="5")
        seq = ET.SubElement(root, "sequence", id="sequence-1")
        ET.SubElement(seq, "name").text = self.p.title
        self.rate.xml(seq)
        media = ET.SubElement(seq, "media")
        video = ET.SubElement(media, "video")
        fmt = ET.SubElement(ET.SubElement(video, "format"), "samplecharacteristics")
        self.rate.xml(fmt)
        ET.SubElement(fmt, "width").text = str(self.p.width)
        ET.SubElement(fmt, "height").text = str(self.p.height)
        ET.SubElement(fmt, "pixelaspectratio").text = "square"
        ET.SubElement(fmt, "anamorphic").text = "FALSE"
        ET.SubElement(fmt, "fielddominance").text = "none"
        audio = ET.SubElement(media, "audio")
        ET.SubElement(audio, "numOutputChannels").text = "2"

        # V1 is LumaFusion's main track (offset 0); overlay tracks stack above.
        vtracks = sorted((t for t in self.p.tracks if not t.audio_only), key=lambda t: t.offset)
        # Audio follows the same order, then audio-only tracks (offset -1, -2...).
        atracks = vtracks + sorted((t for t in self.p.tracks if t.audio_only), key=lambda t: -t.offset)

        # Pre-assign clip ids so video and audio halves can link to each other.
        ids = {}
        vt_index = {}
        at_index = {}
        for ti, t in enumerate(vtracks, 1):
            for ci, it in enumerate([i for i in t.items if i.has_video], 1):
                ids[(id(it), "video")] = self.new_id()
                vt_index[id(it)] = (ti, ci)
        for ti, t in enumerate([t for t in atracks if any(i.has_audio for i in t.items)], 1):
            for ci, it in enumerate([i for i in t.items if i.has_audio], 1):
                ids[(id(it), "audio")] = self.new_id()
                at_index[id(it)] = (ti, ci)
        self.ids, self.vt_index, self.at_index = ids, vt_index, at_index

        end = 0
        for t in vtracks:
            items = [i for i in t.items if i.has_video]
            video.append(self.track(t, items, "video"))
            end = max([end] + [self.rate.frames(i.end) for i in items])
        for t in atracks:
            items = [i for i in t.items if i.has_audio]
            if items:
                audio.append(self.track(t, items, "audio"))
                end = max([end] + [self.rate.frames(i.end) for i in items])

        dur = ET.Element("duration")
        dur.text = str(end)
        seq.insert(1, dur)
        for mk in self.markers():
            seq.append(self.marker(mk))
        tc = ET.SubElement(seq, "timecode")
        self.rate.xml(tc)
        ET.SubElement(tc, "string").text = "00:00:00:00"
        ET.SubElement(tc, "frame").text = "0"
        ET.SubElement(tc, "displayformat").text = "NDF"

        xml = minidom.parseString(ET.tostring(root, encoding="unicode")).toprettyxml(indent="  ")
        xml = xml.replace('<?xml version="1.0" ?>', '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>', 1)
        Path(out_path).write_text(xml, encoding="utf-8")

    def markers(self):
        """Project markers (titles) plus one per clip with settings to rebuild."""
        out = list(self.p.markers)
        for t in self.p.tracks:
            for it in t.items:
                if it.notes:
                    out.append(Marker(it.start, it.end, f"Rebuild: {it.title}", "; ".join(it.notes)))
        return sorted(out, key=lambda mk: mk.start)

    def new_id(self):
        self.clip_n += 1
        return f"clipitem-{self.clip_n}"

    def track(self, t, items, kind):
        el = ET.Element("track")
        rate = self.rate
        # Transitions and clips must appear in timeline order.
        entries = [(i.start, 1, i) for i in items]
        for tr in t.transitions:
            neighbours = [n for n in (tr.prev, tr.nxt) if n is not None]
            if not all(n.has_video if kind == "video" else n.has_audio for n in neighbours):
                continue
            entries.append((tr.start, 0, tr))
        entries.sort(key=lambda e: (e[0], e[1]))
        for _, _, e in entries:
            if isinstance(e, Transition):
                el.append(self.transitionitem(e, kind))
            else:
                el.append(self.clipitem(e, kind))
        ET.SubElement(el, "enabled").text = "TRUE" if t.enabled else "FALSE"
        ET.SubElement(el, "locked").text = "TRUE" if t.locked else "FALSE"
        return el

    def transitionitem(self, tr, kind):
        el = ET.Element("transitionitem")
        ET.SubElement(el, "start").text = str(self.rate.frames(tr.start))
        ET.SubElement(el, "end").text = str(self.rate.frames(tr.end))
        ET.SubElement(el, "alignment").text = tr.alignment
        self.rate.xml(el)
        el.append(transition_effect(kind))
        return el

    def clipitem(self, it, kind):
        m = it.media
        el = ET.Element("clipitem", id=self.ids[(id(it), kind)])
        ET.SubElement(el, "name").text = it.title
        ET.SubElement(el, "enabled").text = "TRUE" if it.enabled else "FALSE"
        mrate = m.rate if m.kind != "image" else self.rate
        if m.duration is not None:
            ET.SubElement(el, "duration").text = str(mrate.frames(m.duration))
        mrate.xml(el)
        ET.SubElement(el, "start").text = str(self.rate.frames(it.start))
        ET.SubElement(el, "end").text = str(self.rate.frames(it.end))
        src_in, src_out = it.file_range()
        ET.SubElement(el, "in").text = str(max(0, mrate.frames(src_in)))
        ET.SubElement(el, "out").text = str(mrate.frames(src_out))
        el.append(self.file(m))
        if it.speed != 1 or it.reversed:
            el.append(self.time_remap(it, kind))
        if kind == "audio":
            st = ET.SubElement(el, "sourcetrack")
            ET.SubElement(st, "mediatype").text = "audio"
            ET.SubElement(st, "trackindex").text = "1"
        for f in self.filters(it, kind):
            el.append(f)
        for k in ("video", "audio"):
            ref = self.ids.get((id(it), k))
            if ref and (it.has_video and it.has_audio):
                link = ET.SubElement(el, "link")
                ET.SubElement(link, "linkclipref").text = ref
                ET.SubElement(link, "mediatype").text = k
                ti, ci = (self.vt_index if k == "video" else self.at_index)[id(it)]
                ET.SubElement(link, "trackindex").text = str(ti)
                ET.SubElement(link, "clipindex").text = str(ci)
        return el

    @staticmethod
    def time_remap(it, kind):
        f = ET.Element("filter")
        eff = ET.SubElement(f, "effect")
        for tag, text in (("name", "Time Remap"), ("effectid", "timeremap"), ("effectcategory", "motion"),
                          ("effecttype", "motion"), ("mediatype", kind)):
            ET.SubElement(eff, tag).text = text
        for pid, value in (("variablespeed", "0"), ("speed", f"{float(it.speed) * 100:.6g}"),
                           ("reverse", "TRUE" if it.reversed else "FALSE"), ("frameblending", "FALSE")):
            p = ET.SubElement(eff, "parameter")
            ET.SubElement(p, "parameterid").text = pid
            ET.SubElement(p, "name").text = pid
            ET.SubElement(p, "value").text = value
        return f

    def marker(self, mk):
        el = ET.Element("marker")
        ET.SubElement(el, "name").text = mk.name
        ET.SubElement(el, "comment").text = mk.comment
        ET.SubElement(el, "in").text = str(self.rate.frames(mk.start))
        ET.SubElement(el, "out").text = str(self.rate.frames(mk.end))
        return el

    def filters(self, it, kind):
        """FCP7 filters for a clip's animatable properties (only changed ones)."""
        anim = it.anim
        when = lambda t: self.keyframe_when(it, t)
        changed = lambda *names: any(n in anim and anim[n].changed for n in names)

        def param(pid, name, vmin, vmax, a, pick=lambda v: v):
            keys = [(when(t), pick(v)) for t, v in a.keys]
            return pid, name, vmin, vmax, pick(a.value), keys

        out = []
        if kind == "audio":
            if changed("volume"):
                out.append(self.filter("Audio Levels", "audiolevels", "audiolevels", "audiolevels", "audio",
                                       [param("level", "Level", 0, 3.98109, anim["volume"])]))
            if changed("pan"):
                out.append(self.filter("Audio Pan", "audiopan", "audiopan", "audiopan", "audio",
                                       [param("pan", "Pan", -1, 1, anim["pan"])]))
            return out
        if changed("scale", "rotation", "center"):
            out.append(self.filter("Basic Motion", "basic", "motion", "motion", "video", [
                param("scale", "Scale", 0, 1000, anim["scale"]),
                param("rotation", "Rotation", -8640, 8640, anim["rotation"]),
                param("center", "Center", None, None, anim["center"]),
            ]))
        if changed("crop"):
            out.append(self.filter("Crop", "crop", "motion", "motion", "video", [
                param(side, side, 0, 100, anim["crop"], lambda v, i=i: v[i])
                for i, side in enumerate(("left", "right", "top", "bottom"))
            ]))
        if changed("opacity"):
            out.append(self.filter("Opacity", "opacity", "motion", "motion", "video",
                                   [param("opacity", "opacity", 0, 100, anim["opacity"])]))
        return out

    def keyframe_when(self, it, t):
        """Frame number for a keyframe t seconds after the clip's LumaFusion start."""
        offset = it.lf_start + t - it.start  # transitions may have moved the start
        if not KEYFRAME_WHEN_IS_SOURCE_FRAME:
            return self.rate.frames(offset)
        src_in, src_out = it.file_range()
        src = src_out - offset * it.speed if it.reversed else src_in + offset * it.speed
        return it.media.rate.frames(src)

    @staticmethod
    def filter(name, effectid, category, effecttype, mediatype, params):
        f = ET.Element("filter")
        eff = ET.SubElement(f, "effect")
        ET.SubElement(eff, "name").text = name
        ET.SubElement(eff, "effectid").text = effectid
        ET.SubElement(eff, "effectcategory").text = category
        ET.SubElement(eff, "effecttype").text = effecttype
        ET.SubElement(eff, "mediatype").text = mediatype
        for pid, pname, vmin, vmax, value, keys in params:
            p = ET.SubElement(eff, "parameter")
            ET.SubElement(p, "parameterid").text = pid
            ET.SubElement(p, "name").text = pname
            if vmin is not None:
                ET.SubElement(p, "valuemin").text = str(vmin)
                ET.SubElement(p, "valuemax").text = str(vmax)
            param_value(p, value)
            for frame, kv in keys:
                k = ET.SubElement(p, "keyframe")
                ET.SubElement(k, "when").text = str(frame)
                param_value(k, kv)
        return f

    def file(self, m):
        el = ET.Element("file", id=m.id)
        if m.id in self.files_written:
            return el  # later references are empty, per the format
        self.files_written.add(m.id)
        ET.SubElement(el, "name").text = m.name
        ET.SubElement(el, "pathurl").text = path_url(m.path)
        mrate = m.rate if m.kind != "image" else self.rate
        mrate.xml(el)
        if m.duration is not None:
            ET.SubElement(el, "duration").text = str(mrate.frames(m.duration))
        media = ET.SubElement(el, "media")
        if m.kind != "audio":
            sc = ET.SubElement(ET.SubElement(media, "video"), "samplecharacteristics")
            mrate.xml(sc)
            ET.SubElement(sc, "width").text = str(m.width or self.p.width)
            ET.SubElement(sc, "height").text = str(m.height or self.p.height)
            ET.SubElement(sc, "pixelaspectratio").text = "square"
        if m.kind != "image":
            a = ET.SubElement(media, "audio")
            sc = ET.SubElement(a, "samplecharacteristics")
            ET.SubElement(sc, "depth").text = "16"
            ET.SubElement(sc, "samplerate").text = str(m.samplerate)
            ET.SubElement(a, "channelcount").text = "2"
        return el


# --------------------------------------------------------------------------
# Package handling / CLI
# --------------------------------------------------------------------------

def load_package(src, workdir):
    """Return (archive_json, media_dir). Extracts .lfpackage zips into workdir."""
    src = Path(src)
    if src.is_dir():
        archives = sorted(src.glob("*.lfarchive"))
        if not archives:
            raise SystemExit(f"No .lfarchive file found in {src}")
        return json.loads(archives[0].read_text(encoding="utf-8")), src
    if src.suffix.lower() == ".lfarchive":
        return json.loads(src.read_text(encoding="utf-8")), src.parent
    if not zipfile.is_zipfile(src):
        raise SystemExit(f"{src} is not a LumaFusion package (.lfpackage zip or .lfarchive)")
    media_dir = Path(workdir) / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    archive = None
    with zipfile.ZipFile(src) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            name = Path(info.filename).name  # flatten; never trust stored paths
            if not name or name.startswith("."):
                continue
            if name.lower().endswith(".lfarchive"):
                archive = json.loads(z.read(info).decode("utf-8"))
                continue
            dest = media_dir / name
            if not dest.exists() or dest.stat().st_size != info.file_size:
                with z.open(info) as fsrc, open(dest, "wb") as fdst:
                    shutil.copyfileobj(fsrc, fdst)
    if archive is None:
        raise SystemExit(f"No .lfarchive timeline found inside {src}")
    return archive, media_dir


def describe(project):
    lines = [
        f"Project: {project.title}",
        f"  {project.width}x{project.height} @ {float(project.rate.fps):g} fps, "
        f"length {fmt_time(project.duration)}",
    ]
    for t in sorted(project.tracks, key=lambda t: -t.offset):
        kind = "audio track" if t.audio_only else ("main track" if t.offset == 0 else f"overlay track {t.offset}")
        lines.append(f"  [{kind}]")
        events = [(i.start, f"{fmt_time(i.start)}-{fmt_time(i.end)}  {i.title}  "
                   f"(source {fmt_time(i.file_range()[0])}-{fmt_time(i.file_range()[1])}"
                   + (f", {float(i.speed):g}x" if i.speed != 1 else "")
                   + (", reversed" if i.reversed else "")
                   + (f", {db(i.volume):+.1f} dB" if abs(i.volume - 1) > 1e-4 else "") + ")")
                  for i in t.items]
        events += [(tr.start, f"{fmt_time(tr.start)}-{fmt_time(tr.end)}  <{tr.name}> transition")
                   for tr in t.transitions]
        lines += ["    " + e for _, e in sorted(events, key=lambda e: e[0])]
    if project.markers:
        lines.append("  [markers]")
        lines += [f"    {fmt_time(m.start)}-{fmt_time(m.end)}  {m.name}  ({m.comment})" for m in project.markers]
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Convert a LumaFusion .lfpackage to an XML timeline for DaVinci Resolve.")
    ap.add_argument("package", help=".lfpackage file, unzipped package folder, or .lfarchive file")
    ap.add_argument("-o", "--output", help="output folder (default: '<package name> (Resolve)' next to the package)")
    ap.add_argument("--inspect", action="store_true", help="print the decoded timeline and exit")
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args(argv)

    src = Path(args.package)
    if not src.exists():
        ap.error(f"{src} not found")
    out = Path(args.output) if args.output else src.parent / f"{src.stem} (Resolve)"

    if args.inspect and zipfile.is_zipfile(src):
        with zipfile.ZipFile(src) as z:
            name = next((n for n in z.namelist() if n.lower().endswith(".lfarchive")), None)
            if not name:
                raise SystemExit("No .lfarchive timeline found in package")
            archive, media_dir = json.loads(z.read(name)), Path(".")
    else:
        if not args.inspect:
            out.mkdir(parents=True, exist_ok=True)
        archive, media_dir = load_package(src, out)

    project = Decoder(archive, media_dir).decode()
    print(describe(project))
    if project.warnings:
        print("\nNot carried over / check in Resolve:")
        for w in project.warnings:
            print(f"  - {w}")
    if args.inspect:
        return 0

    xml_path = out / f"{project.title}.xml"
    XmemlWriter(project).write(xml_path)
    print(f"\nWrote {xml_path}")
    print(f"Media in {media_dir}")
    print("In Resolve: File > Import > Timeline..., choose the .xml file.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
