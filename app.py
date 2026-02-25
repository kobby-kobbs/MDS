

"""
Thin entry point for Azure App Service.

Gunicorn imports ``app:app`` which resolves to the FastAPI instance
exported by ``mds.main``.  The ``PYTHONPATH`` App Setting must include
``/home/site/wwwroot/src`` so the ``mds`` package is importable.
"""

import os
import sys

_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_dir, "src"))

from mds.main import app  # noqa: F401
