"""python -m semantic_scope render <id>: print the prompt block for a semantic model."""
import sys

from mcp_server_starrocks import server

from semantic_scope import load_scope

if len(sys.argv) != 3 or sys.argv[1] != "render":
    sys.exit("usage: python -m semantic_scope render <semantic_model_id>")
print(load_scope(sys.argv[2], server.db_client).render())
