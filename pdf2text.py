#!/usr/bin/env python3
"""
pdf2text.py

Extract all text, images and tables from a born-digital PDF while preserving
the original reading order. Images (embedded or rasterized vector regions)
are OCR'd and included as text. Tables are detected via pdfplumber and
extracted as rows of text.

Output: a plain text file with objects in page order and an output directory
containing extracted images.

Usage:
    python pdf2text.py input.pdf --out-dir out --out-text out.txt

Dependencies: pymupdf (fitz), pdfplumber, pytesseract, pillow
You must have Tesseract installed on your system (e.g., `brew install tesseract`).
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import tempfile
from typing import List, Tuple, Dict

try:
    import fitz  # PyMuPDF
except Exception as e:
    print("PyMuPDF (fitz) is required. Install with: pip install pymupdf", file=sys.stderr)
    raise

try:
    import pdfplumber
except Exception:
    print("pdfplumber is required. Install with: pip install pdfplumber", file=sys.stderr)
    raise

try:
    import pytesseract
    from PIL import Image
except Exception:
    print("pytesseract and pillow are required. Install with: pip install pytesseract pillow", file=sys.stderr)
    raise


def ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)


def bbox_area(bbox: Tuple[float, float, float, float]) -> float:
    x0, y0, x1, y1 = bbox
    return max(0.0, x1 - x0) * max(0.0, y1 - y0)


def bbox_intersection(a: Tuple[float, float, float, float], b: Tuple[float, float, float, float]) -> float:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0 = max(ax0, bx0)
    iy0 = max(ay0, by0)
    ix1 = min(ax1, bx1)
    iy1 = min(ay1, by1)
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    return (ix1 - ix0) * (iy1 - iy0)


def bbox_overlap_fraction(a: Tuple[float, float, float, float], b: Tuple[float, float, float, float]) -> float:
    # fraction of a's area overlapped by b
    a_area = bbox_area(a)
    if a_area <= 0:
        return 0.0
    inter = bbox_intersection(a, b)
    return inter / a_area


def extract_image_from_xref(doc: fitz.Document, xref: int) -> Tuple[bytes, str]:
    """Return raw image bytes and extension for the given image xref."""
    imgdict = doc.extract_image(xref)
    if not imgdict:
        return b"", "png"
    return imgdict["image"], imgdict.get("ext", "png")


def ocr_image_bytes(img_bytes: bytes) -> str:
    im = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    txt = pytesseract.image_to_string(im)
    return txt.strip()


def ocr_pil_image(im: Image.Image) -> str:
    return pytesseract.image_to_string(im).strip()


def render_region_to_image(page: fitz.Page, bbox: Tuple[float, float, float, float], zoom: float = 2.0) -> bytes:
    mat = fitz.Matrix(zoom, zoom)
    rect = fitz.Rect(bbox)
    pix = page.get_pixmap(matrix=mat, clip=rect, alpha=False)
    return pix.tobytes("png")


def get_text_from_block(block: Dict) -> str:
    # block is from page.get_text("dict"); gather text from lines/spans
    if block.get("type") != 0:
        return ""
    lines = block.get("lines", [])
    parts = []
    for line in lines:
        for span in line.get("spans", []):
            parts.append(span.get("text", ""))
    return "\n".join(p.strip() for p in parts if p.strip())


def main(argv: List[str]):
    p = argparse.ArgumentParser(description="Extract text, images and tables from a born-digital PDF in reading order.")
    p.add_argument("input", help="input PDF file")
    p.add_argument("--out-dir", default="output", help="directory to write images and auxiliary files")
    p.add_argument("--out-text", default="output.txt", help="output plain text file")
    p.add_argument("--image-ocr", action="store_true", help="also OCR images (default: on for images); this flag keeps default behavior")
    args = p.parse_args(argv)

    pdf_path = args.input
    out_dir = args.out_dir
    out_text = args.out_text

    ensure_dir(out_dir)

    doc = fitz.open(pdf_path)
    pl = pdfplumber.open(pdf_path)

    out_lines: List[str] = []
    image_count = 0

    for pno in range(len(doc)):
        page = doc.load_page(pno)
        pl_page = pl.pages[pno]
        # out_lines.append(f"=== PAGE {pno + 1} ===")

        # detect tables via pdfplumber (gives bbox and extract())
        pl_tables = []
        try:
            pl_found = pl_page.find_tables()
            for t in pl_found:
                try:
                    rows = t.extract()
                except Exception:
                    # fallback: try pdfplumber's extract_table with the bbox
                    try:
                        rows = pl_page.extract_table(t)
                    except Exception:
                        rows = []
                pl_tables.append({
                    "bbox": tuple(t.bbox),
                    "rows": rows,
                })
        except Exception:
            # if find_tables not available or errors, ignore tables for this page
            pl_tables = []

        # get blocks from PyMuPDF in reading order
        page_dict = page.get_text("dict")
        blocks = page_dict.get("blocks", [])

        # mark which text blocks overlap tables to avoid duplicate extraction
        skipped_block_indices = set()
        for ti, t in enumerate(pl_tables):
            tbbox = t["bbox"]
            for bi, b in enumerate(blocks):
                bb = tuple(b.get("bbox", (0, 0, 0, 0)))
                if bbox_overlap_fraction(bb, tbbox) > 0.3:
                    skipped_block_indices.add(bi)

        # prepare unified items: each item has type,text,bbox,meta
        items = []

        # add tables as items
        for t in pl_tables:
            items.append({
                "type": "table",
                "bbox": tuple(t["bbox"]),
                "rows": t["rows"],
            })

        # add blocks (text/images/drawings)
        for bi, b in enumerate(blocks):
            bb = tuple(b.get("bbox", (0, 0, 0, 0)))
            btype = b.get("type")
            if bi in skipped_block_indices:
                # table takes precedence
                continue

            if btype == 0:
                txt = get_text_from_block(b)
                items.append({"type": "text", "bbox": bb, "text": txt})
            elif btype == 1:
                # image block; 'image' may be a dict, an int xref, or raw bytes depending on PyMuPDF version
                img_info = b.get("image")
                xref = None
                img_bytes = None
                if isinstance(img_info, dict):
                    xref = img_info.get("xref")
                elif isinstance(img_info, int):
                    xref = img_info
                elif isinstance(img_info, (bytes, bytearray)):
                    img_bytes = bytes(img_info)

                if xref is not None:
                    items.append({"type": "image", "bbox": bb, "xref": xref})
                elif img_bytes is not None:
                    items.append({"type": "image", "bbox": bb, "img_bytes": img_bytes})
                else:
                    # fallback: rasterize region
                    items.append({"type": "drawing", "bbox": bb})
            else:
                # other (drawing) - rasterize
                items.append({"type": "drawing", "bbox": bb})

        # sort items by vertical position (top-to-bottom), then x0
        def sort_key(it):
            x0, y0, x1, y1 = it.get("bbox", (0, 0, 0, 0))
            return (round(y0, 1), round(x0, 1))

        items.sort(key=sort_key)

        # process items in order
        for it in items:
            it_type = it["type"]
            if it_type == "text":
                if it.get("text"):
                    out_lines.append(it["text"])  # text already has internal newlines
            elif it_type == "table":
                # out_lines.append("[TABLE START]")
                rows = it.get("rows") or []
                for row in rows:
                    # row is a list of cell strings; join with tab
                    out_lines.append("\t".join(str(c) if c is not None else "" for c in row))
                # out_lines.append("[TABLE END]")
            elif it_type == "image":
                # item may carry either an xref or raw img_bytes
                img_bytes = it.get("img_bytes")
                ext = "png"
                if img_bytes is None:
                    xref = it.get("xref")
                    try:
                        img_bytes, ext = extract_image_from_xref(doc, xref)
                    except Exception:
                        img_bytes, ext = b"", "png"

                if img_bytes:
                    image_count += 1
                    img_name = f"page{pno+1}_img{image_count}.{ext}"
                    img_path = os.path.join(out_dir, img_name)
                    with open(img_path, "wb") as fh:
                        fh.write(img_bytes)
                    # out_lines.append(f"[IMAGE: {img_name}]")
                    # OCR the image and append text
                    try:
                        ocrt = ocr_image_bytes(img_bytes)
                        if ocrt:
                            out_lines.append(ocrt)
                    except Exception:
                        out_lines.append("[IMAGE OCR FAILED]")
                else:
                    # fallback rasterize
                    png = render_region_to_image(page, it.get("bbox"))
                    image_count += 1
                    img_name = f"page{pno+1}_img{image_count}.png"
                    img_path = os.path.join(out_dir, img_name)
                    with open(img_path, "wb") as fh:
                        fh.write(png)
                    # out_lines.append(f"[IMAGE: {img_name}]")
                    try:
                        out_lines.append(ocr_image_bytes(png))
                    except Exception:
                        out_lines.append("[IMAGE OCR FAILED]")
            elif it_type == "drawing":
                # rasterize the region and OCR
                try:
                    png = render_region_to_image(page, it.get("bbox"))
                    image_count += 1
                    img_name = f"page{pno+1}_draw{image_count}.png"
                    img_path = os.path.join(out_dir, img_name)
                    with open(img_path, "wb") as fh:
                        fh.write(png)
                    # out_lines.append(f"[DRAWING IMAGE: {img_name}]")
                    try:
                        out_lines.append(ocr_image_bytes(png))
                    except Exception:
                        out_lines.append("[DRAWING OCR FAILED]")
                except Exception:
                    out_lines.append("[DRAWING RASTERIZE FAILED]")
            else:
                out_lines.append(f"[UNKNOWN ITEM TYPE: {it_type}]")

        out_lines.append("")

    # write output text file
    with open(out_text, "w", encoding="utf-8") as outf:
        for line in out_lines:
            outf.write(line.rstrip())
            outf.write("\n")

    print(f"Wrote text to {out_text}. Extracted images to {out_dir}/")


if __name__ == "__main__":
    main(sys.argv[1:])
