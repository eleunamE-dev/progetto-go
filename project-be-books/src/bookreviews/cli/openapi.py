import json
import sys
from pathlib import Path

from bookreviews.api.app import create_app
from bookreviews.config import Settings

DEFAULT_PATH = Path("docs/openapi.json")


def export(path: Path) -> None:
    schema = create_app(Settings()).openapi()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8", newline="\n")


def main() -> None:
    export(Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PATH)
