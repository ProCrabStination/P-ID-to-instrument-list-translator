"""Live audit window for the P&ID step: every OCR decision drawn as it happens, so pipeline errors can be seen.

run_with_audit(title, job, log_name) works like GUI.run_with_progress (background thread, live log, log file in
Working/logs, returns the job's result), but `job(log, stream, audit)` also gets `audit(event)`, and the window shows:

  Page rows      the page image with every page OCR text box (green, with what was read) and every symbol-sized
                 closed shape found (coloured by role once read: purple PLC / panel, blue instrument, grey other).
                 Click a symbol to open its row. Wheel = zoom, right / middle drag = pan.
  Symbol rows    "On the page": the page around the symbol with the outline the program found (red).
                 "Given to OCR": the image shape OCR actually read (only the paths inside, outline removed) with
                 every box OCR found and its reading. Then the text used, where it came from, the role / tag it gives,
                 and (when the run is done) the PLC / panels it is linked to.

Pause stops the run before the next symbol, Step lets one more through, "Follow live" shows each symbol as it is
read. The filter finds the ones worth checking: instruments without a PLC / panel, symbols whose text is not a tag,
shape OCR and page OCR disagreeing, symbols with no text.
Nothing here changes a result; the window only shows what the pipeline decided.
"""
import datetime
import gc
import os
import queue
import re
import threading
import traceback
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any

from PIL import Image, ImageTk

from GUI import LOG_DIR, make_window

ROLE_COLORS = {"plc": "#9c27b0", "panel": "#9c27b0", "instrument": "#1f5fd6", "other": "#8a8a8a", "": "#8a8a8a"}
ROW_COLORS = {"plc": "#d9a6ff", "panel": "#d9a6ff", "instrument": "#8fb8ff", "other": "#bdbdbd", "": "#bdbdbd"}
SUSPECT = "#ffb347"
OCR_BOX = "#0a9d3c"
OUTLINE = "#e53935"
FILTERS = ("All", "Instruments", "PLC / panels", "Instruments without PLC / panel", "Not a tag (other) but has text",
           "No text", "Shape OCR and page OCR differ")
ZOOM_STEP = 1.25


def _norm(text) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(text or "").upper())


def _fit_photo(array, width: int, height: int, max_up: float = 8.0):
    """numpy greyscale image -> (PhotoImage scaled to fit width x height, scale used). None for an empty image."""
    if array is None or getattr(array, "size", 0) == 0:
        return None, 1.0
    h, w = array.shape[:2]
    scale = min(width / w, height / h, max_up)
    size = (max(1, int(w * scale)), max(1, int(h * scale)))
    method = Image.Resampling.BOX if scale < 1 else Image.Resampling.BICUBIC
    return ImageTk.PhotoImage(Image.fromarray(array).resize(size, method)), scale


class _AuditWindow:
    """Everything on the main (Tk) thread. The worker only puts plain data in `ui_queue`."""

    def __init__(self, root: tk.Tk, title: str, log_path, ui_queue, running, steps):
        self.root, self.ui_queue, self.running, self.steps = root, ui_queue, running, steps
        self.log_path = log_path
        self.entries = {}          # tree iid -> event (page or shape)
        self.order = []            # iids in arrival order
        self.links = {}            # shape object id -> component info from the "connections" event
        self.roles = {}            # shape object id -> role (from its read, then from its component)
        self.photos = []           # PhotoImages on the canvas (kept alive)
        self.current = None        # iid shown
        self.view = None           # page view: {"page", "scale", "ox", "oy"}
        self._drag = None
        self.done = False
        self.result = None

        frame = ttk.Frame(root, padding=(12, 10))
        frame.pack(fill="both", expand=True)
        bar = ttk.Frame(frame)
        bar.pack(side="bottom", fill="x", pady=(8, 0))
        self.pause_btn = ttk.Button(bar, text="Pause", width=10, command=self.toggle_pause)
        self.pause_btn.pack(side="left")
        self.step_btn = ttk.Button(bar, text="Step", width=8, command=self.step, state="disabled")
        self.step_btn.pack(side="left", padx=(6, 0))
        self.follow = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="Follow live", variable=self.follow).pack(side="left", padx=(12, 0))
        self.done_btn = ttk.Button(bar, text="Continue", width=12, state="disabled", command=root.destroy)
        self.done_btn.pack(side="right")
        ttk.Button(bar, text="Open log file", command=lambda: os.startfile(str(log_path))).pack(side="right", padx=(0, 8))

        ttk.Label(frame, text=title, style="Header.TLabel").pack(anchor="w")
        self.status = tk.StringVar(value="Starting...")
        ttk.Label(frame, textvariable=self.status).pack(anchor="w", pady=(2, 0))
        self.progress = ttk.Progressbar(frame, mode="indeterminate")
        self.progress.pack(fill="x", pady=6)
        self.progress.start(12)

        paned = ttk.Panedwindow(frame, orient="horizontal")
        paned.pack(fill="both", expand=True)
        left = ttk.Frame(paned)
        right = ttk.Frame(paned)
        paned.add(left, weight=0)
        paned.add(right, weight=1)

        # ---- left: filter, list of pages / symbols, log
        top = ttk.Frame(left)
        top.pack(fill="x")
        self.filter = tk.StringVar(value=FILTERS[0])
        box = ttk.Combobox(top, textvariable=self.filter, values=FILTERS, state="readonly", width=34)
        box.pack(side="left")
        box.bind("<<ComboboxSelected>>", lambda e: self.refill())
        self.search = tk.StringVar()
        ttk.Entry(top, textvariable=self.search, width=14).pack(side="left", padx=(6, 0), fill="x", expand=True)
        self.search.trace_add("write", lambda *_: self.refill())
        listing = ttk.Frame(left)
        listing.pack(fill="both", expand=True, pady=(6, 0))
        self.tree = ttk.Treeview(listing, columns=("text", "role", "source", "plc"), selectmode="browse", height=18)
        for column, heading, width in (("#0", "Id", 110), ("text", "Text used", 120), ("role", "Role", 72),
                                       ("source", "From", 70), ("plc", "PLC / panel", 90)):
            self.tree.heading(column, text=heading)
            self.tree.column(column, width=width, stretch=column == "text")
        scroll = ttk.Scrollbar(listing, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="left", fill="y")
        for role, color in ROW_COLORS.items():
            self.tree.tag_configure(f"role-{role}", foreground=color)
        self.tree.tag_configure("suspect", foreground=SUSPECT)
        self.tree.bind("<<TreeviewSelect>>", self.on_select)
        self.log_text = tk.Text(left, height=9, wrap="word", state="disabled", font=("Consolas", 8), bg="#1e1e1e",
                                fg="#d0d0d0", relief="flat")
        self.log_text.pack(fill="x", pady=(6, 0))

        # ---- right: details text, overlay toggles, canvas
        self.details = tk.Text(right, height=9, wrap="word", font=("Consolas", 9), bg="#1e1e1e", fg="#e6e6e6",
                               relief="flat")
        self.details.pack(fill="x", padx=(8, 0))
        toggles = ttk.Frame(right)
        toggles.pack(fill="x", padx=(8, 0), pady=4)
        self.show_text = tk.BooleanVar(value=True)
        self.show_symbols = tk.BooleanVar(value=True)
        self.show_letters = tk.BooleanVar(value=False)
        for label, var in (("Page OCR boxes", self.show_text), ("Symbols", self.show_symbols),
                           ("Lettering strokes", self.show_letters)):
            ttk.Checkbutton(toggles, text=label, variable=var, command=self.redraw).pack(side="left", padx=(0, 10))
        self.mouse_xy = tk.StringVar(value="")       # pointer position in page points (x right, y down)
        ttk.Label(toggles, textvariable=self.mouse_xy, font=("Consolas", 10), width=30).pack(side="left", padx=(6, 0))
        self.crop_map = None       # shape view: canvas -> page points for the "On the page" crop
        self.on_page_btn = ttk.Button(toggles, text="Show on page", command=self.show_on_page, state="disabled")
        self.on_page_btn.pack(side="right")
        self.canvas = tk.Canvas(right, bg="#ffffff", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, padx=(8, 0))
        self.canvas.bind("<Configure>", lambda e: self.redraw())
        self.canvas.bind("<MouseWheel>", self.on_wheel)
        self.canvas.bind("<Button-1>", self.on_click)
        self.canvas.bind("<Motion>", self.on_mouse)
        self.canvas.bind("<Leave>", lambda e: self.mouse_xy.set(""))
        for button in ("2", "3"):
            self.canvas.bind(f"<ButtonPress-{button}>", self.on_press)
            self.canvas.bind(f"<B{button}-Motion>", self.on_drag)
            self.canvas.bind(f"<ButtonRelease-{button}>", lambda e: self.redraw())

    # ------------------------------------------------------------------------------------------- controls
    def toggle_pause(self) -> None:
        if self.running.is_set():
            self.running.clear()
            self.pause_btn.configure(text="Resume")
            self.step_btn.configure(state="normal")
            self.status.set(self.status.get() + "   (paused)")
        else:
            self.running.set()
            self.pause_btn.configure(text="Pause")
            self.step_btn.configure(state="disabled")

    def step(self) -> None:
        self.steps.release()

    def log(self, text: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text)
        self.log_text.configure(state="disabled")
        self.log_text.see("end")

    def finish(self, error) -> None:
        self.done = True
        self.running.set()
        self.progress.stop()
        self.progress.configure(mode="determinate", value=100)
        self.pause_btn.configure(state="disabled")
        self.step_btn.configure(state="disabled")
        self.status.set(f"Failed: {error}" if error else "Done. Filter the list to check the results; Continue goes on.")
        self.done_btn.configure(state="normal", text="Close" if error else "Continue")

    # ------------------------------------------------------------------------------------------- events
    def add(self, event: dict) -> None:
        kind = event["type"]
        if kind == "page":
            iid = f"page:{event['page']}"
            event["label"] = (f"{len(event['texts'])} OCR text(s), {len(event['symbols'])} symbol(s)")
            self.entries[iid] = event
            self.order.append(iid)
            self.status.set(f"Page {event['page']}: reading {len(event['symbols'])} symbol(s)...")
            self.progress.stop()
            self.progress.configure(mode="determinate", maximum=max(len(event["symbols"]), 1), value=0)
            self._insert(iid)
            if self.follow.get():
                self.select(iid)
        elif kind == "shape":
            iid = event["id"]
            self.entries[iid] = event
            self.order.append(iid)
            self.roles[iid] = event["role"]
            self.progress.configure(value=event["count"])
            self.status.set(f"Page {event['page']}: symbol {event['count']} of {event['total']}"
                            + ("   (paused)" if not self.running.is_set() else ""))
            self._insert(iid)
            if self.follow.get():
                self.select(iid)
        elif kind == "connections":
            for comp in event["components"]:
                for part in comp["parts"]:
                    self.links[part] = comp
                    self.roles[part] = comp["role"]
            self.refill()
            if self.current:
                self.show(self.current)

    def _suspect(self, iid: str) -> bool:
        e = self.entries[iid]
        if e["type"] != "shape":
            return False
        link = self.links.get(iid)
        role = (link or e)["role"]
        if role == "instrument" and link is not None and not link["controllers"]:
            return True
        return (role == "other" and bool(e["text"])) or not e["text"] or self._differ(e)

    @staticmethod
    def _differ(e: dict) -> bool:
        page = _norm(" ".join(t for t, _ in e["page_texts"]))
        return bool(e["shape_text"]) and bool(page) and _norm(e["shape_text"]) != page

    def _matches(self, iid: str) -> bool:
        e = self.entries[iid]
        if e["type"] == "page":
            return True
        word = self.search.get().strip().upper()
        if word and word not in (e["text"] or "").upper() and word not in iid.upper():
            return False
        link = self.links.get(iid)
        role = (link or e)["role"]
        f = self.filter.get()
        return {"All": True,
                "Instruments": role == "instrument",
                "PLC / panels": role in ("plc", "panel"),
                "Instruments without PLC / panel": role == "instrument" and (link is None or not link["controllers"]),
                "Not a tag (other) but has text": role == "other" and bool(e["text"]),
                "No text": not e["text"],
                "Shape OCR and page OCR differ": self._differ(e)}.get(f, True)

    def _row(self, iid: str) -> dict:
        e = self.entries[iid]
        if e["type"] == "page":
            return {"text": f"Page {e['page']}", "values": (e["label"], "", "", "")}
        link = self.links.get(iid)
        role = (link or e)["role"]
        plc = ""
        if link is not None:
            plc = (", ".join(link["controllers"]) or "none") if role == "instrument" else \
                (f"{len(link['instruments'])} instr." if role in ("plc", "panel") else "")
        tags = ("suspect",) if self._suspect(iid) else (f"role-{role}",)
        return {"text": iid, "values": (e["text"], role, e["source"], plc), "tags": tags}

    def _insert(self, iid: str) -> None:
        e = self.entries[iid]
        if not self._matches(iid):
            return
        parent = "" if e["type"] == "page" else f"page:{e['page']}"
        if parent and not self.tree.exists(parent):
            parent = ""
        self.tree.insert(parent, "end", iid=iid, open=True, **self._row(iid))

    def refill(self) -> None:
        keep = self.current
        self.tree.delete(*self.tree.get_children())
        for iid in self.order:
            self._insert(iid)
        if keep and self.tree.exists(keep):
            self.tree.selection_set(keep)
            self.tree.see(keep)

    def select(self, iid: str) -> None:
        if self.tree.exists(iid):
            self.tree.selection_set(iid)
            self.tree.see(iid)
        else:
            self.show(iid)

    def on_select(self, _event=None) -> None:
        chosen = self.tree.selection()
        if chosen and chosen[0] != self.current:
            self.show(chosen[0])

    # ------------------------------------------------------------------------------------------- showing
    def show(self, iid: str, focus=None) -> None:
        self.current = iid
        e = self.entries[iid]
        if e["type"] == "page":
            self.view = None
            self._page_view(e["page"], focus)
        else:
            self.view = None
            self.on_page_btn.configure(state="normal")
            self._details(self._shape_text(iid))
            self.redraw()

    def redraw(self) -> None:
        if not self.current or self.current not in self.entries:
            return
        e = self.entries[self.current]
        if e["type"] == "page" or self.view is not None:
            self._render_page()
        else:
            self._render_shape(e)

    def _details(self, text: str) -> None:
        self.details.delete("1.0", "end")
        self.details.insert("1.0", text)

    def _shape_text(self, iid: str) -> str:
        e = self.entries[iid]
        lines = [f"{iid}   {e['kind']}   page {e['page']}   box {[round(v, 1) for v in e['rect']]}"
                 + (f"   (outline made of {len(e['made_of'])} separate line(s))" if e.get("made_of") else "")]
        reads = ", ".join(f'"{t}" {s:.2f}' for t, s, _ in e["ocr_results"]) or "nothing"
        lines.append(f"Shape OCR read:   {reads}" + ("" if e["ocr_image"] is not None and e["ocr_image"].size
                                                     else "   (nothing drawn inside the outline)"))
        if e.get("retry_results") is not None:
            lines.append(f"Read again at {e.get('retry_dpi')} dpi (low confidence):  "
                         + (", ".join(f'"{t}" {s:.2f}' for t, s, _ in e["retry_results"]) or "nothing"))
        lines.append("Page OCR inside:  " + (", ".join(f'"{t}" {s:.2f}' for t, s in e["page_texts"]) or "nothing"))
        source = {"shape OCR": "shape OCR", "inside": "page OCR inside it", "": "nothing"}.get(e["source"], e["source"])
        lines.append(f'Text used:        "{e["text"]}"  from {source}  ->  role {e["role"]}'
                     + (f', tag "{e["tag"]}"' if e["tag"] else ""))
        if e["role"] == "other" and e["text"]:
            lines.append("  Not a tag: a tag is 1-5 letters then a number (Pdf_Connections.TAG_RE); PLC / panel words "
                         "are Pdf_Connections.PANEL_WORDS.")
        link = self.links.get(iid)
        if link is not None:
            lines.append(f"Symbol {link['id']} (role {link['role']}"
                         + (f', text "{link["text"]}"' if link["text"] != e["text"] else "") + ")")
            if link["role"] == "instrument":
                lines.append("  PLC / panels: " + (", ".join(link["controllers"]) or "none found on its lines"))
            elif link["role"] in ("plc", "panel"):
                lines.append("  instruments: " + (", ".join(link["instruments"]) or "none found on its lines"))
            if link.get("labelled_by"):
                lines.append(f"  named by the tag beside it: {link['labelled_by']}")
        elif self.done:
            lines.append("Not a symbol in the connection table (merged into a bigger symbol, or too big / small).")
        else:
            lines.append("PLC / panel links: shown when the run is done.")
        return "\n".join(lines)

    def _render_shape(self, e: dict) -> None:
        c = self.canvas
        c.delete("all")
        self.photos = []
        self.crop_map = None
        width, height = max(c.winfo_width(), 100), max(c.winfo_height(), 100)
        half = width // 2
        c.create_text(10, 8, anchor="nw", text="On the page (outline found in red)", fill="#333333",
                      font=("Segoe UI", 10, "bold"))
        c.create_text(half + 10, 8, anchor="nw", text="Given to OCR (paths inside, outline removed) + what OCR found",
                      fill="#333333", font=("Segoe UI", 10, "bold"))
        c.create_line(half, 0, half, height, fill="#cccccc")
        crop = e.get("page_image")
        if crop:
            photo, scale = _fit_photo(crop["image"], half - 20, height - 40, 10)
            if photo:
                self.photos.append(photo)
                x0, y0 = 10, 32
                c.create_image(x0, y0, anchor="nw", image=photo)
                zoom = crop["dpi"] / 72.0

                def to_canvas(p):
                    return (x0 + (p[0] * zoom - crop["origin"][0]) * scale, y0 + (p[1] * zoom - crop["origin"][1]) * scale)
                self._outline(e, to_canvas, OUTLINE, 2)
                h, w = crop["image"].shape[:2]
                self.crop_map = {"box": (x0, y0, x0 + w * scale, y0 + h * scale), "x0": x0, "y0": y0,
                                 "scale": scale, "zoom": zoom, "origin": crop["origin"], "page": e["page"]}
        image = e.get("ocr_image")
        if image is None or image.size == 0:
            c.create_text(half + 10, 40, anchor="nw", fill="#b00020", font=("Segoe UI", 11),
                          text="Nothing is drawn inside this outline,\nso shape OCR had nothing to read.")
            return
        photo, scale = _fit_photo(image, width - half - 20, height - 40, 6)
        self.photos.append(photo)
        x0, y0 = half + 10, 32
        c.create_rectangle(x0 - 1, y0 - 1, x0 + image.shape[1] * scale + 1, y0 + image.shape[0] * scale + 1,
                           outline="#dddddd")
        c.create_image(x0, y0, anchor="nw", image=photo)
        for text, score, box in e["ocr_results"]:
            points = [v for p in box for v in (x0 + p[0] * scale, y0 + p[1] * scale)]
            c.create_polygon(*points, outline=OCR_BOX, fill="", width=2)
            c.create_text(points[0], points[1] - 2, anchor="sw", text=f"{text}  {score:.2f}", fill=OCR_BOX,
                          font=("Segoe UI", 11, "bold"))
        if not e["ocr_results"]:
            c.create_text(x0, y0 + image.shape[0] * scale + 8, anchor="nw", fill="#b00020", font=("Segoe UI", 11),
                          text="OCR found no text in this image.")

    def _outline(self, e: dict, to_canvas, color: str, width: float) -> None:
        c = self.canvas
        if e.get("vertices"):
            points = [v for p in e["vertices"] for v in to_canvas(p)]
            c.create_polygon(*points, outline=color, fill="", width=width)
        elif e["kind"] in ("circle", "oval"):
            a, b = to_canvas(e["rect"][:2]), to_canvas(e["rect"][2:])
            c.create_oval(a[0], a[1], b[0], b[1], outline=color, width=width)
        else:
            a, b = to_canvas(e["rect"][:2]), to_canvas(e["rect"][2:])
            c.create_rectangle(a[0], a[1], b[0], b[1], outline=color, width=width)

    # ---- page view
    def show_on_page(self) -> None:
        if self.current and self.entries[self.current]["type"] == "shape":
            e = self.entries[self.current]
            self._page_view(e["page"], e["rect"])

    def _page_view(self, number: int, focus=None) -> None:
        page = self.entries.get(f"page:{number}")
        if page is None:
            return
        c = self.canvas
        width, height = max(c.winfo_width(), 100), max(c.winfo_height(), 100)
        img_h, img_w = page["image"].shape[:2]
        zoom = page["dpi"] / 72.0
        if focus:
            span = max(focus[2] - focus[0], focus[3] - focus[1], 20) + 80
            scale = min(width, height) / (span * zoom)
            cx, cy = (focus[0] + focus[2]) / 2 * zoom, (focus[1] + focus[3]) / 2 * zoom
            self.view = {"page": number, "scale": scale, "ox": width / 2 - cx * scale, "oy": height / 2 - cy * scale,
                         "focus": self.current if self.entries.get(self.current, {}).get("type") == "shape" else None}
        else:
            scale = min(width / img_w, height / img_h)
            self.view = {"page": number, "scale": scale, "ox": (width - img_w * scale) / 2, "oy": 0, "focus": None}
        if not focus:
            self.on_page_btn.configure(state="disabled")
            page_rows = [i for i in self.order if self.entries[i]["type"] == "shape" and self.entries[i]["page"] == number]
            self._details(f"Page {number}: {len(page['texts'])} page OCR text(s), {len(page['symbols'])} symbol-sized "
                          f"closed shape(s), {len(page_rows)} read so far.\n"
                          "Green boxes: page OCR texts. Coloured outlines: symbols (purple PLC / panel, blue "
                          "instrument, grey other / not read yet). A tag with no outline around it was not found as a "
                          "closed shape. Click a symbol to open it. Wheel: zoom; right / middle drag: pan.")
        self._render_page()

    def _render_page(self) -> None:
        v = self.view
        if v is None:
            if self.current and self.entries[self.current]["type"] == "page":
                self._page_view(self.entries[self.current]["page"])
            return
        page = self.entries[f"page:{v['page']}"]
        c = self.canvas
        c.delete("all")
        self.photos = []
        width, height = max(c.winfo_width(), 100), max(c.winfo_height(), 100)
        img = page["image"]
        img_h, img_w = img.shape[:2]
        s, ox, oy = v["scale"], v["ox"], v["oy"]
        ix0, iy0 = max(0, int(-ox / s)), max(0, int(-oy / s))
        ix1, iy1 = min(img_w, int((width - ox) / s) + 1), min(img_h, int((height - oy) / s) + 1)
        if ix1 > ix0 and iy1 > iy0:
            size = (max(1, round((ix1 - ix0) * s)), max(1, round((iy1 - iy0) * s)))
            method = Image.Resampling.BOX if s < 1 else Image.Resampling.BILINEAR
            photo = ImageTk.PhotoImage(Image.fromarray(img[iy0:iy1, ix0:ix1]).resize(size, method))
            self.photos.append(photo)
            c.create_image(ox + ix0 * s, oy + iy0 * s, anchor="nw", image=photo)
        zoom = page["dpi"] / 72.0

        def to_canvas(p):
            return ox + p[0] * zoom * s, oy + p[1] * zoom * s
        if self.show_letters.get():
            for r in page["lettering"]:
                a, b = to_canvas(r[:2]), to_canvas(r[2:])
                c.create_rectangle(a[0], a[1], b[0], b[1], outline="#f4a261")
        if self.show_text.get():
            readable = zoom * s > 1.2
            for t in page["texts"]:
                a, b = to_canvas(t["rect"][:2]), to_canvas(t["rect"][2:])
                c.create_rectangle(a[0], a[1], b[0], b[1], outline=OCR_BOX)
                if readable:
                    c.create_text(a[0], a[1] - 1, anchor="sw", text=t["text"], fill=OCR_BOX,
                                  font=("Segoe UI", max(7, min(12, int(3 * zoom * s))), "bold"))
        if self.show_symbols.get():
            for sym in page["symbols"]:
                role = self.roles.get(sym["id"], "")
                self._outline(sym, to_canvas, ROLE_COLORS.get(role, "#8a8a8a"), 2)
        if v.get("focus") and v["focus"] in self.entries:
            self._outline(self.entries[v["focus"]], to_canvas, OUTLINE, 3)

    def on_wheel(self, event) -> None:
        if self.view is None:
            return
        f = ZOOM_STEP if event.delta > 0 else 1 / ZOOM_STEP
        v = self.view
        v["scale"] *= f
        v["ox"], v["oy"] = event.x - (event.x - v["ox"]) * f, event.y - (event.y - v["oy"]) * f
        self._render_page()

    def on_press(self, event) -> None:
        self._drag = (event.x, event.y)

    def on_drag(self, event) -> None:
        if self.view is None or not self._drag:
            return
        dx, dy = event.x - self._drag[0], event.y - self._drag[1]
        self.canvas.move("all", dx, dy)
        self.view["ox"] += dx
        self.view["oy"] += dy
        self._drag = (event.x, event.y)

    def on_mouse(self, event) -> None:
        """Pointer position in page points (same units as the boxes in the details and the JSON files):
        anywhere on the page view, or over the "On the page" crop of a shape view; blank elsewhere."""
        v = self.view
        if v is not None:
            page = self.entries.get(f"page:{v['page']}")
            if page is None:
                return
            zoom = page["dpi"] / 72.0
            x, y = (event.x - v["ox"]) / (v["scale"] * zoom), (event.y - v["oy"]) / (v["scale"] * zoom)
            number = v["page"]
        elif self.crop_map is not None:
            m = self.crop_map
            bx0, by0, bx1, by1 = m["box"]
            if not (bx0 <= event.x <= bx1 and by0 <= event.y <= by1):
                self.mouse_xy.set("")
                return
            x = ((event.x - m["x0"]) / m["scale"] + m["origin"][0]) / m["zoom"]
            y = ((event.y - m["y0"]) / m["scale"] + m["origin"][1]) / m["zoom"]
            number = m["page"]
        else:
            self.mouse_xy.set("")
            return
        self.mouse_xy.set(f"page {number}  x {x:8.1f}  y {y:8.1f} pt")

    def on_click(self, event) -> None:
        v = self.view
        if v is None:
            return
        page = self.entries[f"page:{v['page']}"]
        zoom = page["dpi"] / 72.0
        p = ((event.x - v["ox"]) / (v["scale"] * zoom), (event.y - v["oy"]) / (v["scale"] * zoom))
        hits = [s for s in page["symbols"] if s["rect"][0] <= p[0] <= s["rect"][2] and s["rect"][1] <= p[1] <= s["rect"][3]]
        if not hits:
            return
        sid = min(hits, key=lambda s: (s["rect"][2] - s["rect"][0]) * (s["rect"][3] - s["rect"][1]))["id"]
        if sid in self.entries:
            if not self.tree.exists(sid):          # filtered out: show everything so it can be selected
                self.filter.set(FILTERS[0])
                self.search.set("")
                self.refill()
            self.select(sid)
            if self.current != sid:
                self.show(sid)

    # ------------------------------------------------------------------------------------------- queue
    def pump(self) -> None:
        """Main thread, every 60 ms: apply what the worker queued (the only place the window changes)."""
        try:
            for _ in range(400):
                kind, value = self.ui_queue.get_nowait()
                if kind == "text":
                    self.log(value)
                elif kind == "audit":
                    self.add(value)
                elif kind == "finish":
                    self.finish(value)
        except queue.Empty:
            pass
        if self.root.winfo_exists():
            self.root.after(60, self.pump)


def run_with_audit(title: str, job, log_name: str = "run") -> Any:
    """Like GUI.run_with_progress, with the live audit view (see module docstring). Runs `job(log, stream, audit)`
    on a background thread; returns its result, or None if it failed or the window was closed early.
    `title` may be a nested list of titles with `job` the matching nested list of jobs (one window each, same-shape
    results back)."""
    if isinstance(title, (list, tuple)):
        return [run_with_audit(t, j, log_name) for t, j in zip(title, job)]
    gc.collect()   # free Tk objects of earlier windows here, on the main thread (see GUI.run_with_progress)
    root = make_window(title, "1500x920", (1100, 700))
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{log_name}_{datetime.datetime.now():%Y%m%d_%H%M%S}.log"
    log_file = log_path.open("w", encoding="utf-8", buffering=1)
    file_lock = threading.Lock()
    ui_queue = queue.Queue()
    running, steps, closed = threading.Event(), threading.Semaphore(0), threading.Event()
    running.set()
    outcome = {"result": None, "done": False}
    window = _AuditWindow(root, title, log_path, ui_queue, running, steps)

    # ---- worker-thread side: plain data and thread primitives only, never a Tk object
    def write_file(text: str) -> None:
        with file_lock:
            if not log_file.closed:
                log_file.write(text)

    def log(message: str) -> None:
        line = f"[{datetime.datetime.now():%H:%M:%S}] {message}\n"
        write_file(line)
        ui_queue.put(("text", line))

    def stream(text: str) -> None:
        write_file(text)
        ui_queue.put(("text", text))

    def audit(event: dict) -> None:
        ui_queue.put(("audit", event))
        if event.get("type") == "shape":          # Pause holds the run here, before the next symbol
            while not running.is_set() and not closed.is_set():
                if steps.acquire(timeout=0.1):
                    break

    def work() -> None:
        error = None
        try:
            outcome["result"] = job(log, stream, audit)
            outcome["done"] = True
        except Exception as exc:  # noqa: BLE001 - shown in the window and the log file
            error = str(exc) or exc.__class__.__name__
            write_file(traceback.format_exc())
            log(f"FAILED: {error}")
            outcome["done"] = True
        ui_queue.put(("finish", error))

    def on_close() -> None:
        if not window.done and not messagebox.askyesno("Still working", "This step is still running. Stop and close?"):
            return
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    log(f"Log file: {log_path}")
    threading.Thread(target=work, daemon=True).start()
    window.pump()
    root.mainloop()
    closed.set()       # a paused worker is let go
    running.set()
    finished = window.done
    with file_lock:
        log_file.close()
    del window, root   # drop the Tk references here, on the main thread
    gc.collect()
    return outcome["result"] if finished else None
