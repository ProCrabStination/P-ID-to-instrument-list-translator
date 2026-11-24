"""Shapes and their text on a P&ID page, from PyMuPDF's vector paths - only what is needed to find instrument tags and
their connections to the PLC / panels.

classify_drawings()  each vector path -> one object: line, polyline, arc, mixed, circle, oval, dot, small circle,
                     rectangle, diamond, triangle, quadrilateral, polygon, quad, arrowhead, filled area.
ocr_page()           OCR of the whole page (300 dpi, tiles, also turned 90 degrees) -> every text with its box.
mark_text_strokes()  paths that are strokes of lettering (CAD fonts drawn as lines) -> "part of text", so letters are
                     not taken for signal lines.
release_wired_strokes()  ... except drawing lines OCR swallowed into a text box (a solenoid box, a dashed outline).
close_line_loops()   separate lines that close into an outline (PLC diamonds drawn as two halves, dashed outlines,
                     outlines a letter touches) -> one shape "made of" those lines.
read_shape_text()    every symbol-sized closed shape: OCR of the paths INSIDE it, drawn on their own without its outline
                     (the tag in a bubble, "PLC" in a diamond); the page text inside it if that reads nothing.
shapes_with_text()   all of the above for one page; all OCR is done fresh on every run (nothing cached).
Coordinates are PDF points (1/72 inch), origin top-left, like PyMuPDF.
"""
import math
from collections import Counter, defaultdict

import cv2
import numpy as np
import pymupdf

from GUI import map_nested

OCR_DPI = 300                 # page OCR resolution
SHAPE_OCR_DPI = 600           # shape OCR resolution
TILE = 1280                   # page OCR tile size in pixels (RapidOCR shrinks bigger images, losing small text)
TILE_OVERLAP = 192            # overlap between tiles so text on a tile edge is read whole
MIN_SCORE = 0.5               # OCR results below this confidence are dropped
RETRY_SCORE = 0.85            # shape OCR less confident than this is read again at RETRY_DPI
LEFTOVER_MARGIN = 0.5         # shape OCR: ink this close (pt) to a found text box counts as read
LEFTOVER_JOIN = 0.8           # shape OCR: leftover ink this close (pt) is one blob (one word)
LEFTOVER_MIN_HEIGHT = 3.0     # shape OCR: leftover blobs this tall (pt) or more are read alone (a missed "1")
LEFTOVER_MAX_HEIGHT = 15.0    # ... and no taller (bigger is drawing, not lettering)
PAGE_FALLBACK_SCORE = 0.65    # the page OCR inside a shape is only used if shape OCR and its retry are below this
RETRY_DPI = 400           # second shape OCR resolution (a different scale often splits / reads lines differently)
SYMBOL_MIN = 6.0              # a closed shape must be at least this wide AND tall (pt) to be a symbol / get shape OCR
SYMBOL_OCR_MAX = 300.0        # closed shapes bigger than this (pt) get no shape OCR (panel borders use page text)
STROKE_SLACK = 1.5            # a path inside a text box (+ this many pt) is a stroke of that text
WIRE_TOUCH = 0.3              # a line end this close (pt) to a lettering stroke means the stroke is a drawn line
STROKE_DASH_GAP = 4.0         # a "lettering stroke" in line with a drawing line across a gap up to this (pt) is a dash
SNAP = 0.5                    # loop tracing: line ends closer than this (pt) are one point
LOOP_GAP = 4.0                # loop tracing: dash gaps up to this (pt) are bridged, so dashed outlines close
LETTER_PATH_MAX = 8.0         # loop tracing: second trace without lines this small (pt), for outlines letters touch
LOOP_MAX_SIZE = 400.0         # closed loops bigger than this (pt) are not made into shapes
LOOP_MAX_CORNERS = 100        # corners after straight runs are merged and curves simplified (CORNER_TOLERANCE)
LOOP_MAX_POINTS = 2000        # traced outlines with more points than this are skipped (time), before simplifying
CORNER_TOLERANCE = 0.25       # traced outlines: corners closer than this (pt) to the simplified outline are dropped
SPLIT_CELL = 12.0             # grid cell (pt) for finding paths that cross / touch, when cutting them at those points
LOOP_MIN_AREA = 4.0           # square points
CURVE_PIECES = 16             # a curve is kept / redrawn as this many straight pieces (1:1 look in the viewer)
CLOSED_KINDS ={"circle", "oval", "rectangle", "diamond", "triangle", "polygon", "quadrilateral", "quad"}

_ENGINE = {}


def _ocr_engine():
    """One RapidOCR engine for the whole run (installed with Docling; loading it takes a moment)."""
    if "ocr" not in _ENGINE:
        from rapidocr import RapidOCR
        _ENGINE["ocr"] = RapidOCR()
    return _ENGINE["ocr"]


# ------------------------------------------------------------------------------------------------ geometry
def _xy(point) -> tuple:
    return (float(point.x), float(point.y)) if hasattr(point, "x") else (float(point[0]), float(point[1]))


def _r(point) -> list:
    return [round(point[0], 2), round(point[1], 2)]


def _close(a, b, tol=0.25) -> bool:
    return abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol


def _length(a, b) -> float:
    return math.hypot(b[0] - a[0], b[1] - a[1])


def _diagonal(a, b) -> bool:
    angle = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) % 180
    return not (angle <= 1 or angle >= 179 or 89 <= angle <= 91)


def _hex(color) -> str:
    """(r, g, b) 0..1 -> "#rrggbb"; None -> ""."""
    if not color:
        return ""
    return "#" + "".join(f"{max(0, min(255, round(c * 255))):02x}" for c in color[:3])


def _area(points: list) -> float:
    """Signed area (shoelace)."""
    return 0.5 * sum(points[i - 1][0] * points[i][1] - points[i][0] * points[i - 1][1] for i in range(len(points)))


def _in_polygon(point, polygon) -> bool:
    """Ray casting: is the point inside the polygon?"""
    x, y = point
    inside = False
    for i in range(len(polygon)):
        (x1, y1), (x2, y2) = polygon[i - 1], polygon[i]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def _inside(point, rect, slack=0.0) -> bool:
    return rect[0] - slack <= point[0] <= rect[2] + slack and rect[1] - slack <= point[1] <= rect[3] + slack


def _rect_within(inner, outer, slack=0.0) -> bool:
    return (inner[0] >= outer[0] - slack and inner[1] >= outer[1] - slack
            and inner[2] <= outer[2] + slack and inner[3] <= outer[3] + slack)


def _point_segment_distance(p, a, b) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = dx * dx + dy * dy
    t = 0.0 if length == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / length))
    return math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy)


def _vertices(items: list) -> list:
    """Corner points of a path made of straight segments, in drawing order, without repeats."""
    points = []
    for item in items:
        for p in (item[1], item[2]):
            p = _xy(p)
            if not points or not _close(points[-1], p):
                points.append(p)
    if len(points) > 2 and _close(points[0], points[-1]):
        points.pop()
    return points


def _drop_straight_corners(points: list) -> list:
    """Remove corners where the outline just continues straight (two collinear edges = one edge)."""
    changed = True
    while changed and len(points) > 3:
        changed = False
        for i in range(len(points)):
            a, b, c = points[i - 1], points[i], points[(i + 1) % len(points)]
            ab, bc = (b[0] - a[0], b[1] - a[1]), (c[0] - b[0], c[1] - b[1])
            sine = (ab[0] * bc[1] - ab[1] * bc[0]) / max(_length(a, b) * _length(b, c), 1e-9)
            if abs(sine) < 0.02 and ab[0] * bc[0] + ab[1] * bc[1] > 0:
                points = points[:i] + points[i + 1:]
                changed = True
                break
    return points


def _polygon_kind(corners: list, filled: bool, size: float) -> str:
    """Kind of a closed straight-edged outline from its corners."""
    diagonal = [_diagonal(corners[i], corners[(i + 1) % len(corners)]) for i in range(len(corners))]
    if len(corners) == 3:
        return "arrowhead" if filled and size < 8 else "triangle"
    if len(corners) == 4 and not any(diagonal):
        return "rectangle"
    if len(corners) == 4 and all(diagonal):
        return "diamond"
    return "quadrilateral" if len(corners) == 4 else "polygon"


def _bezier_points(p1, p2, p3, p4, pieces: int = CURVE_PIECES) -> list:
    """Points along a cubic curve (its ends included), `pieces` straight pieces apart."""
    out = []
    for i in range(pieces + 1):
        t = i / pieces
        out.append(tuple((1 - t) ** 3 * p1[k] + 3 * (1 - t) ** 2 * t * p2[k] + 3 * (1 - t) * t ** 2 * p3[k]
                         + t ** 3 * p4[k] for k in (0, 1)))
    return out


def _path_segments(items: list) -> list:
    """Straight pieces of a path of lines and curves (each curve as CURVE_PIECES pieces along it)."""
    segments = []
    for item in items:
        if item[0] == "l":
            segments.append((_xy(item[1]), _xy(item[2])))
        elif item[0] == "c":
            points = _bezier_points(*(_xy(p) for p in item[1:5]))
            segments += list(zip(points, points[1:]))
    return segments


def _box_corners(item) -> list:
    """Corners of a rectangle ("re") or quad ("qu") path item, in drawing order."""
    if item[0] == "re":
        r = item[1]
        return [(r.x0, r.y0), (r.x1, r.y0), (r.x1, r.y1), (r.x0, r.y1)]
    q = item[1]
    return [_xy(q.ul), _xy(q.ur), _xy(q.lr), _xy(q.ll)]


def _path_runs(d: dict) -> list:
    """The path exactly as drawn, for the viewer: runs of joined points [[x, y], ...] (curves CURVE_PIECES pieces,
    rectangles / quads as closed runs of their own)."""
    runs = []
    for item in d.get("items") or []:
        if item[0] in ("re", "qu"):
            corners = _box_corners(item)
            runs.append(corners + corners[:1])
            continue
        if item[0] == "l":
            points = [_xy(item[1]), _xy(item[2])]
        elif item[0] == "c":
            points = _bezier_points(*(_xy(p) for p in item[1:5]))
        else:
            continue
        if runs and _close(runs[-1][-1], points[0], 0.01) and not (len(runs[-1]) > 1 and runs[-1][0] == runs[-1][-1]):
            runs[-1] += points[1:]
        else:
            runs.append(points)
    if d.get("closePath") and runs and not _close(runs[-1][0], runs[-1][-1], 0.01):
        runs[-1].append(runs[-1][0])
    return [[_r(p) for p in run] for run in runs]


# ------------------------------------------------------------------------------------------------ classify
def _classify_path(d: dict) -> dict:
    """Kind and geometry of one PyMuPDF drawing path."""
    items = d.get("items") or []
    ops = {item[0] for item in items}
    x0, y0, x1, y1 = (float(v) for v in d["rect"])
    w, h = x1 - x0, y1 - y0
    filled = d.get("fill") is not None
    obj = {"rect": [round(v, 2) for v in (x0, y0, x1, y1)], "width_pt": round(w, 2), "height_pt": round(h, 2)}
    if ops == {"c"}:
        start, end = _xy(items[0][1]), _xy(items[-1][4])
        closed = bool(d.get("closePath")) or _close(start, end, 0.5)
        if closed and max(w, h) < 6:
            kind = "dot" if filled else "small circle"
        elif closed:
            kind = "circle" if max(w, h) / max(min(w, h), 1e-6) < 1.15 else "oval"
        else:
            kind = "arc"
            obj["segments"] = [[_r(a), _r(b)] for a, b in _path_segments(items)]
        obj.update(kind=kind, closed=closed, start=_r(start), end=_r(end))
    elif ops == {"re"}:
        obj.update(kind="rectangle", closed=True)
    elif ops == {"qu"} and len(items) == 1:    # one quad: its real corners (a diamond is not its bounding box)
        corners = _box_corners(items[0])
        obj.update(kind=_polygon_kind(corners, filled, max(w, h)), closed=True, vertices=[_r(p) for p in corners])
    elif ops == {"qu"}:
        obj.update(kind="quad", closed=True)
    elif ops == {"l"}:
        segments = [(_xy(i[1]), _xy(i[2])) for i in items]
        obj["segments"] = [[_r(a), _r(b)] for a, b in segments]
        start, end = segments[0][0], segments[-1][1]
        closed = bool(d.get("closePath")) or (len(segments) > 2 and _close(start, end, 0.5))
        if closed:
            corners = _vertices(items)
            obj.update(kind=_polygon_kind(corners, filled, max(w, h)), closed=True, vertices=[_r(p) for p in corners])
        else:
            obj.update(kind="line" if len(segments) == 1 else "polyline", closed=False, start=_r(start), end=_r(end))
    else:
        boxes = [i for i in items if i[0] in ("re", "qu")]
        segments = _path_segments(items)           # the lines and curves (rectangles / quads not included)
        if len(boxes) == 1:
            # One rectangle / quad drawn together with lines in one path (a PLC diamond with its stem): the box is
            # the closed outline, the other lines are kept as pieces (Pdf_Connections makes them wires).
            corners = _box_corners(boxes[0])
            obj.update(kind=_polygon_kind(corners, filled, max(w, h)), closed=True, vertices=[_r(p) for p in corners])
            if segments:
                obj["segments"] = [[_r(a), _r(b)] for a, b in segments]
        else:
            obj.update(kind="mixed", closed=bool(d.get("closePath")))
            if segments and not boxes:  # lines + curves: keep its ends / pieces so it can carry a connection
                start, end = segments[0][0], segments[-1][1]
                obj["closed"] = obj["closed"] or (len(segments) > 2 and _close(start, end, 0.5))
                obj.update(start=_r(start), end=_r(end), segments=[[_r(a), _r(b)] for a, b in segments])
                if obj["closed"]:              # its real outline (a circle drawn as curves + short lines)
                    obj["vertices"] = [_r(a) for a, _ in segments]
    if filled and not d.get("color") and obj["kind"] in ("rectangle", "polygon", "quadrilateral", "mixed"):
        obj["kind"] = "filled area"
    return obj


def classify_drawings(drawings, page_number: int = 1) -> list:
    """Every vector path of a page (PyMuPDF get_drawings) as {"id": "p<page>-<path number>", kind, geometry,
    line_width, dashes, color, fill}. Clip / group entries are skipped. `drawings` may be a nested list of pages'
    drawings (same-shape results back)."""
    if drawings and isinstance(drawings[0], (list, tuple)):
        return [classify_drawings(one, page_number) for one in drawings]
    objects = []
    for index, d in enumerate(drawings or []):
        if d.get("type") in ("clip", "group") or not d.get("items"):
            continue
        obj = {"id": f"p{page_number}-{index}"}
        obj.update(_classify_path(d))
        obj.update(line_width=round(d.get("width") or 0, 3), dashes=d.get("dashes") or "",
                   color=_hex(d.get("color")), fill=_hex(d.get("fill")), path=_path_runs(d))
        objects.append(obj)
    return objects


# ------------------------------------------------------------------------------------------------ lettering
def mark_text_strokes(objects: list, texts: list) -> list:
    """Paths lying inside an OCR text box, centred in it and not much bigger than the lettering, are strokes of that
    text: text = the text, text_source = "part of text" (so they are not taken for lines). Closed symbol shapes are
    never marked. Returns the marked objects. `objects` may be a nested list (same-shape results back)."""
    if objects and isinstance(objects[0], (list, tuple)):
        return [mark_text_strokes(one, texts) for one in objects]
    marked = []
    for obj in objects:
        if obj["kind"] in CLOSED_KINDS and min(obj["width_pt"], obj["height_pt"]) >= SYMBOL_MIN:
            continue
        size = max(obj["width_pt"], obj["height_pt"])
        x0, y0, x1, y1 = obj["rect"]
        middle = ((x0 + x1) / 2, (y0 + y1) / 2)
        for t in texts:
            letter = max(t["rect"][3] - t["rect"][1], t["rect"][2] - t["rect"][0])
            if size <= 1.6 * letter and _rect_within(obj["rect"], t["rect"], STROKE_SLACK) and _inside(middle, t["rect"]):
                obj.update(text=t["text"], text_source="part of text")
                marked.append(obj)
                break
    return marked


def release_wired_strokes(objects: list) -> list:
    """Lettering strokes that are really drawing lines inside an OCR text box are given back as lines:
    a non-lettering line ENDS on them (the box of an "S" solenoid OCR read together with the "120V" beside it), or
    they carry a non-lettering line on in line across a dash gap (corners of a dashed diamond read as "<" ">"); and
    so do strokes joined to those, end to end or in line. Returns the released objects.
    `objects` may be a nested list of pages' objects (same-shape results back)."""
    if objects and isinstance(objects[0], (list, tuple)):
        return [release_wired_strokes(one) for one in objects]
    strokes = [o for o in objects if o.get("text_source") == "part of text" and o.get("segments")]
    if not strokes:
        return []
    cell = 8.0
    grid = defaultdict(list)    # cell -> [(stroke index, a, b)]
    for i, o in enumerate(strokes):
        for a, b in o["segments"]:
            for cx in range(math.floor((min(a[0], b[0]) - WIRE_TOUCH) / cell), math.floor((max(a[0], b[0]) + WIRE_TOUCH) / cell) + 1):
                for cy in range(math.floor((min(a[1], b[1]) - WIRE_TOUCH) / cell), math.floor((max(a[1], b[1]) + WIRE_TOUCH) / cell) + 1):
                    grid[(cx, cy)].append((i, _xy(a), _xy(b)))

    def strokes_at(p):
        return {i for i, a, b in grid.get((math.floor(p[0] / cell), math.floor(p[1] / cell)), ())
                if _point_segment_distance(p, a, b) <= WIRE_TOUCH}

    def strokes_ahead(p, u):
        found = set()
        for cx in range(math.floor((p[0] - STROKE_DASH_GAP) / cell), math.floor((p[0] + STROKE_DASH_GAP) / cell) + 1):
            for cy in range(math.floor((p[1] - STROKE_DASH_GAP) / cell), math.floor((p[1] + STROKE_DASH_GAP) / cell) + 1):
                for i, a, b in grid.get((cx, cy), ()):
                    length = _length(a, b)
                    if length < 1e-6 or abs(u[0] * (b[1] - a[1]) - u[1] * (b[0] - a[0])) / length > 0.05:
                        continue
                    for q in (a, b):
                        d = (q[0] - p[0], q[1] - p[1])
                        if 0 < d[0] * u[0] + d[1] * u[1] <= STROKE_DASH_GAP and abs(d[0] * u[1] - d[1] * u[0]) <= 0.6:
                            found.add(i)
                            break
        return found

    def line_ends(segments):
        (a, b), (c, d) = (_xy(segments[0][0]), _xy(segments[0][1])), (_xy(segments[-1][0]), _xy(segments[-1][1]))
        out = []
        for p, q in ((a, b), (d, c)):
            length = _length(p, q)
            if length > 1e-6:
                out.append((p, ((p[0] - q[0]) / length, (p[1] - q[1]) / length)))
        return out

    released, todo = set(), []

    def release(found):
        for i in found - released:
            released.add(i)
            todo.append(i)

    for o in objects:
        if o.get("text_source") == "part of text" or o["kind"] not in ("line", "polyline", "arc") or not o.get("start"):
            continue
        for p in (_xy(o["start"]), _xy(o["end"])):
            release(strokes_at(p))
        for p, u in line_ends(o["segments"]):
            release(strokes_ahead(p, u))
    while todo:
        i = todo.pop()
        for a, b in strokes[i]["segments"]:
            for p in (_xy(a), _xy(b)):
                release({j for j, c, d in grid.get((math.floor(p[0] / cell), math.floor(p[1] / cell)), ())
                         if _close(p, c, WIRE_TOUCH) or _close(p, d, WIRE_TOUCH)})
        for p, u in line_ends(strokes[i]["segments"]):
            release(strokes_ahead(p, u))
    out = []
    for i in sorted(released):
        o = strokes[i]
        o.pop("text", None)
        o.pop("text_source", None)
        out.append(o)
    return out


# ------------------------------------------------------------------------------------------------ closed loops
def _simple_loops(trace: list) -> list:
    """Split a closed trace that passes a node more than once into simple loops, in the trace's direction."""
    loops, path, where = [], [], {}
    for n in trace:
        if n in where:
            i = where[n]
            loops.append(path[i:])
            for m in path[i + 1:]:
                del where[m]
            path = path[:i + 1]
        else:
            where[n] = len(path)
            path.append(n)
    loops.append(path)
    return [loop for loop in loops if len(loop) >= 3]


def _gap_edges(edges: dict, coords: dict) -> set:
    """Edges across dash gaps: two loose ends facing each other in line, up to LOOP_GAP apart."""
    degree, neighbour = Counter(), {}
    for u, v in edges:
        degree[u] += 1
        degree[v] += 1
        neighbour[u], neighbour[v] = v, u
    loose = [n for n in degree if degree[n] == 1]
    grid = defaultdict(list)
    for n in loose:
        grid[(math.floor(coords[n][0] / LOOP_GAP), math.floor(coords[n][1] / LOOP_GAP))].append(n)

    def outward(n):
        p, q = coords[n], coords[neighbour[n]]
        length = _length(p, q) or 1e-9
        return ((p[0] - q[0]) / length, (p[1] - q[1]) / length)

    out = set()
    for n in loose:
        p, u = coords[n], outward(n)
        cx, cy = math.floor(p[0] / LOOP_GAP), math.floor(p[1] / LOOP_GAP)
        for m in (m for i in (-1, 0, 1) for j in (-1, 0, 1) for m in grid.get((cx + i, cy + j), ())):
            if m == n:
                continue
            v = outward(m)
            d = (coords[m][0] - p[0], coords[m][1] - p[1])
            if (0 < d[0] * u[0] + d[1] * u[1] <= LOOP_GAP and abs(d[0] * u[1] - d[1] * u[0]) <= 0.6
                    and u[0] * v[0] + u[1] * v[1] < -0.99):
                out.add(tuple(sorted((n, m))))
    return out


def _trace_loops(pieces: list) -> list:
    """Every enclosed area of straight pieces [(a, b, id)] -> [{"points", "members": {line ids}, "gaps"}].
    Planar face tracing: inside areas come out with positive area, the outside of each group of lines negative
    (dropped); a trace passing a point twice is split into simple loops, each judged by its own direction."""
    coords, edges = {}, defaultdict(set)

    def node(p):
        key = (round(p[0] / SNAP), round(p[1] / SNAP))
        coords.setdefault(key, (float(p[0]), float(p[1])))
        return key

    for a, b, oid in pieces:
        u, v = node(a), node(b)
        if u != v:
            edges[tuple(sorted((u, v)))].add(oid)
    gap_edges = _gap_edges(edges, coords)
    for edge in gap_edges:
        edges.setdefault(edge, set())
    neighbours = defaultdict(set)
    for u, v in edges:
        neighbours[u].add(v)
        neighbours[v].add(u)
    order = {n: sorted(neighbours[n], key=lambda m: math.atan2(coords[m][1] - coords[n][1], coords[m][0] - coords[n][0]))
             for n in neighbours}
    visited, loops = set(), []
    for u in neighbours:
        for v in neighbours[u]:
            if (u, v) in visited:
                continue
            face, a, b = [], u, v
            while (a, b) not in visited and len(face) < 10000:
                visited.add((a, b))
                face.append(a)
                ring = order[b]
                a, b = b, ring[ring.index(a) - 1]
            changed = True
            while changed and len(face) > 2:           # remove dead-end spurs (a, b, a -> a)
                changed = False
                for i in range(len(face)):
                    if face[i - 1] == face[(i + 1) % len(face)]:
                        face = [n for j, n in enumerate(face) if j not in (i, (i + 1) % len(face))]
                        changed = True
                        break
            for loop in _simple_loops(face):
                points = [coords[n] for n in loop]
                if _area(points) < LOOP_MIN_AREA:
                    continue
                sides = [tuple(sorted((loop[i - 1], loop[i]))) for i in range(len(loop))]
                loops.append({"points": points, "members": {oid for s in sides for oid in edges.get(s, ())},
                              "gaps": sum(1 for s in sides if s in gap_edges)})
    return loops


def _cut_pair(pieces: list, i: int, j: int, cuts) -> None:
    """Where pieces i and j cross, or one ends on (or overlaps along) the other, note the cut point on the piece that
    goes on through it: cuts[k] gets (position 0..1 along piece k, point)."""
    a, b = pieces[i][0], pieces[i][1]
    c, d = pieces[j][0], pieces[j][1]
    if (max(a[0], b[0]) + SNAP < min(c[0], d[0]) or max(c[0], d[0]) + SNAP < min(a[0], b[0])
            or max(a[1], b[1]) + SNAP < min(c[1], d[1]) or max(c[1], d[1]) + SNAP < min(a[1], b[1])):
        return
    r, s = (b[0] - a[0], b[1] - a[1]), (d[0] - c[0], d[1] - c[1])
    lr, ls = math.hypot(*r), math.hypot(*s)
    if lr < 1e-9 or ls < 1e-9:
        return
    # an end of one ON the other piece (T-junction, or the ends of a line lying along part of a longer line)
    for k, (p0, v, length), ends in ((i, (a, r, lr), (c, d)), (j, (c, s, ls), (a, b))):
        for p in ends:
            t = ((p[0] - p0[0]) * v[0] + (p[1] - p0[1]) * v[1]) / (length * length)
            if SNAP < t * length < length - SNAP and _point_segment_distance(p, p0, (p0[0] + v[0], p0[1] + v[1])) <= SNAP:
                cuts[k].append((t, p))
    # a proper crossing
    den = r[0] * s[1] - r[1] * s[0]
    if abs(den) > 0.01 * lr * ls:
        e = (c[0] - a[0], c[1] - a[1])
        t, u = (e[0] * s[1] - e[1] * s[0]) / den, (e[0] * r[1] - e[1] * r[0]) / den
        if SNAP < t * lr < lr - SNAP and SNAP < u * ls < ls - SNAP:
            p = (a[0] + t * r[0], a[1] + t * r[1])
            cuts[i].append((t, p))
            cuts[j].append((u, p))


def _split_pieces(pieces: list) -> list:
    """Straight pieces [(a, b, id)] cut wherever another piece crosses them, ends on them or overlaps part of them, so
    every crossing / touching point is a corner of the drawing and a line lying along part of a longer line shares
    that line's corners (the overlapping part becomes one edge carrying both ids). Same format back."""
    grid = defaultdict(list)
    for index, (a, b, _) in enumerate(pieces):
        for cx in range(math.floor((min(a[0], b[0]) - SNAP) / SPLIT_CELL), math.floor((max(a[0], b[0]) + SNAP) / SPLIT_CELL) + 1):
            for cy in range(math.floor((min(a[1], b[1]) - SNAP) / SPLIT_CELL),
                            math.floor((max(a[1], b[1]) + SNAP) / SPLIT_CELL) + 1):
                grid[(cx, cy)].append(index)
    cuts, checked = defaultdict(list), set()
    for members in grid.values():
        for n, i in enumerate(members):
            for j in members[n + 1:]:
                if (i, j) not in checked:
                    checked.add((i, j))
                    _cut_pair(pieces, i, j, cuts)
    out = []
    for index, (a, b, oid) in enumerate(pieces):
        points = [a] + [p for _, p in sorted(cuts.get(index, ()), key=lambda c: c[0])] + [b]
        kept = [points[0]]
        for p in points[1:]:
            if _length(kept[-1], p) > SNAP / 2:
                kept.append(p)
        out += [(p, q, oid) for p, q in zip(kept, kept[1:])]
    return out


def _object_pieces(o: dict) -> list:
    """Every straight piece of an object as drawn (its "path": lines, curves in CURVE_PIECES pieces, rectangle / quad
    edges): [(a, b, id)]."""
    runs = o.get("path") or ([[a, b] for a, b in o["segments"]] if o.get("segments") else [])
    return [(_xy(p), _xy(q), o["id"]) for run in runs for p, q in zip(run, run[1:])]


def _simplify_line(points: list, tolerance: float) -> list:
    """Douglas-Peucker: keep the ends and every point further than `tolerance` (pt) from the simplified line."""
    keep, stack = {0, len(points) - 1}, [(0, len(points) - 1)]
    while stack:
        i, j = stack.pop()
        far, index = 0.0, None
        for k in range(i + 1, j):
            d = _point_segment_distance(points[k], points[i], points[j])
            if d > far:
                far, index = d, k
        if index is not None and far > tolerance:
            keep.add(index)
            stack += [(i, index), (index, j)]
    return [points[k] for k in sorted(keep)]


def _simplify_ring(points: list, tolerance: float) -> list:
    """A closed outline with corners closer than `tolerance` (pt) to the simplified outline removed (a curve drawn
    as many short pieces becomes a few corners)."""
    if len(points) <= 4 or tolerance <= 0:
        return points
    far = max(range(len(points)), key=lambda k: _length(points[0], points[k]))
    first = _simplify_line(points[:far + 1], tolerance)
    second = _simplify_line(points[far:] + [points[0]], tolerance)
    ring = first[:-1] + second[:-1]
    return ring if len(ring) >= 3 else points


def _joining_paths(drawn: list, small: set, pieces: list) -> set:
    """Letter-sized open paths that are really drawing lines: BOTH ends meet (within SNAP) a line that is not
    letter-sized, e.g. a 6.6 pt side of a small PLC diamond drawn as its own line. Repeated so a chain of short lines
    joining up counts too. A letter touching or crossing an outline meets it at one end at most.
    Returns their ids. `pieces` are the cut pieces [(a, b, id)] (_split_pieces); a piece ending on another piece's
    middle has been cut there, so ends that meet are piece ends."""
    ends = {}
    for o in drawn:
        runs = o.get("path") or []
        if o["id"] in small and runs and runs[0] and runs[-1] and not o.get("closed"):
            first, last = _xy(runs[0][0]), _xy(runs[-1][-1])
            if _length(first, last) > SNAP:
                ends[o["id"]] = (first, last)
    if not ends:
        return set()
    grid, own = defaultdict(list), defaultdict(list)
    for a, b, pid in pieces:
        if pid in ends:
            own[pid].append((a, b))

    def add(oid) -> None:
        for a, b in own[oid]:
            for p in (a, b):
                grid[(math.floor(p[0] / SNAP), math.floor(p[1] / SNAP))].append((p, oid))

    def meets(p, oid) -> bool:
        cx, cy = math.floor(p[0] / SNAP), math.floor(p[1] / SNAP)
        return any(other != oid and _length(p, q) <= SNAP
                   for dx in (-1, 0, 1) for dy in (-1, 0, 1) for q, other in grid.get((cx + dx, cy + dy), ()))

    for a, b, pid in pieces:
        if pid not in small:
            for p in (a, b):
                grid[(math.floor(p[0] / SNAP), math.floor(p[1] / SNAP))].append((p, pid))
    joined, changed = set(), True
    while changed:
        changed = False
        for oid, (first, last) in ends.items():
            if oid not in joined and meets(first, oid) and meets(last, oid):
                joined.add(oid)
                add(oid)
                changed = True
    return joined


def _points_box(points) -> list:
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return [min(xs), min(ys), max(xs), max(ys)]


def close_line_loops(objects: list, page_number: int = 1) -> list:
    """Every enclosed area of the drawing becomes a shape: {"id": "p1-S<n>", "kind", "rect", "vertices",
    "made_of": [ids of the paths bounding it], "outline": "solid" / "dashed: ..."}; those paths get "part_of_shape".
    Built from every drawn path (lines, polylines, curves, the edges of rectangles / circles / polygons; not
    lettering strokes), cut wherever paths cross, end on or run along each other (_split_pieces), so an area bounded
    by parts of other shapes' lines is found too. An area that is just one closed path's own inside is not repeated.
    Traced twice: with every path, and without letter-sized paths (a "P" touching or crossing a PLC diamond's
    outline would otherwise cut it up) - except letter-sized lines joined at both ends (_joining_paths); first-trace areas inside a second-trace shape that are bounded partly by
    letter-sized paths are dropped. Returns the new shapes (also appended to `objects`).
    `objects` may be a nested list of pages' objects (same-shape results back)."""
    if objects and isinstance(objects[0], (list, tuple)):
        return [close_line_loops(one, page_number) for one in objects]
    by_id = {o["id"]: o for o in objects}
    drawn = [o for o in objects if o.get("text_source") != "part of text" and not o.get("made_of")
             and (o.get("color") or not o.get("fill"))]          # stroked paths (an unstroked fill has no edge)
    pieces = _split_pieces([p for o in drawn for p in _object_pieces(o)])
    small = {o["id"] for o in drawn if max(o["width_pt"], o["height_pt"]) <= LETTER_PATH_MAX}
    small -= _joining_paths(drawn, small, pieces)
    whole = _trace_loops([p for p in pieces if p[2] not in small])
    boxes = [(_points_box(loop["points"]), loop["members"]) for loop in whole]
    # an every-path area bounded partly by letter-sized paths is a letter-cut piece of a first-trace shape only when
    # it lies inside that shape AND shares one of its outline paths (the notch a "C" cuts in a PLC diamond); a small
    # dashed diamond inside some big area of pipes shares nothing with it and is kept
    detailed = [loop for loop in _trace_loops(pieces)
                if not (loop["members"] & small
                        and any(loop["members"] & outline and _rect_within(_points_box(loop["points"]), b, 0.5)
                                for b, outline in boxes))]
    closed = {o["id"] for o in drawn if o.get("closed")}
    own_boxes = {tuple(round(v) for v in o["rect"]) for o in drawn if o.get("closed")}
    shapes, seen = [], set()
    for loop in whole + detailed:
        x0, y0, x1, y1 = _points_box(loop["points"])
        if max(x1 - x0, y1 - y0) > LOOP_MAX_SIZE or len(loop["points"]) > LOOP_MAX_POINTS:
            continue
        if len(loop["members"]) == 1 and loop["members"] <= closed:
            continue                                   # just the inside of one closed path: that path is the shape
        # small rounded corners (each curve is CURVE_PIECES pieces) count as one or two corners, so a box with
        # rounded-corner feet (a motor) is not "too many corners"
        corners = _simplify_ring(_drop_straight_corners(loop["points"]), CORNER_TOLERANCE)
        x0, y0, x1, y1 = _points_box(corners)
        box = (round(x0), round(y0), round(x1), round(y1))
        signature = box + (len(corners),)
        if signature in seen or box in own_boxes or not (3 <= len(corners) <= LOOP_MAX_CORNERS):
            continue
        seen.add(signature)
        members = sorted(loop["members"])
        first = by_id[members[0]] if members else {}
        shape = {"id": f"p{page_number}-S{len(shapes) + 1}", "kind": _polygon_kind(corners, False, max(x1 - x0, y1 - y0)),
                 "rect": [round(v, 2) for v in (x0, y0, x1, y1)], "width_pt": round(x1 - x0, 2),
                 "height_pt": round(y1 - y0, 2), "closed": True, "vertices": [_r(p) for p in corners],
                 "made_of": members, "line_width": first.get("line_width", 0), "dashes": first.get("dashes", ""),
                 "color": first.get("color", ""), "fill": "",
                 "outline": f"dashed: {loop['gaps']} gap(s) bridged" if loop["gaps"] else "solid"}
        for oid in members:
            by_id[oid].setdefault("part_of_shape", []).append(shape["id"])
        shapes.append(shape)
    objects.extend(shapes)
    return shapes


# ------------------------------------------------------------------------------------------------ OCR
class PageImages:
    """The page rendered once for the page OCR, and the paths inside a shape drawn on their own (shape_image())."""

    def __init__(self, page, drawings=None):
        self.page = page
        self.drawings = drawings
        self._image = None
        self._scratch = None
        self._boxes = None

    def gray(self, dpi: int) -> np.ndarray:
        if self._image is None:
            zoom = dpi / 72.0
            pix = self.page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), colorspace=pymupdf.csGRAY, alpha=False)
            self._image = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width).copy()
        return self._image

    def shape_image(self, shape: dict, dpi: int) -> np.ndarray:
        """Image of what is drawn INSIDE a shape: every path with any part strictly inside its outline (not the
        shape's own paths) drawn on a blank page in a scratch PDF in memory - text-sized paths whole, long lines
        passing through only for their part inside the shape's box - rendered at `dpi` (no crop of the page).
        A shape traced from several paths does not skip its letter-sized ones (a "C" crossing a PLC diamond's
        outline is one of the paths bounding the traced area, but it is lettering to read)."""
        sx0, sy0, sx1, sy1 = shape["rect"]
        size = max(sx1 - sx0, sy1 - sy0, 1.0)
        step = max(min(sx1 - sx0, sy1 - sy0) / 10, 0.3)
        if self._boxes is None:
            self._boxes = [tuple(float(v) for v in d["rect"]) if d.get("items") and d.get("type") not in ("clip", "group")
                           else None for d in self.drawings]
            self._scratch = pymupdf.open()
        skip = _path_indices(shape)
        if shape.get("made_of"):
            skip = {i for i in skip if i >= len(self._boxes) or self._boxes[i] is None
                    or max(self._boxes[i][2] - self._boxes[i][0], self._boxes[i][3] - self._boxes[i][1]) > LETTER_PATH_MAX}
        sheet = self._scratch.new_page(width=self.page.rect.width, height=self.page.rect.height)
        try:
            pen = sheet.new_shape()
            frame, drawn = [sx0, sy0, sx1, sy1], 0
            for index, box in enumerate(self._boxes):
                if box is None or index in skip or box[2] < sx0 or box[0] > sx1 or box[3] < sy0 or box[1] > sy1:
                    continue
                d = self.drawings[index]
                pieces = [piece for item in d["items"] for piece in _item_segments(item)]
                if not any(_inside_outline(shape, p) for a, b in pieces for p in _sample(a, b, step)):
                    continue
                if max(box[2] - box[0], box[3] - box[1]) <= size:
                    for item in d["items"]:
                        if item[0] == "l":
                            pen.draw_line(item[1], item[2])
                        elif item[0] == "c":
                            pen.draw_bezier(item[1], item[2], item[3], item[4])
                        elif item[0] == "re":
                            pen.draw_rect(item[1])
                        elif item[0] == "qu":
                            pen.draw_quad(item[1])
                    frame = [min(frame[0], box[0]), min(frame[1], box[1]), max(frame[2], box[2]), max(frame[3], box[3])]
                    fill, close = ((0, 0, 0) if d.get("fill") is not None else None), bool(d.get("closePath"))
                else:
                    for a, b in pieces:
                        inside = _clip_segment(a, b, (sx0, sy0, sx1, sy1))
                        if inside:
                            pen.draw_line(inside[0], inside[1])
                    fill, close = None, False
                dashes = d.get("dashes") if d.get("dashes") not in (None, "", "[] 0", "[] 0.0") else None
                pen.finish(color=(0, 0, 0) if d.get("color") is not None else None, fill=fill,
                           width=d.get("width") or 0.72, closePath=close, even_odd=bool(d.get("even_odd")), dashes=dashes,
                           lineCap=1, lineJoin=1)   # round ends and corners: a mitred "V" point reads as "Y"
                drawn += 1
            if not drawn:
                return np.zeros((0, 0), dtype=np.uint8)
            pen.commit()
            zoom = dpi / 72.0
            area = pymupdf.Rect(frame[0] - 2, frame[1] - 2, frame[2] + 2, frame[3] + 2)
            pix = sheet.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=area, colorspace=pymupdf.csGRAY, alpha=False)
            return np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width).copy()
        finally:
            self._scratch.delete_page(sheet.number)


def _path_indices(shape: dict) -> set:
    """Drawing-path numbers that draw a shape ("p1-123" -> 123; a shape made of lines: those lines' paths)."""
    out = set()
    for oid in shape.get("made_of") or [shape["id"]]:
        tail = str(oid).rsplit("-", 1)[-1]
        if tail.isdigit():
            out.add(int(tail))
    return out


def _item_segments(item) -> list:
    """Straight pieces of one PyMuPDF path item: a line; a curve as 4 chords; a rectangle's / quad's 4 edges."""
    if item[0] == "l":
        return [(_xy(item[1]), _xy(item[2]))]
    if item[0] == "c":
        p = [_xy(q) for q in item[1:5]]
        points = [tuple((1 - t) ** 3 * p[0][k] + 3 * (1 - t) ** 2 * t * p[1][k] + 3 * (1 - t) * t ** 2 * p[2][k]
                        + t ** 3 * p[3][k] for k in (0, 1)) for t in (0, 0.25, 0.5, 0.75, 1)]
        return list(zip(points, points[1:]))
    if item[0] == "re":
        r = item[1]
        corners = [(r.x0, r.y0), (r.x1, r.y0), (r.x1, r.y1), (r.x0, r.y1)]
    elif item[0] == "qu":
        q = item[1]
        corners = [_xy(q.ul), _xy(q.ur), _xy(q.lr), _xy(q.ll)]
    else:
        return []
    return [(corners[i - 1], corners[i]) for i in range(4)]


def _sample(a, b, step: float) -> list:
    count = max(1, math.ceil(_length(a, b) / step))
    return [(a[0] + (b[0] - a[0]) * i / count, a[1] + (b[1] - a[1]) * i / count) for i in range(count + 1)]


def _inside_outline(shape: dict, p, margin: float = 0.3) -> bool:
    """Is p inside the shape's outline by more than `margin` pt (lines only ending ON the outline are out)?"""
    x0, y0, x1, y1 = shape["rect"]
    if not (x0 < p[0] < x1 and y0 < p[1] < y1):
        return False
    if shape["kind"] in ("circle", "oval"):
        a, b = max((x1 - x0) / 2, 1e-6), max((y1 - y0) / 2, 1e-6)
        return math.hypot((p[0] - (x0 + x1) / 2) / a, (p[1] - (y0 + y1) / 2) / b) < 1 - margin / min(a, b)
    if shape.get("vertices"):
        v = shape["vertices"]
        return _in_polygon(p, v) and min(_point_segment_distance(p, v[i - 1], v[i]) for i in range(len(v))) > margin
    return x0 + margin < p[0] < x1 - margin and y0 + margin < p[1] < y1 - margin


def _clip_segment(a, b, rect):
    """The part of segment a-b inside rect (Liang-Barsky), or None."""
    x0, y0, x1, y1 = rect
    dx, dy = b[0] - a[0], b[1] - a[1]
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, a[0] - x0), (dx, x1 - a[0]), (-dy, a[1] - y0), (dy, y1 - a[1])):
        if p == 0:
            if q < 0:
                return None
        else:
            t = q / p
            if p < 0:
                t0 = max(t0, t)
            else:
                t1 = min(t1, t)
            if t0 > t1:
                return None
    return (a[0] + t0 * dx, a[1] + t0 * dy), (a[0] + t1 * dx, a[1] + t1 * dy)


def _ocr_array(gray: np.ndarray) -> list:
    """RapidOCR on one greyscale image -> [(text, score, 4 corner points in pixels)]."""
    if gray.size == 0:
        return []
    # All three steps named every call: RapidOCR keeps these settings from the last call (the leftover-ink read
    # turns text finding off).
    result = _ocr_engine()(cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR), use_det=True, use_cls=True, use_rec=True)
    if result is None or getattr(result, "txts", None) is None or getattr(result, "boxes", None) is None:
        return []
    return [(str(t), float(s), np.asarray(b, dtype=float)) for t, s, b in zip(result.txts, result.scores, result.boxes)
            if str(t).strip() and float(s) >= MIN_SCORE]


def _read_leftover_ink(gray: np.ndarray, results: list, dpi: int) -> list:
    """Text the OCR's text finder skipped: RapidOCR's detector often misses a lone narrow character (the "1" under
    "EF"). Ink not inside any found box (boxes grown by LEFTOVER_MARGIN pt) is grouped into blobs; a text-sized blob
    (LEFTOVER_MIN_HEIGHT..LEFTOVER_MAX_HEIGHT pt tall, not a thin line) is cut out with a white border and given to
    the recogniser alone (no text finding). Returns extra results like _ocr_array: [(text, score, 4 corners in pixels)].
    `gray` may be a nested list of images with `results` the matching nested lists (same-shape back)."""
    if isinstance(gray, (list, tuple)):
        return [_read_leftover_ink(g, r, dpi) for g, r in zip(gray, results)]
    if gray.size == 0:
        return []
    zoom = dpi / 72.0
    ink = (gray < 128).astype(np.uint8)
    if results:
        read = np.zeros_like(ink)
        cv2.fillPoly(read, [np.round(np.asarray(box)).astype(np.int32) for _, _, box in results], 1)
        grow = max(int(LEFTOVER_MARGIN * zoom), 1) * 2 + 1
        ink[cv2.dilate(read, np.ones((grow, grow), np.uint8)) > 0] = 0
    if not ink.any():
        return []
    join = max(int(LEFTOVER_JOIN * zoom), 1) * 2 + 1          # letters of one word become one blob
    count, _, stats, _ = cv2.connectedComponentsWithStats(cv2.dilate(ink, np.ones((join, join), np.uint8)))
    found = []
    for x, y, w, h, _ in stats[1:count]:
        x, y, w, h = int(x), int(y), int(w), int(h)
        if not (LEFTOVER_MIN_HEIGHT * zoom <= h <= LEFTOVER_MAX_HEIGHT * zoom) or w < 0.2 * h:
            continue                                         # too small / too big for a letter, or a thin line
        pad = h // 2
        crop = cv2.copyMakeBorder(gray[y:y + h, x:x + w], pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=255)
        result = _ocr_engine()(cv2.cvtColor(crop, cv2.COLOR_GRAY2BGR), use_det=False, use_cls=False, use_rec=True)
        for text, score in zip(result.txts or (), result.scores or ()):
            if str(text).strip() and float(score) >= MIN_SCORE:
                corners = np.array([[x, y], [x + w, y], [x + w, y + h], [x, y + h]], dtype=float)
                found.append((str(text).strip(), float(score), corners))
    return found


def _iou(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - ix * iy
    return ix * iy / union if union > 0 else 0.0


def ocr_page(images, log=print, dpi: int = OCR_DPI) -> list:
    """OCR of the whole page: [{"text", "score", "rect" pt, "center"}]. Read in overlapping tiles, as drawn and turned
    90 degrees (vertical labels); repeats are merged keeping the most confident reading.
    `images` is a PageImages (or a PyMuPDF page); it may be a nested list (same-shape results back)."""
    if isinstance(images, (list, tuple)):
        return map_nested(lambda one: ocr_page(one, log, dpi), images)
    if not isinstance(images, PageImages):
        images = PageImages(images)
    zoom = dpi / 72.0
    img = images.gray(dpi)
    height = img.shape[0]
    found = []
    for turned in (False, True):
        work = np.ascontiguousarray(np.rot90(img, k=-1)) if turned else img
        h, w = work.shape[:2]
        step = TILE - TILE_OVERLAP
        tiles = [(top, left) for top in range(0, max(h - TILE_OVERLAP, 1), step)
                 for left in range(0, max(w - TILE_OVERLAP, 1), step)]
        log(f"    page OCR {'turned 90 degrees' if turned else 'as drawn'}: {len(tiles)} tile(s)...")
        for top, left in tiles:
            for text, score, box in _ocr_array(work[top:top + TILE, left:left + TILE]):
                xs, ys = box[:, 0] + left, box[:, 1] + top
                if turned:   # point (x', y') in the turned image = (y', height - 1 - x') in the page image
                    xs, ys = ys.copy(), (height - 1) - xs
                rect = [float(xs.min()) / zoom, float(ys.min()) / zoom, float(xs.max()) / zoom, float(ys.max()) / zoom]
                found.append({"text": text.strip(), "score": round(score, 3), "rect": [round(v, 2) for v in rect]})
    merged = []
    for item in sorted(found, key=lambda f: -f["score"]):
        if all(_iou(item["rect"], kept["rect"]) < 0.4 for kept in merged):
            merged.append(item)
    for item in merged:
        x0, y0, x1, y1 = item["rect"]
        item["center"] = [round((x0 + x1) / 2, 2), round((y0 + y1) / 2, 2)]
    merged.sort(key=lambda f: (round(f["center"][1] / 4), f["center"][0]))
    return merged


def ocr_shape_detail(images, shape, dpi: int = SHAPE_OCR_DPI) -> dict:
    """OCR of what is drawn inside one shape, with what was seen: {"text": readings in reading order ("" if none),
    "image": the greyscale image given to OCR (PageImages.shape_image), "results": [(text, score, corner points in
    image pixels)]}. `shape` may be a nested list of shapes (same-shape results back)."""
    if isinstance(shape, (list, tuple)):
        return map_nested(lambda one: ocr_shape_detail(images, one, dpi), shape)
    image = images.shape_image(shape, dpi)
    results = _ocr_array(image)
    results += _read_leftover_ink(image, results, dpi)
    line_height = 6 * dpi / 72.0
    results.sort(key=lambda r: (round(float(r[2][:, 1].mean()) / line_height), float(r[2][:, 0].min())))
    return {"text": " ".join(text.strip() for text, _, _ in results), "image": image,
            "results": [(text.strip(), score, box.tolist()) for text, score, box in results]}


def ocr_shape(images, shape, dpi: int = SHAPE_OCR_DPI) -> str:
    """OCR of what is drawn inside one shape (PageImages.shape_image), in reading order; "" if none.
    `shape` may be a nested list of shapes (same-shape results back)."""
    if isinstance(shape, (list, tuple)):
        return map_nested(lambda one: ocr_shape(images, one, dpi), shape)
    return ocr_shape_detail(images, shape, dpi)["text"]


def _page_crop(images, rect, margin: float = 8.0, dpi: int = OCR_DPI) -> dict:
    """The page image around a box (pt), for the audit window only (not used for OCR):
    {"image", "origin": its top-left corner in page-image pixels, "dpi"}."""
    page = images.gray(dpi)
    zoom = dpi / 72.0
    x0, y0 = max(int((rect[0] - margin) * zoom), 0), max(int((rect[1] - margin) * zoom), 0)
    x1, y1 = min(int((rect[2] + margin) * zoom) + 1, page.shape[1]), min(int((rect[3] + margin) * zoom) + 1, page.shape[0])
    return {"image": page[y0:y1, x0:x1].copy(), "origin": (x0, y0), "dpi": dpi}


def _is_symbol(obj: dict) -> bool:
    """Closed shape big enough to hold a tag (SYMBOL_MIN) and not bigger than SYMBOL_OCR_MAX."""
    return (obj.get("closed") and obj["kind"] not in ("dot", "small circle", "filled area")
            and min(obj["width_pt"], obj["height_pt"]) >= SYMBOL_MIN
            and max(obj["width_pt"], obj["height_pt"]) <= SYMBOL_OCR_MAX)


def _lowest_score(scores) -> float:
    """Confidence of a reading made of several OCR pieces: its least confident piece (0 if none)."""
    return min((float(s) for s in scores), default=0.0)


def read_shape_text(objects: list, texts: list, images=None, log=print, audit=None) -> list:
    """Text of every symbol-sized closed shape (_is_symbol). Readings: OCR of the paths inside it drawn without its
    outline ("shape OCR", read fresh every run); when that is below RETRY_SCORE, the same at RETRY_DPI; and the page
    texts whose centre is inside it ("inside") - only if the shape OCR and its retry are both below
    PAGE_FALLBACK_SCORE. A reading that is a tag / PLC / panel wins, then the most confident.
    Changes the shapes in place and returns them. `audit(event)`, if given, is called for every
    symbol as it is read, with everything the decision was made from (see Ocr_Audit).
    `objects` may be a nested list (same-shape back)."""
    if objects and isinstance(objects[0], (list, tuple)):
        return [read_shape_text(one, texts, images, log, audit) for one in objects]
    from Pdf_Connections import classify_text, symbol_role   # here, not at the top: Pdf_Connections imports this module
    symbols = [o for o in objects if _is_symbol(o) and o.get("text_source") != "part of text"]
    log(f"    shape OCR: {len(symbols)} symbol-sized closed shape(s) to read...")
    for count, obj in enumerate(symbols, start=1):
        detail = ocr_shape_detail(images, obj) if images is not None else {"text": "", "image": None, "results": []}
        if count % 50 == 0:
            log(f"      {count} / {len(symbols)}")
        own = detail["text"]
        readings = [{"text": own, "score": _lowest_score(s for _, s, _ in detail["results"]), "source": "shape OCR"}]
        retry = None
        if images is not None and readings[0]["score"] < RETRY_SCORE:
            # Unsure: read the same drawing again at another resolution (RapidOCR finds the lines afresh).
            retry = ocr_shape_detail(images, obj, RETRY_DPI)
            readings.append({"text": retry["text"], "score": _lowest_score(s for _, s, _ in retry["results"]),
                             "source": f"shape OCR at {RETRY_DPI} dpi"})
        inside = [t for t in texts if _inside(t["center"], obj["rect"])
                  and (not obj.get("vertices") or _in_polygon(t["center"], obj["vertices"]))]
        inside.sort(key=lambda t: (round(t["center"][1] / 6), t["center"][0]))   # top line first, left to right
        page_text = " ".join(t["text"] for t in inside)
        if max(r["score"] for r in readings) < PAGE_FALLBACK_SCORE:
            # Only when the shape OCR and its retry are both unsure (or read nothing) may the page OCR be used.
            readings.append({"text": page_text, "score": _lowest_score(t["score"] for t in inside), "source": "inside"})
        # A reading that is a tag / PLC / panel beats one that is not (tags are what this tool is for); among those,
        # the most confident (each reading scored by its least confident piece); a tie keeps the earlier one.
        best = max((r for r in readings if r["text"]),
                   key=lambda r: (classify_text(r["text"])[0] != "other", r["score"]), default=None)
        obj.update(text=best["text"] if best else "", text_source=best["source"] if best else "")
        if audit is not None:
            role, tag = symbol_role(obj["text"], obj["kind"])
            audit({"type": "shape", "page": int(str(obj["id"]).split("-")[0][1:] or 1), "id": obj["id"],
                   "kind": obj["kind"], "rect": obj["rect"], "vertices": obj.get("vertices"),
                   "made_of": obj.get("made_of"), "page_image": _page_crop(images, obj["rect"]) if images is not None else None,
                   "ocr_image": detail["image"], "ocr_results": detail["results"], "shape_text": own,
                   "retry_results": retry["results"] if retry else None, "retry_dpi": RETRY_DPI,
                   "page_texts": [(t["text"], t["score"]) for t in inside], "text": obj["text"],
                   "source": obj["text_source"], "role": role, "tag": tag, "count": count, "total": len(symbols)})
    return symbols


# ------------------------------------------------------------------------------------------------ one page
def shapes_with_text(page, drawings: list, log=print, audit=None) -> dict:
    """{"objects": every classified object (symbols with their text), "ocr_text": every page OCR text}.
    classify -> page OCR -> mark lettering -> release drawing lines from text boxes -> close line loops -> shape OCR.
    Everything is read fresh on every run (no OCR cache). `audit(event)`, if given, gets the page image with every
    page OCR text and symbol outline once the shapes are found, then every symbol as it is read (see Ocr_Audit).
    `page` may be a nested list of pages with `drawings` the matching nested list (same-shape results back)."""
    if isinstance(page, (list, tuple)):
        return [shapes_with_text(p, d, log, audit) for p, d in zip(page, drawings)]
    number = page.number + 1
    objects = classify_drawings(drawings, number)
    log(f"    {len(objects)} path(s)")
    images = PageImages(page, drawings)
    texts = ocr_page(images, log)
    log(f"    page OCR found {len(texts)} text(s)")
    strokes = mark_text_strokes(objects, texts)
    released = release_wired_strokes(objects)
    log(f"    {len(strokes) - len(released)} lettering stroke(s); {len(released)} drawing line(s) given back from text boxes")
    loops = close_line_loops(objects, number)
    log(f"    {len(loops)} closed shape(s) made of separate lines")
    if audit is not None:
        audit({"type": "page", "page": number, "image": images.gray(OCR_DPI), "dpi": OCR_DPI,
               "rect": [float(v) for v in page.rect], "texts": texts,
               "symbols": [{"id": o["id"], "kind": o["kind"], "rect": o["rect"], "vertices": o.get("vertices")}
                           for o in objects if _is_symbol(o) and o.get("text_source") != "part of text"],
               "lettering": [o["rect"] for o in objects if o.get("text_source") == "part of text"]})
    read_shape_text(objects, texts, images, log, audit)
    return {"objects": objects, "ocr_text": texts}
