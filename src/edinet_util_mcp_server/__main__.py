"""`python -m edinet_util_mcp_server`, which is how a client that installed the
wheel starts the server: no console script on PATH, no absolute path to bake into
a configuration file."""

from .server import main

main()
