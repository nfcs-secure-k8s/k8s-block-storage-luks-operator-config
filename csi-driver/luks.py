"""
Thin subprocess wrappers around cryptsetup and mkfs.
All idempotency checks live here so callers stay simple.
"""

import logging
import os
import subprocess

LOG = logging.getLogger(__name__)


def _run(cmd: list[str], input_data: bytes | None = None) -> subprocess.CompletedProcess:
    LOG.debug("Running: %s", " ".join(cmd))
    result = subprocess.run(
        cmd,
        input=input_data,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Command {cmd[0]} failed (exit {result.returncode}): "
            f"{result.stderr.decode().strip()}"
        )
    return result


def is_luks(device: str) -> bool:
    """Return True if device already has a LUKS header.

    Bypasses _run() intentionally: a non-zero exit here means "not LUKS",
    not a fatal error, so we inspect the return code directly.
    """
    result = subprocess.run(
        ["cryptsetup", "isLuks", device],
        capture_output=True,
    )
    return result.returncode == 0


def mapper_exists(mapper_name: str) -> bool:
    """Return True if /dev/mapper/<mapper_name> exists and is active."""
    return os.path.exists(f"/dev/mapper/{mapper_name}")


def luks_format(device: str, key: bytes, luks_type: str = "luks2") -> None:
    """Format device with LUKS. key is the raw passphrase bytes."""
    LOG.info("Formatting %s with LUKS (%s)", device, luks_type)
    _run(
        [
            "cryptsetup", "luksFormat",
            "--batch-mode",
            "--type", luks_type,
            "--key-file", "-",
            device,
        ],
        input_data=key,
    )


def luks_open(device: str, mapper_name: str, key: bytes) -> None:
    """Open an existing LUKS device, creating /dev/mapper/<mapper_name>."""
    if mapper_exists(mapper_name):
        LOG.info("Mapper %s already open, skipping luksOpen", mapper_name)
        return
    LOG.info("Opening LUKS device %s as %s", device, mapper_name)
    _run(
        [
            "cryptsetup", "luksOpen",
            "--key-file", "-",
            device, mapper_name,
        ],
        input_data=key,
    )


def luks_close(mapper_name: str) -> None:
    """Close an open LUKS mapper. Safe to call even if already closed."""
    if not mapper_exists(mapper_name):
        LOG.debug("Mapper %s not open, nothing to close", mapper_name)
        return
    LOG.info("Closing LUKS mapper %s", mapper_name)
    _run(["cryptsetup", "luksClose", mapper_name])


def make_filesystem(mapper_name: str, filesystem: str = "ext4") -> None:
    """Create a filesystem on /dev/mapper/<mapper_name>."""
    device = f"/dev/mapper/{mapper_name}"
    LOG.info("Creating %s filesystem on %s", filesystem, device)
    if filesystem == "ext4":
        _run(["mkfs.ext4", "-F", device])
    elif filesystem == "xfs":
        _run(["mkfs.xfs", "-f", device])
    else:
        raise ValueError(f"Unsupported filesystem: {filesystem}")
