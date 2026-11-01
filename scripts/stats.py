#!/usr/bin/env python3
"""Read content-free operational metrics from DATA_DIR."""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from usage_metrics import format_summary, stats


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--days', type=int, default=30)
    parser.add_argument('--json', action='store_true', dest='as_json')
    args = parser.parse_args()
    try:
        result = stats(os.environ.get('DATA_DIR', 'data'), args.days)
    except ValueError as exc:
        parser.error(str(exc))
    if args.as_json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(format_summary(result))


if __name__ == '__main__':
    main()
