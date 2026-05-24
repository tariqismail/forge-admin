import json
import os
import tempfile
from pathlib import Path


def read_json(filepath: Path) -> tuple[dict | list | None, str | None]:
    if not filepath.exists():
        return None, None
    try:
        data = json.loads(filepath.read_text())
        return data, None
    except json.JSONDecodeError as e:
        return None, f"Line {e.lineno}: {e.msg}"


def write_json(filepath: Path, data) -> str | None:
    try:
        content = json.dumps(data, indent=2, ensure_ascii=False)
        fd, tmp_path = tempfile.mkstemp(dir=filepath.parent, suffix=".tmp")
        try:
            os.write(fd, content.encode("utf-8"))
            os.close(fd)
            os.rename(tmp_path, filepath)
        except Exception:
            os.close(fd) if not os.get_inheritable(fd) else None
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise
        return None
    except Exception as e:
        return str(e)


def touch_sentinel(sentinel_path: Path):
    sentinel_path.touch()
