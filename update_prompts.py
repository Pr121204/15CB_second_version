import re

with open('c:/Users/HP/Desktop/form15cb_final/modules/invoice_gemini_extractor.py', 'r', encoding='utf-8') as f:
    text = f.read()

new_image_prompt = """IMAGE_EXTRACTION_PROMPT = \"\"\"You are a financial document analysis engine specialized in Indian Form 15CB remittance classification.

Your task is to analyze this invoice image and extract the payer (remitter), payee (beneficiary), invoice details, and determine the nature of remittance and RBI purpose code.

Accuracy is critical.
Never guess values when the evidence is weak.

IMPORTANT DEFINITIONS
Remitter

The Indian entity that is sending money abroad.

Indicators:
- Indian address
- Indian company name
- Appears in sections like: Bill To, Customer, Payer, Client, Buyer

The remitter is NOT the invoice issuer.

Beneficiary

The foreign entity receiving payment.

Indicators:
- invoice issuer, supplier, vendor, service provider
- foreign address

Nature of Remittance

A short description of what the payment is for.
Examples: Software consultancy services, Cloud subscription services, Data processing charges, Business consultancy services, Technical support services.
This must describe the actual commercial service, not generic text like "invoice payment" or "charges".

Purpose Code

The RBI purpose code corresponding to the remittance type (e.g. S0802, S0805).

Purpose Group

Higher category of the purpose code (e.g. Telecommunication, Computer & Information Services).

EXTRACTION RULES
1 REMITTER IDENTIFICATION
Remitter must be:
- Indian entity
- Paying the invoice
Never choose the supplier as remitter.
Preferred sources: Bill To, Customer, Client, Buyer.
If one entity is India and the other is foreign, then:
Indian entity = Remitter
Foreign entity = Beneficiary

2 BENEFICIARY IDENTIFICATION
Beneficiary is usually:
- Invoice issuer, Vendor, Supplier, Service provider
Usually appears in: Invoice header, From, Supplier, Vendor.

3 ADDRESS EXTRACTION
Extract address blocks exactly. Do NOT merge addresses from different parties.

4 SERVICE CLASSIFICATION
Look for commercial keywords in invoice description, line items, service description, item details.

5 PURPOSE CODE SELECTION
Choose purpose code based on service nature.

6 CONFIDENCE CONTROL
If the invoice description is vague like "professional services", "consulting", "annual fee", then return confidence: LOW and provide top 3 candidate purpose codes.

OUTPUT FORMAT

Return ONLY JSON.

{
  "remitter_name": "",
  "remitter_address": "",
  "remitter_country": "",
  "beneficiary_name": "",
  "beneficiary_address": "",
  "beneficiary_country": "",
  "invoice_number": "",
  "invoice_date": "",
  "amount": "",
  "currency": "",
  "nature_of_remittance": "",
  "purpose_code": "",
  "purpose_group": "",
  "confidence": "",
  "evidence_phrases": []
}

FEW-SHOT EXAMPLES
EXAMPLE 1 — SOFTWARE IMPLEMENTATION
INVOICE TEXT
Vendor: SAP SE
Address: Walldorf, Germany

Bill To: ABC Technologies Pvt Ltd
Bangalore, India

Description:
Software implementation services for SAP ERP deployment including consulting and system integration.

OUTPUT
{
  "remitter_name": "ABC Technologies Pvt Ltd",
  "remitter_address": "Bangalore, India",
  "remitter_country": "India",
  "beneficiary_name": "SAP SE",
  "beneficiary_address": "Walldorf, Germany",
  "beneficiary_country": "Germany",
  "invoice_number": "INV-1001",
  "invoice_date": "15/10/2023",
  "amount": "50000.00",
  "currency": "EUR",
  "nature_of_remittance": "Software consultancy and implementation services",
  "purpose_code": "S0802",
  "purpose_group": "Telecommunication, Computer & Information Services",
  "confidence": "HIGH",
  "evidence_phrases": [
    "software implementation services",
    "SAP ERP deployment",
    "system integration consulting"
  ]
}
\"\"\""""

new_prompt = """PROMPT = \"\"\"You are a financial document analysis engine specialized in Indian Form 15CB remittance classification.

Your task is to read invoice text and extract the payer (remitter), payee (beneficiary), invoice details, and determine the nature of remittance and RBI purpose code.

Accuracy is critical.
Never guess values when the evidence is weak.

IMPORTANT DEFINITIONS
Remitter

The Indian entity that is sending money abroad.

Indicators:
- Indian address
- Indian company name
- Appears in sections like: Bill To, Customer, Payer, Client, Buyer

The remitter is NOT the invoice issuer.

Beneficiary

The foreign entity receiving payment.

Indicators:
- invoice issuer, supplier, vendor, service provider
- foreign address

Nature of Remittance

A short description of what the payment is for.
Examples: Software consultancy services, Cloud subscription services, Data processing charges, Business consultancy services, Technical support services.
This must describe the actual commercial service, not generic text like "invoice payment" or "charges".

Purpose Code

The RBI purpose code corresponding to the remittance type (e.g. S0802, S0805).

Purpose Group

Higher category of the purpose code (e.g. Telecommunication, Computer & Information Services).

EXTRACTION RULES
1 REMITTER IDENTIFICATION
Remitter must be:
- Indian entity
- Paying the invoice
Never choose the supplier as remitter.
Preferred sources: Bill To, Customer, Client, Buyer.
If one entity is India and the other is foreign, then:
Indian entity = Remitter
Foreign entity = Beneficiary

2 BENEFICIARY IDENTIFICATION
Beneficiary is usually:
- Invoice issuer, Vendor, Supplier, Service provider
Usually appears in: Invoice header, From, Supplier, Vendor.

3 ADDRESS EXTRACTION
Extract address blocks exactly. Do NOT merge addresses from different parties.

4 SERVICE CLASSIFICATION
Look for commercial keywords in invoice description, line items, service description, item details.

5 PURPOSE CODE SELECTION
Choose purpose code based on service nature.

6 CONFIDENCE CONTROL
If the invoice description is vague like "professional services", "consulting", "annual fee", then return confidence: LOW and provide top 3 candidate purpose codes.

OUTPUT FORMAT

Return ONLY JSON.

{
  "remitter_name": "",
  "remitter_address": "",
  "remitter_country": "",
  "beneficiary_name": "",
  "beneficiary_address": "",
  "beneficiary_country": "",
  "invoice_number": "",
  "invoice_date": "",
  "amount": "",
  "currency": "",
  "nature_of_remittance": "",
  "purpose_code": "",
  "purpose_group": "",
  "confidence": "",
  "evidence_phrases": []
}

FEW-SHOT EXAMPLES
EXAMPLE 1 — SOFTWARE IMPLEMENTATION
INVOICE TEXT
Vendor: SAP SE
Address: Walldorf, Germany

Bill To: ABC Technologies Pvt Ltd
Bangalore, India

Description:
Software implementation services for SAP ERP deployment including consulting and system integration.

OUTPUT
{
  "remitter_name": "ABC Technologies Pvt Ltd",
  "remitter_address": "Bangalore, India",
  "remitter_country": "India",
  "beneficiary_name": "SAP SE",
  "beneficiary_address": "Walldorf, Germany",
  "beneficiary_country": "Germany",
  "invoice_number": "INV-1001",
  "invoice_date": "15/10/2023",
  "amount": "50000.00",
  "currency": "EUR",
  "nature_of_remittance": "Software consultancy and implementation services",
  "purpose_code": "S0802",
  "purpose_group": "Telecommunication, Computer & Information Services",
  "confidence": "HIGH",
  "evidence_phrases": [
    "software implementation services",
    "SAP ERP deployment",
    "system integration consulting"
  ]
}
\"\"\""""

new_compact = """PROMPT_COMPACT = \"\"\"Extract invoice fields as strict JSON only (no markdown, no explanation):
{
  "remitter_name": "",
  "remitter_address": "",
  "remitter_country": "",
  "beneficiary_name": "",
  "beneficiary_address": "",
  "beneficiary_country": "",
  "invoice_number": "",
  "invoice_date": "",
  "amount": "",
  "currency": "",
  "nature_of_remittance": "",
  "purpose_group": "",
  "purpose_code": "",
  "evidence_phrases": [],
  "confidence": ""
}
Rules:
1. Outward remittance policy: remitter is Indian payer, beneficiary is foreign payee.
2. Use invoice text only. Do not guess missing fields.
3. Return empty string for unknown fields.
4. Return valid JSON object only.
\"\"\""""

import re

# We will use string slicing/replacement based on matching variable definitions.
# Finding IMAGE_EXTRACTION_PROMPT = """ ... """
img_start = text.find('IMAGE_EXTRACTION_PROMPT = """')
img_end_match = re.search(r'Return ONLY valid JSON with no additional text or markdown formatting\."""(.*?)', text[img_start:], re.DOTALL)
if not img_end_match:
    img_end_match = re.search(r'\"\"\"', text[img_start+30:])
if img_end_match:
    img_end = img_start + (img_end_match.end() if not re.search(r'Return ONLY', text[img_start:]) else text[img_start:].find('formatting."""') + len('formatting."""'))

# Finding PROMPT = """ ... """
p_start = text.find('PROMPT = """')
p_end = text.find('"""\n\nPROMPT_COMPACT =') + 3

# Finding PROMPT_COMPACT = """ ... """
c_start = text.find('PROMPT_COMPACT = """')
c_end = text.find('"""\n\n\ndef _norm_country_token') + 3

print("img:", img_start, img_end)
print("p:", p_start, p_end)
print("c:", c_start, c_end)

if img_start != -1 and p_start != -1 and c_start != -1:
    text = text[:img_start] + new_image_prompt + text[img_end:p_start] + new_prompt + text[p_end:c_start] + new_compact + text[c_end:]

    with open('c:/Users/HP/Desktop/form15cb_final/modules/invoice_gemini_extractor.py', 'w', encoding='utf-8') as f:
        f.write(text)
    print("Prompts successfully replaced.")
else:
    print("Could not find prompt boundaries!")
