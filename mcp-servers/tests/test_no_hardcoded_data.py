"""Guards the AkashX integration against hard-coded data sources.

The assistant must work with any AkashX server: no dataset, semantic model id, server address
or database may be hard-coded in the AkashX MCP servers or their LibreChat / Docker config.

  python -m pytest mcp-servers/tests -q        (from the repo root)
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG = [ROOT / "librechat.yaml", ROOT / "docker-compose.override.yml", ROOT / ".env.example",
          ROOT / "mcp-servers" / "AKASHX_ASSISTANT_PROMPT.md", ROOT / "AKASHX_DEPLOYMENT.md"]
CODE = sorted(p for d in ("akashxdb", "akashx-documents", "akashx-knowledge")
              for p in (ROOT / "mcp-servers" / d).rglob("*.py")
              if "tests" not in p.parts and ".cache" not in p.parts)
DATASETS = re.compile(r"financebench|fbgolden|officeqa|ragbkt22|\b10-?K\b|\bfilings?\b", re.IGNORECASE)
IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
LOCAL = {"127.0.0.1", "0.0.0.0"}   # health checks and listen addresses


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _addresses(text: str) -> list[str]:
    return [ip for ip in IPV4.findall(text) if ip not in LOCAL]


def test_files_exist():
    assert all(p.exists() for p in CONFIG) and len(CODE) > 5


def test_no_dataset_names():
    hits = [f"{p.relative_to(ROOT)}:{n}" for p in CONFIG + CODE
            for n, line in enumerate(_text(p).splitlines(), 1) if DATASETS.search(line)]
    assert hits == []


def test_no_semantic_model_id_is_set():
    for p in CONFIG:
        text = _text(p)
        assert not re.search(r"X-Semantic-Model-Id:\s*['\"]?\w", text), p
        assert not re.search(r"^\s*AKASHX_SEMANTIC_MODEL_ID=\S", text, re.MULTILINE), p
        assert "mcp-akashxdb-scoped" not in text, p
    compose = _text(ROOT / "docker-compose.override.yml")
    assert "SEMANTIC_MODEL_ID=${AKASHX_SEMANTIC_MODEL_ID:-}" in compose


def test_no_server_addresses_or_databases_in_code_and_compose():
    compose = _text(ROOT / "docker-compose.override.yml")
    assert _addresses(compose) == []
    assert "AKASHX_MCP_HOST:-}" in compose and "AKASHX_MCP_DB:-}" in compose
    for p in CODE:
        assert _addresses(_text(p)) == [], p


def test_local_mcp_container_is_optional():
    compose = _text(ROOT / "docker-compose.override.yml")
    block = compose[compose.index("  akashxdb-mcp:"):compose.index("  akashx-knowledge:")]
    assert 'profiles: ["local-db-mcp"]' in block
