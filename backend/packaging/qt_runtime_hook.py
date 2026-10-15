"""Locate packaged Qt plugins independently of the build machine."""
import os
from pathlib import Path
import sys

platforms = Path(sys._MEIPASS) / "qt6/plugins/platforms"
os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = str(platforms)
