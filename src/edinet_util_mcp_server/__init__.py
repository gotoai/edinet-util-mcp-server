"""EDINET API v2 access for agents: list filings and read them as Markdown."""


def main() -> None:
    # Imported here, not at the top: the snapshot builder imports this package's
    # API client and figures without needing the MCP server.
    from .server import main as run

    run()


__all__ = ["main"]
