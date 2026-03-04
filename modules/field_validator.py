def validate_pan(pan):
    if not pan: return False
    return len(pan)==10
