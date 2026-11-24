"""P&ID instrument connection viewer: every recognised symbol and line redrawn in a window, from the saved files.

Drawn from "<pdf> - shapes.json" (each object's real geometry) and "<pdf> - instrument connections.json"
(Pdf_Connections), so what you see is exactly what the program recognised:
  purple     PLC / panels         blue    instruments (tagged symbols)      light blue   other symbols
  black      lines joining 2+ symbols         light grey   lines that join nothing
  green      the text OCR read (lettering strokes are hidden; tick "Lettering strokes" to show them)
Click a symbol: it turns red, the symbols its lines reach orange, the joining lines magenta; the panel shows its
PLC / panel links. Click a line: its net and the symbols on it. "Find" (tag, text, id, role; e.g. PLC) lists
matching symbols; "Highlight all" marks every match. Wheel = zoom, right / middle drag = pan.
Both files are only read.

Run it on its own:  python Connection_Viewer.py ["Working\\<pdf> - shapes.json" "Working\\<pdf> - instrument connections.json"]
(no paths = file dialogs).
"""
import gc
import json
import sys
import tkinter as tk
from pathlib import Path
from tkinter import ttk

import Pdf_Connections
from GUI import make_window

BACKGROUND = "#ffffff"
ROLE_COLORS = {"plc": "#7b1fa2", "panel": "#7b1fa2", "instrument": "#1f4e9e", "other": "#7d9cc4"}
WIRE = "#222222"
LOOSE = "#b4b4b4"
STROKE = "#9e9e9e"
OCR_TEXT = "#0a7d2c"
SELECTED = "#d62728"
LINKED = "#ff7f0e"
NET = "#c2185b"
ELLIPSE_KINDS = {"circle", "oval", "small circle", "dot"}
BOX_KINDS = {"rectangle", "rectangles", "quad", "quads", "mixed", "filled area"}
ZOOM_STEP = 1.2


def _load(path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))   # only read


def _chains(segments) -> list:
    """Segments [[a, b], ...] -> runs of joined points [[a, b, c, ...], ...] (one canvas line per run)."""
    runs = []
    for a, b in segments:
        if runs and abs(runs[-1][-1][0] - a[0]) < 0.01 and abs(runs[-1][-1][1] - a[1]) < 0.01:
            runs[-1].append(b)
        else:
            runs.append([a, b])
    return runs


class _Viewer:
    def __init__(self, root: tk.Tk, shapes: dict, connections: dict, search: str):
        self.root = root
        self.connections = connections
        self.links = connections.get("links") or {"controllers": [], "instruments": []}
        self.pages = {p["page"]: p for p in shapes.get("pages", [])}
        self.conn_pages = {p["page"]: p for p in connections.get("pages", [])}
        self.zoom, self.ox, self.oy = 1.0, 0.0, 0.0
        self.original = {}       # canvas item -> its normal colour / width options
        self.text_size = {}      # OCR text item -> letter height in pt
        self.line_size = {}      # drawn item -> line width in pt (screen width follows the zoom)
        self.highlighted = []
        self.selected = []
        self.components = {}
        self.nets = {}
        self.page_number = None
        self._drag = None
        self._fitted = False

        body = ttk.Frame(root)
        body.pack(fill="both", expand=True)
        bar = ttk.Frame(root, padding=(16, 8, 16, 14))
        bar.pack(side="bottom", fill="x", before=body)   # pinned to the bottom so it stays visible at any size
        ttk.Label(bar, text="Wheel: zoom    Right / middle drag: pan    Click: select a shape or line").pack(side="left")
        ttk.Button(bar, text="Continue", width=12, command=root.destroy).pack(side="right")
        ttk.Button(bar, text="Fit to window", command=self.fit).pack(side="right", padx=(0, 8))

        paned = ttk.Panedwindow(body, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=8, pady=8)
        holder = ttk.Frame(paned)
        self.canvas = tk.Canvas(holder, bg=BACKGROUND, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        side = ttk.Frame(paned, padding=(10, 0, 0, 0))
        paned.add(holder, weight=1)
        paned.add(side, weight=0)

        top = ttk.Frame(side)
        top.pack(fill="x")
        ttk.Label(top, text="Page").pack(side="left")
        self.page_var = tk.StringVar(value=str(min(self.pages) if self.pages else ""))
        pages = ttk.Combobox(top, textvariable=self.page_var, values=sorted(self.pages), width=5, state="readonly")
        pages.pack(side="left", padx=(6, 0))
        pages.bind("<<ComboboxSelected>>", lambda e: self.show_page(int(self.page_var.get())))

        ttk.Label(side, text="Find symbols (tag, text, id or role):").pack(anchor="w", pady=(10, 2))
        find = ttk.Frame(side)
        find.pack(fill="x")
        self.search_var = tk.StringVar(value=search or "")
        ttk.Entry(find, textvariable=self.search_var).pack(side="left", fill="x", expand=True)
        ttk.Button(find, text="Highlight all", command=self.highlight_matches).pack(side="left", padx=(6, 0))
        self.search_var.trace_add("write", lambda *_: self.fill_list())

        toggles = ttk.Frame(side)
        toggles.pack(fill="x", pady=(8, 4))
        self.show_ocr = tk.BooleanVar(value=True)
        self.show_strokes = tk.BooleanVar(value=False)
        self.show_loose = tk.BooleanVar(value=True)
        for text, var, tag in (("OCR text", self.show_ocr, "ocr"), ("Lettering strokes", self.show_strokes, "stroke"),
                               ("Lines that join nothing", self.show_loose, "loose")):
            ttk.Checkbutton(toggles, text=text, variable=var,
                            command=lambda v=var, t=tag: self.canvas.itemconfigure(
                                t, state="normal" if v.get() else "hidden")).pack(anchor="w")

        listing = ttk.Frame(side)
        listing.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(listing, columns=("role", "text", "links"), height=12, selectmode="browse")
        for column, title, width in (("#0", "Id", 80), ("role", "Role", 80), ("text", "Tag / text", 140),
                                     ("links", "Links", 45)):
            self.tree.heading(column, text=title)
            self.tree.column(column, width=width, stretch=column == "text")
        scroll = ttk.Scrollbar(listing, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="left", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self.on_list_select)

        ttk.Label(side, text="Selected:").pack(anchor="w", pady=(8, 2))
        details = ttk.Frame(side)
        details.pack(fill="both", expand=True)
        self.details = tk.Text(details, width=58, height=16, wrap="none", font=("Consolas", 9), bg="#1e1e1e",
                               fg="#e6e6e6", insertbackground="#e6e6e6", relief="flat")
        dscroll = ttk.Scrollbar(details, orient="vertical", command=self.details.yview)
        self.details.configure(yscrollcommand=dscroll.set)
        self.details.pack(side="left", fill="both", expand=True)
        dscroll.pack(side="left", fill="y")

        c = self.canvas
        c.bind("<MouseWheel>", self.on_wheel)
        for button in ("2", "3"):
            c.bind(f"<ButtonPress-{button}>", self.on_press)
            c.bind(f"<B{button}-Motion>", self.on_drag)
        c.bind("<Button-1>", self.on_click)
        c.bind("<Configure>", self.on_resize)

    # ------------------------------------------------------------------------------------------- drawing
    def show_page(self, number: int) -> None:
        self.page_number = number
        conn = self.conn_pages.get(number, {"components": [], "nets": []})
        self.components = {c["id"]: c for c in conn["components"]}
        self.nets = {n["id"]: n for n in conn["nets"]}
        self.fill_list()
        self.fit()

    def _xy(self, p) -> tuple:
        return p[0] * self.zoom + self.ox, p[1] * self.zoom + self.oy

    def _flat(self, points) -> list:
        return [v for p in points for v in self._xy(p)]

    def fit(self) -> None:
        """Whole page in the window, drawn fresh."""
        page = self.pages.get(self.page_number)
        if page is None:
            return
        width, height = max(self.canvas.winfo_width(), 50), max(self.canvas.winfo_height(), 50)
        x0, y0, x1, y1 = page.get("rect") or [0, 0, 1224, 792]
        self.zoom = min((width - 20) / max(x1 - x0, 1), (height - 20) / max(y1 - y0, 1))
        self.ox, self.oy = 10 - x0 * self.zoom, 10 - y0 * self.zoom
        self.draw()
        if self.selected:
            self.select(self.selected, center=False)

    def draw(self) -> None:
        c = self.canvas
        c.delete("all")
        self.original.clear()
        self.text_size.clear()
        self.line_size.clear()
        self.highlighted = []
        page = self.pages[self.page_number]
        part_of = {}     # object id -> component id
        for comp in self.components.values():
            for oid in comp["parts"]:
                part_of[oid] = comp["id"]
        net_of = {w: n["id"] for n in self.nets.values() for w in n["wires"]}
        layers = {"loose": [], "stroke": [], "wire": [], "comp": []}
        for o in page.get("objects", []):
            if o.get("made_of"):
                continue                           # shapes made of lines are drawn as their lines
            comp = part_of.get(o["id"])
            outline_of = next((part_of[s] for s in o.get("part_of_shape") or [] if s in part_of), None)
            net = net_of.get(o["id"])
            if o.get("text_source") == "part of text":
                layers["stroke"].append((o, ["stroke"], STROKE))
            elif comp:
                layers["comp"].append((o, ["comp", f"C:{comp}"], ROLE_COLORS.get(self.components[comp]["role"], WIRE)))
            elif net and len(self.nets[net]["components"]) >= 2:
                layers["wire"].append((o, ["wire", f"N:{net}"], WIRE))    # a wire, even where it also bounds a shape
            elif outline_of and not net:
                layers["comp"].append((o, ["comp", f"C:{outline_of}"],
                                       ROLE_COLORS.get(self.components[outline_of]["role"], WIRE)))
            else:
                layers["loose"].append((o, ["loose"] + ([f"N:{net}"] if net else []), LOOSE))
        for layer in ("loose", "stroke", "wire", "comp"):
            for o, tags, color in layers[layer]:
                self._draw_object(o, tags, color)
        for t in page.get("ocr_text", []):
            self._draw_text(t)
        c.itemconfigure("ocr", state="normal" if self.show_ocr.get() else "hidden")
        c.itemconfigure("stroke", state="normal" if self.show_strokes.get() else "hidden")
        c.itemconfigure("loose", state="normal" if self.show_loose.get() else "hidden")

    def _px(self, item) -> float:
        """Screen width of a drawn line: its real width in the PDF at the current zoom (at least 1 pixel)."""
        return max(self.line_size.get(item, 0.72) * self.zoom, 1.0)

    def _draw_object(self, o: dict, tags: list, color: str) -> None:
        c = self.canvas
        tags = tuple(tags + [f"O:{o['id']}"])
        size = max(o.get("line_width") or 0.72, 0.3)     # pt, as in the PDF
        if "comp" in tags:
            size *= 1.5                                    # symbols a little bolder so the colours show
        dash = {"dash": (4, 3)} if (o.get("dashes") or "") not in Pdf_Connections.SOLID_DASHES else {}
        kind = o["kind"]
        items = []
        if o.get("path"):
            # 1:1 redraw: every piece of the path as drawn in the PDF (curves as many short pieces), filled if the PDF
            # fills it.
            filled = bool(o.get("fill"))
            for run in o["path"]:
                closed = len(run) > 2 and run[0] == run[-1]
                if filled and closed:
                    items.append((c.create_polygon(*self._flat(run), outline=color, fill=color, tags=tags),
                                  "outline", True))
                elif len(run) > 1:
                    items.append((c.create_line(*self._flat(run), fill=color, tags=tags, **dash), "fill", False))
        elif kind in ELLIPSE_KINDS:
            # (files saved before "path" was kept: drawn from the simplified geometry)
            filled = kind == "dot" or bool(o.get("fill"))
            items.append((c.create_oval(*self._flat([o["rect"][:2], o["rect"][2:]]), outline=color,
                                        fill=color if filled else "", tags=tags), "outline", filled))
        elif o.get("vertices") and o.get("closed") and o.get("fill"):
            items.append((c.create_polygon(*self._flat(o["vertices"]), outline=color, fill=color,
                                           tags=tags), "outline", True))
        elif o.get("segments"):
            for run in _chains(o["segments"]):
                items.append((c.create_line(*self._flat(run), fill=color, tags=tags, **dash), "fill", False))
        elif kind in BOX_KINDS or o.get("closed"):
            items.append((c.create_rectangle(*self._flat([o["rect"][:2], o["rect"][2:]]), outline=color,
                                             tags=tags, **dash), "outline", False))
        for item, key, filled in items:
            self.line_size[item] = size
            c.itemconfigure(item, width=self._px(item))
            self.original[item] = {key: color, **({"fill": color} if filled and key == "outline" else {})}

    def _draw_text(self, t: dict) -> None:
        # The direction comes from the box shape (OCR's "orientation" only says which pass read it): a tall box
        # holding several characters is vertical text. The letter size is the box's short side, but never so big that
        # the text runs past the box's long side (about 0.62 letter heights per character).
        x0, y0, x1, y1 = t["rect"]
        width, height = x1 - x0, y1 - y0
        count = max(len(t["text"]), 1)
        vertical = count > 1 and height > 1.2 * width
        along, across = (height, width) if vertical else (width, height)
        size = max(min(0.8 * across, along / (0.62 * count)), 0.5)
        item = self.canvas.create_text(*self._xy(t["center"]), text=t["text"], fill=OCR_TEXT, angle=90 if vertical else 0,
                                       font=("Arial", -max(1, int(size * self.zoom))), tags=("ocr",))
        self.text_size[item] = size

    # ------------------------------------------------------------------------------------------- view
    def on_resize(self, event) -> None:
        if not self._fitted and event.width > 60 and self.pages:
            self._fitted = True
            self.show_page(int(self.page_var.get()))
            if self.search_var.get().strip():
                self.highlight_matches()

    def _zoom_at(self, x: float, y: float, factor: float) -> None:
        self.canvas.scale("all", x, y, factor, factor)
        self.ox, self.oy = x + factor * (self.ox - x), y + factor * (self.oy - y)
        self.zoom *= factor
        for item, size in self.text_size.items():
            self.canvas.itemconfigure(item, font=("Arial", -max(1, int(size * self.zoom))))
        bold = set(self.highlighted)
        for item in self.line_size:
            if item not in bold:
                self.canvas.itemconfigure(item, width=self._px(item))

    def on_wheel(self, event) -> None:
        self._zoom_at(event.x, event.y, ZOOM_STEP if event.delta > 0 else 1 / ZOOM_STEP)

    def on_press(self, event) -> None:
        self._drag = (event.x, event.y)

    def on_drag(self, event) -> None:
        if self._drag:
            dx, dy = event.x - self._drag[0], event.y - self._drag[1]
            self.canvas.move("all", dx, dy)
            self.ox, self.oy = self.ox + dx, self.oy + dy
            self._drag = (event.x, event.y)

    def center_on(self, cid: str) -> None:
        comp = self.components.get(cid)
        if not comp:
            return
        x0, y0, x1, y1 = comp["rect"]
        size = max(x1 - x0, y1 - y0, 1) * self.zoom
        width, height = self.canvas.winfo_width(), self.canvas.winfo_height()
        cx, cy = self._xy(((x0 + x1) / 2, (y0 + y1) / 2))
        dx, dy = width / 2 - cx, height / 2 - cy
        self.canvas.move("all", dx, dy)
        self.ox, self.oy = self.ox + dx, self.oy + dy
        if size < 60:                              # zoom in so the shape is easy to see
            self._zoom_at(width / 2, height / 2, min(60 / size, 12))

    # ------------------------------------------------------------------------------------------- selection
    def _paint(self, tag: str, color: str, width: float) -> None:
        for item in self.canvas.find_withtag(tag):
            if item not in self.original:
                continue
            options = {key: color for key in self.original[item]}
            self.canvas.itemconfigure(item, width=max(width, self._px(item)), **options)
            self.canvas.tag_raise(item)
            self.highlighted.append(item)

    def clear(self) -> None:
        for item in self.highlighted:
            self.canvas.itemconfigure(item, width=self._px(item), **self.original[item])
        self.highlighted = []
        self.canvas.tag_raise("ocr")

    def _show_details(self, text: str) -> None:
        self.details.delete("1.0", "end")
        self.details.insert("1.0", text)

    def select(self, cids: list, center: bool = True) -> None:
        """Highlight components, the components their lines join them to and the joining nets."""
        self.clear()
        self.selected = list(cids)
        for cid in cids:
            for link in self.components.get(cid, {}).get("connections", []):
                self._paint(f"N:{link['via']}", NET, 2.6)
                self._paint(f"C:{link['id']}", LINKED, 2.6)
        for cid in cids:
            self._paint(f"C:{cid}", SELECTED, 3.0)
        self.canvas.tag_raise("ocr")
        self._show_details("\n\n".join(self._link_text(cid) for cid in cids))
        if center and len(cids) == 1:
            self.center_on(cids[0])

    def _link_text(self, cid: str) -> str:
        """A symbol's PLC / panel links (from the saved instrument links) and its direct connections."""
        comp = self.components.get(cid, {})
        lines = [f'{cid}  {comp.get("role", "")}  "{comp.get("tag") or comp.get("text", "")}"  at {comp.get("center")}']
        for c in self.links["controllers"]:
            if c["id"] == cid:
                lines.append(f"  instruments on its lines ({len(c['instruments'])}):")
                for i in c["instruments"]:
                    through = ("  through " + ", ".join(f'{t["id"]} "{t["text"]}"' for t in i["through"])) if i["through"] else ""
                    beside = f'  (tag beside {i["beside"]["id"]} {i["beside"]["kind"]})' if i.get("beside") else ""
                    lines.append(f"    {i['tag']:<10} {i['id']}{through}{beside}")
        for i in self.links["instruments"]:
            if i["id"] == cid:
                lines.append("  PLC / panels: " + (", ".join(i["controllers"]) or "(none found)"))
        if comp.get("labels"):
            lines.append("  names the symbol(s) beside it: " + ", ".join(comp["labels"]))
        if comp.get("labelled_by"):
            lines.append(f"  named by the tag beside it: {comp['labelled_by']}")
        lines.append("  direct connections:")
        found = Pdf_Connections.query_components(self.connections, ids=[cid], depth=1)
        lines += [Pdf_Connections.describe(found[0]["connections"], 2)] if found and found[0]["connections"] else ["    (none)"]
        return "\n".join(lines)

    def select_net(self, nid: str) -> None:
        self.clear()
        self.selected = []
        net = self.nets[nid]
        self._paint(f"N:{nid}", NET, 2.6)
        for cid in net["components"]:
            self._paint(f"C:{cid}", LINKED, 2.6)
        self.canvas.tag_raise("ocr")
        lines = [f"{nid}  {'dashed' if net.get('dashed') else 'solid'}  {len(net['wires'])} line path(s)", "reaches:"]
        lines += [f'    {cid}  {self.components[cid]["role"]}  "{self.components[cid]["tag"] or self.components[cid]["text"]}"'
                  for cid in net["components"]] or ["    (no symbols)"]
        self._show_details("\n".join(lines))

    def on_click(self, event) -> None:
        p = ((event.x - self.ox) / self.zoom, (event.y - self.oy) / self.zoom)
        slack = 3 / self.zoom
        inside = [c for c in self.components.values()
                  if c["rect"][0] - slack <= p[0] <= c["rect"][2] + slack and c["rect"][1] - slack <= p[1] <= c["rect"][3] + slack]
        if inside:
            cid = min(inside, key=lambda c: (c["rect"][2] - c["rect"][0]) * (c["rect"][3] - c["rect"][1]))["id"]
            self.select([cid], center=False)
            if self.tree.exists(cid):
                self.tree.selection_set(cid)   # on_list_select sees it is already selected and does not re-centre
                self.tree.see(cid)
            return
        for item in reversed(self.canvas.find_overlapping(event.x - 3, event.y - 3, event.x + 3, event.y + 3)):
            net = next((t[2:] for t in self.canvas.gettags(item) if t.startswith("N:")), None)
            if net:
                self.select_net(net)
                return
        self.clear()
        self.selected = []
        self._show_details("")

    # ------------------------------------------------------------------------------------------- list / search
    def _matches(self) -> list:
        text = self.search_var.get().strip().lower()
        return [c for c in self.components.values()
                if not text or any(text in (c.get(key) or "").lower() for key in ("text", "tag", "id", "role", "kind"))]

    def fill_list(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for c in self._matches():
            self.tree.insert("", "end", iid=c["id"], text=c["id"],
                             values=(c["role"], c["tag"] or c["text"], len(c["connections"])))

    def on_list_select(self, _event=None) -> None:
        chosen = self.tree.selection()
        if chosen and [chosen[0]] != self.selected:
            self.select([chosen[0]], center=True)

    def highlight_matches(self) -> None:
        if self.search_var.get().strip():
            self.select([c["id"] for c in self._matches()], center=False)


def run_connection_viewer(shapes_json, connections_json, search: str = "PLC") -> dict:
    """Open the viewer (see module docstring) and wait until it is closed.
    Returns {"selected": [component ids selected when it was closed]}.
    `shapes_json` / `connections_json` may be matching nested lists of paths (one window after another,
    same-shape results back)."""
    if isinstance(shapes_json, (list, tuple)):
        return [run_connection_viewer(s, c, search) for s, c in zip(shapes_json, connections_json)]
    properties, connections = _load(shapes_json), _load(connections_json)
    gc.collect()  # free Tk objects from earlier windows on this (main) thread, see GUI.run_with_progress
    root = make_window(f"P&ID Connection Viewer - {Path(connections.get('source_pdf', '')).name}", "1500x900",
                       (1000, 600))
    viewer = _Viewer(root, properties, connections, search)
    root.protocol("WM_DELETE_WINDOW", root.destroy)
    root.mainloop()
    result = {"selected": list(viewer.selected)}
    # Window is gone: drop the Tk references here, on the main thread (see GUI.run_with_progress).
    del viewer, root
    gc.collect()
    return result


if __name__ == "__main__":
    paths = sys.argv[1:3]
    if len(paths) < 2:
        from tkinter import filedialog
        picker = tk.Tk()
        picker.withdraw()
        paths = [filedialog.askopenfilename(title="Shapes JSON (<pdf> - shapes.json)", filetypes=[("JSON", "*.json")]),
                 filedialog.askopenfilename(title="Instrument connections JSON", filetypes=[("JSON", "*.json")])]
        picker.destroy()
    if all(paths):
        print(run_connection_viewer(paths[0], paths[1]))
