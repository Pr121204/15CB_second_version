import re

FILE_PATH = "c:/Users/HP/Desktop/form15cb_final/modules/invoice_gemini_extractor.py"

with open(FILE_PATH, "r", encoding="utf-8") as f:
    text = f.read()

# 1. Add fields to IMAGE_EXTRACTION_PROMPT JSON schema
img_prompt_search = '"purpose_code": "best matching RBI purpose code (e.g., S1023, S0014, S1005)"\n}'
img_prompt_replace = '"purpose_code": "best matching RBI purpose code (e.g., S1023, S0014, S1005)",\n  "classification_evidence": ["array of short exact strings from invoice text justifying the classification"],\n  "review_required": false,\n  "classification_confidence": "high"\n}'
text = text.replace(img_prompt_search, img_prompt_replace)

# 2. Add fields to PROMPT JSON schema
prompt_search = '"purpose_code": "best matching RBI purpose code from the group above - return EXACT code (e.g. S1023, S0014, S1005) or empty string if unsure"\n}'
prompt_replace = '"purpose_code": "best matching RBI purpose code from the group above - return EXACT code (e.g. S1023, S0014, S1005) or empty string if unsure",\n  "classification_evidence": ["array of short exact strings from invoice text justifying the classification"],\n  "review_required": false,\n  "classification_confidence": "high"\n}'
text = text.replace(prompt_search, prompt_replace)

# 3. Add fields to PROMPT_COMPACT JSON schema
compact_search = '"purpose_code": ""\n}'
compact_replace = '"purpose_code": "",\n  "classification_evidence": [],\n  "review_required": false,\n  "classification_confidence": "high"\n}'
text = text.replace(compact_search, compact_replace)

classification_instructions = """
9. CLASSIFICATION & PURPOSE CODE RULES (VERY IMPORTANT):
   - Prefer the actual item/service being paid for over generic terms.
   - "software", "license", "licence", "upgrade", "subscription", "renewal" strongly suggest SOFTWARE LICENCES and likely purpose code S0902. Do not classify these as registration charges.
   - "commissioning", "installation", "startup", "engineer visit", "user site", "service ticket", "site support" strongly suggest FEES FOR TECHNICAL SERVICES (e.g., S1009), not Research & Development (R&D).
   - Do not output broad guesses like "registration charges" unless the invoice text clearly says registration fee or conference/event registration.
   - If invoice text supports multiple interpretations, return the best fit but set review_required=true.
   - If unsure, never invent. Return best guess with evidence and review_required=true.
   - If the exact purpose code is uncertain but the nature is clear, keep the best-supported nature, set review_required=true, and do not force a wrong purpose_code.

   FEW-SHOT CLASSIFICATION EXAMPLES:
   
   Example 1: Software license / upgrade
   Invoice text snippet: "Winsam 8 Upgrade", "Software upgrade", "License renewal"
   Interpretation: nature_of_remittance: "SOFTWARE LICENCES", purpose_code: "S0902"
   Reason: Payment for software rights/usage, not a registration charge.
   
   Example 2: Software subscription
   Invoice text snippet: "Annual software subscription", "Enterprise license renewal"
   Interpretation: nature_of_remittance: "SOFTWARE LICENCES", purpose_code: "S0902"
   
   Example 3: Technical commissioning at user site
   Invoice text snippet: "Total commissioning charge user site", "Engineer visit", "Service ticket number"
   Interpretation: nature_of_remittance: "FEES FOR TECHNICAL SERVICES", purpose_group: "Other Business Services", purpose_code: "S1009"
   Reason: This is technical/on-site commissioning, not R&D.
   
   Example 4: Legal services
   Invoice text snippet: "Legal services", "Attorney fee", "Contract review services"
   Interpretation: nature_of_remittance: "LEGAL SERVICES"
   
   Example 5: True registration/event fee
   Invoice text snippet: "Conference registration fee", "Delegate registration", "Seminar registration charges"
   Interpretation: Map to registration/training category. Do not confuse with software upgrade/license.
"""

# Append to IMAGE_EXTRACTION_PROMPT
img_instr_search = 'Return ONLY valid JSON with no additional text or markdown formatting."""'
img_instr_replace = classification_instructions + '\nReturn ONLY valid JSON with no additional text or markdown formatting."""'
text = text.replace(img_instr_search, img_instr_replace)

# Insert before 6. BEST EFFORT: in PROMPT
prompt_instr_search = '6. BEST EFFORT:'
# Note: we need to re-number it to fit in the text. Let's make it 6 and push BEST EFFORT to 7.
prompt_class_instr = classification_instructions.replace("9. CLASSIFICATION & PURPOSE CODE RULES", "6. CLASSIFICATION & PURPOSE CODE RULES").replace("VERY IMPORTANT):", "VERY IMPORTANT):\n")
prompt_instr_replace = prompt_class_instr + '\n\n7. BEST EFFORT:'
text = text.replace(prompt_instr_search, prompt_instr_replace)

with open(FILE_PATH, "w", encoding="utf-8") as f:
    f.write(text)

print("Prompts updated successfully!")
