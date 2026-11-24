"""P&ID -> instrument tags and their connections to the PLC / panels.

The PDF is read into memory and opened from there (the file is only read). For every page: the vector paths are
classified, the page and every symbol are OCR'd fresh on every run (Pdf_Shapes_OCR, no cache), and the lines are
traced into the connection table (Pdf_Connections). Written to the Working folder:
  "<pdf name> - shapes.json"                  every object and OCR text per page (what the viewer draws)
  "<pdf name> - instrument connections.json"  components, nets and the PLC / panel <-> instrument links
  "<pdf name> - instrument connections.txt"   readable report: each PLC / panel with its instruments, then every
                                              instrument with its PLC / panels

Run it on its own:  python Pid_Instruments.py ["path\\to\\drawing.pdf" ...]   (no path = a file dialog opens)
"""
import json
import sys
from pathlib import Path

import pymupdf

from GUI import map_nested, working_path
from Pdf_Connections import build_connections, export_connections
from Pdf_Shapes_OCR import shapes_with_text


def extract_instrument_connections(pdf_path, log=print, audit=None) -> dict:
    """Run the whole P&ID step for one PDF (see module docstring).
    Returns {"shapes": Path, "connections": Path, "report": Path, "links": {"controllers", "instruments"}}.
    `audit(event)`, if given (Ocr_Audit.run_with_audit), is shown every step as it happens: the page OCR, every
    symbol's OCR, and at the end each page's symbols with their roles and PLC / panel links.
    `pdf_path` may be a nested list (same-shape results back)."""
    if isinstance(pdf_path, (list, tuple)):
        return map_nested(lambda one: extract_instrument_connections(one, log, audit), pdf_path)
    pdf = Path(pdf_path).expanduser().resolve()
    if not pdf.is_file():
        raise FileNotFoundError(f"PDF not found: {pdf}")
    log(f"Reading {pdf} (read-only)...")
    doc = pymupdf.open(stream=pdf.read_bytes(), filetype="pdf")
    shape_pages, tables = [], []
    try:
        for page in doc:
            number = page.number + 1
            log(f"  page {number} of {doc.page_count}: shapes and OCR...")
            found = shapes_with_text(page, page.get_drawings(extended=True), log, audit)   # OCR fresh every run
            shape_pages.append({"page": number, "rect": [round(v, 2) for v in page.rect],
                                "objects": found["objects"], "ocr_text": found["ocr_text"]})
            log(f"  page {number}: tracing connections...")
            table = build_connections(found["objects"], tuple(page.rect), number)
            log(f"    {table['counts']}")
            tables.append(table)
    finally:
        doc.close()
    shapes_path = working_path(f"{pdf.stem} - shapes.json")
    shapes_path.write_text(json.dumps({"source_pdf": str(pdf), "pages": shape_pages}, ensure_ascii=False),
                           encoding="utf-8")
    log(f"Saved: {shapes_path}")
    result = export_connections(tables, pdf, log)
    links = result["links"]
    if audit is not None:
        controllers_of = {i["id"]: i["controllers"] for i in links["instruments"]}
        instruments_of = {c["id"]: [i["tag"] for i in c["instruments"]] for c in links["controllers"]}
        for table in tables:
            audit({"type": "connections", "page": table["page"],
                   "components": [{"id": c["id"], "role": c["role"], "tag": c["tag"], "text": c["text"],
                                   "rect": c["rect"], "parts": c["parts"], "labelled_by": c.get("labelled_by"),
                                   "controllers": controllers_of.get(c["id"], []),
                                   "instruments": instruments_of.get(c["id"], [])}
                                  for c in table["components"]]})
    log(f"{len(links['controllers'])} PLC / panel(s), {len(links['instruments'])} instrument(s), "
        f"{sum(1 for i in links['instruments'] if i['controllers'])} instrument(s) linked to a PLC / panel")
    return {"shapes": shapes_path, **result}


if __name__ == "__main__":
    paths = sys.argv[1:]
    if not paths:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        paths = list(filedialog.askopenfilenames(title="P&ID PDF(s)", filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")]))
        root.destroy()
    for result in extract_instrument_connections(paths) if paths else []:
        print(result["report"])
