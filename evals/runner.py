"""Run the evaluation from the project root; same as `netmedic eval [options]`."""

import sys

from netmedic.cli import app

if __name__ == "__main__":
    app(["eval", *sys.argv[1:]])
