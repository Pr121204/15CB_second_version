import re

def patch_app_ui_logs():
    path = "c:/Users/HP/Desktop/form15cb_final/app.py"
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()

    search = '''                    try:
                        new_state = render_invoice_tab(inv["state"], show_header=False)'''
    
    insert = '''                    try:
                        old_form = dict(inv["state"].get("form", {}))
                        new_state = render_invoice_tab(inv["state"], show_header=False)
                        new_form = new_state.get("form", {})
                        for k in ["CountryRemMadeSecb", "NatureRemCategory", "RevPurCategory", "RevPurCode", "RateTdsADtaa", "BasisDeterTax", "TaxPayGrossSecb"]:
                            if k in new_form and k in old_form and new_form[k] != old_form[k]:
                                logger.info("ui_field_changed invoice_id=%s field=%s old=%r new=%r", inv_id, k, old_form[k], new_form[k])'''
    
    if "logger.info(\"ui_field_changed" not in text:
        text = text.replace(search, insert)
    
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)

if __name__ == "__main__":
    patch_app_ui_logs()
    print("UI traces injected.")
