
from modules.text_remittance_ai_helper import classify_text_field
import json

def test_rd_rule():
    text = "research and development services for the project"
    result = classify_text_field(text)
    print("Result for 'research and development':")
    print(json.dumps(result, indent=2))
    
    text2 = "r&d services fee"
    result2 = classify_text_field(text2)
    print("\nResult for 'r&d':")
    print(json.dumps(result2, indent=2))

if __name__ == "__main__":
    test_rd_rule()
