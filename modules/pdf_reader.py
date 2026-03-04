import pdfplumber
import io
from modules.text_normalizer import normalize_invoice_text

def extract_text_from_pdf(source):
    """Extract text from a PDF. source can be a file path (str) or BytesIO."""
    text = ""
    try:
        with pdfplumber.open(source) as pdf:
            for p in pdf.pages:
                page_text = p.extract_text()
                if page_text:
                    text += page_text + "\n"
    except Exception:
        return ""
    return normalize_invoice_text(text, keep_newlines=True)
