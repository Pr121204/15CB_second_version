import os
from config.settings import INPUT_FOLDER, OUTPUT_FOLDER, LOG_FOLDER
def ensure_folders():
    os.makedirs(INPUT_FOLDER, exist_ok=True)
    os.makedirs(OUTPUT_FOLDER, exist_ok=True)
    os.makedirs(LOG_FOLDER, exist_ok=True)
def save_uploaded_file(uploaded_file, dest_name=None):
    ensure_folders()
    path = os.path.join(INPUT_FOLDER, dest_name or uploaded_file.name)
    with open(path, 'wb') as f:
        f.write(uploaded_file.getbuffer())
    return path
