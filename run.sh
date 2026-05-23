#!/usr/bin/env bash
# Use the system Python 3.12 where all packages are installed.
# The local python3 (3.14) does not have the packages.
exec /usr/bin/python3 main.py "$@"
