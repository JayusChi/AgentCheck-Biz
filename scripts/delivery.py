"""D35 candidate: demo, serve, health, stop and verify-bundle."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentcheck_biz.delivery import main

if __name__ == '__main__':
    raise SystemExit(main())
