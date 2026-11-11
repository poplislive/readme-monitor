"""Entry point of the frozen engine (PyInstaller): the same command line as `readme-monitor`."""
import sys

from readme_monitor.cli import main

if __name__ == "__main__":
    sys.exit(main())
