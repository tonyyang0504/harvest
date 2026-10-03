"""A stand-in for the agent CLI (`HARVEST_AGENT_BIN`) in the UI e2e suite.

harvest launches it exactly as it launches `claude -p <brief> ...` (cwd = the project directory). It reads the
brief to tell the job kind and does what a well-behaved agent would, through harvest's own service API:

- census / audit: records the candidates listed in `$HUIE2E_CENSUS_FILE` (round `stub:<n>`), once each;
- build: writes the fixture scraper module to the path named in the brief.

It prints one stream-json `result` event, like the real CLI.
"""

import json
import os
import re
import sys
from pathlib import Path


def main() -> int:
    prompt = sys.argv[sys.argv.index("-p") + 1]
    project = Path.cwd().name
    from harvest_ai import service

    if "scraper build" in prompt:
        sys.path.insert(0, str(Path(__file__).parent))
        from uisite import CAR_MODULE
        module = re.search(r"You write ONE scraper module, `([^`]+)`", prompt).group(1)
        Path(module).write_text(CAR_MODULE, encoding="utf-8")
        result = f"wrote {module}"
    else:
        path = Path(os.environ["HUIE2E_CENSUS_FILE"])
        batches = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []
        batch = batches.pop(0) if batches else []
        path.write_text(json.dumps(batches), encoding="utf-8")
        res = service.census_add(project, batch, f"stub:{len(batches)}") if batch else {"added": [], "merged": [], "rejected": []}
        result = f"census: {len(res['added'])} added, {len(res['rejected'])} rejected"
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": result, "num_turns": 1}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
