"""Synthetic report fixture shared by both harness adapters."""
import json
from pathlib import Path
import sys

package = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(package))
from renewal.placement import check

root = Path(sys.argv[1]).resolve()
path = sys.argv[2]
decision = check(root, path)
if not decision["allowed"]:
    print(json.dumps(decision))
    raise SystemExit(1)
target = root / path
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(json.dumps({"completed": True, "synthetic": True, "fixture": "synthetic", "result": "report delivered"}) + "\n")
print(json.dumps({"completed": True, "path": str(target)}))
