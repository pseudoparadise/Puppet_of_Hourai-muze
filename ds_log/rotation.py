import os

MAX_SIZE_BYTES = 10 * 1024 * 1024
MAX_BACKUPS = 1


def rotate_if_needed(log_path: str):
    try:
        if not os.path.exists(log_path):
            return
        size = os.path.getsize(log_path)
        if size < MAX_SIZE_BYTES:
            return
        backup = log_path + ".old"
        if os.path.exists(backup):
            os.remove(backup)
        os.rename(log_path, backup)
    except OSError:
        pass
