# import os
# import shutil
# # Path to tesseract executable - change this after installing Tesseract on your machine
# # Example Windows: r"C:\Program Files\Tesseract-OCR\tesseract.exe"
# TESSERACT_PATH = os.getenv("TESSERACT_PATH") or shutil.which("tesseract") or "/usr/bin/tesseract"

import os
import shutil
import platform

# Explicit fallback for Windows if PATH not configured
DEFAULT_WINDOWS_PATH = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

if platform.system() == "Windows":
    TESSERACT_PATH = (
        os.getenv("TESSERACT_PATH")
        or shutil.which("tesseract")
        or DEFAULT_WINDOWS_PATH
    )
else:
    TESSERACT_PATH = (
        os.getenv("TESSERACT_PATH")
        or shutil.which("tesseract")
        or "/usr/bin/tesseract"
    )



# folders (absolute paths to avoid confusion)
ROOT = os.getcwd()
INPUT_FOLDER = os.path.join(ROOT, "data", "input")
OUTPUT_FOLDER = os.path.join(ROOT, "data", "output")
LOG_FOLDER = os.path.join(ROOT, "data", "logs")
PROPOSED_DATE_OFFSET = 15
PARITY_UI_ENABLED = os.getenv("PARITY_UI_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}

# ensure folders exist can be called from file_manager.ensure_folders
