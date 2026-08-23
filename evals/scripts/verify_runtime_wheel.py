"""Falha o CI se material de avaliação cruzar a fronteira do pacote de produção."""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

FORBIDDEN_NAME_PARTS = ("evals", "golden", "fixture", "prompt_only", "parquet")
# O schema de produção guarda ``golden_digest`` para reprodutibilidade, portanto a palavra
# isolada é legítima. O conteúdo proibido é o dataset, a baseline e formatos da fixture.
FORBIDDEN_CONTENT_PARTS = ("prompt_only", "expected-paths", ".parquet")


def main(path: Path) -> int:
    with zipfile.ZipFile(path) as wheel:
        names = wheel.namelist()
        text_payload = b"\n".join(
            wheel.read(name).lower()
            for name in names
            if name.endswith((".py", ".txt", ".md", "METADATA"))
        )
    leaked = [name for name in names if any(part in name.lower() for part in FORBIDDEN_NAME_PARTS)]
    leaked.extend(
        f"conteúdo:{part}"
        for part in FORBIDDEN_CONTENT_PARTS
        if part.encode("utf-8") in text_payload
    )
    if leaked:
        raise SystemExit(f"material de avaliação encontrado na wheel: {leaked}")
    print(f"wheel de produção isolada: {len(names)} arquivos")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("uso: verify_runtime_wheel.py WHEEL")
    raise SystemExit(main(Path(sys.argv[1])))
