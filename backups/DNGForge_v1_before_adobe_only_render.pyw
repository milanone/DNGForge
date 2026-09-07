import sys
import os
import math
import time
import copy
import shutil
import subprocess
import tempfile

import numpy as np
import cv2
import rawpy

try:
    import exiftool
    HAVE_PYEXIFTOOL = True
except ImportError:
    HAVE_PYEXIFTOOL = False

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QSlider, QVBoxLayout,
    QHBoxLayout, QFormLayout, QPushButton, QFileDialog, QGroupBox,
    QComboBox, QSplitter, QScrollArea, QSizePolicy, QMessageBox, QCheckBox,
    QRubberBand,
)
from PySide6.QtGui import QImage, QPixmap, QAction, QPainter, QPen, QColor
from PySide6.QtCore import Qt, QTimer, Signal, QPointF, QRectF, QRect, QPoint, QSize, QThread

# Suppresses the transient console window Windows otherwise flashes open for every
# subprocess.run() call to a console-subsystem executable (exiftool.exe, Adobe DNG
# Converter.exe) — noticed by the user specifically because RethinkRAW's own background
# DNG Converter calls don't do this, making ours look comparatively janky. No-op on
# non-Windows (the attribute doesn't exist there), though this app is Windows-only anyway.
_NO_WINDOW_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)

WB_PRESETS = {
    # name -> (temperature_K, tint)
    "Daylight": (5500, 0),
    "Cloudy": (6500, 0),
    "Shade": (7500, 10),
    "Tungsten": (2850, 0),
    "Fluorescent": (3800, 20),
    "Flash": (5500, 0),
}

WB_GAMMA = 2.2  # sRGB-ish display gamma, see kelvin_to_multipliers()

CURVE_PRESETS = {
    # ported verbatim from RethinkRAW's xmp.go ToneCurvePV2012 control points
    "Linear": None,
    "Medium Contrast": [(0, 0), (32, 22), (64, 56), (128, 128), (192, 196), (255, 255)],
    "Strong Contrast": [(0, 0), (32, 16), (64, 50), (128, 128), (192, 202), (255, 255)],
    "Custom": None,
}

# Crop aspect-ratio presets — width/height in *output pixels*. "Original" is resolved to the
# raw image's own W/H at selection time (see DNGForge._on_crop_aspect_changed), not fixed here.
CROP_ASPECT_RATIOS = {
    "Freeform": None,
    "Original": "original",
    "1:1": 1.0,
    "4:3": 4.0 / 3.0,
    "3:2": 3.0 / 2.0,
    "16:9": 16.0 / 9.0,
    "5:4": 5.0 / 4.0,
}

# Exact field list/order requested by the user for the Metadata panel (roadmap item 10).
METADATA_FIELDS = [
    "File Name", "Dimensions", "Cropped", "Date Taken", "Exposure Time",
    "Focal Length", "Focal Length (35mm)", "Brightness Value", "Exposure Bias",
    "ISO", "Flash", "Exposure Program", "Metering Mode", "Make", "Model", "Lens", "Software",
]


# ---------------------------------------------------------------------------
# DNG colorimetric white balance — a port of Adobe's DNG SDK
# dng_temperature/dng_color_spec algorithm, via RethinkRAW's Go port
# (pkg/dng/temp.go, profile.go, lightsrc.go — see project memory
# dngforge-rethinkraw-research). This is what makes a given Temperature/Tint
# actually mean the same thing DNGForge shows as it does in Lightroom/ACR for
# a given DNG: it uses that specific camera's own embedded ColorMatrix1/2 +
# CalibrationIlluminant1/2 (read via exiftool), not a generic/camera-agnostic
# blackbody heuristic. kelvin_to_multipliers() below remains as a fallback
# for when exiftool or these tags aren't available.
# ---------------------------------------------------------------------------

# EXIF/DNG standard LightSource codes -> color temperature (K).
# Port of dng_camera_profile::IlluminantToTemperature.
ILLUMINANT_TEMPERATURES = {
    1: 5500.0, 2: (3800.0 + 4500.0) / 2, 3: 2850.0, 4: 5500.0,           # Daylight/Fluor/Tungsten/Flash
    9: 5500.0, 10: 6500.0, 11: 7500.0,                                   # Fine Weather/Cloudy/Shade
    12: (5700.0 + 7100.0) / 2, 13: (4600.0 + 5500.0) / 2,                # Daylight/Day White Fluorescent
    14: (3800.0 + 4500.0) / 2, 15: (3250.0 + 3800.0) / 2,                # Cool White/White Fluorescent
    16: (2600.0 + 3250.0) / 2,                                          # Warm White Fluorescent
    17: 2850.0, 18: 5500.0, 19: 6500.0,                                  # Standard Light A/B/C
    20: 5500.0, 21: 6500.0, 22: 7500.0, 23: 5000.0,                      # D55/D65/D75/D50
    24: 3200.0,                                                         # ISO Studio Tungsten
}


def illuminant_temperature(code) -> float:
    try:
        return ILLUMINANT_TEMPERATURES.get(int(code), 0.0)
    except (TypeError, ValueError):
        return 0.0


_D50_XY = (0.34567, 0.35850)

# Wyszecki & Stiles blackbody locus table (Adobe DNG SDK dng_temperature.cpp):
# (reciprocal-megakelvin, u, v, slope)
_TEMP_TABLE = [
    (0, 0.18006, 0.26352, -0.24341), (10, 0.18066, 0.26589, -0.25479),
    (20, 0.18133, 0.26846, -0.26876), (30, 0.18208, 0.27119, -0.28539),
    (40, 0.18293, 0.27407, -0.30470), (50, 0.18388, 0.27709, -0.32675),
    (60, 0.18494, 0.28021, -0.35156), (70, 0.18611, 0.28342, -0.37915),
    (80, 0.18740, 0.28668, -0.40955), (90, 0.18880, 0.28997, -0.44278),
    (100, 0.19032, 0.29326, -0.47888), (125, 0.19462, 0.30141, -0.58204),
    (150, 0.19962, 0.30921, -0.70471), (175, 0.20525, 0.31647, -0.84901),
    (200, 0.21142, 0.32312, -1.0182), (225, 0.21807, 0.32909, -1.2168),
    (250, 0.22511, 0.33439, -1.4512), (275, 0.23247, 0.33904, -1.7298),
    (300, 0.24010, 0.34308, -2.0637), (325, 0.24792, 0.34655, -2.4681),
    (350, 0.25591, 0.34951, -2.9641), (375, 0.26400, 0.35200, -3.5814),
    (400, 0.27218, 0.35407, -4.3633), (425, 0.28039, 0.35577, -5.3762),
    (450, 0.28863, 0.35714, -6.7262), (475, 0.29685, 0.35823, -8.5955),
    (500, 0.30505, 0.35907, -11.324), (525, 0.31320, 0.35968, -15.628),
    (550, 0.32129, 0.36011, -23.325), (575, 0.32931, 0.36038, -40.770),
    (600, 0.33724, 0.36051, -116.45),
]
_TINT_SCALE = -3000.0


def _xy_to_temperature_tint(x: float, y: float) -> tuple[float, float]:
    """Port of dng_temperature::Set_xy_coord: CIE xy -> (temperature, tint)."""
    u = 2.0 * x / (1.5 - x + 6.0 * y)
    v = 3.0 * y / (1.5 - x + 6.0 * y)

    last_dt = last_du = last_dv = 0.0
    temperature = tint = 0.0

    for index in range(1, len(_TEMP_TABLE)):
        du = 1.0
        dv = _TEMP_TABLE[index][3]
        length = math.hypot(du, dv)
        du, dv = du / length, dv / length

        uu = u - _TEMP_TABLE[index][1]
        vv = v - _TEMP_TABLE[index][2]
        dt = -uu * dv + vv * du

        if dt <= 0.0 or index == len(_TEMP_TABLE) - 1:
            if dt > 0.0:
                dt = 0.0
            dt = -dt

            f = 0.0 if index == 1 else dt / (last_dt + dt)
            temperature = 1.0e6 / (_TEMP_TABLE[index - 1][0] * f + _TEMP_TABLE[index][0] * (1.0 - f))

            uu = u - (_TEMP_TABLE[index - 1][1] * f + _TEMP_TABLE[index][1] * (1.0 - f))
            vv = v - (_TEMP_TABLE[index - 1][2] * f + _TEMP_TABLE[index][2] * (1.0 - f))
            du = du * (1.0 - f) + last_du * f
            dv = dv * (1.0 - f) + last_dv * f
            length = math.hypot(du, dv)
            du, dv = du / length, dv / length

            tint = (uu * du + vv * dv) * _TINT_SCALE
            break

        last_dt, last_du, last_dv = dt, du, dv

    return temperature, tint


def _temperature_tint_to_xy(temperature: float, tint: float) -> tuple[float, float]:
    """Port of dng_temperature::Get_xy_coord: (temperature, tint) -> CIE xy."""
    r = 1.0e6 / temperature
    offset = tint * (1.0 / _TINT_SCALE)

    for index in range(1, len(_TEMP_TABLE)):
        if r < _TEMP_TABLE[index][0] or index == len(_TEMP_TABLE) - 1:
            f = (_TEMP_TABLE[index][0] - r) / (_TEMP_TABLE[index][0] - _TEMP_TABLE[index - 1][0])

            u = _TEMP_TABLE[index - 1][1] * f + _TEMP_TABLE[index][1] * (1.0 - f)
            v = _TEMP_TABLE[index - 1][2] * f + _TEMP_TABLE[index][2] * (1.0 - f)

            uu1, vv1 = 1.0, _TEMP_TABLE[index - 1][3]
            length = math.hypot(uu1, vv1)
            uu1, vv1 = uu1 / length, vv1 / length

            uu2, vv2 = 1.0, _TEMP_TABLE[index][3]
            length = math.hypot(uu2, vv2)
            uu2, vv2 = uu2 / length, vv2 / length

            uu3 = uu1 * f + uu2 * (1.0 - f)
            vv3 = vv1 * f + vv2 * (1.0 - f)
            length = math.hypot(uu3, vv3)
            uu3, vv3 = uu3 / length, vv3 / length

            u += uu3 * offset
            v += vv3 * offset

            return 1.5 * u / (u - 4.0 * v + 2.0), v / (u - 4.0 * v + 2.0)

    return _D50_XY


def _xyz_to_xy(vec3: np.ndarray) -> tuple[float, float]:
    total = float(vec3[0] + vec3[1] + vec3[2])
    if total <= 0.0:
        return _D50_XY
    return float(vec3[0]) / total, float(vec3[1]) / total


class DNGCameraProfile:
    """A DNG's embedded color calibration (port of dng_color_spec).

    Interpolates between the two calibration illuminants' ColorMatrix by
    temperature, so a given (Temperature, Tint) maps to camera-native RGB
    multipliers the same way Adobe's own tools compute it for this specific
    camera — not a generic/camera-agnostic approximation.
    """

    def __init__(self, color_matrix1, color_matrix2, calib_illum1, calib_illum2,
                 camera_calibration1=None, camera_calibration2=None, analog_balance=None):
        cm1 = np.array(color_matrix1, dtype=np.float64).reshape(3, 3)
        cm2 = np.array(color_matrix2, dtype=np.float64).reshape(3, 3) if color_matrix2 else cm1.copy()

        if camera_calibration1:
            cm1 = np.array(camera_calibration1, dtype=np.float64).reshape(3, 3) @ cm1
        if analog_balance:
            cm1 = np.diag(analog_balance) @ cm1

        t1 = illuminant_temperature(calib_illum1)
        t2 = illuminant_temperature(calib_illum2)

        # matches dng_color_spec exactly: falls back to a single matrix
        # whenever CameraCalibration2 is absent, even if ColorMatrix2 isn't
        if camera_calibration2 is None or color_matrix2 is None or t1 == t2 or t1 <= 0 or t2 <= 0:
            t1 = t2 = 5000.0
            cm2 = cm1.copy()
        else:
            cm2 = np.array(camera_calibration2, dtype=np.float64).reshape(3, 3) @ cm2
            if analog_balance:
                cm2 = np.diag(analog_balance) @ cm2
            if t1 > t2:
                t1, t2 = t2, t1
                cm1, cm2 = cm2, cm1

        self.temperature1, self.temperature2 = t1, t2
        self.matrix1, self.matrix2 = cm1, cm2

    def _find_xyz_to_camera(self, xy):
        temperature, _ = _xy_to_temperature_tint(*xy)
        if temperature <= self.temperature1:
            g = 1.0
        elif temperature >= self.temperature2:
            g = 0.0
        else:
            g = (1.0 / temperature - 1.0 / self.temperature2) / (1.0 / self.temperature1 - 1.0 / self.temperature2)
        if g >= 1.0:
            return self.matrix1
        if g <= 0.0:
            return self.matrix2
        return g * self.matrix1 + (1.0 - g) * self.matrix2

    def neutral_to_temperature_tint(self, neutral) -> tuple[float, float]:
        """Camera-space neutral (e.g. AsShotNeutral, or 1/multipliers) -> (Temperature, Tint)."""
        last = _D50_XY
        for _ in range(30):
            m = self._find_xyz_to_camera(last)
            xyz = np.linalg.solve(m, np.array(neutral, dtype=np.float64))
            nxt = _xyz_to_xy(xyz)
            if abs(nxt[0] - last[0]) + abs(nxt[1] - last[1]) < 1e-7:
                return _xy_to_temperature_tint(*nxt)
            last = nxt
        return _xy_to_temperature_tint(*last)

    def temperature_tint_to_multipliers(self, temperature: float, tint: float) -> list[float]:
        """(Temperature, Tint) -> WB multipliers [R, G, B, G2] for rawpy's user_wb, green-normalized."""
        xy = _temperature_tint_to_xy(temperature, tint)
        xyz = np.array([xy[0] / xy[1], 1.0, (1 - xy[0] - xy[1]) / xy[1]])
        neutral = self._find_xyz_to_camera(xy) @ xyz
        neutral = np.where(np.abs(neutral) < 1e-6, 1e-6, neutral)
        mult = (1.0 / neutral)
        mult = mult / mult[1]
        return [float(mult[0]), float(mult[1]), float(mult[2]), float(mult[1])]


def _locate_exiftool() -> str | None:
    """Find an exiftool executable new enough for PyExifTool (needs >= 12.15).

    PATH order can't be trusted: on this machine an old 11.91 build sits
    ahead of a newer scoop-installed one in PATH, so PyExifTool's default
    "just use PATH" fails outright. Check the scoop shim first (known good
    once installed), then fall back to whatever plain PATH resolves to,
    version-checking either way.
    """
    candidates = []
    scoop_shim = os.path.expanduser(r"~\scoop\shims\exiftool.exe")
    if os.path.isfile(scoop_shim):
        candidates.append(scoop_shim)
    which = shutil.which("exiftool")
    if which:
        candidates.append(which)

    for path in candidates:
        try:
            out = subprocess.run([path, "-ver"], capture_output=True, text=True, timeout=5,
                                  creationflags=_NO_WINDOW_FLAGS)
            major, minor = (int(p) for p in out.stdout.strip().split(".")[:2])
            if (major, minor) >= (12, 15):
                return path
        except Exception:
            continue
    return None


def _locate_dng_converter() -> str | None:
    """Find Adobe DNG Converter.exe at its standard Windows install location(s), or None
    if it isn't installed. Used for the background "authoritative Adobe render" preview
    pipeline (see DngConverterRenderThread) — entirely optional, the app already works
    fully without it (falls back to the fast approximate cv2 preview only).

    Deliberately does **not** run the executable to verify it (e.g. no `-ver`/`-h` probe
    like `_locate_exiftool()` does) — confirmed empirically that Adobe DNG Converter pops
    up its GUI for basically any invocation without a full, valid conversion command line,
    which would be a startling side effect of just checking whether it exists.
    """
    for candidate in (
        r"C:\Program Files\Adobe\Adobe DNG Converter\Adobe DNG Converter.exe",
        r"C:\Program Files (x86)\Adobe\Adobe DNG Converter\Adobe DNG Converter.exe",
    ):
        if os.path.isfile(candidate):
            return candidate
    return None


def load_dng_camera_profile(exiftool_path: str, path: str, tags: "dict | None" = None) -> "DNGCameraProfile | None":
    """Build a DNGCameraProfile from a DNG's embedded tags, or None if
    unavailable (no exiftool, not a DNG, or the file lacks ColorMatrix1) —
    callers should fall back to kelvin_to_multipliers() in that case.

    If `tags` is given (a dict from read_combined_edit_tags()), it's used
    directly instead of spawning this function's own exiftool process —
    see read_combined_edit_tags()'s docstring for why this matters.
    """
    if tags is None:
        if not exiftool_path or not HAVE_PYEXIFTOOL:
            return None
        try:
            with exiftool.ExifToolHelper(executable=exiftool_path) as et:
                tags = et.get_tags([path], tags=[
                    "ColorMatrix1", "ColorMatrix2", "CalibrationIlluminant1", "CalibrationIlluminant2",
                    "CameraCalibration1", "CameraCalibration2", "AnalogBalance",
                ])[0]
        except Exception:
            return None

    def floats(key):
        v = tags.get(f"EXIF:{key}")
        return [float(x) for x in str(v).split()] if v is not None else None

    cm1 = floats("ColorMatrix1")
    if not cm1 or len(cm1) != 9:
        return None

    try:
        return DNGCameraProfile(
            cm1, floats("ColorMatrix2"),
            tags.get("EXIF:CalibrationIlluminant1"), tags.get("EXIF:CalibrationIlluminant2"),
            camera_calibration1=floats("CameraCalibration1"),
            camera_calibration2=floats("CameraCalibration2"),
            analog_balance=floats("AnalogBalance"),
        )
    except Exception:
        return None


def read_metadata(exiftool_path: str, path: str, width: int, height: int) -> dict:
    """Read the camera-settings fields for the Metadata panel (roadmap item 10) — the exact
    field list/order requested by the user, see METADATA_FIELDS.

    Unlike every other exiftool read in this app, this one wants *human-readable* values
    ("1/60", "On, Return detected", "Program AE") for direct display, not machine-parsable
    numbers — so it builds its own ExifToolHelper with print conversion left enabled
    (common_args=["-G"], no "-n"), rather than reusing the app's usual -n convention.

    width/height should be the actual decoded raw dimensions (self.raw.sizes), not read back
    from any single ambiguous "ImageWidth" EXIF tag — this DNG structure can have several
    same-named tags across IFDs at different sizes (see CLAUDE.md's JpgFromRaw writeup).

    CONFIRMED BUG, fixed: "Cropped" originally checked EXIF:DefaultCropSize against
    width/height — but DefaultCropSize is a DNG-spec sensor-level tag (the valid-pixel
    boundary within the raw buffer), essentially unrelated to whether a *user* (in
    Lightroom, or in this app's own Crop & Straighten tool) actually cropped the shot. It
    reported "No" even on DSC_6751.dng, which has a real Lightroom crop
    (XMP-crs:HasCrop=True, boundaries well inside 0..1). Fixed to check XMP-crs:HasCrop
    instead — the same real edit-state tag Crop & Straighten's own read_crop() uses.
    """
    values = {field: "—" for field in METADATA_FIELDS}
    values["File Name"] = os.path.basename(path)
    values["Dimensions"] = f"{width} x {height}"
    values["Cropped"] = "No"

    if not exiftool_path or not HAVE_PYEXIFTOOL:
        return values
    try:
        with exiftool.ExifToolHelper(executable=exiftool_path, common_args=["-G"]) as et:
            tags = et.get_tags([path], tags=[
                "EXIF:DateTimeOriginal", "EXIF:ExposureTime", "EXIF:FocalLength",
                "Composite:FocalLength35efl", "EXIF:BrightnessValue", "EXIF:ExposureCompensation",
                "EXIF:ISO", "EXIF:Flash", "EXIF:ExposureProgram", "EXIF:MeteringMode",
                "EXIF:Make", "EXIF:Model", "EXIF:LensModel", "XMP:Lens", "EXIF:Software",
                "XMP-crs:HasCrop",
            ])[0]
    except Exception:
        return values

    def first(*keys):
        for k in keys:
            v = tags.get(k)
            if v not in (None, ""):
                return str(v)
        return None

    mapping = {
        "Date Taken": first("EXIF:DateTimeOriginal"),
        "Exposure Time": first("EXIF:ExposureTime"),
        "Focal Length": first("EXIF:FocalLength"),
        "Focal Length (35mm)": first("Composite:FocalLength35efl"),
        "Brightness Value": first("EXIF:BrightnessValue"),
        "Exposure Bias": first("EXIF:ExposureCompensation"),
        "ISO": first("EXIF:ISO"),
        "Flash": first("EXIF:Flash"),
        "Exposure Program": first("EXIF:ExposureProgram"),
        "Metering Mode": first("EXIF:MeteringMode"),
        "Make": first("EXIF:Make"),
        "Model": first("EXIF:Model"),
        "Lens": first("EXIF:LensModel", "XMP:Lens"),
        "Software": first("EXIF:Software"),
    }
    for field, value in mapping.items():
        if value is not None:
            values[field] = value

    if tags.get("XMP:HasCrop"):
        values["Cropped"] = "Yes"

    return values


# Bare tag names written by _build_xmp_crs_args(), used again to read them back.
XMP_CRS_TAGS = [
    "CameraProfile", "WhiteBalance", "ColorTemperature", "Tint", "AutoTone",
    "Exposure2012", "Contrast2012", "Highlights2012", "Shadows2012", "Whites2012", "Blacks2012",
    "Texture", "Clarity2012", "Dehaze", "Vibrance", "Saturation",
    "ToneCurveName2012", "ToneCurvePV2012",
    "Sharpness", "LuminanceSmoothing", "ColorNoiseReduction",
]


def read_xmp_crs_state(exiftool_path: str, path: str, tags: "dict | None" = None) -> "dict | None":
    """Read back the -XMP-crs:* edit-state tags written by _build_xmp_crs_args(),
    so reopening a DNG previously saved by this app can repopulate its own
    controls instead of always starting from Default.

    If `tags` is given (a dict from read_combined_edit_tags()), it's used
    directly instead of spawning this function's own exiftool process.

    Returns a dict keyed by bare tag name (e.g. "WhiteBalance"), or None if
    exiftool is unavailable or the file has no crs data at all — a DNG never
    saved by this app. WhiteBalance is unconditionally written by every save
    (see _build_xmp_crs_args), so its absence is the signal used here to
    distinguish "no data" from "a save where every field happened to be 0".
    """
    if tags is None:
        if not exiftool_path or not HAVE_PYEXIFTOOL:
            return None
        try:
            with exiftool.ExifToolHelper(executable=exiftool_path) as et:
                tags = et.get_tags([path], tags=[f"XMP-crs:{t}" for t in XMP_CRS_TAGS])[0]
        except Exception:
            return None

    if tags.get("XMP:WhiteBalance") is None:
        return None
    return {t: tags.get(f"XMP:{t}") for t in XMP_CRS_TAGS}


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def build_radial_xmp_value(filters: list) -> str:
    """Serialize self.radial_filters into exiftool's JSON-like struct-literal
    syntax for -XMP-crs:CircularGradientBasedCorrections — Adobe's *real*
    local-adjustment schema (a list of crs:Correction structs, each holding a
    crs:CorrectionMasks list with one Mask/CircularGradient entry), not a
    simplified stand-in. Field names/types/nesting verified against exiftool's
    own XMP.pm source (%sCorrection / %sCorrectionMask, crs namespace) rather
    than guessed — the one thing that source can't tell us is which *unit*
    real Lightroom expects for Angle (radians vs degrees) or the exact visual
    meaning of Midpoint/Roundness, since this project has no Lightroom to
    check against; see CLAUDE.md "Radial filter persistence" for what's
    verified (structural round-trip through this app, and through exiftool's
    own -struct reader) versus best-effort (visual fidelity in Lightroom).

    Returns "" (meaning: clear/delete the tag) if filters is empty, so
    removing every region in the UI and saving actually removes them from the
    file too, instead of leaving a previous save's regions stranded there.
    """
    if not filters:
        return ""
    corrections = []
    for f in filters:
        left, right = f["cx"] - f["rx"], f["cx"] + f["rx"]
        top, bottom = f["cy"] - f["ry"], f["cy"] + f["ry"]
        mask = (
            "{What=Mask/CircularGradient,MaskValue=1,"
            f"Top={top:.6f},Left={left:.6f},Bottom={bottom:.6f},Right={right:.6f},"
            f"Angle={f['rotation']:.4f},Midpoint=50,Roundness=0,Feather={f['feather']:.2f},"
            f"Flipped={'True' if f['invert'] else 'False'},Version=2,"
            f"SizeX={f['rx']:.6f},SizeY={f['ry']:.6f},X={f['cx']:.6f},Y={f['cy']:.6f}}}"
        )
        corrections.append(
            "{What=Correction,CorrectionAmount=1,CorrectionActive=True,"
            f"LocalExposure2012={f['exposure']:.3f},LocalContrast2012={f['contrast']:.0f},"
            f"LocalSaturation={f['saturation']:.0f},CorrectionMasks=[{mask}]}}"
        )
    return "[" + ",".join(corrections) + "]"


def read_radial_filters(exiftool_path: str, path: str, tags: "dict | None" = None) -> list:
    """Read back -XMP-crs:CircularGradientBasedCorrections into the same list-
    of-dicts shape as self.radial_filters. Requires exiftool's -struct mode —
    the *flattened* (non-struct) tag names for this particular structure
    collapse a multi-region list down to just the last region's values (the
    crs schema explicitly disables List behavior on these flattened tags,
    confirmed in XMP.pm; see build_radial_xmp_value()'s docstring), unlike
    every other tag this app reads, so this one function can't reuse the
    plain et.get_tags() pattern the rest of the app uses.

    If `tags` is given (a dict from read_combined_edit_tags(), already fetched
    with -struct), it's used directly instead of spawning this function's own
    exiftool process.

    Malformed/foreign entries (e.g. a mask type this app doesn't produce,
    like a real Lightroom gradient or brush mask) are skipped individually
    rather than aborting the whole read — one bad region shouldn't cost the
    others. Numeric fields are clamped to the ranges the UI itself enforces
    (see ImageLabel's drag-handle math and the radial sliders' own min/max),
    so a hand-edited or foreign file can't hand the renderer an out-of-range
    value.
    """
    if tags is None:
        if not exiftool_path or not HAVE_PYEXIFTOOL:
            return []
        try:
            with exiftool.ExifToolHelper(executable=exiftool_path) as et:
                tags = et.get_tags(
                    [path], tags=["XMP-crs:CircularGradientBasedCorrections"], params=["-struct"]
                )[0]
        except Exception:
            return []

    corrections = tags.get("XMP:CircularGradientBasedCorrections")
    if not corrections:
        return []

    filters = []
    for corr in corrections:
        masks = corr.get("CorrectionMasks") or []
        mask = masks[0] if masks else None
        if not mask or mask.get("What") != "Mask/CircularGradient":
            continue  # not a radial-filter mask this app understands — skip, don't crash
        try:
            filters.append({
                "cx": _clamp(float(mask["X"]), 0.0, 1.0),
                "cy": _clamp(float(mask["Y"]), 0.0, 1.0),
                "rx": _clamp(float(mask["SizeX"]), 0.02, 1.5),
                "ry": _clamp(float(mask["SizeY"]), 0.02, 1.5),
                "rotation": _clamp(float(mask.get("Angle", 0.0)), -180.0, 180.0),
                "feather": _clamp(float(mask.get("Feather", 50.0)), 0.0, 100.0),
                "invert": bool(mask.get("Flipped", False)),
                "exposure": _clamp(float(corr.get("LocalExposure2012", 0.0)), -3.0, 3.0),
                "contrast": _clamp(float(corr.get("LocalContrast2012", 0.0)), -100.0, 100.0),
                "saturation": _clamp(float(corr.get("LocalSaturation", 0.0)), -100.0, 100.0),
            })
        except (KeyError, TypeError, ValueError):
            continue  # missing/unparseable core field — skip this region only
    return filters


def build_gradient_xmp_value(filters: list) -> str:
    """Serialize self.gradient_filters into exiftool's struct-literal syntax for
    -XMP-crs:GradientBasedCorrections — same real Adobe schema/verification approach
    as build_radial_xmp_value(), but this one's mask geometry (What=Mask/Gradient,
    just ZeroX/ZeroY/FullX/FullY) was confirmed against an actual Lightroom-saved DNG
    the user provided (DSC_6751.dng), not just exiftool's source — real ground truth,
    not a best-effort reading of a schema this project has no way to render-test.

    There is no "Flipped"/invert field in this mask struct (unlike the circular
    filter's) — real Lightroom doesn't need one, since dragging a gradient the other
    direction already swaps which side is affected. To stay schema-faithful rather
    than inventing a nonstandard field, an internally-inverted filter is written with
    its Zero/Full points swapped instead: the rendered mask ends up identical either
    way, so nothing is lost, but a round-trip through this function won't necessarily
    reproduce the *original* zero/full assignment — only the equivalent one. See
    read_gradient_filters()'s docstring for the read-side half of this.
    """
    if not filters:
        return ""
    corrections = []
    for f in filters:
        if f["invert"]:
            zx, zy, fx, fy = f["full_x"], f["full_y"], f["zero_x"], f["zero_y"]
        else:
            zx, zy, fx, fy = f["zero_x"], f["zero_y"], f["full_x"], f["full_y"]
        mask = (
            "{What=Mask/Gradient,MaskValue=1,"
            f"ZeroX={zx:.6f},ZeroY={zy:.6f},FullX={fx:.6f},FullY={fy:.6f}}}"
        )
        corrections.append(
            "{What=Correction,CorrectionAmount=1,CorrectionActive=True,"
            f"LocalExposure2012={f['exposure']:.3f},LocalContrast2012={f['contrast']:.0f},"
            f"LocalSaturation={f['saturation']:.0f},CorrectionMasks=[{mask}]}}"
        )
    return "[" + ",".join(corrections) + "]"


def read_gradient_filters(exiftool_path: str, path: str, tags: "dict | None" = None) -> list:
    """Read back -XMP-crs:GradientBasedCorrections into the same list-of-dicts shape
    as self.gradient_filters. Requires exiftool's -struct mode, same reason as
    read_radial_filters() — the flattened tag names collapse a multi-region list down
    to the last region only.

    If `tags` is given (a dict from read_combined_edit_tags(), already fetched with
    -struct), it's used directly instead of spawning this function's own exiftool
    process.

    Always restores invert=False: this mask has no dedicated invert/Flipped field (see
    build_gradient_xmp_value()'s docstring), so the on-disk Zero/Full points alone
    already encode the correct rendered effect — there's nothing further to invert.
    """
    if tags is None:
        if not exiftool_path or not HAVE_PYEXIFTOOL:
            return []
        try:
            with exiftool.ExifToolHelper(executable=exiftool_path) as et:
                tags = et.get_tags(
                    [path], tags=["XMP-crs:GradientBasedCorrections"], params=["-struct"]
                )[0]
        except Exception:
            return []

    corrections = tags.get("XMP:GradientBasedCorrections")
    if not corrections:
        return []

    filters = []
    for corr in corrections:
        masks = corr.get("CorrectionMasks") or []
        mask = masks[0] if masks else None
        if not mask or mask.get("What") != "Mask/Gradient":
            continue  # not a graduated-filter mask this app understands — skip, don't crash
        try:
            filters.append({
                "zero_x": _clamp(float(mask["ZeroX"]), 0.0, 1.0),
                "zero_y": _clamp(float(mask["ZeroY"]), 0.0, 1.0),
                "full_x": _clamp(float(mask["FullX"]), 0.0, 1.0),
                "full_y": _clamp(float(mask["FullY"]), 0.0, 1.0),
                "invert": False,
                "exposure": _clamp(float(corr.get("LocalExposure2012", 0.0)), -3.0, 3.0),
                "contrast": _clamp(float(corr.get("LocalContrast2012", 0.0)), -100.0, 100.0),
                "saturation": _clamp(float(corr.get("LocalSaturation", 0.0)), -100.0, 100.0),
            })
        except (KeyError, TypeError, ValueError):
            continue  # missing/unparseable core field — skip this region only
    return filters


def read_crop(exiftool_path: str, path: str, tags: "dict | None" = None) -> "dict | None":
    """Read back Crop/Straighten's -XMP-crs:HasCrop/CropTop/CropLeft/CropBottom/
    CropRight/CropAngle — flat tags, no -struct needed (unlike the radial/gradient
    filters' nested Correction/CorrectionMasks shape). Same fraction/degree convention
    self.crop already uses internally, confirmed against a real Lightroom-saved crop
    in DSC_6751.dng, so no unit conversion is needed on either side.

    If `tags` is given (a dict from read_combined_edit_tags()), it's used directly
    instead of spawning this function's own exiftool process.

    Returns None when HasCrop is false/absent — either a file this app never cropped,
    or one where Crop was explicitly turned off (see _build_xmp_crs_args(), which always
    writes HasCrop=False in that case, clearing any earlier crop rather than leaving it).
    """
    if tags is None:
        if not exiftool_path or not HAVE_PYEXIFTOOL:
            return None
        try:
            with exiftool.ExifToolHelper(executable=exiftool_path) as et:
                tags = et.get_tags([path], tags=[
                    "XMP-crs:HasCrop", "XMP-crs:CropTop", "XMP-crs:CropLeft",
                    "XMP-crs:CropBottom", "XMP-crs:CropRight", "XMP-crs:CropAngle",
                ])[0]
        except Exception:
            return None

    if not tags.get("XMP:HasCrop"):
        return None
    try:
        return {
            "angle": _clamp(float(tags.get("XMP:CropAngle", 0.0)), -45.0, 45.0),
            "left": _clamp(float(tags["XMP:CropLeft"]), 0.0, 1.0),
            "top": _clamp(float(tags["XMP:CropTop"]), 0.0, 1.0),
            "right": _clamp(float(tags["XMP:CropRight"]), 0.0, 1.0),
            "bottom": _clamp(float(tags["XMP:CropBottom"]), 0.0, 1.0),
        }
    except (KeyError, TypeError, ValueError):
        return None


def build_spot_xmp_value(spots: list) -> str:
    """Serialize self.spots into exiftool's struct-literal syntax for
    -XMP-crs:RetouchAreas — same struct-literal write mechanism as
    build_radial_xmp_value()/build_gradient_xmp_value(), see the radial filter's own
    persistence writeup for that mechanics.

    **Unverified against a real Lightroom-saved retouch region** (no such sample exists in
    this project, unlike the radial/gradient/crop schemas). Field names (`SpotType`,
    `SourceState`, `Method`, `SourceX`, `OffsetY`, `Opacity`, `Feather`, `Seed`, and the
    `Masks` list reusing the same `CorrectionMask` struct the radial/gradient filters use)
    come straight from exiftool's own `XMP.pm` source, but two specific choices below are
    this app's own best-effort interpretation, not confirmed fact:
    - `SourceX`/`OffsetY` are written as **both deltas** from the destination center
      (`src - dest` on each axis) despite the asymmetric naming (one reads like an
      absolute coordinate) — chosen because a self-consistent "both offsets" reading is
      simpler and more defensible than guessing one field is absolute and the other
      relative with no way to check.
    - The mask's `What` is written as `Mask/CircularGradient` (the same value the radial
      filter uses) since a spot is geometrically just a circle — there's no evidence for
      or against a distinct retouch-specific mask type name.
    Round-trips correctly through this app's own read_spot_removals() either way; the risk
    is purely about whether real Lightroom would interpret a DNGForge-saved spot the same
    way, the same category of risk already flagged for the radial filter's own
    Angle/Midpoint/Roundness.
    """
    if not spots:
        return ""
    areas = []
    for s in spots:
        dx, dy, r = s["dest_x"], s["dest_y"], s["radius"]
        source_x, offset_y = s["src_x"] - dx, s["src_y"] - dy
        mask = (
            "{What=Mask/CircularGradient,MaskValue=1,"
            f"X={dx:.6f},Y={dy:.6f},SizeX={r:.6f},SizeY={r:.6f},"
            "Angle=0,Midpoint=50,Roundness=0,Feather=0,Flipped=False}"
        )
        areas.append(
            "{SpotType=Spot,SourceState=SourceSetExplicitly,"
            f"Method={s.get('method', 'Heal')},SourceX={source_x:.6f},OffsetY={offset_y:.6f},"
            f"Opacity={_clamp(s.get('opacity', 100), 0, 100):.0f},Feather=0,Seed=0,"
            f"Masks=[{mask}]}}"
        )
    return "[" + ",".join(areas) + "]"


def read_spot_removals(exiftool_path: str, path: str, tags: "dict | None" = None) -> list:
    """Read back -XMP-crs:RetouchAreas into the same list-of-dicts shape as self.spots.
    Requires exiftool's -struct mode, same reason as read_radial_filters()/
    read_gradient_filters() — the flattened tag names collapse a multi-region list down to
    the last region only.

    If `tags` is given (a dict from read_combined_edit_tags(), already fetched with
    -struct), it's used directly instead of spawning this function's own exiftool
    process.
    """
    if tags is None:
        if not exiftool_path or not HAVE_PYEXIFTOOL:
            return []
        try:
            with exiftool.ExifToolHelper(executable=exiftool_path) as et:
                tags = et.get_tags([path], tags=["XMP-crs:RetouchAreas"], params=["-struct"])[0]
        except Exception:
            return []

    areas = tags.get("XMP:RetouchAreas")
    if not areas:
        return []

    spots = []
    for area in areas:
        masks = area.get("Masks") or []
        mask = masks[0] if masks else None
        if not mask:
            continue  # not a retouch mask this app understands — skip, don't crash
        try:
            dest_x, dest_y = float(mask["X"]), float(mask["Y"])
            radius = float(mask.get("SizeX", mask.get("SizeY", 0.03)))
            source_x = float(area.get("SourceX", 0.0))
            offset_y = float(area.get("OffsetY", 0.0))
            method = area.get("Method", "Heal")
            if method not in ("Heal", "Clone"):
                method = "Heal"  # foreign/unrecognized method — fall back rather than crash
            spots.append({
                "dest_x": _clamp(dest_x, 0.0, 1.0), "dest_y": _clamp(dest_y, 0.0, 1.0),
                "src_x": _clamp(dest_x + source_x, 0.0, 1.0),
                "src_y": _clamp(dest_y + offset_y, 0.0, 1.0),
                "radius": _clamp(radius, 0.005, 0.4),
                "opacity": _clamp(float(area.get("Opacity", 100)), 0.0, 100.0),
                "method": method,
            })
        except (KeyError, TypeError, ValueError):
            continue  # missing/unparseable core field — skip this region only
    return spots


def read_combined_edit_tags(exiftool_path: str, path: str) -> "dict | None":
    """One combined exiftool call fetching every tag load_dng_camera_profile(),
    read_xmp_crs_state(), read_radial_filters(), read_gradient_filters(),
    read_spot_removals(), and read_crop() each need, so load_dng() can spawn
    exiftool once instead of once per function.

    Profiling found each of those 6 functions previously opened its own
    ExifToolHelper — 7 total exiftool spawns per file load counting
    read_metadata() too — and each spawn costs ~440-470ms almost entirely as
    process-startup overhead (Perl interpreter init + tag table loading),
    regardless of how few tags it actually requests. That added up to
    ~3.15s just for metadata reads on every file open. All 6 of these
    functions already share the same "-G -n" (machine-parsable) convention,
    and -struct is a global exiftool option that doesn't change non-struct
    tag output — so all 6 can safely share one call with -struct always on,
    bringing the total down to ~1s. read_metadata() is the one exception
    (it wants human-readable print-converted values for direct UI display)
    and keeps its own separate call.

    Returns None on any failure (no exiftool, not installed, read error).
    Every caller already knows how to fall back to its own standalone call
    when passed tags=None, so a combined-call failure just means the old
    per-function behavior kicks in instead of a hard failure.
    """
    if not exiftool_path or not HAVE_PYEXIFTOOL:
        return None
    try:
        with exiftool.ExifToolHelper(executable=exiftool_path) as et:
            requested = (
                ["ColorMatrix1", "ColorMatrix2", "CalibrationIlluminant1", "CalibrationIlluminant2",
                 "CameraCalibration1", "CameraCalibration2", "AnalogBalance"]
                + [f"XMP-crs:{t}" for t in XMP_CRS_TAGS]
                + ["XMP-crs:CircularGradientBasedCorrections", "XMP-crs:GradientBasedCorrections",
                   "XMP-crs:RetouchAreas", "XMP-crs:HasCrop", "XMP-crs:CropTop", "XMP-crs:CropLeft",
                   "XMP-crs:CropBottom", "XMP-crs:CropRight", "XMP-crs:CropAngle"]
            )
            return et.get_tags([path], tags=requested, params=["-struct"])[0]
    except Exception:
        return None


def sample_raw_neutral(path: str, x_frac: float, y_frac: float, radius: int = 4) -> list[float]:
    """Sample raw (pre-demosaic) sensor data around a normalized (0..1)
    click point on the image and return a green-normalized 'neutral' triple
    [R/G, 1, B/G] — the same convention as AsShotNeutral, so it can go
    straight into DNGCameraProfile.neutral_to_temperature_tint(). This is
    what "click on a gray/white area to set white balance" actually means:
    treat whatever raw values are under the click as what a neutral patch
    should read, and solve for the Temperature/Tint that would make them
    render neutral. Averages a small block per Bayer channel for noise
    robustness (mirrors RethinkRAW's own 4x4-sample color picker), and
    subtracts the sensor's black level before taking ratios — skipping that
    would bias the ratios, especially for a shadow-area click.

    Takes a **path**, not an already-open rawpy handle, and always opens its
    own short-lived one — found empirically that a failed raw_image_visible
    access (the linear-DNG case below) leaves LibRaw's internal state broken
    enough that a *later, unrelated* postprocess() call on the same handle
    then fails with "Out of order call of libraw function". Must never risk
    that on the app's long-lived self.raw handle used for the live preview.

    Falls back to _sample_linear_neutral() when there's no Bayer mosaic to
    read — e.g. a DNG saved with Adobe DNG Converter's `-lossy` option,
    which demosaics *before* JPEG-compressing (mosaiced data doesn't
    compress well), so rawpy's raw_image raises "RAW image is not flat".
    Found this by hitting it on the project's own Nikon D90 test file.
    """
    with rawpy.imread(path) as raw:
        try:
            img = raw.raw_image_visible
            colors = raw.raw_colors_visible
        except RuntimeError:
            return _sample_linear_neutral(path, x_frac, y_frac, radius)

        black = raw.black_level_per_channel
        h, w = img.shape

        cx, cy = int(x_frac * w), int(y_frac * h)
        x0, x1 = max(0, cx - radius), min(w, cx + radius)
        y0, y1 = max(0, cy - radius), min(h, cy + radius)

        block = img[y0:y1, x0:x1].astype(np.float64)
        block_colors = colors[y0:y1, x0:x1]

        def channel_mean(channel_index):
            mask = block_colors == channel_index
            if not np.any(mask):
                return None
            return float((block[mask] - black[channel_index]).mean())

        r = channel_mean(0)
        g_values = [v for v in (channel_mean(1), channel_mean(3)) if v is not None]
        g = sum(g_values) / len(g_values) if g_values else None
        b = channel_mean(2)

    if r is None or g is None or b is None or r <= 0 or g <= 0 or b <= 0:
        raise ValueError("Punto non valido per il bilanciamento del bianco (troppo scuro o dati raw insufficienti)")

    return [r / g, 1.0, b / g]


def _sample_linear_neutral(path: str, x_frac: float, y_frac: float, radius: int = 4) -> list[float]:
    """Fallback for linear/already-demosaiced DNGs (see sample_raw_neutral).
    Renders at camera-native color with white balance fully disabled
    (user_wb=[1,1,1,1], linear gamma) and samples RGB directly — less
    physically pure than mosaic sampling (demosaic mixes neighboring
    pixels/channels a little) but a standard, workable substitute. Opens
    its own handle for the same state-safety reason as sample_raw_neutral.
    """
    with rawpy.imread(path) as raw:
        rgb = raw.postprocess(
            half_size=True, use_camera_wb=False, user_wb=[1.0, 1.0, 1.0, 1.0],
            no_auto_bright=True, output_bps=16, gamma=(1, 1),
            output_color=rawpy.ColorSpace.raw,
        )
    h, w = rgb.shape[:2]
    cx, cy = int(x_frac * w), int(y_frac * h)
    x0, x1 = max(0, cx - radius), min(w, cx + radius)
    y0, y1 = max(0, cy - radius), min(h, cy + radius)

    block = rgb[y0:y1, x0:x1].astype(np.float64)
    r, g, b = block[..., 0].mean(), block[..., 1].mean(), block[..., 2].mean()
    if r <= 0 or g <= 0 or b <= 0:
        raise ValueError("Punto non valido per il bilanciamento del bianco (troppo scuro o dati insufficienti)")
    return [r / g, 1.0, b / g]


def compute_gray_world_neutral(path: str) -> list[float]:
    """Whole-image gray-world 'neutral' estimate for Auto white balance, in
    the same camera-native, green-normalized [R/G, 1, B/G] convention as
    AsShotNeutral/sample_raw_neutral (so it feeds straight into
    DNGCameraProfile like everything else does).

    CONFIRMED BUG this replaces: the original version averaged the
    already-camera-WB-corrected, gamma-encoded 8-bit preview and used that
    ratio *as if* it were a fresh camera-native multiplier — both wrong
    domain (gamma vs. camera-native linear, the same mistake fixed twice
    already this session for WB_GAMMA and the color picker) *and* wrong
    baseline (a small correction *on top of* an already-corrected image,
    instead of *replacing* it, silently discarding the camera's real
    multiplier). Reported by the user: a normal photo landing on 2563K,
    tint -107 — the temperature/tint solver was reaching for an extreme,
    barely-reachable point trying to satisfy a target neutral the camera's
    real ColorMatrix can't actually produce at any plausible temperature.

    Opens its own short-lived rawpy handle (same LibRaw state-safety reason
    as sample_raw_neutral) and reuses its exact mosaic/linear-DNG dual path.
    """
    with rawpy.imread(path) as raw:
        try:
            img = raw.raw_image_visible
            colors = raw.raw_colors_visible
            black = raw.black_level_per_channel

            def channel_mean(channel_index):
                mask = colors == channel_index
                if not np.any(mask):
                    return None
                return float((img[mask].astype(np.float64) - black[channel_index]).mean())

            r = channel_mean(0)
            g_values = [v for v in (channel_mean(1), channel_mean(3)) if v is not None]
            g = sum(g_values) / len(g_values) if g_values else None
            b = channel_mean(2)
            if r is None or g is None or b is None or r <= 0 or g <= 0 or b <= 0:
                raise RuntimeError("no usable mosaic data")
        except RuntimeError:
            rgb = raw.postprocess(
                half_size=True, use_camera_wb=False, user_wb=[1.0, 1.0, 1.0, 1.0],
                no_auto_bright=True, output_bps=16, gamma=(1, 1),
                output_color=rawpy.ColorSpace.raw,
            ).astype(np.float64)
            r, g, b = rgb[..., 0].mean(), rgb[..., 1].mean(), rgb[..., 2].mean()

    if r <= 0 or g <= 0 or b <= 0:
        raise ValueError("Immagine non valida per il bilanciamento del bianco automatico")
    return [r / g, 1.0, b / g]


def kelvin_to_multipliers(temp_k: float, tint: float) -> list[float]:
    """Approximate WB multipliers [R, G, B, G2] for a given Kelvin/tint pair.

    Fallback used only when a DNGCameraProfile isn't available (no exiftool,
    or the file lacks the needed tags) — see DNGCameraProfile above for the
    real, camera-calibrated method, which is what's used whenever possible.

    Blackbody RGB appearance via the Tanner Helland approximation, then
    inverted (normalized on green) to get correction multipliers. This is a
    visual approximation, not the DNG ColorMatrix-based colorimetric method
    (see project memory: dngforge-rethinkraw-research, "bonus finding" on
    porting RethinkRAW's dng_temperature.go) planned for a later refinement.

    Tanner Helland's RGB is a *display-gamma* appearance (0-255 as it would
    look on screen), but rawpy's `user_wb` multiplies *linear* raw sensor
    data before demosaic/gamma. Applying a gamma-space ratio directly as a
    linear gain under-corrects a lot (a linear gain k only shows up as
    k**(1/gamma) once gamma-encoded for display) — visually the labeled
    Kelvin value ends up needing roughly double itself to look right. Fixed
    by raising the ratios to WB_GAMMA before returning them.
    """
    t = max(1000.0, min(40000.0, temp_k)) / 100.0

    if t <= 66:
        red = 255.0
    else:
        red = 329.698727446 * ((t - 60) ** -0.1332047592)

    if t <= 66:
        green = 99.4708025861 * math.log(t) - 161.1195681661
    else:
        green = 288.1221695283 * ((t - 60) ** -0.0755148492)

    if t >= 66:
        blue = 255.0
    elif t <= 19:
        blue = 0.0
    else:
        blue = 138.5177312231 * math.log(t - 10) - 305.0447927307

    red, green, blue = (max(1.0, min(255.0, c)) for c in (red, green, blue))

    mult_r = green / red
    mult_g = 1.0
    mult_b = green / blue

    # tint: positive = magenta (less green), negative = green
    mult_g *= 1.0 - 0.003 * tint
    mult_g = max(0.2, mult_g)

    mult_r **= WB_GAMMA
    mult_g **= WB_GAMMA
    mult_b **= WB_GAMMA

    return [mult_r, mult_g, mult_b, mult_g]


def multipliers_to_kelvin_tint(mult_r: float, mult_g: float, mult_b: float) -> tuple[float, float]:
    """Inverse of kelvin_to_multipliers(), for display only.

    Used to show the actual Temperature/Tint that produced a given set of WB
    multipliers on the (disabled) sliders when the mode isn't Custom/a named
    preset — e.g. Auto, As Shot, Camera Matching — so the reading always
    reflects what's really being applied, not stale slider positions. The
    forward formula isn't trivially invertible (piecewise blackbody
    approximation), so temperature is found by a coarse numeric search over
    the R/B ratio; tint is closed-form from the green multiplier.
    """
    target_ratio = mult_r / mult_b if mult_b else 1.0
    best_temp, best_diff = 5500.0, float("inf")
    for temp in range(2000, 12001, 50):
        r, _, b, _ = kelvin_to_multipliers(temp, 0)
        diff = abs((r / b if b else 1.0) - target_ratio)
        if diff < best_diff:
            best_diff = diff
            best_temp = float(temp)

    g_gamma_space = mult_g ** (1.0 / WB_GAMMA) if mult_g > 0 else 1.0
    tint = (1.0 - g_gamma_space) / 0.003
    tint = max(-150.0, min(150.0, tint))
    return best_temp, tint


def apply_profile(img_u8: np.ndarray, profile: str) -> np.ndarray:
    """Simplified 'look' presets — NOT real Adobe DCP camera profiles.

    Adobe's actual camera profiles are proprietary color-transform data this
    project doesn't have; these are approximate stand-ins with the same
    names, built from the tone/saturation tools already in this file, so the
    Profile dropdown does something distinguishable rather than nothing.
    """
    if profile in ("Adobe Monochrome", "Adobe Standard B&W"):
        gray = cv2.cvtColor(img_u8, cv2.COLOR_RGB2GRAY)
        return cv2.cvtColor(gray, cv2.COLOR_GRAY2RGB)
    if profile == "Adobe Vivid":
        return apply_tone(apply_saturation(img_u8, 25), 15, 0, 0, 0, 0)
    if profile == "Adobe Landscape":
        return apply_tone(apply_saturation(img_u8, 15), 10, 0, 0, 0, 0)
    if profile == "Adobe Portrait":
        return apply_tone(img_u8, -10, 0, 0, 0, 0)
    if profile == "Adobe Neutral":
        return apply_tone(img_u8, -15, 0, 0, 0, 0)
    return img_u8  # Adobe Color / Adobe Standard / Custom: baseline, no change


_SRGB_OETF_LUT_CACHE: dict[float, np.ndarray] = {}


def apply_exposure_and_gamma(rgb_linear16: np.ndarray, exposure_ev: float) -> np.ndarray:
    """Exposure (2**EV, real stop adjustment on *scene-linear* light) + the
    proper sRGB OETF (piecewise linear+power, not a naive **(1/2.2)),
    combined into one 65536-entry lookup table so the expensive power()
    call runs 65536 times instead of once per pixel — computing it directly
    over a multi-megapixel float64 array took ~3s per render, which made
    every slider feel laggy despite the 60ms debounce; the LUT version is
    effectively free (memory-bound fancy indexing).

    Exposure is folded in here (rather than a separate linear-space step)
    for the same reason — same principle as WB_GAMMA: a physical stop
    adjustment must happen on scene-linear data, not gamma-encoded 8-bit,
    or shadows/highlights shift unevenly relative to a true stop change.
    Contrast/Highlights/Shadows/Whites/Blacks are perceptual operations and
    stay post-gamma in apply_tone() below — moving those to linear would
    make them worse, not better, so this fix is deliberately exposure-only.
    """
    lut = _SRGB_OETF_LUT_CACHE.get(exposure_ev)
    if lut is None:
        x = np.arange(65536, dtype=np.float64) / 65535.0
        if exposure_ev:
            x = np.clip(x * (2.0 ** exposure_ev), 0.0, 1.0)
        srgb = np.where(x <= 0.0031308, x * 12.92, 1.055 * np.power(x, 1.0 / 2.4) - 0.055)
        lut = np.clip(srgb * 255.0 + 0.5, 0, 255).astype(np.uint8)
        if len(_SRGB_OETF_LUT_CACHE) > 8:
            _SRGB_OETF_LUT_CACHE.clear()  # bound memory; sliders settle on a handful of values
        _SRGB_OETF_LUT_CACHE[exposure_ev] = lut
    return lut[rgb_linear16]


def apply_tone(img_u8: np.ndarray, contrast: float, shadows: float, highlights: float,
                whites: float = 0, blacks: float = 0) -> np.ndarray:
    """Simple tone-curve style adjustment, applied to LAB's L (luminance) channel only —
    exposure is handled separately, in linear light, by apply_exposure_and_gamma().

    CONFIRMED BUG, fixed: this used to run on the raw RGB array, adding/scaling the same
    amount on R, G, and B independently. That desaturates: adding a flat offset to all
    three channels shrinks (max-min), which is what HSV saturation is built from — worst
    on already-dark, already-saturated colors (a shadowed red tablecloth fold, say), which
    is exactly what a real photo's Shadows/Blacks lift touches most. Confirmed with a real
    Lightroom-edited file used as ground truth (DSC_6751.dng, whose own embedded preview
    is Lightroom's actual render of these exact slider values): DNGForge's RGB-channel
    version measured mean saturation 22.4 and contrast (pixel std) 18.6 against Lightroom's
    own 79.5 / 53.3 for the identical settings — not subtle. `apply_texture()`/
    `apply_clarity()` already avoid this by working on LAB's L; this now does the same.
    """
    if not (contrast or shadows or highlights or whites or blacks):
        return img_u8

    lab = cv2.cvtColor(img_u8, cv2.COLOR_RGB2LAB)
    l = lab[..., 0].astype(np.float32) / 255.0

    if shadows:
        # strength 0.3, not 0.5 — isolated testing against the same Lightroom ground truth
        # showed Shadows alone (even after the LAB fix above) was still the single biggest
        # contributor to a flattened, hazy look; Highlights' own mask/strength measured
        # much closer to Lightroom already and was left alone. Still an approximation, not
        # a match to Adobe's real curve — see this function's docstring.
        mask = (1.0 - np.clip(l, 0, 1)) ** 2
        l = l + (shadows / 100.0) * 0.3 * mask

    if highlights:
        mask = np.clip(l, 0, 1) ** 2
        l = l + (highlights / 100.0) * 0.5 * mask

    if contrast:
        l = (l - 0.5) * (1.0 + contrast / 100.0) + 0.5

    if blacks:
        # same "measured weaker against Lightroom ground truth" reasoning as Shadows above
        mask = (1.0 - np.clip(l, 0, 1)) ** 4  # sharper falloff than Shadows' **2 —
        l = l + (blacks / 100.0) * 0.3 * mask  # a "clip point" tool, not a broad lift

    if whites:
        mask = np.clip(l, 0, 1) ** 4  # sharper falloff than Highlights' **2
        l = l + (whites / 100.0) * 0.5 * mask

    lab[..., 0] = np.clip(l * 255.0, 0.0, 255.0).astype(np.uint8)
    return cv2.cvtColor(lab, cv2.COLOR_LAB2RGB)


def compute_auto_exposure_linear(rgb_linear16: np.ndarray) -> float:
    """Auto-exposure from scene-linear statistics, aiming the median toward
    the traditional ~18% middle-gray target (in linear light, not the
    ~118/255 gamma-space target a pre-gamma-fix version of this used).
    """
    luminance = rgb_linear16.astype(np.float64).mean(axis=-1) / 65535.0
    median = float(np.median(luminance))
    if median <= 1e-6:
        return 0.0
    return float(np.clip(math.log2(0.18 / median), -2, 2))


def compute_auto_contrast(img_u8: np.ndarray) -> float:
    """Auto-contrast from the gamma-encoded image's tonal spread (unchanged
    from the original compute_auto_tone(), just split from exposure).
    """
    gray = cv2.cvtColor(img_u8, cv2.COLOR_RGB2GRAY).astype(np.float32)
    lo, hi = np.percentile(gray, [1, 99])
    spread = max(hi - lo, 1.0)
    return float(np.clip((80.0 - spread) / 80.0 * 100, -50, 50))


def apply_curve(img_u8: np.ndarray, points) -> np.ndarray:
    if not points:
        return img_u8
    xs, ys = zip(*points)
    lut = np.interp(np.arange(256), xs, ys).astype(np.uint8)
    return lut[img_u8]


def apply_texture(img_u8: np.ndarray, amount: float) -> np.ndarray:
    """Fine-detail local contrast (small-radius unsharp mask on luminance)."""
    if not amount:
        return img_u8
    lab = cv2.cvtColor(img_u8, cv2.COLOR_RGB2LAB)
    l = lab[..., 0].astype(np.float32)
    blurred = cv2.GaussianBlur(l, (0, 0), sigmaX=6)
    l = np.clip(l + (amount / 100.0) * (l - blurred), 0, 255)
    lab[..., 0] = l.astype(np.uint8)
    return cv2.cvtColor(lab, cv2.COLOR_LAB2RGB)


def apply_clarity(img_u8: np.ndarray, amount: float) -> np.ndarray:
    """Midtone local contrast (large-radius unsharp mask on luminance)."""
    if not amount:
        return img_u8
    lab = cv2.cvtColor(img_u8, cv2.COLOR_RGB2LAB)
    l = lab[..., 0].astype(np.float32)
    blurred = cv2.GaussianBlur(l, (0, 0), sigmaX=30)
    l = np.clip(l + (amount / 100.0) * (l - blurred), 0, 255)
    lab[..., 0] = l.astype(np.uint8)
    return cv2.cvtColor(lab, cv2.COLOR_LAB2RGB)


def apply_dehaze(img_u8: np.ndarray, amount: float) -> np.ndarray:
    """Approximate dehaze via CLAHE-blended luminance (not a real haze/depth model)."""
    if not amount:
        return img_u8
    lab = cv2.cvtColor(img_u8, cv2.COLOR_RGB2LAB)
    l = lab[..., 0]
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    l_eq = clahe.apply(l)
    blend = np.clip(amount / 100.0, -1, 1)
    l_new = np.clip(l.astype(np.float32) + blend * (l_eq.astype(np.float32) - l.astype(np.float32)), 0, 255)
    lab[..., 0] = l_new.astype(np.uint8)
    return cv2.cvtColor(lab, cv2.COLOR_LAB2RGB)


def apply_vibrance(img_u8: np.ndarray, vibrance: float) -> np.ndarray:
    """Saturation boost weighted toward already-desaturated pixels (protects skin tones)."""
    if not vibrance:
        return img_u8
    hsv = cv2.cvtColor(img_u8, cv2.COLOR_RGB2HSV).astype(np.float32)
    s = hsv[..., 1]
    weight = 1.0 - (s / 255.0)
    hsv[..., 1] = np.clip(s * (1.0 + (vibrance / 100.0) * weight), 0, 255)
    return cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2RGB)


def apply_saturation(img_u8: np.ndarray, saturation: float) -> np.ndarray:
    """Scale color saturation by +/-100% via the HSV S channel."""
    if not saturation:
        return img_u8
    hsv = cv2.cvtColor(img_u8, cv2.COLOR_RGB2HSV).astype(np.float32)
    hsv[..., 1] = np.clip(hsv[..., 1] * (1.0 + saturation / 100.0), 0, 255)
    return cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2RGB)


def apply_luminance_nr(img_u8: np.ndarray, amount: float) -> np.ndarray:
    """Edge-preserving denoise of the luminance channel."""
    if not amount:
        return img_u8
    lab = cv2.cvtColor(img_u8, cv2.COLOR_RGB2LAB)
    l = lab[..., 0]
    d = int(amount / 12) * 2 + 1
    sigma = 10 + amount * 0.5
    lab[..., 0] = cv2.bilateralFilter(l, d=d, sigmaColor=sigma, sigmaSpace=sigma)
    return cv2.cvtColor(lab, cv2.COLOR_LAB2RGB)


def apply_color_nr(img_u8: np.ndarray, amount: float) -> np.ndarray:
    """Denoise chroma (a/b) channels — color noise is lower frequency than luminance noise."""
    if not amount:
        return img_u8
    lab = cv2.cvtColor(img_u8, cv2.COLOR_RGB2LAB).astype(np.float32)
    sigma = max(0.5, amount / 12.0)
    lab[..., 1] = cv2.GaussianBlur(lab[..., 1], (0, 0), sigmaX=sigma)
    lab[..., 2] = cv2.GaussianBlur(lab[..., 2], (0, 0), sigmaX=sigma)
    return cv2.cvtColor(np.clip(lab, 0, 255).astype(np.uint8), cv2.COLOR_LAB2RGB)


def apply_sharpness(img_u8: np.ndarray, amount: float) -> np.ndarray:
    """Unsharp mask: img + amount * (img - gaussian_blur(img)).

    sigma=1.5/strength=amount/100 (the original values here) are barely
    measurable after the preview gets scaled down to fit the window — a
    ~2px-radius effect on a multi-thousand-pixel-wide render is a fraction
    of a screen pixel once displayed, so the slider looked like it did
    nothing (confirmed by measuring mean pixel difference *after*
    simulating the same downscale Qt applies for display: ~0.8/255, i.e.
    invisible). Bumped both so the effect actually survives that downscale.
    """
    if not amount:
        return img_u8
    blurred = cv2.GaussianBlur(img_u8, (0, 0), sigmaX=2.5)
    strength = amount / 40.0
    return cv2.addWeighted(img_u8, 1.0 + strength, blurred, -strength, 0)


def build_radial_mask(h: int, w: int, cx: float, cy: float, rx: float, ry: float,
                       rotation_deg: float, feather: float) -> np.ndarray:
    """Soft elliptical mask, 1.0 inside fading to 0.0 outside, at image resolution.

    cx/cy/rx/ry are fractions (0..1) of image width/height — same convention
    as the WB picker's click coordinates, so placement is resolution-
    independent (works the same on the half-size preview and the full-res
    save render).
    """
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float64)
    cx_px, cy_px = cx * w, cy * h
    rx_px, ry_px = max(rx * w, 1.0), max(ry * h, 1.0)

    theta = math.radians(rotation_deg)
    dx, dy = xx - cx_px, yy - cy_px
    dx_rot = dx * math.cos(theta) + dy * math.sin(theta)
    dy_rot = -dx * math.sin(theta) + dy * math.cos(theta)
    dist = np.sqrt((dx_rot / rx_px) ** 2 + (dy_rot / ry_px) ** 2)  # 0 at center, 1 at ellipse edge

    inner = max(1.0 - feather / 100.0, 0.0)
    span = max(1.0 - inner, 1e-6)
    return np.clip(1.0 - (dist - inner) / span, 0.0, 1.0)


def apply_radial_filter(img_u8: np.ndarray, filt: "dict | None") -> np.ndarray:
    """Blend a locally-adjusted copy of the image into the original through
    a soft elliptical mask — Lightroom-style Radial Filter, applied last
    (after every global adjustment), same as Lightroom's own local tools.

    Exposure here is a simple gamma-space multiplicative brightness change,
    *not* routed through the linear-light exposure pipeline the global
    Exposure slider uses (see apply_exposure_and_gamma()) — a deliberate
    scope limitation, not an oversight: doing a proper per-region linear
    blend would need a bigger restructuring of _render(). Documented so it
    isn't mistaken for the same fix later.
    """
    if filt is None:
        return img_u8
    h, w = img_u8.shape[:2]
    mask = build_radial_mask(h, w, filt["cx"], filt["cy"], filt["rx"], filt["ry"],
                              filt["rotation"], filt["feather"])
    if filt["invert"]:
        mask = 1.0 - mask
    if mask.max() <= 0:
        return img_u8

    adjusted = img_u8
    if filt["exposure"]:
        adjusted = np.clip(adjusted.astype(np.float32) * (2.0 ** filt["exposure"]), 0, 255).astype(np.uint8)
    if filt["contrast"]:
        adjusted = apply_tone(adjusted, filt["contrast"], 0, 0, 0, 0)
    if filt["saturation"]:
        adjusted = apply_saturation(adjusted, filt["saturation"])

    mask3 = mask[..., None].astype(np.float32)
    blended = img_u8.astype(np.float32) * (1.0 - mask3) + adjusted.astype(np.float32) * mask3
    return np.clip(blended, 0, 255).astype(np.uint8)


def apply_radial_filters(img_u8: np.ndarray, filters: list) -> np.ndarray:
    """Apply a list of radial filters in sequence (multi-region support —
    each one blends independently, in placement order). Thin wrapper so the
    well-tested single-filter apply_radial_filter() stays unchanged.
    """
    for filt in filters:
        img_u8 = apply_radial_filter(img_u8, filt)
    return img_u8


def build_linear_mask(h: int, w: int, zero_x: float, zero_y: float,
                       full_x: float, full_y: float) -> np.ndarray:
    """Soft linear-gradient mask, 0.0 at the (zero_x, zero_y) point's perpendicular
    line fading to 1.0 at (full_x, full_y)'s, same two-point convention Adobe's own
    Mask/Gradient uses (crs:CorrectionMasks' ZeroX/ZeroY/FullX/FullY — confirmed
    against a real Lightroom-saved DNG, see CLAUDE.md "Graduated filter persistence").
    Direction and feather width are both implicit in the distance/angle between the
    two points — no separate angle or feather parameter, unlike the radial filter.
    """
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float64)
    xx, yy = xx / w, yy / h
    dx, dy = full_x - zero_x, full_y - zero_y
    length_sq = dx * dx + dy * dy
    if length_sq < 1e-9:
        return np.zeros((h, w), dtype=np.float64)  # degenerate (zero == full): no effect
    t = ((xx - zero_x) * dx + (yy - zero_y) * dy) / length_sq
    return np.clip(t, 0.0, 1.0)


def apply_gradient_filter(img_u8: np.ndarray, filt: "dict | None") -> np.ndarray:
    """Blend a locally-adjusted copy of the image into the original through a soft
    linear-gradient mask — Lightroom-style Graduated Filter. Same scope/placement
    choices as apply_radial_filter() (gamma-space exposure, applied last): see that
    function's docstring, this one deliberately mirrors it field for field.
    """
    if filt is None:
        return img_u8
    h, w = img_u8.shape[:2]
    mask = build_linear_mask(h, w, filt["zero_x"], filt["zero_y"], filt["full_x"], filt["full_y"])
    if filt["invert"]:
        mask = 1.0 - mask
    if mask.max() <= 0:
        return img_u8

    adjusted = img_u8
    if filt["exposure"]:
        adjusted = np.clip(adjusted.astype(np.float32) * (2.0 ** filt["exposure"]), 0, 255).astype(np.uint8)
    if filt["contrast"]:
        adjusted = apply_tone(adjusted, filt["contrast"], 0, 0, 0, 0)
    if filt["saturation"]:
        adjusted = apply_saturation(adjusted, filt["saturation"])

    mask3 = mask[..., None].astype(np.float32)
    blended = img_u8.astype(np.float32) * (1.0 - mask3) + adjusted.astype(np.float32) * mask3
    return np.clip(blended, 0, 255).astype(np.uint8)


def apply_gradient_filters(img_u8: np.ndarray, filters: list) -> np.ndarray:
    """Apply a list of graduated filters in sequence — same multi-region pattern
    as apply_radial_filters()."""
    for filt in filters:
        img_u8 = apply_gradient_filter(img_u8, filt)
    return img_u8


def apply_spot_removal(img_u8: np.ndarray, spot: "dict | None") -> np.ndarray:
    """Lightroom-style Spot Removal — copies a circular patch from (src_x, src_y) onto
    (dest_x, dest_y), both fractions (0..1) of image width/height, `radius` a fraction of
    image *width* (a true circle in pixel space, unlike the radial filter's independent
    rx/ry — this tool only ever needs one size, matching Lightroom's own circular brush).

    Two methods, matching Lightroom's own pair:
    - **Heal**: `cv2.seamlessClone()` (Poisson blending) — blends the source's *texture*
      into the destination while adapting to the destination's own local color/lighting.
      A real algorithm, not an approximation of one.
    - **Clone**: a plain feathered alpha copy — no color adaptation, for when Heal's
      auto-blending picks the wrong tone (e.g. copying across a hard edge).

    Silently no-ops a spot whose source or destination patch would fall outside the image
    bounds (extending a circle off the edge) rather than crashing — same
    "skip the malformed region, don't abort the whole render" convention already used when
    reading back radial/gradient filters from XMP.
    """
    if not spot:
        return img_u8
    h, w = img_u8.shape[:2]
    r = max(1, int(spot["radius"] * w))
    dx, dy = int(round(spot["dest_x"] * w)), int(round(spot["dest_y"] * h))
    sx, sy = int(round(spot["src_x"] * w)), int(round(spot["src_y"] * h))

    if not (r <= dx <= w - r and r <= dy <= h - r and r <= sx <= w - r and r <= sy <= h - r):
        return img_u8  # circle would extend past the frame — skip rather than clip/crash

    opacity = _clamp(spot.get("opacity", 100) / 100.0, 0.0, 1.0)
    src_patch = img_u8[sy - r:sy + r, sx - r:sx + r]

    if spot.get("method", "Heal") == "Heal":
        mask = np.zeros((2 * r, 2 * r), dtype=np.uint8)
        cv2.circle(mask, (r, r), r, 255, -1)
        try:
            healed = cv2.seamlessClone(src_patch, img_u8, mask, (dx, dy), cv2.NORMAL_CLONE)
        except cv2.error:
            return img_u8  # degenerate patch (e.g. a uniform-color source) — leave untouched
        if opacity >= 1.0:
            return healed
        return cv2.addWeighted(healed, opacity, img_u8, 1.0 - opacity, 0)

    # Clone: soft-edged direct copy, feathered so the patch boundary doesn't hard-cut
    yy, xx = np.mgrid[-r:r, -r:r].astype(np.float32)
    dist = np.sqrt(xx ** 2 + yy ** 2) / r
    feather = np.clip(1.0 - (dist - 0.8) / 0.2, 0.0, 1.0) * opacity  # full strength to 80% radius, fades to the edge
    feather3 = feather[..., None]
    result = img_u8.copy()
    dest_region = result[dy - r:dy + r, dx - r:dx + r]
    result[dy - r:dy + r, dx - r:dx + r] = np.clip(
        dest_region.astype(np.float32) * (1.0 - feather3) + src_patch.astype(np.float32) * feather3, 0, 255
    ).astype(np.uint8)
    return result


def apply_spot_removals(img_u8: np.ndarray, spots: list) -> np.ndarray:
    """Apply a list of spot-removal regions in sequence — same multi-region pattern as
    apply_radial_filters()/apply_gradient_filters()."""
    for spot in spots:
        img_u8 = apply_spot_removal(img_u8, spot)
    return img_u8


def max_inscribed_rect(w: float, h: float, angle_deg: float) -> tuple[float, float]:
    """Largest axis-aligned (w2, h2) rectangle, centered, that fits entirely inside a
    w x h rectangle after it's been rotated by angle_deg around its own center — i.e. the
    biggest crop that avoids the black wedges rotation introduces at the corners. Standard
    closed-form solution (there's no data-dependent search here, just geometry), used to
    clamp the crop rectangle whenever Rotation changes so it never includes rotated-in
    empty corners.
    """
    if w <= 0 or h <= 0:
        return (0.0, 0.0)
    angle = math.radians(abs(angle_deg) % 180.0)
    if angle > math.pi / 2:
        angle = math.pi - angle
    sin_a, cos_a = math.sin(angle), math.cos(angle)
    if sin_a < 1e-9:
        return (w, h)

    if w <= h:
        shorter, longer = w, h
    else:
        shorter, longer = h, w

    if shorter <= 2.0 * sin_a * cos_a * longer + 1e-9 or abs(cos_a - sin_a) < 1e-9:
        half = shorter / 2.0
        if w <= h:
            wr, hr = half / sin_a, half / cos_a
        else:
            wr, hr = half / cos_a, half / sin_a
    else:
        cos_2a = cos_a * cos_a - sin_a * sin_a
        wr = (w * cos_a - h * sin_a) / cos_2a
        hr = (h * cos_a - w * sin_a) / cos_2a

    return (min(wr, w), min(hr, h))


def clamp_crop_to_rotation(crop: dict, w: int, h: int) -> dict:
    """Shrink/re-center crop's (left, top, right, bottom) fractions, if needed, so the
    rectangle they describe still fits inside max_inscribed_rect() for crop['angle'] —
    "auto-adjusts the crop rectangle to stay within the rotated image bounds" per the
    roadmap. Only shrinks when the current rectangle no longer fits; never grows a
    deliberately-smaller crop the user already set.
    """
    max_w, max_h = max_inscribed_rect(w, h, crop.get("angle", 0.0))
    cx, cy = w / 2.0, h / 2.0
    half_w = min((crop["right"] - crop["left"]) * w / 2.0, max_w / 2.0)
    half_h = min((crop["bottom"] - crop["top"]) * h / 2.0, max_h / 2.0)
    rect_cx = _clamp((crop["left"] + crop["right"]) / 2.0 * w, cx - max_w / 2.0 + half_w, cx + max_w / 2.0 - half_w)
    rect_cy = _clamp((crop["top"] + crop["bottom"]) / 2.0 * h, cy - max_h / 2.0 + half_h, cy + max_h / 2.0 - half_h)
    return {
        **crop,
        "left": (rect_cx - half_w) / w, "right": (rect_cx + half_w) / w,
        "top": (rect_cy - half_h) / h, "bottom": (rect_cy + half_h) / h,
    }


def apply_crop_straighten(img_u8: np.ndarray, crop: "dict | None") -> np.ndarray:
    """Rotate (Straighten) then crop, applied last in _render() — after every global
    adjustment and every local filter, matching where Lightroom's own Crop/Straighten
    tool sits in the pipeline. Only ever touches the *rendered* image passed in here,
    never self.raw or the DNG's own pixel data — the raw decode this function's caller
    receives is always the untouched full frame; cropping/rotating happens purely on the
    8-bit render, same non-destructive convention as every other adjustment in this app.
    """
    if not crop:
        return img_u8
    h, w = img_u8.shape[:2]
    angle = crop.get("angle", 0.0)
    if angle:
        matrix = cv2.getRotationMatrix2D((w / 2.0, h / 2.0), angle, 1.0)
        img_u8 = cv2.warpAffine(img_u8, matrix, (w, h), flags=cv2.INTER_LINEAR,
                                 borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0))
    x0, y0 = int(round(crop["left"] * w)), int(round(crop["top"] * h))
    x1, y1 = int(round(crop["right"] * w)), int(round(crop["bottom"] * h))
    x1, y1 = max(x1, x0 + 1), max(y1, y0 + 1)
    return img_u8[y0:y1, x0:x1]


def numpy_to_pixmap(rgb: np.ndarray) -> QPixmap:
    rgb = np.ascontiguousarray(rgb)
    h, w, _ = rgb.shape
    qimg = QImage(rgb.data, w, h, 3 * w, QImage.Format_RGB888)
    # QImage doesn't own the numpy buffer, so copy before it goes out of scope
    return QPixmap.fromImage(qimg.copy())


class Slider(QWidget):
    """Labeled slider with a live numeric readout, scaled from int ticks to a float range."""

    def __init__(self, label, minimum, maximum, default, step=1, decimals=0, on_change=None):
        super().__init__()
        self.decimals = decimals
        self.scale = 10 ** decimals

        self.slider = QSlider(Qt.Horizontal)
        self.slider.setMinimum(int(minimum * self.scale))
        self.slider.setMaximum(int(maximum * self.scale))
        self.slider.setSingleStep(int(step * self.scale))
        self.slider.setValue(int(default * self.scale))

        self.value_label = QLabel(self._fmt(default))
        self.value_label.setFixedWidth(56)
        self.value_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(QLabel(label), 1)
        row.addWidget(self.slider, 4)
        row.addWidget(self.value_label)

        self._on_change = on_change
        self.slider.valueChanged.connect(self._changed)

    def _fmt(self, v):
        return f"{v:.{self.decimals}f}"

    def _changed(self, raw_value):
        v = raw_value / self.scale
        self.value_label.setText(self._fmt(v))
        if self._on_change:
            self._on_change()

    def value(self):
        return self.slider.value() / self.scale

    def set_value(self, v):
        self.slider.blockSignals(True)
        self.slider.setValue(int(v * self.scale))
        self.value_label.setText(self._fmt(v))
        self.slider.blockSignals(False)


class ImageLabel(QLabel):
    """A QLabel that reports where within its (centered, aspect-fit) pixmap a click landed,
    as fractions (0..1) of the pixmap's own width/height — used by the white-balance picker
    and the radial filter placement. Also draws every radial filter's ellipse as an overlay
    (self.overlay_ellipses, purely visual — never part of the actual image data): the selected
    one gets full drag handles (move/resize/rotate, Lightroom-style), the rest a plain thin
    outline so you can see where they are and click one to select it.
    """

    clicked = Signal(float, float)
    ellipseChanged = Signal(dict)
    ellipseSelected = Signal(int)
    lineChanged = Signal(dict)
    lineSelected = Signal(int)
    cropChanged = Signal(dict)
    spotChanged = Signal(dict)
    spotSelected = Signal(int)
    zoomRequested = Signal(int)  # +1 (in) or -1 (out), emitted on Ctrl+wheel
    zoomToRectRequested = Signal(float, float, float, float)  # left, top, right, bottom fractions

    HANDLE_HIT_RADIUS = 10
    ROTATE_HANDLE_OFFSET = 25  # screen px beyond the ellipse's top edge
    LINE_BOUNDARY_HALF_LEN = 60  # screen px, half-length of the zero/full boundary ticks
    LINE_BODY_HIT_DIST = 8  # screen px, how close a click needs to be to the gradient's spine

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.overlay_ellipses = []  # list of dict(cx, cy, rx, ry, rotation), one per radial filter
        self.selected_index = -1
        self._drag_mode = None  # None | 'center' | 'rx' | 'ry' | 'rotate'
        self.overlay_lines = []  # list of dict(zero_x, zero_y, full_x, full_y), one per graduated filter
        self.selected_line_index = -1
        self._line_drag_mode = None  # None | 'zero' | 'full' | 'move'
        self._line_move_start = None  # (mouse_px, mouse_py, zero_x, zero_y, full_x, full_y) while dragging 'move'
        self.overlay_crop = None  # dict(left, top, right, bottom) in the *already-rotated* preview's
        # own fractions, or None — no rotation handling needed here since, while the crop guide is
        # shown, _render() itself already renders the straightened full frame (see
        # DNGForge._effective_crop_for_render()), so this overlay is always a plain axis-aligned
        # rectangle on top of an already-correctly-rotated pixmap.
        self._crop_drag_mode = None  # None | 'tl' | 'tr' | 'bl' | 'br' | 'move'
        self._crop_move_start = None  # (mouse_px, mouse_py, left, top, right, bottom) while dragging 'move'
        self.crop_aspect = None  # target (right-left)/(bottom-top) fraction ratio, or None (freeform)
        self.overlay_spots = []  # list of dict(dest_x, dest_y, src_x, src_y, radius), one per spot-removal region
        self.selected_spot_index = -1
        self._spot_drag_mode = None  # None | 'dest' | 'src' | 'resize'
        self.zoom_select_armed = False  # True while "marquee zoom" mode is active
        self._zoom_select_origin = None  # QPoint, set on mousePress while dragging a marquee
        self._zoom_rubber_band = None  # QRubberBand, created lazily on first use

    def _screen_geom_for(self, index):
        """(cx, cy, rx, ry, rotation, x_off, y_off, pixmap_w, pixmap_h) in screen pixels, or None."""
        pixmap = self.pixmap()
        if not pixmap or pixmap.isNull() or not (0 <= index < len(self.overlay_ellipses)):
            return None
        e = self.overlay_ellipses[index]
        x_off = max(0.0, (self.width() - pixmap.width()) / 2.0)
        y_off = max(0.0, (self.height() - pixmap.height()) / 2.0)
        return (
            x_off + e["cx"] * pixmap.width(), y_off + e["cy"] * pixmap.height(),
            e["rx"] * pixmap.width(), e["ry"] * pixmap.height(), e["rotation"],
            x_off, y_off, pixmap.width(), pixmap.height(),
        )

    def _ellipse_screen_geom(self):
        return self._screen_geom_for(self.selected_index)

    @staticmethod
    def _local_to_screen(cx, cy, rotation_deg, lx, ly):
        theta = math.radians(rotation_deg)
        sx = lx * math.cos(theta) - ly * math.sin(theta)
        sy = lx * math.sin(theta) + ly * math.cos(theta)
        return cx + sx, cy + sy

    def _handle_positions(self, geom):
        cx, cy, rx, ry, rotation = geom[:5]
        return {
            "center": (cx, cy),
            "rx": self._local_to_screen(cx, cy, rotation, rx, 0),
            "ry": self._local_to_screen(cx, cy, rotation, 0, ry),
            "rotate": self._local_to_screen(cx, cy, rotation, 0, -ry - self.ROTATE_HANDLE_OFFSET),
        }

    def _hit_test_handles(self, pos):
        geom = self._ellipse_screen_geom()
        if geom is None:
            return None
        for name, (hx, hy) in self._handle_positions(geom).items():
            if (pos.x() - hx) ** 2 + (pos.y() - hy) ** 2 <= self.HANDLE_HIT_RADIUS ** 2:
                return name
        return None

    def _hit_test_ellipse_body(self, pos, index):
        geom = self._screen_geom_for(index)
        if geom is None:
            return False
        cx, cy, rx, ry, rotation = geom[:5]
        theta = math.radians(rotation)
        dx, dy = pos.x() - cx, pos.y() - cy
        local_x = dx * math.cos(theta) + dy * math.sin(theta)
        local_y = -dx * math.sin(theta) + dy * math.cos(theta)
        dist = math.hypot(local_x / max(rx, 1.0), local_y / max(ry, 1.0))
        return dist <= 1.0

    def _line_screen_geom_for(self, index):
        """(zero_x, zero_y, full_x, full_y, x_off, y_off, pixmap_w, pixmap_h) in screen pixels, or None."""
        pixmap = self.pixmap()
        if not pixmap or pixmap.isNull() or not (0 <= index < len(self.overlay_lines)):
            return None
        ln = self.overlay_lines[index]
        x_off = max(0.0, (self.width() - pixmap.width()) / 2.0)
        y_off = max(0.0, (self.height() - pixmap.height()) / 2.0)
        pw, ph = pixmap.width(), pixmap.height()
        return (
            x_off + ln["zero_x"] * pw, y_off + ln["zero_y"] * ph,
            x_off + ln["full_x"] * pw, y_off + ln["full_y"] * ph,
            x_off, y_off, pw, ph,
        )

    def _selected_line_screen_geom(self):
        return self._line_screen_geom_for(self.selected_line_index)

    def _line_handle_positions(self, geom):
        zx, zy, fx, fy = geom[:4]
        return {"zero": (zx, zy), "full": (fx, fy)}

    def _hit_test_line_handles(self, pos):
        geom = self._selected_line_screen_geom()
        if geom is None:
            return None
        for name, (hx, hy) in self._line_handle_positions(geom).items():
            if (pos.x() - hx) ** 2 + (pos.y() - hy) ** 2 <= self.HANDLE_HIT_RADIUS ** 2:
                return name
        return None

    def _hit_test_line_body(self, pos, index):
        """Distance from pos to the segment between the zero and full handles —
        clicking anywhere near the gradient's spine (not just the two endpoints)
        selects it, same "click the body to select" convenience as the ellipse."""
        geom = self._line_screen_geom_for(index)
        if geom is None:
            return False
        zx, zy, fx, fy = geom[:4]
        dx, dy = fx - zx, fy - zy
        length_sq = dx * dx + dy * dy
        if length_sq < 1e-6:
            return math.hypot(pos.x() - zx, pos.y() - zy) <= self.LINE_BODY_HIT_DIST
        t = max(0.0, min(1.0, ((pos.x() - zx) * dx + (pos.y() - zy) * dy) / length_sq))
        px, py = zx + t * dx, zy + t * dy
        return math.hypot(pos.x() - px, pos.y() - py) <= self.LINE_BODY_HIT_DIST

    def _crop_screen_geom(self):
        """(x0, y0, x1, y1, x_off, y_off, pixmap_w, pixmap_h) in screen pixels, or None."""
        pixmap = self.pixmap()
        if not pixmap or pixmap.isNull() or self.overlay_crop is None:
            return None
        c = self.overlay_crop
        x_off = max(0.0, (self.width() - pixmap.width()) / 2.0)
        y_off = max(0.0, (self.height() - pixmap.height()) / 2.0)
        pw, ph = pixmap.width(), pixmap.height()
        return (x_off + c["left"] * pw, y_off + c["top"] * ph,
                x_off + c["right"] * pw, y_off + c["bottom"] * ph, x_off, y_off, pw, ph)

    def _crop_handle_positions(self, geom):
        x0, y0, x1, y1 = geom[:4]
        return {"tl": (x0, y0), "tr": (x1, y0), "bl": (x0, y1), "br": (x1, y1)}

    def _hit_test_crop_handles(self, pos):
        geom = self._crop_screen_geom()
        if geom is None:
            return None
        for name, (hx, hy) in self._crop_handle_positions(geom).items():
            if (pos.x() - hx) ** 2 + (pos.y() - hy) ** 2 <= self.HANDLE_HIT_RADIUS ** 2:
                return name
        return None

    def _hit_test_crop_body(self, pos):
        geom = self._crop_screen_geom()
        if geom is None:
            return False
        x0, y0, x1, y1 = geom[:4]
        return x0 <= pos.x() <= x1 and y0 <= pos.y() <= y1

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            return super().mousePressEvent(event)
        pos = event.position()

        if self.zoom_select_armed:
            self._zoom_select_origin = QPoint(int(pos.x()), int(pos.y()))
            if self._zoom_rubber_band is None:
                self._zoom_rubber_band = QRubberBand(QRubberBand.Rectangle, self)
            self._zoom_rubber_band.setGeometry(QRect(self._zoom_select_origin, QSize()))
            self._zoom_rubber_band.show()
            return

        if self.overlay_crop is not None:
            hit = self._hit_test_crop_handles(pos)
            if hit is not None:
                self._crop_drag_mode = hit
                return
            if self._hit_test_crop_body(pos):
                self._crop_drag_mode = "move"
                c = self.overlay_crop
                self._crop_move_start = (pos.x(), pos.y(), c["left"], c["top"], c["right"], c["bottom"])
                return

        if self.selected_index >= 0:
            hit = self._hit_test_handles(pos)
            if hit is not None:
                self._drag_mode = hit
                return

        if self.selected_line_index >= 0:
            hit = self._hit_test_line_handles(pos)
            if hit is not None:
                self._line_drag_mode = hit
                return
            if self._hit_test_line_body(pos, self.selected_line_index):
                geom = self._selected_line_screen_geom()
                self._line_drag_mode = "move"
                ln = self.overlay_lines[self.selected_line_index]
                self._line_move_start = (pos.x(), pos.y(), ln["zero_x"], ln["zero_y"], ln["full_x"], ln["full_y"])
                return

        if self.selected_spot_index >= 0:
            hit = self._hit_test_spot_handles(pos)  # resize grip, small precise target
            if hit is not None:
                self._spot_drag_mode = hit
                return
            geom = self._selected_spot_screen_geom()
            if geom is not None:
                sdx, sdy, ssx, ssy, sr = geom[:5]
                if (pos.x() - sdx) ** 2 + (pos.y() - sdy) ** 2 <= sr ** 2:
                    self._spot_drag_mode = "dest"
                    return
                if (pos.x() - ssx) ** 2 + (pos.y() - ssy) ** 2 <= sr ** 2:
                    self._spot_drag_mode = "src"
                    return

        for i in range(len(self.overlay_ellipses) - 1, -1, -1):  # most-recently-placed first
            if self._hit_test_ellipse_body(pos, i):
                self.ellipseSelected.emit(i)
                return

        for i in range(len(self.overlay_lines) - 1, -1, -1):
            if self._hit_test_line_body(pos, i):
                self.lineSelected.emit(i)
                return

        for i in range(len(self.overlay_spots) - 1, -1, -1):
            if self._hit_test_spot_body(pos, i):
                self.spotSelected.emit(i)
                return

        pixmap = self.pixmap()
        if pixmap and not pixmap.isNull():
            x_off = max(0.0, (self.width() - pixmap.width()) / 2.0)
            y_off = max(0.0, (self.height() - pixmap.height()) / 2.0)
            px, py = pos.x() - x_off, pos.y() - y_off
            if 0 <= px < pixmap.width() and 0 <= py < pixmap.height():
                self.clicked.emit(px / pixmap.width(), py / pixmap.height())
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.zoom_select_armed and self._zoom_select_origin is not None:
            pos = event.position()
            current = QPoint(int(pos.x()), int(pos.y()))
            self._zoom_rubber_band.setGeometry(QRect(self._zoom_select_origin, current).normalized())
            return
        if self._drag_mode is not None and self.selected_index >= 0:
            self._move_ellipse(event)
            return
        if self._line_drag_mode is not None and self.selected_line_index >= 0:
            self._move_line(event)
            return
        if self._crop_drag_mode is not None and self.overlay_crop is not None:
            self._move_crop(event)
            return
        if self._spot_drag_mode is not None and self.selected_spot_index >= 0:
            self._move_spot(event)
            return
        super().mouseMoveEvent(event)

    def _move_ellipse(self, event):
        geom = self._ellipse_screen_geom()
        if geom is None:
            return
        cx, cy, rx, ry, rotation, x_off, y_off, pw, ph = geom
        pos = event.position()
        e = self.overlay_ellipses[self.selected_index]

        if self._drag_mode == "center":
            e["cx"] = max(0.0, min(1.0, (pos.x() - x_off) / pw))
            e["cy"] = max(0.0, min(1.0, (pos.y() - y_off) / ph))
        else:
            dx, dy = pos.x() - cx, pos.y() - cy
            theta = math.radians(rotation)
            if self._drag_mode == "rx":
                local_x = dx * math.cos(theta) + dy * math.sin(theta)
                e["rx"] = max(0.02, min(1.5, abs(local_x) / pw))
            elif self._drag_mode == "ry":
                local_y = -dx * math.sin(theta) + dy * math.cos(theta)
                e["ry"] = max(0.02, min(1.5, abs(local_y) / ph))
            elif self._drag_mode == "rotate":
                e["rotation"] = math.degrees(math.atan2(dx, -dy))

        self.update()
        self.ellipseChanged.emit(dict(e))

    def _move_line(self, event):
        geom = self._selected_line_screen_geom()
        if geom is None:
            return
        _, _, _, _, x_off, y_off, pw, ph = geom
        pos = event.position()
        ln = self.overlay_lines[self.selected_line_index]

        if self._line_drag_mode == "zero":
            ln["zero_x"] = max(0.0, min(1.0, (pos.x() - x_off) / pw))
            ln["zero_y"] = max(0.0, min(1.0, (pos.y() - y_off) / ph))
        elif self._line_drag_mode == "full":
            ln["full_x"] = max(0.0, min(1.0, (pos.x() - x_off) / pw))
            ln["full_y"] = max(0.0, min(1.0, (pos.y() - y_off) / ph))
        elif self._line_drag_mode == "move" and self._line_move_start is not None:
            start_px, start_py, zx0, zy0, fx0, fy0 = self._line_move_start
            dx_frac = (pos.x() - start_px) / pw
            dy_frac = (pos.y() - start_py) / ph
            ln["zero_x"] = max(0.0, min(1.0, zx0 + dx_frac))
            ln["zero_y"] = max(0.0, min(1.0, zy0 + dy_frac))
            ln["full_x"] = max(0.0, min(1.0, fx0 + dx_frac))
            ln["full_y"] = max(0.0, min(1.0, fy0 + dy_frac))

        self.update()
        self.lineChanged.emit(dict(ln))

    CROP_MIN_SIZE = 0.05  # fraction of image width/height, smallest allowed crop rectangle

    def _move_crop(self, event):
        geom = self._crop_screen_geom()
        if geom is None:
            return
        _, _, _, _, x_off, y_off, pw, ph = geom
        pos = event.position()
        c = self.overlay_crop
        fx = max(0.0, min(1.0, (pos.x() - x_off) / pw))
        fy = max(0.0, min(1.0, (pos.y() - y_off) / ph))

        if self._crop_drag_mode == "move" and self._crop_move_start is not None:
            start_px, start_py, l0, t0, r0, b0 = self._crop_move_start
            dx_frac = (pos.x() - start_px) / pw
            dy_frac = (pos.y() - start_py) / ph
            w, h = r0 - l0, b0 - t0
            new_left = max(0.0, min(1.0 - w, l0 + dx_frac))
            new_top = max(0.0, min(1.0 - h, t0 + dy_frac))
            c["left"], c["right"] = new_left, new_left + w
            c["top"], c["bottom"] = new_top, new_top + h
        elif self._crop_drag_mode in ("tl", "tr", "bl", "br"):
            # Unified corner-drag: resize around the *opposite* corner (the anchor).
            # With crop_aspect set, clamp the free (fx, fy) target to the ratio by taking
            # whichever implied extent (from the horizontal or vertical mouse movement) is
            # larger, so the rectangle always at least reaches the cursor along one axis.
            anchor_x = c["right"] if self._crop_drag_mode in ("tl", "bl") else c["left"]
            anchor_y = c["bottom"] if self._crop_drag_mode in ("tl", "tr") else c["top"]
            new_x, new_y = fx, fy
            if self.crop_aspect:
                dw, dh = abs(fx - anchor_x), abs(fy - anchor_y)
                if dw / self.crop_aspect >= dh:
                    dh = dw / self.crop_aspect
                else:
                    dw = dh * self.crop_aspect
                new_x = anchor_x + (dw if fx >= anchor_x else -dw)
                new_y = anchor_y + (dh if fy >= anchor_y else -dh)
            left, right = min(anchor_x, new_x), max(anchor_x, new_x)
            top, bottom = min(anchor_y, new_y), max(anchor_y, new_y)
            if right - left >= self.CROP_MIN_SIZE and bottom - top >= self.CROP_MIN_SIZE:
                c["left"], c["right"], c["top"], c["bottom"] = left, right, top, bottom

        self.update()
        self.cropChanged.emit(dict(c))

    def _spot_screen_geom_for(self, index):
        """(dest_x, dest_y, src_x, src_y, radius, x_off, y_off, pixmap_w, pixmap_h) in screen px,
        or None. `radius` is a fraction of image *width* (a true circle), converted here using
        the pixmap's own width so it stays circular regardless of the image's aspect ratio."""
        pixmap = self.pixmap()
        if not pixmap or pixmap.isNull() or not (0 <= index < len(self.overlay_spots)):
            return None
        s = self.overlay_spots[index]
        x_off = max(0.0, (self.width() - pixmap.width()) / 2.0)
        y_off = max(0.0, (self.height() - pixmap.height()) / 2.0)
        pw, ph = pixmap.width(), pixmap.height()
        return (
            x_off + s["dest_x"] * pw, y_off + s["dest_y"] * ph,
            x_off + s["src_x"] * pw, y_off + s["src_y"] * ph,
            s["radius"] * pw, x_off, y_off, pw, ph,
        )

    def _selected_spot_screen_geom(self):
        return self._spot_screen_geom_for(self.selected_spot_index)

    def _spot_handle_positions(self, geom):
        dx, dy, sx, sy, r = geom[:5]
        return {"resize": (dx + r, dy)}

    def _hit_test_spot_handles(self, pos):
        """Only the resize grip is a small precise target — dest/src are dragged by
        clicking anywhere in their (potentially large) circle body instead, checked
        separately in mousePressEvent, since requiring a pixel-precise click on a tiny
        center dot to move a large circle would be poor UX."""
        geom = self._selected_spot_screen_geom()
        if geom is None:
            return None
        for name, (hx, hy) in self._spot_handle_positions(geom).items():
            if (pos.x() - hx) ** 2 + (pos.y() - hy) ** 2 <= self.HANDLE_HIT_RADIUS ** 2:
                return name
        return None

    def _hit_test_spot_body(self, pos, index):
        """Clicking inside *either* circle (destination or source) selects that spot."""
        geom = self._spot_screen_geom_for(index)
        if geom is None:
            return False
        dx, dy, sx, sy, r = geom[:5]
        return (pos.x() - dx) ** 2 + (pos.y() - dy) ** 2 <= r ** 2 or \
            (pos.x() - sx) ** 2 + (pos.y() - sy) ** 2 <= r ** 2

    def _move_spot(self, event):
        geom = self._selected_spot_screen_geom()
        if geom is None:
            return
        dx, dy, sx, sy, r, x_off, y_off, pw, ph = geom
        pos = event.position()
        fx = max(0.0, min(1.0, (pos.x() - x_off) / pw))
        fy = max(0.0, min(1.0, (pos.y() - y_off) / ph))
        s = self.overlay_spots[self.selected_spot_index]

        if self._spot_drag_mode == "dest":
            s["dest_x"], s["dest_y"] = fx, fy
        elif self._spot_drag_mode == "src":
            s["src_x"], s["src_y"] = fx, fy
        elif self._spot_drag_mode == "resize":
            new_r_px = math.hypot(pos.x() - dx, pos.y() - dy)
            s["radius"] = max(0.005, min(0.4, new_r_px / pw))

        self.update()
        self.spotChanged.emit(dict(s))

    def mouseReleaseEvent(self, event):
        if self.zoom_select_armed and self._zoom_select_origin is not None:
            pos = event.position()
            end = QPoint(int(pos.x()), int(pos.y()))
            rect = QRect(self._zoom_select_origin, end).normalized()
            self._zoom_select_origin = None
            self.zoom_select_armed = False  # single-shot, same as the other picker tools
            if self._zoom_rubber_band is not None:
                self._zoom_rubber_band.hide()

            pixmap = self.pixmap()
            if pixmap and not pixmap.isNull() and rect.width() > 4 and rect.height() > 4:
                x_off = max(0.0, (self.width() - pixmap.width()) / 2.0)
                y_off = max(0.0, (self.height() - pixmap.height()) / 2.0)
                pw, ph = pixmap.width(), pixmap.height()
                left = max(0.0, min(1.0, (rect.left() - x_off) / pw))
                top = max(0.0, min(1.0, (rect.top() - y_off) / ph))
                right = max(0.0, min(1.0, (rect.right() - x_off) / pw))
                bottom = max(0.0, min(1.0, (rect.bottom() - y_off) / ph))
                if right > left and bottom > top:
                    self.zoomToRectRequested.emit(left, top, right, bottom)
        elif self._drag_mode is not None:
            self._drag_mode = None
        elif self._line_drag_mode is not None:
            self._line_drag_mode = None
            self._line_move_start = None
        elif self._crop_drag_mode is not None:
            self._crop_drag_mode = None
            self._crop_move_start = None
        elif self._spot_drag_mode is not None:
            self._spot_drag_mode = None
        else:
            super().mouseReleaseEvent(event)

    def wheelEvent(self, event):
        """Ctrl+wheel zooms (emits zoomRequested, handled by DNGForge); a plain wheel
        scroll is left to the surrounding QScrollArea's own default handling."""
        if event.modifiers() & Qt.ControlModifier:
            self.zoomRequested.emit(1 if event.angleDelta().y() > 0 else -1)
            event.accept()
            return
        super().wheelEvent(event)

    def paintEvent(self, event):
        super().paintEvent(event)
        if (not self.overlay_ellipses and not self.overlay_lines and self.overlay_crop is None
                and not self.overlay_spots):
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        if self.overlay_crop is not None:
            self._paint_crop(painter)

        for i in range(len(self.overlay_ellipses)):
            if i == self.selected_index:
                continue
            geom = self._screen_geom_for(i)
            if geom is None:
                continue
            cx, cy, rx, ry, rotation = geom[:5]
            painter.setPen(QPen(QColor(255, 220, 0, 140), 1, Qt.DashLine))
            painter.setBrush(Qt.NoBrush)
            painter.translate(cx, cy)
            painter.rotate(rotation)
            painter.drawEllipse(QPointF(0, 0), rx, ry)
            painter.resetTransform()

        geom = self._ellipse_screen_geom()
        if geom is not None:
            cx, cy, rx, ry, rotation = geom[:5]

            painter.setPen(QPen(QColor(255, 220, 0), 2, Qt.DashLine))
            painter.setBrush(Qt.NoBrush)
            painter.translate(cx, cy)
            painter.rotate(rotation)
            painter.drawEllipse(QPointF(0, 0), rx, ry)
            painter.resetTransform()

            top_x, top_y = self._local_to_screen(cx, cy, rotation, 0, -ry)
            rot_x, rot_y = self._local_to_screen(cx, cy, rotation, 0, -ry - self.ROTATE_HANDLE_OFFSET)
            painter.setPen(QPen(QColor(255, 220, 0), 1))
            painter.drawLine(QPointF(top_x, top_y), QPointF(rot_x, rot_y))

            painter.setBrush(QColor(255, 220, 0))
            for hx, hy in self._handle_positions(geom).values():
                painter.drawEllipse(QPointF(hx, hy), 5, 5)

        self._paint_lines(painter)
        self._paint_spots(painter)

    def _paint_one_spot(self, painter, geom, selected):
        dx, dy, sx, sy, r = geom[:5]
        color = QColor(255, 90, 200) if selected else QColor(255, 90, 200, 140)
        width = 2 if selected else 1
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(color, width))
        painter.drawEllipse(QPointF(dx, dy), r, r)  # destination — solid
        painter.setPen(QPen(color, width, Qt.DashLine))
        painter.drawEllipse(QPointF(sx, sy), r, r)  # source — dashed
        painter.drawLine(QPointF(dx, dy), QPointF(sx, sy))

        if selected:
            painter.setPen(QPen(color, width))
            painter.setBrush(color)
            for hx, hy in self._spot_handle_positions(geom).values():
                painter.drawEllipse(QPointF(hx, hy), 5, 5)

    def _paint_spots(self, painter):
        for i in range(len(self.overlay_spots)):
            if i == self.selected_spot_index:
                continue
            geom = self._spot_screen_geom_for(i)
            if geom is not None:
                self._paint_one_spot(painter, geom, selected=False)

        geom = self._selected_spot_screen_geom()
        if geom is not None:
            self._paint_one_spot(painter, geom, selected=True)

    def _paint_one_line(self, painter, geom, selected):
        zx, zy, fx, fy = geom[:4]
        dx, dy = fx - zx, fy - zy
        length = math.hypot(dx, dy)
        if length < 1e-6:
            ux, uy = 0.0, 1.0  # degenerate: arbitrary perpendicular so ticks still draw
        else:
            ux, uy = -dy / length, dx / length  # unit perpendicular

        color = QColor(60, 200, 255) if selected else QColor(60, 200, 255, 140)
        width = 2 if selected else 1
        painter.setPen(QPen(color, width, Qt.DashLine))
        painter.drawLine(QPointF(zx, zy), QPointF(fx, fy))
        L = self.LINE_BOUNDARY_HALF_LEN
        painter.drawLine(QPointF(zx - ux * L, zy - uy * L), QPointF(zx + ux * L, zy + uy * L))
        painter.drawLine(QPointF(fx - ux * L, fy - uy * L), QPointF(fx + ux * L, fy + uy * L))

        if selected:
            painter.setPen(QPen(color, width))
            painter.setBrush(color)
            painter.drawEllipse(QPointF(zx, zy), 5, 5)
            painter.drawEllipse(QPointF(fx, fy), 5, 5)

    def _paint_lines(self, painter):
        for i in range(len(self.overlay_lines)):
            if i == self.selected_line_index:
                continue
            geom = self._line_screen_geom_for(i)
            if geom is not None:
                self._paint_one_line(painter, geom, selected=False)

        geom = self._selected_line_screen_geom()
        if geom is not None:
            self._paint_one_line(painter, geom, selected=True)

    def _paint_crop(self, painter):
        """Darkens everything outside the crop rectangle (standard crop-tool framing),
        draws the rectangle with a rule-of-thirds grid, and 4 corner drag handles."""
        geom = self._crop_screen_geom()
        if geom is None:
            return
        x0, y0, x1, y1, x_off, y_off, pw, ph = geom

        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, 140))
        painter.drawRect(QRectF(x_off, y_off, pw, y0 - y_off))
        painter.drawRect(QRectF(x_off, y1, pw, (y_off + ph) - y1))
        painter.drawRect(QRectF(x_off, y0, x0 - x_off, y1 - y0))
        painter.drawRect(QRectF(x1, y0, (x_off + pw) - x1, y1 - y0))

        painter.setPen(QPen(QColor(255, 255, 255), 2))
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(QRectF(x0, y0, x1 - x0, y1 - y0))

        painter.setPen(QPen(QColor(255, 255, 255, 160), 1, Qt.DashLine))
        for i in (1, 2):
            gx = x0 + (x1 - x0) * i / 3.0
            painter.drawLine(QPointF(gx, y0), QPointF(gx, y1))
            gy = y0 + (y1 - y0) * i / 3.0
            painter.drawLine(QPointF(x0, gy), QPointF(x1, gy))

        painter.setPen(QPen(QColor(255, 255, 255), 1))
        painter.setBrush(QColor(255, 255, 255))
        for hx, hy in self._crop_handle_positions(geom).values():
            painter.drawRect(QRectF(hx - 5, hy - 5, 10, 10))


class CurveEditor(QWidget):
    """Minimal draggable tone-curve editor for the Curve group's "Custom" mode.

    5 fixed x control points (0/64/128/192/255), y freely draggable,
    connected with straight segments — deliberately matches apply_curve()'s
    existing piecewise-linear LUT (np.interp) rather than adding a spline
    library dependency just for this. Points persist across mode switches
    (only Reset Curve or the panel-wide Reset touches them), so toggling
    away to a preset and back to Custom doesn't lose an edit in progress.
    """

    curveChanged = Signal()
    POINT_XS = [0, 64, 128, 192, 255]
    RADIUS = 6
    PAD = 10

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(200)
        self.setMaximumHeight(200)
        self.points_y = list(self.POINT_XS)  # identity curve
        self._drag_index = None

    def get_points(self):
        return list(zip(self.POINT_XS, self.points_y))

    def reset(self):
        self.points_y = list(self.POINT_XS)
        self.update()
        self.curveChanged.emit()

    def set_points(self, points):
        """Restore y-values from a saved (x, y) list — positional, since this
        editor's x's are always the fixed POINT_XS. Ignored if the count
        doesn't match (foreign/malformed ToneCurvePV2012 data)."""
        if len(points) != len(self.POINT_XS):
            return
        self.points_y = [float(y) for _, y in points]
        self.update()
        self.curveChanged.emit()

    def _to_widget(self, x, y):
        w, h = self.width(), self.height()
        px = self.PAD + (x / 255.0) * (w - 2 * self.PAD)
        py = h - self.PAD - (y / 255.0) * (h - 2 * self.PAD)
        return px, py

    def _y_from_widget(self, py):
        h = self.height()
        y = ((h - self.PAD - py) / (h - 2 * self.PAD)) * 255.0
        return max(0.0, min(255.0, y))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor(248, 248, 248))

        painter.setPen(QPen(QColor(210, 210, 210), 1, Qt.DashLine))
        x0, y0 = self._to_widget(0, 0)
        x1, y1 = self._to_widget(255, 255)
        painter.drawLine(int(x0), int(y0), int(x1), int(y1))

        pts = [self._to_widget(x, y) for x, y in zip(self.POINT_XS, self.points_y)]
        painter.setPen(QPen(QColor(70, 70, 70), 2))
        for i in range(len(pts) - 1):
            painter.drawLine(int(pts[i][0]), int(pts[i][1]), int(pts[i + 1][0]), int(pts[i + 1][1]))

        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(190, 60, 60))
        for px, py in pts:
            painter.drawEllipse(QPointF(px, py), self.RADIUS, self.RADIUS)

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        pos = event.position()
        for i, (x, y) in enumerate(zip(self.POINT_XS, self.points_y)):
            px, py = self._to_widget(x, y)
            if (pos.x() - px) ** 2 + (pos.y() - py) ** 2 <= (self.RADIUS * 2) ** 2:
                self._drag_index = i
                break

    def mouseMoveEvent(self, event):
        if self._drag_index is not None:
            self.points_y[self._drag_index] = self._y_from_widget(event.position().y())
            self.update()
            self.curveChanged.emit()

    def mouseReleaseEvent(self, event):
        self._drag_index = None


class DngConverterWorkCopyThread(QThread):
    """Background job: create a small (2560px-long-edge) downscaled DNG copy of the
    original file, used as the fast-to-reconvert input for DngConverterRenderThread —
    same architecture RethinkRAW itself uses for its own live preview (its `edit.dng`),
    confirmed by reading its Go source (`edit.go`'s `previewEdit()`): converting the full
    original on every settle would take seconds; converting an already-2560px copy takes
    well under a second (measured ~0.8s on this dev machine).

    Runs entirely off the Qt main thread — safe, since it only touches plain file paths
    via subprocess, never a Qt widget.
    """

    ready = Signal(str)   # work_dng_path
    failed = Signal()

    def __init__(self, dngconverter_path, source_path, out_dir, parent=None):
        super().__init__(parent)
        self.dngconverter_path = dngconverter_path
        self.source_path = source_path
        self.out_dir = out_dir

    def run(self):
        try:
            out_name = "work.dng"
            result = subprocess.run(
                [self.dngconverter_path, "-c", "-lossy", "-side", "2560",
                 "-d", self.out_dir, "-o", out_name, self.source_path],
                capture_output=True, timeout=60, creationflags=_NO_WINDOW_FLAGS,
            )
            out_path = os.path.join(self.out_dir, out_name)
            if result.returncode == 0 and os.path.isfile(out_path):
                self.ready.emit(out_path)
            else:
                self.failed.emit()
        except Exception:
            self.failed.emit()


class DngConverterRenderThread(QThread):
    """Background job: write the current edit state's XMP-crs tags onto the small working
    copy, re-run it through Adobe DNG Converter, and extract the resulting preview — a
    genuinely Adobe-rendered image, not this app's own cv2 approximation. Mirrors
    RethinkRAW's own `previewEdit()` (same "retag the small DNG, reconvert, extract
    PreviewImage" sequence, confirmed by reading its Go source), just reimplemented here
    directly against exiftool + Adobe DNG Converter instead of Go + RethinkRAW's own
    wrapper packages.

    `xmp_args` must be pre-built on the **main thread** before this thread starts —
    building them (DNGForge._build_all_edit_xmp_args()) reads Qt widget values
    (slider.value() etc.), which is not safe to do from a background thread; this class
    only ever touches plain data (a path, a list of strings) once running.

    `generation` is an opaque counter the caller uses to detect staleness: if the user
    changes another control while this thread is still running, its eventual result no
    longer reflects the current state and should be discarded — this thread doesn't try
    to detect that itself, it just carries the number through to the `ready` signal for
    the caller to check.
    """

    ready = Signal(str, int)   # jpeg_path, generation
    failed = Signal(int)       # generation

    def __init__(self, work_dng_path, xmp_args, exiftool_path, dngconverter_path, generation, parent=None):
        super().__init__(parent)
        self.work_dng_path = work_dng_path
        self.xmp_args = xmp_args
        self.exiftool_path = exiftool_path
        self.dngconverter_path = dngconverter_path
        self.generation = generation

    def run(self):
        out_dir = os.path.dirname(self.work_dng_path)
        rendered_path = os.path.join(out_dir, "rendered.dng")
        jpeg_path = os.path.join(out_dir, "rendered_preview.jpg")
        try:
            with exiftool.ExifToolHelper(executable=self.exiftool_path) as et:
                et.execute(*self.xmp_args, "-overwrite_original", self.work_dng_path)

            result = subprocess.run(
                [self.dngconverter_path, "-c", "-p2", "-d", out_dir, "-o", "rendered.dng", self.work_dng_path],
                capture_output=True, timeout=60, creationflags=_NO_WINDOW_FLAGS,
            )
            if result.returncode != 0 or not os.path.isfile(rendered_path):
                self.failed.emit(self.generation)
                return

            # Binary extraction goes through a plain subprocess call, not
            # ExifToolHelper's persistent stay-open process — pyexiftool's stay-open
            # protocol is text/JSON-oriented and isn't a reliable way to pull raw binary
            # (JPEG) bytes back out; every other binary extraction in this project
            # (save_to_dng()'s embedded-preview verification, the export feature, the
            # ad-hoc scratch testing throughout this session) uses this same direct
            # subprocess + "-b" pattern instead.
            #
            # CONFIRMED, extracts JpgFromRaw not PreviewImage: same multi-preview-size
            # DNG structure already documented in this project's own "Save" section
            # (the JpgFromRaw bug). Measured directly on a real Adobe DNG Converter
            # output here: PreviewImage (SubIFD1) was only 1024x680, while JpgFromRaw
            # (SubIFD2) was the full working-copy resolution, 2560x1700. Extracting
            # PreviewImage would have silently swapped in a *lower*-resolution preview
            # than the fast cv2 one it's meant to upgrade — a downgrade, not an upgrade.
            with open(jpeg_path, "wb") as f:
                extract = subprocess.run(
                    [self.exiftool_path, "-b", "-JpgFromRaw", rendered_path],
                    stdout=f, timeout=30, creationflags=_NO_WINDOW_FLAGS,
                )
            if extract.returncode != 0 or os.path.getsize(jpeg_path) == 0:
                # Fall back to PreviewImage on the off chance this particular DNG
                # structure has no JpgFromRaw slot at all — smaller is still better than
                # nothing.
                with open(jpeg_path, "wb") as f:
                    extract = subprocess.run(
                        [self.exiftool_path, "-b", "-PreviewImage", rendered_path],
                        stdout=f, timeout=30, creationflags=_NO_WINDOW_FLAGS,
                    )
            if extract.returncode != 0 or os.path.getsize(jpeg_path) == 0:
                self.failed.emit(self.generation)
                return

            self.ready.emit(jpeg_path, self.generation)
        except Exception:
            self.failed.emit(self.generation)
        finally:
            if os.path.exists(rendered_path):
                try:
                    os.unlink(rendered_path)
                except OSError:
                    pass


class DNGForge(QMainWindow):
    """DNGForge: open a DNG, edit non-destructively, preview live.

    Editing is in-memory only so far — nothing is written back to the DNG
    yet (embedded save is a later project phase, see CLAUDE.md). Control
    panel layout (Profile / White Balance / Tone / Presence / Curve /
    Detail) deliberately mirrors RethinkRAW's, the reference implementation
    studied for this project (see project memory dngforge-rethinkraw-research).
    Lens Corrections is the one reference section not reproduced yet — it
    needs lensfunpy + a lens profile database, still phase 4 of the roadmap.
    """

    PREVIEW_HALF_SIZE = True  # decode at half resolution for responsive sliders
    ZOOM_PRESETS = [None, 0.10, 0.25, 0.50, 0.75, 1.00, 1.50, 2.00, 3.00, 4.00]  # None = Fit
    UNDO_STACK_MAX = 20  # bounded history — "a few steps back," not an infinite undo log
    PREVIEW_MAX_DIM = 1600  # further cap the live preview's long edge, in pixels — half_size
    # alone still leaves ~3000px on a 24MP sensor, expensive to push through the whole
    # per-pixel pipeline (tone/curve/presence/detail/local filters) on every debounced
    # slider tweak. Only affects the interactive preview: Save always renders full
    # resolution (half_size=False bypasses this entirely — see _render()).

    def __init__(self):
        super().__init__()
        self.setWindowTitle("DNGForge")
        self.resize(1400, 900)

        self.raw = None
        self.raw_path = None
        self.camera_wb = None  # as-shot multipliers from the DNG itself
        self._auto_wb = None   # cached gray-world multipliers, computed on demand
        self._linear_cache = None      # cached raw.postprocess() output — see _render()
        self._linear_cache_key = None  # (half_size, wb_multipliers) the cache is valid for
        self._pre_local_cache = None      # cached RGB after every global adjustment, before
        self._pre_local_cache_key = None  # radial/gradient/crop — see _render()
        self._undo_stack = []       # past edit-state snapshots, oldest first — see undo()
        self._redo_stack = []       # states undone, most-recent-undone last — see redo()
        self._current_edit_state = None  # snapshot matching what's currently on screen
        self._restoring_state = False    # True while undo()/redo() is applying a snapshot,
        # so _maybe_push_undo_state() doesn't record the restore itself as a new change
        self.camera_profile = None  # DNGCameraProfile for this file, or None (falls back to kelvin_to_multipliers)
        self.exiftool_path = _locate_exiftool()
        self._zoom_level = None  # None = Fit to window; float = fraction of the *preview*
        # pixmap's own resolution (which may itself be capped by PREVIEW_MAX_DIM — this is
        # not a true 1:1-raw-pixel zoom, see _update_image_label()).

        # Background "authoritative Adobe render" preview pipeline — entirely optional,
        # only active when Adobe DNG Converter is actually installed (see
        # _locate_dng_converter()). The fast cv2 preview always renders first/instantly;
        # this replaces it with a genuinely Adobe-rendered image ~1s after things settle,
        # so editing stays responsive while the *shown* result still converges on what
        # Lightroom would actually produce, instead of staying a permanent approximation.
        self._dngconv_path = _locate_dng_converter()
        self._work_dng_path = None       # small (2560px) reconvertible working copy
        self._work_dng_dir = None        # its temp directory, for cleanup
        self._workcopy_thread = None     # currently building the working copy, or None
        self._dngconv_thread = None      # currently re-rendering the working copy, or None
        self._dngconv_pending = False    # a change arrived while _dngconv_thread was running
        self._last_dngconv_state = None  # edit-state snapshot the last Adobe render was for
        self._dngconv_generation = 0     # bumped whenever a stale in-flight render should
        # be ignored (new file loaded) — checked against each thread's own captured value
        self._dngconv_bootstrapped = False  # True once the *first* Adobe render for the
        # current file has landed — from then on _render_preview() never shows the cv2
        # approximation again, see that method's own docstring for why
        self._old_work_dirs = []  # work-copy temp dirs from previous files, swept up on close
        # (not deleted immediately on file switch — a background thread may still be
        # using one; safe to just let them accumulate for one session and clean up on exit)

        self._wb_picker_armed = False
        self._radial_picker_armed = False
        self.radial_filters = []  # list of dict(cx, cy, rx, ry, rotation, feather, invert, exposure, contrast, saturation)
        self.radial_selected = -1  # index into radial_filters, or -1 if none selected
        self._gradient_picker_armed = False
        self.gradient_filters = []  # list of dict(zero_x, zero_y, full_x, full_y, invert, exposure, contrast, saturation)
        self.gradient_selected = -1  # index into gradient_filters, or -1 if none selected
        self._spot_picker_armed = False
        self.spots = []  # list of dict(dest_x, dest_y, src_x, src_y, radius, opacity, method)
        self.spot_selected = -1  # index into spots, or -1 if none selected
        self.crop = None  # None (no crop) | dict(angle, left, top, right, bottom)
        self._crop_editing = False  # True while the crop guide/handles are shown

        self._update_timer = QTimer(self)
        self._update_timer.setSingleShot(True)
        self._update_timer.timeout.connect(self._render_preview)

        self._build_ui()
        self._build_menu()

        if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]):
            self.load_dng(sys.argv[1])

    # ---- UI ----

    def _build_menu(self):
        open_action = QAction("Open DNG…", self)
        open_action.setShortcut("Ctrl+O")
        open_action.triggered.connect(self.open_dng)

        save_action = QAction("Save", self)
        save_action.setShortcut("Ctrl+S")
        save_action.triggered.connect(self.save_to_dng)

        revert_action = QAction("Revert to Last Save", self)
        revert_action.triggered.connect(self.revert_to_saved)

        export_action = QAction("Export Preview as JPEG…", self)
        export_action.setShortcut("Ctrl+E")
        export_action.triggered.connect(self.export_preview_jpeg)

        file_menu = self.menuBar().addMenu("File")
        file_menu.addAction(open_action)
        file_menu.addAction(save_action)
        file_menu.addAction(export_action)
        file_menu.addSeparator()
        file_menu.addAction(revert_action)

        undo_action = QAction("Undo", self)
        undo_action.setShortcut("Ctrl+Z")
        undo_action.triggered.connect(self.undo)

        redo_action = QAction("Redo", self)
        redo_action.setShortcut("Ctrl+Y")
        redo_action.triggered.connect(self.redo)

        edit_menu = self.menuBar().addMenu("Edit")
        edit_menu.addAction(undo_action)
        edit_menu.addAction(redo_action)

        zoom_in_action = QAction("Zoom In", self)
        zoom_in_action.setShortcut("Ctrl+=")
        zoom_in_action.triggered.connect(lambda: self._zoom_step(1))

        zoom_out_action = QAction("Zoom Out", self)
        zoom_out_action.setShortcut("Ctrl+-")
        zoom_out_action.triggered.connect(lambda: self._zoom_step(-1))

        zoom_fit_action = QAction("Zoom to Fit", self)
        zoom_fit_action.setShortcut("Ctrl+0")
        zoom_fit_action.triggered.connect(lambda: self._set_zoom_level(None))

        zoom_100_action = QAction("Zoom 100%", self)
        zoom_100_action.setShortcut("Ctrl+1")
        zoom_100_action.triggered.connect(lambda: self._set_zoom_level(1.0))

        view_menu = self.menuBar().addMenu("View")
        view_menu.addAction(zoom_in_action)
        view_menu.addAction(zoom_out_action)
        view_menu.addAction(zoom_fit_action)
        view_menu.addAction(zoom_100_action)

    def _build_ui(self):
        self.image_label = ImageLabel("Apri un file DNG (File → Open DNG…)")
        self.image_label.setAlignment(Qt.AlignCenter)
        self.image_label.setMinimumSize(200, 200)
        self.image_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.image_label.clicked.connect(self._on_image_clicked)
        self.image_label.ellipseChanged.connect(self._on_ellipse_dragged)
        self.image_label.ellipseSelected.connect(self._on_ellipse_selected)
        self.image_label.lineChanged.connect(self._on_line_dragged)
        self.image_label.lineSelected.connect(self._on_line_selected)
        self.image_label.cropChanged.connect(self._on_crop_dragged)
        self.image_label.spotChanged.connect(self._on_spot_dragged)
        self.image_label.spotSelected.connect(self._on_spot_selected)
        self.image_label.zoomRequested.connect(self._zoom_step)
        self.image_label.zoomToRectRequested.connect(self._on_zoom_to_rect)

        self.image_scroll = QScrollArea()
        self.image_scroll.setWidgetResizable(True)
        self.image_scroll.setWidget(self.image_label)

        zoom_bar = QHBoxLayout()
        zoom_out_btn = QPushButton("−")
        zoom_out_btn.setFixedWidth(30)
        zoom_out_btn.clicked.connect(lambda: self._zoom_step(-1))
        zoom_in_btn = QPushButton("+")
        zoom_in_btn.setFixedWidth(30)
        zoom_in_btn.clicked.connect(lambda: self._zoom_step(1))
        self.zoom_combo = QComboBox()
        self.zoom_combo.addItems([self._zoom_label(p) for p in self.ZOOM_PRESETS])
        self.zoom_combo.currentIndexChanged.connect(self._on_zoom_combo_changed)
        self.zoom_select_btn = QPushButton("🔍 Select Area")
        self.zoom_select_btn.setCheckable(True)
        self.zoom_select_btn.clicked.connect(self._toggle_zoom_select)
        zoom_bar.addWidget(zoom_out_btn)
        zoom_bar.addWidget(self.zoom_combo)
        zoom_bar.addWidget(zoom_in_btn)
        zoom_bar.addWidget(self.zoom_select_btn)
        zoom_bar.addStretch(1)

        image_container = QWidget()
        image_container_layout = QVBoxLayout(image_container)
        image_container_layout.setContentsMargins(0, 0, 0, 0)
        image_container_layout.addLayout(zoom_bar)
        image_container_layout.addWidget(self.image_scroll)

        controls = self._build_controls()
        controls_scroll = QScrollArea()
        controls_scroll.setWidgetResizable(True)
        controls_scroll.setWidget(controls)
        controls_scroll.setFixedWidth(340)

        splitter = QSplitter()
        splitter.addWidget(image_container)
        splitter.addWidget(controls_scroll)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)

        self.setCentralWidget(splitter)
        self.statusBar().showMessage("Pronto")

    def _build_controls(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setAlignment(Qt.AlignTop)

        # Profile
        profile_group = QGroupBox("Profile")
        profile_layout = QVBoxLayout(profile_group)
        self.profile_combo = QComboBox()
        self.profile_combo.addItems([
            "Adobe Color", "Adobe Monochrome", "Adobe Landscape", "Adobe Neutral",
            "Adobe Portrait", "Adobe Vivid", "Adobe Standard", "Adobe Standard B&W", "Custom",
        ])
        self.profile_combo.currentIndexChanged.connect(self._schedule_update)
        profile_layout.addWidget(self.profile_combo)
        layout.addWidget(profile_group)

        # White Balance
        wb_group = QGroupBox("White Balance")
        wb_layout = QVBoxLayout(wb_group)

        self.wb_mode = QComboBox()
        self.wb_mode.addItems([
            "As Shot", "Auto", "Daylight", "Cloudy", "Shade", "Tungsten",
            "Fluorescent", "Flash", "Custom", "Camera Matching…",
        ])
        self.wb_mode.currentIndexChanged.connect(self._on_wb_mode_changed)
        wb_layout.addWidget(self.wb_mode)

        self.temp_slider = Slider("Temperature", 2000, 12000, 5500, step=50, on_change=self._on_wb_slider_changed)
        self.tint_slider = Slider("Tint", -150, 150, 0, on_change=self._on_wb_slider_changed)
        wb_layout.addWidget(self.temp_slider)
        wb_layout.addWidget(self.tint_slider)

        self.wb_picker_btn = QPushButton("Pick White Balance")
        self.wb_picker_btn.setCheckable(True)
        self.wb_picker_btn.clicked.connect(self._toggle_wb_picker)
        wb_layout.addWidget(self.wb_picker_btn)

        layout.addWidget(wb_group)

        # Tone
        tone_group = QGroupBox("Tone")
        tone_layout = QVBoxLayout(tone_group)

        self.tone_mode = QComboBox()
        self.tone_mode.addItems(["Auto", "Default", "Custom"])
        self.tone_mode.setCurrentText("Default")
        self.tone_mode.currentIndexChanged.connect(self._on_tone_mode_changed)
        tone_layout.addWidget(self.tone_mode)

        self.exposure_slider = Slider("Exposure", -3, 3, 0, decimals=2, on_change=self._on_tone_slider_changed)
        self.contrast_slider = Slider("Contrast", -100, 100, 0, on_change=self._on_tone_slider_changed)
        self.highlights_slider = Slider("Highlights", -100, 100, 0, on_change=self._on_tone_slider_changed)
        self.shadows_slider = Slider("Shadows", -100, 100, 0, on_change=self._on_tone_slider_changed)
        self.whites_slider = Slider("Whites", -100, 100, 0, on_change=self._on_tone_slider_changed)
        self.blacks_slider = Slider("Blacks", -100, 100, 0, on_change=self._on_tone_slider_changed)
        for s in (self.exposure_slider, self.contrast_slider, self.highlights_slider,
                  self.shadows_slider, self.whites_slider, self.blacks_slider):
            tone_layout.addWidget(s)
        layout.addWidget(tone_group)

        # Presence
        presence_group = QGroupBox("Presence")
        presence_layout = QVBoxLayout(presence_group)
        self.texture_slider = Slider("Texture", -100, 100, 0, on_change=self._schedule_update)
        self.clarity_slider = Slider("Clarity", -100, 100, 0, on_change=self._schedule_update)
        self.dehaze_slider = Slider("Dehaze", -100, 100, 0, on_change=self._schedule_update)
        self.vibrance_slider = Slider("Vibrance", -100, 100, 0, on_change=self._schedule_update)
        self.saturation_slider = Slider("Saturation", -100, 100, 0, on_change=self._schedule_update)
        for s in (self.texture_slider, self.clarity_slider, self.dehaze_slider,
                  self.vibrance_slider, self.saturation_slider):
            presence_layout.addWidget(s)
        layout.addWidget(presence_group)

        # Curve
        curve_group = QGroupBox("Curve")
        curve_layout = QVBoxLayout(curve_group)
        self.curve_combo = QComboBox()
        self.curve_combo.addItems(list(CURVE_PRESETS.keys()))
        self.curve_combo.currentIndexChanged.connect(self._on_curve_mode_changed)
        curve_layout.addWidget(self.curve_combo)

        self.curve_editor = CurveEditor()
        self.curve_editor.setVisible(False)
        self.curve_editor.curveChanged.connect(self._schedule_update)
        curve_layout.addWidget(self.curve_editor)

        self.curve_reset_btn = QPushButton("Reset Curve")
        self.curve_reset_btn.setVisible(False)
        self.curve_reset_btn.clicked.connect(self.curve_editor.reset)
        curve_layout.addWidget(self.curve_reset_btn)

        layout.addWidget(curve_group)

        # Detail
        detail_group = QGroupBox("Detail")
        detail_layout = QVBoxLayout(detail_group)
        self.sharpness_slider = Slider("Sharpness", 0, 100, 0, on_change=self._schedule_update)
        self.luminance_nr_slider = Slider("Luminance NR", 0, 100, 0, on_change=self._schedule_update)
        self.color_nr_slider = Slider("Color NR", 0, 100, 0, on_change=self._schedule_update)
        for s in (self.sharpness_slider, self.luminance_nr_slider, self.color_nr_slider):
            detail_layout.addWidget(s)
        layout.addWidget(detail_group)

        # Radial Filter
        radial_group = QGroupBox("Radial Filter")
        radial_layout = QVBoxLayout(radial_group)

        radial_buttons = QHBoxLayout()
        self.radial_place_btn = QPushButton("Place Radial Filter")
        self.radial_place_btn.setCheckable(True)
        self.radial_place_btn.clicked.connect(self._toggle_radial_picker)
        self.radial_clear_btn = QPushButton("Remove")
        self.radial_clear_btn.setVisible(False)
        self.radial_clear_btn.clicked.connect(self._clear_radial_filter)
        radial_buttons.addWidget(self.radial_place_btn)
        radial_buttons.addWidget(self.radial_clear_btn)
        radial_layout.addLayout(radial_buttons)

        self.radial_show_check = QCheckBox("Show handles / edit")
        self.radial_show_check.setChecked(True)
        self.radial_show_check.setVisible(False)
        self.radial_show_check.toggled.connect(self._toggle_radial_visibility)
        radial_layout.addWidget(self.radial_show_check)

        self.radial_controls = QWidget()
        rc_layout = QVBoxLayout(self.radial_controls)
        rc_layout.setContentsMargins(0, 0, 0, 0)
        self.radial_size_x_slider = Slider("Size X", 5, 100, 25, on_change=self._on_radial_slider_changed)
        self.radial_size_y_slider = Slider("Size Y", 5, 100, 25, on_change=self._on_radial_slider_changed)
        self.radial_rotation_slider = Slider("Rotation", -180, 180, 0, on_change=self._on_radial_slider_changed)
        self.radial_feather_slider = Slider("Feather", 0, 100, 50, on_change=self._on_radial_slider_changed)
        self.radial_invert_check = QCheckBox("Invert")
        self.radial_invert_check.toggled.connect(self._on_radial_slider_changed)
        self.radial_exposure_slider = Slider("Exposure", -3, 3, 0, decimals=2, on_change=self._on_radial_slider_changed)
        self.radial_contrast_slider = Slider("Contrast", -100, 100, 0, on_change=self._on_radial_slider_changed)
        self.radial_saturation_slider = Slider("Saturation", -100, 100, 0, on_change=self._on_radial_slider_changed)
        for w in (self.radial_size_x_slider, self.radial_size_y_slider, self.radial_rotation_slider,
                  self.radial_feather_slider, self.radial_invert_check, self.radial_exposure_slider,
                  self.radial_contrast_slider, self.radial_saturation_slider):
            rc_layout.addWidget(w)
        self.radial_controls.setVisible(False)
        radial_layout.addWidget(self.radial_controls)

        layout.addWidget(radial_group)

        # Graduated Filter — same interaction pattern as Radial Filter above, but the
        # geometry is two draggable points (no Size/Rotation/Feather sliders): see
        # build_linear_mask()/CLAUDE.md "Graduated filter persistence" for why a linear
        # gradient needs only two points, unlike the radial filter's ellipse.
        gradient_group = QGroupBox("Graduated Filter")
        gradient_layout = QVBoxLayout(gradient_group)

        gradient_buttons = QHBoxLayout()
        self.gradient_place_btn = QPushButton("Place Graduated Filter")
        self.gradient_place_btn.setCheckable(True)
        self.gradient_place_btn.clicked.connect(self._toggle_gradient_picker)
        self.gradient_clear_btn = QPushButton("Remove")
        self.gradient_clear_btn.setVisible(False)
        self.gradient_clear_btn.clicked.connect(self._clear_gradient_filter)
        gradient_buttons.addWidget(self.gradient_place_btn)
        gradient_buttons.addWidget(self.gradient_clear_btn)
        gradient_layout.addLayout(gradient_buttons)

        self.gradient_show_check = QCheckBox("Show handles / edit")
        self.gradient_show_check.setChecked(True)
        self.gradient_show_check.setVisible(False)
        self.gradient_show_check.toggled.connect(self._toggle_gradient_visibility)
        gradient_layout.addWidget(self.gradient_show_check)

        self.gradient_controls = QWidget()
        gc_layout = QVBoxLayout(self.gradient_controls)
        gc_layout.setContentsMargins(0, 0, 0, 0)
        self.gradient_invert_check = QCheckBox("Invert")
        self.gradient_invert_check.toggled.connect(self._on_gradient_slider_changed)
        self.gradient_exposure_slider = Slider("Exposure", -3, 3, 0, decimals=2, on_change=self._on_gradient_slider_changed)
        self.gradient_contrast_slider = Slider("Contrast", -100, 100, 0, on_change=self._on_gradient_slider_changed)
        self.gradient_saturation_slider = Slider("Saturation", -100, 100, 0, on_change=self._on_gradient_slider_changed)
        for w in (self.gradient_invert_check, self.gradient_exposure_slider,
                  self.gradient_contrast_slider, self.gradient_saturation_slider):
            gc_layout.addWidget(w)
        self.gradient_controls.setVisible(False)
        gradient_layout.addWidget(self.gradient_controls)

        layout.addWidget(gradient_group)

        # Spot Removal (roadmap item 13) — two circles per region (destination + source),
        # both draggable by clicking anywhere in their body, plus a resize grip on the
        # destination circle (shared radius, since the tool copies a same-size patch).
        spot_group = QGroupBox("Spot Removal")
        spot_layout = QVBoxLayout(spot_group)

        spot_buttons = QHBoxLayout()
        self.spot_place_btn = QPushButton("Place Spot")
        self.spot_place_btn.setCheckable(True)
        self.spot_place_btn.clicked.connect(self._toggle_spot_picker)
        self.spot_clear_btn = QPushButton("Remove")
        self.spot_clear_btn.setVisible(False)
        self.spot_clear_btn.clicked.connect(self._clear_spot)
        spot_buttons.addWidget(self.spot_place_btn)
        spot_buttons.addWidget(self.spot_clear_btn)
        spot_layout.addLayout(spot_buttons)

        self.spot_show_check = QCheckBox("Show handles / edit")
        self.spot_show_check.setChecked(True)
        self.spot_show_check.setVisible(False)
        self.spot_show_check.toggled.connect(self._toggle_spot_visibility)
        spot_layout.addWidget(self.spot_show_check)

        self.spot_controls = QWidget()
        sc_layout = QVBoxLayout(self.spot_controls)
        sc_layout.setContentsMargins(0, 0, 0, 0)
        self.spot_method_combo = QComboBox()
        self.spot_method_combo.addItems(["Heal", "Clone"])
        self.spot_method_combo.currentIndexChanged.connect(self._on_spot_slider_changed)
        sc_layout.addWidget(self.spot_method_combo)
        self.spot_opacity_slider = Slider("Opacity", 0, 100, 100, on_change=self._on_spot_slider_changed)
        sc_layout.addWidget(self.spot_opacity_slider)
        self.spot_controls.setVisible(False)
        spot_layout.addWidget(self.spot_controls)

        layout.addWidget(spot_group)

        # Crop & Straighten (roadmap items 8-9) — only ever affects the rendered preview/JPEG,
        # never self.raw or the DNG's own pixel data, same non-destructive convention as every
        # other adjustment (confirmed with the user explicitly). Straighten's Rotation slider
        # auto-clamps the crop rectangle to stay inside the rotated frame's valid area — see
        # clamp_crop_to_rotation()/max_inscribed_rect().
        crop_group = QGroupBox("Crop & Straighten")
        crop_layout = QVBoxLayout(crop_group)

        # Single "Crop" toggle, Lightroom-style (like pressing R): checking it *enters*
        # editing mode (full frame + rectangle guide, creating a default full-frame
        # rectangle only the first time — an existing/restored crop is shown as-is, never
        # reset), unchecking it *commits* whatever rectangle is currently set and shows the
        # actual cropped result. Toggling it on/off never discards the crop by itself —
        # only the separate "Reset" button does that (same Place/Remove split the radial
        # and gradient filters already use, chosen after the user tried a single
        # enable/disable button and found it confusingly discarded their crop on re-check).
        crop_buttons = QHBoxLayout()
        self.crop_enable_btn = QPushButton("Crop")
        self.crop_enable_btn.setCheckable(True)
        self.crop_enable_btn.clicked.connect(self._toggle_crop_enable)
        self.crop_reset_btn = QPushButton("Reset")
        self.crop_reset_btn.setVisible(False)
        self.crop_reset_btn.clicked.connect(self._reset_crop)
        crop_buttons.addWidget(self.crop_enable_btn)
        crop_buttons.addWidget(self.crop_reset_btn)
        crop_layout.addLayout(crop_buttons)

        self.crop_controls = QWidget()
        cc_layout = QVBoxLayout(self.crop_controls)
        cc_layout.setContentsMargins(0, 0, 0, 0)
        self.crop_aspect_combo = QComboBox()
        self.crop_aspect_combo.addItems(list(CROP_ASPECT_RATIOS.keys()))
        self.crop_aspect_combo.currentIndexChanged.connect(self._on_crop_aspect_changed)
        cc_layout.addWidget(self.crop_aspect_combo)
        self.crop_rotation_slider = Slider("Rotation", -45, 45, 0, step=0.1, decimals=1,
                                            on_change=self._on_crop_rotation_changed)
        cc_layout.addWidget(self.crop_rotation_slider)
        self.crop_controls.setVisible(False)
        crop_layout.addWidget(self.crop_controls)

        layout.addWidget(crop_group)

        # Metadata — read-only, exact field list/order requested by the user (roadmap item 10).
        # Populated by _update_metadata_panel(), called from load_dng(). Uses exiftool with
        # print-conversion ENABLED (unlike every other exiftool read in this app, which passes
        # -n for machine-parsable numeric values) — this panel wants "1/125", "Off, Did not
        # fire", "Aperture-priority AE", not raw EXIF codes.
        metadata_group = QGroupBox("Metadata")
        metadata_form = QFormLayout(metadata_group)
        metadata_form.setLabelAlignment(Qt.AlignLeft)
        self.metadata_labels = {}
        for field in METADATA_FIELDS:
            value_label = QLabel("—")
            value_label.setWordWrap(True)
            value_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self.metadata_labels[field] = value_label
            metadata_form.addRow(f"{field}:", value_label)
        layout.addWidget(metadata_group)

        buttons_row = QHBoxLayout()
        reset_btn = QPushButton("Reset")
        reset_btn.clicked.connect(self.reset_controls)
        save_btn = QPushButton("Save")
        save_btn.clicked.connect(self.save_to_dng)
        buttons_row.addWidget(reset_btn)
        buttons_row.addWidget(save_btn)
        layout.addLayout(buttons_row)

        self._set_wb_controls_enabled(False)
        return panel

    def _set_wb_controls_enabled(self, enabled):
        self.temp_slider.setEnabled(enabled)
        self.tint_slider.setEnabled(enabled)

    def _on_wb_mode_changed(self):
        mode = self.wb_mode.currentText()
        if mode in WB_PRESETS:
            temp, tint = WB_PRESETS[mode]
            self.temp_slider.set_value(temp)
            self.tint_slider.set_value(tint)
        elif mode in ("As Shot", "Camera Matching…") and self.camera_wb:
            temp, tint = self._multipliers_to_temp_tint(self.camera_wb)
            self.temp_slider.set_value(temp)
            self.tint_slider.set_value(tint)
        elif mode == "Auto" and self.raw is not None:
            self._auto_wb = self._compute_auto_wb()
            temp, tint = self._multipliers_to_temp_tint(self._auto_wb)
            self.temp_slider.set_value(temp)
            self.tint_slider.set_value(tint)
        self._set_wb_controls_enabled(mode in WB_PRESETS or mode == "Custom")
        self._schedule_update()

    def _on_wb_slider_changed(self):
        mode = self.wb_mode.currentText()
        if mode in WB_PRESETS:
            self.wb_mode.blockSignals(True)
            self.wb_mode.setCurrentText("Custom")
            self.wb_mode.blockSignals(False)
        self._schedule_update()

    def _toggle_wb_picker(self, checked):
        if self.raw is None:
            self.wb_picker_btn.setChecked(False)
            return
        self._wb_picker_armed = checked
        self.image_label.setCursor(Qt.CrossCursor if checked else Qt.ArrowCursor)
        self.statusBar().showMessage(
            "Clicca su un'area neutra (grigia/bianca) dell'immagine…" if checked else "Pronto"
        )

    def _on_image_clicked(self, x_frac, y_frac):
        if self._radial_picker_armed and self.raw is not None:
            self._place_radial_filter(x_frac, y_frac)
            return
        if self._gradient_picker_armed and self.raw is not None:
            self._place_gradient_filter(x_frac, y_frac)
            return
        if self._spot_picker_armed and self.raw is not None:
            self._place_spot(x_frac, y_frac)
            return
        if not self._wb_picker_armed or self.raw is None:
            return
        self.wb_picker_btn.setChecked(False)
        self._wb_picker_armed = False
        self.image_label.setCursor(Qt.ArrowCursor)

        try:
            neutral = sample_raw_neutral(self.raw_path, x_frac, y_frac)
        except ValueError as e:
            QMessageBox.warning(self, "Bilanciamento del bianco", str(e))
            return

        if self.camera_profile is not None:
            temp, tint = self.camera_profile.neutral_to_temperature_tint(neutral)
        else:
            mult = [1.0 / neutral[0] if neutral[0] else 1.0, 1.0, 1.0 / neutral[2] if neutral[2] else 1.0]
            temp, tint = multipliers_to_kelvin_tint(*mult)
        temp = max(2000.0, min(12000.0, temp))
        tint = max(-150.0, min(150.0, tint))

        self.wb_mode.blockSignals(True)
        self.wb_mode.setCurrentText("Custom")
        self.wb_mode.blockSignals(False)
        self._set_wb_controls_enabled(True)
        self.temp_slider.set_value(temp)
        self.tint_slider.set_value(tint)
        self._schedule_update()
        self.statusBar().showMessage(f"Bilanciamento del bianco impostato dal click: {temp:.0f}K, tint {tint:.0f}")

    def _toggle_radial_picker(self, checked):
        if self.raw is None:
            self.radial_place_btn.setChecked(False)
            return
        self._radial_picker_armed = checked
        self.image_label.setCursor(Qt.CrossCursor if checked else Qt.ArrowCursor)
        self.statusBar().showMessage("Clicca sull'immagine per posizionare un nuovo filtro radiale…" if checked else "Pronto")

    def _place_radial_filter(self, x_frac, y_frac):
        """Adds a new radial filter region — does NOT replace existing ones (multi-region,
        like Lightroom: "se torno in radial filter mi aspetto di poter vedere le varie
        ellissi piazzate in precedenza"). The new one becomes the selected/editable one.
        """
        self.radial_place_btn.setChecked(False)
        self._radial_picker_armed = False
        self.image_label.setCursor(Qt.ArrowCursor)

        self.radial_filters.append({
            "cx": x_frac, "cy": y_frac, "rx": 0.25, "ry": 0.25, "rotation": 0.0, "feather": 50.0,
            "invert": False, "exposure": 0.0, "contrast": 0, "saturation": 0,
        })
        self._select_radial_filter(len(self.radial_filters) - 1)
        self._schedule_update()

    def _select_radial_filter(self, index):
        """Make radial_filters[index] the one shown/edited — loads its params into the
        sliders (blocked signals, so that doesn't itself trigger a change) and refreshes
        the overlay so the selected ellipse gets full drag handles, others a thin outline.
        """
        self.radial_selected = index
        self.image_label.selected_index = index
        if not (0 <= index < len(self.radial_filters)):
            self.radial_controls.setVisible(False)
            self.radial_clear_btn.setVisible(False)
            self.radial_show_check.setVisible(False)
            self._update_radial_overlay()
            return

        f = self.radial_filters[index]
        for slider, value in (
            (self.radial_size_x_slider, f["rx"] * 100.0), (self.radial_size_y_slider, f["ry"] * 100.0),
            (self.radial_rotation_slider, f["rotation"]), (self.radial_feather_slider, f["feather"]),
            (self.radial_exposure_slider, f["exposure"]), (self.radial_contrast_slider, f["contrast"]),
            (self.radial_saturation_slider, f["saturation"]),
        ):
            slider.set_value(value)
        self.radial_invert_check.blockSignals(True)
        self.radial_invert_check.setChecked(f["invert"])
        self.radial_invert_check.blockSignals(False)

        self.radial_controls.setVisible(True)
        self.radial_clear_btn.setVisible(True)
        self.radial_show_check.setVisible(True)
        self.radial_show_check.blockSignals(True)
        self.radial_show_check.setChecked(True)
        self.radial_show_check.blockSignals(False)
        self._update_radial_overlay()
        n = len(self.radial_filters)
        self.statusBar().showMessage(f"Filtro radiale {index + 1} di {n} selezionato")

    def _clear_radial_filter(self):
        """Removes the *selected* filter only — others, if any, stay in place."""
        if 0 <= self.radial_selected < len(self.radial_filters):
            del self.radial_filters[self.radial_selected]
        next_index = min(self.radial_selected, len(self.radial_filters) - 1)
        self._select_radial_filter(next_index)
        self._schedule_update()

    def _toggle_radial_visibility(self, checked):
        """Hide the selected ellipse's handles (and the adjustment sliders) without
        touching radial_filters — every filter's effect stays fully active on the
        rendered image either way, this only controls whether it's shown/draggable.
        Added after the user asked how to "get back to" the global controls once done
        placing/adjusting a filter — nothing was actually blocking that, but the
        always-visible overlay/handles felt like it was still "in the way" on the preview.
        """
        self.radial_controls.setVisible(checked)
        if checked:
            self.image_label.selected_index = self.radial_selected
        else:
            self.image_label.selected_index = -1
        self.image_label.update()

    def _on_radial_slider_changed(self):
        if not (0 <= self.radial_selected < len(self.radial_filters)):
            return
        self.radial_filters[self.radial_selected].update({
            "rx": self.radial_size_x_slider.value() / 100.0,
            "ry": self.radial_size_y_slider.value() / 100.0,
            "rotation": self.radial_rotation_slider.value(),
            "feather": self.radial_feather_slider.value(),
            "invert": self.radial_invert_check.isChecked(),
            "exposure": self.radial_exposure_slider.value(),
            "contrast": self.radial_contrast_slider.value(),
            "saturation": self.radial_saturation_slider.value(),
        })
        self._update_radial_overlay()
        self._schedule_update()

    def _update_radial_overlay(self):
        self.image_label.overlay_ellipses = [
            {"cx": f["cx"], "cy": f["cy"], "rx": f["rx"], "ry": f["ry"], "rotation": f["rotation"]}
            for f in self.radial_filters
        ]
        self.image_label.selected_index = self.radial_selected
        self.image_label.update()

    def _on_ellipse_dragged(self, geom):
        """The ImageLabel overlay was dragged directly (move/resize/rotate handle) —
        sync radial_filters[selected] and the sliders (which drive it the other way, via
        _on_radial_slider_changed) so both stay consistent regardless of which one the
        user is interacting with.
        """
        if not (0 <= self.radial_selected < len(self.radial_filters)):
            return
        self.radial_filters[self.radial_selected].update({
            "cx": geom["cx"], "cy": geom["cy"], "rx": geom["rx"], "ry": geom["ry"], "rotation": geom["rotation"],
        })
        self.radial_size_x_slider.set_value(geom["rx"] * 100.0)
        self.radial_size_y_slider.set_value(geom["ry"] * 100.0)
        self.radial_rotation_slider.set_value(geom["rotation"])
        self._schedule_update()

    def _on_ellipse_selected(self, index):
        """User clicked directly on a (non-handle) ellipse body — select that one for editing."""
        self._select_radial_filter(index)

    def _toggle_gradient_picker(self, checked):
        if self.raw is None:
            self.gradient_place_btn.setChecked(False)
            return
        self._gradient_picker_armed = checked
        self.image_label.setCursor(Qt.CrossCursor if checked else Qt.ArrowCursor)
        self.statusBar().showMessage(
            "Clicca sull'immagine per posizionare un nuovo filtro graduato…" if checked else "Pronto"
        )

    def _place_gradient_filter(self, x_frac, y_frac):
        """Adds a new graduated filter region — appends rather than replaces, same
        multi-region behavior as _place_radial_filter(). Defaults to a vertical band
        (zero above the click, full below), the same default orientation Lightroom's
        own graduated filter starts with on a plain click.
        """
        self.gradient_place_btn.setChecked(False)
        self._gradient_picker_armed = False
        self.image_label.setCursor(Qt.ArrowCursor)

        self.gradient_filters.append({
            "zero_x": x_frac, "zero_y": max(0.0, y_frac - 0.15),
            "full_x": x_frac, "full_y": min(1.0, y_frac + 0.15),
            "invert": False, "exposure": 0.0, "contrast": 0, "saturation": 0,
        })
        self._select_gradient_filter(len(self.gradient_filters) - 1)
        self._schedule_update()

    def _select_gradient_filter(self, index):
        """Make gradient_filters[index] the one shown/edited — mirrors _select_radial_filter()."""
        self.gradient_selected = index
        self.image_label.selected_line_index = index
        if not (0 <= index < len(self.gradient_filters)):
            self.gradient_controls.setVisible(False)
            self.gradient_clear_btn.setVisible(False)
            self.gradient_show_check.setVisible(False)
            self._update_gradient_overlay()
            return

        f = self.gradient_filters[index]
        for slider, value in (
            (self.gradient_exposure_slider, f["exposure"]), (self.gradient_contrast_slider, f["contrast"]),
            (self.gradient_saturation_slider, f["saturation"]),
        ):
            slider.set_value(value)
        self.gradient_invert_check.blockSignals(True)
        self.gradient_invert_check.setChecked(f["invert"])
        self.gradient_invert_check.blockSignals(False)

        self.gradient_controls.setVisible(True)
        self.gradient_clear_btn.setVisible(True)
        self.gradient_show_check.setVisible(True)
        self.gradient_show_check.blockSignals(True)
        self.gradient_show_check.setChecked(True)
        self.gradient_show_check.blockSignals(False)
        self._update_gradient_overlay()
        n = len(self.gradient_filters)
        self.statusBar().showMessage(f"Filtro graduato {index + 1} di {n} selezionato")

    def _clear_gradient_filter(self):
        """Removes the *selected* filter only — others, if any, stay in place."""
        if 0 <= self.gradient_selected < len(self.gradient_filters):
            del self.gradient_filters[self.gradient_selected]
        next_index = min(self.gradient_selected, len(self.gradient_filters) - 1)
        self._select_gradient_filter(next_index)
        self._schedule_update()

    def _toggle_gradient_visibility(self, checked):
        """Hide the selected line's handles without touching gradient_filters — mirrors
        _toggle_radial_visibility()."""
        self.gradient_controls.setVisible(checked)
        if checked:
            self.image_label.selected_line_index = self.gradient_selected
        else:
            self.image_label.selected_line_index = -1
        self.image_label.update()

    def _on_gradient_slider_changed(self):
        if not (0 <= self.gradient_selected < len(self.gradient_filters)):
            return
        self.gradient_filters[self.gradient_selected].update({
            "invert": self.gradient_invert_check.isChecked(),
            "exposure": self.gradient_exposure_slider.value(),
            "contrast": self.gradient_contrast_slider.value(),
            "saturation": self.gradient_saturation_slider.value(),
        })
        self._schedule_update()

    def _update_gradient_overlay(self):
        self.image_label.overlay_lines = [
            {"zero_x": f["zero_x"], "zero_y": f["zero_y"], "full_x": f["full_x"], "full_y": f["full_y"]}
            for f in self.gradient_filters
        ]
        self.image_label.selected_line_index = self.gradient_selected
        self.image_label.update()

    def _on_line_dragged(self, geom):
        """The ImageLabel overlay line was dragged directly — sync gradient_filters[selected].
        Unlike the radial filter, there are no sliders driven the other way (geometry is
        drag-only), so this is a one-way sync, not the bidirectional pattern
        _on_ellipse_dragged() uses.
        """
        if not (0 <= self.gradient_selected < len(self.gradient_filters)):
            return
        self.gradient_filters[self.gradient_selected].update({
            "zero_x": geom["zero_x"], "zero_y": geom["zero_y"],
            "full_x": geom["full_x"], "full_y": geom["full_y"],
        })
        self._schedule_update()

    def _on_line_selected(self, index):
        """User clicked directly on a (non-handle) gradient line's spine — select it."""
        self._select_gradient_filter(index)

    SPOT_DEFAULT_RADIUS = 0.03  # fraction of image width

    def _toggle_spot_picker(self, checked):
        if self.raw is None:
            self.spot_place_btn.setChecked(False)
            return
        self._spot_picker_armed = checked
        self.image_label.setCursor(Qt.CrossCursor if checked else Qt.ArrowCursor)
        self.statusBar().showMessage(
            "Clicca sulla macchia da rimuovere…" if checked else "Pronto"
        )

    def _place_spot(self, x_frac, y_frac):
        """Adds a new spot-removal region — appends rather than replaces, same multi-region
        behavior as radial/gradient. Auto-picks a default source position offset to the
        side (flipping to the left near the right edge, so the source circle never starts
        off-frame) — a simplified stand-in for Lightroom's own "nearby similar texture"
        auto-search, which this app doesn't attempt; the user drags the source circle to
        wherever actually looks right.
        """
        self.spot_place_btn.setChecked(False)
        self._spot_picker_armed = False
        self.image_label.setCursor(Qt.ArrowCursor)

        r = self.SPOT_DEFAULT_RADIUS
        offset = r * 2.5
        src_x = x_frac - offset if x_frac + offset + r > 1.0 else x_frac + offset
        self.spots.append({
            "dest_x": x_frac, "dest_y": y_frac,
            "src_x": _clamp(src_x, r, 1.0 - r), "src_y": y_frac,
            "radius": r, "opacity": 100, "method": "Heal",
        })
        self._select_spot(len(self.spots) - 1)
        self._schedule_update()

    def _select_spot(self, index):
        """Make spots[index] the one shown/edited — mirrors _select_radial_filter()."""
        self.spot_selected = index
        self.image_label.selected_spot_index = index
        if not (0 <= index < len(self.spots)):
            self.spot_controls.setVisible(False)
            self.spot_clear_btn.setVisible(False)
            self.spot_show_check.setVisible(False)
            self._update_spot_overlay()
            return

        s = self.spots[index]
        self.spot_method_combo.blockSignals(True)
        self.spot_method_combo.setCurrentText(s["method"])
        self.spot_method_combo.blockSignals(False)
        self.spot_opacity_slider.set_value(s["opacity"])

        self.spot_controls.setVisible(True)
        self.spot_clear_btn.setVisible(True)
        self.spot_show_check.setVisible(True)
        self.spot_show_check.blockSignals(True)
        self.spot_show_check.setChecked(True)
        self.spot_show_check.blockSignals(False)
        self._update_spot_overlay()
        n = len(self.spots)
        self.statusBar().showMessage(f"Macchia {index + 1} di {n} selezionata")

    def _clear_spot(self):
        """Removes the *selected* spot only — others, if any, stay in place."""
        if 0 <= self.spot_selected < len(self.spots):
            del self.spots[self.spot_selected]
        next_index = min(self.spot_selected, len(self.spots) - 1)
        self._select_spot(next_index)
        self._schedule_update()

    def _toggle_spot_visibility(self, checked):
        """Hide the selected spot's handles without touching self.spots — mirrors
        _toggle_radial_visibility()."""
        self.spot_controls.setVisible(checked)
        if checked:
            self.image_label.selected_spot_index = self.spot_selected
        else:
            self.image_label.selected_spot_index = -1
        self.image_label.update()

    def _on_spot_slider_changed(self):
        if not (0 <= self.spot_selected < len(self.spots)):
            return
        self.spots[self.spot_selected].update({
            "method": self.spot_method_combo.currentText(),
            "opacity": self.spot_opacity_slider.value(),
        })
        self._schedule_update()

    def _update_spot_overlay(self):
        self.image_label.overlay_spots = [
            {"dest_x": s["dest_x"], "dest_y": s["dest_y"], "src_x": s["src_x"], "src_y": s["src_y"],
             "radius": s["radius"]}
            for s in self.spots
        ]
        self.image_label.selected_spot_index = self.spot_selected
        self.image_label.update()

    def _on_spot_dragged(self, geom):
        """The ImageLabel overlay circle(s) were dragged directly — sync spots[selected].
        One-way sync (geometry is drag-only, no sliders drive it the other way), same
        pattern _on_line_dragged() uses.
        """
        if not (0 <= self.spot_selected < len(self.spots)):
            return
        self.spots[self.spot_selected].update({
            "dest_x": geom["dest_x"], "dest_y": geom["dest_y"],
            "src_x": geom["src_x"], "src_y": geom["src_y"],
            "radius": geom["radius"],
        })
        self._schedule_update()

    def _on_spot_selected(self, index):
        """User clicked directly on a (non-handle) spot's circle body — select it."""
        self._select_spot(index)

    def _toggle_crop_enable(self, checked):
        """The single "Crop" toggle: checking it *enters* editing mode (full frame +
        rectangle guide); unchecking it *commits* whatever rectangle is currently set and
        switches the preview to the actual cropped result. Never discards self.crop by
        itself — creates a default full-frame rectangle only the very first time (no crop
        exists yet); re-checking on an existing/restored crop shows it as-is, unchanged,
        so re-opening a cropped file and clicking "Crop" reveals the full frame with the
        *original* crop boundary marked, ready for further adjustment.
        """
        if self.raw is None:
            self.crop_enable_btn.setChecked(False)
            return
        if self.crop is None:
            self.crop = {"angle": 0.0, "left": 0.0, "top": 0.0, "right": 1.0, "bottom": 1.0}
            self.crop_rotation_slider.set_value(0.0)
            self.crop_aspect_combo.blockSignals(True)
            self.crop_aspect_combo.setCurrentIndex(0)  # Freeform
            self.crop_aspect_combo.blockSignals(False)
            self.image_label.crop_aspect = None
        self._crop_editing = checked
        self.crop_controls.setVisible(True)
        self.crop_reset_btn.setVisible(True)
        self._update_crop_overlay()
        self._schedule_update()

    def _reset_crop(self):
        """Actually discards the crop — the only control that does (mirrors the radial/
        gradient filters' own Place/Remove split, chosen after a single enable/disable
        toggle was found to confusingly discard the crop on re-check)."""
        self.crop = None
        self._crop_editing = False
        self.crop_enable_btn.blockSignals(True)
        self.crop_enable_btn.setChecked(False)
        self.crop_enable_btn.blockSignals(False)
        self.crop_controls.setVisible(False)
        self.crop_reset_btn.setVisible(False)
        self.crop_aspect_combo.blockSignals(True)
        self.crop_aspect_combo.setCurrentIndex(0)  # Freeform
        self.crop_aspect_combo.blockSignals(False)
        self.image_label.crop_aspect = None
        self._update_crop_overlay()
        self._schedule_update()

    def _on_crop_aspect_changed(self):
        if self.crop is None or self.raw is None:
            return
        label = self.crop_aspect_combo.currentText()
        ratio = CROP_ASPECT_RATIOS.get(label)
        if ratio == "original":
            ratio = self.raw.sizes.width / self.raw.sizes.height
        self._apply_crop_aspect_ratio(ratio)

    def _apply_crop_aspect_ratio(self, ratio):
        """ratio is a target output width/height in *pixels*, or None for freeform.
        Reshapes the current rectangle to that ratio (centered, shrunk to fit within its
        own current bounding box) and arms ImageLabel's corner-drag constraint to keep
        matching it afterward.
        """
        w, h = self.raw.sizes.width, self.raw.sizes.height
        if ratio is None:
            self.image_label.crop_aspect = None
            self._update_crop_overlay()
            self._schedule_update()
            return

        frac_ratio = ratio * h / w  # target (right-left)/(bottom-top) in fraction space
        self.image_label.crop_aspect = frac_ratio
        c = self.crop
        cx, cy = (c["left"] + c["right"]) / 2.0, (c["top"] + c["bottom"]) / 2.0
        cur_w, cur_h = c["right"] - c["left"], c["bottom"] - c["top"]
        if cur_w / frac_ratio <= cur_h:
            new_w, new_h = cur_w, cur_w / frac_ratio
        else:
            new_h, new_w = cur_h, cur_h * frac_ratio
        new_w, new_h = min(new_w, 1.0), min(new_h, 1.0)
        left = _clamp(cx - new_w / 2.0, 0.0, 1.0 - new_w)
        top = _clamp(cy - new_h / 2.0, 0.0, 1.0 - new_h)
        c["left"], c["right"] = left, left + new_w
        c["top"], c["bottom"] = top, top + new_h
        self._update_crop_overlay()
        self._schedule_update()

    def _on_crop_rotation_changed(self):
        if self.crop is None or self.raw is None:
            return
        self.crop["angle"] = self.crop_rotation_slider.value()
        self.crop = clamp_crop_to_rotation(self.crop, self.raw.sizes.width, self.raw.sizes.height)
        if self.image_label.crop_aspect:
            # clamping to the rotated bounds can shrink one axis more than the other and
            # break the active ratio — reshape back to it within the now-smaller bounds.
            ratio_label = self.crop_aspect_combo.currentText()
            ratio = CROP_ASPECT_RATIOS.get(ratio_label)
            if ratio == "original":
                ratio = self.raw.sizes.width / self.raw.sizes.height
            if ratio:
                self._apply_crop_aspect_ratio(ratio)
                return  # already schedules update / overlay refresh
        self._update_crop_overlay()
        self._schedule_update()

    def _on_crop_dragged(self, geom):
        if self.crop is None:
            return
        self.crop.update({"left": geom["left"], "top": geom["top"],
                           "right": geom["right"], "bottom": geom["bottom"]})
        self._schedule_update()

    def _update_crop_overlay(self):
        if self.crop is not None and self._crop_editing:
            self.image_label.overlay_crop = {
                "left": self.crop["left"], "top": self.crop["top"],
                "right": self.crop["right"], "bottom": self.crop["bottom"],
            }
        else:
            self.image_label.overlay_crop = None
        self.image_label.update()

    def _restore_crop_ui(self):
        """Sync the Crop panel's buttons/slider to self.crop right after load_dng()
        restores it from XMP. Leaves _crop_editing False and crop_enable_btn unchecked —
        reopening a file shows the actual committed crop, same as save_to_dng() would
        produce, not an editing guide the user never asked to see (matches how
        radial/gradient filters restore unselected rather than mid-edit). The button no
        longer means "does a crop exist" (it means "am I editing it right now"), so it
        stays unchecked here even when has_crop is True — checking it afterward enters
        editing mode showing this *same* restored rectangle, not a fresh one.
        """
        has_crop = self.crop is not None
        self._crop_editing = False
        self.crop_enable_btn.blockSignals(True)
        self.crop_enable_btn.setChecked(False)
        self.crop_enable_btn.blockSignals(False)
        self.crop_controls.setVisible(has_crop)
        self.crop_reset_btn.setVisible(has_crop)
        # Aspect preset isn't persisted, only the resulting geometry — always comes back
        # as Freeform (the ratio the saved rectangle happens to have isn't tracked).
        self.crop_aspect_combo.blockSignals(True)
        self.crop_aspect_combo.setCurrentIndex(0)
        self.crop_aspect_combo.blockSignals(False)
        self.image_label.crop_aspect = None
        if has_crop:
            self.crop_rotation_slider.set_value(self.crop["angle"])
        self._update_crop_overlay()

    def _set_tone_controls_enabled(self, enabled):
        for s in (self.exposure_slider, self.contrast_slider, self.highlights_slider,
                  self.shadows_slider, self.whites_slider, self.blacks_slider):
            s.setEnabled(enabled)

    def _on_tone_mode_changed(self):
        mode = self.tone_mode.currentText()
        if mode == "Default":
            self.exposure_slider.set_value(0)
            self.contrast_slider.set_value(0)
            self.highlights_slider.set_value(0)
            self.shadows_slider.set_value(0)
            self.whites_slider.set_value(0)
            self.blacks_slider.set_value(0)
        self._set_tone_controls_enabled(mode != "Auto")
        self._schedule_update()

    def _on_tone_slider_changed(self):
        if self.tone_mode.currentText() == "Default":
            self.tone_mode.blockSignals(True)
            self.tone_mode.setCurrentText("Custom")
            self.tone_mode.blockSignals(False)
        self._schedule_update()

    def _on_curve_mode_changed(self):
        is_custom = self.curve_combo.currentText() == "Custom"
        self.curve_editor.setVisible(is_custom)
        self.curve_reset_btn.setVisible(is_custom)
        self._schedule_update()

    def _current_curve_points(self):
        if self.curve_combo.currentText() == "Custom":
            return self.curve_editor.get_points()
        return CURVE_PRESETS[self.curve_combo.currentText()]

    # ---- File I/O ----

    def open_dng(self):
        path, _ = QFileDialog.getOpenFileName(self, "Apri DNG", "", "DNG files (*.dng);;Tutti i file (*.*)")
        if path:
            self.load_dng(path)

    def load_dng(self, path):
        if self.raw is not None:
            self.raw.close()

        self.statusBar().showMessage(f"Apertura di {os.path.basename(path)}…")
        QApplication.processEvents()

        t0 = time.time()
        self.raw = rawpy.imread(path)
        self.raw_path = path
        self._linear_cache = None
        self._linear_cache_key = None
        self._pre_local_cache = None
        self._pre_local_cache_key = None
        self._undo_stack = []
        self._redo_stack = []
        self._current_edit_state = None
        self._set_zoom_level(None)  # back to Fit for a newly opened file
        self._reset_dngconv_pipeline(path)

        wb = list(self.raw.camera_whitebalance)
        if not wb or wb[0] <= 0:
            wb = list(self.raw.daylight_whitebalance)
        self.camera_wb = wb
        self._auto_wb = None
        combined_tags = read_combined_edit_tags(self.exiftool_path, path)
        self.camera_profile = load_dng_camera_profile(self.exiftool_path, path, tags=combined_tags)

        self.setWindowTitle(f"DNGForge — {os.path.basename(path)}")
        self.reset_controls()
        xmp_state = read_xmp_crs_state(self.exiftool_path, path, tags=combined_tags)
        if xmp_state:
            self._apply_xmp_crs_state(xmp_state)
        self.radial_filters = read_radial_filters(self.exiftool_path, path, tags=combined_tags)
        self._select_radial_filter(-1)  # none active-selected; restored ellipses still drawn
        self.gradient_filters = read_gradient_filters(self.exiftool_path, path, tags=combined_tags)
        self._select_gradient_filter(-1)  # none active-selected; restored lines still drawn
        self.spots = read_spot_removals(self.exiftool_path, path, tags=combined_tags)
        self._select_spot(-1)  # none active-selected; restored spots still drawn
        self.crop = read_crop(self.exiftool_path, path, tags=combined_tags)
        self._restore_crop_ui()
        self._update_metadata_panel(path)
        self._render_preview()
        colorimetry = "colorimetria DNG attiva" if self.camera_profile else "colorimetria approssimata (ColorMatrix non disponibile)"
        restored_bits = []
        if xmp_state:
            restored_bits.append("editing")
        if self.radial_filters:
            restored_bits.append(f"{len(self.radial_filters)} filtro/i radiale/i")
        if self.gradient_filters:
            restored_bits.append(f"{len(self.gradient_filters)} filtro/i graduato/i")
        if self.spots:
            restored_bits.append(f"{len(self.spots)} macchia/e rimossa/e")
        if self.crop:
            restored_bits.append("crop")
        edit_state = f" — {' e '.join(restored_bits)} ripristinato/i" if restored_bits else ""
        self.statusBar().showMessage(
            f"{os.path.basename(path)} — {self.raw.sizes.width}x{self.raw.sizes.height} "
            f"(caricato in {time.time() - t0:.2f}s, {colorimetry}){edit_state}"
        )

    def _update_metadata_panel(self, path):
        values = read_metadata(self.exiftool_path, path, self.raw.sizes.width, self.raw.sizes.height)
        for field, label in self.metadata_labels.items():
            label.setText(values.get(field, "—"))

    def reset_controls(self):
        self.profile_combo.setCurrentIndex(0)  # Adobe Color
        self.wb_mode.blockSignals(True)
        self.wb_mode.setCurrentIndex(0)  # As Shot
        self.wb_mode.blockSignals(False)
        self._on_wb_mode_changed()  # populate Temperature/Tint display for As Shot
        self.tone_mode.setCurrentText("Default")
        self.exposure_slider.set_value(0)
        self.contrast_slider.set_value(0)
        self.highlights_slider.set_value(0)
        self.shadows_slider.set_value(0)
        self.whites_slider.set_value(0)
        self.blacks_slider.set_value(0)
        self.texture_slider.set_value(0)
        self.clarity_slider.set_value(0)
        self.dehaze_slider.set_value(0)
        self.vibrance_slider.set_value(0)
        self.saturation_slider.set_value(0)
        self.curve_combo.setCurrentText("Linear")
        self.curve_editor.reset()
        self.sharpness_slider.set_value(0)
        self.luminance_nr_slider.set_value(0)
        self.color_nr_slider.set_value(0)
        self.radial_filters = []
        self._select_radial_filter(-1)
        self.gradient_filters = []
        self._select_gradient_filter(-1)
        self.spots = []
        self._select_spot(-1)
        self.crop = None
        self._crop_editing = False
        self.crop_enable_btn.setChecked(False)
        self.crop_controls.setVisible(False)
        self.crop_reset_btn.setVisible(False)
        self.crop_aspect_combo.blockSignals(True)
        self.crop_aspect_combo.setCurrentIndex(0)
        self.crop_aspect_combo.blockSignals(False)
        self.image_label.crop_aspect = None
        self._update_crop_overlay()
        self._set_wb_controls_enabled(False)
        self._set_tone_controls_enabled(True)
        self._schedule_update()

    def _apply_xmp_crs_state(self, state):
        """Repopulate controls from read_xmp_crs_state(), on top of the Default
        state reset_controls() just set — mirrors _build_xmp_crs_args()'s write
        logic field for field so a save/reload round-trips through this app.

        Radial/gradient filters are restored separately (read_radial_filters(),
        read_gradient_filters(), called directly from load_dng()) since they
        live in their own structured XMP tags, not the flat -XMP-crs:* ones
        this function mirrors.
        """
        profile = state.get("CameraProfile")
        if profile and self.profile_combo.findText(str(profile)) >= 0:
            self.profile_combo.setCurrentText(str(profile))
        elif profile is None:
            # _build_xmp_crs_args() only omits CameraProfile when "Custom" was
            # selected at save time (Custom isn't a real Adobe profile name).
            self.profile_combo.setCurrentText("Custom")

        wb_mode = state.get("WhiteBalance")
        if wb_mode and self.wb_mode.findText(str(wb_mode)) >= 0:
            self.wb_mode.setCurrentText(str(wb_mode))
            if wb_mode in WB_PRESETS or wb_mode == "Custom":
                temp, tint = state.get("ColorTemperature"), state.get("Tint")
                if temp is not None:
                    self.temp_slider.set_value(float(temp))
                if tint is not None:
                    self.tint_slider.set_value(float(tint))

        if state.get("AutoTone"):
            self.tone_mode.setCurrentText("Auto")
        else:
            tone_values = {
                self.exposure_slider: state.get("Exposure2012"),
                self.contrast_slider: state.get("Contrast2012"),
                self.highlights_slider: state.get("Highlights2012"),
                self.shadows_slider: state.get("Shadows2012"),
                self.whites_slider: state.get("Whites2012"),
                self.blacks_slider: state.get("Blacks2012"),
            }
            if any(v not in (None, 0, 0.0) for v in tone_values.values()):
                self.tone_mode.setCurrentText("Custom")
            for slider, value in tone_values.items():
                if value is not None:
                    slider.set_value(float(value))

        for slider, key in (
            (self.texture_slider, "Texture"), (self.clarity_slider, "Clarity2012"),
            (self.dehaze_slider, "Dehaze"), (self.vibrance_slider, "Vibrance"),
            (self.saturation_slider, "Saturation"), (self.sharpness_slider, "Sharpness"),
            (self.luminance_nr_slider, "LuminanceSmoothing"),
            (self.color_nr_slider, "ColorNoiseReduction"),
        ):
            value = state.get(key)
            if value is not None:
                slider.set_value(float(value))

        curve_name = state.get("ToneCurveName2012")
        if curve_name and curve_name in CURVE_PRESETS:
            self.curve_combo.setCurrentText(curve_name)
            if curve_name == "Custom":
                raw_points = state.get("ToneCurvePV2012")
                if raw_points:
                    try:
                        points = [tuple(float(v) for v in pair.split(",")) for pair in str(raw_points).split(";")]
                        self.curve_editor.set_points(points)
                    except ValueError:
                        pass  # malformed/foreign ToneCurvePV2012 — leave identity curve

        self._schedule_update()

    # ---- Rendering ----

    def _schedule_update(self):
        self._update_timer.start(60)

    def _wb_multipliers(self, base_rgb_for_auto=None):
        mode = self.wb_mode.currentText()
        if mode == "As Shot":
            return self.camera_wb
        if mode == "Camera Matching…":
            # Approximation: true camera-matching reads the EXIF WhiteBalance
            # setting name via exiftool (not wired in yet, see CLAUDE.md phase 5).
            return self.camera_wb
        if mode == "Auto":
            if self._auto_wb is None:
                self._auto_wb = self._compute_auto_wb()
            return self._auto_wb
        temp, tint = self.temp_slider.value(), self.tint_slider.value()
        if self.camera_profile is not None:
            return self.camera_profile.temperature_tint_to_multipliers(temp, tint)
        return kelvin_to_multipliers(temp, tint)

    def _compute_auto_wb(self):
        neutral = compute_gray_world_neutral(self.raw_path)
        r, g, b = neutral
        return [1.0 / r if r else 1.0, 1.0, 1.0 / b if b else 1.0, 1.0]

    def _multipliers_to_temp_tint(self, mult):
        """Inverse of the multiplier calc above, for display on the (disabled) sliders.
        Prefers the real DNGCameraProfile when available, same as the forward direction.
        """
        r, g, b = mult[0], mult[1] or 1.0, mult[2]
        if self.camera_profile is not None:
            neutral = [1.0 / r if r else 1.0, 1.0 / g, 1.0 / b if b else 1.0]
            try:
                return self.camera_profile.neutral_to_temperature_tint(neutral)
            except Exception:
                pass  # fall through to the approximation below
        return multipliers_to_kelvin_tint(r, g, b)

    def _render(self, half_size: bool, for_save: bool = False) -> np.ndarray:
        """Core edit pipeline, shared by the live preview and the full-res save render.

        Decodes to scene-linear light (gamma=(1,1)) rather than straight to
        8-bit sRGB, so Exposure can be applied as a true stop multiplication
        in linear space before converting to display-referred sRGB — see
        apply_exposure_and_gamma()'s docstring for why only exposure needs
        this and not Contrast/Highlights/Shadows/etc.

        `raw.postprocess()` (the full demosaic) is by far the most expensive step here —
        and its output only depends on `half_size` and the white-balance multipliers, not
        on any of the Tone/Presence/Curve/Detail/local-filter sliders below. Every other
        adjustment used to still trigger a fresh postprocess() call on every single slider
        tweak (the 60ms debounce only throttled *how often*, not *how much work* each
        render did), which is what made the app feel sluggish on larger files. Cached here,
        keyed on the only two things that actually change its output — see CLAUDE.md
        "Performance: caching the demosaic step" for the measured before/after.
        """
        wb = self._wb_multipliers()
        cache_key = (half_size, tuple(wb))
        if self._linear_cache is not None and self._linear_cache_key == cache_key:
            rgb_linear = self._linear_cache
        else:
            rgb_linear = self.raw.postprocess(
                half_size=half_size,
                use_camera_wb=False,
                user_wb=wb,
                no_auto_bright=True,
                output_bps=16,
                gamma=(1, 1),
                output_color=rawpy.ColorSpace.sRGB,
            )
            if half_size and self.PREVIEW_MAX_DIM:
                h, w = rgb_linear.shape[:2]
                scale = self.PREVIEW_MAX_DIM / max(w, h)
                if scale < 1.0:
                    rgb_linear = cv2.resize(rgb_linear, (max(1, int(w * scale)), max(1, int(h * scale))),
                                             interpolation=cv2.INTER_AREA)
            self._linear_cache = rgb_linear
            self._linear_cache_key = cache_key

        # Second cache tier: everything from here through Sharpness only depends on the
        # global adjustment controls (Profile/Tone/Curve/Presence/Detail), never on the
        # radial/gradient filters' or crop's own geometry. Dragging an ellipse, gradient
        # line, or crop handle changes none of that — it used to still re-run the entire
        # tone/curve/presence/detail chain on every dragged pixel of mouse movement, which
        # is what made those drags feel sluggish even after the demosaic cache above.
        # `cache_key` (already unique per half_size + WB) is folded in here too so this
        # cache can never be reused against a stale/different rgb_linear.
        global_key = (cache_key, self._global_adjustments_key())
        if self._pre_local_cache is not None and self._pre_local_cache_key == global_key:
            rgb = self._pre_local_cache
        else:
            is_auto_tone = self.tone_mode.currentText() == "Auto"
            exposure_ev = compute_auto_exposure_linear(rgb_linear) if is_auto_tone else self.exposure_slider.value()
            rgb = apply_exposure_and_gamma(rgb_linear, exposure_ev)

            rgb = apply_profile(rgb, self.profile_combo.currentText())

            if is_auto_tone:
                rgb = apply_tone(rgb, compute_auto_contrast(rgb), 0, 0, 0, 0)
            else:
                rgb = apply_tone(
                    rgb,
                    contrast=self.contrast_slider.value(),
                    shadows=self.shadows_slider.value(),
                    highlights=self.highlights_slider.value(),
                    whites=self.whites_slider.value(),
                    blacks=self.blacks_slider.value(),
                )

            rgb = apply_curve(rgb, self._current_curve_points())

            rgb = apply_texture(rgb, self.texture_slider.value())
            rgb = apply_clarity(rgb, self.clarity_slider.value())
            rgb = apply_dehaze(rgb, self.dehaze_slider.value())
            rgb = apply_vibrance(rgb, self.vibrance_slider.value())
            rgb = apply_saturation(rgb, self.saturation_slider.value())

            rgb = apply_luminance_nr(rgb, self.luminance_nr_slider.value())
            rgb = apply_color_nr(rgb, self.color_nr_slider.value())
            rgb = apply_sharpness(rgb, self.sharpness_slider.value())

            self._pre_local_cache = rgb
            self._pre_local_cache_key = global_key

        # Spot Removal goes first among the local tools — it's a fix to the underlying
        # image (dust, blemishes), so radial/gradient filters' creative grading applies on
        # top of the already-healed result, not the other way around.
        rgb = apply_spot_removals(rgb, self.spots)
        rgb = apply_radial_filters(rgb, self.radial_filters)
        rgb = apply_gradient_filters(rgb, self.gradient_filters)
        rgb = apply_crop_straighten(rgb, self._effective_crop_for_render(for_save))

        return rgb

    def _global_adjustments_key(self):
        """Snapshot of every control that feeds the pre-local-filter part of _render() —
        used to validate self._pre_local_cache. Deliberately excludes radial_filters/
        gradient_filters/crop (those are applied *after* the cached stage, always fresh).
        """
        return (
            self.profile_combo.currentText(),
            self.tone_mode.currentText(),
            self.exposure_slider.value(), self.contrast_slider.value(),
            self.highlights_slider.value(), self.shadows_slider.value(),
            self.whites_slider.value(), self.blacks_slider.value(),
            self.curve_combo.currentText(), tuple(self._current_curve_points() or ()),
            self.texture_slider.value(), self.clarity_slider.value(), self.dehaze_slider.value(),
            self.vibrance_slider.value(), self.saturation_slider.value(),
            self.sharpness_slider.value(), self.luminance_nr_slider.value(),
            self.color_nr_slider.value(),
        )

    def _effective_crop_for_render(self, for_save: bool = False):
        """While the crop guide is being edited, the live preview shows the *full*
        straightened frame (so the user can see the whole rotated image behind the crop
        rectangle overlay) rather than the actual cropped result — same "guide vs
        committed" distinction Lightroom's own Crop tool uses. Saving always applies the
        real crop regardless of whether the guide happens to be showing at the time.
        """
        if not self.crop:
            return None
        if self._crop_editing and not for_save:
            return {"angle": self.crop["angle"], "left": 0.0, "top": 0.0, "right": 1.0, "bottom": 1.0}
        return self.crop

    def _render_preview(self):
        """Renders and displays the fast cv2 approximation — but only as a *bootstrap*,
        shown while no Adobe-rendered preview exists yet for this file (right after
        opening it, or when Adobe DNG Converter isn't installed/available at all). Once
        the first real Adobe render lands (_dngconv_bootstrapped becomes True), this stops
        touching the display entirely: the user explicitly asked not to see the cv2
        approximation flash in on every settle ("non mi piace che quando muovo lo slider
        l'anteprima diventa quella slavata... meglio lasciare sempre quella di
        adobedngconverter") — so from then on the *previous* Adobe-rendered image just
        stays on screen, unchanged, until _on_dngconv_render_ready() replaces it with the
        next one. No intermediate "downgrade" is ever shown again for that file.
        """
        if self.raw is None:
            return
        self._maybe_push_undo_state()
        if not self._dngconv_path or not self._dngconv_bootstrapped:
            rgb = self._render(half_size=self.PREVIEW_HALF_SIZE)
            self._preview_pixmap = numpy_to_pixmap(rgb)
            self._update_image_label()
        self._schedule_dngconv_render()

    # ---- Background Adobe-render preview pipeline ----

    def _reset_dngconv_pipeline(self, path):
        """Called from load_dng(): invalidates any in-flight background render (via the
        generation counter — safe even if a thread from the *previous* file is still
        running, its eventual result just gets ignored) and kicks off building a fresh
        small working copy for the newly opened file. No-ops entirely if Adobe DNG
        Converter isn't installed.
        """
        self._dngconv_generation += 1
        self._work_dng_path = None
        self._last_dngconv_state = None
        self._dngconv_pending = False
        self._dngconv_bootstrapped = False
        if self._work_dng_dir is not None:
            self._old_work_dirs.append(self._work_dng_dir)
            self._work_dng_dir = None
        if not self._dngconv_path:
            return
        work_dir = tempfile.mkdtemp(prefix="dngforge_work_")
        self._work_dng_dir = work_dir
        self._workcopy_thread = DngConverterWorkCopyThread(self._dngconv_path, path, work_dir, self)
        self._workcopy_thread.ready.connect(self._on_workcopy_ready)
        self._workcopy_thread.failed.connect(self._on_workcopy_failed)
        self._workcopy_thread.start()

    def _on_workcopy_ready(self, work_dng_path):
        self._workcopy_thread = None
        self._work_dng_path = work_dng_path
        self._schedule_dngconv_render()  # the file may already have settled edits by now

    def _on_workcopy_failed(self):
        self._workcopy_thread = None  # Adobe-render pipeline just stays inactive for this file

    def _schedule_dngconv_render(self):
        """Kicks off (or queues) a background Adobe re-render if the edit state has
        actually changed since the last one — called after every fast cv2 preview render,
        but this is cheap to call redundantly (a no-op comparison) since the real work
        only starts when something's different.
        """
        if not self._dngconv_path or not self._work_dng_path or self.raw is None:
            return
        state = self._capture_edit_state()
        if state == self._last_dngconv_state:
            return
        self._last_dngconv_state = state
        if self._dngconv_thread is not None:
            # One in flight already — remember to re-render with the *latest* state once
            # it finishes, rather than piling up multiple Adobe DNG Converter processes.
            self._dngconv_pending = True
            return
        self._start_dngconv_render()

    def _start_dngconv_render(self):
        self._dngconv_pending = False
        self._dngconv_generation += 1
        generation = self._dngconv_generation
        xmp_args = self._build_all_edit_xmp_args()  # must happen on the main thread — see
        # DngConverterRenderThread's own docstring for why
        self._dngconv_thread = DngConverterRenderThread(
            self._work_dng_path, xmp_args, self.exiftool_path, self._dngconv_path, generation, self
        )
        self._dngconv_thread.ready.connect(self._on_dngconv_render_ready)
        self._dngconv_thread.failed.connect(self._on_dngconv_render_failed)
        self._dngconv_thread.start()
        # The user asked to never see the cv2 approximation again once an Adobe preview
        # has landed — so while this runs (several seconds), the image on screen simply
        # doesn't change. A status bar note is the only feedback that anything is
        # happening, deliberately not a new preview flash.
        self.statusBar().showMessage("Rendering anteprima Adobe DNG Converter…")

    def _on_dngconv_render_ready(self, jpeg_path, generation):
        try:
            if generation == self._dngconv_generation and self.raw is not None:
                pixmap = QPixmap(jpeg_path)
                if not pixmap.isNull():
                    self._preview_pixmap = pixmap
                    self._update_image_label()
                    self._dngconv_bootstrapped = True
                    self.statusBar().showMessage("Anteprima Adobe DNG Converter aggiornata", 3000)
        finally:
            if os.path.exists(jpeg_path):
                try:
                    os.unlink(jpeg_path)
                except OSError:
                    pass
            self._dngconv_thread = None
            if self._dngconv_pending:
                self._start_dngconv_render()

    def _on_dngconv_render_failed(self, generation):
        self._dngconv_thread = None
        if self._dngconv_pending:
            self._start_dngconv_render()
        elif generation == self._dngconv_generation:
            # Whatever was on screen before (cv2 bootstrap, or the last successful Adobe
            # render) is left untouched — a failed background render is not a reason to
            # replace a valid image with nothing, or with the disliked cv2 approximation.
            self.statusBar().showMessage("Rendering Adobe DNG Converter non riuscito — anteprima invariata", 4000)

    # ---- Undo/Redo ----
    #
    # Snapshots are taken on the *settled* state — i.e. right here in _render_preview(),
    # which the 60ms debounce timer already only calls once dragging pauses — rather than
    # hooking every individual slider/combo/checkbox's own change signal. A slider fires
    # valueChanged continuously while being dragged; snapshotting each of those would fill
    # the bounded stack with near-duplicate in-drag states instead of the "a few meaningful
    # steps back" the user actually asked for. Reusing the existing debounce timer this way
    # needed no new signal wiring anywhere else in the app.

    def _capture_edit_state(self) -> dict:
        """Full in-memory snapshot of every editable control. Distinct from the XMP
        round-trip (_build_xmp_crs_args()/_apply_xmp_crs_state()): this stays entirely in
        memory, no exiftool involved, so it's cheap enough to take on every settled change.
        """
        return {
            "profile": self.profile_combo.currentText(),
            "wb_mode": self.wb_mode.currentText(),
            "temp": self.temp_slider.value(), "tint": self.tint_slider.value(),
            "tone_mode": self.tone_mode.currentText(),
            "exposure": self.exposure_slider.value(), "contrast": self.contrast_slider.value(),
            "highlights": self.highlights_slider.value(), "shadows": self.shadows_slider.value(),
            "whites": self.whites_slider.value(), "blacks": self.blacks_slider.value(),
            "texture": self.texture_slider.value(), "clarity": self.clarity_slider.value(),
            "dehaze": self.dehaze_slider.value(), "vibrance": self.vibrance_slider.value(),
            "saturation": self.saturation_slider.value(),
            "curve_mode": self.curve_combo.currentText(),
            "curve_points": list(self.curve_editor.get_points()),
            "sharpness": self.sharpness_slider.value(),
            "luminance_nr": self.luminance_nr_slider.value(),
            "color_nr": self.color_nr_slider.value(),
            "radial_filters": copy.deepcopy(self.radial_filters),
            "gradient_filters": copy.deepcopy(self.gradient_filters),
            "spots": copy.deepcopy(self.spots),
            "crop": copy.deepcopy(self.crop),
        }

    def _apply_edit_state(self, state: dict):
        """Restores every control from a _capture_edit_state() snapshot — the Undo/Redo
        counterpart to _apply_xmp_crs_state(), working from an in-memory dict instead of
        exiftool tags, and additionally covering radial/gradient filters + crop (which
        round-trip through XMP separately on save, but are just part of the same snapshot
        here, since Undo needs to cover everything, not only what gets persisted).
        """
        self._restoring_state = True
        try:
            self.profile_combo.setCurrentText(state["profile"])

            self.wb_mode.blockSignals(True)
            self.wb_mode.setCurrentText(state["wb_mode"])
            self.wb_mode.blockSignals(False)
            self.temp_slider.set_value(state["temp"])
            self.tint_slider.set_value(state["tint"])
            self._set_wb_controls_enabled(state["wb_mode"] in WB_PRESETS or state["wb_mode"] == "Custom")

            self.tone_mode.blockSignals(True)
            self.tone_mode.setCurrentText(state["tone_mode"])
            self.tone_mode.blockSignals(False)
            self.exposure_slider.set_value(state["exposure"])
            self.contrast_slider.set_value(state["contrast"])
            self.highlights_slider.set_value(state["highlights"])
            self.shadows_slider.set_value(state["shadows"])
            self.whites_slider.set_value(state["whites"])
            self.blacks_slider.set_value(state["blacks"])
            self._set_tone_controls_enabled(state["tone_mode"] != "Auto")

            self.texture_slider.set_value(state["texture"])
            self.clarity_slider.set_value(state["clarity"])
            self.dehaze_slider.set_value(state["dehaze"])
            self.vibrance_slider.set_value(state["vibrance"])
            self.saturation_slider.set_value(state["saturation"])

            self.curve_combo.blockSignals(True)
            self.curve_combo.setCurrentText(state["curve_mode"])
            self.curve_combo.blockSignals(False)
            self.curve_editor.set_points(state["curve_points"])
            is_custom_curve = state["curve_mode"] == "Custom"
            self.curve_editor.setVisible(is_custom_curve)
            self.curve_reset_btn.setVisible(is_custom_curve)

            self.sharpness_slider.set_value(state["sharpness"])
            self.luminance_nr_slider.set_value(state["luminance_nr"])
            self.color_nr_slider.set_value(state["color_nr"])

            self.radial_filters = copy.deepcopy(state["radial_filters"])
            self._select_radial_filter(-1)
            self.gradient_filters = copy.deepcopy(state["gradient_filters"])
            self._select_gradient_filter(-1)
            self.spots = copy.deepcopy(state.get("spots", []))
            self._select_spot(-1)

            self.crop = copy.deepcopy(state["crop"])
            self._crop_editing = False
            has_crop = self.crop is not None
            self.crop_enable_btn.blockSignals(True)
            self.crop_enable_btn.setChecked(False)
            self.crop_enable_btn.blockSignals(False)
            self.crop_controls.setVisible(has_crop)
            self.crop_reset_btn.setVisible(has_crop)
            if has_crop:
                self.crop_rotation_slider.set_value(self.crop["angle"])
            self._update_crop_overlay()
        finally:
            self._restoring_state = False

        self._schedule_update()

    def _maybe_push_undo_state(self):
        if self._restoring_state:
            return  # don't record undo()/redo() applying a snapshot as a new change
        new_state = self._capture_edit_state()
        if self._current_edit_state is not None and new_state == self._current_edit_state:
            return  # nothing actually changed since the last settled snapshot
        if self._current_edit_state is not None:
            self._undo_stack.append(self._current_edit_state)
            if len(self._undo_stack) > self.UNDO_STACK_MAX:
                self._undo_stack.pop(0)
            self._redo_stack.clear()
        self._current_edit_state = new_state

    def undo(self):
        if not self._undo_stack:
            self.statusBar().showMessage("Niente da annullare")
            return
        self._redo_stack.append(self._current_edit_state)
        prev_state = self._undo_stack.pop()
        self._apply_edit_state(prev_state)
        self._current_edit_state = prev_state
        self.statusBar().showMessage(f"Annullato ({len(self._undo_stack)} passo/i ancora disponibile/i)")

    def redo(self):
        if not self._redo_stack:
            self.statusBar().showMessage("Niente da ripetere")
            return
        self._undo_stack.append(self._current_edit_state)
        if len(self._undo_stack) > self.UNDO_STACK_MAX:
            self._undo_stack.pop(0)
        next_state = self._redo_stack.pop()
        self._apply_edit_state(next_state)
        self._current_edit_state = next_state
        self.statusBar().showMessage(f"Ripetuto ({len(self._redo_stack)} passo/i ancora disponibile/i)")

    def revert_to_saved(self):
        """Discards every in-session change (including the undo/redo history — a fresh
        load_dng() resets it, see there) and reloads the file exactly as it was last
        saved. Simpler and cheaper than undo: no snapshot needed, the file itself already
        holds that state."""
        if self.raw_path is None:
            return
        reply = QMessageBox.question(
            self, "Revert to Last Save",
            "Scartare tutte le modifiche non salvate e ricaricare il file dall'ultimo salvataggio?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self.load_dng(self.raw_path)

    # ---- Save (embedded, no sidecar — see CLAUDE.md phase 5) ----

    def _build_xmp_crs_args(self) -> list[str]:
        """-XMP-crs:Tag=value exiftool args recording the current edit state.

        Adobe Camera Raw tag names/conventions (matches RethinkRAW's own
        xmpSettings, see project memory dngforge-rethinkraw-research) so a
        DNG saved here round-trips through our own reload logic and remains
        at least readable (if not identically rendered — Profile/Curve are
        our own approximations, not real Adobe transforms) by other tools.
        """
        args = ["-XMP-crs:ProcessVersion=11.0"]

        # Crop/Straighten — flat tags, real Adobe schema (verified against a genuine
        # Lightroom-saved crop in DSC_6751.dng: HasCrop/CropTop/CropLeft/CropBottom/
        # CropRight/CropAngle, same fraction convention this app already uses internally,
        # so no unit conversion is needed). Always written, even when there's no crop, so
        # a crop from a *previous* save gets properly cleared rather than left stranded.
        if self.crop:
            args += [
                "-XMP-crs:HasCrop=True",
                f"-XMP-crs:CropTop={self.crop['top']:.6f}",
                f"-XMP-crs:CropLeft={self.crop['left']:.6f}",
                f"-XMP-crs:CropBottom={self.crop['bottom']:.6f}",
                f"-XMP-crs:CropRight={self.crop['right']:.6f}",
                f"-XMP-crs:CropAngle={self.crop['angle']:.4f}",
            ]
        else:
            args.append("-XMP-crs:HasCrop=False")

        profile = self.profile_combo.currentText()
        if profile != "Custom":
            args.append(f"-XMP-crs:CameraProfile={profile}")

        wb_mode = self.wb_mode.currentText()
        # "Camera Matching…" has no distinct crs concept; record it as Custom.
        args.append(f"-XMP-crs:WhiteBalance={'Custom' if wb_mode == 'Camera Matching…' else wb_mode}")
        if wb_mode in WB_PRESETS or wb_mode == "Custom":
            args.append(f"-XMP-crs:ColorTemperature={self.temp_slider.value():.0f}")
            args.append(f"-XMP-crs:Tint={self.tint_slider.value():.0f}")

        if self.tone_mode.currentText() == "Auto":
            args.append("-XMP-crs:AutoTone=True")
        else:
            args += [
                f"-XMP-crs:Exposure2012={self.exposure_slider.value():.2f}",
                f"-XMP-crs:Contrast2012={self.contrast_slider.value():.0f}",
                f"-XMP-crs:Highlights2012={self.highlights_slider.value():.0f}",
                f"-XMP-crs:Shadows2012={self.shadows_slider.value():.0f}",
                f"-XMP-crs:Whites2012={self.whites_slider.value():.0f}",
                f"-XMP-crs:Blacks2012={self.blacks_slider.value():.0f}",
            ]

        args += [
            f"-XMP-crs:Texture={self.texture_slider.value():.0f}",
            f"-XMP-crs:Clarity2012={self.clarity_slider.value():.0f}",
            f"-XMP-crs:Dehaze={self.dehaze_slider.value():.0f}",
            f"-XMP-crs:Vibrance={self.vibrance_slider.value():.0f}",
            f"-XMP-crs:Saturation={self.saturation_slider.value():.0f}",
            f"-XMP-crs:ToneCurveName2012={self.curve_combo.currentText()}",
        ]
        if self.curve_combo.currentText() == "Custom":
            points = "; ".join(f"{x:.0f}, {y:.0f}" for x, y in self.curve_editor.get_points())
            args.append(f"-XMP-crs:ToneCurvePV2012={points}")

        args += [
            f"-XMP-crs:Sharpness={self.sharpness_slider.value():.0f}",
            f"-XMP-crs:LuminanceSmoothing={self.luminance_nr_slider.value():.0f}",
            f"-XMP-crs:ColorNoiseReduction={self.color_nr_slider.value():.0f}",
        ]
        return args

    def _build_all_edit_xmp_args(self) -> list[str]:
        """_build_xmp_crs_args() plus the three structured local-adjustment tags
        (radial/gradient/spot) — the complete edit-state write, shared by save_to_dng()
        and the background Adobe-render pipeline (DngConverterRenderThread) so the two
        can't drift apart into writing different tag sets for "the same" edit state.
        """
        args = self._build_xmp_crs_args()
        args.append("-XMP-crs:CircularGradientBasedCorrections=" + build_radial_xmp_value(self.radial_filters))
        args.append("-XMP-crs:GradientBasedCorrections=" + build_gradient_xmp_value(self.gradient_filters))
        args.append("-XMP-crs:RetouchAreas=" + build_spot_xmp_value(self.spots))
        return args

    def save_to_dng(self):
        """Render full resolution and embed it + the edit state into the DNG in place.

        Always embeds — never falls back to an external .xmp sidecar, even
        on a pristine never-edited DNG (that's RethinkRAW's default-Save
        behavior, deliberately not replicated here, see CLAUDE.md phase 5
        and project memory dngforge-rethinkraw-research for why).
        """
        if self.raw is None or self.raw_path is None:
            return
        if not self.exiftool_path:
            QMessageBox.critical(self, "Salvataggio non riuscito",
                                  "exiftool non trovato: impossibile scrivere le modifiche nel DNG.")
            return

        self.statusBar().showMessage("Rendering a piena risoluzione…")
        QApplication.processEvents()
        t0 = time.time()
        path = self.raw_path
        tmp_jpeg = None

        try:
            rgb = self._render(half_size=False, for_save=True)
            bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
            ok, buf = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 69])
            if not ok:
                raise RuntimeError("Codifica JPEG fallita")

            tmp_jpeg = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
            tmp_jpeg.write(buf.tobytes())
            tmp_jpeg.close()

            # release our read handle before exiftool rewrites the file in
            # place — on Windows a rename-over-open-file fails silently
            # otherwise (the exact bug we found and root-caused in RethinkRAW)
            self.raw.close()
            self.raw = None

            args = [
                "-PreviewImage<=" + tmp_jpeg.name,
                # Some DNGs (notably ones run through Adobe DNG Converter's -lossy mode,
                # like this project's own test file) also embed a *separate* full-res JPEG
                # under the distinct "JpgFromRaw" tag (SubIFD2 in that structure) — never
                # touched by -PreviewImage<= alone. A LibRaw-based viewer (FastStone, and
                # plausibly others) reads JpgFromRaw preferentially over PreviewImage, so
                # skipping this left edits invisible there forever, confirmed by hashing
                # JpgFromRaw before/after a save: byte-identical without this line. See
                # CLAUDE.md "Save" section for the full before/after verification.
                "-JpgFromRaw<=" + tmp_jpeg.name,
                "-tagsfromfile", tmp_jpeg.name,
                "-ifd0:imagewidth<imagewidth",
                "-ifd0:imageheight<imageheight",
                "-ifd0:rowsperstrip<imageheight",
            ]
            args += self._build_all_edit_xmp_args()
            # Keep the file's own OS timestamps matching when the photo was actually
            # taken, not "now" — otherwise every save bumps the file to today's date,
            # breaking chronological sorting in a file browser. Sourced from the file's
            # own DateTimeOriginal (verified: FileCreateDate is a real, working exiftool
            # pseudo-tag on Windows/NTFS, not just FileModifyDate).
            #
            # CONFIRMED BUG, fixed before this ever shipped: without "-tagsfromfile @" to
            # reset the source back to the target file itself, these two directives were
            # silently inheriting the earlier "-tagsfromfile tmp_jpeg.name" context (set
            # a few lines up, for -ifd0:imagewidth<imagewidth) — so they tried reading
            # DateTimeOriginal from the freshly cv2-encoded temp JPEG, which has no EXIF
            # data at all, and silently no-op'd (FileModifyDate stayed at "now"). Caught
            # by an automated test that bumped the file's mtime before saving and checked
            # it was restored, not by assuming the CLI syntax was self-evidently right.
            args += ["-tagsfromfile", "@", "-FileModifyDate<DateTimeOriginal", "-FileCreateDate<DateTimeOriginal"]
            args += ["-overwrite_original", path]

            with exiftool.ExifToolHelper(executable=self.exiftool_path) as et:
                et.execute(*args)

            self.statusBar().showMessage(
                f"Salvato in {os.path.basename(path)} ({time.time() - t0:.1f}s) — "
                "tag XMP standard, ma il rendering in altri editor (Lightroom, ecc.) può differire dall'anteprima qui"
            )
        except Exception as e:
            QMessageBox.critical(self, "Salvataggio non riuscito", str(e))
            self.statusBar().showMessage("Salvataggio fallito")
        finally:
            if tmp_jpeg is not None and os.path.exists(tmp_jpeg.name):
                os.unlink(tmp_jpeg.name)
            if self.raw is None:
                self.raw = rawpy.imread(path)  # always leave the app with a usable handle

    def export_preview_jpeg(self):
        """Renders the *current* editing state (not necessarily what's saved in the DNG —
        this exports live, independent of whether Save has been clicked) at full
        resolution to a standalone JPEG. Distinct from Save's own embedded preview: a
        higher quality (92 vs the embedded preview's 69, since this is a deliverable meant
        to be viewed/shared/printed on its own, not an internal thumbnail) and, at the
        user's request, the exported file's OS timestamp is set to match the photo's own
        DateTimeOriginal — the same "keep the real shooting date, not today" fix applied to
        Save (see save_to_dng()) — so exports sort correctly alongside the source DNGs in
        a file browser instead of all clustering on today's date.
        """
        if self.raw is None or self.raw_path is None:
            return
        default_name = os.path.splitext(os.path.basename(self.raw_path))[0] + ".jpg"
        default_path = os.path.join(os.path.dirname(self.raw_path), default_name)
        out_path, _ = QFileDialog.getSaveFileName(
            self, "Export Preview as JPEG", default_path, "JPEG files (*.jpg *.jpeg)"
        )
        if not out_path:
            return

        self.statusBar().showMessage("Rendering a piena risoluzione per l'esportazione…")
        QApplication.processEvents()
        t0 = time.time()
        try:
            rgb = self._render(half_size=False, for_save=True)
            bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
            ok, buf = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 92])
            if not ok:
                raise RuntimeError("Codifica JPEG fallita")
            with open(out_path, "wb") as f:
                f.write(buf.tobytes())

            timestamp_note = ""
            if self.exiftool_path:
                try:
                    with exiftool.ExifToolHelper(executable=self.exiftool_path) as et:
                        et.execute(
                            "-tagsfromfile", self.raw_path,
                            "-FileModifyDate<DateTimeOriginal", "-FileCreateDate<DateTimeOriginal",
                            "-overwrite_original", out_path,
                        )
                except Exception:
                    timestamp_note = " (timestamp non impostato: errore exiftool)"
            else:
                timestamp_note = " (timestamp non impostato: exiftool non trovato)"

            self.statusBar().showMessage(
                f"Anteprima esportata in {os.path.basename(out_path)} "
                f"({time.time() - t0:.1f}s){timestamp_note}"
            )
        except Exception as e:
            QMessageBox.critical(self, "Esportazione non riuscita", str(e))
            self.statusBar().showMessage("Esportazione fallita")

    def _update_image_label(self):
        if getattr(self, "_preview_pixmap", None) is None:
            return
        if self._zoom_level is None:
            self.image_scroll.setWidgetResizable(True)
            scaled = self._preview_pixmap.scaled(
                self.image_label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
            self.image_label.setPixmap(scaled)
        else:
            # Explicit zoom: the label takes its own (larger-than-viewport) size and the
            # QScrollArea shows scrollbars, instead of always fitting to the viewport.
            self.image_scroll.setWidgetResizable(False)
            w = max(1, int(self._preview_pixmap.width() * self._zoom_level))
            h = max(1, int(self._preview_pixmap.height() * self._zoom_level))
            scaled = self._preview_pixmap.scaled(w, h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.image_label.setPixmap(scaled)
            self.image_label.resize(scaled.size())

    def _zoom_label(self, level):
        return "Fit" if level is None else f"{level * 100:.0f}%"

    def _on_zoom_combo_changed(self):
        self._set_zoom_level(self.ZOOM_PRESETS[self.zoom_combo.currentIndex()])

    def _set_zoom_level(self, level):
        """`level` is relative to the *preview* pixmap's own resolution, not necessarily
        the raw file's actual pixel dimensions — the live preview may itself be capped by
        PREVIEW_MAX_DIM (see _render()'s "Performance" docstring). Re-rendering at a
        higher resolution just to make a high zoom level pixel-accurate would reintroduce
        the per-slider-change cost that cap was added to avoid, so this app doesn't attempt
        it; zoom here is for panning/inspecting the current preview, not a guarantee of
        true 1:1 raw pixels at "100%".
        """
        self._zoom_level = level
        self.zoom_combo.blockSignals(True)
        # Marquee "zoom to selection" computes an exact-fit level that won't generally
        # land on one of the fixed presets — show the combo as blank rather than crash.
        self.zoom_combo.setCurrentIndex(self.ZOOM_PRESETS.index(level) if level in self.ZOOM_PRESETS else -1)
        self.zoom_combo.blockSignals(False)
        self._update_image_label()

    def _toggle_zoom_select(self, checked):
        """Arms/disarms marquee-zoom mode. Single-shot, same convention as the WB/radial/
        gradient/spot picker buttons: ImageLabel disarms itself the moment a rectangle is
        dragged (see its mouseReleaseEvent), so the button here is kept in sync from
        _on_zoom_to_rect() rather than staying checked after one use.
        """
        if self.raw is None:
            self.zoom_select_btn.setChecked(False)
            return
        self.image_label.zoom_select_armed = checked
        self.image_label.setCursor(Qt.CrossCursor if checked else Qt.ArrowCursor)

    def _on_zoom_to_rect(self, left, top, right, bottom):
        """Handles ImageLabel.zoomToRectRequested — the "marquee zoom" the user asked for:
        drag a rectangle on the image, zoom in to fill the viewport with just that area.
        `left/top/right/bottom` are fractions (0..1) of the currently-displayed pixmap,
        already computed by ImageLabel from the rubber-band's screen geometry.
        """
        self.zoom_select_btn.setChecked(False)
        self.image_label.setCursor(Qt.ArrowCursor)
        if self._preview_pixmap is None:
            return
        pw, ph = self._preview_pixmap.width(), self._preview_pixmap.height()
        rect_w_frac, rect_h_frac = right - left, bottom - top
        if rect_w_frac <= 0 or rect_h_frac <= 0:
            return

        viewport = self.image_scroll.viewport().size()
        rect_w_px, rect_h_px = rect_w_frac * pw, rect_h_frac * ph
        needed_zoom = min(viewport.width() / rect_w_px, viewport.height() / rect_h_px)
        needed_zoom = max(0.05, min(8.0, needed_zoom))

        self._set_zoom_level(needed_zoom)  # switches to setWidgetResizable(False) and rescales

        # Center the scroll area on the selected rectangle's own center.
        pixmap = self.image_label.pixmap()
        if pixmap is None or pixmap.isNull():
            return
        center_x = (left + right) / 2.0 * pixmap.width()
        center_y = (top + bottom) / 2.0 * pixmap.height()
        self.image_scroll.horizontalScrollBar().setValue(int(center_x - viewport.width() / 2.0))
        self.image_scroll.verticalScrollBar().setValue(int(center_y - viewport.height() / 2.0))

    def _zoom_step(self, direction):
        if self._zoom_level is None:
            # From Fit, the first step in either direction lands on 100% — the natural
            # "actual size" anchor — rather than jumping straight past it to 150%/75%.
            self._set_zoom_level(1.0)
            return
        numeric = [p for p in self.ZOOM_PRESETS if p is not None]
        current = self._zoom_level
        if direction > 0:
            candidates = [p for p in numeric if p > current + 1e-6]
            new_level = min(candidates) if candidates else numeric[-1]
        else:
            candidates = [p for p in numeric if p < current - 1e-6]
            new_level = max(candidates) if candidates else numeric[0]
        self._set_zoom_level(new_level)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_image_label()

    def closeEvent(self, event):
        if self.raw is not None:
            self.raw.close()
        if self._work_dng_dir is not None:
            self._old_work_dirs.append(self._work_dng_dir)
        for d in self._old_work_dirs:
            shutil.rmtree(d, ignore_errors=True)
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    window = DNGForge()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
