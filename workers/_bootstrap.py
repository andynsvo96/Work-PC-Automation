"""
Helpers for worker scripts that live under the workers/ folder.
"""

import os
import sys


def ensure_project_root_on_path():
    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if root_dir not in sys.path:
        sys.path.insert(0, root_dir)
    return root_dir
