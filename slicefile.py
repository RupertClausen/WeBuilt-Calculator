"""Parse sliced files from OrcaSlicer / Bambu Studio.

Two shapes are supported:

  *.gcode / *.gco      plain text, metadata in a comment block at the end
  *.gcode.3mf / *.3mf  zip archive, metadata in Metadata/slice_info.config

The 3mf path is preferred when available: slice_info.config states the
per-filament grams and the predicted seconds as structured XML, so nothing
has to be inferred.

`parse()` returns a LIST of SlicedJob, one per PLATE. A plain .gcode gives
one; a .gcode.3mf holding three plates gives three, each with its own grams
and duration. They are kept apart on purpose: the failure model is
exponential in duration and does not aggregate, so a 2 h plate and a 20 h
plate must never be flattened into two 11 h plates.
"""

from __future__ import annotations

import base64
import binascii
import io
import re
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field


class ParseError(Exception):
    """The upload was not a sliced file we can read."""


@dataclass
class FilamentUse:
    type: str                 # "PLA", "PETG", ...
    grams: float
    colour: str | None = None
    brand: str | None = None


@dataclass
class SlicedJob:
    filaments: list[FilamentUse]
    seconds: float
    printer_model: str | None = None
    plates: int = 1
    plate_index: int = 1      # which plate of its source file
    source: str = ""          # which parser produced this
    warnings: list[str] = field(default_factory=list)
    # The plate picture the slicer embedded — the same image the printer's
    # screen shows. None when the file carries no usable one.
    thumbnail: bytes | None = field(default=None, repr=False)
    thumbnail_mime: str | None = None

    @property
    def hours(self) -> float:
        return self.seconds / 3600.0

    @property
    def total_grams(self) -> float:
        return sum(f.grams for f in self.filaments)


# ---------------------------------------------------------------------------
# duration helpers
# ---------------------------------------------------------------------------

_DUR_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([dhms])", re.I)


def parse_duration(text: str) -> float | None:
    """'1d 2h 3m 4s' -> seconds. Returns None if nothing parseable."""
    if text is None:
        return None
    text = text.strip()
    if not text:
        return None

    # A bare number is already seconds (Bambu's `prediction` field).
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return float(text)

    scale = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    total = 0.0
    found = False
    for value, unit in _DUR_RE.findall(text):
        total += float(value) * scale[unit.lower()]
        found = True
    return total if found else None


def _split_multi(raw: str) -> list[str]:
    """Slicers list per-extruder values as 'a;b' or 'a,b'."""
    parts = re.split(r"[;,]", raw)
    return [p.strip() for p in parts if p.strip()]


# ---------------------------------------------------------------------------
# thumbnails
# ---------------------------------------------------------------------------
# Slicers embed a picture of each plate at slice time; printers just display
# it. Only PNG and JPEG are kept, because those are what a browser can show.
# Orca can also write QOI, which browsers cannot display, so it is skipped.
# The type is decided by the bytes, never by the label in the file.

MAX_THUMBNAIL_BYTES = 1024 * 1024


def image_mime(data: bytes | None) -> str | None:
    if not data:
        return None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    return None


def _usable(data: bytes | None) -> tuple[bytes, str] | None:
    mime = image_mime(data)
    if mime and len(data) <= MAX_THUMBNAIL_BYTES:
        return data, mime
    return None


def thumbnail_from_3mf(zf: zipfile.ZipFile, plate_index: int) -> tuple[bytes, str] | None:
    """Bambu Studio and Orca store `Metadata/plate_<n>.png` per plate, the
    picture the printer shows. Fall back to the small or top-down versions."""
    names = set(zf.namelist())
    for name in (f"Metadata/plate_{plate_index}.png",
                 f"Metadata/plate_{plate_index}_small.png",
                 f"Metadata/top_{plate_index}.png"):
        if name in names:
            info = zf.getinfo(name)
            if info.file_size > MAX_THUMBNAIL_BYTES:
                continue
            hit = _usable(zf.read(name))
            if hit:
                return hit
    return None


_THUMB_BEGIN = re.compile(
    r"^;\s*thumbnail(?:_(PNG|JPG|JPEG|QOI))?\s+begin\s+(\d+)\s*x\s*(\d+)", re.I)
_THUMB_END = re.compile(r"^;\s*thumbnail(?:_\w+)?\s+end", re.I)


def thumbnails_from_gcode(lines: list[str]) -> list[tuple[int, bytes, str]]:
    """Every displayable `; thumbnail begin WxH …` block, as (pixels, bytes, mime)."""
    found = []
    buf: list[str] | None = None
    area = 0
    for line in lines:
        if buf is None:
            m = _THUMB_BEGIN.match(line)
            if m:
                buf, area = [], int(m.group(2)) * int(m.group(3))
            continue
        if _THUMB_END.match(line):
            try:
                data = base64.b64decode("".join(buf), validate=False)
            except (binascii.Error, ValueError):
                data = None
            hit = _usable(data)
            if hit:
                found.append((area, *hit))
            buf = None
            continue
        buf.append(line.lstrip(";").strip())
        if sum(map(len, buf)) > MAX_THUMBNAIL_BYTES * 4 // 3 + 4:
            buf = None          # too big to keep; stop collecting this block
    return found


# ---------------------------------------------------------------------------
# .gcode.3mf  (preferred)
# ---------------------------------------------------------------------------

def parse_3mf(data: bytes) -> list[SlicedJob]:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ParseError("File looks like a 3MF but is not a valid zip archive.") from exc

    names = set(zf.namelist())
    if "Metadata/slice_info.config" not in names:
        raise ParseError(
            "This 3MF has no Metadata/slice_info.config — it is a project file, "
            "not a sliced one. Use 'Export plate sliced file' in Orca/Bambu Studio."
        )

    root = ET.fromstring(zf.read("Metadata/slice_info.config"))

    jobs: list[SlicedJob] = []

    for index, plate in enumerate(root.findall(".//plate"), start=1):
        meta = {
            m.get("key"): m.get("value")
            for m in plate.findall("metadata")
            if m.get("key")
        }
        warnings: list[str] = []

        seconds = parse_duration(meta.get("prediction", ""))
        if not seconds:
            warnings.append(f"Plate {index}: no time prediction in slice_info.")
            continue

        filaments = [
            FilamentUse(
                type=(fil.get("type") or "UNKNOWN").strip().upper(),
                grams=float(fil.get("used_g")),
                colour=fil.get("color"),
            )
            for fil in plate.findall("filament")
            if fil.get("used_g") and float(fil.get("used_g")) > 0
        ]
        if not filaments:
            continue

        plate_index = int(meta.get("index", index) or index)
        thumb = thumbnail_from_3mf(zf, plate_index)
        jobs.append(SlicedJob(
            filaments=_merge_same_type(filaments),
            seconds=seconds,
            printer_model=meta.get("printer_model_id"),
            plates=1,
            plate_index=plate_index,
            source="3mf/slice_info.config",
            warnings=warnings,
            thumbnail=thumb[0] if thumb else None,
            thumbnail_mime=thumb[1] if thumb else None,
        ))

    if not jobs:
        raise ParseError(
            "slice_info.config listed no usable plate (no filament usage or "
            "no time prediction)."
        )
    return jobs


# ---------------------------------------------------------------------------
# plain .gcode
# ---------------------------------------------------------------------------

# Orca writes `; key = value`, Bambu Studio writes `; key: value` for some
# of its own summary lines. Accept either.
_KV_RE = re.compile(r"^;\s*([^:=]+?)\s*[:=]\s*(.+?)\s*$")

_TIME_KEYS = (
    "estimated printing time (normal mode)",
    "total estimated time",
    "model printing time",
    "estimated printing time",
)
_GRAM_KEYS = (
    "filament used [g]",
    "total filament used [g]",
    "total filament weight [g]",
    "filament used_g",
)


def parse_gcode(text: str) -> list[SlicedJob]:
    lines = text.splitlines()
    kv: dict[str, str] = {}
    in_thumb = False
    for line in lines:
        if not line.startswith(";"):
            continue
        # base64 lines can end in "=" and would otherwise read as settings
        if in_thumb:
            in_thumb = not _THUMB_END.match(line)
            continue
        if _THUMB_BEGIN.match(line):
            in_thumb = True
            continue
        m = _KV_RE.match(line)
        if m:
            kv[m.group(1).strip().lower()] = m.group(2).strip()

    if not kv:
        raise ParseError("No slicer metadata comments found in this G-code.")

    warnings: list[str] = []

    # --- time ---
    seconds = None
    for key in _TIME_KEYS:
        if key in kv:
            seconds = parse_duration(kv[key])
            if seconds:
                break
    if not seconds:
        raise ParseError(
            "Could not find an estimated print time. Was this sliced by "
            "OrcaSlicer or Bambu Studio?"
        )

    # --- grams, per extruder ---
    grams_raw = None
    for key in _GRAM_KEYS:
        if key in kv:
            grams_raw = kv[key]
            break
    if grams_raw is None:
        raise ParseError(
            "Could not find filament weight. Enable 'Verbose G-code comments' "
            "in the slicer's output options."
        )
    grams = [float(g) for g in _split_multi(grams_raw)]

    # --- types, per extruder ---
    types_raw = kv.get("filament_type") or kv.get("filament type") or ""
    types = [t.upper() for t in _split_multi(types_raw)]

    if not types:
        warnings.append("No filament_type in the file; material set to UNKNOWN.")
        types = ["UNKNOWN"] * len(grams)
    elif len(types) < len(grams):
        # Single type declared for a multi-extruder job
        types = types * len(grams) if len(types) == 1 else types + ["UNKNOWN"] * (len(grams) - len(types))

    filaments = [
        FilamentUse(type=t, grams=g)
        for t, g in zip(types, grams)
        if g > 0
    ]
    if not filaments:
        raise ParseError("The file reports zero filament used.")

    printer_model = kv.get("printer_model") or kv.get("printer_settings_id")

    # Slicers often embed several sizes; the largest looks best on a receipt.
    thumbs = sorted(thumbnails_from_gcode(lines), key=lambda t: t[0])
    best = thumbs[-1] if thumbs else None

    return [SlicedJob(
        filaments=_merge_same_type(filaments),
        seconds=seconds,
        printer_model=printer_model,
        plates=1,
        source="gcode comments",
        warnings=warnings,
        thumbnail=best[1] if best else None,
        thumbnail_mime=best[2] if best else None,
    )]


def _merge_same_type(items: list[FilamentUse]) -> list[FilamentUse]:
    """Two AMS slots of the same material price identically — combine them."""
    merged: dict[str, FilamentUse] = {}
    for f in items:
        if f.type in merged:
            merged[f.type].grams += f.grams
        else:
            merged[f.type] = FilamentUse(type=f.type, grams=f.grams, colour=f.colour)
    return list(merged.values())


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def parse(filename: str, data: bytes) -> list[SlicedJob]:
    """Returns one SlicedJob PER PLATE. Never flattens plates together."""
    name = (filename or "").lower()

    if data[:2] == b"PK":                     # any zip, regardless of extension
        return parse_3mf(data)

    if name.endswith((".gcode", ".gco", ".g", ".nc", ".txt")) or data[:1] in (b";", b"M", b"G"):
        try:
            text = data.decode("utf-8", errors="replace")
        except Exception as exc:              # pragma: no cover
            raise ParseError("Could not decode the file as text.") from exc
        return parse_gcode(text)

    raise ParseError(
        f"Unrecognised file type: {filename!r}. Upload a .gcode or .gcode.3mf "
        "exported from OrcaSlicer or Bambu Studio."
    )
