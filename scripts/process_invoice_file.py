#!/usr/bin/env python
"""Process a single invoice file (PDF or image) using the project's extractors.
Usage:
  python scripts/process_invoice_file.py path/to/file.pdf
"""
import sys
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()

if len(sys.argv) < 2:
    print("Usage: python scripts/process_invoice_file.py <path-to-file>")
    sys.exit(1)

p = Path(sys.argv[1])
if not p.exists():
    print(f"File not found: {p}")
    sys.exit(1)

from modules.invoice_gemini_extractor import extract_invoice_core_fields, extract_invoice_core_fields_from_image
from modules.pdf_reader import extract_text_from_pdf
from pdf2image import convert_from_path, convert_from_bytes

print(f"Processing: {p}")

if p.suffix.lower() == '.pdf':
    # Try to extract text first
    try:
        text = extract_text_from_pdf(str(p))
    except Exception:
        text = ""
    if text and len(text.strip()) >= 20:
        print("Using PDF text extraction path")
        out = extract_invoice_core_fields(text)
    else:
        print("Converting first PDF page to image and using image extractor")
        images = convert_from_path(str(p), dpi=300)
        buf = None
        if images:
            from io import BytesIO
            buf = BytesIO()
            images[0].save(buf, format='JPEG', quality=90)
            out = extract_invoice_core_fields_from_image(buf.getvalue())
        else:
            print("No images extracted from PDF; falling back to text extractor")
            out = extract_invoice_core_fields(text)
else:
    with open(p, 'rb') as f:
        data = f.read()
    out = extract_invoice_core_fields_from_image(data)

import json
print(json.dumps(out, indent=2))
