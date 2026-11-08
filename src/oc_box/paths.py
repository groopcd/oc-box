"""Resolve checkout and packaged resources without changing installed layouts."""

from pathlib import Path


def source_root(root):
    """A src package uses checkout/sdist metadata; installed copies use theirs."""
    root = Path(root).resolve()
    if root.name == "oc_box" and root.parent.name == "src":
        candidate = root.parent.parent
        if (candidate / "pyproject.toml").is_file():
            return candidate
    return root


def runtime_root(root):
    root = source_root(root)
    package = root / "src/oc_box"
    if (root / "src").is_symlink() or package.is_symlink():
        raise ValueError("Source runtime directory must not be a symlink.")
    if (package / "oc_box.py").is_file():
        return package
    return root
