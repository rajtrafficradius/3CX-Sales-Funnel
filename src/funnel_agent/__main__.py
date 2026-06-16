"""Enable `python -m funnel_agent ...` (equivalent to the `funnel-agent` script)."""

from .cli import app

if __name__ == "__main__":
    app()
