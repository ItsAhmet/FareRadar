import os
import sys
from pathlib import Path

# Pin the home airports before any project module is imported. config reads
# AVELO_HOME_AIRPORTS (and, via load_dotenv, the owner's .env) at import time, and
# GetawayQuery.origin / mock_data.PRIMARY_HOME bind the result at import, so this
# must be an environment-level pin: monkeypatching config.PRIMARY_HOME later would
# not reach them. load_dotenv() never overrides a variable that is already set.
os.environ["AVELO_HOME_AIRPORTS"] = "ILG,PHL,BWI"

# Make the flat project modules importable when pytest runs from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
