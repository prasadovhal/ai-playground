"""Run frozen analysis with its missing pathlib import supplied (no API calls)."""
from pathlib import Path

from .. import analysis


def main():
    # The frozen csv_write helper references Path without importing it. Supply
    # only that missing dependency; do not change outcomes or inference gates.
    analysis.Path = Path
    return analysis.analyze()


if __name__ == '__main__':
    main()
