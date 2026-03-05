# """
# Module: zip_intake

# This module provides helper functions to ingest a ZIP archive that contains
# one Excel spreadsheet and a collection of invoice documents.  The Excel
# spreadsheet contains metadata for each invoice (such as the reference
# identifier, currency, foreign and local amounts, and the posting date for
# TDS deduction).  The functions here parse the ZIP, read the Excel using
# ``pandas``, compute derived values (like exchange rates and the final
# ``DednDateTds`` field), and produce a dictionary of invoice records
# compatible with the rest of the Form 15CB application.

# Key design points:

# * The Excel file is expected to have exactly one sheet and one row per
#   invoice.  The ``Reference`` column must match the filename (stem) of
#   each invoice in the ZIP.  This mapping allows us to look up the
#   currency, amounts and posting date for each invoice without manual
#   intervention.
# * The exchange rate is computed as ``abs(INR amount / FCY amount)`` so
#   that the resulting rate is always positive, even if the amounts are
#   negative in the Excel export.
# * Dates in the ``Posting Date`` column may be strings in a variety of
#   formats, Excel serial numbers, Python ``datetime`` objects or ``NaN``.
#   The ``parse_excel_date`` function handles these cases and returns
#   ``YYYY-MM-DD`` strings.  If parsing fails, an empty string is
#   returned and the UI will allow the user to correct it.
# * Each invoice record includes placeholders for extraction, state,
#   overrides and XML status to support the higher‑level logic in
#   ``app.py``.  These records live entirely in ``st.session_state``.

# You should not need to modify this module when extending the app unless
# the structure of the Excel changes.
# """

from __future__ import annotations

import os
import zipfile
from io import BytesIO
from typing import Dict, Iterable, List, Tuple

import pandas as pd


def parse_zip(zip_bytes: bytes) -> Tuple[str, bytes, List[Tuple[str, bytes]]]:
    """Extracts the Excel file and invoice documents from a ZIP archive.

    Args:
        zip_bytes: Raw bytes of the uploaded ZIP file.

    Returns:
        A tuple of ``(excel_name, excel_bytes, invoice_files)`` where
        ``excel_name`` is the name of the Excel file inside the ZIP,
        ``excel_bytes`` are the raw bytes of that Excel file, and
        ``invoice_files`` is a list of ``(filename, bytes)`` pairs for
        each invoice document (PDF/JPG/PNG).

    Raises:
        ValueError: If no Excel file is found in the archive.
    """
    excel_name: str | None = None
    excel_bytes: bytes | None = None
    invoice_files: List[Tuple[str, bytes]] = []
    with zipfile.ZipFile(BytesIO(zip_bytes)) as zf:
        for name in zf.namelist():
            lower = name.lower()
            if lower.endswith(".xlsx"):
                # Take the first Excel file found.  The spec says there will
                # only ever be one.
                if excel_bytes is None:
                    excel_name = name
                    excel_bytes = zf.read(name)
            elif any(lower.endswith(ext) for ext in (".pdf", ".png", ".jpg", ".jpeg")):
                invoice_files.append((name, zf.read(name)))
    if excel_bytes is None:
        raise ValueError("No Excel (.xlsx) file found in the ZIP archive.")
    return excel_name or "", excel_bytes, invoice_files


def read_excel(excel_bytes: bytes) -> pd.DataFrame:
    """Reads the Excel bytes into a pandas DataFrame.

    ``pandas`` uses ``openpyxl`` under the hood for .xlsx files.  The
    sheet is assumed to be the first sheet in the workbook.  Cells are
    read as their native Python types where possible (dates become
    ``datetime`` objects, numbers become ``float``/``int``).  Missing
    values become ``NaN``.

    Args:
        excel_bytes: Raw bytes of the Excel file.

    Returns:
        A pandas ``DataFrame`` containing the Excel data.
    """
    return pd.read_excel(BytesIO(excel_bytes), engine="openpyxl")



def parse_excel_date(value: object) -> str:
    """Converts an arbitrary Excel cell value into an ISO date string.

    Supported inputs:
    - datetime/date/Timestamp
    - Excel serial numbers (int/float) using openpyxl's from_excel
    - Strings in common formats (YYYY-MM-DD, DD-MM-YYYY, DD/MM/YYYY, DD.MM.YYYY, etc.)

    Returns YYYY-MM-DD or "" if parsing fails.
    """
    import datetime
    import pandas as pd
    from openpyxl.utils.datetime import WINDOWS_EPOCH, from_excel

    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""

    # datetime / pandas Timestamp
    if isinstance(value, (datetime.date, datetime.datetime, pd.Timestamp)):
        try:
            return value.date().isoformat()  # type: ignore[attr-defined]
        except Exception:
            try:
                return value.isoformat()  # type: ignore[call-arg]
            except Exception:
                return ""

    # Excel serial as numeric (int/float)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            dt = from_excel(value, WINDOWS_EPOCH)
            if isinstance(dt, datetime.datetime):
                return dt.date().isoformat()
            if isinstance(dt, datetime.date):
                return dt.isoformat()
        except Exception:
            pass

    # Strings
    if isinstance(value, str):
        s = value.strip()
        for fmt in (
            "%Y-%m-%d",
            "%d-%m-%Y",
            "%d/%m/%Y",
            "%d.%m.%Y",
            "%Y/%m/%d",
            "%m/%d/%Y",
        ):
            try:
                dt = datetime.datetime.strptime(s, fmt)
                return dt.date().isoformat()
            except Exception:
                continue
        try:
            dt = pd.to_datetime(s, errors="raise")
            return dt.date().isoformat()
        except Exception:
            return ""

    return ""


def _to_float(v) -> float:
    try:
        s = str(v).replace(",", "").strip()
        return float(s) if s else 0.0
    except Exception:
        return 0.0


def build_invoice_registry(df: pd.DataFrame, invoice_files: Iterable[Tuple[str, bytes]]) -> Dict[str, Dict[str, object]]:
    """Constructs the initial invoice registry from the DataFrame and files.

    Each row in the Excel data is keyed by its ``Reference`` column.  Each
    invoice file is matched by comparing its filename stem against the
    reference.  For each match, we build a record containing:

    * ``invoice_id``: the filename stem
    * ``file_name`` and ``file_bytes``: original document
    * ``excel_row``: a dictionary of the entire row for debugging
    * ``excel``: derived values (currency, amounts, exchange rate,
      posting date, parsed deduction date)
    * placeholders for overrides and processing results

    Args:
        df: DataFrame of Excel contents.
        invoice_files: List of ``(filename, bytes)`` pairs for invoice documents.

    Returns:
        A dictionary keyed by ``invoice_id`` with invoice records.
    """
    invoices: Dict[str, Dict[str, object]] = {}
    if df is None:
        return invoices
    # Normalize and index rows by Reference (strip whitespace)
    ref_to_rows: Dict[str, List[pd.Series]] = {}
    if not df.empty:
        for _, row in df.fillna("").iterrows():
            ref = str(row.get("Reference") or "").strip()
            if ref:
                ref_to_rows.setdefault(ref, []).append(row)
    for filename, fbytes in invoice_files:
        stem = os.path.splitext(os.path.basename(filename))[0]
        row_list = ref_to_rows.get(stem, [])
        row: pd.Series | None = row_list[0] if row_list else None
        # Derive values from row
        currency = ""
        fcy_amount = 0.0
        inr_amount = 0.0
        exchange_rate = 0.0
        posting_raw = None
        dedn_date = ""
        if row is not None:
            currency = str(row.get("Document currency") or "").strip().upper()
            if currency == "NAN":
                currency = ""
            fcy_amount = _to_float(row.get("Amount in doc. curr."))
            inr_amount = _to_float(row.get("Amount in local currency"))
            exchange_rate = abs(inr_amount / fcy_amount) if fcy_amount not in (0, 0.0) else 0.0
            
            # Use 'Posting Date' for 'Date of deduction of TDS' as per user request
            posting_raw = row.get("Posting Date")
            dedn_date = parse_excel_date(posting_raw)
        
        invoices[stem] = {
            "invoice_id": stem,
            "file_name": filename,
            "file_bytes": fbytes,
            "file_type": filename.split(".")[-1].lower(),
            "excel_row": row.to_dict() if row is not None else {},
            "excel": {
                "currency": currency,
                "fcy_amount": fcy_amount,
                "inr_amount": inr_amount,
                "exchange_rate": exchange_rate,
                "posting_date_raw": posting_raw,
                "dedn_date_tds": dedn_date,
            },
            # Overrides (None means inherit global)
            "mode_override": None,
            "gross_override": None,
            "it_act_rate_override": None,
            # Memoization
            "config_sig": None,
            # Processing artifacts
            "extracted": None,
            "state": None,
            "xml_bytes": None,
            # Status fields
            "status": "new",          # new | processing | processed | failed
            "error": None,
            "xml_status": "none",     # none | ok | failed
            "xml_error": None,
        }
    return invoices
