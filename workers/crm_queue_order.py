"""Launch one existing CRM workflow inside its queue worker's isolated process."""

import argparse
import json
import os
import runpy
import sys

from _bootstrap import ensure_project_root_on_path

ensure_project_root_on_path()

from queue_order_work import worker_invocation


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request-json", required=True)
    parser.add_argument("--profile-dir", required=True)
    parser.add_argument("--result-file", required=True)
    parser.add_argument("--visible", action="store_true")
    options = parser.parse_args(argv)
    request = json.loads(options.request_json)
    filename, arguments = worker_invocation(request, options.profile_dir, options.result_file)
    # These overrides live only in this subprocess. Legacy workers importing
    # either config name (including Separator's helper imports) see this slot.
    import config
    config.CRM_PROFILE_DIR = options.profile_dir
    config.PROCESSOR_PROFILE_DIR = options.profile_dir
    os.environ["COPYRIGHT_CANCEL_PROFILE_DIR"] = options.profile_dir
    if options.visible:
        arguments.append("--visible")
    path = os.path.join(os.path.dirname(__file__), filename)
    sys.argv = [path] + arguments
    try:
        runpy.run_path(path, run_name="__main__")
    except SystemExit as exc:
        return exc.code
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
