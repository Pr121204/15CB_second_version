import re

def patch_extractor():
    path = "c:/Users/HP/Desktop/form15cb_final/modules/invoice_gemini_extractor.py"
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()

    # Add invoice_id to signatures
    text = text.replace(
        "def extract_invoice_core_fields(text: str) -> Dict[str, str]:",
        "def extract_invoice_core_fields(text: str, invoice_id: str = \"\") -> Dict[str, str]:"
    )
    text = text.replace(
        "def extract_invoice_core_fields_from_image(image_path_or_bytes: Union[str, bytes, Path]) -> Dict[str, str]:",
        "def extract_invoice_core_fields_from_image(image_path_or_bytes: Union[str, bytes, Path], invoice_id: str = \"\") -> Dict[str, str]:"
    )

    # Insert classification_gemini_raw
    search_raw = '    logger.info("gemini_extract_response parsed_keys=%s", sorted(parsed.keys()))'
    insert_raw = search_raw + '''
    if invoice_id:
        logger.info(
            "classification_gemini_raw invoice_id=%s nature=%r purpose_group=%r purpose_code=%r beneficiary=%r",
            invoice_id,
            parsed.get("nature_of_remittance", ""),
            parsed.get("purpose_group", ""),
            parsed.get("purpose_code", ""),
            parsed.get("beneficiary_name", ""),
        )'''
    text = text.replace(search_raw, insert_raw)

    # Insert nature fuzzy match normalization
    search_nature = '''    if nature_suggestion:
        matched_nature = _fuzzy_match_nature(nature_suggestion)
        out["nature_of_remittance"] = matched_nature'''
    insert_nature = search_nature + '''
        if invoice_id:
            logger.info("classification_normalized invoice_id=%s nature_before=%r nature_after=%r", invoice_id, nature_suggestion, matched_nature)'''
    text = text.replace(search_nature, insert_nature)

    # Insert group fuzzy match normalization
    search_group = '''    if group_suggestion:
        matched_group = _fuzzy_match_purpose_group(group_suggestion)
        out["purpose_group"] = matched_group'''
    insert_group = search_group + '''
        if invoice_id:
            logger.info("classification_normalized invoice_id=%s purpose_group_before=%r purpose_group_after=%r", invoice_id, group_suggestion, matched_group)'''
    text = text.replace(search_group, insert_group)

    # Insert code fuzzy match normalization
    search_code = '''    if code_suggestion:
        matched_code = _fuzzy_match_purpose_code(code_suggestion, out["purpose_group"])
        out["purpose_code"] = matched_code'''
    insert_code = search_code + '''
        if invoice_id:
            logger.info("classification_normalized invoice_id=%s purpose_code_before=%r purpose_code_after=%r", invoice_id, code_suggestion, matched_code)'''
    text = text.replace(search_code, insert_code)

    # Insert fallback normalization
    search_fallback = '''        if not out["nature_of_remittance"] and fallback_nature:
            # Fuzzy-match the fallback value to get exact master label
            matched = _fuzzy_match_nature(fallback_nature)
            out["nature_of_remittance"] = matched if matched else fallback_nature'''
    insert_fallback = search_fallback + '''
            if invoice_id:
                logger.info("classification_normalized invoice_id=%s nature_before='' nature_after=%r source='keyword_fallback'", invoice_id, out["nature_of_remittance"])'''
    text = text.replace(search_fallback, insert_fallback)

    search_fallback_code = '''        if not out["purpose_code"] and fallback_code:
            # Fuzzy-match the fallback value to get exact master code
            matched = _fuzzy_match_purpose_code(fallback_code, out["purpose_group"])
            out["purpose_code"] = matched if matched and _is_valid_purpose_code(matched) else ""'''
    insert_fallback_code = search_fallback_code + '''
            if invoice_id:
                logger.info("classification_normalized invoice_id=%s purpose_code_before='' purpose_code_after=%r source='keyword_fallback'", invoice_id, out["purpose_code"])'''
    text = text.replace(search_fallback_code, insert_fallback_code)

    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def patch_state():
    path = "c:/Users/HP/Desktop/form15cb_final/modules/invoice_state.py"
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()

    # Track sources
    search_state = '    state: Dict[str, object] = {'
    insert_state = '    source_nature, source_group, source_code = "missing", "missing", "missing"\n' + search_state
    text = text.replace(search_state, insert_state)

    # Insert classifier_output logging and sources
    search_cls = '''        cls = classify_remittance(raw_text, extracted)
        if cls:
            # We want to preserve Gemini's values if they exist, else fallback to classifier'''
    insert_cls = '''        cls = classify_remittance(raw_text, extracted)
        if cls:
            logger.info(
                "classification_classifier_output invoice_id=%s nature_code=%r purpose_code=%r confidence=%s review=%s evidence=%r",
                invoice_id, cls.nature.code, cls.purpose.purpose_code, cls.confidence, cls.needs_review, cls.evidence
            )
            # We want to preserve Gemini's values if they exist, else fallback to classifier'''
    text = text.replace(search_cls, insert_cls)

    # source markers
    text = text.replace(
        '# Fallback to classifier for purpose',
        '# Fallback to classifier for purpose\n                source_group = "classifier"\n                source_code = "classifier"'
    )
    text = text.replace(
        '# Keep Gemini\'s purpose',
        '# Keep Gemini\'s purpose\n                source_group = "gemini"\n                source_code = "gemini"'
    )
    text = text.replace(
        '# Fallback to classifier for nature',
        '# Fallback to classifier for nature\n                source_nature = "classifier"'
    )
    text = text.replace(
        '# Keep Gemini\'s nature',
        '# Keep Gemini\'s nature\n                source_nature = "gemini"'
    )
    text = text.replace(
        'nature_label = str(extracted.get("nature_of_remittance", "")).strip()',
        'nature_label = str(extracted.get("nature_of_remittance", "")).strip()\n            source_nature = "gemini"'
    )
    text = text.replace(
        'purpose_code = str(extracted.get("purpose_code", "")).strip().upper()',
        'purpose_code = str(extracted.get("purpose_code", "")).strip().upper()\n            source_code = "gemini"'
    )

    # classification_final and classification_mismatch
    search_final = '    state = recompute_invoice(state)\n    logger.info('
    insert_final = '''
    # Add classification final + mismatch logging
    final_nature = form.get("NatureRemCategory", "")
    final_rev_cat = form.get("RevPurCategory", "")
    final_rev_code = form.get("RevPurCode", "")
    
    logger.info(
        "classification_final invoice_id=%s nature_text=%r purpose_group=%r purpose_code=%r NatureRemCategory=%r RevPurCategory=%r RevPurCode=%r source_nature=%s source_group=%s source_code=%s",
        invoice_id,
        extracted.get("nature_of_remittance", ""),
        form.get("_purpose_group", ""),
        form.get("_purpose_code", ""),
        final_nature,
        final_rev_cat,
        final_rev_code,
        source_nature,
        source_group,
        source_code,
    )
    
    gemini_code = str(extracted.get("purpose_code") or "").strip().upper()
    if gemini_code and final_rev_code and not final_rev_code.endswith(gemini_code):
        logger.warning(
            "classification_mismatch invoice_id=%s gemini_purpose=%r xml_purpose=%r msg='Gemini purpose overridden'",
            invoice_id, gemini_code, final_rev_code
        )
    if 'cls' in locals() and cls:
        if cls.purpose.purpose_code and final_rev_code and not final_rev_code.endswith(cls.purpose.purpose_code):
            logger.warning(
                "classification_mismatch invoice_id=%s classifier_purpose=%r xml_purpose=%r msg='Classifier purpose not used'",
                invoice_id, cls.purpose.purpose_code, final_rev_code
            )
        if cls.nature.code and final_nature and final_nature != cls.nature.code:
            logger.warning(
                "classification_mismatch invoice_id=%s classifier_nature_code=%r xml_nature_code=%r msg='Classifier nature overridden'",
                invoice_id, cls.nature.code, final_nature
            )

    state = recompute_invoice(state)
    logger.info('''
    text = text.replace(search_final, insert_final)

    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def patch_app():
    path = "c:/Users/HP/Desktop/form15cb_final/app.py"
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()

    # Pass invoice_id to extract calls
    text = text.replace(
        'extracted = extract_invoice_core_fields(text)',
        'extracted = extract_invoice_core_fields(text, invoice_id=inv["id"])'
    )
    text = text.replace(
        'page_extracted = extract_invoice_core_fields_from_image(image_bytes)',
        'page_extracted = extract_invoice_core_fields_from_image(image_bytes, invoice_id=inv["id"])'
    )
    text = text.replace(
        'text_extracted = extract_invoice_core_fields(page_ocr)',
        'text_extracted = extract_invoice_core_fields(page_ocr, invoice_id=inv["id"])'
    )
    text = text.replace(
        'extracted = extract_invoice_core_fields_from_image(file_bytes)',
        'extracted = extract_invoice_core_fields_from_image(file_bytes, invoice_id=inv["id"])'
    )

    # Note: process_invoice_file.py in scripts is missing invoice_id for local tests, 
    # but the prompt specifically asked for tracing classification mapping inside the pipeline.
    # We will patch xml_prewrite and ui_field_changed separately via another script to avoid making this one too huge.

    with open(path, "w", encoding="utf-8") as f:
        f.write(text)

if __name__ == "__main__":
    patch_extractor()
    patch_state()
    patch_app()
    print("Core traces injected.")
