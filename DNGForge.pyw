import sys
import os
import math
import time
import copy
import shutil
import subprocess
import tempfile
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import rawpy
from PIL import Image
import cv2

try:
    import exiftool
    HAVE_PYEXIFTOOL = True
except ImportError:
    HAVE_PYEXIFTOOL = False

try:
    import lensfunpy
    HAVE_LENSFUNPY = True
except ImportError:
    HAVE_LENSFUNPY = False

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QSlider, QVBoxLayout,
    QHBoxLayout, QPushButton, QFileDialog, QGroupBox,
    QComboBox, QSplitter, QScrollArea, QSizePolicy, QMessageBox, QCheckBox,
    QRubberBand, QDialog, QPlainTextEdit, QLineEdit, QToolButton, QFrame,
)
from PySide6.QtGui import (
    QPixmap, QAction, QPainter, QPen, QColor, QFont, QDoubleValidator, QIntValidator, QIcon,
    QPainterPath,
)
from PySide6.QtCore import Qt, QTimer, Signal, QPointF, QRectF, QRect, QPoint, QSize, QThread

# Suppresses the transient console window Windows otherwise flashes open for every
# subprocess.run() call to a console-subsystem executable (exiftool.exe, Adobe DNG
# Converter.exe) — noticed by the user specifically because RethinkRAW's own background
# DNG Converter calls don't do this, making ours look comparatively janky. No-op on
# non-Windows (the attribute doesn't exist there), though this app is Windows-only anyway.
_NO_WINDOW_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Stamped into EXIF:Software/XMP:CreatorTool on every save_to_dng() (see that function) so a
# saved file carries real evidence of having been edited by this app — previously absent
# entirely: a DNG saved by DNGForge many times over still showed only "Adobe DNG Converter",
# the original NEF->DNG conversion tool, in every software-identity tag (confirmed directly by
# inspecting this project's own long-running test file), since save_to_dng() only ever wrote
# -XMP-crs:* edit-state tags, never anything identifying the editing app itself. "(Beta)" per
# the user's own description of the current state of this project — not a tracked version
# number (this project has none), just an honest status label.
APP_SOFTWARE_NAME = "DNGForge (Beta)"

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

# The "Adobe Color/Vivid/Landscape/Neutral/Portrait/Monochrome/Standard/Standard B&W" profiles
# are NOT resolved by Adobe DNG Converter from a bare -XMP-crs:CameraProfile=Name tag — confirmed
# empirically (writing CameraProfile=Adobe Vivid alone produced a byte-identical render to no
# profile at all). These 8 names are Adobe's built-in, camera-agnostic "Look" presets, resolved
# internally by Adobe's engine via a fixed LookUUID + LookTable hash, not by name lookup — real
# Lightroom/ACR writes the full Look struct (LookName/LookUUID/LookParameters*, including each
# look's own embedded tone curve) rather than relying on CameraProfile at all for these. Ported
# verbatim from RethinkRAW's profiles.go profileSettings map (same UUIDs/LookTable hashes/curve
# points), the only source found for these otherwise-undocumented constants. CameraProfile is
# always cleared alongside Look* for every entry, matching RethinkRAW exactly. Verified: writing
# this struct for "Adobe Vivid" (vs. clearing Look* with no profile at all) produces a real,
# measurable difference in Adobe DNG Converter's own rendered output, unlike the bare-name tag.
# Each entry is a list of (tag, value) pairs, in write order — value is a literal string, a list
# of (x, y) point tuples (formatted as curve points, same convention as CURVE_PRESETS), or None
# (written as an empty value, i.e. "clear this tag"). "Look*" wildcard-clears every Look-prefixed
# tag first so a stale LookParameters* from a *previously* selected different profile can't survive
# a profile switch — same "always write, even the absent/off state" convention this app already
# uses for HasCrop/radial/gradient filters.
PROFILE_LOOK_SETTINGS = {
    "Adobe Standard": [
        ("ConvertToGrayscale", None), ("CameraProfile", None), ("Look*", None),
    ],
    "Adobe Standard B&W": [
        ("ConvertToGrayscale", "True"), ("CameraProfile", None), ("Look*", None),
    ],
    "Adobe Color": [
        ("ConvertToGrayscale", None), ("CameraProfile", None), ("Look*", None),
        ("LookName", "Adobe Color"), ("LookUUID", "B952C231111CD8E0ECCF14B86BAA7077"),
        ("LookParametersToneCurvePV2012", [(0, 0), (22, 16), (40, 35), (127, 127), (224, 230), (240, 246), (255, 255)]),
        ("LookParametersLookTable", "E1095149FDB39D7A057BAB208837E2E1"),
    ],
    "Adobe Monochrome": [
        ("ConvertToGrayscale", "True"), ("CameraProfile", None), ("Look*", None),
        ("LookName", "Adobe Monochrome"), ("LookUUID", "0CFE8F8AB5F63B2A73CE0B0077D20817"),
        ("LookParametersConvertToGrayscale", "True"), ("LookParametersClarity2012", "+8"),
        ("LookParametersToneCurvePV2012", [(0, 0), (64, 56), (128, 128), (192, 197), (255, 255)]),
        ("LookParametersLookTable", "73ED6C18DDE909DD7EA2D771F5AC282D"),
    ],
    "Adobe Landscape": [
        ("ConvertToGrayscale", None), ("CameraProfile", None), ("Look*", None),
        ("LookName", "Adobe Landscape"), ("LookUUID", "6F9C877E84273F4E8271E6B91BEB36A1"),
        ("LookParametersHighlights2012", "-12"), ("LookParametersShadows2012", "+12"),
        ("LookParametersClarity2012", "+10"),
        ("LookParametersToneCurvePV2012", [(0, 0), (64, 60), (128, 128), (192, 196), (255, 255)]),
        ("LookParametersLookTable", "0B3BFB5CFB7DBF7FF175E98F24D316B0"),
    ],
    "Adobe Neutral": [
        ("ConvertToGrayscale", None), ("CameraProfile", None), ("Look*", None),
        ("LookName", "Adobe Neutral"), ("LookUUID", "1E8E067A11CD44394A3C36A327BB34D1"),
        ("LookParametersToneCurvePV2012", [(0, 0), (16, 24), (64, 72), (128, 128), (192, 176), (244, 234), (255, 255)]),
        ("LookParametersLookTable", "7740BB918B2F6D93D7B95A4DBB78DB23"),
    ],
    "Adobe Portrait": [
        ("ConvertToGrayscale", None), ("CameraProfile", None), ("Look*", None),
        ("LookName", "Adobe Portrait"), ("LookUUID", "D6496412E06A83789C499DF9540AA616"),
        ("LookParametersToneCurvePV2012", [(0, 0), (66, 64), (190, 192), (255, 255)]),
        ("LookParametersLookTable", "E5A76DBB8B3F132A04C01AF45DC2EF1B"),
    ],
    "Adobe Vivid": [
        ("ConvertToGrayscale", None), ("CameraProfile", None), ("Look*", None),
        ("LookName", "Adobe Vivid"), ("LookUUID", "EA1DE074F188405965EF399C72C221D9"),
        ("LookParametersClarity2012", "+10"),
        ("LookParametersToneCurvePV2012", [(0, 0), (32, 22), (64, 56), (128, 128), (224, 232), (240, 246), (255, 255)]),
        ("LookParametersLookTable", "2FE663AB0D3CE5DA7B9F657BBCD66DFE"),
    ],
}


def _format_curve_points(points) -> str:
    """"x, y; x, y; …" — same format RethinkRAW itself writes. Must be written under the global
    exiftool "-sep" option (see _build_xmp_crs_args()) so each "x, y" pair becomes its own rdf:Seq
    list item — the real on-disk XMP structure for ToneCurvePV2012/LookParametersToneCurvePV2012,
    confirmed by inspecting a RethinkRAW-produced file's raw XMP packet. Without -sep, exiftool
    writes this string as one flat attribute instead of a list, which Adobe DNG Converter's
    renderer silently ignores — confirmed empirically (an extreme inverted curve written that way
    produced a byte-identical render to no curve at all).
    """
    return "; ".join(f"{x:.0f}, {y:.0f}" for x, y in points)


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

# crs:PostCropVignetteStyle is an integer-enum XMP tag (1/2/3), not a free string like
# ToneCurveName2012/WhiteBalance — writes and reads (read side always via the app's usual
# "-n" convention, see read_xmp_crs_state()) both need this explicit int<->label mapping.
VIGNETTE_STYLE_VALUES = {"Highlight Priority": 1, "Color Priority": 2, "Paint Overlay": 3}
VIGNETTE_STYLE_NAMES = {v: k for k, v in VIGNETTE_STYLE_VALUES.items()}

# The 8 colors both the HSL panel (HueAdjustment*/SaturationAdjustment*/LuminanceAdjustment*)
# and the Gray Mixer (GrayMixer*) key their per-color XMP tags on — same order Lightroom's own
# panel uses, confirmed against exiftool's XMP.pm crs table and cross-checked directly in a
# genuine Lightroom-saved file (DSC_6751.dng has all 24 HueAdjustment*/SaturationAdjustment*/
# LuminanceAdjustment* tags present, in this exact spelling).
HSL_COLORS = ["Red", "Orange", "Yellow", "Green", "Aqua", "Blue", "Purple", "Magenta"]

# Exact field list/order requested by the user for the Metadata panel (roadmap item 10).
METADATA_FIELDS = [
    "File Name", "Dimensions", "Cropped", "Date Taken", "Exposure Time", "Aperture",
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
    if it isn't installed. This is a hard requirement, not an optional enhancement — Adobe
    DNG Converter is the sole renderer in this app (see CLAUDE.md), so a None return here
    makes DNGForge.load_dng() refuse to open any file at all (see
    DNGForge._show_dngconv_missing_error()).

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
                "EXIF:DateTimeOriginal", "EXIF:ExposureTime", "EXIF:FNumber",
                "Composite:Aperture", "EXIF:FocalLength",
                "Composite:FocalLength35efl", "EXIF:BrightnessValue", "EXIF:ExposureCompensation",
                "EXIF:ISO", "EXIF:Flash", "EXIF:ExposureProgram", "EXIF:MeteringMode",
                "EXIF:Make", "EXIF:Model", "Composite:LensID", "EXIF:LensModel", "XMP:Lens",
                "EXIF:Software", "XMP-crs:HasCrop",
            ])[0]
    except Exception:
        return values

    def first(*keys):
        for k in keys:
            v = tags.get(k)
            if v not in (None, ""):
                return str(v)
        return None

    aperture = first("EXIF:FNumber", "Composite:Aperture")
    mapping = {
        "Date Taken": first("EXIF:DateTimeOriginal"),
        "Exposure Time": first("EXIF:ExposureTime"),
        "Aperture": f"f/{aperture}" if aperture else None,
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
        # Composite:LensID resolves third-party lenses to their real brand+model (e.g. "Sigma
        # 17-50mm F2.8 EX DC OS HSM") via exiftool's own lens-ID database — plain EXIF:LensModel
        # alone often can't, since many camera bodies (Nikon in particular, see this project's
        # own roadmap item 4 note on third-party lens IDs) tag a third-party lens with only its
        # generic focal-length/aperture spec ("17.0-50.0 mm f/2.8"), no brand or exact model.
        # Verified directly against DSC_6751.dng (a real Sigma 17-50mm shot): LensModel alone
        # read "17.0-50.0 mm f/2.8"; Composite:LensID correctly resolved to the full
        # "Sigma 17-50mm F2.8 EX DC OS HSM" — and against RAW_NIKON_D90.dng's own kit lens
        # ("50.0 mm f/1.4" -> "AF Nikkor 50mm f/1.4D") and RAW_LEICA_M8.DNG's manual-focus glass
        # ("Summicron-M 50mm f/2 (IV, V)", resolved from MakerNotes:LensType with no plain
        # LensModel/XMP:Lens tag present at all in that file). Falls back to the plain tags only
        # for the rarer case of a lens/file combination exiftool's database can't resolve.
        "Lens": first("Composite:LensID", "EXIF:LensModel", "XMP:Lens"),
        "Software": first("EXIF:Software"),
    }
    for field, value in mapping.items():
        if value is not None:
            values[field] = value

    if tags.get("XMP:HasCrop"):
        values["Cropped"] = "Yes"

    return values


# Bare tag names written by _build_xmp_crs_args(), used again to read them back.
# LookName/ConvertToGrayscale round-trip the named-profile "Look" struct (see
# PROFILE_LOOK_SETTINGS) — CameraProfile is only ever populated by files this app *didn't* write
# itself (an older DNGForge version, or a real Lightroom/ACR file using a genuine per-camera DCP
# profile), kept as a fallback in _apply_xmp_crs_state().
XMP_CRS_TAGS = [
    "CameraProfile", "LookName", "ConvertToGrayscale", "WhiteBalance", "ColorTemperature", "Tint",
    "AutoTone",
    "Exposure2012", "Contrast2012", "Highlights2012", "Shadows2012", "Whites2012", "Blacks2012",
    "Texture", "Clarity2012", "Dehaze", "Vibrance", "Saturation",
    "ToneCurveName2012", "ToneCurvePV2012",
    "ParametricShadows", "ParametricDarks", "ParametricLights", "ParametricHighlights",
    "ParametricShadowSplit", "ParametricMidtoneSplit", "ParametricHighlightSplit",
    *[f"HueAdjustment{c}" for c in HSL_COLORS],
    *[f"SaturationAdjustment{c}" for c in HSL_COLORS],
    *[f"LuminanceAdjustment{c}" for c in HSL_COLORS],
    *[f"GrayMixer{c}" for c in HSL_COLORS],
    "SplitToningShadowHue", "SplitToningShadowSaturation",
    "SplitToningHighlightHue", "SplitToningHighlightSaturation", "SplitToningBalance",
    "Sharpness", "SharpenRadius", "SharpenDetail", "SharpenEdgeMasking",
    "LuminanceSmoothing", "LuminanceNoiseReductionDetail", "LuminanceNoiseReductionContrast",
    "ColorNoiseReduction", "ColorNoiseReductionDetail", "ColorNoiseReductionSmoothness",
    "DefringePurpleAmount", "DefringePurpleHueLo", "DefringePurpleHueHi",
    "DefringeGreenAmount", "DefringeGreenHueLo", "DefringeGreenHueHi",
    "GrainAmount", "GrainSize", "GrainFrequency",
    "PostCropVignetteAmount", "PostCropVignetteMidpoint", "PostCropVignetteFeather",
    "PostCropVignetteRoundness", "PostCropVignetteStyle",
    "LensProfileEnable", "AutoLateralCA",
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
    state = {t: tags.get(f"XMP:{t}") for t in XMP_CRS_TAGS}

    # LookName is a flattened convenience name for crs:Look/Name — works as a *flat* tag when
    # requested on its own (the standalone tags=None path above), but exiftool's -struct mode
    # (used by read_combined_edit_tags(), needed for the radial/gradient/spot struct reads)
    # switches it to a real nested struct instead: {'Name': ..., 'UUID': ..., 'Parameters': {...}}
    # under "XMP:Look", with no flat "XMP:LookName" key at all. Confirmed by dumping -XMP-crs:all
    # under -struct on a file saved with a named profile. Falls back to the nested shape only
    # when the flat lookup found nothing, so both call paths restore the Profile combo correctly.
    if state["LookName"] is None:
        look = tags.get("XMP:Look")
        if isinstance(look, dict):
            state["LookName"] = look.get("Name")

    return state


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
                   "XMP-crs:CropBottom", "XMP-crs:CropRight", "XMP-crs:CropAngle",
                   # "LookName" (already in XMP_CRS_TAGS) is a flattened convenience name that
                   # only resolves under *non*-struct mode; under this call's -struct mode, the
                   # named-profile data this app actually wrote only shows up under the real
                   # struct name "Look" ({'Name': ..., 'UUID': ..., 'Parameters': {...}}) — see
                   # read_xmp_crs_state()'s own fallback for the read side of this.
                   "XMP-crs:Look"]
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


def kelvin_to_multipliers(temp_k: float, tint: float) -> list[float]:
    """Approximate WB multipliers [R, G, B, G2] for a given Kelvin/tint pair.

    Not used for rendering — all rendering now goes exclusively through Adobe
    DNG Converter (see the "Adobe DNG Converter is now the sole renderer"
    section in CLAUDE.md), which does its own colorimetric WB from the
    Temperature/Tint values written to -XMP-crs:ColorTemperature/Tint. This
    function survives only as the internal forward-model helper
    multipliers_to_kelvin_tint() below needs for its numeric search, itself
    used only to *display* a Temperature/Tint reading on the (disabled)
    sliders in As Shot/Auto/Camera Matching… mode when no DNGCameraProfile
    is available for the real colorimetric conversion.

    Blackbody RGB appearance via the Tanner Helland approximation, then
    inverted (normalized on green) to get correction multipliers. This is a
    visual approximation, not the DNG ColorMatrix-based colorimetric method
    (see project memory: dngforge-rethinkraw-research, "bonus finding" on
    porting RethinkRAW's dng_temperature.go) planned for a later refinement.

    Tanner Helland's RGB is a *display-gamma* appearance (0-255 as it would
    look on screen); the ratios are raised to WB_GAMMA purely so the search
    in multipliers_to_kelvin_tint() below stays consistent with itself —
    since nothing here feeds pixel data anymore, the gamma correction no
    longer has a rendering consequence, just an internal-consistency one.
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


class NoWheelSlider(QSlider):
    """A QSlider that ignores mouse-wheel events instead of nudging its value.

    Without this, resting the cursor anywhere over the control panel while scrolling the
    right-hand scroll area risks silently dragging whichever slider happens to be under the
    pointer (reported directly by the user: passing over the controls while scrolling the
    sidebar could accidentally change a slider's value). Ignoring the event here — rather than
    just not calling super() — makes Qt propagate it up to the parent widget chain, so the
    QScrollArea still scrolls normally; only click-and-drag (or the keyboard) can change the
    value now.
    """

    def wheelEvent(self, event):
        event.ignore()


class NoWheelComboBox(QComboBox):
    """A QComboBox that ignores mouse-wheel events instead of changing its selection.

    Same reasoning as NoWheelSlider above — scrolling the sidebar over a combo box (Profile,
    White Balance mode, Tone mode, Curve, ...) shouldn't silently change what's selected.
    """

    def wheelEvent(self, event):
        event.ignore()


class CollapsibleGroupBox(QGroupBox):
    """A checkable QGroupBox whose own title checkbox collapses/expands its contents, instead
    of just graying them out the way a plain checkable QGroupBox normally would. User-requested
    to keep long control-panel sections manageable ("possiamo rendere collassabili alcuni gruppi
    di controlli?"), starting with HSL/Color — the single biggest group in the panel (24
    sliders). Checked = expanded (the default, so nothing changes for a group the user hasn't
    touched); unchecking hides every direct child in the box's own layout so the surrounding
    scroll area actually reclaims the space, rather than leaving disabled sliders in place.
    """

    def __init__(self, title, start_expanded=True, parent=None):
        super().__init__(title, parent)
        self.setCheckable(True)
        # NOT applied here: at __init__ time the caller hasn't attached a QVBoxLayout (or added
        # any child sliders to it) yet — that happens in the lines immediately following
        # construction, back in the caller. Setting checked=False this early would find
        # self.layout() still None and silently hide nothing, leaving every child slider visible
        # despite the box claiming to be collapsed. Deferred to the first showEvent() instead,
        # by which point the caller's whole synchronous build-the-panel pass has long finished.
        self._start_expanded = start_expanded
        self._initial_state_applied = False
        self.toggled.connect(self._on_toggled)

    def showEvent(self, event):
        super().showEvent(event)
        if not self._initial_state_applied:
            self._initial_state_applied = True
            self.setChecked(self._start_expanded)

    def _on_toggled(self, expanded):
        layout = self.layout()
        if layout is None:
            return
        self._set_layout_visible(layout, expanded)

    def _set_layout_visible(self, layout, visible):
        # Recurses into nested layouts (e.g. the "Style" combo row in Post-Crop Vignette, added
        # via addLayout() rather than addWidget()) so every control in the group is actually
        # hidden on collapse, not just the ones sitting directly in the group's own top-level
        # layout.
        for i in range(layout.count()):
            item = layout.itemAt(i)
            widget = item.widget()
            if widget is not None:
                widget.setVisible(visible)
                continue
            sub_layout = item.layout()
            if sub_layout is not None:
                self._set_layout_visible(sub_layout, visible)


class ValueLineEdit(QLineEdit):
    """The slider's numeric readout, upgraded from a plain QLabel to a directly editable field
    (user-requested: "potremmo fare in modo che i valori numerici risultanti siano direttamente
    editabili?"). Selects its whole contents on focus so overtyping a new value doesn't require
    manually clearing the old one first — same convenience as clicking into a spreadsheet cell.
    The select-all is deferred a tick (QTimer.singleShot(0, ...)) because a click-to-focus would
    otherwise immediately place the cursor at the click position, undoing a selectAll() called
    synchronously inside focusInEvent.
    """

    def focusInEvent(self, event):
        super().focusInEvent(event)
        QTimer.singleShot(0, self.selectAll)


class Slider(QWidget):
    """Labeled slider with a live, directly-editable numeric readout, scaled from int ticks to
    a float range."""

    def __init__(self, label, minimum, maximum, default, step=1, decimals=0, on_change=None):
        super().__init__()
        self.decimals = decimals
        self.scale = 10 ** decimals

        self.slider = NoWheelSlider(Qt.Horizontal)
        self.slider.setMinimum(int(minimum * self.scale))
        self.slider.setMaximum(int(maximum * self.scale))
        self.slider.setSingleStep(int(step * self.scale))
        self.slider.setValue(int(default * self.scale))

        self.value_label = ValueLineEdit(self._fmt(default))
        self.value_label.setFixedWidth(56)
        self.value_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.value_label.setFrame(False)
        self.value_label.setStyleSheet("background: transparent;")
        if decimals > 0:
            validator = QDoubleValidator(minimum, maximum, decimals, self.value_label)
            validator.setNotation(QDoubleValidator.StandardNotation)
        else:
            validator = QIntValidator(int(minimum), int(maximum), self.value_label)
        self.value_label.setValidator(validator)
        self.value_label.editingFinished.connect(self._on_value_edited)

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

    def _on_value_edited(self):
        text = self.value_label.text().strip().replace(",", ".")
        try:
            v = float(text)
        except ValueError:
            v = self.value()
        self.slider.setValue(round(v * self.scale))
        # QSlider.setValue() clamps to [min, max] and only emits valueChanged (which would
        # otherwise reformat the label via _changed()) when the value actually changes — so an
        # out-of-range or oddly-formatted typed value (e.g. "50" into a 1-decimal field) needs
        # an explicit resync here too, same "label must match what the slider actually landed
        # on" lesson set_value() below already had to learn.
        self.value_label.setText(self._fmt(self.value()))

    def value(self):
        return self.slider.value() / self.scale

    def set_value(self, v):
        self.slider.blockSignals(True)
        self.slider.setValue(int(v * self.scale))
        # CONFIRMED BUG, fixed: QSlider.setValue() clamps internally to [min, max], but the
        # label was being set from the raw, possibly out-of-range `v` passed in — so an
        # out-of-range computed value (e.g. an Auto-WB temperature above the Temperature
        # slider's 12000K ceiling) showed the slider handle pinned at its max while the text
        # label kept displaying the unclamped number (reported directly by the user: "il
        # rendering è corretto però mi dà come temperatura 13200K", the slider's own max
        # being 12000 — rendering was unaffected since _build_xmp_crs_args() already reads
        # via value(), which returns the correctly-clamped slider position; only this label
        # was wrong). Re-reading self.slider.value() after setValue() gives the value the
        # slider actually landed on, so the label always matches what's visually shown.
        actual = self.slider.value() / self.scale
        self.value_label.setText(self._fmt(actual))
        self.slider.blockSignals(False)


class FilmstripWidget(QWidget):
    """A horizontal strip of DNG thumbnails for a single folder — File → Apri cartella…
    populates it (DNGForge.open_folder()), clicking a thumbnail loads that file into the main
    editor via the existing load_dng(), same as always. Deliberately **not** a catalog/library:
    nothing here is saved between sessions or even between folders — set_photos() wipes and
    rebuilds from scratch every time, matching the user's own actual workflow (open a shoot's
    folder, work through it, then move the finished files to a separate drive organized by
    date — a persistent database would just be overhead for that).

    Thumbnails themselves are filled in asynchronously by a separate ThumbnailScanThread (see
    that class) — this widget only ever receives already-decoded QPixmaps via set_thumbnail(),
    it has no exiftool/file-I/O logic of its own.
    """

    photoActivated = Signal(str)  # emitted with a DNG path when a thumbnail is clicked

    THUMB_SIZE = 90

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(self.THUMB_SIZE + 36)
        self._buttons = {}  # path -> QToolButton
        self._current_path = None

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 2, 0, 2)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        strip = QWidget()
        self.strip_layout = QHBoxLayout(strip)
        self.strip_layout.setContentsMargins(4, 2, 4, 2)
        self.strip_layout.setSpacing(4)
        self.strip_layout.addStretch(1)  # keeps buttons left-aligned as more are added
        self.scroll.setWidget(strip)
        outer.addWidget(self.scroll)
        self.hide()  # nothing to show until a folder is actually opened

    def set_photos(self, paths):
        """Wipes any previous strip content and lays out one empty (icon-less) button per
        path, in the given order — ThumbnailScanThread fills in the actual icons afterward,
        one at a time, via set_thumbnail(). Hides the whole widget again for an empty list,
        so it doesn't sit there as a bare empty bar when no folder is open."""
        for btn in self._buttons.values():
            btn.deleteLater()
        self._buttons = {}
        self._current_path = None
        while self.strip_layout.count() > 1:  # everything except the trailing stretch
            item = self.strip_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        if not paths:
            self.hide()
            return
        for path in paths:
            btn = QToolButton()
            btn.setFixedSize(self.THUMB_SIZE + 8, self.THUMB_SIZE + 8)
            btn.setIconSize(QSize(self.THUMB_SIZE, self.THUMB_SIZE))
            btn.setAutoRaise(True)
            btn.setCheckable(True)
            btn.setToolTip(os.path.basename(path))
            btn.clicked.connect(lambda checked=False, p=path: self.photoActivated.emit(p))
            self._buttons[path] = btn
            self.strip_layout.insertWidget(self.strip_layout.count() - 1, btn)
        self.show()

    def set_thumbnail(self, path, pixmap):
        """pixmap may be a null/empty QPixmap (no embedded preview found for that file, or
        extraction failed) — falls back to showing the bare filename as button text instead
        of leaving a blank, unexplained icon."""
        btn = self._buttons.get(path)
        if btn is None:
            return  # a stale result from an already-superseded folder scan — ignore
        if pixmap is None or pixmap.isNull():
            btn.setText(os.path.basename(path))
        else:
            btn.setIcon(QIcon(pixmap))

    def set_current(self, path):
        """Highlights whichever thumbnail corresponds to the file currently open in the main
        editor — a no-op (not an error) when path isn't part of the current filmstrip, e.g.
        a file opened directly via File → Open DNG… rather than picked from the gallery."""
        if self._current_path is not None:
            prev = self._buttons.get(self._current_path)
            if prev is not None:
                prev.setChecked(False)
        self._current_path = path
        btn = self._buttons.get(path)
        if btn is not None:
            btn.setChecked(True)
            self.scroll.ensureWidgetVisible(btn)


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
    panRequested = Signal(int, int)  # dx, dy screen px since the last move event, while
    # click-dragging with no other tool armed and no handle/overlay under the cursor — see
    # "Zoom" in CLAUDE.md for why this exists (plain scrollbars alone weren't discoverable/
    # usable enough once zoomed in past the viewport, per direct user feedback)

    HANDLE_HIT_RADIUS = 10
    ROTATE_HANDLE_OFFSET = 25  # screen px beyond the ellipse's top edge
    LINE_BOUNDARY_HALF_LEN = 60  # screen px, half-length of the zero/full boundary ticks
    LINE_BODY_HIT_DIST = 8  # screen px, how close a click needs to be to the gradient's spine
    SPINNER_DOT_COUNT = 12
    SPINNER_RADIUS = 18  # screen px, ring radius the dots sit on
    SPINNER_DOT_RADIUS = 4  # screen px, each dot's own radius

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
        # shown, the Adobe DNG Converter render itself already renders the straightened full frame
        # (see DNGForge._effective_crop_for_render()), so this overlay is always a plain axis-aligned
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
        self._pan_press_pos = None  # QPoint where a potential pan-drag started, or None — set
        # on any plain press that no other tool/handle/overlay claims (see mousePressEvent's
        # final fallback); only actually becomes a pan once the mouse moves past a small
        # threshold, so a plain click still reaches the WB/radial/gradient/spot pickers below
        self._pan_active = False  # True once that threshold has been crossed this drag
        self._spinner_active = False  # True while a background Adobe DNG Converter render is
        # in flight — start_spinner()/stop_spinner(), called by DNGForge around the render
        # pipeline (see _start_dngconv_render()), same "the only feedback is that something
        # is happening" spot the status bar text already covers, just visual (RethinkRAW-style
        # dotted spinner centered on the image, user-requested after seeing it there).
        self._spinner_frame = 0
        self._spinner_timer = QTimer(self)
        self._spinner_timer.timeout.connect(self._advance_spinner)

    def start_spinner(self):
        if self._spinner_active:
            return  # already running (e.g. one render settling straight into the next,
            # single-in-flight-plus-pending queueing — see _schedule_dngconv_render()) —
            # leave it animating rather than resetting/flickering
        self._spinner_active = True
        self._spinner_frame = 0
        self._spinner_timer.start(80)
        self.update()

    def stop_spinner(self):
        if not self._spinner_active:
            return
        self._spinner_active = False
        self._spinner_timer.stop()
        self.update()

    def _advance_spinner(self):
        self._spinner_frame = (self._spinner_frame + 1) % self.SPINNER_DOT_COUNT
        self.update()

    def _paint_spinner(self, painter):
        """RethinkRAW-style dotted circle: a ring of dots around the image's center,
        fading out behind a bright 'lead' dot that advances each timer tick — purely
        visual feedback that a background render is in flight, same idea as (and drawn
        on top of) the status bar's own "Rendering anteprima…" text.

        CONFIRMED BUG, fixed: centering on self.rect() (the label's own full size) put the
        spinner off in a corner or off-frame entirely whenever this label was larger than
        its containing QScrollArea's viewport — i.e. whenever zoomed in past 100%, the
        common case this spinner is most useful in. self.rect() is the *whole* scrollable
        canvas, not what's actually on screen. Fixed by centering on
        self.visibleRegion().boundingRect() instead — the portion of this widget actually
        unclipped by the scroll area, which tracks scrolling live (recomputed fresh on
        every paint, and the spinner's own 80ms timer already forces a repaint that often)
        without ImageLabel needing any direct reference to its containing QScrollArea,
        keeping the same loose coupling used everywhere else between the two (see
        panRequested/zoomRequested — always signals, never a stored parent reference).
        Falls back to the plain widget rect on the degenerate empty-region case (e.g. not
        yet shown)."""
        visible = self.visibleRegion().boundingRect()
        if visible.isEmpty():
            visible = self.rect()
        cx, cy = visible.center().x(), visible.center().y()
        n = self.SPINNER_DOT_COUNT
        painter.setPen(Qt.NoPen)
        # soft dark backdrop so the dots stay visible over a bright part of the photo
        painter.setBrush(QColor(0, 0, 0, 90))
        painter.drawEllipse(QPointF(cx, cy), self.SPINNER_RADIUS + 12, self.SPINNER_RADIUS + 12)
        for i in range(n):
            angle = 2 * math.pi * i / n
            x = cx + self.SPINNER_RADIUS * math.cos(angle)
            y = cy + self.SPINNER_RADIUS * math.sin(angle)
            lag = (i - self._spinner_frame) % n
            alpha = max(30, 255 - lag * (225 // n))
            painter.setBrush(QColor(255, 255, 255, alpha))
            painter.drawEllipse(QPointF(x, y), self.SPINNER_DOT_RADIUS, self.SPINNER_DOT_RADIUS)

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

        # Nothing else claimed this press — record it as a *potential* pan-drag start.
        # `clicked` is deliberately **not** emitted here anymore (it used to fire
        # unconditionally on press): now deferred to mouseReleaseEvent, and only if the
        # press never crossed the drag threshold below — otherwise a click-and-drag pan
        # would *also* fire the WB/radial/gradient/spot pickers at the press-down point,
        # which is not what dragging to pan means.
        self._pan_press_pos = QPoint(int(pos.x()), int(pos.y()))
        self._pan_active = False
        super().mousePressEvent(event)

    PAN_DRAG_THRESHOLD = 3  # screen px of movement before a plain press becomes a pan-drag
    # rather than a click — small enough to feel immediate, large enough that a WB-picker/
    # radial-placement click with a bit of hand tremor doesn't accidentally turn into a
    # (harmless, but confusing) tiny pan instead.

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
        if self._pan_press_pos is not None:
            pos = event.position()
            dx = pos.x() - self._pan_press_pos.x()
            dy = pos.y() - self._pan_press_pos.y()
            if not self._pan_active and (abs(dx) > self.PAN_DRAG_THRESHOLD or abs(dy) > self.PAN_DRAG_THRESHOLD):
                self._pan_active = True
                self.setCursor(Qt.ClosedHandCursor)
            if self._pan_active:
                self.panRequested.emit(int(dx), int(dy))
                self._pan_press_pos = QPoint(int(pos.x()), int(pos.y()))  # base for next delta
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
        elif self._pan_active:
            self._pan_active = False
            self._pan_press_pos = None
            self.setCursor(Qt.ArrowCursor)
        else:
            # A plain click that never crossed the pan-drag threshold — this is where
            # `clicked` actually fires now (see mousePressEvent's own comment for why it
            # moved here), feeding the WB/radial/gradient/spot pickers same as before.
            if self._pan_press_pos is not None:
                pos = event.position()
                pixmap = self.pixmap()
                if pixmap and not pixmap.isNull():
                    x_off = max(0.0, (self.width() - pixmap.width()) / 2.0)
                    y_off = max(0.0, (self.height() - pixmap.height()) / 2.0)
                    px, py = pos.x() - x_off, pos.y() - y_off
                    if 0 <= px < pixmap.width() and 0 <= py < pixmap.height():
                        self.clicked.emit(px / pixmap.width(), py / pixmap.height())
            self._pan_press_pos = None
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
                and not self.overlay_spots and not self._spinner_active):
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

        if self._spinner_active:
            self._paint_spinner(painter)

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
    connected with straight segments — deliberately simple rather than adding a spline
    library dependency; the points are written as-is to -XMP-crs:ToneCurvePV2012 and
    Adobe DNG Converter (the sole renderer, see CLAUDE.md) does the actual curve
    rendering. Points persist across mode switches
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


_persistent_exiftool_lock = threading.Lock()
_persistent_exiftool_helper = None  # lazily created, see _persistent_exiftool_execute()


def _persistent_exiftool_execute(exiftool_path: str, *args):
    """Runs an exiftool write via one shared, kept-alive ExifToolHelper process instead of
    spawning a fresh `with exiftool.ExifToolHelper(...) as et:` block per call — every
    render-pipeline XMP write (DngConverterRenderThread.run(), _render_single_frame(), so also
    the zoom-detail and local-adjustment pipelines that reuse it) goes through this now.

    User-reported DNGForge feeling visibly slower per-edit than RethinkRAW (a single Go binary
    with no external exiftool process at all). Measured directly on this machine: a cold
    `with ExifToolHelper(...) as et:` spawn+write costs ~0.8-1.0s — almost entirely Perl-
    interpreter startup and exiftool's own tag-table loading, not the write itself (the same
    per-spawn cost already profiled and fixed for file-*load* reads, see "Performance:
    consolidated exiftool reads on load"; this is the same problem in the *render* hot path,
    never fixed there before now). Reusing an already-running process for a second call on the
    same instance dropped that to ~0.25-0.5s — roughly half of one render cycle's total time.

    Guarded by a module-level lock: pyexiftool's ExifToolHelper talks to its subprocess over a
    single stdin/stdout pipe using an "-execute" marker protocol, so two threads calling
    .execute() on the *same* instance at once would interleave commands and corrupt both
    results. The main preview pipeline and the zoom-detail pipeline can genuinely have a render
    in flight at the same time (see CLAUDE.md "Two independent pipelines sharing one spinner"),
    so this isn't just theoretical — every caller serializes through this lock, which is still
    far cheaper than each paying its own ~0.8-1.0s cold-spawn cost. Left running for the app's
    lifetime; terminated in DNGForge.closeEvent().
    """
    global _persistent_exiftool_helper
    with _persistent_exiftool_lock:
        if _persistent_exiftool_helper is None or not _persistent_exiftool_helper.running:
            if _persistent_exiftool_helper is not None:
                try:
                    _persistent_exiftool_helper.terminate()
                except Exception:
                    pass
            _persistent_exiftool_helper = exiftool.ExifToolHelper(executable=exiftool_path)
        _persistent_exiftool_helper.execute(*args)


def _terminate_persistent_exiftool():
    """Stops the shared background exiftool process started by _persistent_exiftool_execute(),
    if one was ever started — called from DNGForge.closeEvent() so the app doesn't leave an
    orphaned exiftool.exe running after the window closes.
    """
    global _persistent_exiftool_helper
    with _persistent_exiftool_lock:
        if _persistent_exiftool_helper is not None:
            try:
                _persistent_exiftool_helper.terminate()
            except Exception:
                pass
            _persistent_exiftool_helper = None


def _extract_dngconverter_preview(exiftool_path: str, rendered_dng_path: str, out_jpeg_path: str) -> bool:
    """Extract the embedded preview JPEG from a DNG Adobe DNG Converter just produced,
    writing it to out_jpeg_path. Shared by DngConverterRenderThread (small live-preview
    renders) and DNGForge.save_to_dng()/export_preview_jpeg() (full-res renders) — same
    extraction logic either way, just at different resolutions.

    Prefers -JpgFromRaw over -PreviewImage — the same multi-preview-size DNG structure
    documented in CLAUDE.md's "Save" section: JpgFromRaw (SubIFD2) is Adobe DNG
    Converter's full-resolution embedded preview, while PreviewImage (SubIFD1) can be
    considerably smaller (measured 1024x680 vs. a 2560x1700 JpgFromRaw on the same file) —
    extracting PreviewImage first would silently swap in a lower-resolution image.
    PreviewImage is tried only as a fallback, for the rare DNG structure with no
    JpgFromRaw slot at all.

    Binary extraction goes through a plain subprocess call, not ExifToolHelper's
    persistent stay-open process — pyexiftool's stay-open protocol is text/JSON-oriented
    and isn't a reliable way to pull raw binary (JPEG) bytes back out.

    Returns True on success (a non-empty file was written), False otherwise — callers
    decide how to react (background thread emits `failed`; save/export raise).
    """
    with open(out_jpeg_path, "wb") as f:
        extract = subprocess.run(
            [exiftool_path, "-b", "-JpgFromRaw", rendered_dng_path],
            stdout=f, timeout=30, creationflags=_NO_WINDOW_FLAGS,
        )
    if extract.returncode != 0 or os.path.getsize(out_jpeg_path) == 0:
        with open(out_jpeg_path, "wb") as f:
            extract = subprocess.run(
                [exiftool_path, "-b", "-PreviewImage", rendered_dng_path],
                stdout=f, timeout=30, creationflags=_NO_WINDOW_FLAGS,
            )
    return extract.returncode == 0 and os.path.getsize(out_jpeg_path) > 0


_LENSFUN_DB = None


def _get_lensfun_db():
    """Lazily loads and caches lensfunpy's bundled lens database (parsing the whole XML
    database is the expensive part, ~tens of ms — worth doing once per process, not once
    per file). Read-only after construction, so sharing the one instance across the main
    thread (for the lookup in compute_lensfun_correction()) and background render threads
    (for building a Modifier from an already-resolved Camera/Lens pair) is safe — no
    thread creates or mutates a Database itself, only reads plain calibration records from
    the one loaded here.
    """
    global _LENSFUN_DB
    if _LENSFUN_DB is None:
        _LENSFUN_DB = lensfunpy.Database()
    return _LENSFUN_DB


def compute_lensfun_correction(metadata_values: dict) -> "dict | None":
    """Thin wrapper around lensfun_lookup_with_status() for callers that only need the
    correction dict, not the human-readable status text (currently none — kept for API
    symmetry with the pre-status-label version of this function)."""
    correction, _status = lensfun_lookup_with_status(metadata_values)
    return correction


def lensfun_lookup_with_status(metadata_values: dict) -> "tuple[dict | None, str]":
    """Looks up a matching Camera+Lens in lensfunpy's bundled database (see CLAUDE.md, "Lens
    Corrections" — Adobe DNG Converter's own CLI never applies LensProfileEnable's geometric/
    vignetting correction, confirmed by direct testing, so this app renders it itself
    instead) from the same `metadata_values` dict `_update_metadata_panel()` already computes
    for the read-only Metadata panel — no extra exiftool spawn needed.

    Critically uses `metadata_values["Lens"]` (sourced from `Composite:LensID`, exiftool's own
    lens-ID resolution database — see read_metadata()'s own comment) as the lens search hint,
    not the raw `EXIF:LensModel` tag: many camera bodies (Nikon in particular) tag a
    third-party lens with only a generic focal-length/aperture string ("17.0-50.0 mm f/2.8"),
    which lensfunpy's own fuzzy search cannot match to anything — confirmed directly (zero
    results either with or without an explicit lens_maker guess). Composite:LensID already
    solves exactly this problem, the same one this project's roadmap item 4 originally
    expected to need a hand-built lens-ID mapping table for.

    Returns (correction, status_text). `correction` is a dict describing the match (camera,
    lens, focal, aperture, distance — everything _apply_lensfun_correction() needs) or None
    when lensfunpy isn't installed, the camera or lens isn't in the database, or a shot
    parameter is missing. Never raises — a lookup failure just means the "Enable Profile
    Corrections" checkbox has nothing to apply, handled the same as if lensfunpy weren't
    installed at all. `status_text` is always a short, user-facing explanation of the result
    (match found, or why not) — added because the checkbox previously gave no indication
    whether a given camera/lens combination was actually recognized, see CLAUDE.md's own
    "Lens Corrections" section for the underlying no-op-CLI story this label documents.
    """
    if not HAVE_LENSFUNPY:
        return None, "lensfunpy non installato — nessuna correzione obiettivo disponibile"
    make = metadata_values.get("Make")
    model = metadata_values.get("Model")
    lens_hint = metadata_values.get("Lens")
    focal_str = metadata_values.get("Focal Length")
    aperture_str = metadata_values.get("Aperture")
    if not make or not model:
        return None, "camera non identificata nei metadati EXIF"
    if not lens_hint or lens_hint == "—":
        return None, "obiettivo non identificato nei metadati EXIF (Composite:LensID assente)"
    if not focal_str or not aperture_str:
        return None, "lunghezza focale o apertura mancante nei metadati EXIF"
    try:
        focal = float(str(focal_str).split()[0])
        aperture_num_str = str(aperture_str)
        if aperture_num_str.startswith("f/"):
            aperture_num_str = aperture_num_str[2:]
        aperture = float(aperture_num_str)
    except (ValueError, IndexError):
        return None, "lunghezza focale o apertura non interpretabile"
    try:
        db = _get_lensfun_db()
        cams = db.find_cameras(make, model)
        if not cams:
            return None, f"camera \"{make} {model}\" non presente nel database lensfun"
        cam = cams[0]
        lenses = db.find_lenses(cam, None, lens_hint)
        if not lenses:
            return None, f"obiettivo \"{lens_hint}\" non presente nel database lensfun per questa camera"
        lens = lenses[0]
        correction = {"camera": cam, "lens": lens, "focal": focal, "aperture": aperture, "distance": 1000.0}
        return correction, f"riconosciuto: {cam.model} + {lens.model}"
    except Exception:
        return None, "errore durante la ricerca nel database lensfun"


def _apply_lensfun_correction(jpeg_path: str, lens_correction: "dict | None") -> None:
    """In-place: if lens_correction is given (from compute_lensfun_correction()), decode
    jpeg_path, apply lensfunpy's geometric distortion + vignetting correction, and overwrite
    jpeg_path with the corrected image. A silent no-op when lens_correction is None (checkbox
    off, or no database match) or on any failure — a lens-correction problem should never
    sink an otherwise-successful render, same "degrade gracefully" convention already used
    for a failed/mismatched local-adjustment region elsewhere in this pipeline.

    Deliberately does NOT touch chromatic aberration — AutoLateralCA already renders
    correctly through Adobe DNG Converter's own CLI (confirmed directly, unlike
    LensProfileEnable), so requesting TCA here too would double-correct fringing whenever
    both are enabled. Only DISTORTION/GEOMETRY/VIGNETTING are requested.

    Uses the same PIL<->numpy round-trip convention as
    _render_local_adjustments_composite()/_apply_spot_heal() (plain RGB array, no BGR
    swap needed — lensfun's vignetting model applies the same per-pixel gain to every
    channel uniformly, so channel order doesn't matter for it, and cv2.remap() is purely
    geometric, also channel-order-agnostic).
    """
    if lens_correction is None:
        return
    try:
        img = np.array(Image.open(jpeg_path).convert("RGB"))
        h, w = img.shape[:2]
        mod = lensfunpy.Modifier(lens_correction["lens"], lens_correction["camera"].crop_factor, w, h)
        flags = (lensfunpy.ModifyFlags.DISTORTION | lensfunpy.ModifyFlags.GEOMETRY
                 | lensfunpy.ModifyFlags.VIGNETTING)
        mod.initialize(lens_correction["focal"], lens_correction["aperture"],
                        lens_correction["distance"], scale=0.0, flags=flags)
        mod.apply_color_modification(img)  # vignetting, in place
        undist_coords = mod.apply_geometry_distortion()
        if undist_coords is not None:
            img = cv2.remap(img, undist_coords, None, cv2.INTER_LANCZOS4)
        Image.fromarray(img, "RGB").save(jpeg_path, quality=95)
    except Exception:
        pass


SAVE_PREVIEW_QUALITY = 69  # user-requested quality for the DNG's own embedded previews —
# matches this app's own pre-Adobe-only local pipeline's historical Save quality (see
# CLAUDE.md, "Preserving file timestamps": "previously quality 69" for Save's embedded
# preview, before rendering moved to Adobe DNG Converter and there was no local re-encode
# step left to pick a quality for). Only used by save_to_dng() — export_preview_jpeg()
# still embeds Adobe's own bytes completely as-is, unchanged, since that's a standalone
# file the user may want at full quality.
SAVE_THUMB_MAX_SIDE = 1024  # long-edge cap for the small IFD0/SubIFD1/PreviewIFD preview —
# see _prepare_save_preview_jpegs()'s own docstring for why this needs to be genuinely
# small rather than a second full-resolution copy.

SAVE_JPGFROMRAW_TARGET_MP = 12  # user-requested cap for the *full-resolution* JpgFromRaw
# embed itself — a 24MP+ sensor's own native resolution was "anche troppi" for an embedded
# preview. See DNGForge._compute_save_render_side() for how this is achieved even with an
# active crop (the cropped *output* should still reach this target, not the uncropped
# frame) — and, symmetrically, why a small enough crop is never upscaled past its own real
# native resolution to force it, per the user's own explicit direction ("se il ritaglio è
# tale che la foto non può essere 12 mp non fare upscaling, lascia la risoluzione che
# viene").


def _prepare_save_preview_jpegs(jpeg_path: str, out_dir: str):
    """save_to_dng()-only post-process of the already-rendered full-resolution JPEG: (1)
    re-encodes it in place at SAVE_PREVIEW_QUALITY (this app's own historical Save quality,
    see that constant), and (2) produces a genuinely small thumbnail (long edge capped at
    SAVE_THUMB_MAX_SIDE) at a new path. Returns (full_res_path, thumb_path) — the caller
    feeds the first to -JpgFromRaw<= and the second to -PreviewImage<=, instead of the same
    full-resolution file to both.

    Purely a JPEG re-compression/resize of pixels Adobe DNG Converter already rendered —
    not a second rendering pass, so this doesn't conflict with "Adobe DNG Converter is the
    sole renderer" (CLAUDE.md) any more than _apply_lensfun_correction()'s own decode/
    re-encode round-trip already doesn't.

    **Why this exists**: exiftool's `-PreviewImage<=` writes into *every* physical location
    the (single, logical) "PreviewImage" tag occurs in this DNG structure — IFD0, SubIFD1,
    and the legacy PreviewIFD chain, three separate small/medium-preview slots inherited
    from Adobe DNG Converter's own initial NEF→DNG conversion — all with the *same* bytes
    handed to that one directive. Historically this app fed it the exact same full-
    resolution image already going to -JpgFromRaw<= (see save_to_dng()'s own comment on
    that line, "CONFIRMED BUG, fixed: a saved edit didn't show up in FastStone" — the fix
    that introduced -JpgFromRaw<= never needed -PreviewImage<= to also carry full
    resolution, only JpgFromRaw does, for LibRaw-based viewers specifically) — bloating
    three small-preview slots up to full size each. **Confirmed on a real user file**
    (`DSC_6795.dng`, 24MP): four separate ~7,039,901-byte copies of the identical image
    (IFD0.PreviewImage, SubIFD1.PreviewImage, PreviewIFD.PreviewImage, SubIFD2.JpgFromRaw),
    ~28MB of pure redundancy in a 37MB file, confirmed via `exiftool -a -G1` showing all
    four at the identical byte length despite very different *declared* ImageWidth/Height
    (5339×3559, 1024×683, unlabeled, 6000×4000 respectively) — the declared dimensions on
    three of those four slots were already stale/wrong before this fix, unrelated to it.
    Reported by the user as a save that "took 32 seconds" and "the file grew to 35 MB" —
    root-caused to this redundancy (file size confirmed stable across repeated saves, not
    literally growing further each time, but the same ~28MB of avoidable writes every
    single save) compounding an unrelated, unrepeatable slowdown that turned out to be
    the user editing directly off an SD card, not a code issue (see CLAUDE.md's own "Save"
    section for the full investigation writeup).
    """
    img = Image.open(jpeg_path).convert("RGB")
    img.save(jpeg_path, quality=SAVE_PREVIEW_QUALITY)
    w, h = img.size
    scale = SAVE_THUMB_MAX_SIDE / max(w, h)
    thumb = img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS) if scale < 1 else img
    thumb_path = os.path.join(out_dir, "preview_thumb.jpg")
    thumb.save(thumb_path, quality=SAVE_PREVIEW_QUALITY)
    return jpeg_path, thumb_path


def compute_rgb_histogram(image_path: str, bins: int = 256):
    """Per-channel pixel counts for HistogramWidget, computed directly from whatever JPEG
    Adobe DNG Converter just rendered (see DNGForge._on_dngconv_render_ready()) — the exact
    same pixels the app already displays, never a separate/approximated computation, matching
    this project's "Adobe DNG Converter is the sole renderer" rule (this function only
    visualizes its output, it doesn't recompute anything about the image itself).

    Returns (r_counts, g_counts, b_counts), each a `bins`-length np.ndarray, or None on any
    failure — a histogram is a nice-to-have display, never worth sinking an otherwise-
    successful render over.
    """
    try:
        img = np.asarray(Image.open(image_path).convert("RGB"))
        r = np.bincount(img[:, :, 0].ravel(), minlength=bins)[:bins]
        g = np.bincount(img[:, :, 1].ravel(), minlength=bins)[:bins]
        b = np.bincount(img[:, :, 2].ravel(), minlength=bins)[:bins]
        return r, g, b
    except Exception:
        return None


class HistogramWidget(QWidget):
    """A Lightroom-style RGB histogram, docked at the top of the right-hand control panel —
    user-requested ("si può mettere un grafico dell'istogramma in alto a destra come in
    lightroom?"). Purely a display: it never computes or approximates anything about the
    photo itself, only visualizes whatever compute_rgb_histogram() measured from the most
    recent real Adobe DNG Converter render (see DNGForge._on_dngconv_render_ready(), the one
    call site that feeds it) — consistent with "Adobe DNG Converter is the sole renderer"
    (CLAUDE.md), the same way every other preview element in this app ultimately traces back
    to a real Adobe render rather than a local approximation.

    Deliberately updated only from the *main* preview pipeline, not the zoom-detail one — the
    histogram should always reflect the whole photo, not whatever cropped region happens to be
    on screen while zoomed in, matching real Lightroom's own histogram behavior.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(100)
        self.setMaximumHeight(120)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._counts = None  # (r, g, b), each a 256-length np.ndarray, or None (nothing to show)

    def set_data(self, counts):
        self._counts = counts
        self.update()

    def clear_data(self):
        self._counts = None
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = self.rect()
        painter.fillRect(rect, QColor(30, 30, 30))
        painter.setPen(QPen(QColor(70, 70, 70)))
        painter.drawRect(rect.adjusted(0, 0, -1, -1))
        if self._counts is None:
            painter.setPen(QColor(140, 140, 140))
            painter.drawText(rect, Qt.AlignCenter, "Istogramma non disponibile")
            painter.end()
            return

        w, h = rect.width(), rect.height()
        n_bins = len(self._counts[0])
        bin_w = w / n_bins
        # Square-root scale, same reasoning Lightroom's own histogram display uses: a typical
        # photo's tallest bin (often near-black or near-white) would otherwise dwarf every
        # other bin into an invisible flat line at a plain linear scale. A full log scale was
        # tried first and rejected — it compresses so aggressively that nearly every non-empty
        # bin reads as "almost full height", losing the actual peak/valley shape; sqrt keeps
        # that shape visible while still pulling small bins up out of invisibility.
        max_count = max(int(c.max()) for c in self._counts) or 1
        sqrt_max = math.sqrt(max_count)

        def path_for(channel):
            path = QPainterPath()
            path.moveTo(0, h)
            for i in range(n_bins):
                x = i * bin_w
                y = h - (math.sqrt(int(channel[i])) / sqrt_max) * h if sqrt_max > 0 else h
                path.lineTo(x, y)
            path.lineTo(w, h)
            path.closeSubpath()
            return path

        # Additive blend — where two or three channels overlap, colors add toward white/
        # cyan/magenta/yellow instead of the later channel simply occluding the earlier one,
        # the same visual signature real Lightroom's overlaid RGB histogram has.
        painter.setCompositionMode(QPainter.CompositionMode_Plus)
        painter.setPen(Qt.NoPen)
        for channel, color in (
            (self._counts[0], QColor(255, 60, 60, 150)),
            (self._counts[1], QColor(60, 220, 60, 150)),
            (self._counts[2], QColor(60, 130, 255, 150)),
        ):
            painter.setBrush(color)
            painter.drawPath(path_for(channel))
        painter.end()


class ThumbnailScanThread(QThread):
    """Background job for FilmstripWidget: extracts a small embedded preview for every DNG in
    a folder, in **one** exiftool process rather than one spawn per file — the same "batch
    instead of N spawns" discipline already applied to read_combined_edit_tags() (each spawn
    costs ~440-470ms almost entirely as process-startup overhead, see CLAUDE.md's
    "Performance: consolidated exiftool reads on load"; for a folder of dozens of shots that
    adds up fast). Uses exiftool's own `-w` (write) option to dump each file's `PreviewImage`
    straight to its own JPEG in `scratch_dir`, named after the source file's own basename —
    `%f` in exiftool's format-string syntax. `PreviewImage` (not the tiny `ThumbnailImage`)
    because a DNG converted by Adobe DNG Converter — which every file this app can even open
    already has to be, see CLAUDE.md's "Adobe DNG Converter is the sole renderer" — generally
    has no separate small IFD0 thumbnail at all, only PreviewImage/JpgFromRaw at real preview
    resolution (confirmed directly: RAW_NIKON_D90.dng and DSC_6751_prova.dng both have zero
    `ThumbnailImage` tag). A source DNG with neither tag at all (found on this project's own
    RAW_LEICA_M8.DNG, an unconverted raw-samples test file, not an Adobe DNG Converter output)
    simply gets no output file — the caller's own per-path fallback (a blank icon, filename
    text instead) handles that, not this thread.

    `scratch_dir` is created and later deleted by the **caller** (DNGForge.open_folder()/
    _on_thumbnail_scan_finished()), not by this thread — cross-thread signal delivery is
    asynchronous, so deleting it from inside run() right after emitting the last
    `thumbnail_ready` could race the main thread's own not-yet-processed handler still trying
    to read one of the files this thread just wrote. Same reasoning already documented for
    DngConverterRenderThread's own per-instance token scheme, just solved here by leaving
    cleanup entirely to the receiver instead.

    `generation` lets the caller discard a stale scan's results the same way the render
    pipeline's own generation counter already works — if the user opens a *second* folder
    before the first folder's scan finishes, the first scan's thumbnails should never land on
    the second folder's filmstrip.
    """

    thumbnail_ready = Signal(str, str, int)  # dng_path, extracted_thumb_jpeg_path (or ""), generation
    finished_scan = Signal(int)              # generation

    def __init__(self, paths, exiftool_path, scratch_dir, generation, parent=None):
        super().__init__(parent)
        self.paths = paths
        self.exiftool_path = exiftool_path
        self.scratch_dir = scratch_dir
        self.generation = generation

    def run(self):
        try:
            args = [self.exiftool_path, "-b", "-PreviewImage",
                    "-w", os.path.join(self.scratch_dir, "%f_thumb.jpg")] + self.paths
            subprocess.run(args, capture_output=True, timeout=180, creationflags=_NO_WINDOW_FLAGS)
            for path in self.paths:
                stem = os.path.splitext(os.path.basename(path))[0]
                thumb_path = os.path.join(self.scratch_dir, f"{stem}_thumb.jpg")
                self.thumbnail_ready.emit(path, thumb_path if os.path.isfile(thumb_path) else "",
                                           self.generation)
        except Exception:
            pass
        finally:
            self.finished_scan.emit(self.generation)


class DngConverterWorkCopyThread(QThread):
    """Background job: create a downscaled, already-demosaiced DNG copy of the original
    file, used as the fast-to-reconvert input for DngConverterRenderThread — same
    architecture RethinkRAW itself uses for its own live preview (its `edit.dng`),
    confirmed by reading its Go source (`edit.go`'s `previewEdit()`): converting the full
    original on every settle would take seconds; converting an already-downscaled copy
    takes well under a second (measured ~0.8s on this dev machine for the default
    2560px-long-edge copy).

    `side` is the long-edge target in pixels, same convention as Adobe DNG Converter's own
    `-side` flag; `None` omits `-side` entirely, producing a copy at *full* native
    resolution instead — used for `ZOOM_DETAIL_SIDE` (see "Zoom" below): the one-time cost
    of demosaicing at full resolution, paid once per file the first time the user zooms in
    past the work copy's own resolution, so every subsequent detail-region re-render
    (`DngConverterRenderThread` again, with its own `side` set to the *requested detail
    resolution*, not None) only has to re-crop-and-scale an already-demosaiced source
    instead of re-decoding the raw mosaic from scratch each time.

    Runs entirely off the Qt main thread — safe, since it only touches plain file paths
    via subprocess, never a Qt widget.
    """

    ready = Signal(str)   # out_path
    failed = Signal()

    def __init__(self, dngconverter_path, source_path, out_dir, out_name, side, parent=None):
        super().__init__(parent)
        self.dngconverter_path = dngconverter_path
        self.source_path = source_path
        self.out_dir = out_dir
        self.out_name = out_name
        self.side = side

    def run(self):
        try:
            args = [self.dngconverter_path, "-c", "-lossy"]
            if self.side:
                args += ["-side", str(self.side)]
            args += ["-d", self.out_dir, "-o", self.out_name, self.source_path]
            result = subprocess.run(
                args, capture_output=True, timeout=120, creationflags=_NO_WINDOW_FLAGS,
            )
            out_path = os.path.join(self.out_dir, self.out_name)
            if result.returncode == 0 and os.path.isfile(out_path):
                self.ready.emit(out_path)
            else:
                self.failed.emit()
        except Exception:
            self.failed.emit()


class DngConverterRenderThread(QThread):
    """Background job: write the current edit state's XMP-crs tags onto the small working
    copy, re-run it through Adobe DNG Converter, and extract the resulting preview — the
    only kind of preview this app ever shows, since Adobe DNG Converter is its sole
    renderer (see CLAUDE.md). Mirrors RethinkRAW's own `previewEdit()` (same "retag the
    small DNG, reconvert, extract PreviewImage" sequence, confirmed by reading its Go
    source), just reimplemented here
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

    `side`, if given, adds Adobe DNG Converter's own `-side` flag to the reconversion —
    used by the "Zoom" detail-region pipeline (see that section) to request a specific
    output resolution when re-cropping the full-resolution zoom source; `None` (the normal
    live-preview case) omits it, converting `work_dng_path` at whatever resolution it
    already is.

    CONFIRMED BUG, fixed: the main preview pipeline (operating on work.dng) and the
    zoom-detail pipeline (operating on zoom_source.dng) both live in the *same* per-file
    temp dir (see "Zoom detail rendering" in CLAUDE.md) and, until this fix, both hardcoded
    the exact same output filenames ("rendered.dng"/"rendered_preview.jpg") inside that
    shared directory. Editing a slider while zoomed in past ZOOM_DETAIL_THRESHOLD starts
    both pipelines' threads at roughly the same time, so their two Adobe DNG Converter
    processes raced to write/read/delete the *same* two files — caught via a real repro
    (scripted slider edits while zoomed and panned) showing "File not found - rendered.dng"
    errors and, worse, self._preview_pixmap silently ending up holding the *zoom-detail*
    pipeline's own (differently sized, differently framed) output instead of the main
    pipeline's, which is what actually produced the reported "view jumps/zooms randomly on
    edit while zoomed" bug — not a scroll-position or zoom-level bug at all, a file-identity
    collision between two unrelated background renders. Fixed by giving every thread
    instance its own unique filenames (a uuid4 token minted once in __init__), so two
    concurrently-running renders — regardless of which pipeline started them — can never
    step on each other's intermediate files.
    """

    ready = Signal(str, int)   # jpeg_path, generation
    failed = Signal(int)       # generation

    def __init__(self, work_dng_path, xmp_args, exiftool_path, dngconverter_path, generation,
                 side=None, lens_correction=None, parent=None):
        super().__init__(parent)
        self.work_dng_path = work_dng_path
        self.xmp_args = xmp_args
        self.exiftool_path = exiftool_path
        self.dngconverter_path = dngconverter_path
        self.generation = generation
        self.side = side
        self.lens_correction = lens_correction  # see _apply_lensfun_correction() — plain
        # data (or None), same "must be resolved on the main thread before this thread
        # starts" reasoning as xmp_args above
        self._token = uuid.uuid4().hex[:8]  # see class docstring's "CONFIRMED BUG, fixed" —
        # keeps this instance's intermediate files from colliding with any other
        # DngConverterRenderThread writing into the same per-file temp dir concurrently

    def run(self):
        out_dir = os.path.dirname(self.work_dng_path)
        rendered_path = os.path.join(out_dir, f"rendered_{self._token}.dng")
        jpeg_path = os.path.join(out_dir, f"rendered_preview_{self._token}.jpg")
        try:
            _persistent_exiftool_execute(
                self.exiftool_path, *self.xmp_args, "-overwrite_original", self.work_dng_path
            )

            convert_args = [self.dngconverter_path, "-c"]
            if self.side:
                convert_args += ["-side", str(self.side)]
            convert_args += ["-p2", "-d", out_dir, "-o", os.path.basename(rendered_path), self.work_dng_path]
            result = subprocess.run(
                convert_args, capture_output=True, timeout=60, creationflags=_NO_WINDOW_FLAGS,
            )
            if result.returncode != 0 or not os.path.isfile(rendered_path):
                self.failed.emit(self.generation)
                return

            if not _extract_dngconverter_preview(self.exiftool_path, rendered_path, jpeg_path):
                self.failed.emit(self.generation)
                return

            _apply_lensfun_correction(jpeg_path, self.lens_correction)
            self.ready.emit(jpeg_path, self.generation)
        except Exception:
            self.failed.emit(self.generation)
        finally:
            if os.path.exists(rendered_path):
                try:
                    os.unlink(rendered_path)
                except OSError:
                    pass


def _render_single_frame(work_dng_path, xmp_args, exiftool_path, dngconverter_path, side=None):
    """Write xmp_args onto work_dng_path, convert via Adobe DNG Converter, extract the
    resulting preview JPEG, and return its path (caller must delete it) — or None on any
    failure. The standalone-function twin of DngConverterRenderThread.run() (see that
    class's own docstring for the unique-per-call-token filename scheme, reused here
    unchanged): used by _render_local_adjustments_composite() below, which needs to call
    this once per region rather than once per file.
    """
    out_dir = os.path.dirname(work_dng_path)
    token = uuid.uuid4().hex[:8]
    rendered_path = os.path.join(out_dir, f"rendered_{token}.dng")
    jpeg_path = os.path.join(out_dir, f"rendered_preview_{token}.jpg")
    try:
        _persistent_exiftool_execute(exiftool_path, *xmp_args, "-overwrite_original", work_dng_path)
        convert_args = [dngconverter_path, "-c"]
        if side:
            convert_args += ["-side", str(side)]
        convert_args += ["-p2", "-d", out_dir, "-o", os.path.basename(rendered_path), work_dng_path]
        result = subprocess.run(
            convert_args, capture_output=True, timeout=60, creationflags=_NO_WINDOW_FLAGS,
        )
        if result.returncode != 0 or not os.path.isfile(rendered_path):
            return None
        if not _extract_dngconverter_preview(exiftool_path, rendered_path, jpeg_path):
            return None
        return jpeg_path
    except Exception:
        return None
    finally:
        if os.path.exists(rendered_path):
            try:
                os.unlink(rendered_path)
            except OSError:
                pass


def _radial_alpha_map(w, h, f):
    """Per-pixel effect strength (0..1) for a radial filter region, in full-resolution
    image pixel space (w, h = the rendered frame's own dimensions — already remapped into
    that frame's fractions by DNGForge._local_filters_for_render() if a crop is active).
    Same inverse-rotation-then-normalize ellipse math as ImageLabel's own screen-space
    overlay (_local_to_screen()), just evaluated per-pixel instead of for on-screen handle
    positions. The feather falloff shape (a linear ramp centered on the nominal ellipse
    boundary) is this project's own approximation — Feather's exact Lightroom curve was
    never confirmed against a real Lightroom file (see CLAUDE.md, "Radial filter
    persistence") — everything else here (position/size/rotation/invert) is exact.
    """
    cx_px, cy_px = f["cx"] * w, f["cy"] * h
    rx_px = max(1.0, f["rx"] * w)
    ry_px = max(1.0, f["ry"] * h)
    theta = math.radians(f["rotation"])
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    ys, xs = np.mgrid[0:h, 0:w]
    dx = xs - cx_px
    dy = ys - cy_px
    lx = dx * cos_t + dy * sin_t
    ly = -dx * sin_t + dy * cos_t
    dist = np.sqrt((lx / rx_px) ** 2 + (ly / ry_px) ** 2)
    half_band = max(f["feather"], 1.0) / 100.0
    lo, hi = 1.0 - half_band, 1.0 + half_band
    alpha = np.clip((hi - dist) / (hi - lo), 0.0, 1.0)
    if f.get("invert"):
        alpha = 1.0 - alpha
    return alpha


def _gradient_alpha_map(w, h, f):
    """Per-pixel effect strength (0..1) for a graduated filter — an exact linear ramp from
    0 at the 'zero' point to 1 at the 'full' point, clamped beyond either end: the same
    definition Lightroom itself uses for a linear gradient mask (confirmed against a real
    Lightroom-saved file, see CLAUDE.md "Graduated filter persistence"). Unlike the radial
    filter's feather shape, nothing here is approximated — it's a plain dot-product
    projection onto the zero->full direction.
    """
    zx, zy = f["zero_x"] * w, f["zero_y"] * h
    fx, fy = f["full_x"] * w, f["full_y"] * h
    dx, dy = fx - zx, fy - zy
    length_sq = dx * dx + dy * dy
    ys, xs = np.mgrid[0:h, 0:w]
    if length_sq < 1e-9:
        alpha = np.ones((h, w), dtype=np.float64)
    else:
        t = ((xs - zx) * dx + (ys - zy) * dy) / length_sq
        alpha = np.clip(t, 0.0, 1.0)
    if f.get("invert"):
        alpha = 1.0 - alpha
    return alpha


_SPOT_CLONE_CORE_FRAC = 0.6  # inner fraction of the radius held at full strength before
# feathering starts — see _apply_spot_clone()'s own note for why this isn't just a plain
# center-to-edge cone


def _apply_spot_clone(arr, spot, w, h):
    """Feathered direct copy from the source circle to the destination circle, in place on
    `arr` (uint8, H x W x 3) — no color/lighting adaptation, the same 'Clone' algorithm this
    app used before the Adobe-only migration (see CLAUDE.md, "Spot Removal"), just applied
    on top of an Adobe render instead of being the only renderer.

    CONFIRMED weak-effect report, fixed: the original falloff was a plain center-to-edge
    cone (mask = 1-dist across the *entire* radius), so only the exact center pixel ever
    reached full strength and the visible effect looked soft/weak even at Opacity=100 — not
    how a real retouching brush reads (a solid disc with a soft edge only near the
    boundary). Fixed with a two-zone falloff: the inner `_SPOT_CLONE_CORE_FRAC` of the
    radius is held at full strength, feathering only in the outer ring.

    `opacity` is intentionally not clamped to 1.0 — the Opacity slider now goes to 200% (see
    its own UI-setup comment) precisely so a still-too-subtle result can be pushed harder:
    this is a plain linear blend between dst and src, so opacity > 1.0 is a well-defined
    extrapolation past a pure 100% copy (pushed further in the same direction, then clamped
    to a valid pixel range at the very end), not an error case.
    """
    radius_px = max(2, int(round(spot["radius"] * w)))
    cx, cy = int(round(spot["dest_x"] * w)), int(round(spot["dest_y"] * h))
    sx, sy = int(round(spot["src_x"] * w)), int(round(spot["src_y"] * h))
    dx, dy = cx - sx, cy - sy
    opacity = max(0.0, spot["opacity"] / 100.0)

    y0, y1 = max(0, cy - radius_px), min(h, cy + radius_px + 1)
    x0, x1 = max(0, cx - radius_px), min(w, cx + radius_px + 1)
    if y1 <= y0 or x1 <= x0:
        return
    ys, xs = np.mgrid[y0:y1, x0:x1]
    dist = np.sqrt((xs - cx) ** 2 + (ys - cy) ** 2) / radius_px
    core = _SPOT_CLONE_CORE_FRAC
    strength = np.where(dist <= core, 1.0, np.clip((1.0 - dist) / (1.0 - core), 0.0, 1.0))
    mask = (strength * opacity)[..., None]

    src_ys, src_xs = ys - dy, xs - dx
    valid = (src_ys >= 0) & (src_ys < h) & (src_xs >= 0) & (src_xs < w)
    src_ys_c, src_xs_c = np.clip(src_ys, 0, h - 1), np.clip(src_xs, 0, w - 1)
    src_pixels = arr[src_ys_c, src_xs_c].astype(np.float64)
    dst_pixels = arr[y0:y1, x0:x1].astype(np.float64)
    blended = dst_pixels * (1 - mask) + src_pixels * mask
    blended = np.where(valid[..., None], blended, dst_pixels)
    arr[y0:y1, x0:x1] = np.clip(blended, 0, 255).astype(np.uint8)


def _apply_spot_heal(arr, spot, w, h):
    """Poisson-blended copy from source to destination via cv2.seamlessClone — adapts the
    copied texture's color/lighting to the destination, the same 'Heal' algorithm this app
    used before the Adobe-only migration (see CLAUDE.md, "Spot Removal"). Falls back to
    _apply_spot_clone() when the source/destination circle doesn't fit entirely within the
    frame (seamlessClone requires its mask region to be fully interior) rather than raising."""
    radius_px = max(4, int(round(spot["radius"] * w)))
    src_cx, src_cy = int(round(spot["src_x"] * w)), int(round(spot["src_y"] * h))
    dst_cx, dst_cy = int(round(spot["dest_x"] * w)), int(round(spot["dest_y"] * h))
    margin = radius_px + 2
    if not (margin <= src_cx < w - margin and margin <= src_cy < h - margin
            and margin <= dst_cx < w - margin and margin <= dst_cy < h - margin):
        _apply_spot_clone(arr, spot, w, h)
        return
    # Not clamped to 1.0 — see _apply_spot_clone()'s own note on why Opacity > 100% is a
    # well-defined extrapolation past the seamlessClone result, not an error case.
    opacity = max(0.0, spot["opacity"] / 100.0)
    mask = np.zeros((h, w), dtype=np.uint8)
    cv2.circle(mask, (src_cx, src_cy), radius_px, 255, -1)
    try:
        bgr = arr[:, :, ::-1].copy()  # cv2 wants BGR
        result = cv2.seamlessClone(bgr, bgr, mask, (dst_cx, dst_cy), cv2.NORMAL_CLONE)
        blended = bgr.astype(np.float64) * (1 - opacity) + result.astype(np.float64) * opacity
        arr[:, :, :] = np.clip(blended, 0, 255).astype(np.uint8)[:, :, ::-1]
    except cv2.error:
        _apply_spot_clone(arr, spot, w, h)


def _render_local_adjustments_composite(work_dng_path, base_xmp_args, base_exposure, base_contrast,
                                         base_saturation, radial_filters, gradient_filters, spots,
                                         exiftool_path, dngconverter_path, side=None,
                                         lens_correction=None):
    """The actual base-render + per-region-render + composite work — see
    LocalAdjustmentRenderThread's own docstring for *why* this exists (Adobe DNG Converter's
    CLI does not apply local-adjustment masks in the previews/conversions it produces, see
    CLAUDE.md). A plain synchronous function so it can be called directly from
    save_to_dng()/export_preview_jpeg() (already synchronous, blocking calls) and reused
    unchanged inside LocalAdjustmentRenderThread.run() for the async live-preview pipeline.

    radial_filters/gradient_filters/spots must already be in the *rendered frame's own*
    fractions (i.e. already remapped for an active crop — see
    DNGForge._local_filters_for_render()); this function itself is crop-agnostic.

    `lens_correction`, if given (see compute_lensfun_correction()), is applied to the final
    composited image, once, after every region/spot has already been blended in — not to
    each region's own render separately. Lens distortion is a whole-frame geometric warp;
    warping each region before compositing would desync the alpha masks (defined in the
    rendered frame's own, pre-warp pixel coordinates) from the now-warped geometry.

    Returns the path to a composited JPEG (caller must delete it), or None on failure.

    Renders every needed frame (base + one per active region) IN PARALLEL, not one after
    another — added once profiling showed this was the other half (alongside the persistent-
    exiftool-process fix, see _persistent_exiftool_execute()) of DNGForge's per-edit
    sluggishness compared to RethinkRAW. Each render is an independent Adobe DNG Converter
    subprocess, so real OS-level parallelism is available (subprocess.run() releases the
    GIL while waiting) — measured on this project's own real gradient-filter test file
    (DSC_6751_prova.dng, 1 active region): a sequential base+region render took ~5.0s
    end-to-end through the real app; running both concurrently cut that to roughly half.
    """
    def is_noop(f):
        return f["exposure"] == 0 and f["contrast"] == 0 and f["saturation"] == 0

    def region_args(f):
        exposure = max(-5.0, min(5.0, base_exposure + f["exposure"]))
        contrast = max(-100, min(100, base_contrast + f["contrast"]))
        saturation = max(-100, min(100, base_saturation + f["saturation"]))
        return base_xmp_args + [
            f"-XMP-crs:Exposure2012={exposure:.3f}",
            f"-XMP-crs:Contrast2012={contrast:.0f}",
            f"-XMP-crs:Saturation={saturation:.0f}",
        ]

    # tasks[0] is always the base render; every following entry is one active (non-no-op)
    # radial/gradient region, in the same order they get alpha-blended in below — a single
    # source of truth for "which regions actually need a render", so the no-op skip logic
    # only ever lives in one place instead of needing to stay in sync across two passes.
    tasks = [{"xmp_args": base_xmp_args, "filter": None, "alpha_fn": None}]
    for f in radial_filters:
        if is_noop(f):
            continue  # no-op region — skip the extra render entirely
        tasks.append({"xmp_args": region_args(f), "filter": f, "alpha_fn": _radial_alpha_map})
    for f in gradient_filters:
        if is_noop(f):
            continue
        tasks.append({"xmp_args": region_args(f), "filter": f, "alpha_fn": _gradient_alpha_map})

    def render_one(xmp_args, own_work_dng_path):
        jpeg_path = _render_single_frame(own_work_dng_path, xmp_args, exiftool_path, dngconverter_path, side)
        if jpeg_path is None:
            return None
        try:
            return np.asarray(Image.open(jpeg_path).convert("RGB"), dtype=np.float64)
        finally:
            try:
                os.unlink(jpeg_path)
            except OSError:
                pass

    if len(tasks) == 1:
        results = [render_one(tasks[0]["xmp_args"], work_dng_path)]
    else:
        # Each concurrent render needs its OWN copy of work_dng_path: _render_single_frame()
        # writes that render's XMP tags directly onto whatever path it's given before
        # invoking Adobe DNG Converter, so two renders sharing the same file at once would
        # race — one thread's tags could be overwritten by another's before Adobe DNG
        # Converter ever reads them. Copying the small work DNG (well under 1MB) costs a few
        # milliseconds, trivial next to the ~0.6-0.9s Adobe DNG Converter itself takes.
        work_copies = [f"{work_dng_path}.{uuid.uuid4().hex[:8]}.tmp.dng" for _ in tasks]
        for copy_path in work_copies:
            shutil.copyfile(work_dng_path, copy_path)
        try:
            with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
                results = list(pool.map(
                    lambda i: render_one(tasks[i]["xmp_args"], work_copies[i]), range(len(tasks))
                ))
        finally:
            for copy_path in work_copies:
                try:
                    os.unlink(copy_path)
                except OSError:
                    pass

    base = results[0]
    if base is None:
        return None
    h, w = base.shape[:2]
    final = base

    for task, region in zip(tasks[1:], results[1:]):
        if region is None or region.shape != base.shape:
            continue  # a failed/mismatched region render is skipped, not fatal to the rest
        alpha = task["alpha_fn"](w, h, task["filter"])[..., None]
        final = final * (1 - alpha) + region * alpha

    final_u8 = np.clip(final, 0, 255).astype(np.uint8)
    for s in spots:
        try:
            if s["method"] == "Heal":
                _apply_spot_heal(final_u8, s, w, h)
            else:
                _apply_spot_clone(final_u8, s, w, h)
        except Exception:
            continue  # one bad spot region shouldn't sink the whole render

    out_path = os.path.join(os.path.dirname(work_dng_path), f"composited_{uuid.uuid4().hex[:8]}.jpg")
    Image.fromarray(final_u8, "RGB").save(out_path, quality=95)
    _apply_lensfun_correction(out_path, lens_correction)
    return out_path


class LocalAdjustmentRenderThread(QThread):
    """Renders the current edit state exactly like DngConverterRenderThread, then
    approximates every active radial/gradient/spot region on top of that base render —
    only ever instantiated when radial_filters/gradient_filters/spots is non-empty, so the
    common case (no local adjustments) keeps using the simpler, cheaper
    DngConverterRenderThread unchanged.

    CONFIRMED (this session): Adobe DNG Converter's CLI does not apply
    crs:CircularGradientBasedCorrections/GradientBasedCorrections/RetouchAreas in the
    previews/conversions it produces — verified exhaustively (this app's own struct, a
    struct copied byte-for-byte from a genuine Lightroom-saved file, Lightroom's exact XML
    serialization style injected as a raw XMP packet, and the newer LR-11-era
    MaskGroupBasedCorrections tag — all byte-identical to a render with no local adjustment
    at all, regardless of the correction's magnitude). Bringing back full local rendering
    (the pre-Adobe-only architecture) was rejected — that's exactly the "Tone sliders never
    matched Lightroom" dead end this project already went through. Photoshop was also
    considered (it has the real Camera Raw engine, so it would render these correctly) but
    the only copy available is Photoshop CS2 (2005) — years before Process Version 2012,
    the graduated filter (2010), the radial filter (2013), or the modern masking system
    (2022) existed, so it doesn't understand any of the tags this app writes at all.

    So instead: renders the SAME frame a second time per active radial/graduated region,
    with that region's own Exposure/Contrast/Saturation delta added to the *global* values
    (LocalExposure2012 etc. really is defined as a delta on top of the base state — this
    mirrors that definition exactly), then alpha-blends that second render into the base
    using the region's own mask geometry: an *exact* linear ramp for graduated filters
    (confirmed against a real Lightroom file, see CLAUDE.md "Graduated filter persistence")
    and an approximated feather falloff for radial filters (Feather's exact curve was never
    confirmed against Lightroom either way). Every blended pixel comes from a real Adobe
    render at one of its two endpoints (0% or 100% of the region's delta) — only the shape
    of the transition *between* those two endpoints is approximated (a linear blend in
    output-pixel space, not a re-derivation of Adobe's own tone-mapping curve). Spot
    removal needs no extra Adobe render at all — Heal/Clone are pure pixel-copy operations
    applied directly on the already-composited result (see _apply_spot_heal()/
    _apply_spot_clone()), the same algorithms this app used before the Adobe-only
    migration.

    The XMP written to the *original* file on save is completely unaffected by any of this
    — save_to_dng() still writes the real, Lightroom-verified CircularGradientBasedCorrections/
    GradientBasedCorrections/RetouchAreas structs (see _build_all_edit_xmp_args()); this
    thread only ever changes how DNGForge's *own* preview pixels are produced.
    """

    ready = Signal(str, int)   # jpeg_path, generation
    failed = Signal(int)       # generation

    def __init__(self, work_dng_path, base_xmp_args, base_exposure, base_contrast, base_saturation,
                 radial_filters, gradient_filters, spots, exiftool_path, dngconverter_path,
                 generation, side=None, lens_correction=None, parent=None):
        super().__init__(parent)
        self.work_dng_path = work_dng_path
        self.base_xmp_args = base_xmp_args
        self.base_exposure = base_exposure
        self.base_contrast = base_contrast
        self.base_saturation = base_saturation
        self.radial_filters = radial_filters
        self.gradient_filters = gradient_filters
        self.spots = spots
        self.exiftool_path = exiftool_path
        self.dngconverter_path = dngconverter_path
        self.generation = generation
        self.side = side
        self.lens_correction = lens_correction  # see _apply_lensfun_correction()

    def run(self):
        try:
            out_path = _render_local_adjustments_composite(
                self.work_dng_path, self.base_xmp_args, self.base_exposure, self.base_contrast,
                self.base_saturation, self.radial_filters, self.gradient_filters, self.spots,
                self.exiftool_path, self.dngconverter_path, self.side, self.lens_correction,
            )
            if out_path is None:
                self.failed.emit(self.generation)
            else:
                self.ready.emit(out_path, self.generation)
        except Exception:
            self.failed.emit(self.generation)


class DNGForge(QMainWindow):
    """DNGForge: open a DNG, edit non-destructively, preview live, embed edits back into
    the DNG on save. Every rendered pixel — preview and save alike — comes from Adobe DNG
    Converter, the sole renderer in this app (see CLAUDE.md, "Adobe DNG Converter is the
    sole renderer"); this class only ever builds edit-state XMP tags and hands them to it.
    Control panel layout (Profile / White Balance / Tone / Presence / Curve / Detail)
    deliberately mirrors RethinkRAW's, the reference implementation studied for this
    project (see project memory dngforge-rethinkraw-research). Lens Corrections is the
    one reference section not reproduced yet — it needs lensfunpy + a lens profile
    database, still phase 4 of the roadmap.
    """

    ZOOM_PRESETS = [None, 0.10, 0.25, 0.50, 0.75, 1.00, 1.50, 2.00, 3.00, 4.00]  # None = Fit
    UNDO_STACK_MAX = 20  # bounded history — "a few steps back," not an infinite undo log
    WORK_COPY_SIDE = 1920  # long edge of the normal live-preview work copy — a plain FullHD
    # screen's own long edge, not 2560; the higher-detail zoom pipeline (ZOOM_DETAIL_MAX_SIDE)
    # now covers anything sharper than that, so the work copy no longer needs headroom above a
    # typical display's own resolution just to make Fit/≤100% zoom look sharp (user's own
    # observation: "2560 px per la copia di lavoro forse è troppo... un normale schermo full hd
    # ha 1920px"). Smaller also means every debounced slider re-render (which runs against this
    # copy) has less work to do.
    ZOOM_DETAIL_MAX_SIDE = 2000  # long edge, in pixels, requested for the *zoomed-in region*
    # re-render described in "Zoom" below — an explicit user request ("un po' come fa
    # Lightroom... risoluzione sul lato lungo di circa 2000 pixel"), capped by the region's own
    # native pixel size so this never asks Adobe DNG Converter to invent detail that isn't there.
    ZOOM_DETAIL_THRESHOLD = 1.0  # zoom level above which the work copy's own resolution is
    # already being upscaled to display, and the higher-detail pipeline kicks in
    ZOOM_DETAIL_MIN_REQUEST_SIDE = 1600  # confirmed empirically: below roughly 1000-1500px,
    # Adobe DNG Converter silently stops embedding a full-size "JpgFromRaw" preview for a
    # cropped/small conversion and falls back to the much smaller, fixed-ratio "PreviewImage"
    # tier instead (~1/6 of the requested resolution, losing real detail — not a rendering
    # difference, purely which embedded preview slot gets populated). Always requesting at
    # least this much from Adobe reliably gets the real full-size render; the result is then
    # downscaled back down to the *correct*, native-capped size in _on_zoom_detail_render_ready()
    # — a display-only geometric resize (same Qt.SmoothTransformation already used for
    # Fit/zoom display everywhere else in this app), not a rendering decision, so this never
    # actually shows invented detail beyond what the sensor really has.

    def __init__(self):
        super().__init__()
        self.setWindowTitle("DNGForge")
        self.resize(1400, 900)

        self.raw = None
        self.raw_path = None
        self._preview_pixmap = None  # set only once the first Adobe render for the current
        # file lands (_on_dngconv_render_ready()) — there's no faster internal fallback to
        # show meanwhile, so this can legitimately stay None for a few seconds after
        # load_dng(). Initialized here (not just implicitly on first render) so every
        # reader can rely on the attribute existing, not just its value.
        self.camera_wb = None  # as-shot multipliers from the DNG itself
        self._lens_correction = None  # compute_lensfun_correction() result, or None — see load_dng()
        self._undo_stack = []       # past edit-state snapshots, oldest first — see undo()
        self._redo_stack = []       # states undone, most-recent-undone last — see redo()
        self._current_edit_state = None  # snapshot matching what's currently on screen
        self._restoring_state = False    # True while undo()/redo() is applying a snapshot,
        # so _maybe_push_undo_state() doesn't record the restore itself as a new change
        self._saved_edit_state = None  # snapshot matching what's on disk right now — see
        # load_dng()/save_to_dng() (sets it) and closeEvent() (compares against it)
        self.camera_profile = None  # DNGCameraProfile for this file, or None (falls back to kelvin_to_multipliers)
        self.exiftool_path = _locate_exiftool()
        self._zoom_level = None  # None = Fit to window; float = fraction of the *preview*
        # pixmap's own resolution (which may itself be capped by the Adobe work copy's own
        # 2560px long edge — this is not a true 1:1-raw-pixel zoom, see _update_image_label()).

        # Background Adobe DNG Converter render pipeline — the *only* rendering path in
        # this app (see CLAUDE.md, "Adobe DNG Converter is the sole renderer"). No internal
        # Python/cv2 fallback exists: if Adobe DNG Converter isn't installed,
        # _locate_dng_converter() returns None and load_dng() refuses to open any file
        # (see the check there) rather than silently rendering with an approximation.
        self._dngconv_path = _locate_dng_converter()
        self._work_dng_path = None       # small (2560px) reconvertible working copy
        self._work_dng_dir = None        # its temp directory, for cleanup
        self._workcopy_thread = None     # currently building the working copy, or None
        self._dngconv_thread = None      # currently re-rendering the working copy, or None
        self._dngconv_pending = False    # a change arrived while _dngconv_thread was running
        self._last_dngconv_state = None  # edit-state snapshot the last Adobe render was for
        self._dngconv_generation = 0     # bumped whenever a stale in-flight render should
        # be ignored (new file loaded) — checked against each thread's own captured value
        self._old_work_dirs = []  # work-copy temp dirs from previous files, swept up on close
        # (not deleted immediately on file switch — a background thread may still be
        # using one; safe to just let them accumulate for one session and clean up on exit)

        # "Default preview" toggle (zoom toolbar, next to the ⓘ XMP/EXIF/Metadata buttons) —
        # see _toggle_default_preview()/_start_default_preview_render(). A pure display swap:
        # renders the image with every control at reset_controls()'s own default values
        # EXCEPT crop/rotation (kept at the real, current self.crop), and shows that instead
        # of self._preview_pixmap until toggled off — never touches the actual widget state,
        # undo history, or the file itself.
        self._default_preview_active = False
        self._default_preview_pixmap = None
        self._default_preview_thread = None
        self._default_preview_generation = 0
        # Session edit cache (load_dng()) — path -> _capture_edit_state() snapshot, for
        # whichever file was open right before switching to a different one, so working
        # through a folder of files in the gallery filmstrip without saving each one doesn't
        # lose adjustments the moment another photo is opened. Deliberately in-memory only,
        # not written to disk — the same "usa e getta", session-only convention already used
        # for the gallery filmstrip and Copy/Paste Settings (self._settings_clipboard): this
        # is about not losing work while clicking between photos in one running session, not
        # about surviving an app crash or restart (which would need loading it back on next
        # launch too, a materially bigger feature nobody asked for). revert_to_saved() clears
        # a file's own entry before reloading it — see that method — so an explicit revert
        # can't be silently undone by this cache handing the discarded state right back.
        self._session_edit_cache = {}

        self._default_preview_src_paths = {}  # generation -> disposable work.dng copy the
        # in-flight thread for that generation is writing tags onto — can't reuse
        # self._work_dng_path directly, since the normal live pipeline can be writing to
        # it at the same moment (DngConverterRenderThread writes its XMP tags in place via
        # -overwrite_original). Keyed by generation, not a single shared path, for the same
        # reason _thumbnail_scan_dirs is (see that dict's own docstring in open_folder()) —
        # a shared attribute would let a second toggle started before the first one's
        # cleanup ran delete the *new* one's file instead of its own.

        # Gallery filmstrip (File → Apri cartella…, see open_folder()/FilmstripWidget) — see
        # ThumbnailScanThread's own docstring for why generation/scratch-dir cleanup are split
        # the way they are.
        self._gallery_generation = 0
        self._thumbnail_thread = None
        self._thumbnail_scan_dirs = {}  # generation -> scratch dir, see open_folder()

        # Copy/Paste Settings (Edit menu, Ctrl+Alt+C/V) — see copy_settings()/paste_settings().
        # Deliberately session-only, like the gallery filmstrip: nothing here is written to
        # disk or restored on next launch, just an in-memory _capture_edit_state() snapshot.
        self._settings_clipboard = None

        # Zoom detail rendering — see "Zoom" below. Lazily builds a full-resolution,
        # already-demosaiced copy the first time the user zooms in past the work copy's own
        # resolution (ZOOM_DETAIL_THRESHOLD), then re-renders just the currently visible
        # region from it (capped at ZOOM_DETAIL_MAX_SIDE) on every pan/zoom settle — same
        # generation-counter/single-in-flight-plus-pending queueing as the main preview
        # pipeline above, just a second, independent instance of that pattern.
        self._zoom_source_path = None      # full-res lossy-demosaiced copy, or None if not
        # built yet for this file (built into the same self._work_dng_dir as work.dng)
        self._zoom_source_thread = None    # currently building it, or None
        self._zoom_detail_thread = None    # currently re-rendering the current region, or None
        self._zoom_detail_pending = False
        self._zoom_detail_generation = 0
        self._last_zoom_detail_state = None  # (edit_state, region) the last detail render was for
        self._pending_zoom_detail_region = None  # region the in-flight thread was started with
        self._pending_zoom_detail_cap = None     # its correct (native-capped) target size —
        # see ZOOM_DETAIL_MIN_REQUEST_SIDE for why the actual Adobe request can exceed this
        self._pending_zoom_detail_native_capped = None  # True if that cap came from native
        # resolution, not ZOOM_DETAIL_MAX_SIDE — shown in the status bar once it lands
        self._zoom_detail_pixmap = None    # current high-res patch, or None — composited on top
        # of the normal (lower-res) preview in _update_image_label(), never replaces it outright
        self._zoom_detail_region = None    # fractions {left,top,right,bottom} of the *original
        # full raw frame* the current _zoom_detail_pixmap covers, or None
        self._zoom_detail_timer = QTimer(self)
        self._zoom_detail_timer.setSingleShot(True)
        self._zoom_detail_timer.timeout.connect(self._schedule_zoom_detail_render)

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

        if not self._dngconv_path:
            self._show_dngconv_missing_error()
        elif len(sys.argv) > 1 and os.path.isfile(sys.argv[1]):
            self.load_dng(sys.argv[1])

    def _show_dngconv_missing_error(self):
        """Adobe DNG Converter is a hard requirement — this app has no other rendering
        path (see CLAUDE.md, "Adobe DNG Converter is the sole renderer"). Shown once at
        startup if it's missing, and again from load_dng() every time opening a file is
        attempted, since the app genuinely cannot render anything without it.
        """
        QMessageBox.critical(
            self, "Adobe DNG Converter non trovato",
            "DNGForge richiede Adobe DNG Converter: è l'unico motore di rendering usato "
            "dall'app, non esiste più un algoritmo interno di riserva.\n\n"
            "Percorsi controllati:\n"
            r"C:\Program Files\Adobe\Adobe DNG Converter\Adobe DNG Converter.exe" "\n"
            r"C:\Program Files (x86)\Adobe\Adobe DNG Converter\Adobe DNG Converter.exe" "\n\n"
            "Installalo (gratuito, da Adobe) e riavvia DNGForge."
        )

    # ---- UI ----

    def _build_menu(self):
        open_action = QAction("Open DNG…", self)
        open_action.setShortcut("Ctrl+O")
        open_action.triggered.connect(self.open_dng)

        open_folder_action = QAction("Apri cartella…", self)
        open_folder_action.setShortcut("Ctrl+Shift+O")
        open_folder_action.triggered.connect(self.open_folder)

        save_action = QAction("Save", self)
        save_action.setShortcut("Ctrl+S")
        save_action.triggered.connect(self.save_to_dng)

        revert_action = QAction("Revert to Last Save", self)
        revert_action.triggered.connect(self.revert_to_saved)

        export_action = QAction("Export Preview as JPEG…", self)
        export_action.setShortcut("Ctrl+E")
        export_action.triggered.connect(self.export_preview_jpeg)

        # Close the currently open photo without quitting the app (distinct from Esci below)
        # — user-requested ("nel menu file vorrei un close (della foto) e un exit"), see
        # close_photo()'s own docstring.
        close_photo_action = QAction("Chiudi foto", self)
        close_photo_action.setShortcut("Ctrl+W")
        close_photo_action.triggered.connect(self.close_photo)

        # Reuses self.close() → closeEvent(), so Esci gets the exact same unsaved-changes
        # confirmation the window's own × button/Alt+F4 already have, instead of duplicating
        # that logic here.
        exit_action = QAction("Esci", self)
        exit_action.setShortcut("Ctrl+Q")
        exit_action.triggered.connect(self.close)

        file_menu = self.menuBar().addMenu("File")
        file_menu.addAction(open_action)
        file_menu.addAction(open_folder_action)
        file_menu.addAction(save_action)
        file_menu.addAction(export_action)
        file_menu.addSeparator()
        file_menu.addAction(revert_action)
        file_menu.addAction(close_photo_action)
        file_menu.addSeparator()
        file_menu.addAction(exit_action)

        undo_action = QAction("Undo", self)
        undo_action.setShortcut("Ctrl+Z")
        undo_action.triggered.connect(self.undo)

        redo_action = QAction("Redo", self)
        redo_action.setShortcut("Ctrl+Y")
        redo_action.triggered.connect(self.redo)

        copy_settings_action = QAction("Copia impostazioni", self)
        copy_settings_action.setShortcut("Ctrl+Alt+C")  # same shortcut real Lightroom uses
        copy_settings_action.triggered.connect(self.copy_settings)

        self.paste_settings_action = QAction("Incolla impostazioni", self)
        self.paste_settings_action.setShortcut("Ctrl+Alt+V")  # ditto
        self.paste_settings_action.triggered.connect(self.paste_settings)
        self.paste_settings_action.setEnabled(False)  # nothing copied yet this session

        edit_menu = self.menuBar().addMenu("Edit")
        edit_menu.addAction(undo_action)
        edit_menu.addAction(redo_action)
        edit_menu.addSeparator()
        edit_menu.addAction(copy_settings_action)
        edit_menu.addAction(self.paste_settings_action)

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
        self.image_label.panRequested.connect(self._on_pan_requested)

        self.image_scroll = QScrollArea()
        self.image_scroll.setWidgetResizable(True)
        self.image_scroll.setWidget(self.image_label)
        # Panning while zoomed past ZOOM_DETAIL_THRESHOLD should eventually bring the newly
        # visible area into high-res detail too — see "Zoom detail rendering" and
        # _schedule_zoom_detail_render(). Debounced the same way as edit changes (via
        # self._zoom_detail_timer), just a longer delay since a detail render is a real Adobe
        # DNG Converter conversion (hundreds of ms to ~1s), not a cheap comparison.
        self.image_scroll.horizontalScrollBar().valueChanged.connect(self._on_scroll_changed)
        self.image_scroll.verticalScrollBar().valueChanged.connect(self._on_scroll_changed)

        zoom_bar = QHBoxLayout()
        zoom_out_btn = QPushButton("−")
        zoom_out_btn.setFixedWidth(30)
        zoom_out_btn.clicked.connect(lambda: self._zoom_step(-1))
        zoom_in_btn = QPushButton("+")
        zoom_in_btn.setFixedWidth(30)
        zoom_in_btn.clicked.connect(lambda: self._zoom_step(1))
        self.zoom_combo = NoWheelComboBox()
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
        # "Default preview" toggle — shows the image as if every control were at its
        # reset_controls() default, keeping the real crop/rotation, so the user can quickly
        # compare "what the file actually looked like before editing" (user-requested,
        # explicitly on/off toggle logic like a before/after view) — see
        # _toggle_default_preview()/_start_default_preview_render().
        self.default_preview_btn = QPushButton("👁 Default")
        self.default_preview_btn.setCheckable(True)
        self.default_preview_btn.setToolTip(
            "Mostra l'anteprima con tutti i controlli ai valori di default "
            "(crop/raddrizzamento esclusi, restano quelli attuali) — on/off"
        )
        self.default_preview_btn.clicked.connect(self._toggle_default_preview)
        zoom_bar.addWidget(self.default_preview_btn)
        # Two separate debug-info buttons, split at the user's own request once they realized
        # a freshly-downloaded (never edited) DNG has no -XMP-crs:* data at all — the editing
        # "recipe" (XMP-crs) and the camera's own capture/calibration data (EXIF: ColorMatrix,
        # AsShotNeutral, Make/Model/Lens, ...) are two genuinely different kinds of tags, so one
        # combined dump was conflating two questions ("what did I edit" vs "what does the camera
        # say about this shot") into one. See _show_xmp_info()/_show_exif_info().
        self.info_xmp_btn = QPushButton("ⓘ XMP")
        self.info_xmp_btn.setToolTip("Mostra i tag -XMP-crs:* (la 'ricetta' di editing) salvati nel file")
        self.info_xmp_btn.clicked.connect(self._show_xmp_info)
        zoom_bar.addWidget(self.info_xmp_btn)
        self.info_exif_btn = QPushButton("ⓘ EXIF")
        self.info_exif_btn.setToolTip(
            "Mostra i tag EXIF (calibrazione/cattura della fotocamera: ColorMatrix, "
            "AsShotNeutral, Make/Model/Lens, ...) salvati nel file"
        )
        self.info_exif_btn.clicked.connect(self._show_exif_info)
        zoom_bar.addWidget(self.info_exif_btn)
        self.info_metadata_btn = QPushButton("ⓘ Metadata")
        self.info_metadata_btn.setToolTip(
            "Mostra i campi metadata di sola lettura (dimensioni, esposizione, ISO, "
            "fotocamera/obiettivo, ...) — ex-pannello 'Metadata' della sidebar"
        )
        self.info_metadata_btn.clicked.connect(self._show_metadata_info)
        zoom_bar.addWidget(self.info_metadata_btn)

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

        # Filmstrip (File → Apri cartella…, see FilmstripWidget/open_folder()) spans the full
        # window width at the bottom, below both the image and the control panel — Lightroom's
        # own filmstrip placement, not just under the image, so it stays visible regardless of
        # which develop-panel section is scrolled into view on the right. Hidden until a folder
        # is actually opened (FilmstripWidget.set_photos([]) keeps it that way).
        central = QWidget()
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)
        central_layout.addWidget(splitter, 1)
        self.filmstrip = FilmstripWidget()
        self.filmstrip.photoActivated.connect(self.load_dng)
        central_layout.addWidget(self.filmstrip)

        self.setCentralWidget(central)
        self.statusBar().showMessage("Pronto")

    def _build_controls(self):
        """Panel structure mirrors real Lightroom's actual Develop-panel boundaries — user-
        requested ("vorrei clonare il layout di lightroom"), a step further than the earlier
        RethinkRAW-inspired flat section order (see this method's own git history / earlier
        CLAUDE.md notes): Basic, Tone Curve, HSL/Color, Split Toning, Detail and Effects are
        now single merged panels, each containing the same controls that used to be separate
        top-level group boxes, exactly matching where Lightroom itself draws its own panel
        boundaries. Sub-sections inside each merged panel keep their own inner QGroupBox
        border rather than switching to Lightroom's borderless divider style — this app
        already uses QGroupBox as its one section-boundary convention everywhere, and
        stripping those inner borders would be a materially bigger, riskier change for a
        purely cosmetic gain.

        Collapse defaults were chosen per sub-section, not per merged panel, specifically to
        preserve the granular "start collapsed" behavior already requested for Parametric
        Curve / Defringe / Post-Crop Vignette (see CLAUDE.md) even though they now live
        inside a bigger always-expanded outer panel — a CollapsibleGroupBox nested inside
        another CollapsibleGroupBox works correctly (the outer one's collapse just
        shows/hides the inner box wholesale, without touching the inner box's own checked
        state), so nothing about that earlier per-control tuning needed to be lost in the
        merge. Radial/Graduated Filter and Spot Removal are DNGForge's own on-canvas tools with
        no equivalent permanent side-panel section in real Lightroom (there they're toolbar
        icons overlaid on the canvas, not Develop-panel entries), so they're left after the
        Lightroom-mirrored panels above. Crop & Straighten is the one on-canvas tool NOT left
        there — moved inside the Basic panel, directly above White Balance, at the user's
        explicit request (workflow order: straighten/crop the frame first, then judge white
        balance/tone against the final framing, not the other way around).
        """
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setAlignment(Qt.AlignTop)

        # Histogram — top of the right-hand panel, same placement as real Lightroom's own
        # Develop-module histogram (see HistogramWidget's own docstring). Fed exclusively from
        # DNGForge._on_dngconv_render_ready(), the main preview pipeline's single choke point —
        # not built/updated here.
        self.histogram_widget = HistogramWidget()
        layout.addWidget(self.histogram_widget)

        # ================================================================
        # Basic — Lightroom's own Basic panel merges Profile, White Balance, Tone and
        # Presence into one single panel; none of the four are independently collapsible
        # there, so none are here either — this outer box is the only collapse toggle.
        # ================================================================
        basic_group = CollapsibleGroupBox("Basic", start_expanded=True)
        basic_layout = QVBoxLayout(basic_group)

        # Profile
        profile_group = QGroupBox("Profile")
        profile_layout = QVBoxLayout(profile_group)
        self.profile_combo = NoWheelComboBox()
        self.profile_combo.addItems([
            "Adobe Color", "Adobe Monochrome", "Adobe Landscape", "Adobe Neutral",
            "Adobe Portrait", "Adobe Vivid", "Adobe Standard", "Adobe Standard B&W", "Custom",
        ])
        self.profile_combo.currentIndexChanged.connect(self._schedule_update)
        profile_layout.addWidget(self.profile_combo)
        basic_layout.addWidget(profile_group)

        # Crop & Straighten (roadmap items 8-9) — only ever affects the rendered preview/JPEG,
        # never self.raw or the DNG's own pixel data, same non-destructive convention as every
        # other adjustment (confirmed with the user explicitly). Straighten's Rotation slider
        # auto-clamps the crop rectangle to stay inside the rotated frame's valid area — see
        # clamp_crop_to_rotation()/max_inscribed_rect(). Placed right above White Balance, not
        # after the Lightroom-mirrored panels below (its original position) — user-requested
        # ("sposta crop straighten subito sopra white balance perché è una delle prime
        # operazioni che si fa"): straightening/cropping the frame is naturally done before
        # judging white balance/tone on the final framing, not after.
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
        self.crop_aspect_combo = NoWheelComboBox()
        self.crop_aspect_combo.addItems(list(CROP_ASPECT_RATIOS.keys()))
        self.crop_aspect_combo.currentIndexChanged.connect(self._on_crop_aspect_changed)
        cc_layout.addWidget(self.crop_aspect_combo)
        self.crop_rotation_slider = Slider("Rotation", -45, 45, 0, step=0.1, decimals=1,
                                            on_change=self._on_crop_rotation_changed)
        cc_layout.addWidget(self.crop_rotation_slider)
        self.crop_controls.setVisible(False)
        crop_layout.addWidget(self.crop_controls)

        basic_layout.addWidget(crop_group)

        # White Balance
        wb_group = QGroupBox("White Balance")
        wb_layout = QVBoxLayout(wb_group)

        self.wb_mode = NoWheelComboBox()
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

        basic_layout.addWidget(wb_group)

        # Basic Corrections (Exposure/Contrast) — split out of Tone into its own group,
        # user-requested: conceptually these are whole-image adjustments the user wants to
        # set once and keep, independent of the Tone panel's Auto/Default/Eye Perception/
        # Custom recipe-cycling ("il tone 'default' non deve toccare i valori di exposure e
        # contrast... ecco perché è importante spostare questi due valori fuori da tone").
        # Their own on_change is a plain _schedule_update() (like Presence's sliders), not
        # _on_tone_slider_changed() — nudging them no longer flips tone_mode to "Custom",
        # since they're no longer part of what that combo tracks at all. Still disabled
        # together with the Tone sliders while Tone Mode is Auto (_set_tone_controls_enabled())
        # — AutoTone is a single XMP concept covering the whole basic recipe (Adobe computes
        # Exposure2012/Contrast2012 too, not just Highlights/Shadows/Whites/Blacks), a tag-
        # level fact that doesn't change just because the UI groups them differently now.
        basic_corrections_group = QGroupBox("Basic Corrections")
        basic_corrections_layout = QVBoxLayout(basic_corrections_group)
        self.exposure_slider = Slider("Exposure", -5, 5, 0, decimals=2, on_change=self._schedule_update)
        self.contrast_slider = Slider("Contrast", -100, 100, 0, on_change=self._schedule_update)
        for s in (self.exposure_slider, self.contrast_slider):
            basic_corrections_layout.addWidget(s)
        basic_layout.addWidget(basic_corrections_group)

        # Tone
        tone_group = QGroupBox("Tone")
        tone_layout = QVBoxLayout(tone_group)

        self.tone_mode = NoWheelComboBox()
        self.tone_mode.addItems(["Auto", "Default", "Eye Perception", "Custom"])
        self.tone_mode.setCurrentText("Default")
        self.tone_mode.currentIndexChanged.connect(self._on_tone_mode_changed)
        tone_layout.addWidget(self.tone_mode)

        self.highlights_slider = Slider("Highlights", -100, 100, 0, on_change=self._on_tone_slider_changed)
        self.shadows_slider = Slider("Shadows", -100, 100, 0, on_change=self._on_tone_slider_changed)
        self.whites_slider = Slider("Whites", -100, 100, 0, on_change=self._on_tone_slider_changed)
        self.blacks_slider = Slider("Blacks", -100, 100, 0, on_change=self._on_tone_slider_changed)
        for s in (self.highlights_slider, self.shadows_slider, self.whites_slider, self.blacks_slider):
            tone_layout.addWidget(s)
        basic_layout.addWidget(tone_group)

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
        basic_layout.addWidget(presence_group)

        layout.addWidget(basic_group)

        # ================================================================
        # Tone Curve — merges the point-curve editor and the parametric-curve region
        # sliders into one panel, matching Lightroom's own single "Tone Curve" panel (its
        # graph view and region-slider view are two faces of the same panel there too,
        # never independently collapsible). Parametric stays its own nested
        # CollapsibleGroupBox so it keeps starting collapsed within this panel, same as
        # before the merge (see this method's own docstring for why nesting works).
        # ================================================================
        curve_panel_group = CollapsibleGroupBox("Tone Curve", start_expanded=True)
        curve_panel_layout = QVBoxLayout(curve_panel_group)

        curve_group = QGroupBox("Point Curve")
        curve_layout = QVBoxLayout(curve_group)
        self.curve_combo = NoWheelComboBox()
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

        curve_panel_layout.addWidget(curve_group)

        # Parametric (roadmap item 19) — real top-to-bottom slider order is Highlights,
        # Lights, Darks, Shadows (bright-to-dark, matching the Basic panel's own
        # convention) — confirmed against XMP.pm and cross-checked with DSC_6751.dng,
        # which also confirms the 3 split points default to 25/50/75, not 0: they're
        # meaningful position markers even in an otherwise-untouched state.
        parametric_group = CollapsibleGroupBox("Parametric", start_expanded=False)
        parametric_layout = QVBoxLayout(parametric_group)
        self.parametric_highlights_slider = Slider("Highlights", -100, 100, 0, on_change=self._schedule_update)
        self.parametric_lights_slider = Slider("Lights", -100, 100, 0, on_change=self._schedule_update)
        self.parametric_darks_slider = Slider("Darks", -100, 100, 0, on_change=self._schedule_update)
        self.parametric_shadows_slider = Slider("Shadows", -100, 100, 0, on_change=self._schedule_update)
        self.parametric_shadow_split_slider = Slider("Shadow Split", 0, 100, 25, on_change=self._schedule_update)
        self.parametric_midtone_split_slider = Slider("Midtone Split", 0, 100, 50, on_change=self._schedule_update)
        self.parametric_highlight_split_slider = Slider("Highlight Split", 0, 100, 75, on_change=self._schedule_update)
        for s in (self.parametric_highlights_slider, self.parametric_lights_slider,
                  self.parametric_darks_slider, self.parametric_shadows_slider,
                  self.parametric_shadow_split_slider, self.parametric_midtone_split_slider,
                  self.parametric_highlight_split_slider):
            parametric_layout.addWidget(s)
        curve_panel_layout.addWidget(parametric_group)

        layout.addWidget(curve_panel_group)

        # ================================================================
        # HSL / Color — merges HSL/Color and Black & White into ONE panel, matching real
        # Lightroom's actual "HSL / Color / B&W" panel: the B&W toggle lives inside the
        # panel itself (not a separate box), and only swaps which sliders are visible —
        # HSL in Color mode, Gray Mixer in B&W mode — rather than hiding the whole panel
        # the way the previous two-separate-boxes version did. See
        # _update_hsl_bw_content_visibility() for the swap logic and why it also needs to
        # run after this panel's own expand/collapse, not only on the checkbox itself.
        # ================================================================
        self.hsl_group = CollapsibleGroupBox("HSL / Color", start_expanded=False)
        hsl_outer_layout = QVBoxLayout(self.hsl_group)

        # Black & White (roadmap item 16) — a standalone treatment toggle, independent of
        # Profile: real Lightroom lets you hit "Black & White" regardless of which color
        # profile is active, same ConvertToGrayscale tag the "Adobe Standard B&W"/"Adobe
        # Monochrome" profile entries already set (see PROFILE_LOOK_SETTINGS) — this checkbox
        # is just a second, independent way to set the same flag, exactly like real Lightroom
        # has both a profile-driven and a treatment-button-driven path to the same tag. See
        # _build_xmp_crs_args() for how the two are reconciled on write.
        self.bw_checkbox = QCheckBox("Convert to Black & White")
        self.bw_checkbox.toggled.connect(self._on_bw_toggled)
        hsl_outer_layout.addWidget(self.bw_checkbox)

        # HSL/Color (roadmap item 20) — Lightroom's real panel has 3 tabs (Hue/Saturation/
        # Luminance) over the same 8 colors; this flat layout uses 3 nested group boxes
        # instead of tabs, same 24 tags either way. Wrapped in one QWidget so the whole
        # trio can be shown/hidden together as Color/B&W mode swaps.
        self.hsl_color_content = QWidget()
        hsl_color_layout = QVBoxLayout(self.hsl_color_content)
        hsl_color_layout.setContentsMargins(0, 0, 0, 0)
        self.hsl_hue_sliders = {}
        self.hsl_saturation_sliders = {}
        self.hsl_luminance_sliders = {}
        for title, store in (
            ("Hue", self.hsl_hue_sliders),
            ("Saturation", self.hsl_saturation_sliders),
            ("Luminance", self.hsl_luminance_sliders),
        ):
            sub_group = QGroupBox(title)
            sub_layout = QVBoxLayout(sub_group)
            for color in HSL_COLORS:
                s = Slider(color, -100, 100, 0, on_change=self._schedule_update)
                store[color] = s
                sub_layout.addWidget(s)
            hsl_color_layout.addWidget(sub_group)
        hsl_outer_layout.addWidget(self.hsl_color_content)

        self.gray_mixer_group = QGroupBox("Gray Mixer")
        gray_mixer_layout = QVBoxLayout(self.gray_mixer_group)
        self.gray_mixer_sliders = {}
        for color in HSL_COLORS:
            s = Slider(color, -100, 100, 0, on_change=self._schedule_update)
            self.gray_mixer_sliders[color] = s
            gray_mixer_layout.addWidget(s)
        hsl_outer_layout.addWidget(self.gray_mixer_group)

        layout.addWidget(self.hsl_group)
        # CollapsibleGroupBox's own _on_toggled() has no notion of "these two children are
        # mutually exclusive" — on every expand it would show HSL content AND Gray Mixer at
        # once. Connected *after* construction, so it always runs right after the base
        # handler and corrects that.
        self.hsl_group.toggled.connect(lambda _checked: self._update_hsl_bw_content_visibility())

        # ================================================================
        # Split Toning — unchanged, single panel already matching Lightroom's real
        # structure (Color Grading in modern Lightroom reuses these exact same tags).
        # ================================================================
        split_group = CollapsibleGroupBox("Split Toning", start_expanded=False)
        split_layout = QVBoxLayout(split_group)
        self.split_shadow_hue_slider = Slider("Shadow Hue", 0, 360, 0, on_change=self._schedule_update)
        self.split_shadow_sat_slider = Slider("Shadow Saturation", 0, 100, 0, on_change=self._schedule_update)
        self.split_highlight_hue_slider = Slider("Highlight Hue", 0, 360, 0, on_change=self._schedule_update)
        self.split_highlight_sat_slider = Slider("Highlight Saturation", 0, 100, 0, on_change=self._schedule_update)
        self.split_balance_slider = Slider("Balance", -100, 100, 0, on_change=self._schedule_update)
        for s in (self.split_shadow_hue_slider, self.split_shadow_sat_slider,
                  self.split_highlight_hue_slider, self.split_highlight_sat_slider,
                  self.split_balance_slider):
            split_layout.addWidget(s)
        layout.addWidget(split_group)

        # ================================================================
        # Detail — merges Sharpening, Noise Reduction and Defringe into one panel,
        # matching Lightroom's real "Detail" panel (Defringe sits at the bottom of that
        # same panel there too, not a separate top-level one — see CLAUDE.md item 18).
        # Defringe stays its own nested CollapsibleGroupBox so it keeps starting collapsed
        # within this panel, same as before the merge.
        # ================================================================
        detail_group = CollapsibleGroupBox("Detail", start_expanded=True)
        detail_layout = QVBoxLayout(detail_group)

        # Sharpening + Noise Reduction — restructured into Lightroom's real two-group,
        # 4+6-slider layout at the user's request ("la sezione... dovrebbe essere
        # sharpening (amount - radius - detail - masking) e noise reduction (luminance -
        # detail - contrast - color - detail - smoothness)") — every tag name confirmed
        # against exiftool's own XMP.pm crs table, not guessed (Sharpen*/
        # LuminanceNoiseReduction*/ColorNoiseReduction*), and SharpenRadius's real-world
        # value cross-checked against the genuine Lightroom-saved DSC_6751.dng (+1.0 —
        # matches the domain-mandated default chosen below, not an arbitrary number).
        sharpen_group = QGroupBox("Sharpening")
        sharpen_layout = QVBoxLayout(sharpen_group)
        self.sharpness_slider = Slider("Amount", 0, 150, 0, on_change=self._schedule_update)
        self.sharpen_radius_slider = Slider("Radius", 0.5, 3.0, 1.0, decimals=1, on_change=self._schedule_update)
        self.sharpen_detail_slider = Slider("Detail", 0, 100, 0, on_change=self._schedule_update)
        self.sharpen_masking_slider = Slider("Masking", 0, 100, 0, on_change=self._schedule_update)
        for s in (self.sharpness_slider, self.sharpen_radius_slider,
                  self.sharpen_detail_slider, self.sharpen_masking_slider):
            sharpen_layout.addWidget(s)
        detail_layout.addWidget(sharpen_group)

        noise_group = QGroupBox("Noise Reduction")
        noise_layout = QVBoxLayout(noise_group)
        self.luminance_nr_slider = Slider("Luminance", 0, 100, 0, on_change=self._schedule_update)
        self.luminance_nr_detail_slider = Slider("Luminance Detail", 0, 100, 0, on_change=self._schedule_update)
        self.luminance_nr_contrast_slider = Slider("Luminance Contrast", 0, 100, 0, on_change=self._schedule_update)
        self.color_nr_slider = Slider("Color", 0, 100, 0, on_change=self._schedule_update)
        self.color_nr_detail_slider = Slider("Color Detail", 0, 100, 0, on_change=self._schedule_update)
        self.color_nr_smoothness_slider = Slider("Color Smoothness", 0, 100, 0, on_change=self._schedule_update)
        for s in (self.luminance_nr_slider, self.luminance_nr_detail_slider, self.luminance_nr_contrast_slider,
                  self.color_nr_slider, self.color_nr_detail_slider, self.color_nr_smoothness_slider):
            noise_layout.addWidget(s)
        detail_layout.addWidget(noise_group)

        # Defringe (roadmap item 18) — Amount's real Lightroom range is 0-20 (unlike every
        # other 0-100 "Amount" slider in this app) — confirmed against XMP.pm; HueLo/HueHi
        # default to 30/70 (Purple) and 40/60 (Green), not 0 — real, meaningful hue-band
        # defaults confirmed directly from DSC_6751.dng's own untouched values, not
        # guessed placeholders.
        defringe_group = CollapsibleGroupBox("Defringe", start_expanded=False)
        defringe_layout = QVBoxLayout(defringe_group)
        self.defringe_purple_amount_slider = Slider("Purple Amount", 0, 20, 0, on_change=self._schedule_update)
        self.defringe_purple_hue_lo_slider = Slider("Purple Hue Lo", 0, 100, 30, on_change=self._schedule_update)
        self.defringe_purple_hue_hi_slider = Slider("Purple Hue Hi", 0, 100, 70, on_change=self._schedule_update)
        self.defringe_green_amount_slider = Slider("Green Amount", 0, 20, 0, on_change=self._schedule_update)
        self.defringe_green_hue_lo_slider = Slider("Green Hue Lo", 0, 100, 40, on_change=self._schedule_update)
        self.defringe_green_hue_hi_slider = Slider("Green Hue Hi", 0, 100, 60, on_change=self._schedule_update)
        for s in (self.defringe_purple_amount_slider, self.defringe_purple_hue_lo_slider,
                  self.defringe_purple_hue_hi_slider, self.defringe_green_amount_slider,
                  self.defringe_green_hue_lo_slider, self.defringe_green_hue_hi_slider):
            defringe_layout.addWidget(s)
        detail_layout.addWidget(defringe_group)

        layout.addWidget(detail_group)

        # Lens Corrections (roadmap item 4) — UI mirrors RethinkRAW's own implementation exactly
        # (xmp.go/raw-editor.gohtml): just two plain booleans, LensProfileEnable/AutoLateralCA,
        # no lens-ID mapping in the XMP-writing side. AutoLateralCA renders correctly through
        # Adobe DNG Converter's own CLI. LensProfileEnable's own geometric/vignetting correction
        # is a confirmed no-op in that CLI (isolated flag testing, cross-checked against
        # RethinkRAW's own live server — see "Lens Corrections — LensProfileEnable is a confirmed
        # no-op..." in CLAUDE.md), so this app applies that correction itself, locally, via
        # lensfunpy after Adobe's own render — see compute_lensfun_correction()/
        # _apply_lensfun_correction() and CLAUDE.md's "Lens Corrections: local lensfunpy-based
        # rendering" for the implementation.
        lens_group = QGroupBox("Lens Corrections")
        lens_layout = QVBoxLayout(lens_group)
        self.lens_profile_checkbox = QCheckBox("Enable Profile Corrections")
        self.lens_profile_checkbox.toggled.connect(self._schedule_update)
        self.lateral_ca_checkbox = QCheckBox("Remove Chromatic Aberration")
        self.lateral_ca_checkbox.toggled.connect(self._schedule_update)
        lens_layout.addWidget(self.lens_profile_checkbox)
        lens_layout.addWidget(self.lateral_ca_checkbox)
        # Status of the lensfunpy camera+lens database lookup (see lensfun_lookup_with_status())
        # — previously the checkbox gave no indication at all of whether it had anything to
        # actually apply for the currently loaded file. Wraps rather than truncates since the
        # matched camera/lens names can be long.
        self.lens_match_label = QLabel("")
        self.lens_match_label.setWordWrap(True)
        self.lens_match_label.setStyleSheet("color: gray; font-size: 11px;")
        lens_layout.addWidget(self.lens_match_label)
        layout.addWidget(lens_group)

        # ================================================================
        # Effects — merges Post-Crop Vignette and Grain into one panel, matching
        # Lightroom's real "Effects" panel (Vignetting listed above Grain there). Vignette
        # stays its own nested CollapsibleGroupBox so it keeps starting collapsed within
        # this panel, same as before the merge.
        # ================================================================
        effects_group = CollapsibleGroupBox("Effects", start_expanded=True)
        effects_layout = QVBoxLayout(effects_group)

        # Post-Crop Vignette (roadmap item 15) — plus one Style combo
        # (crs:PostCropVignetteStyle is an int enum, see VIGNETTE_STYLE_VALUES).
        vignette_group = CollapsibleGroupBox("Post-Crop Vignette", start_expanded=False)
        vignette_layout = QVBoxLayout(vignette_group)
        self.vignette_amount_slider = Slider("Amount", -100, 100, 0, on_change=self._schedule_update)
        self.vignette_midpoint_slider = Slider("Midpoint", 0, 100, 50, on_change=self._schedule_update)
        self.vignette_feather_slider = Slider("Feather", 0, 100, 50, on_change=self._schedule_update)
        self.vignette_roundness_slider = Slider("Roundness", -100, 100, 0, on_change=self._schedule_update)
        for s in (self.vignette_amount_slider, self.vignette_midpoint_slider,
                  self.vignette_feather_slider, self.vignette_roundness_slider):
            vignette_layout.addWidget(s)
        style_row = QHBoxLayout()
        style_row.addWidget(QLabel("Style"))
        self.vignette_style_combo = NoWheelComboBox()
        self.vignette_style_combo.addItems(list(VIGNETTE_STYLE_VALUES.keys()))
        self.vignette_style_combo.currentIndexChanged.connect(self._schedule_update)
        style_row.addWidget(self.vignette_style_combo)
        vignette_layout.addLayout(style_row)
        effects_layout.addWidget(vignette_group)

        # Grain (roadmap item 14) — flat -XMP-crs:Grain* tags, same "just sliders" pattern
        # as Presence/Detail above; Adobe DNG Converter renders it, nothing local to do.
        grain_group = QGroupBox("Grain")
        grain_layout = QVBoxLayout(grain_group)
        self.grain_amount_slider = Slider("Amount", 0, 100, 0, on_change=self._schedule_update)
        self.grain_size_slider = Slider("Size", 0, 100, 25, on_change=self._schedule_update)
        self.grain_frequency_slider = Slider("Frequency", 0, 100, 50, on_change=self._schedule_update)
        for s in (self.grain_amount_slider, self.grain_size_slider, self.grain_frequency_slider):
            grain_layout.addWidget(s)
        effects_layout.addWidget(grain_group)

        layout.addWidget(effects_group)

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
        # CLAUDE.md "Graduated filter persistence" for why a linear gradient needs only
        # two points, unlike the radial filter's ellipse.
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
        self.spot_method_combo = NoWheelComboBox()
        self.spot_method_combo.addItems(["Heal", "Clone"])
        self.spot_method_combo.currentIndexChanged.connect(self._on_spot_slider_changed)
        sc_layout.addWidget(self.spot_method_combo)
        self.spot_opacity_slider = Slider("Opacity", 0, 200, 100, on_change=self._on_spot_slider_changed)
        # Range extended past Lightroom's usual 0-100 at the user's explicit request ("si
        # potrebbe aumentare l'intensità... magari anche molto più marcato, poi regolo io
        # l'intensità") — both _apply_spot_heal()/_apply_spot_clone() already do a plain
        # linear blend between the original and the healed/cloned result, so opacity > 100%
        # is a well-defined extrapolation *past* that result (pushed further in the same
        # direction), not an error case — see those functions' own docstrings.
        sc_layout.addWidget(self.spot_opacity_slider)
        self.spot_controls.setVisible(False)
        spot_layout.addWidget(self.spot_controls)

        layout.addWidget(spot_group)

        # Metadata — read-only, exact field list/order requested by the user (roadmap item 10).
        # Used to live as its own always-visible sidebar QGroupBox; folded into a third ⓘ
        # debug-info button (_show_metadata_info(), next to XMP/EXIF in the zoom toolbar) at the
        # user's own suggestion, once XMP/EXIF had already proven that pattern out — these 16
        # fields are read-only camera data nothing in this app ever edits, so there's no benefit
        # to keeping them permanently on screen versus one click away like the other two debug
        # dumps. self.metadata_values (populated by _update_metadata_panel(), called from
        # load_dng()) replaces the old self.metadata_labels dict of QLabel widgets — plain
        # strings are all a dialog needs, no widgets to keep updated in a layout no longer built.
        self.metadata_values = {}

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
        elif mode in ("As Shot", "Camera Matching…", "Auto") and self.camera_wb:
            # CONFIRMED BUG, fixed: Auto used to show its own local gray-world guess
            # (_compute_auto_wb()/compute_gray_world_neutral(), now removed) here — but Adobe
            # DNG Converter computes Auto WB with its own undisclosed algorithm that this app
            # has no access to, so that guess was a genuinely different, independently-derived
            # number, not a preview of what Auto actually renders. Measured directly on a file
            # the user flagged as behaving oddly (RAW_LEICA_M8_prova.DNG): the gray-world
            # guess's own Custom-equivalent render differed from the real Auto render by
            # mean|diff|~22 (max 254 out of 255) — a wholesale mismatch, not a rounding gap,
            # because a whole-image gray-world average is easily thrown off by any photo that
            # lacks genuinely neutral content, exactly what happened here. RethinkRAW (this
            # project's reference implementation) never synthesizes a number for Auto/As
            # Shot/Camera Matching at all — it just leaves the field at whatever the file's own
            # (usually absent/zero) tag holds, disabled — confirmed by reading its
            # whiteBalanceChange() in assets/raw-editor.js. Showing the camera's own As-Shot
            # reading here instead is a middle ground: still a real, non-fabricated number (the
            # exact same accurate calibration math already used for As Shot itself, verified
            # elsewhere to match Adobe's own As-Shot render almost exactly), and empirically a
            # much closer real-world proxy for Auto than an invented gray-world guess — a real
            # Adobe render of Auto vs. As Shot on this same file differed by mean|diff|~17, well
            # under half the gray-world guess's own error, consistent with the user's own
            # side-by-side observation that RethinkRAW's Auto and As-Shot renders looked similar.
            temp, tint = self._multipliers_to_temp_tint(self.camera_wb)
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
            # Default to "Original" (the raw's own aspect ratio), not Freeform —
            # user-requested. A fresh crop already starts as the full 0..1 frame, which by
            # definition already *is* the original aspect ratio in fraction space (any
            # target-ratio's frac_ratio = ratio*h/w reduces to exactly 1.0 when
            # ratio == w/h, see _apply_crop_aspect_ratio()) — so this only needs to *arm*
            # the constraint for the next drag, not reshape anything.
            self.crop_aspect_combo.blockSignals(True)
            self.crop_aspect_combo.setCurrentText("Original")
            self.crop_aspect_combo.blockSignals(False)
            self.image_label.crop_aspect = 1.0
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
            # Deliberately does NOT touch Exposure/Contrast — user-requested: those moved
            # out to their own "Basic Corrections" group specifically so Tone's own
            # Auto/Default/Eye Perception/Custom cycling never resets them (see that
            # group's own __init__/build comment for the full rationale).
            self.highlights_slider.set_value(0)
            self.shadows_slider.set_value(0)
            self.whites_slider.set_value(0)
            self.blacks_slider.set_value(0)
            # Also clears the HSL corrections "Eye Perception" sets (Red/Orange Saturation,
            # Red Luminance) — user-requested, same "always write/reset the off-state too"
            # convention already used throughout this app (HasCrop, WhiteBalance, Black &
            # White, ...): switching from Eye Perception back to Default must not leave
            # those stranded, since they exist specifically to compensate for Eye
            # Perception's own Presence boost and mean nothing once that's gone.
            self.hsl_saturation_sliders["Red"].set_value(0)
            self.hsl_saturation_sliders["Orange"].set_value(0)
            self.hsl_luminance_sliders["Red"].set_value(0)
        elif mode == "Eye Perception":
            # Tone: perceptual/local-adaptation dynamic-range compression, see CLAUDE.md's
            # "Vivace" / "Eye Perception" writeup for the full rationale — this is not a
            # literal "increase contrast" reading of these four sliders.
            self.highlights_slider.set_value(-50)
            self.shadows_slider.set_value(50)
            self.whites_slider.set_value(-50)
            self.blacks_slider.set_value(50)
            # Presence: restores the local contrast/punch the tone compression above gave up —
            # user-requested, same "flatten globally, punch back locally" pairing already
            # documented for the tone values themselves.
            self.texture_slider.set_value(10)
            self.clarity_slider.set_value(10)
            self.dehaze_slider.set_value(10)
            self.vibrance_slider.set_value(10)
            self.saturation_slider.set_value(10)
            # HSL: reds are usually the first channel to clip when Presence is pushed (least
            # headroom in most camera color science) — user-observed, corrected selectively
            # here rather than pulling back Vibrance/Saturation globally.
            self.hsl_saturation_sliders["Red"].set_value(-10)
            self.hsl_luminance_sliders["Red"].set_value(-10)
            # Orange carries skin tones — pushed Vibrance/Saturation without a correction
            # here reliably turns skin an unnatural orange/carrot tone ("altrimenti la pelle
            # viene a carota", user-observed as needed almost every time), the same class of
            # per-channel correction as the Red one above, just for a different failure mode.
            self.hsl_saturation_sliders["Orange"].set_value(-20)
            # Curve: an S-curve adds midtone contrast — a third, complementary way of
            # recovering vivacity (midtones) alongside Presence (local detail/haze) and the
            # HSL Red correction (color) — user-requested as the final piece of the recipe.
            self.curve_combo.setCurrentText("Strong Contrast")
        self._set_tone_controls_enabled(mode != "Auto")
        self._schedule_update()

    def _on_tone_slider_changed(self):
        if self.tone_mode.currentText() in ("Default", "Eye Perception"):
            self.tone_mode.blockSignals(True)
            self.tone_mode.setCurrentText("Custom")
            self.tone_mode.blockSignals(False)
        self._schedule_update()

    def _on_curve_mode_changed(self):
        is_custom = self.curve_combo.currentText() == "Custom"
        self.curve_editor.setVisible(is_custom)
        self.curve_reset_btn.setVisible(is_custom)
        self._schedule_update()

    def _on_bw_toggled(self, checked):
        """Mirrors real Lightroom: HSL/Color and Black & White are never both meaningful at
        once (Hue/Saturation are no-ops on a fully desaturated image), so only one slider
        group is shown inside the merged HSL/Color panel at a time — same idea as
        Lightroom's own "HSL / Color / B&W" panel swapping its slider content, not hiding
        the whole panel the way an earlier two-separate-boxes version of this app did."""
        self._update_hsl_bw_content_visibility()
        self._schedule_update()

    def _update_hsl_bw_content_visibility(self):
        """Shows the HSL sliders (Color mode) or the Gray Mixer sliders (B&W mode) inside
        the merged HSL/Color panel — never both, matching real Lightroom's own single
        "HSL / Color / B&W" panel. Also called after the panel's own expand/collapse
        (connected to self.hsl_group's `toggled` signal in _build_controls()):
        CollapsibleGroupBox's generic _on_toggled() has no notion of "these two children
        are mutually exclusive", so on every re-expand it would otherwise show both HSL and
        Gray Mixer at once — this runs immediately after it to correct that.
        """
        if not self.hsl_group.isChecked():
            return  # panel collapsed — _on_toggled() already hid everything, nothing to fix
        is_bw = self.bw_checkbox.isChecked()
        self.hsl_color_content.setVisible(not is_bw)
        self.gray_mixer_group.setVisible(is_bw)

    # ---- File I/O ----

    def open_dng(self):
        path, _ = QFileDialog.getOpenFileName(self, "Apri DNG", "", "DNG files (*.dng);;Tutti i file (*.*)")
        if path:
            self.load_dng(path)

    def open_folder(self):
        """File → Apri cartella… (Ctrl+Shift+O): scans a folder for .dng files and populates
        the filmstrip (FilmstripWidget) — a disposable, per-session gallery, not a persistent
        catalog (see that class's own docstring for why). Thumbnails themselves are filled in
        asynchronously by ThumbnailScanThread so a folder with many files doesn't freeze the UI
        while exiftool works through it.
        """
        folder = QFileDialog.getExistingDirectory(self, "Apri cartella")
        if not folder:
            return
        try:
            names = os.listdir(folder)
        except OSError as e:
            QMessageBox.critical(self, "Apertura cartella non riuscita", str(e))
            return
        paths = sorted(
            os.path.join(folder, n) for n in names if os.path.splitext(n)[1].lower() == ".dng"
        )
        self.filmstrip.set_photos(paths)
        if not paths:
            self.statusBar().showMessage(f"Nessun file .dng trovato in {os.path.basename(folder)}")
            return

        self._gallery_generation += 1
        generation = self._gallery_generation
        scratch = tempfile.mkdtemp(prefix="dngforge_thumbs_")
        self._thumbnail_scan_dirs[generation] = scratch
        self._thumbnail_thread = ThumbnailScanThread(paths, self.exiftool_path, scratch, generation, parent=self)
        self._thumbnail_thread.thumbnail_ready.connect(self._on_thumbnail_ready)
        self._thumbnail_thread.finished_scan.connect(self._on_thumbnail_scan_finished)
        self._thumbnail_thread.start()
        self.statusBar().showMessage(f"{len(paths)} file .dng trovati in {os.path.basename(folder)} — caricamento anteprime…")

    def _on_thumbnail_ready(self, dng_path, thumb_path, generation):
        if generation != self._gallery_generation:
            return  # a stale result from a folder scan the user has since replaced
        pix = QPixmap()
        if thumb_path:
            loaded = QPixmap(thumb_path)
            if not loaded.isNull():
                pix = loaded.scaled(
                    FilmstripWidget.THUMB_SIZE, FilmstripWidget.THUMB_SIZE,
                    Qt.KeepAspectRatio, Qt.SmoothTransformation,
                )
            try:
                os.unlink(thumb_path)  # already decoded into pix above, the file itself is
                # no longer needed — deleting it here (not in the scan thread, see that
                # class's own docstring) avoids a cross-thread delete-before-read race
            except OSError:
                pass
        self.filmstrip.set_thumbnail(dng_path, pix)

    def _on_thumbnail_scan_finished(self, generation):
        if generation == self._gallery_generation:
            self.statusBar().showMessage("Anteprime caricate")
        # Each generation's own scratch dir is tracked independently (not a single shared
        # attribute) precisely so that opening a *second* folder while an earlier scan is
        # still running can't lose track of the earlier one's directory — every generation
        # gets cleaned up exactly once, whenever its own thread actually finishes, regardless
        # of how many newer scans have started in the meantime.
        scratch = self._thumbnail_scan_dirs.pop(generation, None)
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)

    def load_dng(self, path):
        if not self._dngconv_path:
            self._show_dngconv_missing_error()
            return

        # Session edit cache (user-requested, "quando lavoro con una cartella di file... così
        # quando passo da una foto all'altra senza aver salvato non perdo gli aggiustamenti
        # fatti") — stash whatever's currently on screen for the file we're switching *away*
        # from, keyed by its own path, before it gets torn down below. In-memory and
        # session-only, the same "usa e getta" convention already used for the gallery
        # filmstrip/Copy-Paste Settings — see self._session_edit_cache's own __init__ comment
        # for why a persistent on-disk store wasn't built (nothing here needs to survive a
        # crash or an app restart, only switching between photos within one running session).
        # Skipped on a same-path reload (revert_to_saved() calling load_dng(self.raw_path)
        # again) — revert_to_saved() itself clears that path's entry instead, since re-caching
        # the about-to-be-discarded state here would just silently undo the revert.
        if self.raw_path is not None and self.raw_path != path:
            self._session_edit_cache[self.raw_path] = self._capture_edit_state()

        if self.raw is not None:
            self.raw.close()

        self.statusBar().showMessage(f"Apertura di {os.path.basename(path)}…")
        QApplication.processEvents()

        t0 = time.time()
        self.raw = rawpy.imread(path)
        self.raw_path = path
        self.filmstrip.set_current(path)  # no-op if path isn't part of the current gallery
        self._undo_stack = []
        self._redo_stack = []
        self._current_edit_state = None
        self._set_zoom_level(None)  # back to Fit for a newly opened file
        self._reset_dngconv_pipeline(path)

        wb = list(self.raw.camera_whitebalance)
        if not wb or wb[0] <= 0:
            wb = list(self.raw.daylight_whitebalance)
        self.camera_wb = wb
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
        # Reuses metadata_values (already fetched for the Metadata panel, including
        # Composite:LensID — see compute_lensfun_correction()'s own docstring for why that
        # specific field matters) rather than a fresh exiftool call, matching this file's own
        # "one combined read per file load" discipline.
        self._lens_correction, lens_status_text = lensfun_lookup_with_status(self.metadata_values)
        self.lens_match_label.setText(lens_status_text)
        self.lens_match_label.setStyleSheet(
            "color: #2e7d32; font-size: 11px;" if self._lens_correction else "color: gray; font-size: 11px;"
        )
        # Baseline for the unsaved-changes check in closeEvent() — "what's on disk right now"
        # for a freshly (re)opened file is whatever state was just restored above, saved or
        # default alike. Captured *before* the session-cache restore below, on purpose: the
        # dirty-check must still compare against the real on-disk state, so a file reopened
        # with unsaved-this-session edits correctly shows as dirty again (it is).
        self._saved_edit_state = self._capture_edit_state()
        restored_from_session = path in self._session_edit_cache
        if restored_from_session:
            self._apply_edit_state(self._session_edit_cache[path])  # overrides the
            # just-restored (saved-on-disk-or-default) state with whatever was on screen
            # for this same file earlier in this session — see load_dng()'s own top-of-
            # function comment for why this exists and revert_to_saved() for the one path
            # that deliberately bypasses it
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
        if restored_from_session:
            edit_state += " — modifiche non salvate di questa sessione ripristinate"
        self.statusBar().showMessage(
            f"{os.path.basename(path)} — {self.raw.sizes.width}x{self.raw.sizes.height} "
            f"(caricato in {time.time() - t0:.2f}s, {colorimetry}){edit_state}"
        )

    def _update_metadata_panel(self, path):
        self.metadata_values = read_metadata(
            self.exiftool_path, path, self.raw.sizes.width, self.raw.sizes.height
        )

    def _show_xmp_info(self):
        """User-requested debug tool ("puoi implementare un tasto info... così anche io posso
        aiutare col debug? una cosa del genere c'è in rethinkraw") — the editing "recipe" half
        of the split (see _show_exif_info() for the other half, and its own docstring for why
        they're two separate buttons/dialogs rather than one combined dump). Scoped to
        `-XMP-crs:all` — the actual Camera Raw develop-settings namespace, not every XMP
        namespace in the file (xmpMM provenance, dc:format, etc. aren't "what did I edit").

        CONFIRMED BUG, caught before this ever shipped: `-XMP-crs:all` alone silently omits
        some tags entirely — `WhiteBalance` and `ColorTemperature` specifically (confirmed
        missing with or without `-struct`, and regardless of whether the wildcard is scoped to
        the crs group or left at the broader `-XMP:all` — group-scoping the wildcard doesn't
        fix it), almost certainly because exiftool marks them `Avoid => 1` in its own crs table
        (ambiguous short names that collide with same-named tags in other groups) and skips
        Avoid-flagged tags from *any* wildcard group request unless named explicitly. A tool
        whose whole purpose is "show me everything so I can debug" silently hiding the exact
        tag that caused the bug it was built to help diagnose (see "Slider label vs. actual
        value" above — this app's own ColorTemperature bug) would be a real problem, not a
        cosmetic one. Fixed by also explicitly requesting every tag in `XMP_CRS_TAGS` (the same
        list every other read in this app already keys off) alongside the wildcard.
        """
        self._show_tag_info_dialog(
            "Tag XMP-crs (editing)",
            ["-struct", "-XMP-crs:all"] + [f"-XMP-crs:{t}" for t in XMP_CRS_TAGS],
            "(nessun tag -XMP-crs:* trovato in questo file — probabilmente un file mai "
            "modificato da Lightroom/Camera Raw/DNGForge, vedi CLAUDE.md 'Virgin-file defaults')",
        )

    def _show_exif_info(self):
        """The camera/capture-data half of the debug-info split (see _show_xmp_info() for the
        editing-recipe half) — added right after the user asked what to call "questi altri tag"
        (ColorMatrix1/2, CalibrationIlluminant1/2, AsShotNeutral, Make/Model/Lens, ...): they're
        the standard `EXIF` group exiftool itself already uses (confirmed by inspecting real
        output — every one of those tags prints under the `[EXIF]` prefix), so that's the label
        used here too, not an invented name. Unlike XMP-crs, plain `-EXIF:all` doesn't drop any
        of this app's own relevant tags (verified directly against RAW_LEICA_M8.DNG — no
        Avoid-flag omission here), so no explicit-tag-list workaround is needed.
        """
        self._show_tag_info_dialog(
            "Tag EXIF (fotocamera)", ["-EXIF:all"],
            "(nessun tag EXIF trovato in questo file)",
        )

    def _show_metadata_info(self):
        """Third ⓘ debug-info button, alongside XMP/EXIF — folds the former always-visible
        sidebar "Metadata" QGroupBox (16 read-only fields, METADATA_FIELDS) into the same
        on-demand dialog pattern, at the user's own suggestion once XMP/EXIF had already proven
        it out. Unlike its two siblings, this doesn't re-run exiftool: every field here is
        read-only camera capture data nothing in this app ever writes to, so whatever
        self.metadata_values already holds (populated by _update_metadata_panel() at load time,
        via the same print-converted exiftool read the old panel used) is exactly what a fresh
        disk read would show too — no reason to pay for a second exiftool spawn just to get the
        same answer.
        """
        if self.raw_path is None:
            return
        lines = [f"{field}: {self.metadata_values.get(field, '—')}" for field in METADATA_FIELDS]
        self._show_text_dialog("Metadata", "\n".join(lines))

    def _show_tag_info_dialog(self, title, exiftool_args, empty_message):
        """Shared by _show_xmp_info()/_show_exif_info(): runs exiftool with `-G -s -n` (this
        app's usual machine-parsable convention) plus whatever group-scoped args the caller
        wants, against the file *on disk* right now — not this app's own in-memory edit
        state, so it reflects reality regardless of unsaved changes — and shows the raw
        output via the shared _show_text_dialog() dialog.
        """
        if self.raw_path is None:
            return
        if not self.exiftool_path:
            QMessageBox.critical(self, title, "exiftool non trovato: impossibile leggere i tag.")
            return
        try:
            result = subprocess.run(
                [self.exiftool_path, "-G", "-s", "-n"] + exiftool_args + [self.raw_path],
                capture_output=True, text=True, timeout=30, creationflags=_NO_WINDOW_FLAGS,
            )
        except Exception as e:
            QMessageBox.critical(self, title, f"Lettura exiftool non riuscita: {e}")
            return
        text = result.stdout.strip() or empty_message
        if result.stderr.strip():
            text += "\n\n--- stderr ---\n" + result.stderr.strip()
        self._show_text_dialog(title, text)

    def _show_text_dialog(self, title, text):
        """Shared read-only, monospace, copy-to-clipboard-able dialog body used by every ⓘ
        debug-info button (XMP/EXIF/Metadata) — only the title and text differ per caller.
        """
        dialog = QDialog(self)
        dialog.setWindowTitle(f"{title} — {os.path.basename(self.raw_path)}")
        dialog.resize(700, 600)
        layout = QVBoxLayout(dialog)

        editor = QPlainTextEdit()
        editor.setReadOnly(True)
        editor.setFont(QFont("Consolas", 9))
        editor.setPlainText(text)
        layout.addWidget(editor)

        button_row = QHBoxLayout()
        copy_btn = QPushButton("Copia negli appunti")
        copy_btn.clicked.connect(lambda: QApplication.clipboard().setText(text))
        close_btn = QPushButton("Chiudi")
        close_btn.clicked.connect(dialog.accept)
        button_row.addWidget(copy_btn)
        button_row.addStretch(1)
        button_row.addWidget(close_btn)
        layout.addLayout(button_row)

        dialog.exec()

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
        self.parametric_highlights_slider.set_value(0)
        self.parametric_lights_slider.set_value(0)
        self.parametric_darks_slider.set_value(0)
        self.parametric_shadows_slider.set_value(0)
        self.parametric_shadow_split_slider.set_value(25)
        self.parametric_midtone_split_slider.set_value(50)
        self.parametric_highlight_split_slider.set_value(75)
        for store in (self.hsl_hue_sliders, self.hsl_saturation_sliders, self.hsl_luminance_sliders,
                      self.gray_mixer_sliders):
            for s in store.values():
                s.set_value(0)
        self.bw_checkbox.blockSignals(True)
        self.bw_checkbox.setChecked(False)
        self.bw_checkbox.blockSignals(False)
        self._update_hsl_bw_content_visibility()
        self.split_shadow_hue_slider.set_value(0)
        self.split_shadow_sat_slider.set_value(0)
        self.split_highlight_hue_slider.set_value(0)
        self.split_highlight_sat_slider.set_value(0)
        self.split_balance_slider.set_value(0)
        # Sharpening/Noise Reduction defaults below are Lightroom's own real out-of-the-box
        # recipe for a raw file, not this app's usual "everything off" convention — see
        # "Virgin-file defaults match Lightroom, not zero" in CLAUDE.md for the project-wide
        # rule and per-field confidence level (SharpenRadius=1.0 and the Parametric-Curve/
        # Defringe defaults elsewhere in this function are hard-confirmed against a genuine
        # Lightroom-saved file, DSC_6751.dng; Amount=40/Color=25/Color Detail=50/Luminance
        # Detail=50 below are well-established Lightroom documentation values, not
        # independently confirmed against a local reference file the way the others are).
        self.sharpness_slider.set_value(40)
        self.sharpen_radius_slider.set_value(1.0)  # Lightroom's Radius range excludes 0
        # (0.5-3.0) — 1.0 is the neutral in-range default, confirmed against a real
        # Lightroom-saved file's own unedited value (DSC_6751.dng: SharpenRadius=+1.0)
        self.sharpen_detail_slider.set_value(25)
        self.sharpen_masking_slider.set_value(0)
        self.luminance_nr_slider.set_value(0)
        self.luminance_nr_detail_slider.set_value(50)
        self.luminance_nr_contrast_slider.set_value(0)
        self.color_nr_slider.set_value(25)
        self.color_nr_detail_slider.set_value(50)
        self.color_nr_smoothness_slider.set_value(0)
        self.defringe_purple_amount_slider.set_value(0)
        self.defringe_purple_hue_lo_slider.set_value(30)
        self.defringe_purple_hue_hi_slider.set_value(70)
        self.defringe_green_amount_slider.set_value(0)
        self.defringe_green_hue_lo_slider.set_value(40)
        self.defringe_green_hue_hi_slider.set_value(60)
        # Both default to checked, not unchecked — real Lightroom's own out-of-the-box recipe,
        # confirmed against DSC_6751.dng's own untouched LensProfileEnable/AutoLateralCA values
        # (both 1), same "virgin-file defaults match Lightroom, not zero" rule as Sharpening/NR
        # above.
        self.lens_profile_checkbox.setChecked(True)
        self.lateral_ca_checkbox.setChecked(True)
        self.grain_amount_slider.set_value(0)
        self.grain_size_slider.set_value(25)
        self.grain_frequency_slider.set_value(50)
        self.vignette_amount_slider.set_value(0)
        self.vignette_midpoint_slider.set_value(50)
        self.vignette_feather_slider.set_value(50)
        self.vignette_roundness_slider.set_value(0)
        self.vignette_style_combo.setCurrentIndex(0)  # Highlight Priority
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
        # Named profiles round-trip via LookName (see PROFILE_LOOK_SETTINGS/"CONFIRMED BUGS,
        # fixed: Curve and Profile" in CLAUDE.md) — CameraProfile is always cleared for every
        # named profile now, so it's only a meaningful signal for files this app didn't write
        # itself. "Adobe Standard" and "Custom" are indistinguishable on read (both clear
        # LookName/CameraProfile/ConvertToGrayscale, and both render identically — no Look
        # applied either way), so an ambiguous read resolves to "Adobe Standard", matching
        # RethinkRAW's own same-shaped resolution rather than guessing "Custom".
        look_name = state.get("LookName")
        profile = state.get("CameraProfile")
        grayscale = str(state.get("ConvertToGrayscale")).strip().lower() in ("true", "1")
        if look_name and self.profile_combo.findText(str(look_name)) >= 0:
            self.profile_combo.setCurrentText(str(look_name))
        elif grayscale:
            self.profile_combo.setCurrentText("Adobe Standard B&W")
        elif profile and self.profile_combo.findText(str(profile)) >= 0:
            self.profile_combo.setCurrentText(str(profile))
        else:
            self.profile_combo.setCurrentText("Adobe Standard")

        # Black & White is a standalone toggle independent of Profile (see its own UI-setup
        # comment) — reflects the file's real ConvertToGrayscale value regardless of which
        # profile the block above resolved to, since either path (a B&W-flavored profile, or
        # this checkbox) can be what actually set it.
        self.bw_checkbox.setChecked(grayscale)

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
            (self.saturation_slider, "Saturation"),
            (self.parametric_highlights_slider, "ParametricHighlights"),
            (self.parametric_lights_slider, "ParametricLights"),
            (self.parametric_darks_slider, "ParametricDarks"),
            (self.parametric_shadows_slider, "ParametricShadows"),
            (self.parametric_shadow_split_slider, "ParametricShadowSplit"),
            (self.parametric_midtone_split_slider, "ParametricMidtoneSplit"),
            (self.parametric_highlight_split_slider, "ParametricHighlightSplit"),
            (self.split_shadow_hue_slider, "SplitToningShadowHue"),
            (self.split_shadow_sat_slider, "SplitToningShadowSaturation"),
            (self.split_highlight_hue_slider, "SplitToningHighlightHue"),
            (self.split_highlight_sat_slider, "SplitToningHighlightSaturation"),
            (self.split_balance_slider, "SplitToningBalance"),
            (self.sharpness_slider, "Sharpness"),
            (self.sharpen_radius_slider, "SharpenRadius"), (self.sharpen_detail_slider, "SharpenDetail"),
            (self.sharpen_masking_slider, "SharpenEdgeMasking"),
            (self.luminance_nr_slider, "LuminanceSmoothing"),
            (self.luminance_nr_detail_slider, "LuminanceNoiseReductionDetail"),
            (self.luminance_nr_contrast_slider, "LuminanceNoiseReductionContrast"),
            (self.color_nr_slider, "ColorNoiseReduction"),
            (self.color_nr_detail_slider, "ColorNoiseReductionDetail"),
            (self.color_nr_smoothness_slider, "ColorNoiseReductionSmoothness"),
            (self.defringe_purple_amount_slider, "DefringePurpleAmount"),
            (self.defringe_purple_hue_lo_slider, "DefringePurpleHueLo"),
            (self.defringe_purple_hue_hi_slider, "DefringePurpleHueHi"),
            (self.defringe_green_amount_slider, "DefringeGreenAmount"),
            (self.defringe_green_hue_lo_slider, "DefringeGreenHueLo"),
            (self.defringe_green_hue_hi_slider, "DefringeGreenHueHi"),
            (self.grain_amount_slider, "GrainAmount"), (self.grain_size_slider, "GrainSize"),
            (self.grain_frequency_slider, "GrainFrequency"),
            (self.vignette_amount_slider, "PostCropVignetteAmount"),
            (self.vignette_midpoint_slider, "PostCropVignetteMidpoint"),
            (self.vignette_feather_slider, "PostCropVignetteFeather"),
            (self.vignette_roundness_slider, "PostCropVignetteRoundness"),
        ):
            value = state.get(key)
            if value is not None:
                slider.set_value(float(value))

        for prefix, store in (
            ("HueAdjustment", self.hsl_hue_sliders),
            ("SaturationAdjustment", self.hsl_saturation_sliders),
            ("LuminanceAdjustment", self.hsl_luminance_sliders),
            ("GrayMixer", self.gray_mixer_sliders),
        ):
            for color, slider in store.items():
                value = state.get(f"{prefix}{color}")
                if value is not None:
                    slider.set_value(float(value))

        style_val = state.get("PostCropVignetteStyle")
        if style_val is not None:
            try:
                style_name = VIGNETTE_STYLE_NAMES.get(int(float(style_val)))
            except (TypeError, ValueError):
                style_name = None
            if style_name:
                self.vignette_style_combo.setCurrentText(style_name)

        for checkbox, key in (
            (self.lens_profile_checkbox, "LensProfileEnable"),
            (self.lateral_ca_checkbox, "AutoLateralCA"),
        ):
            value = state.get(key)
            if value is not None:
                checkbox.setChecked(str(value).strip() in ("1", "True", "true"))

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

    def _multipliers_to_temp_tint(self, mult):
        """Inverse of the multiplier calc above, for display on the (disabled) sliders.
        Prefers the real DNGCameraProfile when available, same as the forward direction.

        Display-only: never fed into rendering (Adobe DNG Converter computes its own Auto WB
        when -XMP-crs:WhiteBalance=Auto is written — see _build_xmp_crs_args()) — this exists
        purely so the (disabled) Temperature/Tint sliders show a plausible reading instead of a
        stale/default one.
        """
        r, g, b = mult[0], mult[1] or 1.0, mult[2]
        if self.camera_profile is not None:
            neutral = [1.0 / r if r else 1.0, 1.0 / g, 1.0 / b if b else 1.0]
            try:
                return self.camera_profile.neutral_to_temperature_tint(neutral)
            except Exception:
                pass  # fall through to the approximation below
        return multipliers_to_kelvin_tint(r, g, b)

    def _effective_crop_for_render(self, for_save: bool = False):
        """While the crop guide is being edited, the preview shows the *full* straightened
        frame (so the user can see the whole rotated image behind the crop rectangle
        overlay) rather than the actual cropped result — same "guide vs committed"
        distinction Lightroom's own Crop tool uses. Saving always applies the real crop
        regardless of whether the guide happens to be showing at the time. Consumed by
        _build_xmp_crs_args() (in place of self.crop directly) so the Adobe-rendered
        preview honors this distinction, not just a since-removed local render path.
        """
        if not self.crop:
            return None
        if self._crop_editing and not for_save:
            return {"angle": self.crop["angle"], "left": 0.0, "top": 0.0, "right": 1.0, "bottom": 1.0}
        return self.crop

    def _local_filters_for_render(self, for_save: bool = False):
        """radial_filters/gradient_filters/spots are stored as fractions of the *full raw
        frame* (same convention as HasCrop's own Top/Left/Bottom/Right), but Adobe DNG
        Converter only ever renders the *effective crop* (see _effective_crop_for_render())
        — so LocalAdjustmentRenderThread/_render_local_adjustments_composite(), which work
        entirely in the rendered frame's own pixel space, need these remapped into that
        frame's own fractions first, the same transform _region_within_effective_crop()
        already does for the zoom-detail pipeline's region (points scale/translate by the
        crop's own left/top/width/height; lengths — rx/ry/radius — scale only).

        Returns None when compositing isn't possible right now — a rotated effective crop
        (mapping a sub-rectangle back through a rotation isn't handled, same scope limit
        _region_within_effective_crop() already has) or a degenerate crop — signaling the
        caller to fall back to the plain DngConverterRenderThread/single-render path
        instead of showing a misplaced region. Otherwise returns (radial, gradient, spots),
        each freshly deep-copied so the caller can hand them to a background thread safely.
        """
        crop = self._effective_crop_for_render(for_save)
        if crop and crop.get("angle"):
            return None
        if not crop:
            return (copy.deepcopy(self.radial_filters), copy.deepcopy(self.gradient_filters),
                    copy.deepcopy(self.spots))
        cw, ch = crop["right"] - crop["left"], crop["bottom"] - crop["top"]
        if cw <= 0 or ch <= 0:
            return None

        def px(x):
            return (x - crop["left"]) / cw

        def py(y):
            return (y - crop["top"]) / ch

        radial = []
        for f in self.radial_filters:
            g = dict(f)
            g["cx"], g["cy"] = px(f["cx"]), py(f["cy"])
            g["rx"], g["ry"] = f["rx"] / cw, f["ry"] / ch
            radial.append(g)

        gradient = []
        for f in self.gradient_filters:
            g = dict(f)
            g["zero_x"], g["zero_y"] = px(f["zero_x"]), py(f["zero_y"])
            g["full_x"], g["full_y"] = px(f["full_x"]), py(f["full_y"])
            gradient.append(g)

        spots = []
        for s in self.spots:
            g = dict(s)
            g["dest_x"], g["dest_y"] = px(s["dest_x"]), py(s["dest_y"])
            g["src_x"], g["src_y"] = px(s["src_x"]), py(s["src_y"])
            g["radius"] = s["radius"] / cw  # radius is a width-fraction, same convention as rx
            spots.append(g)

        return radial, gradient, spots

    def _render_preview(self):
        """Schedules the one and only rendering path in this app: a background Adobe DNG
        Converter render reflecting the current edit state (see CLAUDE.md, "Adobe DNG
        Converter is the sole renderer" — every pixel ever shown comes from Adobe's own
        engine, never an internal Python/cv2 approximation). Nothing is drawn
        synchronously here — the display only changes once _on_dngconv_render_ready()
        delivers the next real render, so a freshly opened file (or one whose first
        render for the current edit state is still in flight) simply shows nothing yet,
        with the status bar carrying the "rendering…" feedback meanwhile.
        """
        if self.raw is None:
            return
        self._maybe_push_undo_state()
        self._schedule_dngconv_render()
        self._schedule_zoom_detail_render()  # keep the zoomed-in detail patch, if any, in
        # sync with edit changes too — not just zoom/pan (see "Zoom detail rendering" below)

    # ---- Background Adobe-render preview pipeline ----

    def _reset_dngconv_pipeline(self, path):
        """Called from load_dng(): invalidates any in-flight background render (via the
        generation counter — safe even if a thread from the *previous* file is still
        running, its eventual result just gets ignored) and kicks off building a fresh
        small working copy for the newly opened file. The `if not self._dngconv_path`
        guard below is unreachable in practice — load_dng() already refuses to open any
        file when Adobe DNG Converter isn't installed — kept only as defense in depth.
        """
        self._dngconv_generation += 1
        self._work_dng_path = None
        self._last_dngconv_state = None
        self._dngconv_pending = False
        # Zoom detail state is per-file (the zoom source itself is built from this file's own
        # raw data) — reset it the same way as the main pipeline above, one generation bump
        # covering both so a stale in-flight thread from the *previous* file can't land here.
        self._zoom_source_path = None
        self._zoom_detail_pending = False
        self._zoom_detail_generation += 1
        self._last_zoom_detail_state = None
        self._zoom_detail_pixmap = None
        self._zoom_detail_region = None
        # Default Preview toggle is per-file too — its pixmap belongs to whichever file was
        # open when it was rendered, and a stale in-flight thread from the *previous* file
        # is invalidated by the same generation-bump pattern as the two pipelines above.
        self._default_preview_active = False
        self._default_preview_pixmap = None
        self._default_preview_generation += 1
        if hasattr(self, "default_preview_btn"):
            self.default_preview_btn.blockSignals(True)
            self.default_preview_btn.setChecked(False)
            self.default_preview_btn.blockSignals(False)
        if self._work_dng_dir is not None:
            self._old_work_dirs.append(self._work_dng_dir)
            self._work_dng_dir = None
        if not self._dngconv_path:
            return
        work_dir = tempfile.mkdtemp(prefix="dngforge_work_")
        self._work_dng_dir = work_dir
        self._workcopy_thread = DngConverterWorkCopyThread(
            self._dngconv_path, path, work_dir, "work.dng", self.WORK_COPY_SIDE, self
        )
        self._workcopy_thread.ready.connect(self._on_workcopy_ready)
        self._workcopy_thread.failed.connect(self._on_workcopy_failed)
        self._workcopy_thread.start()

    def _on_workcopy_ready(self, work_dng_path):
        self._workcopy_thread = None
        self._work_dng_path = work_dng_path
        self._schedule_dngconv_render()  # the file may already have settled edits by now

    def _on_workcopy_failed(self):
        self._workcopy_thread = None  # Adobe-render pipeline just stays inactive for this file

    def _ensure_zoom_source(self):
        """Kicks off building the full-resolution zoom-detail source the first time it's
        needed for the current file (idempotent — a no-op if already built or already
        building). See the "Zoom detail rendering" __init__ comment and "Zoom" in CLAUDE.md.
        """
        if self._zoom_source_path or self._zoom_source_thread is not None:
            return
        if not self._dngconv_path or not self.raw_path or self._work_dng_dir is None:
            return
        self._zoom_source_thread = DngConverterWorkCopyThread(
            self._dngconv_path, self.raw_path, self._work_dng_dir, "zoom_source.dng", None, self
        )
        self._zoom_source_thread.ready.connect(self._on_zoom_source_ready)
        self._zoom_source_thread.failed.connect(self._on_zoom_source_failed)
        self._zoom_source_thread.start()

    def _on_zoom_source_ready(self, path):
        self._zoom_source_thread = None
        self._zoom_source_path = path
        self._schedule_zoom_detail_render()  # the viewport may have moved on while this built

    def _on_zoom_source_failed(self):
        self._zoom_source_thread = None  # zoom detail just stays unavailable for this file —
        # the normal (lower-res, but already-working) preview keeps showing regardless, same
        # graceful-degradation the rest of this pipeline already relies on

    def _sync_spinner(self):
        """The spinner overlay (ImageLabel.start_spinner()/stop_spinner()) is active exactly
        when any of the three background render pipelines — the main preview one below, the
        zoom-detail one (see "Zoom detail rendering"), or the Default Preview toggle's own
        one-off render — currently has a thread in flight: a plain OR of all three
        pipelines' own state, recomputed fresh on every call rather than paired start/stop
        calls kept in sync by hand. That matters because the main/zoom-detail pipelines
        settle straight into a queued re-render without an intervening "stop" (see the
        pending/generation queueing in _on_dngconv_render_ready()/_on_zoom_detail_render_ready()) —
        a naive call-count-based start/stop pairing would go out of sync the first time that
        happens, since one pipeline's "stop" can fire while another is still rendering.
        Called after every state change to self._dngconv_thread, self._zoom_detail_thread,
        or self._default_preview_thread, so it's always cheap to call redundantly.
        """
        active = (self._dngconv_thread is not None or self._zoom_detail_thread is not None
                  or self._default_preview_thread is not None)
        if active:
            self.image_label.start_spinner()
        else:
            self.image_label.stop_spinner()

    def _schedule_dngconv_render(self):
        """Kicks off (or queues) a background Adobe re-render if the edit state has
        actually changed since the last one — called from every _render_preview(), but
        this is cheap to call redundantly (a no-op comparison) since the real work only
        starts when something's different.

        The dedup key pairs _capture_edit_state() with self._crop_editing separately —
        toggling the crop guide checkbox alone doesn't change any value
        _capture_edit_state() itself snapshots (it's not a real edit, so it deliberately
        isn't part of Undo/Redo history either), but it *does* change what
        _build_xmp_crs_args() writes for the crop tags (see _effective_crop_for_render()),
        so without this the guide toggle would silently fail to trigger a fresh render.
        """
        if not self._dngconv_path or not self._work_dng_path or self.raw is None:
            return
        state = (self._capture_edit_state(), self._crop_editing)
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

        # Local adjustments (radial/graduated/spot) aren't rendered by Adobe DNG Converter
        # at all (see LocalAdjustmentRenderThread's own docstring) — when any are active,
        # route through that compositing pipeline instead of the plain single-render one.
        lens_correction = self._lens_correction if self.lens_profile_checkbox.isChecked() else None
        has_local = bool(self.radial_filters or self.gradient_filters or self.spots)
        local = self._local_filters_for_render(for_save=False) if has_local else None
        if local is not None:
            radial, gradient, spots = local
            base_args = self._build_xmp_crs_args()  # flat tags only — the struct tags
            # _build_all_edit_xmp_args() adds on top are irrelevant to rendering (that's
            # the whole reason this pipeline exists) and would be wasted work here
            base_exposure = 0.0 if self.tone_mode.currentText() == "Auto" else self.exposure_slider.value()
            base_contrast = 0.0 if self.tone_mode.currentText() == "Auto" else self.contrast_slider.value()
            base_saturation = self.saturation_slider.value()
            self._dngconv_thread = LocalAdjustmentRenderThread(
                self._work_dng_path, base_args, base_exposure, base_contrast, base_saturation,
                radial, gradient, spots, self.exiftool_path, self._dngconv_path, generation,
                lens_correction=lens_correction, parent=self,
            )
        else:
            xmp_args = self._build_all_edit_xmp_args()  # must happen on the main thread —
            # see DngConverterRenderThread's own docstring for why
            self._dngconv_thread = DngConverterRenderThread(
                self._work_dng_path, xmp_args, self.exiftool_path, self._dngconv_path, generation,
                lens_correction=lens_correction, parent=self,
            )
        self._dngconv_thread.ready.connect(self._on_dngconv_render_ready)
        self._dngconv_thread.failed.connect(self._on_dngconv_render_failed)
        self._dngconv_thread.start()
        # While this runs (up to a few seconds — longer still with local adjustments
        # active, one extra Adobe render per region, see LocalAdjustmentRenderThread), the
        # image on screen simply doesn't change — there's no faster internal approximation
        # to show meanwhile (see CLAUDE.md, "Adobe DNG Converter is the sole renderer" —
        # still true even with local-adjustment compositing, every blended pixel still
        # comes from a real Adobe render). The status bar note plus the spinner overlay
        # (RethinkRAW-style dotted circle, centered on the image, see _sync_spinner()) are
        # the only feedback that anything is happening.
        self.statusBar().showMessage("Rendering anteprima Adobe DNG Converter…")
        self._sync_spinner()

    def _on_dngconv_render_ready(self, jpeg_path, generation):
        try:
            if generation == self._dngconv_generation and self.raw is not None:
                pixmap = QPixmap(jpeg_path)
                if not pixmap.isNull():
                    self._preview_pixmap = pixmap
                    self._update_image_label()
                    self.histogram_widget.set_data(compute_rgb_histogram(jpeg_path))
                    self.statusBar().showMessage("Anteprima Adobe DNG Converter aggiornata", 3000)
        finally:
            if os.path.exists(jpeg_path):
                try:
                    os.unlink(jpeg_path)
                except OSError:
                    pass
            self._dngconv_thread = None
            if self._dngconv_pending:
                self._start_dngconv_render()  # settles straight into the next render —
                # already calls _sync_spinner() itself, so the plain call below just
                # confirms the (unchanged) still-active state
            self._sync_spinner()

    def _on_dngconv_render_failed(self, generation):
        self._dngconv_thread = None
        if self._dngconv_pending:
            self._start_dngconv_render()
        elif generation == self._dngconv_generation:
            # Whatever was on screen before (the last successful Adobe render, or nothing
            # yet on a freshly opened file) is left untouched — a failed background render
            # is not a reason to replace a valid image with nothing, or an internal
            # approximation this app doesn't have.
            self.statusBar().showMessage("Rendering Adobe DNG Converter non riuscito — anteprima invariata", 4000)
        self._sync_spinner()

    # ---- Default Preview toggle ----
    # A pure display swap (see _update_image_label()) — user-requested, on/off toggle logic,
    # to quickly compare the current edit against "the image as generated at the start" —
    # every control at reset_controls()'s own default, with the current crop/rotation kept
    # as-is. Never touches the real widget state, undo history, or the file on disk.

    def _toggle_default_preview(self, checked):
        if not checked:
            self._default_preview_active = False
            self._default_preview_pixmap = None
            self._update_image_label()
            self.statusBar().showMessage("Anteprima default disattivata", 2000)
            return

        if self.raw is None or not self._work_dng_path or not os.path.isfile(self._work_dng_path):
            self.default_preview_btn.setChecked(False)
            return

        # Build the "default state + current crop" XMP args by briefly swapping every
        # widget to reset_controls()'s own defaults, capturing what would get written, then
        # restoring the real edit state — reusing reset_controls() (rather than a second,
        # hardcoded copy of what "default" means) keeps this in sync with "Virgin-file
        # defaults match Lightroom, not zero" in CLAUDE.md automatically, forever. The whole
        # swap is synchronous with no Qt event-loop yield in between, so nothing actually
        # repaints mid-swap — the widgets never visibly flicker.
        real_state = self._capture_edit_state()
        self.reset_controls()
        self.crop = copy.deepcopy(real_state["crop"])  # the one thing this preview must
        # NOT reset, per the user's own request — keep the real, current crop/rotation
        default_args = self._build_all_edit_xmp_args(for_save=True)  # for_save=True: the
        # real committed crop, regardless of whether the guide happens to be showing right now
        default_lens_correction = (
            self._lens_correction if self.lens_profile_checkbox.isChecked() else None
        )
        self._apply_edit_state(real_state)  # restores every widget (crop included) and
        # re-arms the normal debounced pipeline pointed at the real state again — the live
        # pipeline is completely unaffected by this toggle, it keeps rendering real edits
        # into self._preview_pixmap in the background exactly as if nothing happened

        self._default_preview_active = True
        self._start_default_preview_render(default_args, default_lens_correction)

    def _start_default_preview_render(self, xmp_args, lens_correction):
        self._default_preview_generation += 1
        generation = self._default_preview_generation
        out_dir = os.path.dirname(self._work_dng_path)
        src_path = os.path.join(out_dir, f"default_preview_{uuid.uuid4().hex[:8]}.dng")
        shutil.copyfile(self._work_dng_path, src_path)  # a disposable copy, not
        # self._work_dng_path directly — see __init__'s own comment on
        # _default_preview_src_paths for why the main pipeline writing to that same shared
        # file concurrently would be a real race, not just a theoretical one
        self._default_preview_src_paths[generation] = src_path
        self._default_preview_thread = DngConverterRenderThread(
            src_path, xmp_args, self.exiftool_path, self._dngconv_path, generation,
            lens_correction=lens_correction, parent=self,
        )
        self._default_preview_thread.ready.connect(self._on_default_preview_ready)
        self._default_preview_thread.failed.connect(self._on_default_preview_failed)
        self._default_preview_thread.start()
        self.statusBar().showMessage("Rendering anteprima default…")
        self._sync_spinner()

    def _on_default_preview_ready(self, jpeg_path, generation):
        try:
            if (generation == self._default_preview_generation and self.raw is not None
                    and self._default_preview_active):
                pixmap = QPixmap(jpeg_path)
                if not pixmap.isNull():
                    self._default_preview_pixmap = pixmap
                    self._update_image_label()
                    self.statusBar().showMessage("Anteprima default pronta", 3000)
        finally:
            if os.path.exists(jpeg_path):
                try:
                    os.unlink(jpeg_path)
                except OSError:
                    pass
            src_path = self._default_preview_src_paths.pop(generation, None)
            if src_path and os.path.exists(src_path):
                try:
                    os.unlink(src_path)
                except OSError:
                    pass
            self._default_preview_thread = None
            self._sync_spinner()

    def _on_default_preview_failed(self, generation):
        self._default_preview_thread = None
        src_path = self._default_preview_src_paths.pop(generation, None)
        if src_path and os.path.exists(src_path):
            try:
                os.unlink(src_path)
            except OSError:
                pass
        if generation == self._default_preview_generation and self._default_preview_active:
            self.statusBar().showMessage("Rendering anteprima default non riuscito", 4000)
            self.default_preview_btn.setChecked(False)
            self._default_preview_active = False
        self._sync_spinner()

    # ---- Zoom detail rendering ----
    #
    # Past ZOOM_DETAIL_THRESHOLD, the plain work-copy preview is being upscaled to display —
    # this pipeline re-renders just the currently visible region from a full-resolution
    # source instead, converging zoom on true per-pixel detail (user's own request: "un po'
    # come fa Lightroom... risoluzione sul lato lungo di circa 2000 pixel... presi dal file
    # originale, sempre se non ho zoomato troppo"). The result is *composited on top of* the
    # normal preview (_compose_zoom_detail()), never replaces it — panning/zooming always
    # shows something correct immediately, with detail catching up shortly after settling,
    # same "never show nothing, never show something wrong" philosophy as the main pipeline.

    def _compute_visible_region(self):
        """The currently visible viewport, as fractions {left, top, right, bottom} of the
        *original full raw frame*. Composes the work-copy pixmap's own extent (which
        already reflects any committed crop, via _effective_crop_for_render()) with the
        current scroll position and zoom level. Returns None when there's nothing sensible
        to compute — no explicit zoom, no preview yet, or a *rotated* crop/guide (mapping a
        sub-rectangle back through a rotation isn't handled; the plain lower-res preview
        stays on screen in that case, same graceful degradation used elsewhere here).
        """
        if self._zoom_level is None or self._preview_pixmap is None or self._preview_pixmap.isNull():
            return None
        pm_w, pm_h = self._preview_pixmap.width(), self._preview_pixmap.height()
        if pm_w <= 0 or pm_h <= 0:
            return None
        scaled_w, scaled_h = pm_w * self._zoom_level, pm_h * self._zoom_level
        viewport = self.image_scroll.viewport()
        vp_w, vp_h = viewport.width(), viewport.height()
        hbar, vbar = self.image_scroll.horizontalScrollBar(), self.image_scroll.verticalScrollBar()
        x0, y0 = hbar.value(), vbar.value()
        x1, y1 = min(x0 + vp_w, scaled_w), min(y0 + vp_h, scaled_h)
        if x1 <= x0 or y1 <= y0:
            return None
        left, top = x0 / scaled_w, y0 / scaled_h
        right, bottom = x1 / scaled_w, y1 / scaled_h

        crop = self._effective_crop_for_render(for_save=False)
        if crop and crop.get("angle"):
            return None
        if crop:
            cw, ch = crop["right"] - crop["left"], crop["bottom"] - crop["top"]
            left, right = crop["left"] + left * cw, crop["left"] + right * cw
            top, bottom = crop["top"] + top * ch, crop["top"] + bottom * ch

        return {"left": left, "top": top, "right": right, "bottom": bottom}

    def _schedule_zoom_detail_render(self):
        """Kicks off (or queues) a background high-detail re-render of the currently visible
        region — called, debounced via self._zoom_detail_timer, on every zoom-level change
        and every scroll/pan. No-ops below ZOOM_DETAIL_THRESHOLD (clearing any stale detail
        patch so the plain preview shows through again), and lazily builds the
        full-resolution zoom source on first use (_ensure_zoom_source()) rather than paying
        that cost for files the user never zooms into this far.
        """
        if self.raw is None or self._zoom_level is None or self._zoom_level <= self.ZOOM_DETAIL_THRESHOLD:
            if self._zoom_detail_pixmap is not None:
                self._zoom_detail_pixmap = None
                self._zoom_detail_region = None
                self._update_image_label()
            self._last_zoom_detail_state = None
            return
        if not self._zoom_source_path:
            self._ensure_zoom_source()
            return

        region = self._compute_visible_region()
        if region is None:
            return

        state = (self._capture_edit_state(), region)
        if state == self._last_zoom_detail_state:
            return
        self._last_zoom_detail_state = state
        if self._zoom_detail_thread is not None:
            self._zoom_detail_pending = True
            return
        self._start_zoom_detail_render(region)

    def _start_zoom_detail_render(self, region):
        """Renders the *whole effective frame* (the user's own committed crop/rotation, if
        any — never just the tiny viewport sub-region on its own) at whatever full-frame
        resolution makes the region-within-it come out close to its own correct, native-
        capped size, then _on_zoom_detail_render_ready() crops just that region back out in
        Qt. Deliberately not a direct "-XMP-crs:Crop* = this viewport region" render:
        confirmed empirically that Adobe DNG Converter silently stops embedding a full-size
        "JpgFromRaw" preview (falling back to a much smaller, fixed-ratio "PreviewImage"
        instead — real detail lost, not just a slower path) once a *crop* shrinks the
        requested output below roughly 1000-1500px, which is exactly the common case for a
        zoomed-in region. Rendering the full frame — always comfortably above that
        threshold — and cropping candidate detail out of it afterward sidesteps the
        threshold entirely, at the cost of a somewhat larger conversion+JPEG-decode than the
        strict minimum needed (bounded by ZOOM_DETAIL_MIN_REQUEST_SIDE, not unbounded).
        """
        self._zoom_detail_pending = False
        self._zoom_detail_generation += 1
        generation = self._zoom_detail_generation
        self._pending_zoom_detail_region = region

        # Target size for the *region itself* — never exceeds its own native pixel size, so
        # this never shows invented detail (the user's own stated requirement: "sempre se
        # non ho zoomato troppo e quei 2000 pixel non ci sono").
        region_w_frac = region["right"] - region["left"]
        region_h_frac = region["bottom"] - region["top"]
        region_w_px = region_w_frac * self.raw.sizes.width
        region_h_px = region_h_frac * self.raw.sizes.height
        # Same axis (width or height) for both the pixel count *and* the fraction below —
        # mixing axes (e.g. pixels from width, fraction from height) would compute a
        # full-frame side that doesn't actually deliver target_cap pixels on the axis that
        # matters, since width and height fractions of a non-square region/frame differ.
        if region_w_px >= region_h_px:
            native_long_edge, region_long_frac = region_w_px, region_w_frac
        else:
            native_long_edge, region_long_frac = region_h_px, region_h_frac
        target_cap = min(self.ZOOM_DETAIL_MAX_SIDE, max(1, round(native_long_edge)))
        self._pending_zoom_detail_cap = target_cap
        # True when the region's own native resolution — not ZOOM_DETAIL_MAX_SIDE — is what
        # limited target_cap: zooming in *further* from here can't reveal more real detail,
        # only enlarge these same pixels (shown in the status bar once the render lands, see
        # _on_zoom_detail_render_ready(), so the user can tell "genuinely sharper" from "just
        # bigger" apart while zooming).
        self._pending_zoom_detail_native_capped = native_long_edge < self.ZOOM_DETAIL_MAX_SIDE

        # Full-frame side that makes the region-within-it come out to target_cap pixels on
        # its own dominant axis (the exact crop+downscale in _on_zoom_detail_render_ready()
        # is what actually guarantees the final result is correct regardless of any
        # remaining approximation here), floored per this method's own docstring and capped
        # at the frame's own true native size (no point requesting more than that).
        proportional_side = target_cap / region_long_frac if region_long_frac > 0 else target_cap
        frame_native_long = max(self.raw.sizes.width, self.raw.sizes.height)
        request_side = min(frame_native_long, max(proportional_side, self.ZOOM_DETAIL_MIN_REQUEST_SIDE))

        # Local adjustments aren't rendered by Adobe DNG Converter at all (see
        # LocalAdjustmentRenderThread's own docstring) — when any are active, route through
        # that compositing pipeline here too, not just the main preview pipeline. CONFIRMED
        # BUG, fixed: originally only _start_dngconv_render() (the main "Fit" preview) was
        # wired to the compositing pipeline — this zoom-detail pipeline still went straight
        # through the plain DngConverterRenderThread, so a placed spot/radial/gradient
        # correctly showed its effect at Fit but silently reverted to the untouched Adobe
        # render the moment the user zoomed in past ZOOM_DETAIL_THRESHOLD, exactly the
        # symptom the user reported ("non c'è effetto visivo se sono in uno zoom").
        lens_correction = self._lens_correction if self.lens_profile_checkbox.isChecked() else None
        has_local = bool(self.radial_filters or self.gradient_filters or self.spots)
        local = self._local_filters_for_render(for_save=False) if has_local else None
        if local is not None:
            radial, gradient, spots = local
            base_args = self._build_xmp_crs_args()
            base_exposure = 0.0 if self.tone_mode.currentText() == "Auto" else self.exposure_slider.value()
            base_contrast = 0.0 if self.tone_mode.currentText() == "Auto" else self.contrast_slider.value()
            base_saturation = self.saturation_slider.value()
            self._zoom_detail_thread = LocalAdjustmentRenderThread(
                self._zoom_source_path, base_args, base_exposure, base_contrast, base_saturation,
                radial, gradient, spots, self.exiftool_path, self._dngconv_path, generation,
                side=round(request_side), lens_correction=lens_correction, parent=self,
            )
        else:
            xmp_args = self._build_all_edit_xmp_args()  # crop as-is — the user's own
            # committed crop/rotation, not overridden to the viewport region (see this
            # method's docstring)
            self._zoom_detail_thread = DngConverterRenderThread(
                self._zoom_source_path, xmp_args, self.exiftool_path, self._dngconv_path,
                generation, side=round(request_side), lens_correction=lens_correction, parent=self,
            )
        self._zoom_detail_thread.ready.connect(self._on_zoom_detail_render_ready)
        self._zoom_detail_thread.failed.connect(self._on_zoom_detail_render_failed)
        self._zoom_detail_thread.start()
        self.statusBar().showMessage("Rendering dettaglio zoom…")
        self._sync_spinner()

    def _on_zoom_detail_render_ready(self, jpeg_path, generation):
        try:
            if generation == self._zoom_detail_generation and self.raw is not None:
                full_pixmap = QPixmap(jpeg_path)
                region = self._pending_zoom_detail_region
                rel = self._region_within_effective_crop(region) if not full_pixmap.isNull() else None
                if rel is not None:
                    left, top, right, bottom = rel
                    fw, fh = full_pixmap.width(), full_pixmap.height()
                    x0, y0 = max(0, int(left * fw)), max(0, int(top * fh))
                    x1 = min(fw, max(x0 + 1, round(right * fw)))
                    y1 = min(fh, max(y0 + 1, round(bottom * fh)))
                    cropped = full_pixmap.copy(x0, y0, x1 - x0, y1 - y0)
                    if not cropped.isNull():
                        # Trim back down to the size that's actually correct for this region
                        # (see _start_zoom_detail_render()) — a display-only geometric
                        # resize, not a rendering decision; every pixel already came from
                        # Adobe. scaled(cap, cap, KeepAspectRatio, ...) fits the image within
                        # a cap×cap box, so the *long* edge lands on exactly `cap` either way.
                        cap = self._pending_zoom_detail_cap
                        if max(cropped.width(), cropped.height()) > cap:
                            cropped = cropped.scaled(cap, cap, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                        self._zoom_detail_pixmap = cropped
                        self._zoom_detail_region = region
                        self._update_image_label()
                        # User-requested: show the actual rasterized resolution, and flag
                        # when it's capped by native resolution rather than ZOOM_DETAIL_MAX_SIDE
                        # ("...così so se sto zoomando troppo e l'immagine è solo zoomata
                        # digitalmente") — describes the *current* level, not a warning about
                        # zooming further (the user's own correction: not "ulteriore zoom sarà
                        # digitale" but "lo zoom a questo livello è già solo digitale").
                        note = (" — a questo livello lo zoom è solo digitale (nessun dettaglio nativo in più)"
                                if self._pending_zoom_detail_native_capped else "")
                        self.statusBar().showMessage(
                            f"Dettaglio zoom: {cropped.width()}×{cropped.height()}px{note}", 5000
                        )
        finally:
            if os.path.exists(jpeg_path):
                try:
                    os.unlink(jpeg_path)
                except OSError:
                    pass
            self._zoom_detail_thread = None
            if self._zoom_detail_pending:
                region = self._compute_visible_region()
                if region is not None:
                    self._start_zoom_detail_render(region)
            self._sync_spinner()

    def _on_zoom_detail_render_failed(self, generation):
        self._zoom_detail_thread = None
        if self._zoom_detail_pending:
            region = self._compute_visible_region()
            if region is not None:
                self._start_zoom_detail_render(region)
        elif generation == self._zoom_detail_generation:
            self.statusBar().showMessage("Rendering dettaglio zoom non riuscito", 4000)
        # Otherwise: leave whatever's currently displayed (plain preview, or a still-valid
        # older detail patch) untouched — same "a failed background render is not a reason
        # to replace a valid image" convention the main pipeline already follows.
        self._sync_spinner()

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
            "parametric_highlights": self.parametric_highlights_slider.value(),
            "parametric_lights": self.parametric_lights_slider.value(),
            "parametric_darks": self.parametric_darks_slider.value(),
            "parametric_shadows": self.parametric_shadows_slider.value(),
            "parametric_shadow_split": self.parametric_shadow_split_slider.value(),
            "parametric_midtone_split": self.parametric_midtone_split_slider.value(),
            "parametric_highlight_split": self.parametric_highlight_split_slider.value(),
            "hsl_hue": {c: s.value() for c, s in self.hsl_hue_sliders.items()},
            "hsl_saturation": {c: s.value() for c, s in self.hsl_saturation_sliders.items()},
            "hsl_luminance": {c: s.value() for c, s in self.hsl_luminance_sliders.items()},
            "gray_mixer": {c: s.value() for c, s in self.gray_mixer_sliders.items()},
            "bw_enabled": self.bw_checkbox.isChecked(),
            "split_shadow_hue": self.split_shadow_hue_slider.value(),
            "split_shadow_sat": self.split_shadow_sat_slider.value(),
            "split_highlight_hue": self.split_highlight_hue_slider.value(),
            "split_highlight_sat": self.split_highlight_sat_slider.value(),
            "split_balance": self.split_balance_slider.value(),
            "sharpness": self.sharpness_slider.value(),
            "sharpen_radius": self.sharpen_radius_slider.value(),
            "sharpen_detail": self.sharpen_detail_slider.value(),
            "sharpen_masking": self.sharpen_masking_slider.value(),
            "luminance_nr": self.luminance_nr_slider.value(),
            "luminance_nr_detail": self.luminance_nr_detail_slider.value(),
            "luminance_nr_contrast": self.luminance_nr_contrast_slider.value(),
            "color_nr": self.color_nr_slider.value(),
            "color_nr_detail": self.color_nr_detail_slider.value(),
            "color_nr_smoothness": self.color_nr_smoothness_slider.value(),
            "defringe_purple_amount": self.defringe_purple_amount_slider.value(),
            "defringe_purple_hue_lo": self.defringe_purple_hue_lo_slider.value(),
            "defringe_purple_hue_hi": self.defringe_purple_hue_hi_slider.value(),
            "defringe_green_amount": self.defringe_green_amount_slider.value(),
            "defringe_green_hue_lo": self.defringe_green_hue_lo_slider.value(),
            "defringe_green_hue_hi": self.defringe_green_hue_hi_slider.value(),
            "grain_amount": self.grain_amount_slider.value(),
            "grain_size": self.grain_size_slider.value(),
            "grain_frequency": self.grain_frequency_slider.value(),
            "vignette_amount": self.vignette_amount_slider.value(),
            "vignette_midpoint": self.vignette_midpoint_slider.value(),
            "vignette_feather": self.vignette_feather_slider.value(),
            "vignette_roundness": self.vignette_roundness_slider.value(),
            "vignette_style": self.vignette_style_combo.currentText(),
            "lens_profile_enable": self.lens_profile_checkbox.isChecked(),
            "lateral_ca": self.lateral_ca_checkbox.isChecked(),
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

            self.parametric_highlights_slider.set_value(state["parametric_highlights"])
            self.parametric_lights_slider.set_value(state["parametric_lights"])
            self.parametric_darks_slider.set_value(state["parametric_darks"])
            self.parametric_shadows_slider.set_value(state["parametric_shadows"])
            self.parametric_shadow_split_slider.set_value(state["parametric_shadow_split"])
            self.parametric_midtone_split_slider.set_value(state["parametric_midtone_split"])
            self.parametric_highlight_split_slider.set_value(state["parametric_highlight_split"])

            for color, value in state["hsl_hue"].items():
                self.hsl_hue_sliders[color].set_value(value)
            for color, value in state["hsl_saturation"].items():
                self.hsl_saturation_sliders[color].set_value(value)
            for color, value in state["hsl_luminance"].items():
                self.hsl_luminance_sliders[color].set_value(value)
            for color, value in state["gray_mixer"].items():
                self.gray_mixer_sliders[color].set_value(value)
            self.bw_checkbox.blockSignals(True)
            self.bw_checkbox.setChecked(state["bw_enabled"])
            self.bw_checkbox.blockSignals(False)
            self._update_hsl_bw_content_visibility()

            self.split_shadow_hue_slider.set_value(state["split_shadow_hue"])
            self.split_shadow_sat_slider.set_value(state["split_shadow_sat"])
            self.split_highlight_hue_slider.set_value(state["split_highlight_hue"])
            self.split_highlight_sat_slider.set_value(state["split_highlight_sat"])
            self.split_balance_slider.set_value(state["split_balance"])

            self.sharpness_slider.set_value(state["sharpness"])
            self.sharpen_radius_slider.set_value(state["sharpen_radius"])
            self.sharpen_detail_slider.set_value(state["sharpen_detail"])
            self.sharpen_masking_slider.set_value(state["sharpen_masking"])
            self.luminance_nr_slider.set_value(state["luminance_nr"])
            self.luminance_nr_detail_slider.set_value(state["luminance_nr_detail"])
            self.luminance_nr_contrast_slider.set_value(state["luminance_nr_contrast"])
            self.color_nr_slider.set_value(state["color_nr"])
            self.color_nr_detail_slider.set_value(state["color_nr_detail"])
            self.color_nr_smoothness_slider.set_value(state["color_nr_smoothness"])

            self.defringe_purple_amount_slider.set_value(state["defringe_purple_amount"])
            self.defringe_purple_hue_lo_slider.set_value(state["defringe_purple_hue_lo"])
            self.defringe_purple_hue_hi_slider.set_value(state["defringe_purple_hue_hi"])
            self.defringe_green_amount_slider.set_value(state["defringe_green_amount"])
            self.defringe_green_hue_lo_slider.set_value(state["defringe_green_hue_lo"])
            self.defringe_green_hue_hi_slider.set_value(state["defringe_green_hue_hi"])

            self.grain_amount_slider.set_value(state["grain_amount"])
            self.grain_size_slider.set_value(state["grain_size"])
            self.grain_frequency_slider.set_value(state["grain_frequency"])
            self.vignette_amount_slider.set_value(state["vignette_amount"])
            self.vignette_midpoint_slider.set_value(state["vignette_midpoint"])
            self.vignette_feather_slider.set_value(state["vignette_feather"])
            self.vignette_roundness_slider.set_value(state["vignette_roundness"])
            self.vignette_style_combo.setCurrentText(state["vignette_style"])
            self.lens_profile_checkbox.setChecked(state["lens_profile_enable"])
            self.lateral_ca_checkbox.setChecked(state["lateral_ca"])

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

    def copy_settings(self):
        """Edit → Copia impostazioni (Ctrl+Alt+C) — snapshots the *entire* current edit state
        via _capture_edit_state(), the exact same function Undo/Redo already builds its own
        history from, so this needed no new state-serialization logic at all. Copies
        literally everything the snapshot already covers, including crop and the radial/
        gradient/spot regions — no per-category checklist (Basic only, exclude Crop, etc.,
        the way real Lightroom's own Copy Settings dialog offers) since the user explicitly
        asked for the simpler "copy everything" version to start with.

        Session-only, like the gallery filmstrip: self._settings_clipboard lives in memory
        only, cleared on app restart, never written to disk.
        """
        if self.raw is None:
            return
        self._settings_clipboard = self._capture_edit_state()
        self.paste_settings_action.setEnabled(True)
        self.statusBar().showMessage("Impostazioni copiate")

    def paste_settings(self):
        """Edit → Incolla impostazioni (Ctrl+Alt+V) — applies the clipboard snapshot to
        whichever photo is currently open, via _apply_edit_state(), the same restore path
        Undo/Redo already use. Deliberately does **not** manage the undo stack itself (unlike
        undo()/redo(), which pop from/push to _undo_stack/_redo_stack explicitly) — a paste is
        a genuinely new edit, not a history navigation, so it's left to flow through the
        completely ordinary _schedule_update() → _render_preview() → _maybe_push_undo_state()
        path _apply_edit_state() already triggers at its own end, the exact same way a normal
        slider drag gets recorded. Ctrl+Z after a paste correctly undoes it, for free, with no
        paste-specific undo logic needed.
        """
        if self.raw is None or self._settings_clipboard is None:
            return
        self._apply_edit_state(self._settings_clipboard)
        self.statusBar().showMessage("Impostazioni incollate")

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
            # Drop this file's session-cache entry *before* reloading — load_dng() itself
            # only skips caching on a same-path reload, it doesn't know this one is a
            # deliberate revert; without this, load_dng() would restore the very state the
            # user just asked to discard right back (see _session_edit_cache's own __init__
            # comment).
            self._session_edit_cache.pop(self.raw_path, None)
            self.load_dng(self.raw_path)

    def close_photo(self):
        """File → Chiudi foto (Ctrl+W): unloads the currently open file and returns the
        editor to its startup, no-file-open state, without quitting the application (see
        Esci/closeEvent() for that) — user-requested alongside Esci ("nel menu file vorrei
        un close (della foto) e un exit"), since previously the only way to stop editing a
        file was to open a different one or quit the app outright.

        Reuses the exact same dirty-state check and Save/Don't Save/Cancel prompt as
        closeEvent() (see that method's own docstring for why: comparing a fresh
        _capture_edit_state() against self._saved_edit_state, the same equality check
        Undo/Redo and Copy/Paste Settings already rely on, so this can't drift out of sync
        with what those consider "changed"). A no-op when nothing is open.
        """
        if self.raw is None:
            return
        dirty = self._saved_edit_state is not None and self._capture_edit_state() != self._saved_edit_state
        if dirty:
            reply = QMessageBox.question(
                self, "Modifiche non salvate",
                f"'{os.path.basename(self.raw_path)}' ha modifiche non salvate.\n\n"
                "Salvare prima di chiudere?",
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
                QMessageBox.Save,
            )
            if reply == QMessageBox.Cancel:
                return
            if reply == QMessageBox.Save:
                if not self.save_to_dng():
                    # save_to_dng() already showed its own critical dialog explaining why —
                    # don't also close and lose the file on top of a failed save.
                    return

        # Whether saved or discarded (Save/Discard both fall through to here), this file's
        # session-cache entry must go too — leaving a Discard-ed edit cached would silently
        # hand it right back if the same file is reopened later this session (see
        # _session_edit_cache's own __init__ comment). A harmless no-op if nothing was cached.
        self._session_edit_cache.pop(self.raw_path, None)

        # Same generation-bump-and-queue-for-later-cleanup _reset_dngconv_pipeline() does when
        # switching to a *new* file (see that method) — just with no replacement file to build
        # a fresh working copy for, so no thread gets started here.
        self._dngconv_generation += 1
        self._zoom_detail_generation += 1
        self._work_dng_path = None
        self._last_dngconv_state = None
        self._dngconv_pending = False
        self._zoom_source_path = None
        self._zoom_detail_pending = False
        self._last_zoom_detail_state = None
        self._zoom_detail_pixmap = None
        self._zoom_detail_region = None
        self._default_preview_active = False
        self._default_preview_pixmap = None
        self._default_preview_generation += 1
        self.default_preview_btn.blockSignals(True)
        self.default_preview_btn.setChecked(False)
        self.default_preview_btn.blockSignals(False)
        if self._work_dng_dir is not None:
            self._old_work_dirs.append(self._work_dng_dir)
            self._work_dng_dir = None

        self.raw.close()
        self.raw = None
        self.raw_path = None
        self.filmstrip.set_current(None)
        self.camera_profile = None
        self.camera_wb = None
        self.metadata_values = {}
        self._lens_correction = None
        self.lens_match_label.setText("")
        self._undo_stack = []
        self._redo_stack = []
        self._current_edit_state = None
        self._saved_edit_state = None
        self._preview_pixmap = None
        self.histogram_widget.clear_data()
        self.image_label.stop_spinner()
        self.image_label.clear()
        self.image_label.setText("Apri un file DNG (File → Open DNG…)")
        # Sliders/filters/crop back to virgin-file defaults, and every overlay along with them
        # (reset_controls() already sets radial_filters/gradient_filters/spots/crop to their
        # empty/None state and re-selects index -1 on each, which clears the corresponding
        # ImageLabel overlay as a side effect — see _select_radial_filter() etc.). Its own
        # trailing _schedule_update() is a harmless no-op here since _render_preview() already
        # guards on self.raw is None.
        self.reset_controls()
        self._set_zoom_level(None)
        self.setWindowTitle("DNGForge")
        self.statusBar().showMessage("Foto chiusa")

    # ---- Save (embedded, no sidecar — see CLAUDE.md phase 5) ----

    def _build_xmp_crs_args(self, for_save: bool = False) -> list[str]:
        """-XMP-crs:Tag=value exiftool args recording the current edit state — the actual
        instructions handed to Adobe DNG Converter, the sole renderer in this app (see
        CLAUDE.md). Also Adobe Camera Raw tag names/conventions (matches RethinkRAW's own
        xmpSettings, see project memory dngforge-rethinkraw-research), so a DNG saved here
        round-trips through our own reload logic and is readable by other Adobe tools too.

        `for_save` controls which crop geometry gets written — see
        _effective_crop_for_render().
        """
        # Global option, not a tag: tells exiftool to split any "a; b; c"-valued List-type tag
        # (ToneCurvePV2012, LookParametersToneCurvePV2012 — see _format_curve_points()) into real
        # rdf:Seq list items instead of one flat string attribute. Must come before those tags in
        # the argument list to take effect on them, so it's added first, unconditionally — cheap
        # and harmless for every other (non-list) tag in this function.
        args = ["-sep", "; ", "-XMP-crs:ProcessVersion=11.0"]

        # Crop/Straighten — flat tags, real Adobe schema (verified against a genuine
        # Lightroom-saved crop in DSC_6751.dng: HasCrop/CropTop/CropLeft/CropBottom/
        # CropRight/CropAngle, same fraction convention this app already uses internally,
        # so no unit conversion is needed). Always written, even when there's no crop, so
        # a crop from a *previous* save gets properly cleared rather than left stranded.
        # Uses _effective_crop_for_render(), not self.crop directly, so the live preview
        # shows the full straightened frame while the crop guide is being edited (see that
        # method's docstring) — save always passes for_save=True to get the real crop.
        effective_crop = self._effective_crop_for_render(for_save)
        if effective_crop:
            args += [
                "-XMP-crs:HasCrop=True",
                f"-XMP-crs:CropTop={effective_crop['top']:.6f}",
                f"-XMP-crs:CropLeft={effective_crop['left']:.6f}",
                f"-XMP-crs:CropBottom={effective_crop['bottom']:.6f}",
                f"-XMP-crs:CropRight={effective_crop['right']:.6f}",
                f"-XMP-crs:CropAngle={effective_crop['angle']:.4f}",
            ]
        else:
            args.append("-XMP-crs:HasCrop=False")

        # Profile — the named "Adobe Color/Vivid/…" profiles route entirely through Adobe's
        # built-in "Look" system (PROFILE_LOOK_SETTINGS, see that constant's own docstring for
        # why a bare -XMP-crs:CameraProfile=Name tag alone has zero effect on Adobe DNG
        # Converter's actual rendering); "Custom" writes nothing (matches Adobe's own convention
        # of leaving CameraProfile/Look* whatever they already were).
        profile = self.profile_combo.currentText()
        if profile != "Custom":
            for tag, value in PROFILE_LOOK_SETTINGS[profile]:
                # ConvertToGrayscale is handled once, uniformly, right below — combined with
                # the standalone Black & White checkbox (roadmap item 16) rather than written
                # here per-profile, so neither path can leave the other's decision stranded
                # (see that block's own comment for why a naive "only write when checked"
                # version of this had exactly that bug).
                if tag == "ConvertToGrayscale":
                    continue
                if isinstance(value, list):
                    args.append(f"-XMP-crs:{tag}={_format_curve_points(value)}")
                elif value is None:
                    args.append(f"-XMP-crs:{tag}=")
                else:
                    args.append(f"-XMP-crs:{tag}={value}")
        else:
            # Clear CameraProfile/Look* too — otherwise switching *away* from a previously
            # saved named profile back to Custom would leave its LookName/LookParameters*
            # stranded in the file, same "always write the off-state" convention every other
            # mode here already follows (HasCrop, WhiteBalance, radial/gradient filters, …).
            args += ["-XMP-crs:CameraProfile=", "-XMP-crs:Look*="]

        # Black & White (roadmap item 16) — a standalone treatment toggle independent of
        # Profile: real Lightroom lets you hit its own B&W button regardless of which color
        # profile is active, the same ConvertToGrayscale tag the "Adobe Standard B&W"/"Adobe
        # Monochrome" profile entries route through. Written as a plain OR of both paths,
        # always (both True and False explicitly, never omitted) — CONFIRMED BUG caught
        # before shipping: an earlier version only wrote `=True` when the checkbox was
        # checked, leaving the tag whatever it already was otherwise; with Profile=Custom
        # (whose own branch above never touches this tag either), unchecking Black & White
        # after it had been on didn't actually clear grayscale — the classic "always write
        # the off-state too" lesson this file already documents elsewhere (HasCrop,
        # WhiteBalance, radial/gradient filters), just rediscovered here.
        grayscale = self.bw_checkbox.isChecked() or profile in ("Adobe Standard B&W", "Adobe Monochrome")
        args.append(f"-XMP-crs:ConvertToGrayscale={'True' if grayscale else 'False'}")

        wb_mode = self.wb_mode.currentText()
        # CONFIRMED BUG, fixed: Adobe DNG Converter resolves a *named* WhiteBalance preset
        # (Daylight/Cloudy/Shade/Tungsten/Fluorescent/Flash) from its own internal per-camera
        # table, silently discarding whatever literal ColorTemperature/Tint this app also
        # writes alongside it — confirmed by direct experimentation on RAW_LEICA_M8_prova.DNG,
        # bypassing the app: WhiteBalance=Daylight with an explicit, deliberately conflicting
        # ColorTemperature=3000 rendered near-identically to Custom@5500K (mean|diff|~2, not
        # the ~32 a literal 3000K interpretation would produce), and two presets defined with
        # the exact same nominal (temp, tint) pair in WB_PRESETS — Daylight and Flash, both
        # (5500, 0) — rendered visibly differently from each other (mean|diff|~2, max 68) even
        # though both wrote byte-identical ColorTemperature/Tint tags; only the WhiteBalance
        # *name* differed. This is the reported "same displayed temperature, different render
        # depending on which preset I clicked" bug. Same root cause as the already-documented
        # As Shot/Auto behavior (see "Control panel" in CLAUDE.md), just not previously known
        # to extend to the six named presets too — Shade/Tungsten/Flash happened to coincide
        # with this app's own WB_PRESETS numbers for this particular camera, masking the bug
        # until Daylight/Cloudy/Fluorescent were tried. Since the Temperature/Tint sliders are
        # this app's actual source of truth for what the user sees on screen, every preset
        # button and "Camera Matching…" (which never had a distinct crs concept either) now
        # write WhiteBalance=Custom instead of the literal name — the numbers on screen are
        # always exactly what gets rendered, regardless of which preset button set them.
        args.append(f"-XMP-crs:WhiteBalance={'Custom' if (wb_mode in WB_PRESETS or wb_mode == 'Camera Matching…') else wb_mode}")
        if wb_mode in WB_PRESETS or wb_mode == "Custom":
            args.append(f"-XMP-crs:ColorTemperature={self.temp_slider.value():.0f}")
            args.append(f"-XMP-crs:Tint={self.tint_slider.value():.0f}")
        else:
            # CONFIRMED BUG, fixed: for As Shot/Auto/Camera Matching, ColorTemperature/Tint
            # used to simply never be written at all — meaning a stale value saved by an
            # *earlier* session (e.g. a leftover Custom/preset ColorTemperature from before the
            # user switched back to Auto) stayed in the file forever, untouched. Confirmed this
            # actually leaks into the render: on RAW_LEICA_M8_prova.DNG, which had a stale
            # ColorTemperature=13200/Tint=9 from old testing, rendering WhiteBalance=Auto with
            # those stale tags still present versus the same WhiteBalance=Auto with them
            # explicitly cleared produced mean|diff|~18 (max 254/255) — a real, substantial color
            # cast (13200K is the Temperature slider's own maximum, an extreme "compensate for a
            # very cool light source" setting, which in Lightroom's convention pushes the render
            # *warmer* — exactly the "Auto produces an image that's evidently too warm" the user
            # reported). So Adobe DNG Converter's WhiteBalance=Auto resolution is *not* fully
            # independent of a simultaneously-present ColorTemperature/Tint tag, unlike what the
            # named-preset investigation above found for WhiteBalance=Daylight (confirmed to fully
            # ignore a conflicting ColorTemperature there) — Auto/As Shot/Camera Matching needed
            # the same "always write the off-state too" treatment already applied to HasCrop,
            # ConvertToGrayscale, and the radial/gradient filters elsewhere in this function, just
            # missed here. Matches RethinkRAW's own convention exactly (its editXMP() always
            # writes "-XMP-crs:ColorTemperature=", "-XMP-crs:Tint=" for every non-Custom
            # WhiteBalance value, confirmed by reading xmp.go directly).
            args.append("-XMP-crs:ColorTemperature=")
            args.append("-XMP-crs:Tint=")

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
        # Named presets (Medium/Strong Contrast) need their actual point list written too, not
        # just the name — Adobe DNG Converter doesn't resolve a curve from ToneCurveName2012
        # alone (confirmed empirically), so CURVE_PRESETS' own points (already used to populate
        # the disabled preset entries) are sent the same way Custom's points are. Linear needs no
        # points at all — an absent curve tag already renders as the identity curve.
        curve_name = self.curve_combo.currentText()
        if curve_name == "Custom":
            args.append(f"-XMP-crs:ToneCurvePV2012={_format_curve_points(self.curve_editor.get_points())}")
        elif CURVE_PRESETS.get(curve_name):
            args.append(f"-XMP-crs:ToneCurvePV2012={_format_curve_points(CURVE_PRESETS[curve_name])}")

        # Parametric Curve (roadmap item 19) — the Tone Curve panel's other tab, real crs
        # tags, always written (same "always write, even at default" convention as every
        # other slider group — the 3 split points' *defaults* are 25/50/75, not 0, but a
        # value of 25/50/75 is still exactly as much "the default state" as 0 is elsewhere).
        args += [
            f"-XMP-crs:ParametricHighlights={self.parametric_highlights_slider.value():.0f}",
            f"-XMP-crs:ParametricLights={self.parametric_lights_slider.value():.0f}",
            f"-XMP-crs:ParametricDarks={self.parametric_darks_slider.value():.0f}",
            f"-XMP-crs:ParametricShadows={self.parametric_shadows_slider.value():.0f}",
            f"-XMP-crs:ParametricShadowSplit={self.parametric_shadow_split_slider.value():.0f}",
            f"-XMP-crs:ParametricMidtoneSplit={self.parametric_midtone_split_slider.value():.0f}",
            f"-XMP-crs:ParametricHighlightSplit={self.parametric_highlight_split_slider.value():.0f}",
        ]

        # HSL / Color (roadmap item 20) and Gray Mixer (roadmap item 16) — 32 flat per-color
        # tags, generated from the same slider dicts the UI setup built, rather than 32
        # hand-written f-string lines (see HSL_COLORS for the confirmed real tag-name
        # convention). Gray Mixer is written unconditionally regardless of whether Black &
        # White is actually checked — harmless when ConvertToGrayscale is off (Adobe simply
        # ignores it), and avoids yet another conditional-write special case.
        for prefix, store in (
            ("HueAdjustment", self.hsl_hue_sliders),
            ("SaturationAdjustment", self.hsl_saturation_sliders),
            ("LuminanceAdjustment", self.hsl_luminance_sliders),
            ("GrayMixer", self.gray_mixer_sliders),
        ):
            for color in HSL_COLORS:
                args.append(f"-XMP-crs:{prefix}{color}={store[color].value():.0f}")

        # Split Toning (roadmap item 17) — real crs:SplitToning* tags (also reused by modern
        # Lightroom's Color Grading wheels, see XMP.pm's own note on these fields).
        args += [
            f"-XMP-crs:SplitToningShadowHue={self.split_shadow_hue_slider.value():.0f}",
            f"-XMP-crs:SplitToningShadowSaturation={self.split_shadow_sat_slider.value():.0f}",
            f"-XMP-crs:SplitToningHighlightHue={self.split_highlight_hue_slider.value():.0f}",
            f"-XMP-crs:SplitToningHighlightSaturation={self.split_highlight_sat_slider.value():.0f}",
            f"-XMP-crs:SplitToningBalance={self.split_balance_slider.value():.0f}",
        ]

        args += [
            f"-XMP-crs:Sharpness={self.sharpness_slider.value():.0f}",
            f"-XMP-crs:SharpenRadius={self.sharpen_radius_slider.value():.1f}",
            f"-XMP-crs:SharpenDetail={self.sharpen_detail_slider.value():.0f}",
            f"-XMP-crs:SharpenEdgeMasking={self.sharpen_masking_slider.value():.0f}",
            f"-XMP-crs:LuminanceSmoothing={self.luminance_nr_slider.value():.0f}",
            f"-XMP-crs:LuminanceNoiseReductionDetail={self.luminance_nr_detail_slider.value():.0f}",
            f"-XMP-crs:LuminanceNoiseReductionContrast={self.luminance_nr_contrast_slider.value():.0f}",
            f"-XMP-crs:ColorNoiseReduction={self.color_nr_slider.value():.0f}",
            f"-XMP-crs:ColorNoiseReductionDetail={self.color_nr_detail_slider.value():.0f}",
            f"-XMP-crs:ColorNoiseReductionSmoothness={self.color_nr_smoothness_slider.value():.0f}",
        ]

        # Defringe (roadmap item 18) — bottom of the real Detail panel, real crs tags.
        args += [
            f"-XMP-crs:DefringePurpleAmount={self.defringe_purple_amount_slider.value():.0f}",
            f"-XMP-crs:DefringePurpleHueLo={self.defringe_purple_hue_lo_slider.value():.0f}",
            f"-XMP-crs:DefringePurpleHueHi={self.defringe_purple_hue_hi_slider.value():.0f}",
            f"-XMP-crs:DefringeGreenAmount={self.defringe_green_amount_slider.value():.0f}",
            f"-XMP-crs:DefringeGreenHueLo={self.defringe_green_hue_lo_slider.value():.0f}",
            f"-XMP-crs:DefringeGreenHueHi={self.defringe_green_hue_hi_slider.value():.0f}",
        ]

        # Lens Corrections (roadmap item 4) — see the checkbox setup's own comment: these two
        # tags are for real Lightroom/ACR compatibility (AutoLateralCA also renders correctly
        # through Adobe DNG Converter's own CLI). LensProfileEnable's own geometric/vignetting
        # correction doesn't render through Adobe's CLI at all — this app applies it separately,
        # locally, via lensfunpy (see compute_lensfun_correction()/_apply_lensfun_correction()),
        # keyed off this same checkbox but independent of what gets written here. Both tags
        # still always written (True and False alike, never omitted) — same "always write the
        # off-state too" convention as every other toggle in this function.
        args += [
            f"-XMP-crs:LensProfileEnable={1 if self.lens_profile_checkbox.isChecked() else 0}",
            f"-XMP-crs:AutoLateralCA={1 if self.lateral_ca_checkbox.isChecked() else 0}",
        ]

        # Grain / Post-Crop Vignette (roadmap items 14/15) — flat tags, always written
        # (even at 0/default) so a previous save's values get properly cleared rather than
        # left stranded, same convention as every other slider group above.
        args += [
            f"-XMP-crs:GrainAmount={self.grain_amount_slider.value():.0f}",
            f"-XMP-crs:GrainSize={self.grain_size_slider.value():.0f}",
            f"-XMP-crs:GrainFrequency={self.grain_frequency_slider.value():.0f}",
            f"-XMP-crs:PostCropVignetteAmount={self.vignette_amount_slider.value():.0f}",
            f"-XMP-crs:PostCropVignetteMidpoint={self.vignette_midpoint_slider.value():.0f}",
            f"-XMP-crs:PostCropVignetteFeather={self.vignette_feather_slider.value():.0f}",
            f"-XMP-crs:PostCropVignetteRoundness={self.vignette_roundness_slider.value():.0f}",
            f"-XMP-crs:PostCropVignetteStyle={VIGNETTE_STYLE_VALUES[self.vignette_style_combo.currentText()]}",
        ]
        return args

    def _build_all_edit_xmp_args(self, for_save: bool = False) -> list[str]:
        """_build_xmp_crs_args() plus the three structured local-adjustment tags
        (radial/gradient/spot) — the complete edit-state write, shared by save_to_dng()
        and the background Adobe-render pipeline (DngConverterRenderThread) so the two
        can't drift apart into writing different tag sets for "the same" edit state.

        `for_save=True` (only ever passed by save_to_dng()/export_preview_jpeg()) writes
        the real committed crop instead of the guide-mode full frame — see
        _effective_crop_for_render().
        """
        args = self._build_xmp_crs_args(for_save)
        args.append("-XMP-crs:CircularGradientBasedCorrections=" + build_radial_xmp_value(self.radial_filters))
        args.append("-XMP-crs:GradientBasedCorrections=" + build_gradient_xmp_value(self.gradient_filters))
        args.append("-XMP-crs:RetouchAreas=" + build_spot_xmp_value(self.spots))
        return args

    def _compute_save_render_side(self):
        """The `-side` value save_to_dng() should request from Adobe DNG Converter so the
        embedded JpgFromRaw lands at roughly SAVE_JPGFROMRAW_TARGET_MP, or None to render at
        native resolution (no -side flag at all) — user-requested, explicitly including the
        crop-aware behavior: cropping the frame should *not* shrink the embedded resolution
        below the target as long as the crop's own native pixel count can still support it
        ("che siano 12 mp sempre anche se faccio un ritaglio... sfruttando la risoluzione da
        24"), but a crop tight enough that its native resolution is already below the target
        must never be upscaled to force it ("se il ritaglio è tale che la foto non può
        essere 12 mp non fare upscaling, lascia la risoluzione che viene") — the same "never
        invent detail beyond the sensor" rule already established for zoom-detail rendering
        (see CLAUDE.md, "Zoom detail rendering").

        Adobe DNG Converter applies crop tags *after* scaling to the requested -side (see
        CLAUDE.md's own "Zoom detail rendering" investigation — the reason that pipeline
        renders the *whole* effective frame at a big enough -side rather than asking Adobe to
        crop-and-scale in one step directly to a small target), so hitting a *post-crop*
        pixel target means requesting a *larger* full-frame -side, scaled up by how much of
        the frame's area the crop actually keeps.

        Ignores rotation (same scope limit as `_region_within_effective_crop()`/
        `_local_filters_for_render()` elsewhere — a rotated crop's true post-rotation area
        isn't simply width-fraction × height-fraction) — falls back to native resolution in
        that case, same graceful-degradation convention already used for those.
        """
        native_w, native_h = self.raw.sizes.width, self.raw.sizes.height
        crop = self._effective_crop_for_render(for_save=True)
        if crop and not crop.get("angle"):
            crop_area_frac = max(0.0, crop["right"] - crop["left"]) * max(0.0, crop["bottom"] - crop["top"])
        else:
            crop_area_frac = 1.0
        if crop_area_frac <= 0:
            return None
        native_crop_mp = (native_w * native_h) * crop_area_frac / 1e6
        if native_crop_mp <= SAVE_JPGFROMRAW_TARGET_MP:
            return None  # crop's own native resolution is already at/below the target —
            # never upscale past what the sensor actually captured
        native_long_edge = max(native_w, native_h)
        return round(native_long_edge * math.sqrt(SAVE_JPGFROMRAW_TARGET_MP / native_crop_mp))

    def save_to_dng(self):
        """Render full resolution via Adobe DNG Converter — the sole renderer in this app,
        see CLAUDE.md — and embed the result + the edit state into the DNG in place.

        Always embeds — never falls back to an external .xmp sidecar, even
        on a pristine never-edited DNG (that's RethinkRAW's default-Save
        behavior, deliberately not replicated here, see CLAUDE.md phase 5
        and project memory dngforge-rethinkraw-research for why).

        Renders from a disposable full-resolution *copy* of the original, not the
        original itself, so a failure partway through (a bad Adobe DNG Converter run, a
        missing preview) never leaves the original file half-modified — the original is
        only ever touched by the single combined exiftool write at the end, exactly the
        same atomicity the pre-Adobe-only version of this function already had.
        """
        if self.raw is None or self.raw_path is None:
            return False
        if not self.exiftool_path:
            QMessageBox.critical(self, "Salvataggio non riuscito",
                                  "exiftool non trovato: impossibile scrivere le modifiche nel DNG.")
            return False
        if not self._dngconv_path:
            self._show_dngconv_missing_error()
            return False

        self.statusBar().showMessage("Rendering a piena risoluzione con Adobe DNG Converter…")
        QApplication.processEvents()
        t0 = time.time()
        path = self.raw_path
        render_dir = None

        try:
            render_dir = tempfile.mkdtemp(prefix="dngforge_save_")
            temp_full = os.path.join(render_dir, "full.dng")
            shutil.copyfile(path, temp_full)

            # xmp_args (the full set, including the real local-adjustment structs) is
            # always needed below for the metadata write onto the *original* file,
            # regardless of which rendering path runs next — Lightroom compatibility
            # doesn't depend on how DNGForge's own preview pixels got produced.
            xmp_args = self._build_all_edit_xmp_args(for_save=True)

            # Local adjustments aren't rendered by Adobe DNG Converter at all (see
            # LocalAdjustmentRenderThread's own docstring) — when any are active, route
            # through that compositing pipeline instead of a single plain render.
            lens_correction = self._lens_correction if self.lens_profile_checkbox.isChecked() else None
            has_local = bool(self.radial_filters or self.gradient_filters or self.spots)
            local = self._local_filters_for_render(for_save=True) if has_local else None
            # Caps the embedded JpgFromRaw at SAVE_JPGFROMRAW_TARGET_MP — user-requested,
            # a 24MP+ sensor's own native resolution was "anche troppi" for an embedded
            # preview — crop-aware (see _compute_save_render_side()'s own docstring): a
            # crop still reaches the target as long as its own native pixel count supports
            # it, but is never upscaled past that when it doesn't.
            render_side = self._compute_save_render_side()
            if local is not None:
                radial, gradient, spots = local
                base_args = self._build_xmp_crs_args(for_save=True)
                base_exposure = 0.0 if self.tone_mode.currentText() == "Auto" else self.exposure_slider.value()
                base_contrast = 0.0 if self.tone_mode.currentText() == "Auto" else self.contrast_slider.value()
                base_saturation = self.saturation_slider.value()
                jpeg_path = _render_local_adjustments_composite(
                    temp_full, base_args, base_exposure, base_contrast, base_saturation,
                    radial, gradient, spots, self.exiftool_path, self._dngconv_path,
                    side=render_side, lens_correction=lens_correction,
                )
                if jpeg_path is None:
                    raise RuntimeError(
                        "Adobe DNG Converter non è riuscito a renderizzare le correzioni locali a piena risoluzione"
                    )
            else:
                # Shared, kept-alive exiftool process (_persistent_exiftool_execute(), see its
                # own docstring) instead of a fresh `with ExifToolHelper(...) as et:` spawn —
                # user-flagged a full save as unexpectedly slow; profiling save_to_dng() found
                # two separate ~0.3s process-startup costs here and at the final write below,
                # on top of everything else. The render pipeline already made this exact switch
                # for the same reason; this call site just hadn't been touched since.
                _persistent_exiftool_execute(self.exiftool_path, *xmp_args, "-overwrite_original", temp_full)

                rendered_path = os.path.join(render_dir, "rendered.dng")
                convert_args = [self._dngconv_path, "-c"]
                if render_side:
                    convert_args += ["-side", str(render_side)]
                convert_args += ["-p2", "-d", render_dir, "-o", "rendered.dng", temp_full]
                result = subprocess.run(
                    convert_args,
                    capture_output=True, timeout=180, creationflags=_NO_WINDOW_FLAGS,
                )
                if result.returncode != 0 or not os.path.isfile(rendered_path):
                    raise RuntimeError("Adobe DNG Converter non è riuscito a renderizzare il file a piena risoluzione")

                jpeg_path = os.path.join(render_dir, "rendered_preview.jpg")
                if not _extract_dngconverter_preview(self.exiftool_path, rendered_path, jpeg_path):
                    raise RuntimeError("Impossibile estrarre l'anteprima renderizzata da Adobe DNG Converter")
                _apply_lensfun_correction(jpeg_path, lens_correction)

            # Re-encode the full-resolution render at SAVE_PREVIEW_QUALITY and carve out a
            # genuinely small thumbnail — see _prepare_save_preview_jpegs()'s own docstring
            # for why this exists (avoids re-bloating IFD0/SubIFD1/PreviewIFD to full
            # resolution, a confirmed real issue on a real user file, DSC_6795.dng).
            jpeg_path, preview_thumb_path = _prepare_save_preview_jpegs(jpeg_path, render_dir)

            # release our read handle before exiftool rewrites the *original* file in
            # place — on Windows a rename-over-open-file fails silently otherwise (the
            # exact bug we found and root-caused in RethinkRAW)
            self.raw.close()
            self.raw = None

            args = [
                # A genuinely small thumbnail, not the full-resolution render — this single
                # directive writes into *every* physical "PreviewImage" slot this DNG
                # structure has (IFD0/SubIFD1/PreviewIFD), so feeding it a small file here
                # keeps all three small, instead of tripling the full-resolution JpgFromRaw
                # copy below into three more places. See _prepare_save_preview_jpegs().
                "-PreviewImage<=" + preview_thumb_path,
                # Some DNGs (notably ones run through Adobe DNG Converter's -lossy mode,
                # like this project's own test file) also embed a *separate* full-res JPEG
                # under the distinct "JpgFromRaw" tag (SubIFD2 in that structure) — never
                # touched by -PreviewImage<= alone. A LibRaw-based viewer (FastStone, and
                # plausibly others) reads JpgFromRaw preferentially over PreviewImage, so
                # skipping this left edits invisible there forever, confirmed by hashing
                # JpgFromRaw before/after a save: byte-identical without this line. See
                # CLAUDE.md "Save" section for the full before/after verification.
                "-JpgFromRaw<=" + jpeg_path,
                "-tagsfromfile", jpeg_path,
                "-ifd0:imagewidth<imagewidth",
                "-ifd0:imageheight<imageheight",
                "-ifd0:rowsperstrip<imageheight",
                # Stamp this app's own identity into the two flat, ordinarily-writable
                # software-identity tags (unlike EXIF:PreviewApplicationName/
                # PreviewSettingsDigest/PreviewDateTime, which exiftool refuses to write at
                # all — "unsafe for writing" — these two aren't flagged that way). User-
                # requested after noticing a DNGForge-saved file still showed "Adobe DNG
                # Converter" everywhere, with no trace DNGForge had ever touched it at all —
                # confirmed directly against this project's own long-edited test file.
                f"-EXIF:Software={APP_SOFTWARE_NAME}",
                f"-XMP-xmp:CreatorTool={APP_SOFTWARE_NAME}",
            ]
            # Reuse the exact xmp_args already rendered above (not a fresh
            # _build_all_edit_xmp_args() call) so the tags recorded on the original are
            # guaranteed to match what was actually rendered into the embedded preview.
            args += xmp_args
            # Keep the file's own OS timestamps matching when the photo was actually
            # taken, not "now" — otherwise every save bumps the file to today's date,
            # breaking chronological sorting in a file browser. Sourced from the file's
            # own DateTimeOriginal (verified: FileCreateDate is a real, working exiftool
            # pseudo-tag on Windows/NTFS, not just FileModifyDate).
            #
            # CONFIRMED BUG, fixed before this ever shipped: without "-tagsfromfile @" to
            # reset the source back to the target file itself, these two directives were
            # silently inheriting the earlier "-tagsfromfile jpeg_path" context (set a few
            # lines up, for -ifd0:imagewidth<imagewidth) — so they tried reading
            # DateTimeOriginal from the rendered preview JPEG, which has no EXIF data at
            # all, and silently no-op'd (FileModifyDate stayed at "now"). Caught by an
            # automated test that bumped the file's mtime before saving and checked it was
            # restored, not by assuming the CLI syntax was self-evidently right.
            args += ["-tagsfromfile", "@", "-FileModifyDate<DateTimeOriginal", "-FileCreateDate<DateTimeOriginal"]
            args += ["-overwrite_original", path]

            _persistent_exiftool_execute(self.exiftool_path, *args)

            self.statusBar().showMessage(
                f"Salvato in {os.path.basename(path)} ({time.time() - t0:.1f}s) — "
                "rendering Adobe DNG Converter, coerente con Lightroom/ACR per gli stessi tag"
            )
            # Marks "this is now what's on disk" for the unsaved-changes check in closeEvent()
            # (see that method) — captured *after* the write succeeds, not before, so a failed
            # save correctly leaves the file still considered dirty.
            self._saved_edit_state = self._capture_edit_state()
            # A successful save also invalidates this file's session-cache entry (see
            # _session_edit_cache's own __init__ comment): "cached unsaved state" and "what's
            # now on disk" are identical at this exact moment, so there's nothing left for the
            # cache to usefully restore — and leaving the *old*, pre-save entry behind would
            # resurrect stale edits if this same file is ever reloaded again this session
            # (revert_to_saved() already handles its own case; this covers every other path
            # that can trigger a save, since they all funnel through this one function).
            self._session_edit_cache.pop(self.raw_path, None)
            return True
        except Exception as e:
            QMessageBox.critical(self, "Salvataggio non riuscito", str(e))
            self.statusBar().showMessage("Salvataggio fallito")
            return False
        finally:
            if render_dir is not None:
                shutil.rmtree(render_dir, ignore_errors=True)
            if self.raw is None:
                self.raw = rawpy.imread(path)  # always leave the app with a usable handle

    def export_preview_jpeg(self):
        """Renders the *current* editing state (not necessarily what's saved in the DNG —
        this exports live, independent of whether Save has been clicked) at full
        resolution to a standalone JPEG, via Adobe DNG Converter — the sole renderer in
        this app, see CLAUDE.md. At the user's request, the exported file's OS timestamp
        is set to match the photo's own DateTimeOriginal — the same "keep the real
        shooting date, not today" fix applied to Save (see save_to_dng()) — so exports
        sort correctly alongside the source DNGs in a file browser instead of all
        clustering on today's date.

        Unlike before this app's rendering moved entirely to Adobe DNG Converter, JPEG
        quality is no longer something this function controls (there used to be a
        deliberate 92-vs-69 distinction between this export and Save's embedded preview)
        — the exported bytes are exactly what Adobe DNG Converter itself embedded, since
        there's no local re-encoding step left to pick a quality for.
        """
        if self.raw is None or self.raw_path is None:
            return
        if not self.exiftool_path:
            QMessageBox.critical(self, "Esportazione non riuscita",
                                  "exiftool non trovato: impossibile renderizzare l'anteprima.")
            return
        if not self._dngconv_path:
            self._show_dngconv_missing_error()
            return
        default_name = os.path.splitext(os.path.basename(self.raw_path))[0] + ".jpg"
        default_path = os.path.join(os.path.dirname(self.raw_path), default_name)
        out_path, _ = QFileDialog.getSaveFileName(
            self, "Export Preview as JPEG", default_path, "JPEG files (*.jpg *.jpeg)"
        )
        if not out_path:
            return

        self.statusBar().showMessage("Rendering a piena risoluzione con Adobe DNG Converter per l'esportazione…")
        QApplication.processEvents()
        t0 = time.time()
        render_dir = None
        try:
            render_dir = tempfile.mkdtemp(prefix="dngforge_export_")
            temp_full = os.path.join(render_dir, "full.dng")
            shutil.copyfile(self.raw_path, temp_full)

            xmp_args = self._build_all_edit_xmp_args(for_save=True)

            # Local adjustments aren't rendered by Adobe DNG Converter at all (see
            # LocalAdjustmentRenderThread's own docstring) — when any are active, route
            # through that compositing pipeline instead of a single plain render.
            lens_correction = self._lens_correction if self.lens_profile_checkbox.isChecked() else None
            has_local = bool(self.radial_filters or self.gradient_filters or self.spots)
            local = self._local_filters_for_render(for_save=True) if has_local else None
            if local is not None:
                radial, gradient, spots = local
                base_args = self._build_xmp_crs_args(for_save=True)
                base_exposure = 0.0 if self.tone_mode.currentText() == "Auto" else self.exposure_slider.value()
                base_contrast = 0.0 if self.tone_mode.currentText() == "Auto" else self.contrast_slider.value()
                base_saturation = self.saturation_slider.value()
                composited_path = _render_local_adjustments_composite(
                    temp_full, base_args, base_exposure, base_contrast, base_saturation,
                    radial, gradient, spots, self.exiftool_path, self._dngconv_path,
                    lens_correction=lens_correction,
                )
                if composited_path is None:
                    raise RuntimeError(
                        "Adobe DNG Converter non è riuscito a renderizzare le correzioni locali a piena risoluzione"
                    )
                shutil.move(composited_path, out_path)
            else:
                # Shared, kept-alive exiftool process (_persistent_exiftool_execute(), see its
                # own docstring) instead of a fresh `with ExifToolHelper(...) as et:` spawn —
                # user-flagged a full save as unexpectedly slow; profiling save_to_dng() found
                # two separate ~0.3s process-startup costs here and at the final write below,
                # on top of everything else. The render pipeline already made this exact switch
                # for the same reason; this call site just hadn't been touched since.
                _persistent_exiftool_execute(self.exiftool_path, *xmp_args, "-overwrite_original", temp_full)

                rendered_path = os.path.join(render_dir, "rendered.dng")
                result = subprocess.run(
                    [self._dngconv_path, "-c", "-p2", "-d", render_dir, "-o", "rendered.dng", temp_full],
                    capture_output=True, timeout=180, creationflags=_NO_WINDOW_FLAGS,
                )
                if result.returncode != 0 or not os.path.isfile(rendered_path):
                    raise RuntimeError("Adobe DNG Converter non è riuscito a renderizzare il file a piena risoluzione")

                if not _extract_dngconverter_preview(self.exiftool_path, rendered_path, out_path):
                    raise RuntimeError("Impossibile estrarre l'anteprima renderizzata da Adobe DNG Converter")
                _apply_lensfun_correction(out_path, lens_correction)

            # CONFIRMED BUG, fixed: the exported JPEG used to carry *no* metadata at all —
            # confirmed directly by dumping a real exported file's tags: Adobe DNG Converter's
            # embedded JgpFromRaw/PreviewImage stream (what _extract_dngconverter_preview()
            # pulls out) has no EXIF block whatsoever, just raw JPEG image data plus a minimal
            # APP14 color-transform marker — no Make/Model/LensModel/DateTimeOriginal/
            # ExposureTime/ISO/etc., nothing a photo browser or any downstream tool could use.
            # Reported by the user right after confirming the white-balance fixes above:
            # "ora quando esporto il jpg finale non viene scritto nessun tag."
            #
            # Fixed with one combined exiftool call, mirroring save_to_dng()'s own established
            # ordering conventions (each -tagsfromfile call changes the *source* for every
            # subsequent directive until the next -tagsfromfile, same idiom already used and
            # documented for the DateTimeOriginal fix elsewhere in this file):
            #   1. Copy all real camera/EXIF/IPTC/XMP metadata from the source DNG — but
            #      exclude -XMP-crs:all: those are Camera Raw *develop instructions*, meaningless
            #      (and potentially confusing, since this JPEG is already the rendered result) on
            #      a flattened output JPEG, not real photo metadata a viewer would want.
            #   2. Stamp this app's own identity over whatever Software/CreatorTool got copied in
            #      step 1 (usually "Adobe DNG Converter") — same convention save_to_dng() already
            #      uses. A later directive to the same tag overrides an earlier wildcard copy,
            #      confirmed empirically before relying on it here.
            #   3. Re-sync ExifImageWidth/Height from the JPEG's *own* actual pixel dimensions
            #      (self-referential "-tagsfromfile @", the same trick already used for the
            #      timestamp fix below) — needed because step 1 would otherwise copy the DNG's
            #      own raw sensor dimensions verbatim, which silently disagree with the exported
            #      JPEG's real size whenever a crop is active. Verified directly: without this
            #      step, a 2000x1500 cropped export still claimed ExifImageWidth/Height=4288x2848.
            #   4. The pre-existing DateTimeOriginal-based OS timestamp fix (unchanged).
            timestamp_note = ""
            try:
                # Shared, kept-alive exiftool process — see the matching comment in
                # save_to_dng() for why.
                _persistent_exiftool_execute(
                    self.exiftool_path,
                    "-tagsfromfile", self.raw_path, "-all:all", "--xmp-crs:all",
                    f"-EXIF:Software={APP_SOFTWARE_NAME}",
                    f"-XMP-xmp:CreatorTool={APP_SOFTWARE_NAME}",
                    "-tagsfromfile", "@",
                    "-ExifIFD:ExifImageWidth<File:ImageWidth",
                    "-ExifIFD:ExifImageHeight<File:ImageHeight",
                    "-tagsfromfile", self.raw_path,
                    "-FileModifyDate<DateTimeOriginal", "-FileCreateDate<DateTimeOriginal",
                    "-overwrite_original", out_path,
                )
            except Exception:
                timestamp_note = " (metadati/timestamp non impostati: errore exiftool)"

            self.statusBar().showMessage(
                f"Anteprima esportata in {os.path.basename(out_path)} "
                f"({time.time() - t0:.1f}s){timestamp_note}"
            )
        except Exception as e:
            QMessageBox.critical(self, "Esportazione non riuscita", str(e))
            self.statusBar().showMessage("Esportazione fallita")
        finally:
            if render_dir is not None:
                shutil.rmtree(render_dir, ignore_errors=True)

    def _update_image_label(self):
        # Default Preview toggle (see _toggle_default_preview()): a pure display swap — show
        # its own separately-rendered pixmap instead of self._preview_pixmap while active,
        # without touching self._preview_pixmap itself (the real live-edit pipeline keeps
        # rendering into it normally in the background, so turning the toggle back off shows
        # up-to-date real pixels immediately, no re-render needed).
        show_default = (getattr(self, "_default_preview_active", False)
                         and self._default_preview_pixmap is not None)
        pixmap = self._default_preview_pixmap if show_default else getattr(self, "_preview_pixmap", None)
        if pixmap is None:
            return
        if self._zoom_level is None:
            self.image_scroll.setWidgetResizable(True)
            scaled = pixmap.scaled(
                self.image_label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
            self.image_label.setPixmap(scaled)
        else:
            # Explicit zoom: the label takes its own (larger-than-viewport) size and the
            # QScrollArea shows scrollbars, instead of always fitting to the viewport.
            self.image_scroll.setWidgetResizable(False)
            w = max(1, int(pixmap.width() * self._zoom_level))
            h = max(1, int(pixmap.height() * self._zoom_level))
            scaled = pixmap.scaled(w, h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            # Skip the zoom-detail patch while showing the default preview — that patch is
            # always a real render of the *current* edit state (see "Zoom detail rendering"),
            # so compositing it here would paste real edited pixels onto a default-state
            # background, a real mismatch rather than a faithful "before" view.
            if not show_default and self._zoom_detail_pixmap is not None and self._zoom_detail_region is not None:
                scaled = self._compose_zoom_detail(scaled)
            self.image_label.setPixmap(scaled)
            self.image_label.resize(scaled.size())

    def _region_within_effective_crop(self, region):
        """Maps `region` (fractions of the *full raw frame*, as returned by
        _compute_visible_region()) to fractions of the *effective crop's own* frame — i.e.
        the same frame _preview_pixmap and any zoom-detail full-frame render represent.
        Inverse of the crop composition _compute_visible_region() itself does. Returns None
        for a rotated crop/guide (not handled — see that method's own caveat) or a
        degenerate (zero-area) crop.
        """
        crop = self._effective_crop_for_render(for_save=False)
        if crop and crop.get("angle"):
            return None
        if not crop:
            return region["left"], region["top"], region["right"], region["bottom"]
        cw, ch = crop["right"] - crop["left"], crop["bottom"] - crop["top"]
        if cw <= 0 or ch <= 0:
            return None
        left = (region["left"] - crop["left"]) / cw
        right = (region["right"] - crop["left"]) / cw
        top = (region["top"] - crop["top"]) / ch
        bottom = (region["bottom"] - crop["top"]) / ch
        return left, top, right, bottom

    def _compose_zoom_detail(self, base_pixmap):
        """Paints self._zoom_detail_pixmap (a higher-resolution render of just the currently
        visible region, see "Zoom detail rendering") on top of the normal scaled preview, at
        the screen position that region actually covers — never replaces the underlying
        pixmap outright, so panning away from a detail patch before a newer one lands just
        shows the plain (lower-res but always-correct) preview underneath again, not a gap.

        Works in `base_pixmap`'s own full (scaled-by-zoom) coordinate space, not the
        viewport's — so this stays correct regardless of the current scroll position.
        """
        rel = self._region_within_effective_crop(self._zoom_detail_region)
        if rel is None:
            return base_pixmap
        left, top, right, bottom = rel

        bw, bh = base_pixmap.width(), base_pixmap.height()
        x0, y0, x1, y1 = left * bw, top * bh, right * bw, bottom * bh
        if x1 <= x0 or y1 <= y0:
            return base_pixmap

        result = QPixmap(base_pixmap)
        painter = QPainter(result)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        dw, dh = self._zoom_detail_pixmap.width(), self._zoom_detail_pixmap.height()
        painter.drawPixmap(QRectF(x0, y0, x1 - x0, y1 - y0), self._zoom_detail_pixmap,
                            QRectF(0, 0, dw, dh))
        painter.end()
        return result

    def _zoom_label(self, level):
        return "Fit" if level is None else f"{level * 100:.0f}%"

    def _on_zoom_combo_changed(self):
        self._set_zoom_level(self.ZOOM_PRESETS[self.zoom_combo.currentIndex()])

    def _set_zoom_level(self, level):
        """`level` is relative to the *work-copy* preview pixmap's own resolution
        (`WORK_COPY_SIDE`), not necessarily the raw file's actual pixel dimensions. Above
        `ZOOM_DETAIL_THRESHOLD` (i.e. once the work copy is being upscaled to display), a
        second, independent background pipeline re-renders just the currently visible
        region from a full-resolution source at up to `ZOOM_DETAIL_MAX_SIDE` — see "Zoom
        detail rendering" and `_schedule_zoom_detail_render()` — so zoom *does* converge on
        true per-pixel detail past that point, capped by whatever native resolution the
        visible region actually has (never upscaled beyond that).
        """
        self._zoom_level = level
        self.zoom_combo.blockSignals(True)
        # Marquee "zoom to selection" computes an exact-fit level that won't generally
        # land on one of the fixed presets — show the combo as blank rather than crash.
        self.zoom_combo.setCurrentIndex(self.ZOOM_PRESETS.index(level) if level in self.ZOOM_PRESETS else -1)
        self.zoom_combo.blockSignals(False)
        self._update_image_label()
        self._zoom_detail_timer.start(150)

    def _on_scroll_changed(self):
        """Panning while zoomed past ZOOM_DETAIL_THRESHOLD should eventually re-render the
        newly visible area in high detail too. Debounced (longer than the main 60ms edit
        debounce — a detail render is a real conversion, not a cheap comparison) via the
        same self._zoom_detail_timer _set_zoom_level() also uses, so rapid scroll-wheel/drag
        events don't each start their own Adobe DNG Converter process.
        """
        self._zoom_detail_timer.start(300)

    def _on_pan_requested(self, dx, dy):
        """Handles ImageLabel.panRequested — click-and-drag panning directly on the image,
        added after direct user feedback that the plain QScrollArea scrollbars alone weren't
        discoverable/usable enough once zoomed in past the viewport ("la vista è bloccata...
        non è molto utile"). Dragging right/down moves the *content* right/down, so the
        scroll position moves the opposite way — same convention as every other
        click-and-drag image pan (Lightroom included). A no-op in Fit mode (nothing to pan);
        harmless if called anyway since the scrollbars have no range to move within.
        """
        hbar = self.image_scroll.horizontalScrollBar()
        vbar = self.image_scroll.verticalScrollBar()
        hbar.setValue(hbar.value() - dx)
        vbar.setValue(vbar.value() - dy)

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
        """Classic close-confirmation, always shown — every other editor's standard behavior,
        previously missing here entirely (closing always just discarded any in-progress edit
        silently, with no confirmation of any kind, reported directly by the user as feeling
        unpolished). The dialog's actual content depends on file state, not just whether one
        is shown at all:
          - unsaved changes present: Save / Don't Save / Cancel, offering to save first
          - nothing unsaved (including no file open at all): a plain Yes/No "really exit?"

        Dirty-state detection compares the live edit state against self._saved_edit_state (set
        in load_dng() on open and save_to_dng() on a successful save) rather than tracking a
        separate dirty flag, so it can never drift out of sync with what Undo/Redo's own
        _capture_edit_state()-based comparison already considers "changed" — the exact same
        equality check _maybe_push_undo_state() already relies on.
        """
        dirty = (
            self.raw is not None and self._saved_edit_state is not None
            and self._capture_edit_state() != self._saved_edit_state
        )
        if dirty:
            reply = QMessageBox.question(
                self, "Modifiche non salvate",
                f"'{os.path.basename(self.raw_path)}' ha modifiche non salvate.\n\n"
                "Salvare prima di uscire?",
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
                QMessageBox.Save,
            )
            if reply == QMessageBox.Cancel:
                event.ignore()
                return
            if reply == QMessageBox.Save:
                if not self.save_to_dng():
                    # save_to_dng() already showed its own critical dialog explaining why —
                    # don't also close and lose the file on top of a failed save.
                    event.ignore()
                    return
        else:
            reply = QMessageBox.question(
                self, "Esci da DNGForge", "Uscire da DNGForge?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
            )
            if reply != QMessageBox.Yes:
                event.ignore()
                return

        _terminate_persistent_exiftool()
        if self.raw is not None:
            self.raw.close()
        if self._work_dng_dir is not None:
            self._old_work_dirs.append(self._work_dng_dir)
        for d in self._old_work_dirs:
            shutil.rmtree(d, ignore_errors=True)
        for d in self._thumbnail_scan_dirs.values():  # any gallery scan still in flight
            shutil.rmtree(d, ignore_errors=True)
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    window = DNGForge()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
