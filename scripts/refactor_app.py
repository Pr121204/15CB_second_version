import os
import re

app_path = r"c:\Users\HP\Desktop\form15cb_final\app.py"
with open(app_path, "r", encoding="utf-8") as f:
    code = f.read()

# 1. Update _ensure_session_state
new_ensure = """def _ensure_session_state() -> None:
    \"\"\"Initialise keys in ``st.session_state`` that this app relies on.\"\"\"
    if "mode" not in st.session_state:
        st.session_state["mode"] = "single"
    for mode in ["single_mode", "bulk_mode"]:
        if mode not in st.session_state:
            st.session_state[mode] = {
                "invoices": {},
                "global_controls": {
                    "mode": MODE_TDS,
                    "gross_up": False,
                    "it_act_rate": IT_ACT_RATE_DEFAULT,
                },
                "ui_epoch": 0,
                "zip_context": None,
                "single_context": None,
            }

def _get_current_state() -> dict:
    mode = st.session_state.get("mode", "single")
    return st.session_state[f"{mode}_mode"]
"""

code = re.sub(r'def _ensure_session_state\(\) -> None:.*?def _validate_xml_fields', new_ensure + '\n\ndef _validate_xml_fields', code, flags=re.DOTALL)

# 2. Update helpers to use state
code = code.replace(
    'return inv.get("mode_override") or st.session_state["global_controls"].get("mode", MODE_TDS)',
    'return inv.get("mode_override") or _get_current_state()["global_controls"].get("mode", MODE_TDS)'
)
code = code.replace(
    'return bool(st.session_state["global_controls"].get("gross_up", False))',
    'return bool(_get_current_state()["global_controls"].get("gross_up", False))'
)
code = code.replace(
    'return float(st.session_state["global_controls"].get("it_act_rate", IT_ACT_RATE_DEFAULT))',
    'return float(_get_current_state()["global_controls"].get("it_act_rate", IT_ACT_RATE_DEFAULT))'
)

code = code.replace('invoices = st.session_state["invoices"]', 'invoices = _get_current_state()["invoices"]')
code = code.replace('inv = st.session_state["invoices"][inv_id]', 'inv = _get_current_state()["invoices"][inv_id]')


# 3. Refactor main() -> render_bulk_invoice_page()
code = code.replace("def main() -> None:\n    _ensure_session_state()\n    st.title(\"Form 15CB Batch Generator (ZIP-enabled)\")", "def render_bulk_invoice_page() -> None:\n    st.title(\"Form 15CB Batch Generator (ZIP-enabled)\")\n    state = _get_current_state()")

# Fix specific instances in main where st.session_state is used for state vars
# Note: we need to replace st.session_state["invoices"] with state["invoices"] ONLY inside render_bulk_invoice_page and similar
for k in ["invoices", "zip_context", "ui_epoch", "global_controls"]:
    code = code.replace(f'st.session_state["{k}"]', f'state["{k}"]')
    code = code.replace(f'st.session_state.get("{k}"', f'state.get("{k}"')


# 4. Add mode switcher and main router, and single invoice page
new_code = """
import io
import os
import re
import math
from modules.zip_intake import read_excel, _normalize_reference, _to_float, parse_excel_date

def render_mode_switcher() -> None:
    mode = st.session_state.get("mode", "single")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("📄 Process One Invoice", type="primary" if mode == "single" else "secondary", use_container_width=True):
            st.session_state["mode"] = "single"
            st.rerun()
    with col2:
        if st.button("🗂 Process Many Invoices", type="primary" if mode == "bulk" else "secondary", use_container_width=True):
            st.session_state["mode"] = "bulk"
            st.rerun()

def render_single_invoice_page() -> None:
    st.title("Form 15CB - Single Invoice")
    state = _get_current_state()
    invoices = state["invoices"]
    
    st.subheader("Step 1 – Upload Invoice & Excel")
    col1, col2 = st.columns(2)
    with col1:
        uploaded_inv = st.file_uploader("Upload Invoice", type=["pdf", "jpg", "jpeg", "png"], key="single_inv_upload")
    with col2:
        uploaded_excel = st.file_uploader("Upload Excel", type=["xlsx"], key="single_excel_upload")
        
    if uploaded_inv and uploaded_excel:
        if state.get("single_context") != uploaded_inv.name + "|" + uploaded_excel.name:
            try:
                df = read_excel(uploaded_excel.getvalue())
                
                stem = os.path.splitext(uploaded_inv.name)[0]
                norm_stem = _normalize_reference(stem)
                
                # Match row
                ref_to_rows = {}
                if not df.empty:
                    for _, row in df.fillna("").iterrows():
                        raw_ref = row.get("Reference")
                        n_ref = _normalize_reference(raw_ref)
                        if n_ref:
                            ref_to_rows.setdefault(n_ref, []).append(row)
                
                row_list = ref_to_rows.get(norm_stem, [])
                if not row_list:
                    st.error(f"Could not find matching row in Excel for invoice reference: {stem}")
                    return
                elif len(row_list) > 1:
                    st.warning(f"Multiple rows found for reference {stem}. Using the first one.")
                    
                row = row_list[0]
                
                currency = str(row.get("Document currency") or "").strip().upper()
                if currency == "NAN":
                    currency = ""
                fcy_amount = _to_float(row.get("Amount in doc. curr."))
                inr_amount = _to_float(row.get("Amount in local currency"))
                exchange_rate = abs(inr_amount / fcy_amount) if fcy_amount not in (0, 0.0) else 0.0
                posting_raw = row.get("Posting Date")
                dedn_date = parse_excel_date(posting_raw)
                
                inv_id = stem
                state["invoices"] = {
                    inv_id: {
                        "invoice_id": inv_id,
                        "file_name": uploaded_inv.name,
                        "file_bytes": uploaded_inv.getvalue(),
                        "file_type": uploaded_inv.name.split(".")[-1].lower(),
                        "excel_row": row.to_dict(),
                        "excel": {
                            "currency": currency,
                            "fcy_amount": fcy_amount,
                            "inr_amount": inr_amount,
                            "exchange_rate": exchange_rate,
                            "posting_date_raw": posting_raw,
                            "dedn_date_tds": dedn_date,
                        },
                        "mode_override": None,
                        "gross_override": None,
                        "it_act_rate_override": None,
                        "config_sig": None,
                        "extracted": None,
                        "state": None,
                        "xml_bytes": None,
                        "status": "new",
                        "error": None,
                        "xml_status": "none",
                        "xml_error": None,
                    }
                }
                state["single_context"] = uploaded_inv.name + "|" + uploaded_excel.name
                state["global_controls"] = {
                    "mode": MODE_TDS,
                    "gross_up": False,
                    "it_act_rate": IT_ACT_RATE_DEFAULT,
                }
                st.success("Files loaded and matched successfully.")
                st.rerun()
            except Exception as e:
                import traceback
                st.error(f"Error processing files: {e}\\n{traceback.format_exc()}")
                return

        invoices = state.get("invoices", {})
        if invoices:
            inv_id = list(invoices.keys())[0]
            inv = invoices[inv_id]
            
            # Auto-process if not started
            if inv["status"] == "new":
                _process_single_invoice(inv_id)
                st.rerun()
                
            if inv["status"] == "processing":
                st.info("Processing...")
            elif inv["status"] == "failed":
                st.error(f"Processing failed: {inv.get('error')}")
            elif inv["status"] == "processed":
                st.subheader("Step 2 – Review and Generate XML")
                
                ex = inv.get("excel", {})
                currency = ex.get("currency") or "—"
                exchange_rate = ex.get("exchange_rate")
                exchange_rate_str = f"{float(exchange_rate):.4f}" if exchange_rate and float(exchange_rate) > 0 else "—"
                dedn_date = ex.get("dedn_date_tds") or "—"
                with st.container(border=True):
                    st.markdown(f'''
                    <div class="excel-card">
                        <div><span class="label">Currency</span> <span class="arrow">→</span> <code>{currency}</code></div>
                        <div><span class="label">Exchange Rate</span> <span class="arrow">→</span> <code>{exchange_rate_str}</code></div>
                        <div><span class="label">Deduction Date</span> <span class="arrow">→</span> <code>{dedn_date}</code></div>
                    </div>
                    ''', unsafe_allow_html=True)
                
                # Render the invoice form for editing
                from modules.batch_form_ui import render_invoice_tab
                try:
                    old_form = dict(inv["state"].get("form", {}))
                    new_state = render_invoice_tab(inv["state"], show_header=False)
                    new_form = new_state.get("form", {})
                    
                    form = new_state.get("form", {}) if isinstance(new_state, dict) else {}
                    _snap_keys = (
                        "RateTdsSecB", "TaxLiablIt", "TaxLiablDtaa",
                        "AmtPayForgnTds", "AmtPayIndianTds", "ActlAmtTdsForgn",
                        "BasisDeterTax", "RateTdsADtaa",
                    )
                    before = tuple(str(form.get(k) or "") for k in _snap_keys)
                    new_state = recompute_invoice(new_state)
                    form_after = new_state.get("form", {}) if isinstance(new_state, dict) else {}
                    after = tuple(str(form_after.get(k) or "") for k in _snap_keys)
                    inv["state"] = new_state
                    if after != before:
                        inv["xml_bytes"] = None
                        inv["xml_status"] = "none"
                        inv["xml_error"] = None
                    state["invoices"][inv_id] = inv
                except Exception as exc:
                    st.error(f"Rendering form failed: {exc}")

                c1, c2 = st.columns(2)
                with c1:
                    if st.button("Generate XML", type="primary"):
                        _generate_xml_for_invoice(inv_id)
                        if inv.get("xml_status") == "ok":
                            st.success("XML generated successfully.")
                        else:
                            st.error(f"XML generation failed: {inv.get('xml_error')}")
                with c2:
                    if inv.get("xml_status") == "ok" and inv.get("xml_bytes"):
                        filename_stub = (inv.get("state", {}).get("extracted", {}).get("invoice_number") or inv_id).replace(" ", "_")
                        st.download_button(
                            "Download XML",
                            data=inv["xml_bytes"],
                            file_name=f"form15cb_{filename_stub}.xml",
                            mime="application/xml"
                        )

def main() -> None:
    _ensure_session_state()
    render_mode_switcher()
    mode = st.session_state.get("mode", "single")
    if mode == "single":
        render_single_invoice_page()
    else:
        render_bulk_invoice_page()

"""

# Replace main block
code = re.sub(r'if __name__ == "__main__":.*?main\(\)', 'if __name__ == "__main__":\n    main()', code, flags=re.DOTALL)
code = code.replace("if __name__ == \"__main__\":\n    main()", new_code + "\nif __name__ == \"__main__\":\n    main()\n")

# Need to fix specific session state usage that might have missed or gotten duplicated
# like in batch mode tab logic:
code = code.replace('state["invoices"][inv_id] = inv', 'state["invoices"][inv_id] = inv')

# We must be careful about `st.session_state[f"ov_mode_{inv_id}_{epoch}"]` etc. which are fine since epochs/inv_ids separate them.
code = code.replace("global_mode = state[\"global_controls\"][\"mode\"]", "global_mode = state[\"global_controls\"][\"mode\"]")

# Also need to make sure zip_intake has the _normalize_reference, _to_float exports or we just copy them into the script block. They are exported if we import them correctly. 

with open(r"c:\Users\HP\Desktop\form15cb_final\app_new.py", "w", encoding="utf-8") as f:
    f.write(code)

print("Created app_new.py")
