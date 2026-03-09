import pdfplumber
import io
from modules.text_normalizer import normalize_invoice_text

def extract_text_from_pdf(source, return_pages=False):
    """Extract text from a PDF. source can be a file path (str) or BytesIO.
       If return_pages is True, returns a list of strings (one per page).
       Otherwise, returns a single concatenated string.
    """
    pages_text = []
    try:
        with pdfplumber.open(source) as pdf:
            for p in pdf.pages:
                page_text = p.extract_text()
                if page_text:
                    pages_text.append(normalize_invoice_text(page_text, keep_newlines=True))
    except Exception:
        if return_pages:
            return []
        return ""
    
    if return_pages:
        return pages_text
    return "\n".join(pages_text)
