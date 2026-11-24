"""Simple GUI for the Instrument List Transcriber.

Pick the drawings, pick a model, press Run. Everything else is worked out for you (endpoint, API host,
token limit, render DPI, reference images, output file names) and lives under "Advanced" if you want to
override it. Every setting is saved automatically as you change it and restored on the next launch.
"""
import argparse
import datetime
import gc
import json
import os
import queue
import re
import sys
import threading
import time
import traceback
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Dict, List, Optional

# import Claude_Code_Backend as claude_code
# import Cable_Workbook_Step as cable_step
# import Instrument_List_Transcriber as ilt

APP_DIR = Path(__file__).resolve().parent
SETTINGS_FILE = APP_DIR / "Settings/settings.json"
OLD_SETTINGS_FILE = APP_DIR / "Settings/instrument_list_gui_inputs.txt"  # read once to carry over earlier choices
# REFERENCE_IMAGES = {
#     "controls": APP_DIR / "AI Reference - Controls - smaller.png",
#     "pids": APP_DIR / "AI Reference - P&IDs - smaller.png",
# }
WORKING_DIR = APP_DIR / "Working"  # EVERY file this program creates goes here; job files are only ever read
LOG_DIR = WORKING_DIR / "logs"  # one log file per run, written live
LOCAL_HOST = "http://localhost:11434"
CLOUD_HOSTS = {
    "Anthropic": "https://api.anthropic.com"
    # "OpenAI": "https://api.openai.com",
    # "Google": "https://generativelanguage.googleapis.com",
}
DEFAULT_MAX_TOKENS = 8192
DEFAULT_DPI = 300
HISTORY_LENGTH = 8

MODES = [
    ("all", "Full run")
    # ("controls", "Controls drawings only"),
    # ("pids", "P&ID drawings only"),
    # ("combine", "Combine two saved results"),
]

# Every setting the window remembers: name -> default. Booleans and strings only; the rest is stored separately.
# DEFAULTS: Dict[str, object] = {
#     "mode": "all",
#     "controls_pdf": "", "controls_pages": "",
#     "pids_pdf": "", "pids_pages": "",
#     "controls_json": "", "pids_json": "",
#     "model": "", "api_key": "",
#     "out_dir": "",
#     "save_excel": True,
#     "use_ocr": True,
#     "advanced_open": False,
#     # advanced
#     "controls_model": "", "pids_model": "", "combine_model": "",
#     "max_tokens": "", "dpi": "",
#     "auto_reference": True, "controls_reference": "", "pids_reference": "",
#     "ocr_scale": "3.0", "ocr_tables": True, "ocr_pictures": False,
#     # final step: fill the Cable Calculations workbook from the combined list
#     "fill_workbook": True, "workbook_template": str(cable_step.DEFAULT_TEMPLATE),
# }


# --------------------------------------------------------------------------------------------- settings
def load_settings() -> dict:
    """Saved settings, seeded from the older settings file the first time this window is used."""
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass

    seeded: dict = {}
    try:
        old = json.loads(OLD_SETTINGS_FILE.read_text(encoding="utf-8"))
        if isinstance(old, dict):
            for new_key, old_key in (("controls_pdf", "controls_pdf"), ("pids_pdf", "pids_pdf"),
                                     ("controls_pages", "controls_pages"),
                                     ("pids_pages", "pids_pages")):
                if old.get(old_key):
                    seeded[new_key] = old[old_key]
            seeded["model"] = old.get("controls_model") or old.get("model") or ""
            seeded["api_key"] = next((old[k] for k in ("api_key", "controls_api_key", "pids_api_key") if old.get(k)
                                      and old[k] != "anything"), "")
            if old.get("run_mode") in {m for m, _ in MODES}:
                seeded["mode"] = old["run_mode"]
    except (OSError, ValueError):
        pass
    return seeded


def write_settings(data: dict) -> None:
    tmp = SETTINGS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(SETTINGS_FILE)  # atomic, so a crash mid-save can't leave a half-written file


# ------------------------------------------------------------------------------------------ model logic
class RunLog:
    """One log file per run: everything shown in the window (status lines and streamed model output) is also
    written here as it happens, and flushed on every write so the file is complete even if the program dies."""

    def __init__(self, mode: str) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.path = LOG_DIR / f"run_{stamp}_{mode}.log"
        self._file = self.path.open("w", encoding="utf-8", buffering=1)
        self._lock = threading.Lock()
        self.started = time.time()

    def line(self, message: str) -> None:
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        self.chunk(f"[{stamp}] {message}\n")

    def chunk(self, text: str) -> None:
        with self._lock:
            if not self._file.closed:
                self._file.write(text)
                self._file.flush()

    def close(self, outcome: str) -> None:
        self.line(f"{outcome} (took {time.time() - self.started:.0f}s)")
        with self._lock:
            self._file.close()


# def _guess_provider(model: str, api_key: str) -> str:
#     lowered = model.lower()
#     if ilt._is_claude_model(model):
#         return "Anthropic"
#     if lowered.startswith(("gpt", "o1", "o3", "o4", "chatgpt")):
#         return "OpenAI"
#     if lowered.startswith("gemini"):
#         return "Google"
#     if api_key.startswith("sk-ant"):
#         return "Anthropic"
#     if api_key.startswith("AIza"):
#         return "Google"
#     return "OpenAI"


# def _providers_for_key(api_key: str) -> List[str]:
#     if api_key.startswith("sk-ant"):
#         return ["Anthropic"]
#     if api_key.startswith("AIza"):
#         return ["Google"]
#     if api_key.startswith("sk-"):
#         return ["OpenAI"]
#     return []


def _auto_name(pdf: str, suffix: str) -> str:
    stem = re.sub(r" - (controls|pids)$", "", Path(pdf).stem)  # saved stage results feed the combine step
    return f"{stem} - {suffix}"


def flatten(items):
    """Every single value inside `items`, however deeply nested in lists/tuples.
    flatten(["A", ["B", ["C"]]]) -> "A", "B", "C".  flatten("A") -> "A"."""
    if isinstance(items, (list, tuple)):
        for item in items:
            yield from flatten(item)
    else:
        yield items


def map_nested(func, items):
    """Call `func` on every single value in `items` and return the results in the SAME nested shape.
    map_nested(f, ["a", ["b", "c"]]) -> [f("a"), [f("b"), f("c")]].  map_nested(f, "a") -> f("a")."""
    if isinstance(items, (list, tuple)):
        return [map_nested(func, item) for item in items]
    return func(items)


def working_path(name):
    """Path for a new file called `name` inside the Working folder (created if missing).
    `name` may be a nested list of names (same-shape list of paths back)."""
    if isinstance(name, (list, tuple)):
        return map_nested(working_path, name)
    WORKING_DIR.mkdir(parents=True, exist_ok=True)
    return WORKING_DIR / name


EXCEL_EXTENSIONS = (".xlsx", ".xlsm", ".xls")
CHECK_FILE_SUFFIX = " - turn-in check"  # old check workbooks from earlier versions may sit in job folders; never pick one as the turn-in

# Opens the workbook read-only in a hidden Excel and exports every sheet to one PDF.
# Paths come in through environment variables so spaces, quotes, &, etc. in names are safe.
_EXCEL_TO_PDF_PS = r"""
$ErrorActionPreference = 'Stop'
$xl = New-Object -ComObject Excel.Application
try {
    $xl.Visible = $false
    $xl.DisplayAlerts = $false
    $wb = $xl.Workbooks.Open($env:XL_SRC, 0, $true)
    try { $wb.ExportAsFixedFormat(0, $env:XL_DST) } finally { $wb.Close($false) }
} finally {
    $xl.Quit()
    [void][Runtime.InteropServices.Marshal]::ReleaseComObject($xl)
}
"""


def excel_to_pdf(excel_file, pdf_file=None):
    """Convert an Excel workbook to PDF using the installed Excel. The PDF goes next to the workbook
    with the same name unless `pdf_file` is given. Returns the PDF path; raises RuntimeError on failure.
    `excel_file` may be a nested list; the result is a nested list of PDF paths in the same shape.
    If `pdf_file` is also given with a list, it must have the same shape."""
    import subprocess
    if isinstance(excel_file, (list, tuple)):
        if pdf_file is None:
            return map_nested(excel_to_pdf, excel_file)
        if not isinstance(pdf_file, (list, tuple)) or len(pdf_file) != len(excel_file):
            raise ValueError("excel_file and pdf_file lists must have the same shape")
        return [excel_to_pdf(x, p) for x, p in zip(excel_file, pdf_file)]
    src = Path(excel_file).resolve()
    dst = Path(pdf_file).resolve() if pdf_file else working_path(f"{src.stem}.pdf")
    env = dict(os.environ, XL_SRC=str(src), XL_DST=str(dst))
    proc = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _EXCEL_TO_PDF_PS],
                          env=env, capture_output=True, text=True, timeout=180,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if proc.returncode != 0 or not dst.is_file():
        raise RuntimeError((proc.stderr or proc.stdout or "Excel export failed").strip())
    return dst


# Opens the workbook read-only in a hidden Excel, recalculates every formula (so TODAY() etc. are current, not the
# values from when the file was last saved) and saves a copy as .xlsx (51 = xlOpenXMLWorkbook).
_EXCEL_TO_XLSX_PS = r"""
$ErrorActionPreference = 'Stop'
$xl = New-Object -ComObject Excel.Application
try {
    $xl.Visible = $false
    $xl.DisplayAlerts = $false
    $wb = $xl.Workbooks.Open($env:XL_SRC, 0, $true)
    try { $xl.CalculateFull(); $wb.SaveAs($env:XL_DST, 51) } finally { $wb.Close($false) }
} finally {
    $xl.Quit()
    [void][Runtime.InteropServices.Marshal]::ReleaseComObject($xl)
}
"""


def excel_to_xlsx(excel_file, xlsx_file=None):
    """Save a recalculated copy of an Excel workbook as .xlsx using the installed Excel, so openpyxl can read it
    (old .xls) and sees current formula results (openpyxl cannot calculate formulas itself).
    The copy goes to the temp folder unless `xlsx_file` is given. Returns its path; raises RuntimeError on failure.
    `excel_file` may be a nested list; the result is a nested list of .xlsx paths in the same shape.
    If `xlsx_file` is also given with a list, it must have the same shape."""
    import subprocess
    if isinstance(excel_file, (list, tuple)):
        if xlsx_file is None:
            return map_nested(excel_to_xlsx, excel_file)
        if not isinstance(xlsx_file, (list, tuple)) or len(xlsx_file) != len(excel_file):
            raise ValueError("excel_file and xlsx_file lists must have the same shape")
        return [excel_to_xlsx(x, p) for x, p in zip(excel_file, xlsx_file)]
    src = Path(excel_file).resolve()
    dst = Path(xlsx_file).resolve() if xlsx_file else working_path(f"{src.stem} (recalculated).xlsx")
    dst.unlink(missing_ok=True)  # Excel's SaveAs would otherwise stop to ask about overwriting
    env = dict(os.environ, XL_SRC=str(src), XL_DST=str(dst))
    proc = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _EXCEL_TO_XLSX_PS],
                          env=env, capture_output=True, text=True, timeout=180,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if proc.returncode != 0 or not dst.is_file():
        raise RuntimeError((proc.stderr or proc.stdout or "Excel conversion failed").strip())
    return dst


_WINDOW_POSITION: Dict[str, str] = {}  # "+x+y" of the last program window, so the next one opens in the same place


def make_window(title: str, size: str, min_size=None) -> tk.Tk:
    """New main window (dark "black" theme when ttkthemes is installed) of `size` ("640x400"), opened at the same
    screen position as the previous program window, so clicking Next / moving to the next step does not jump
    around. The position is remembered whenever the window is moved.
    `title` may be a nested list of titles: one window each, same-shape list of windows back."""
    if isinstance(title, (list, tuple)):
        return map_nested(lambda one: make_window(one, size, min_size), title)  # pyright: ignore[reportReturnType]
    try:
        from ttkthemes import ThemedTk  # pip install ttkthemes
        root = ThemedTk(theme="black", themebg=True)  # themebg also paints the window background
    except ImportError:
        root = tk.Tk()  # ttkthemes missing: fall back to the default look
    root.title(title)
    root.geometry(size + _WINDOW_POSITION.get("xy", ""))
    if min_size:
        root.minsize(*min_size)

    def remember(event) -> None:
        # Child widgets pass their Configure events up to the window too; only the window's own count.
        if event.widget is root and root.winfo_ismapped():
            match = re.search(r"[+-]-?\d+[+-]-?\d+$", root.geometry())
            if match:
                _WINDOW_POSITION["xy"] = match.group()

    root.bind("<Configure>", remember, add="+")
    return root


def run_with_progress(title: str, job, log_name: str = "run"):
    """Loading screen with a live log, like the submodule's Progress panel.
    Runs `job(log, stream)` on a background thread while the window shows status, a progress bar and the log.
    `log(text)` adds a time-stamped line; `stream(text)` adds raw model output as it arrives.
    Everything shown is also written to logs/<log_name>_<date>_<time>.log.
    Returns whatever `job` returns, or None if it failed or the window was closed early."""
    # Free leftover Tk objects from earlier windows NOW, on the main thread. Otherwise Python's garbage
    # collector may free them later from the worker thread, which aborts with
    # "Tcl_AsyncDelete: async handler deleted by the wrong thread".
    gc.collect()
    root = make_window(title, "640x400", (640, 400))

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{log_name}_{datetime.datetime.now():%Y%m%d_%H%M%S}.log"
    log_file = log_path.open("w", encoding="utf-8", buffering=1)  # line-buffered: complete even after a crash
    file_lock = threading.Lock()
    ui_queue: "queue.Queue" = queue.Queue()
    outcome = {"result": None, "done": False}

    # ---- window layout: title, status line, progress bar, log, buttons ----
    # The button bar is packed FIRST at the bottom, so the log (which fills the rest) can never push it out of view.
    frame = ttk.Frame(root, padding=(16, 12))
    frame.pack(fill="both", expand=True)
    bar = ttk.Frame(frame)
    bar.pack(side="bottom", anchor="e", pady=(10, 0))
    ttk.Button(bar, text="Open log file", command=lambda: os.startfile(str(log_path))).pack(side="left", padx=(0, 8))
    done_btn = ttk.Button(bar, text="Continue", state="disabled", command=root.destroy)
    done_btn.pack(side="left")
    ttk.Label(frame, text=title, style="Header.TLabel").pack(anchor="w")
    status_var = tk.StringVar(value="Starting...")
    ttk.Label(frame, textvariable=status_var, wraplength=600, justify="left").pack(anchor="w", pady=(4, 0))
    progress = ttk.Progressbar(frame, mode="indeterminate")
    progress.pack(fill="x", pady=8)
    progress.start(12)
    log_box = ttk.Frame(frame)
    log_box.pack(fill="both", expand=True)
    log_text = tk.Text(log_box, wrap="word", state="disabled", font=("Consolas", 9), height=5)  # grows to fit
    scroll = ttk.Scrollbar(log_box, orient="vertical", command=log_text.yview)
    log_text.configure(yscrollcommand=scroll.set)
    log_text.pack(side="left", fill="both", expand=True)
    scroll.pack(side="left", fill="y")

    def write_file(text: str) -> None:
        with file_lock:
            if not log_file.closed:
                log_file.write(text)

    def show(text: str) -> None:
        log_text.configure(state="normal")
        log_text.insert(tk.END, text)
        log_text.configure(state="disabled")
        log_text.see(tk.END)

    # ---- called from the worker thread. These only write the file and queue PLAIN TEXT messages:
    # the worker must never hold a reference to any Tk object (widget, StringVar, or a lambda using one),
    # or it could end up being the thread that frees it -> Tcl_AsyncDelete crash. ----
    def log(message: str) -> None:
        line = f"[{datetime.datetime.now():%H:%M:%S}] {message}\n"
        write_file(line)
        ui_queue.put(("text", line))
        page = re.search(r"Processing page (\d+)/(\d+)", str(message))
        if page:
            ui_queue.put(("status", f"Working on page {page.group(1)} of {page.group(2)}..."))

    def stream(text: str) -> None:
        write_file(text)
        ui_queue.put(("text", text))

    def pump() -> None:
        """Main thread, every 80 ms: apply the queued messages to the window (the only place widgets change)."""
        try:
            while True:
                kind, value = ui_queue.get_nowait()
                if kind == "text":
                    show(value)
                elif kind == "status":
                    status_var.set(value)
                elif kind == "finish":
                    finish(value)
        except queue.Empty:
            pass
        if root.winfo_exists():
            root.after(80, pump)

    def finish(error) -> None:
        outcome["done"] = True
        progress.stop()
        if error:
            status_var.set(f"Failed: {error}")
            done_btn.configure(text="Close")
        else:
            status_var.set("Done.")
        done_btn.configure(state="normal")

    def work() -> None:
        error = None
        try:
            outcome["result"] = job(log, stream)
        except Exception as exc:  # noqa: BLE001 - shown in the window and the log file
            error = str(exc) or exc.__class__.__name__
            write_file(traceback.format_exc())
            log(f"FAILED: {error}")
        ui_queue.put(("finish", error))

    def on_close() -> None:
        if not outcome["done"] and not messagebox.askyesno("Still working", "This step is still running. Stop and close?"):
            return
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    log(f"Log file: {log_path}")
    threading.Thread(target=work, daemon=True).start()
    pump()
    root.mainloop()

    with file_lock:
        log_file.close()
    # Window is gone: drop every Tk reference this function holds and free them here, on the main thread.
    del root, frame, status_var, progress, log_box, log_text, scroll, bar, done_btn, show, pump, finish, on_close
    gc.collect()
    return outcome["result"] if outcome["done"] else None


def run_gui(Function="Welcome", JobFolder=None):

    saved = load_settings()
    root = make_window("Instrument List Transcriber", saved.get("geometry") or "640x400", (640, 400))
    root.protocol("WM_DELETE_WINDOW", root.destroy)

    # style = ttk.Style()
    # style.configure(".", font=("Segoe UI", 10))
    # style.configure("Header.TLabel", font=("Segoe UI", 16, "bold"))
    # style.configure("Field.TLabel", font=("Segoe UI", 10, "bold"))
    # style.configure("Section.TLabelframe.Label", font=("Segoe UI", 10, "bold"))
    # style.configure("Run.TButton", font=("Segoe UI", 11, "bold"), padding=(18, 8))
    # style.configure("Hint.TLabel", foreground="#9a9a9a")
    # style.configure("Bad.TLabel", foreground="#ff6b6b")
    # style.configure("Good.TLabel", foreground="#5fd38d")

    # ---- page layout helpers: header at the top, labelled path fields, Cancel/Next pinned bottom-right ----
    # Every page is built into a fresh `page` frame; show_page() throws the old one away first, so the
    # window, theme and single mainloop stay the same while the content changes.
    page: ttk.Frame = ttk.Frame(root)
    page_row = {"n": 0}
    result: Dict[str, str] = {}  # filled in when the last page is accepted; returned to the caller
    options: Dict[str, bool] = {"audit_mode": False}  # options page choices; returned to the caller

    def show_page(build: Callable[[], None]) -> None:
        nonlocal page
        page.destroy()
        page = ttk.Frame(root, padding=(28, 22))
        page.pack(fill="both", expand=True)
        page.columnconfigure(0, weight=1)
        page_row["n"] = 0
        build()

    def next_page_row() -> int:
        page_row["n"] += 1
        return page_row["n"]

    def header(title: str, hint: str) -> None:
        ttk.Label(page, text=title, style="Header.TLabel").grid(
            row=next_page_row(), column=0, columnspan=2, sticky="w")
        ttk.Label(page, text=hint, style="Hint.TLabel", wraplength=560, justify="left").grid(
            row=next_page_row(), column=0, columnspan=2, sticky="w", pady=(2, 18))

    def path_field(label: str, var: tk.StringVar, browse: Callable[[], None]) -> ttk.Entry:
        ttk.Label(page, text=label, style="Field.TLabel").grid(
            row=next_page_row(), column=0, columnspan=2, sticky="w")
        row = next_page_row()
        entry = ttk.Entry(page, textvariable=var)
        entry.grid(row=row, column=0, sticky="ew", pady=(4, 14), ipady=3)
        ttk.Button(page, text="Browse...", command=browse).grid(row=row, column=1, padx=(8, 0), pady=(4, 14))
        return entry

    def nav_buttons(on_next: Callable[[], None], on_back: Optional[Callable[[], None]] = None) -> None:
        spacer = next_page_row()
        page.rowconfigure(spacer, weight=1)  # pushes the buttons to the bottom of the window
        bar = ttk.Frame(page)
        bar.grid(row=next_page_row(), column=0, columnspan=2, sticky="e")
        if on_back:
            ttk.Button(bar, text="Back", width=10, command=on_back).pack(side="left", padx=(0, 8))
        ttk.Button(bar, text="Cancel", width=10, command=root.destroy).pack(side="left")
        ttk.Button(bar, text="Next", width=10, command=on_next).pack(side="left", padx=(8, 0))
        root.bind("<Return>", lambda _e: on_next())
        root.bind("<Escape>", lambda _e: root.destroy())

    def browse_pdf(var: tk.StringVar) -> None:
        current = var.get().strip()
        chosen = filedialog.askopenfilename(filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")],
                                            initialdir=str(Path(current).parent) if current else None)
        if chosen:
            var.set(chosen)

    def browse_excel(var: tk.StringVar) -> None:
        current = var.get().strip()
        chosen = filedialog.askopenfilename(filetypes=[("Excel workbooks", "*.xlsx *.xlsm *.xls"),
                                                       ("All files", "*.*")],
                                            initialdir=str(Path(current).parent) if current else None)
        if chosen:
            var.set(chosen)

    def latest_file(folder, name_contains, extensions=(".pdf",)):
        """Newest file under `folder` (searched recursively) whose name contains any of `name_contains`
        and whose extension is in `extensions`, ignoring case. Empty string if none.
        `folder` may be a nested list: you get back a nested list of results in the same shape.
        `name_contains` and `extensions` may be nested lists: they are flattened into one set of options."""
        if isinstance(folder, (list, tuple)):       # one search per folder, same shape back
            return map_nested(lambda one: latest_file(one, name_contains, extensions), folder)
        folder = Path(folder)
        if not folder.is_dir():
            print(f"latest_file: folder not found: {folder}")
            return ""
        squash = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())  # ignore case, spaces and punctuation
        keys = [squash(k) for k in flatten(name_contains)]  # ["TURN", ["DIST"]] -> ["turn", "dist"]
        exts = {e.lower() for e in flatten(extensions)}     # (".xlsx", [".xls"]) -> {".xlsx", ".xls"}
        matches = [f for f in folder.rglob("*")  # rglob = recursive: walks every subfolder
                   if f.is_file() and f.suffix.lower() in exts
                   and any(k in squash(f.stem) for k in keys)  # name contains ANY of the keys
                   and not f.name.startswith("~$")  # ~$ = Office lock files
                   and CHECK_FILE_SUFFIX not in f.stem.lower()]  # our own check workbooks (incl. time-stamped)
        print(f"latest_file: {len(matches)} match(es) for '{name_contains}' {extensions} in {folder}")
        return str(max(matches, key=lambda f: f.stat().st_mtime)) if matches else ""

    def latest_pdf(folder, name_contains):
        """Newest PDF in `folder` or any of its subfolders whose name contains any of `name_contains`,
        ignoring case and punctuation/spaces (so "PI" matches "P&ID", "P-ID", "P ID"). Empty string if none.
        `folder` may be a nested list (same-shape results back); `name_contains` may be a nested list."""
        if isinstance(folder, (list, tuple)):       # one search per folder, same shape back
            return map_nested(lambda one: latest_pdf(one, name_contains), folder)
        folder = Path(folder)
        if not folder.is_dir():
            print(f"latest_pdf: folder not found: {folder}")
            return ""
        squash = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
        keys = [squash(k) for k in flatten(name_contains)]
        matches = [f for f in folder.rglob("*")  # rglob = recursive: walks every subfolder
                   if f.is_file() and f.suffix.lower() == ".pdf"
                   and any(k in squash(f.stem) for k in keys)]
        print(f"latest_pdf: {len(matches)} match(es) for '{name_contains}' in {folder} (incl. subfolders)")
        return str(max(matches, key=lambda f: f.stat().st_mtime)) if matches else ""

    # ---- validation ----------------------------------------------------------------------------------
    def CheckFolder(Folder: tk.StringVar) -> bool:
        """True if the StringVar holds a path to a folder that exists."""
        text = Folder.get().strip()
        return bool(text) and Path(text).is_dir()

    def CheckFile(File: tk.StringVar) -> bool:
        """True if the StringVar holds a path to a file that exists."""
        text = File.get().strip()
        return bool(text) and Path(text).is_file()

    # ---- page 1: pick the job folder -----------------------------------------------------------------
    def welcome_page(start_folder: str = "") -> None:
        print("Welcome")
        header("Welcome", "Select the job folder to start. The latest P&ID and order turn-in "
                          "PDFs will be found in it on the next page.")

        JobFolder = tk.StringVar(value=start_folder)
        entry = path_field("Job folder", JobFolder,
                           lambda: JobFolder.set(filedialog.askdirectory(title="Job folder") or JobFolder.get()))
        entry.focus_set()

        def next_() -> None:
            if not CheckFolder(JobFolder):
                messagebox.showerror("Error", f"Folder is not valid:\n{JobFolder.get().strip() or '(empty)'}")
                return  # stay on this page
            folder = JobFolder.get().strip()
            show_page(lambda: parse_page(folder))

        nav_buttons(next_)

    # ---- page 2: confirm the latest P&ID and turn-in PDFs ------------------------------------------
    def parse_page(job_folder: str) -> None:
        print("Entering parse", job_folder)
        header("Job Folder Files", "These are the latest P&ID PDF and order turn-in Excel workbook found in the "
                                   "job folder. Check them, or browse to pick different ones.")

        PandIDs = tk.StringVar(value=latest_pdf(Path(job_folder) / "MECHANICAL DRAWINGS", "PI"))
        TurnIn = tk.StringVar(value=latest_file(Path(job_folder) / "ORDER TURN-IN", ["TURN", "DISTRIBUTION"], EXCEL_EXTENSIONS))
        print("P&ID:", PandIDs.get() or "(none found)")
        print("Turn-in:", TurnIn.get() or "(none found)")

        path_field("P&ID latest file", PandIDs, lambda: browse_pdf(PandIDs))
        path_field("Turn-in latest file", TurnIn, lambda: browse_excel(TurnIn))

        def next_() -> None:
            bad = [label for label, var in (("P&ID file", PandIDs), ("Turn-in file", TurnIn)) if not CheckFile(var)]
            if bad:
                messagebox.showerror("Error", "Not a valid file:\n" + "\n".join(f"• {b}" for b in bad))
                return  # stay on this page
            turnin = Path(TurnIn.get().strip())
            if turnin.suffix.lower() not in EXCEL_EXTENSIONS:
                messagebox.showerror("Error", "The turn-in must be an Excel workbook "
                                              f"({', '.join(EXCEL_EXTENSIONS)}):\n{turnin}")
                return  # stay on this page
            pids_pdf = PandIDs.get().strip()
            show_page(lambda: options_page(job_folder, pids_pdf, str(turnin)))

        nav_buttons(next_, on_back=lambda: show_page(lambda: welcome_page(job_folder)))

    # ---- page 3: run options -----------------------------------------------------------------------
    def options_page(job_folder: str, pids_pdf: str, turnin_file: str) -> None:
        print("Options")
        header("Options", "Choose how the run goes, then click Next to start.")
        AuditMode = tk.BooleanVar(value=False)
        ttk.Checkbutton(page, text="Audit Mode", variable=AuditMode).grid(
            row=next_page_row(), column=0, columnspan=2, sticky="w")
        ttk.Label(page, text="Show each OCR step and symbol in the audit window as the P&ID is read, then the "
                             "connection viewer to check the links. Off: a plain progress window only.",
                  style="Hint.TLabel", wraplength=560, justify="left").grid(
            row=next_page_row(), column=0, columnspan=2, sticky="w", pady=(2, 14))

        def next_() -> None:
            result.update(job_folder=job_folder, pids_pdf=pids_pdf, turnin_file=turnin_file)
            options.update(audit_mode=bool(AuditMode.get()))
            root.destroy()  # last page accepted: close the window and hand the choices back

        nav_buttons(next_, on_back=lambda: show_page(lambda: parse_page(job_folder)))

    # ---- run -----------------------------------------------------------------------------------------
    if Function == "Parse Job Folder" and JobFolder:
        show_page(lambda: parse_page(str(JobFolder)))
    else:
        show_page(welcome_page)
    root.mainloop()
    if not result:                 # window closed or Cancel pressed before the last page was accepted
        return None, None, None
    # P&ID PDF, turn-in Excel workbook, options {"audit_mode": bool}
    return Path(result["pids_pdf"]), Path(result["turnin_file"]), dict(options)

