import re

path = r"c:\Users\HP\Desktop\form15cb_final\app.py"

with open(path, "r", encoding="utf-8") as f:
    content = f.read()

# Locate the render_single_invoice_page
func_start = content.find("def render_single_invoice_page() -> None:")

# The goal is to move the config block to the top of the page.
# The config block is:
config_start_marker = '                prev_mode = state["global_controls"].get("mode", MODE_TDS)'
config_end_marker = '                    state["global_controls"]["it_act_rate"] = new_it_rate'

start_idx = content.find(config_start_marker, func_start)
end_idx = content.find(config_end_marker, start_idx) + len(config_end_marker)

config_block = content[start_idx:end_idx]

# Remove the config block from its original location
# And also remove the `st.subheader("Configure and Process")` that precedes it
subheader_marker = '                st.subheader("Configure and Process")\n                \n'
full_remove_start = content.rfind(subheader_marker, func_start, start_idx)
if full_remove_start == -1:
    full_remove_start = start_idx

new_content = content[:full_remove_start] + "                pass # Config moved to top" + content[end_idx:]

# Unindent the config block
# The original config block is indented by 16 spaces (inside if inv["status"] == "new":)
# We want to insert it after `state = _get_current_state()`
# Which is indented by 4 spaces.
unindented_config = []
for line in config_block.split('\n'):
    if line.startswith(' ' * 16):
        unindented_config.append(' ' * 4 + line[16:])
    elif line.startswith(' ' * 12) and not line.strip():
        unindented_config.append('')
    else:
        # just strip leading 12 spaces if possible
        if line.startswith(' ' * 12):
            unindented_config.append(' ' * 4 + line[12:])
        else:
            unindented_config.append(line)

unindented_config_str = '\n'.join(unindented_config)

top_insertion_point = new_content.find('    st.subheader("Upload Invoice & Excel")', func_start)

# We want to add:
#    st.subheader("Configure Defaults")
#    <unindented config block>
#    _reset_invoice_states()
#    st.divider()

insertion_str = """    st.subheader("Configure Defaults")
""" + unindented_config_str + """
        from modules.invoice_calculator import recompute_invoice
        _reset_invoice_states()
        st.rerun()

    st.divider()
"""
new_content = new_content[:top_insertion_point] + insertion_str + new_content[top_insertion_point:]

with open(r"c:\Users\HP\Desktop\form15cb_final\app_refactored.py", "w", encoding="utf-8") as f:
    f.write(new_content)

print("Created app_refactored.py")
