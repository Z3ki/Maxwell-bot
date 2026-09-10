#!/usr/bin/env python3
"""Compatibility entry point for the configurable Mailgun DNS helper."""

import sys

if __package__:
    from .setup_dns import main
else:
    from setup_dns import main


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
