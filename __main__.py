"""Allow python -m oc_box in package-managed installations."""

from .oc_box import entrypoint

raise SystemExit(entrypoint())
