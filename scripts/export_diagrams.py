"""Export every Mermaid block in ``docs/diagrams/*.md`` to SVG and PNG (``make diagrams``).

What: finds each ```mermaid block, renders it with the Mermaid CLI and writes
``docs/diagrams/img/<file>[-n].svg|png``. The ``.md`` files stay the source of truth (GitHub renders
them directly); the images are for slides and for viewers that don't render Mermaid.
Needs: Node.js (``npx`` downloads ``@mermaid-js/mermaid-cli``) and a Chromium. Set
``PUPPETEER_EXECUTABLE_PATH`` to use an existing Chromium; as root (CI, containers) the script adds
``--no-sandbox`` automatically.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

DIAGRAMS = Path(__file__).resolve().parents[1] / "docs" / "diagrams"
BLOCK = re.compile(r"```mermaid\n(.*?)```", re.DOTALL)


def blocks(markdown: str) -> list[str]:
    """The Mermaid sources inside one Markdown file, in order."""
    return BLOCK.findall(markdown)


def puppeteer_config() -> dict[str, object]:
    config: dict[str, object] = {}
    if executable := os.environ.get("PUPPETEER_EXECUTABLE_PATH"):
        config["executablePath"] = executable
    if hasattr(os, "geteuid") and os.geteuid() == 0:  # Chromium refuses to start as root otherwise
        config["args"] = ["--no-sandbox", "--disable-setuid-sandbox"]
    return config


def render(source: str, target: Path, config: Path) -> None:
    """Render one diagram to ``target`` (the extension picks SVG or PNG)."""
    npx = shutil.which("npx")
    if npx is None:
        raise SystemExit("npx not found: install Node.js to export diagrams")
    with tempfile.NamedTemporaryFile("w", suffix=".mmd", delete=False) as handle:
        handle.write(source)
    try:
        subprocess.run(  # noqa: S603
            [npx, "--yes", "@mermaid-js/mermaid-cli", "-p", str(config), "-i", handle.name,
             "-o", str(target), "-b", "white", "-s", "2"],
            check=True, capture_output=True, text=True,
        )  # fmt: skip
    finally:
        Path(handle.name).unlink(missing_ok=True)


def main() -> int:
    out = DIAGRAMS / "img"
    out.mkdir(exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as cfg:
        json.dump(puppeteer_config(), cfg)
    count = 0
    try:
        for path in sorted(DIAGRAMS.glob("*.md")):
            found = blocks(path.read_text())
            for index, source in enumerate(found, 1):
                stem = path.stem if len(found) == 1 else f"{path.stem}-{index}"
                for extension in ("svg", "png"):
                    render(source, out / f"{stem}.{extension}", Path(cfg.name))
                count += 1
                print(f"exported {stem}")
    except subprocess.CalledProcessError as exc:
        print(exc.stderr, file=sys.stderr)
        return 1
    finally:
        Path(cfg.name).unlink(missing_ok=True)
    print(f"{count} diagrams exported to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
