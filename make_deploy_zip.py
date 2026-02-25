"""Create a deployment zip with forward-slash paths for Linux App Service."""
import zipfile, os, sys

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "deploy.zip")

# Only include files needed for the app
INCLUDE = ["app.py", "requirements.txt", "src"]
EXCLUDE_DIRS = {"__pycache__", ".pytest_cache", ".ruff_cache", ".git", ".conda",
                "tests", "keys", "models", "uploads", "docs", "scripts",
                "samples", "infrastructure", ".github"}
EXCLUDE_EXTS = {".pyc", ".pyo"}

with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as zf:
    for item in INCLUDE:
        full = os.path.join(ROOT, item)
        if os.path.isfile(full):
            zf.write(full, item)
            print(f"  + {item}")
        elif os.path.isdir(full):
            for dirpath, dirnames, filenames in os.walk(full):
                # Filter out excluded dirs
                dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
                for f in filenames:
                    if os.path.splitext(f)[1] in EXCLUDE_EXTS:
                        continue
                    filepath = os.path.join(dirpath, f)
                    # Use forward slashes for Linux
                    arcname = os.path.relpath(filepath, ROOT).replace("\\", "/")
                    zf.write(filepath, arcname)
                    print(f"  + {arcname}")

print(f"\nCreated {OUT} ({os.path.getsize(OUT) / 1024:.1f} KB)")
