import sys
from pathlib import Path

# Make the flat project modules importable when pytest runs from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
