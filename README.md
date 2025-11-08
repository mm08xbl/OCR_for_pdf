# pdf2text

Simple utility to extract text, images and tables from born-digital PDFs while preserving reading order.

Features
- Extracts the PDF text layer (with page order preserved).
- Extracts embedded images and rasterizes drawing regions; runs OCR on images to convert them into text.
- Uses pdfplumber to detect and extract tables and outputs their rows as tab-separated lines.

Requirements
- Python 3.8+
- System: Tesseract OCR installed (macOS: `brew install tesseract`)
- Python packages: see `requirements.txt` (install with `pip install -r requirements.txt`).

Usage
```
python pdf2text.py input.pdf --out-dir ./out --out-text ./out/result.txt
```

Result
- A text file with the page content (text, table rows, OCR output from images) in an order approximating the original document order.
- Extracted image files placed in the `--out-dir` directory.

Notes
- This script assumes born-digital PDFs. It will rasterize non-image drawing blocks and OCR them as a fallback.
- Table detection uses pdfplumber; if fine-grained table extraction is needed, consider running a dedicated table extractor (Camelot or Tabula) and merging results.
