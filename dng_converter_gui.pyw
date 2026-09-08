#!/usr/bin/env python3
"""
GUI per l'elaborazione batch di file raw (Nikon NEF e DNG).
Adobe DNG Converter accetta il NEF come input, quindi tutte le operazioni
valgono sia per .dng sia per .nef.
Modello logico: Formato · Risoluzione · Qualità.

  Formato output:
    - JPG                (exiftool estrae il JPEG incorporato; Pillow ridimensiona/ricomprime)
    - DNG lossy          (Adobe DNG Converter, compressione lossy)
    - DNG lossless       (Adobe DNG Converter, compressione lossless)

  Risoluzione (limite, non ingrandisce mai):
    - Originale
    - Megapixel N        (DNG: -count;  JPG: lato lungo calcolato per N MP)
    - 4K                 (lato lungo 3840)
    - 2048               (lato lungo 2048)

  Qualità q (solo JPG): editabile, default 69.
  A risoluzione originale + JPG si può scegliere "senza ricompressione"
  (estrae il JPEG incorporato intatto).

Tool esterni (rilevati all'avvio):
  - Adobe DNG Converter   (solo formati DNG)
  - exiftool              (estrazione JPEG incorporato e copia metadati)
Il ridimensionamento/ricompressione JPG e la data del file da Exif sono ora
gestiti in Python (Pillow + os.utime): nconvert ed exiv2 non servono più.
"""

import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, font, messagebox, scrolledtext, ttk

try:
    from PIL import Image
    try:
        _RESAMPLE = Image.Resampling.LANCZOS
    except AttributeError:               # Pillow < 9.1
        _RESAMPLE = Image.LANCZOS
    PIL_OK = True
except Exception:
    Image = None
    PIL_OK = False

IS_WINDOWS = os.name == "nt"
CNW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # niente console sotto pythonw
DPI = 300  # DPI scritto nel JPEG salvato (come negli script originali)
RAW_EXTS = (".dng", ".nef")  # formati raw accettati in input
# Tag exiftool del JPEG incorporato, in ordine di preferenza (DNG: JpgFromRaw; NEF: PreviewImage)
EMBEDDED_TAGS = ("-JpgFromRaw", "-PreviewImage", "-OtherImage")

# Risoluzione preferita di default per ciascun formato (personalizzabile, poi persistita):
#   JPG → 4K   ·   DNG (lossy/lossless) → originale (nessun ridimensionamento)
RES_DEFAULT_PER_FORMAT = {"jpg": "4k", "dng_lossy": "orig", "dng_lossless": "orig"}
CONFIG_NOME = "dng_converter_gui.cfg.json"

# ---------------------------------------------------------------------------
# Rilevamento tool esterni
# ---------------------------------------------------------------------------

ADOBE_DNG_CANDIDATI = [
    r"C:\Program Files\Adobe\Adobe DNG Converter\Adobe DNG Converter.exe",
    r"C:\Program Files (x86)\Adobe\Adobe DNG Converter\Adobe DNG Converter.exe",
    "/Applications/Adobe DNG Converter.app/Contents/MacOS/Adobe DNG Converter",
]


def trova_adobe_dng():
    for p in ADOBE_DNG_CANDIDATI:
        if Path(p).exists():
            return p
    for root in (os.environ.get("ProgramFiles", ""), os.environ.get("ProgramFiles(x86)", "")):
        if not root:
            continue
        for exe in Path(root).glob("Adobe/Adobe DNG Converter*/Adobe DNG Converter*.exe"):
            return str(exe)
    return shutil.which("Adobe DNG Converter")


def rileva_tool() -> dict:
    return {
        "dng":      trova_adobe_dng(),
        "exiftool": shutil.which("exiftool"),
    }


# ---------------------------------------------------------------------------
# Costruzione comandi (condivisa fra anteprima e worker)
# ---------------------------------------------------------------------------

def _fmt_cmd(cmd) -> str:
    out = [Path(str(cmd[0])).name]
    for a in cmd[1:]:
        a = str(a)
        out.append(f'"{a}"' if " " in a else a)
    return " ".join(out)


def _dng_size_flags(opts) -> list:
    if opts["res"] == "mp":
        return ["-count", str(int(opts["mp"] * 1_000_000))]
    if opts["res"] == "4k":
        return ["-side", "3840"]
    if opts["res"] == "2048":
        return ["-side", "2048"]
    return []  # originale


def cmd_dng(src: Path, out_dir: Path, opts, dng_exe) -> list:
    cmd = [dng_exe, "-c", "-p2", "-fl"]
    if opts["format"] == "dng_lossy":
        cmd.append("-lossy")
    cmd += _dng_size_flags(opts)
    cmd += ["-d", str(out_dir), str(src), "-o", src.stem + ".dng"]
    return cmd


def longest_target(opts, dims):
    """Lato lungo di destinazione in px, o None se non serve ridimensionare.
    dims = (w, h) del file, o None (anteprima senza dati per il caso megapixel)."""
    if opts["res"] == "4k":
        return 3840
    if opts["res"] == "2048":
        return 2048
    if opts["res"] == "mp" and dims and dims[0] and dims[1]:
        w, h = dims
        n = int(opts["mp"] * 1_000_000)
        if w * h > n:  # riduci solo se il sorgente è più grande del target
            aspect = max(w, h) / min(w, h)
            return round((n * aspect) ** 0.5)
    return None


def elabora_jpg(in_jpg, out_jpg, opts, log_q) -> bool:
    """Ridimensiona (lato lungo) e ricomprime un JPEG con Pillow, conservando l'EXIF."""
    try:
        with Image.open(in_jpg) as img:
            img.load()
            exif = img.info.get("exif")
            icc = img.info.get("icc_profile")
            tgt = longest_target(opts, img.size)
            if tgt and max(img.size) > tgt:
                scale = tgt / max(img.size)
                img = img.resize((round(img.width * scale), round(img.height * scale)), _RESAMPLE)
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            kw = dict(quality=int(opts["quality"]), dpi=(DPI, DPI))
            if exif:
                kw["exif"] = exif
            if icc:
                kw["icc_profile"] = icc
            img.save(out_jpg, "JPEG", **kw)
        return True
    except Exception as e:
        log_q.put(("error", f"    ✗ Pillow: {e}"))
        return False


def leggi_data_exif(path, exiftool):
    """Data di scatto via exiftool (DateTimeOriginal → CreateDate → ModifyDate)."""
    try:
        r = subprocess.run([exiftool, "-s3", "-d", "%Y-%m-%d %H:%M:%S",
                            "-DateTimeOriginal", "-CreateDate", "-ModifyDate", str(path)],
                           capture_output=True, text=True, stdin=subprocess.DEVNULL,
                           creationflags=CNW, timeout=30)
        for line in r.stdout.splitlines():
            line = line.strip()
            if line:
                try:
                    return datetime.strptime(line, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    continue
    except Exception:
        pass
    return None


def applica_data(path, dt, log_q):
    """Imposta data di modifica/accesso del file dalla data Exif (sostituisce exiv2 -T)."""
    if dt is None:
        log_q.put(("detail", "    (data Exif non trovata: data file invariata)"))
        return
    ts = dt.timestamp()
    try:
        os.utime(path, (ts, ts))
    except Exception as e:
        log_q.put(("detail", f"    (impossibile impostare la data: {e})"))


def is_pure_extract(opts) -> bool:
    """JPG a risoluzione originale senza ricompressione = estrazione lossless."""
    return opts["format"] == "jpg" and opts["res"] == "orig" and opts["no_recompress"]


def tool_richiesti(opts) -> list:
    req = []
    if opts["format"] in ("dng_lossy", "dng_lossless"):
        req.append("dng")
        if opts["timestamp"]:
            req.append("exiftool")   # serve a leggere la data Exif dal DNG prodotto
    else:
        req.append("exiftool")
        if not is_pure_extract(opts):
            req.append("pillow")
    return req


def cartella_output_auto(opts) -> str:
    """Nome cartella output derivato dalle impostazioni (logica leggibile)."""
    res = {"orig": "", "mp": f"{opts['mp']}MP", "4k": "4K", "2048": "2048"}[opts["res"]]
    if opts["format"] == "jpg":
        parti = ["jpg"]
        if res:
            parti.append(res)
        if not is_pure_extract(opts):
            parti.append(f"q{opts['quality']}")
        return "_".join(parti)
    parti = ["dng", "lossy" if opts["format"] == "dng_lossy" else "lossless"]
    if res:
        parti.append(res)
    return "_".join(parti)


def descrivi_comandi(src: Path, opts, tools, dims) -> list:
    """Righe di anteprima per il primo file."""
    def T(n):
        return Path(str(tools.get(n) or n)).name

    base = src.parent
    if opts["format"] == "jpg" and opts["out_current"]:
        out_dir = base
    else:
        out_dir = base / (opts["out_dir_name"] or cartella_output_auto(opts))
    stem = src.stem
    lines = []

    if opts["format"] in ("dng_lossy", "dng_lossless"):
        lines.append(f'mkdir "{out_dir.name}"')
        lines.append(_fmt_cmd(cmd_dng(src, out_dir, opts, tools.get("dng") or "AdobeDNGConverter")))
        if opts["timestamp"]:
            lines.append(f'imposta data file da Exif → {out_dir.name}/{stem}.dng')
        return lines

    # JPG
    if is_pure_extract(opts):
        lines.append(f'mkdir "{out_dir.name}"')
        lines.append(f'exiftool -b -JpgFromRaw "{src.name}" > "{out_dir.name}/{stem}.jpg"')
        lines.append(_fmt_cmd([T("exiftool"), "-tagsfromfile", src.name, "-overwrite_original",
                               f'{out_dir.name}/{stem}.jpg']))
        if opts["timestamp"]:
            lines.append(f'imposta data file da Exif → {out_dir.name}/{stem}.jpg')
        return lines

    extr = "fullsize" if opts["keep_fullsize"] else "«temp»"
    lines.append(f'exiftool -b -JpgFromRaw "{src.name}" > "{extr}/{stem}.jpg"')
    lines.append(_fmt_cmd([T("exiftool"), "-tagsfromfile", src.name, "-overwrite_original", f'{extr}/{stem}.jpg']))
    lines.append(f'mkdir "{out_dir.name}"')
    tgt = longest_target(opts, dims)
    if tgt:
        res_desc = f'ridimensiona a lato lungo {tgt} px, '
    elif opts["res"] == "mp":
        res_desc = f'ridimensiona a {opts["mp"]} MP, '
    else:
        res_desc = ''
    lines.append(f'Pillow: {res_desc}q{opts["quality"]}, {DPI} dpi  →  {out_dir.name}/{stem}.jpg')
    if opts["timestamp"]:
        lines.append(f'imposta data file da Exif → {out_dir.name}/{stem}.jpg')
    if not opts["keep_fullsize"]:
        lines.append(f'(elimina file temporaneo {stem}.jpg)')
    return lines


# ---------------------------------------------------------------------------
# Esecuzione (thread separato)
# ---------------------------------------------------------------------------

def _run(cmd, log_q, quiet=False) -> int:
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", stdin=subprocess.DEVNULL, creationflags=CNW)
    # In modalità quiet l'output si mostra solo se il comando fallisce: Adobe DNG
    # Converter stampa una lunga diagnostica GPU (es. "GPU Warning: GPU3 disabled…")
    # a ogni file, del tutto innocua ma che intaserebbe il log.
    if not quiet or p.returncode != 0:
        for stream in (p.stdout, p.stderr):
            for line in (stream or "").splitlines():
                if line.strip():
                    log_q.put(("detail", "    " + line.rstrip()))
    return p.returncode


def _estrai(src, dest, exiftool, log_q) -> bool:
    """Estrae il JPEG incorporato provando i tag noti (DNG e NEF usano tag diversi)."""
    for tag in EMBEDDED_TAGS:
        try:
            with open(dest, "wb") as f:
                subprocess.run([exiftool, "-b", tag, str(src)],
                               stdout=f, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                               creationflags=CNW)
            if Path(dest).stat().st_size > 0:
                return True
        except Exception as e:
            log_q.put(("error", f"    ✗ Estrazione fallita: {e}"))
            return False
    log_q.put(("error", f"    ✗ Nessun JPEG incorporato in {src.name}"))
    Path(dest).unlink(missing_ok=True)
    return False


def _leggi_dimensioni(path, exiftool):
    try:
        r = subprocess.run([exiftool, "-s", "-s", "-s", "-ImageSize", str(path)],
                           capture_output=True, text=True, stdin=subprocess.DEVNULL,
                           creationflags=CNW, timeout=30)
        m = re.match(r"(\d+)x(\d+)", r.stdout.strip())
        if m:
            return int(m.group(1)), int(m.group(2))
    except Exception:
        pass
    return None


def _esegui_file(src: Path, opts, tools, log_q) -> bool:
    base = src.parent
    stem = src.stem
    if opts["format"] == "jpg" and opts["out_current"]:
        out_dir = base
    else:
        out_dir = base / (opts["out_dir_name"] or cartella_output_auto(opts))

    # --- DNG ---
    if opts["format"] in ("dng_lossy", "dng_lossless"):
        out_dir.mkdir(parents=True, exist_ok=True)
        cmd = cmd_dng(src, out_dir, opts, tools["dng"])
        log_q.put(("cmd", "  $ " + _fmt_cmd(cmd)))
        if _run(cmd, log_q, quiet=True) != 0:
            return False
        out_file = out_dir / (stem + ".dng")
        if not out_file.exists():
            log_q.put(("error", "    ✗ Adobe DNG Converter non ha prodotto output"))
            return False
        if opts["timestamp"]:
            applica_data(out_file, leggi_data_exif(out_file, tools["exiftool"]), log_q)
        return True

    # --- JPG: estrazione pura (nessuna ricompressione) ---
    if is_pure_extract(opts):
        out_dir.mkdir(parents=True, exist_ok=True)
        dest = out_dir / (stem + ".jpg")
        log_q.put(("cmd", f'  $ exiftool -b -JpgFromRaw "{src.name}" > "{dest.name}"'))
        if not _estrai(src, dest, tools["exiftool"], log_q):
            return False
        _run([tools["exiftool"], "-tagsfromfile", str(src), "-overwrite_original", str(dest)], log_q, quiet=True)
        if opts["timestamp"]:
            applica_data(dest, leggi_data_exif(dest, tools["exiftool"]), log_q)
        return True

    # --- JPG: estrazione + Pillow (ridimensiona / ricomprimi) ---
    if opts["keep_fullsize"]:
        full_dir = base / "fullsize"
        full_dir.mkdir(parents=True, exist_ok=True)
        extr = full_dir / (stem + ".jpg")
        temporaneo = False
    else:
        fd, tmpname = tempfile.mkstemp(suffix=".jpg")
        os.close(fd)
        extr = Path(tmpname)
        temporaneo = True

    try:
        log_q.put(("cmd", f'  $ exiftool -b -JpgFromRaw "{src.name}"  (estrazione)'))
        if not _estrai(src, extr, tools["exiftool"], log_q):
            return False
        _run([tools["exiftool"], "-tagsfromfile", str(src), "-overwrite_original", str(extr)], log_q, quiet=True)
        dt = leggi_data_exif(extr, tools["exiftool"]) if opts["timestamp"] else None
        if opts["timestamp"] and not temporaneo:
            applica_data(extr, dt, log_q)   # data anche sul fullsize conservato

        out_dir.mkdir(parents=True, exist_ok=True)
        out_jpg = out_dir / (stem + ".jpg")
        log_q.put(("cmd", f'  $ Pillow → {out_jpg.name}  (q{opts["quality"]}, {DPI} dpi)'))
        if not elabora_jpg(extr, out_jpg, opts, log_q):
            return False
        if opts["timestamp"]:
            applica_data(out_jpg, dt, log_q)
        return out_jpg.exists()
    finally:
        if temporaneo:
            extr.unlink(missing_ok=True)


def worker(files, opts, tools, log_q, stop_ev):
    ok = err = 0
    for src in files:
        if stop_ev.is_set():
            log_q.put(("detail", "\nInterrotto dall'utente."))
            break
        log_q.put(("info", f"\n▶ {src.name}"))
        try:
            if _esegui_file(src, opts, tools, log_q):
                ok += 1
                log_q.put(("ok", f"  ✓ {src.name}"))
            else:
                err += 1
        except Exception as e:
            err += 1
            log_q.put(("error", f"    ✗ Eccezione: {e}"))
    log_q.put(("summary", f"\n{'='*60}\nCompletato: {ok} OK, {err} errori\n{'='*60}"))
    log_q.put(("done", None))


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Elaborazione raw (DNG/NEF) — batch")
        self.resizable(True, True)
        self.minsize(880, 720)

        self._all_files: list[Path] = []
        self._stop_ev = threading.Event()
        self._log_q = queue.Queue()
        self._tools = rileva_tool()
        self._dims_cache: dict = {}
        self._outdir_auto = True  # True finché l'utente non modifica a mano il nome cartella
        self._res_per_format = dict(RES_DEFAULT_PER_FORMAT)

        self._build_ui()
        self._carica_config()
        self._sync_stati()
        self._aggiorna_stato_tool()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll_log()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        pad = dict(padx=10, pady=4)

        # Cartella
        frm_dir = ttk.LabelFrame(self, text="Cartella sorgente (contenente i .dng/.nef)")
        frm_dir.pack(fill="x", **pad)
        self._var_dir = tk.StringVar(value=str(Path.cwd()))
        ttk.Entry(frm_dir, textvariable=self._var_dir).pack(
            side="left", fill="x", expand=True, padx=5, pady=5)
        ttk.Button(frm_dir, text="Sfoglia…", command=self._scegli_dir).pack(side="left", padx=2)
        ttk.Button(frm_dir, text="Cartella corrente", command=self._dir_corrente).pack(side="left", padx=5)

        # Formato
        frm_fmt = ttk.LabelFrame(self, text="Formato output")
        frm_fmt.pack(fill="x", **pad)
        self._var_format = tk.StringVar(value="jpg")
        for val, lbl in [("jpg", "JPG"), ("dng_lossy", "DNG lossy"), ("dng_lossless", "DNG lossless")]:
            ttk.Radiobutton(frm_fmt, text=lbl, variable=self._var_format, value=val,
                            command=self._on_format_change).pack(side="left", padx=12, pady=4)

        # Risoluzione
        frm_res = ttk.LabelFrame(self, text="Risoluzione (limite lato lungo; non ingrandisce)")
        frm_res.pack(fill="x", **pad)
        self._var_res = tk.StringVar(value="orig")
        ttk.Radiobutton(frm_res, text="Originale", variable=self._var_res, value="orig",
                        command=self._on_res_change).pack(side="left", padx=8, pady=4)
        ttk.Radiobutton(frm_res, text="Megapixel", variable=self._var_res, value="mp",
                        command=self._on_res_change).pack(side="left", padx=(12, 2))
        self._var_mp = tk.IntVar(value=12)
        self._spin_mp = ttk.Spinbox(frm_res, from_=1, to=200, textvariable=self._var_mp,
                                    width=5, command=self._on_change)
        self._spin_mp.pack(side="left")
        self._spin_mp.bind("<KeyRelease>", lambda _e: self._on_change())
        ttk.Label(frm_res, text="MP").pack(side="left", padx=(2, 12))
        ttk.Radiobutton(frm_res, text="4K (lato lungo 3840)", variable=self._var_res, value="4k",
                        command=self._on_res_change).pack(side="left", padx=8)
        ttk.Radiobutton(frm_res, text="2048 (lato lungo)", variable=self._var_res, value="2048",
                        command=self._on_res_change).pack(side="left", padx=8)

        # Qualità JPG
        self._frm_q = ttk.LabelFrame(self, text="JPEG")
        self._frm_q.pack(fill="x", **pad)
        ttk.Label(self._frm_q, text="Qualità (q):").pack(side="left", padx=(8, 2))
        self._var_q = tk.IntVar(value=69)
        self._spin_q = ttk.Spinbox(self._frm_q, from_=1, to=100, textvariable=self._var_q,
                                   width=5, command=self._on_change)
        self._spin_q.pack(side="left")
        self._spin_q.bind("<KeyRelease>", lambda _e: self._on_change())
        ttk.Label(self._frm_q, text="(1–100)", foreground="gray").pack(side="left", padx=(4, 20))
        self._var_norec = tk.BooleanVar(value=True)
        self._chk_norec = ttk.Checkbutton(
            self._frm_q, text="A risoluzione originale: estrai senza ricomprimere (lossless)",
            variable=self._var_norec, command=self._on_change)
        self._chk_norec.pack(side="left", padx=4)
        self._var_keepfull = tk.BooleanVar(value=False)
        self._chk_keepfull = ttk.Checkbutton(
            self._frm_q, text="conserva i JPEG a piena risoluzione (fullsize/)",
            variable=self._var_keepfull, command=self._on_change)
        self._chk_keepfull.pack(side="left", padx=12)

        # Output + opzioni comuni
        frm_out = ttk.LabelFrame(self, text="Output e opzioni")
        frm_out.pack(fill="x", **pad)
        ro = ttk.Frame(frm_out); ro.pack(fill="x", padx=5, pady=3)
        ttk.Label(ro, text="Sottocartella output:").pack(side="left")
        self._var_out = tk.StringVar(value="")
        e_out = ttk.Entry(ro, textvariable=self._var_out, width=22)
        e_out.pack(side="left", padx=4)
        e_out.bind("<KeyRelease>", lambda _e: setattr(self, "_outdir_auto", False))
        self._var_outcur = tk.BooleanVar(value=False)
        self._chk_outcur = ttk.Checkbutton(ro, text="nella cartella corrente (solo JPG)",
                                           variable=self._var_outcur, command=self._on_change)
        self._chk_outcur.pack(side="left", padx=12)
        rc = ttk.Frame(frm_out); rc.pack(fill="x", padx=5, pady=3)
        self._var_ts = tk.BooleanVar(value=True)
        ttk.Checkbutton(rc, text="Imposta data del file da Exif",
                        variable=self._var_ts, command=self._on_change).pack(side="left")
        self._var_rec = tk.BooleanVar(value=False)
        ttk.Checkbutton(rc, text="Includi sottocartelle (ricorsivo)",
                        variable=self._var_rec, command=self._cerca_file).pack(side="left", padx=20)

        # File trovati
        frm_files = ttk.LabelFrame(self, text="File raw .dng/.nef trovati  (Ctrl+A = tutti)")
        frm_files.pack(fill="both", **pad)
        self._listbox = tk.Listbox(frm_files, selectmode="extended", height=6, exportselection=False)
        self._listbox.pack(side="left", fill="both", expand=True, padx=5, pady=5)
        sb = ttk.Scrollbar(frm_files, orient="vertical", command=self._listbox.yview)
        sb.pack(side="right", fill="y", pady=5)
        self._listbox.configure(yscrollcommand=sb.set)
        self._listbox.bind("<<ListboxSelect>>", lambda _e: self._aggiorna_anteprima())
        frm_cerca = ttk.Frame(self); frm_cerca.pack(fill="x", padx=10)
        ttk.Button(frm_cerca, text="🔍 Cerca raw", command=self._cerca_file).pack(side="left")
        self._lbl_files = ttk.Label(frm_cerca, text="")
        self._lbl_files.pack(side="left", padx=10)

        # Anteprima
        frm_prev = ttk.LabelFrame(self, text="Anteprima comandi (primo file selezionato)")
        frm_prev.pack(fill="x", **pad)
        self._txt_prev = tk.Text(frm_prev, height=6, state="disabled", wrap="none",
                                 font=("Consolas", 8), background="#f5f5f5")
        self._txt_prev.pack(side="left", fill="both", expand=True, padx=5, pady=5)
        sbp = ttk.Scrollbar(frm_prev, orient="vertical", command=self._txt_prev.yview)
        sbp.pack(side="right", fill="y", pady=5)
        self._txt_prev.configure(yscrollcommand=sbp.set)

        # Stato tool
        self._lbl_tool = ttk.Label(self, text="", foreground="gray")
        self._lbl_tool.pack(fill="x", padx=12)

        # Avvio
        frm_run = ttk.Frame(self); frm_run.pack(fill="x", **pad)
        self._btn_start = ttk.Button(frm_run, text="▶  Avvia", command=self._avvia)
        self._btn_start.pack(side="left")
        self._btn_stop = ttk.Button(frm_run, text="⏹  Interrompi", command=self._interrompi, state="disabled")
        self._btn_stop.pack(side="left", padx=8)
        self._lbl_stato = ttk.Label(frm_run, text="")
        self._lbl_stato.pack(side="left", padx=10)

        # Log
        frm_log = ttk.LabelFrame(self, text="Log")
        frm_log.pack(fill="both", expand=True, **pad)
        mono = font.Font(family="Consolas", size=9)
        self._log = scrolledtext.ScrolledText(frm_log, wrap="word", font=mono, height=10,
                                              state="disabled", background="#1e1e1e", foreground="#d4d4d4")
        self._log.pack(fill="both", expand=True, padx=5, pady=5)
        for tag, fg in [("info", "#569cd6"), ("ok", "#4ec9b0"), ("error", "#f44747"),
                        ("cmd", "#808080"), ("detail", "#808080"), ("summary", "#ce9178")]:
            self._log.tag_config(tag, foreground=fg)

    # ------------------------------------------------------------------ opts
    def _opts(self) -> dict:
        return {
            "format":        self._var_format.get(),
            "res":           self._var_res.get(),
            "mp":            max(1, self._var_mp.get() or 1),
            "quality":       self._var_q.get(),
            "no_recompress": bool(self._var_norec.get()),
            "keep_fullsize": bool(self._var_keepfull.get()),
            "timestamp":     bool(self._var_ts.get()),
            "recursive":     bool(self._var_rec.get()),
            "out_current":   bool(self._var_outcur.get()),
            "out_dir_name":  self._var_out.get().strip(),
        }

    # ------------------------------------------------------------------ preferenze
    def _on_format_change(self):
        # applica la risoluzione preferita memorizzata per il formato scelto
        self._var_res.set(self._res_per_format.get(self._var_format.get(), "orig"))
        self._on_change()

    def _on_res_change(self):
        # ricorda la risoluzione scelta per il formato corrente
        self._res_per_format[self._var_format.get()] = self._var_res.get()
        self._on_change()

    def _config_file(self):
        try:
            return Path(__file__).with_name(CONFIG_NOME)
        except NameError:
            return Path.cwd() / CONFIG_NOME

    def _carica_config(self):
        try:
            data = json.loads(self._config_file().read_text(encoding="utf-8"))
        except Exception:
            data = {}
        for k, v in (data.get("res_per_format") or {}).items():
            if k in RES_DEFAULT_PER_FORMAT:
                self._res_per_format[k] = v
        self._var_format.set(data.get("format", self._var_format.get()))
        self._var_q.set(int(data.get("quality", self._var_q.get())))
        self._var_mp.set(int(data.get("mp", self._var_mp.get())))
        self._var_norec.set(bool(data.get("no_recompress", self._var_norec.get())))
        self._var_keepfull.set(bool(data.get("keep_fullsize", self._var_keepfull.get())))
        self._var_ts.set(bool(data.get("timestamp", self._var_ts.get())))
        # risoluzione iniziale = preferita per il formato attivo
        self._var_res.set(self._res_per_format.get(self._var_format.get(), "orig"))

    def _salva_config(self):
        data = {
            "format":        self._var_format.get(),
            "res_per_format": self._res_per_format,
            "quality":       self._var_q.get(),
            "mp":            self._var_mp.get(),
            "no_recompress": bool(self._var_norec.get()),
            "keep_fullsize": bool(self._var_keepfull.get()),
            "timestamp":     bool(self._var_ts.get()),
        }
        try:
            self._config_file().write_text(json.dumps(data, indent=2, ensure_ascii=False),
                                           encoding="utf-8")
        except Exception:
            pass

    def _on_close(self):
        self._salva_config()
        self.destroy()

    # ------------------------------------------------------------------ reattività
    def _on_change(self):
        self._sync_stati()
        self._aggiorna_stato_tool()
        self._aggiorna_anteprima()

    def _sync_stati(self):
        opts = self._opts()
        is_jpg = opts["format"] == "jpg"
        is_dng = not is_jpg
        # spinbox megapixel attivo solo con risoluzione "mp"
        self._spin_mp.configure(state="normal" if opts["res"] == "mp" else "disabled")
        # controlli JPEG attivi solo per JPG
        self._spin_q.configure(state="normal" if is_jpg else "disabled")
        # "senza ricompressione" solo per JPG a risoluzione originale
        self._chk_norec.configure(state="normal" if (is_jpg and opts["res"] == "orig") else "disabled")
        # conserva fullsize solo se JPG e c'è ricompressione (non estrazione pura)
        reprocess = is_jpg and not is_pure_extract(opts)
        self._chk_keepfull.configure(state="normal" if reprocess else "disabled")
        # cartella corrente solo per JPG (per DNG sovrascriverebbe il sorgente)
        self._chk_outcur.configure(state="normal" if is_jpg else "disabled")
        if is_dng and self._var_outcur.get():
            self._var_outcur.set(False)
        # aggiorna il nome cartella suggerito (se non modificato a mano)
        if self._outdir_auto:
            self._var_out.set(cartella_output_auto(opts))

    def _tool_presente(self, k):
        return PIL_OK if k == "pillow" else bool(self._tools.get(k))

    def _aggiorna_stato_tool(self):
        nomi = {"dng": "Adobe DNG Converter", "exiftool": "exiftool", "pillow": "Pillow (Python)"}
        req = tool_richiesti(self._opts())
        parti, manca = [], False
        for k in ("dng", "exiftool", "pillow"):
            if k in req:
                trovato = self._tool_presente(k)
                manca = manca or not trovato
                parti.append(("✓ " if trovato else "✗ ") + nomi[k])
        testo = "Tool necessari: " + "   ".join(parti)
        if manca:
            testo += "   →  procurati quelli con ✗ (Pillow: pip install Pillow)"
        self._lbl_tool.configure(text=testo, foreground="#cc3333" if manca else "#128a12")

    def _dims_correnti(self):
        """Dimensioni (aspetto) del file selezionato, per l'anteprima megapixel."""
        sel = self._listbox.curselection()
        if not sel or not self._all_files:
            return None
        src = self._all_files[sel[0]]
        if src not in self._dims_cache:
            self._dims_cache[src] = _leggi_dimensioni(src, self._tools["exiftool"]) if self._tools.get("exiftool") else None
        return self._dims_cache[src]

    def _aggiorna_anteprima(self):
        self._txt_prev.configure(state="normal")
        self._txt_prev.delete("1.0", "end")
        sel = self._listbox.curselection()
        if sel and self._all_files:
            src = self._all_files[sel[0]]
        elif self._all_files:
            src = self._all_files[0]
        else:
            src = Path(self._var_dir.get().strip() or ".") / "esempio.dng"
        dims = self._dims_correnti() if self._opts()["res"] == "mp" else None
        try:
            for riga in descrivi_comandi(src, self._opts(), self._tools, dims):
                self._txt_prev.insert("end", riga + "\n")
        except Exception as e:
            self._txt_prev.insert("end", f"(errore anteprima: {e})")
        self._txt_prev.configure(state="disabled")

    # ------------------------------------------------------------------ cartella/file
    def _scegli_dir(self):
        d = filedialog.askdirectory(initialdir=self._var_dir.get().strip() or str(Path.cwd()))
        if d:
            self._var_dir.set(d)
            self._cerca_file()

    def _dir_corrente(self):
        self._var_dir.set(str(Path.cwd()))
        self._cerca_file()

    def _cerca_file(self):
        d = self._var_dir.get().strip()
        if not d or not Path(d).is_dir():
            messagebox.showwarning("Attenzione", "Seleziona prima una cartella valida.")
            return
        p = Path(d)
        it = p.rglob("*") if self._var_rec.get() else p.glob("*")
        found = sorted({f for f in it if f.suffix.lower() in RAW_EXTS})
        self._all_files = found
        self._dims_cache.clear()
        self._listbox.delete(0, "end")
        for f in found:
            self._listbox.insert("end", str(f.relative_to(p)))
        self._listbox.select_set(0, "end")
        self._lbl_files.config(text=f"{len(found)} file raw trovati")
        self._aggiorna_anteprima()

    # ------------------------------------------------------------------ avvio
    def _avvia(self):
        d = self._var_dir.get().strip()
        if not d or not Path(d).is_dir():
            messagebox.showwarning("Attenzione", "Seleziona una cartella valida.")
            return
        sel = self._listbox.curselection()
        if not sel:
            messagebox.showwarning("Attenzione", "Nessun file selezionato.")
            return
        files = [self._all_files[i] for i in sel]
        opts = self._opts()
        if not opts["out_current"] and not (opts["out_dir_name"] or cartella_output_auto(opts)):
            messagebox.showwarning("Attenzione", "Specifica una sottocartella di output.")
            return
        opts["out_dir_name"] = opts["out_dir_name"] or cartella_output_auto(opts)

        mancanti = [t for t in tool_richiesti(opts) if not self._tool_presente(t)]
        if mancanti:
            nomi = {"dng": "Adobe DNG Converter", "exiftool": "exiftool",
                    "pillow": "Pillow  (pip install Pillow)"}
            messagebox.showerror("Tool mancanti",
                                 "Mancano:\n\n" + "\n".join("• " + nomi[t] for t in mancanti))
            return

        self._log_clear()
        self._log_write("info", f"Inizio: {len(files)} file · formato {opts['format']} · "
                                f"risoluzione {opts['res']}\n")
        self._stop_ev.clear()
        self._btn_start.configure(state="disabled")
        self._btn_stop.configure(state="normal")
        self._lbl_stato.config(text="In corso…")
        threading.Thread(target=worker,
                         args=(files, opts, self._tools, self._log_q, self._stop_ev),
                         daemon=True).start()

    def _interrompi(self):
        self._stop_ev.set()
        self._lbl_stato.config(text="Interruzione…")

    # ------------------------------------------------------------------ log
    def _log_clear(self):
        self._log.configure(state="normal")
        self._log.delete("1.0", "end")
        self._log.configure(state="disabled")

    def _log_write(self, tag, text):
        self._log.configure(state="normal")
        self._log.insert("end", text + "\n", tag)
        self._log.see("end")
        self._log.configure(state="disabled")

    def _poll_log(self):
        try:
            while True:
                tag, msg = self._log_q.get_nowait()
                if tag == "done":
                    self._btn_start.configure(state="normal")
                    self._btn_stop.configure(state="disabled")
                    self._lbl_stato.config(text="Completato")
                    for i in range(3):
                        self.after(i * 250, self.bell)
                else:
                    self._log_write(tag, msg)
        except queue.Empty:
            pass
        self.after(100, self._poll_log)


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    App().mainloop()
