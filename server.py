from pathlib import Path
import sys

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
load_dotenv(ROOT / ".env")

from local_dev_mcp.server import run  # noqa: E402


if __name__ == "__main__":
    run()

