import csv
import os
from pathlib import Path
from tempfile import NamedTemporaryFile


def write_csv(path, fieldnames, rows):
    temporary_path = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", newline="",
                                dir=Path(path).parent, delete=False) as handle:
            temporary_path = Path(handle.name)
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
