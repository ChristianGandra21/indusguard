"""Exporta o contrato do FastAPI de forma determinística para o frontend.

Uso:
    python apps/api/scripts/export_openapi.py apps/web/openapi/indusguard.openapi.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from indusguard_api.main import app


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("informe o caminho de saída do OpenAPI")
    output = Path(sys.argv[1])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(app.openapi(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
