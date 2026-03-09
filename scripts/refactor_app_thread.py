import re

path = r"c:\Users\HP\Desktop\form15cb_final\app.py"

with open(path, "r", encoding="utf-8") as f:
    lines = f.readlines()

# Find start and end
start_idx = -1
end_idx = -1
for i, line in enumerate(lines):
    if line.startswith("def _process_single_invoice(inv_id: str) -> None:"):
        start_idx = i
        break

for i in range(start_idx + 1, len(lines)):
    if line.startswith("def _generate_xml_for_invoice("):
        end_idx = i - 1
        break
    line = lines[i]

if start_idx == -1 or end_idx == -1:
    print("Could not find function bounds.", start_idx, end_idx)
    exit(1)

# Extract the old function body
old_func_lines = lines[start_idx:end_idx]
old_func_str = "".join(old_func_lines)

# We want to split out the processing part
new_worker_def = """import threading
from streamlit.runtime.scriptrunner import add_script_run_ctx

def _process_invoice_worker(inv: dict, inv_id: str, file_bytes: bytes, file_name: str, config: dict) -> None:
    try:
"""
# The old body had:
#     # Extract core fields
#     extracted: Dict[str, str] = {}
#     # Use a spinner so users know work is in progress
#     with st.spinner(f"Processing {file_name}…"):
#         try:

# We replace lines starting from `extracted: Dict[str, str] = {}` all the way down.
# We will match `    # Extract core fields`
core_split = old_func_str.find("    # Extract core fields")
if core_split == -1:
    print("Could not find core split")
    exit(1)

# Everything before core_split is the setup logic for _process_single_invoice
setup_logic = old_func_str[:core_split]
worker_logic_raw = old_func_str[core_split:]

# We need to un-indent worker_logic_raw from inside the `with st.spinner()` block
# The original has:
#     with st.spinner(f"Processing {file_name}…"):
#         try:
#             if file_name.lower().endswith(".pdf"):
#                 ...
#         except ...
worker_logic = worker_logic_raw.replace('    with st.spinner(f"Processing {file_name}…"):\n', '')
# Now unindent everything by 4 spaces
new_worker_logic_lines = []
for line in worker_logic.split('\n'):
    if line.startswith('    '):
        new_worker_logic_lines.append(line[4:])
    else:
        new_worker_logic_lines.append(line)

new_worker_logic_str = '\n'.join(new_worker_logic_lines)

# Create the new triggering function
new_trigger_func = setup_logic + """
    config = {
        "currency_short": inv["excel"].get("currency", ""),
        "exchange_rate": inv["excel"].get("exchange_rate", 0),
        "mode": mode,
        "is_gross_up": gross_up,
        "tds_deduction_date": _get_invoice_dedn_date(inv),
        "it_act_rate": _effective_it_rate(inv),
    }
    
    # Spawn thread
    t = threading.Thread(target=_process_invoice_worker, args=(inv, inv_id, file_bytes, file_name, config))
    try:
        add_script_run_ctx(t)
    except Exception:
        pass
    t.start()
"""

final_replacement = new_worker_def + "    # Extract core fields\n" + new_worker_logic_str + "\n\n" + new_trigger_func

# To fix the duplicate `config = ` in new_trigger_func, we can just replace the old config assignment
new_trigger_func = setup_logic
new_trigger_func += """
    # Spawn thread
    t = threading.Thread(target=_process_invoice_worker, args=(inv, inv_id, file_bytes, file_name, config))
    try:
        add_script_run_ctx(t)
    except Exception:
        pass
    t.start()
"""

# Now assemble
final_replacement = new_worker_def + "    # Extract core fields\n" + new_worker_logic_str + "\n\n" + new_trigger_func

with open(r"c:\Users\HP\Desktop\form15cb_final\app_threaded.py", "w", encoding="utf-8") as f:
    f.writelines(lines[:start_idx])
    f.write(final_replacement)
    f.writelines(lines[end_idx:])

print("Created app_threaded.py")
