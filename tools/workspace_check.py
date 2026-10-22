"""Checkout-local entry point for :mod:`claude_launcher.workspace_check`.

External workspaces use ``claunch workspace check``. Repository gates keep
this entry point so they measure the current checkout with no installation.
"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from claude_launcher.workspace_check import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
