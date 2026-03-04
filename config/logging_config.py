import logging
import os
import sys


ROOT = os.getcwd()
LOG_FOLDER = os.path.join(ROOT, "data", "logs")
os.makedirs(LOG_FOLDER, exist_ok=True)
LOG_PATH = os.path.join(LOG_FOLDER, "app.log")


class SafeConsoleStreamHandler(logging.StreamHandler):
    """Console handler that never fails on Unicode output."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            super().emit(record)
        except UnicodeEncodeError:
            try:
                msg = self.format(record)
                safe = msg.encode("ascii", errors="backslashreplace").decode("ascii")
                stream = self.stream
                stream.write(safe + self.terminator)
                self.flush()
            except Exception:
                self.handleError(record)


try:
    # Preferred path for Windows consoles: emit UTF-8 and escape only when needed.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
except Exception:
    pass


logger = logging.getLogger("form15cb_demo")
logger.setLevel(logging.INFO)
logger.propagate = False

if not logger.handlers:
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    file_handler.setFormatter(formatter)

    console_handler = SafeConsoleStreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
