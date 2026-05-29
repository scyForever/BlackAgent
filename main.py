"""Local CLI entrypoint for BlackAgent.

BlackAgent intentionally does not expose a public HTTP interface.
Run the agent through ``python main.py`` or ``python scripts/run_agent_cli.py``.
"""

from __future__ import annotations

from scripts.run_agent_cli import main


if __name__ == "__main__":
    raise SystemExit(main())
