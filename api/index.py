import sys
import os

# Make project root importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Initialize DB schema on cold start (fast, idempotent)
from database_pg import Database
_db = Database()
_db.init_db()

from app import app  # noqa: E402 — must come after sys.path setup
