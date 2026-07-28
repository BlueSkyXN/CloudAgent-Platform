"""Retired HFS deployment probe.

CloudAgent HFS now starts only the immutable checked-out product source through
``start.sh``. This module is deliberately non-runnable so it cannot masquerade
as the product health surface if invoked outside the exported source wrapper.
"""

raise SystemExit(
    "cloud/hfs/app.py is retired; export the source wrapper and run /app/start.sh instead"
)
