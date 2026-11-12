"""Pure mock inputs for offline host-fixture tests, never acceptance oracles."""


def private_store_info(*, root, image_copy_tmp_dir):
    """Describe the common private rootless layout without creating any state."""
    return {
        "host": {"security": {"rootless": True}, "serviceIsRemote": False},
        "store": {
            "graphDriverName": "overlay",
            "graphRoot": str(root / "data/containers/storage"),
            "runRoot": str(root / "podman-run"),
            "volumePath": str(root / "data/containers/storage/volumes"),
            "imageCopyTmpDir": str(image_copy_tmp_dir),
            "configFile": str(root / "config/containers/storage.conf"),
        },
    }
