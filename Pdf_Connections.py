"""Instrument tags and their connections to the PLC / panels, from one P&ID page's objects (Pdf_Shapes_OCR).

components   symbols: closed shapes big enough to hold a tag (circles, ovals, diamonds, rectangles, ... and shapes
             made of separate lines). A shape drawn inside a small symbol (diamond in a square, circle in a square)
             is one component. Its text is the shape OCR; its role comes from the text:
               "plc"        text has PLC; or a diamond with any other text that is not a tag (STR) - tag = its text
               "panel"      text has PANEL / MCC / VFD / DCS / HMI / LCP / CONTROL / STARTER (PANEL_WORDS)
               "instrument" text is a tag: 1-5 letters + number, e.g. PT 1, TT 023, SV 031, AV 054 ("tag" field)
               "other"      anything else (solenoid box "S", valve symbol, ...)
wires        lines, polylines, arcs, open line+curve paths; junction dots and arrowheads are wire pieces.
             Lettering, symbol outlines and lines drawn wholly inside a small symbol are not wires.
nets         wires joined: end to end or an end on another wire (a T); a dashed line drawn as separate dashes
             (dash to dash across a gap, also round a corner); a line broken where it passes UNDER another line.
             Lines that only cross are not joined.
connections  two components are connected when one net reaches both (a wire end on the outline, or inside a
             small symbol; a dash also reaches a symbol across one gap).

build_connections()  page objects -> {"components", "nets"}
instrument_links()   every PLC / panel -> the instruments its lines reach (directly, or through at most one
                     unlabelled symbol such as a solenoid box); every instrument -> its PLC / panels
export_connections() "<pdf> - instrument connections.json" + ".txt" in Working
query_components() / describe()   one component with its connections (used by the viewer)
Coordinates are PDF points (1/72 inch), origin top-left.
"""
import datetime
import json
import math
import re
from collections import defaultdict
from pathlib import Path

from GUI import flatten, map_nested, working_path
from Pdf_Shapes_OCR import SYMBOL_MIN, _in_polygon, _inside, _rect_within

JOIN = 1.0               # a wire end this close (pt) to another wire joins it
TOUCH = 1.5              # a wire end this close (pt) to a symbol's outline reaches it
SYMBOL_MAX = 80.0        # symbols up to this size (pt) are "small": shapes inside are the same symbol, lines wholly
                         # inside are detail, a wire end inside reaches it
FRAME_FRACTION = 0.6     # closed shapes wider / taller than this part of the page (frame, title block) are skipped
DASH_MAX = 12.0          # a single straight line this short (pt) may be one dash of a dashed line
DASH_GAP = 5.0           # a dash joins the next piece / symbol straight ahead (or the next dash round a corner)
UNDER_GAP = 13.0         # a line broken where it passes under another line: ends in line up to this far apart (pt)
UNDER_CLEAR = 1.0        # ... with another line crossing the break at least this far (pt) from both ends
PARALLEL = 0.05          # dash and next piece run the same way (sine of the angle between them)
ALIGN = 0.6              # ... and are in line: sideways offset up to this (pt)
LABEL_GAP = 8.0          # a tag bubble this close (pt, box to box) to an unlabelled symbol names it (SV tag by its valve)
MAX_THROUGH = 1        # instrument_links: unlabelled symbols a PLC / panel link may pass through (solenoid box)
CELL = 16.0              # grid cell (pt) for finding nearby things

PANEL_WORDS = ("PANEL", "MCC", "VFD", "DCS", "HMI", "LCP", "CONTROL", "STARTER")
TAG_RE = re.compile(r"^([A-Z](?:[A-Z/]{0,3}[A-Z])?)\s*-?\s*(\d{1,4}[A-Z]?)$")   # PT 1, TT-023, H/S 1
SYMBOL_KINDS = {"circle", "oval", "rectangle", "diamond", "triangle", "quadrilateral", "polygon", "quad", "mixed"}
ELLIPSE_KINDS = {"circle", "oval", "small circle", "dot"}
JUNCTION_KINDS = {"dot", "arrowhead"}
WIRE_KINDS = {"line", "polyline", "arc", "mixed"}
SOLID_DASHES = ("", "[] 0", "[] 0.0")


# ------------------------------------------------------------------------------------------------ text -> role
def classify_text(text):
    """(role, tag) from a symbol's text: ("plc", "PLC"), ("panel", text), ("instrument", "PT 1"), ("other", "").
    `text` may be a nested list (same-shape results back)."""
    if isinstance(text, (list, tuple)):
        return map_nested(classify_text, text)
    clean = (text or "").upper().replace("／", "/")          # OCR sometimes reads "/" as the full-width "／"
    clean = re.sub(r"[^A-Z0-9\-/ ]", " ", clean)
    clean = re.sub(r"\s*/\s*", "/", re.sub(r"\s+", " ", clean)).strip()   # "H / S" -> "H/S"
    if re.search(r"P\s*L\s*C", clean):
        return "plc", "PLC"
    if any(word in clean for word in PANEL_WORDS):
        return "panel", clean
    found = TAG_RE.match(clean)
    if found:
        return "instrument", f"{found.group(1)} {found.group(2)}"
    return "other", ""


def symbol_role(text, kind=""):
    """classify_text for a symbol of a given kind: a diamond with text that is not a tag is a controller, listed like
    a PLC with its own text as the tag (the "STR" diamond: starter by customer). `text` may be a nested list, with
    `kind` one kind for all or the matching nested list (same-shape results back)."""
    if isinstance(text, (list, tuple)):
        kinds = kind if isinstance(kind, (list, tuple)) else [kind] * len(text)
        return [symbol_role(t, k) for t, k in zip(text, kinds)]
    role, tag = classify_text(text)
    if role == "other" and kind == "diamond":
        clean = re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9\-/ ]", " ", (text or "").upper())).strip()
        if clean:
            return "plc", clean
    return role, tag


# ------------------------------------------------------------------------------------------------ geometry
def _pt(p) -> tuple:
    return (float(p[0]), float(p[1]))


def _size(o: dict) -> float:
    return max(o["width_pt"], o["height_pt"])


def _area(rect) -> float:
    return max(0.0, rect[2] - rect[0]) * max(0.0, rect[3] - rect[1])


def _center(rect) -> list:
    return [round((rect[0] + rect[2]) / 2, 2), round((rect[1] + rect[3]) / 2, 2)]


def _grow(rect, pad) -> tuple:
    return (rect[0] - pad, rect[1] - pad, rect[2] + pad, rect[3] + pad)


def _seg_dist(p, a, b) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = dx * dx + dy * dy
    t = 0.0 if length == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / length))
    return math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy)


def _unit(a, b) -> tuple:
    length = math.hypot(b[0] - a[0], b[1] - a[1]) or 1e-9
    return ((b[0] - a[0]) / length, (b[1] - a[1]) / length)


def _cross(u, v) -> float:
    return u[0] * v[1] - u[1] * v[0]


def _outline_points(o: dict, count: int = 16) -> list:
    if o["kind"] in ELLIPSE_KINDS:
        x0, y0, x1, y1 = o["rect"]
        cx, cy, a, b = (x0 + x1) / 2, (y0 + y1) / 2, (x1 - x0) / 2, (y1 - y0) / 2
        return [(cx + a * math.cos(2 * math.pi * i / count), cy + b * math.sin(2 * math.pi * i / count))
                for i in range(count)]
    if o.get("vertices") and o.get("closed"):
        return [_pt(p) for p in o["vertices"]]
    x0, y0, x1, y1 = o["rect"]
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


class _Shape:
    """A symbol's outline, for "does a wire end reach it" tests."""

    def __init__(self, obj: dict):
        self.obj = obj
        self.rect = tuple(obj["rect"])
        self.size = _size(obj)
        x0, y0, x1, y1 = self.rect
        self.ellipse = ((x0 + x1) / 2, (y0 + y1) / 2, max((x1 - x0) / 2, 1e-6), max((y1 - y0) / 2, 1e-6)) \
            if obj["kind"] in ELLIPSE_KINDS else None
        points = _outline_points(obj)
        self.edges = [(points[i - 1], points[i]) for i in range(len(points))]

    def distance(self, p) -> float:
        if self.ellipse:
            cx, cy, a, b = self.ellipse
            dx, dy = p[0] - cx, p[1] - cy
            r = math.hypot(dx / a, dy / b)
            return min(a, b) if r == 0 else abs(1 - 1 / r) * math.hypot(dx, dy)
        return min(_seg_dist(p, a, b) for a, b in self.edges)

    def contains(self, p) -> bool:
        if self.ellipse:
            cx, cy, a, b = self.ellipse
            return math.hypot((p[0] - cx) / a, (p[1] - cy) / b) <= 1
        if self.obj.get("vertices"):
            return _in_polygon(p, self.obj["vertices"])
        return _inside(p, self.rect)

    def reached(self, p) -> bool:
        return self.distance(p) <= TOUCH or (self.size <= SYMBOL_MAX and self.contains(p))


class _Grid:
    def __init__(self):
        self.cells = defaultdict(list)

    @staticmethod
    def _span(box):
        return (range(math.floor(box[0] / CELL), math.floor(box[2] / CELL) + 1),
                range(math.floor(box[1] / CELL), math.floor(box[3] / CELL) + 1))

    def add(self, box, item: int) -> None:
        xs, ys = self._span(box)
        for i in xs:
            for j in ys:
                self.cells[(i, j)].append(item)

    def near(self, box) -> set:
        xs, ys = self._span(box)
        return {item for i in xs for j in ys for item in self.cells.get((i, j), ())}


class _Union:
    def __init__(self):
        self.parent = {}

    def find(self, a):
        self.parent.setdefault(a, a)
        while self.parent[a] != a:
            self.parent[a] = self.parent[self.parent[a]]
            a = self.parent[a]
        return a

    def join(self, a, b) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


# ------------------------------------------------------------------------------------------------ line helpers
def _is_dash(segments) -> bool:
    return len(segments) == 1 and math.hypot(segments[0][1][0] - segments[0][0][0],
                                             segments[0][1][1] - segments[0][0][1]) <= DASH_MAX


def _dash_ends(segments) -> list:
    """[(end point, outward direction)] for both ends of a dash; [] for anything else."""
    if not _is_dash(segments):
        return []
    a, b = segments[0]
    return [(b, _unit(a, b)), (a, _unit(b, a))]


def _line_ends(segments) -> list:
    """[(end point, outward direction)] at both ends of an open run of straight pieces."""
    if not segments:
        return []
    (a, b), (c, d) = segments[0], segments[-1]
    if len(segments) > 1 and abs(a[0] - d[0]) < 0.01 and abs(a[1] - d[1]) < 0.01:
        return []
    return [(a, _unit(b, a)), (d, _unit(c, d))]


def _dash_corner(p, u, other_segments) -> bool:
    """Another dash turns a corner with this one: carried on straight, both meet within DASH_GAP of their ends."""
    for q, v in _dash_ends(other_segments):
        turn = _cross(u, v)
        if abs(turn) < 0.5:
            continue
        d = (q[0] - p[0], q[1] - p[1])
        s, r = _cross(d, v) / turn, _cross(d, u) / turn
        if -JOIN <= s <= DASH_GAP and -JOIN <= r <= DASH_GAP:
            return True
    return False


def _runs_through(p, q, gap: float, skip: set, pieces: list, grid) -> bool:
    """Another line crosses the open break p-q, at least UNDER_CLEAR from both ends."""
    r = (q[0] - p[0], q[1] - p[1])
    margin = UNDER_CLEAR / gap
    for k in grid.near((min(p[0], q[0]), min(p[1], q[1]), max(p[0], q[0]), max(p[1], q[1]))):
        w, a, b = pieces[k]
        if w in skip:
            continue
        s = (b[0] - a[0], b[1] - a[1])
        turn = _cross(r, s)
        if abs(turn) < 1e-9:
            continue
        d = (a[0] - p[0], a[1] - p[1])
        t, along = _cross(d, s) / turn, _cross(d, r) / turn
        if margin <= t <= 1 - margin and -0.01 <= along <= 1.01:
            return True
    return False


def _on_outline(a, b, outlines: list) -> bool:
    mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
    return any(all(s.distance(p) <= JOIN for p in (a, mid, b)) for s in outlines)


def _leftover_segments(objects: list, symbol_ids: set) -> list:
    """Pieces of a symbol's own path that are not on its outline (the stem drawn in one path with a PLC diamond):
    wires."""
    out = []
    for o in objects:
        if o["id"] in symbol_ids and o.get("segments") and o.get("text_source") != "part of text":
            outline = [_Shape(o)]
            out += [(o, (_pt(a), _pt(b))) for a, b in o["segments"] if not _on_outline(_pt(a), _pt(b), outline)]
    return out


def _outline_only(o: dict, outlines: list) -> bool:
    """Does every piece of this path lie on the outline of a symbol it bounds? Then it is that symbol's outline, not
    a wire. A path that carries on past the symbol (a pipe or signal line bounding an area, the last dash drawn in one
    polyline with a PLC diamond's edge) stays a whole wire."""
    return bool(outlines) and all(_on_outline(_pt(a), _pt(b), outlines) for a, b in o.get("segments") or [])


def _rect_gap(a, b) -> float:
    """Distance (pt) between two boxes; 0 when they touch or overlap."""
    dx = max(a[0] - b[2], b[0] - a[2], 0.0)
    dy = max(a[1] - b[3], b[1] - a[3], 0.0)
    return math.hypot(dx, dy)


def _label_beside(components: list) -> None:
    """A tag bubble placed beside a symbol without a line (the "SV 121" oval under its valve, beside its "S" box)
    names that symbol: every small unlabelled symbol ("other") within LABEL_GAP of an instrument's box gets
    "labelled_by": the nearest such instrument's id, and the instrument gets "labels": [those symbols' ids].
    instrument_links then counts reaching the symbol as reaching the instrument."""
    tags = [c for c in components if c["role"] == "instrument"]
    for c in components:
        if c["role"] != "other" or max(c["rect"][2] - c["rect"][0], c["rect"][3] - c["rect"][1]) > SYMBOL_MAX:
            continue
        near = [(_rect_gap(c["rect"], t["rect"]), t) for t in tags]
        near = [(gap, t) for gap, t in near if gap <= LABEL_GAP]
        if near:
            tag = min(near, key=lambda n: n[0])[1]
            c["labelled_by"] = tag["id"]
            tag.setdefault("labels", []).append(c["id"])


# ------------------------------------------------------------------------------------------------ build
def build_connections(objects: list, page_rect=None, page_number: int = None) -> dict:
    """{"page", "components": [{"id", "kind", "text", "role", "tag", "center", "rect", "parts", "nets",
    "connections": [{"id", "via"}]}], "nets": [{"id", "wires", "components", "dashed"}], "counts"}.
    `objects` may be a nested list of pages' objects (same-shape results back)."""
    if objects and isinstance(objects[0], (list, tuple)):
        return [build_connections(one, page_rect, page_number) for one in objects]
    objects = objects or []
    if page_number is None:
        found = re.match(r"p(\d+)-", objects[0]["id"]) if objects else None
        page_number = int(found.group(1)) if found else 1
    if page_rect is None:
        rects = [o["rect"] for o in objects] or [[0, 0, 1, 1]]
        page_rect = (min(r[0] for r in rects), min(r[1] for r in rects), max(r[2] for r in rects), max(r[3] for r in rects))
    page_rect = tuple(float(v) for v in page_rect)
    frame = (FRAME_FRACTION * (page_rect[2] - page_rect[0]), FRAME_FRACTION * (page_rect[3] - page_rect[1]))

    # --- symbols
    # (an area traced from other paths' lines - "made_of" - only counts when it holds text: most enclosed areas are
    # just gaps between pipes and lines)
    symbol_objs = [o for o in objects if o.get("closed") and o["kind"] in SYMBOL_KINDS
                   and o.get("text_source") != "part of text" and (not o.get("made_of") or o.get("text"))
                   and min(o["width_pt"], o["height_pt"]) >= SYMBOL_MIN
                   and o["width_pt"] <= frame[0] and o["height_pt"] <= frame[1]]
    symbol_ids = {o["id"] for o in symbol_objs}
    shapes = [_Shape(o) for o in symbol_objs]
    shape_of = {s.obj["id"]: s for s in shapes}
    shape_grid = _Grid()
    for i, s in enumerate(shapes):
        shape_grid.add(_grow(s.rect, TOUCH), i)
    merge = _Union()
    for i, s in enumerate(shapes):
        merge.find(i)
        if s.size > SYMBOL_MAX:
            continue
        holders = [j for j in shape_grid.near(s.rect) if j != i and shapes[j].size <= SYMBOL_MAX
                   and _rect_within(s.rect, shapes[j].rect, 1.0) and _area(shapes[j].rect) >= _area(s.rect)]
        if holders:
            merge.join(i, min(holders, key=lambda j: _area(shapes[j].rect)))
    members = defaultdict(list)
    for i in range(len(shapes)):
        members[merge.find(i)].append(i)
    groups = sorted((sorted(idx, key=lambda k: -_area(shapes[k].rect)) for idx in members.values()),
                    key=lambda idx: (round(_center(shapes[idx[0]].rect)[1] / 4), _center(shapes[idx[0]].rect)[0]))
    group_of, components = {}, []
    for n, idx in enumerate(groups, start=1):
        for k in idx:
            group_of[k] = n - 1
        parts = [shapes[k].obj for k in idx]
        texts = []
        for p in parts:
            if p.get("text") and p["text"] not in texts:
                texts.append(p["text"])
        text = " ".join(texts)
        role, tag = classify_text(text)
        for p in parts[1:]:                          # a tag / PLC in an inner part decides the role
            if role == "other":
                role, tag = classify_text(p.get("text"))
        if role == "other":                          # a diamond with text (STR) is listed like a PLC
            role, tag = symbol_role(text, parts[0]["kind"])
        rect = shapes[idx[0]].rect
        components.append({"id": f"p{page_number}-C{n}", "page": page_number,
                           "kind": parts[0]["kind"] + (f" with {', '.join(p['kind'] for p in parts[1:])} inside"
                                                       if len(parts) > 1 else ""),
                           "text": text, "role": role, "tag": tag, "center": _center(rect),
                           "rect": [round(v, 2) for v in rect], "parts": [p["id"] for p in parts],
                           "nets": [], "connections": []})

    # --- wires
    wires = [(o, [seg], list(seg)) for o, seg in _leftover_segments(objects, symbol_ids)]
    for o in objects:
        if o.get("text_source") == "part of text" or o["id"] in symbol_ids:
            continue
        if _outline_only(o, [shape_of[s] for s in o.get("part_of_shape") or [] if s in shape_of]):
            continue
        junction = o["kind"] in JUNCTION_KINDS
        if not junction and not (o["kind"] in WIRE_KINDS and not o.get("closed") and o.get("segments")):
            continue
        rect = tuple(o["rect"])
        if any(shapes[j].size <= SYMBOL_MAX and _rect_within(rect, shapes[j].rect, TOUCH) for j in shape_grid.near(rect)):
            continue                                  # drawn inside a symbol: detail / lettering
        if junction:
            points = _outline_points(o)
            wires.append((o, [(points[i - 1], points[i]) for i in range(len(points))], points))
        else:
            segments = [(_pt(a), _pt(b)) for a, b in o["segments"]]
            wires.append((o, segments, [_pt(o["start"]), _pt(o["end"])]))

    pieces, piece_grid = [], _Grid()
    for w, (_, segments, _) in enumerate(wires):
        for a, b in segments:
            piece_grid.add(_grow((min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1])), JOIN), len(pieces))
            pieces.append((w, a, b))
    nets = _Union()
    for w, (_, _, ends) in enumerate(wires):         # end to end / end on another wire
        nets.find(w)
        for p in ends:
            for k in piece_grid.near((p[0], p[1], p[0], p[1])):
                other, a, b = pieces[k]
                if other != w and _seg_dist(p, a, b) <= JOIN:
                    nets.join(w, other)
    dashed = set()
    for w, (_, segments, _) in enumerate(wires):     # dashed lines drawn as separate dashes
        for p, u in _dash_ends(segments):
            for k in piece_grid.near(_grow((p[0], p[1], p[0], p[1]), 1.5 * DASH_GAP)):
                other, a, b = pieces[k]
                if other == w:
                    continue
                if abs(_cross(u, _unit(a, b))) <= PARALLEL:
                    joined = any(0 < (q[0] - p[0]) * u[0] + (q[1] - p[1]) * u[1] <= DASH_GAP
                                 and abs(_cross(u, (q[0] - p[0], q[1] - p[1]))) <= ALIGN for q in (a, b))
                else:
                    joined = _dash_corner(p, u, wires[other][1])
                if joined:
                    nets.join(w, other)
                    dashed.update((w, other))
    for w, (o, segments, _) in enumerate(wires):     # broken where it passes under another line
        if o["kind"] in JUNCTION_KINDS:
            continue
        for p, u in _line_ends(segments):
            if any(shapes[j].reached(p) for j in shape_grid.near((p[0], p[1], p[0], p[1]))):
                continue
            for k in piece_grid.near(_grow((p[0], p[1], p[0], p[1]), UNDER_GAP)):
                other = pieces[k][0]
                if other == w or nets.find(other) == nets.find(w):
                    continue
                for q, v in _line_ends(wires[other][1]):
                    d = (q[0] - p[0], q[1] - p[1])
                    ahead = d[0] * u[0] + d[1] * u[1]
                    if (2 * UNDER_CLEAR < ahead <= UNDER_GAP and abs(_cross(u, d)) <= ALIGN
                            and u[0] * v[0] + u[1] * v[1] < -0.99 and _runs_through(p, q, ahead, {w, other}, pieces, piece_grid)):
                        nets.join(w, other)
                        break

    # --- which symbols each net reaches
    def shapes_at(q):
        return [j for j in shape_grid.near((q[0], q[1], q[0], q[1])) if shapes[j].reached(q)]

    reach = defaultdict(set)     # net root -> component indices
    for w, (o, segments, ends) in enumerate(wires):
        directions = dict(_dash_ends(segments))
        for p in ends:
            hits = shapes_at(p)
            if not hits and p in directions:          # a dash reaches the first symbol across one gap
                u = directions[p]
                for t in (0.5 * DASH_GAP, DASH_GAP):
                    hits = shapes_at((p[0] + u[0] * t, p[1] + u[1] * t))
                    if hits:
                        break
            for j in hits:
                reach[nets.find(w)].add(group_of[j])

    net_members = defaultdict(list)
    for w in range(len(wires)):
        net_members[nets.find(w)].append(w)
    net_list = []
    for n, root in enumerate(sorted(net_members, key=lambda r: min(wires[w][0]["rect"][1] for w in net_members[r])), start=1):
        nid = f"p{page_number}-N{n}"
        touched = sorted(reach.get(root, ()))
        is_dashed = any(w in dashed or (wires[w][0].get("dashes") or "") not in SOLID_DASHES for w in net_members[root])
        net_list.append({"id": nid, "page": page_number,
                         "wires": sorted({wires[w][0]["id"] for w in net_members[root]}),
                         "components": [components[g]["id"] for g in touched], "dashed": is_dashed})
        for g in touched:
            components[g]["nets"].append(nid)
            for h in touched:
                if h != g:
                    components[g]["connections"].append({"id": components[h]["id"], "via": nid})
    _label_beside(components)
    return {"page": page_number, "page_rect": [round(v, 2) for v in page_rect], "components": components,
            "nets": net_list,
            "counts": {"components": len(components),
                       "instruments": sum(1 for c in components if c["role"] == "instrument"),
                       "plc": sum(1 for c in components if c["role"] == "plc"),
                       "panels": sum(1 for c in components if c["role"] == "panel"),
                       "nets reaching 2+ components": sum(1 for n in net_list if len(n["components"]) > 1)}}


# ------------------------------------------------------------------------------------------------ links
def _pages(table) -> list:
    """Page tables from: a path to a saved connections JSON, the saved {"pages": [...]}, or one page table."""
    if isinstance(table, (str, Path)):
        table = json.loads(Path(table).read_text(encoding="utf-8"))   # only read
    if isinstance(table, dict) and "pages" in table:
        return list(table["pages"])
    return [table] if isinstance(table, dict) else []


def instrument_links(table, max_through: int = MAX_THROUGH) -> dict:
    """{"controllers": [{"id", "role", "label", "center", "instruments": [{"tag", "id", "center", "through"}],
    "controllers": [ids of PLC / panels linked directly]}], "instruments": [{"tag", "id", "center", "text",
    "controllers": [ids]}]}. A PLC / panel reaches an instrument through connected components, passing through at
    most `max_through` symbols that are neither instruments nor PLC / panels (e.g. a solenoid box "S").
    `table` may be a nested list (same-shape results back)."""
    if isinstance(table, (list, tuple)):
        return map_nested(lambda one: instrument_links(one, max_through), table)
    comps = {c["id"]: c for page in _pages(table) for c in page.get("components", [])}
    controllers, found_by = [], defaultdict(list)   # (instruments reached via a tag beside a symbol: "beside")
    for c in comps.values():
        if c["role"] not in ("plc", "panel"):
            continue
        entry = {"id": c["id"], "role": c["role"], "label": c["tag"] or c["text"], "center": c["center"],
                 "instruments": [], "controllers": []}
        seen, frontier = {c["id"]}, [(c["id"], [])]
        while frontier:
            cid, through = frontier.pop(0)
            for link in comps[cid]["connections"]:
                other = comps.get(link["id"])
                if other is None or other["id"] in seen:
                    continue
                seen.add(other["id"])
                beside = None
                if other["role"] == "other" and other.get("labelled_by") in comps:
                    # a valve / solenoid box named by the tag bubble beside it: reaching it reaches that instrument
                    # (not searched further: its pipe line leads on to other equipment)
                    beside, other = other, comps[other["labelled_by"]]
                    if other["id"] in seen:
                        continue
                    seen.add(other["id"])
                if other["role"] == "instrument":
                    entry["instruments"].append({"tag": other["tag"], "id": other["id"], "center": other["center"],
                                                 "through": through,
                                                 "beside": ({"id": beside["id"], "kind": beside["kind"],
                                                             "text": beside["text"]} if beside else None)})
                    found_by[other["id"]].append(c["id"])
                elif other["role"] in ("plc", "panel"):
                    if not through:
                        entry["controllers"].append(other["id"])
                elif len(through) < max_through:
                    frontier.append((other["id"], through + [{"id": other["id"], "kind": other["kind"],
                                                              "text": other["text"]}]))
        controllers.append(entry)
    instruments = [{"tag": c["tag"], "id": c["id"], "center": c["center"], "text": c["text"],
                    "controllers": found_by.get(c["id"], [])}
                   for c in comps.values() if c["role"] == "instrument"]
    return {"controllers": controllers, "instruments": instruments}


def _report(source, links: dict, pages: list) -> str:
    out = [f"Instrument connections of: {source}", f"Built: {datetime.datetime.now().isoformat(timespec='seconds')}", ""]
    for page in pages:
        out.append(f"page {page['page']}: {page['counts']}")
    out += ["", f"PLC / PANELS ({len(links['controllers'])})"]
    for c in links["controllers"]:
        out.append(f"  {c['id']:<9} {c['role'].upper():<6} {c['label']:<12} at {c['center']}")
        for i in c["instruments"]:
            through = "  through " + ", ".join(f'{t["id"]} {t["kind"]} "{t["text"]}"' for t in i["through"]) \
                if i["through"] else ""
            beside = (f'  (tag beside {i["beside"]["id"]} {i["beside"]["kind"]})') if i.get("beside") else ""
            out.append(f"      -> {i['tag']:<10} {i['id']:<9} at {i['center']}{through}{beside}")
        if c["controllers"]:
            out.append(f"      linked to PLC / panel: {', '.join(c['controllers'])}")
        if not c["instruments"] and not c["controllers"]:
            out.append("      (no instrument found on its lines)")
    out += ["", f"INSTRUMENTS ({len(links['instruments'])})"]
    for i in sorted(links["instruments"], key=lambda i: i["tag"]):
        out.append(f"  {i['tag']:<10} {i['id']:<9} at {i['center']}  -> "
                   + (", ".join(i["controllers"]) if i["controllers"] else "(no PLC / panel found)"))
    return "\n".join(out) + "\n"


def export_connections(pages, source_pdf, log=print) -> dict:
    """Save "<pdf name> - instrument connections.json" ({"pages": page tables, "links": instrument_links}) and
    ".txt" (the readable report) in Working. Returns {"connections": Path, "report": Path, "links": dict}.
    `pages` may be one page table or a (nested) list of them."""
    pages = [p for p in flatten(pages) if isinstance(p, dict) and p]
    links = instrument_links({"pages": pages})
    stem = Path(source_pdf).stem
    json_path = working_path(f"{stem} - instrument connections.json")
    text_path = working_path(f"{stem} - instrument connections.txt")
    json_path.write_text(json.dumps({"source_pdf": str(source_pdf), "pages": pages, "links": links},
                                    indent=1, ensure_ascii=False), encoding="utf-8")
    text_path.write_text(_report(source_pdf, links, pages), encoding="utf-8")
    log(f"Saved: {json_path}")
    log(f"Saved: {text_path}")
    return {"connections": json_path, "report": text_path, "links": links}


# ------------------------------------------------------------------------------------------------ viewer helpers
def query_components(table, ids=None, depth: int = 1) -> list:
    """Components with these ids (a value or nested list), each with its connections `depth` levels deep:
    [{"id", "kind", "role", "tag", "text", "center", "connections": [... "via"]}]."""
    comps = {c["id"]: c for page in _pages(table) for c in page.get("components", [])}
    wanted = list(flatten(ids)) if ids else []

    def expand(c, level):
        record = {key: c.get(key) for key in ("id", "kind", "role", "tag", "text", "center")}
        if level < depth:
            record["connections"] = []
            for link in c["connections"]:
                if link["id"] in comps:
                    record["connections"].append({**expand(comps[link["id"]], level + 1), "via": link["via"]})
        return record

    return [expand(comps[i], 0) for i in wanted if i in comps]


def describe(records, indent: int = 0) -> str:
    """Readable text of query_components() results (a record or a nested list of them)."""
    if isinstance(records, (list, tuple)):
        return "\n".join(describe(r, indent) for r in records)
    pad = "    " * indent
    label = records.get("tag") or records.get("text") or ""
    via = f"  <- via {records['via']}" if records.get("via") else ""
    line = f'{pad}{records["id"]}  {records["role"]}  {records["kind"]}  "{label}"  at {records["center"]}{via}'
    children = records.get("connections")
    if children is None:
        return line
    return line + ("\n" + describe(children, indent + 1) if children else f"\n{pad}    (no connections)")
