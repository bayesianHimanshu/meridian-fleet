"""Write docs/project-stats.json and docs/project-stats.md from the real ledger (wrap-up).
The same as `uv run meridian stats --write docs`; every number in README.md and
docs/project-summary.md comes from this output."""

from pathlib import Path

from meridian.cli_stats import write_stats

if __name__ == "__main__":
    write_stats(Path("docs"))
