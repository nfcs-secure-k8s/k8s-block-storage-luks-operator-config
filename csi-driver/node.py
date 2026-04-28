"""
CSI Node service — LUKS setup, staging, and mounting.

Flow:
  NodeStageVolume   → luksFormat (idempotent) → luksOpen → mount at staging path
  NodePublishVolume → bind-mount staging path  → pod target path
  NodeUnpublishVolume → umount target path
  NodeUnstageVolume   → luksClose
"""

import logging
import os
import socket
import subprocess

import grpc

from generated import csi_pb2, csi_pb2_grpc
import luks

LOG = logging.getLogger(__name__)

# Key inside the CSI secrets map that holds the LUKS passphrase.
LUKS_KEY_FIELD = "luksKey"


def _mapper_name(volume_id: str) -> str:
    """Deterministic dm-crypt mapper name derived from volume ID."""
    # Truncate the full prefixed string to 63 chars (device-mapper limit)
    return ("luks-" + volume_id.replace("/", "-"))[:63]


def _mount(source: str, target: str, fs_type: str = "", flags: list[str] | None = None) -> None:
    cmd = ["mount"]
    if flags:
        cmd += flags
    if fs_type:
        cmd += ["-t", fs_type]
    cmd += [source, target]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"mount failed: {result.stderr.decode().strip()}"
        )


def _is_mounted(path: str) -> bool:
    result = subprocess.run(["mountpoint", "-q", path], capture_output=True)
    return result.returncode == 0


def _umount(path: str) -> None:
    result = subprocess.run(["umount", path], capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(f"umount failed: {result.stderr.decode().strip()}")


class NodeServicer(csi_pb2_grpc.NodeServicer):

    def NodeStageVolume(self, request, context):
        """
        Format + open the LUKS device and mount it at the staging path.

        Expected volume_context keys:
          backingDevice  – block device path (e.g. /dev/loop0, /dev/vdb)
          luksType       – luks1 or luks2 (default: luks2)
          filesystem     – ext4 or xfs   (default: ext4)
        """
        volume_id = request.volume_id
        staging_path = request.staging_target_path
        ctx = request.volume_context
        secrets = request.secrets

        if not volume_id:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details("volume_id is required")
            return csi_pb2.NodeStageVolumeResponse()

        device = ctx.get("backingDevice")
        if not device:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details("volume_context.backingDevice is required")
            return csi_pb2.NodeStageVolumeResponse()

        luks_key_str = secrets.get(LUKS_KEY_FIELD)
        if not luks_key_str:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details(f"secrets.{LUKS_KEY_FIELD} is required")
            return csi_pb2.NodeStageVolumeResponse()

        luks_key = luks_key_str.encode() if isinstance(luks_key_str, str) else luks_key_str
        luks_type = ctx.get("luksType", "luks2")
        filesystem = ctx.get("filesystem", "ext4")
        mapper = _mapper_name(volume_id)

        LOG.info(
            "NodeStageVolume: volume=%s device=%s mapper=%s staging=%s",
            volume_id, device, mapper, staging_path,
        )

        try:
            # 1. Format the device if it doesn't already have a LUKS header
            if not luks.is_luks(device):
                LOG.info("Device %s is not LUKS-formatted; formatting now", device)
                luks.luks_format(device, luks_key, luks_type)
                luks.luks_open(device, mapper, luks_key)
                luks.make_filesystem(mapper, filesystem)
                # Leave the mapper open — luks_open below is idempotent
            else:
                LOG.info("Device %s already LUKS-formatted", device)

            # 2. Open the LUKS device (no-op if already open from format branch)
            luks.luks_open(device, mapper, luks_key)

            # 3. Mount the decrypted device at the staging path
            os.makedirs(staging_path, exist_ok=True)
            if _is_mounted(staging_path):
                LOG.info("Staging path %s already mounted", staging_path)
            else:
                _mount(f"/dev/mapper/{mapper}", staging_path, fs_type=filesystem)
                LOG.info("Mounted /dev/mapper/%s at %s", mapper, staging_path)

        except Exception as e:
            LOG.exception("NodeStageVolume failed")
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(e))
            return csi_pb2.NodeStageVolumeResponse()

        return csi_pb2.NodeStageVolumeResponse()

    def NodeUnstageVolume(self, request, context):
        """Unmount staging path and close the LUKS mapper."""
        volume_id = request.volume_id
        staging_path = request.staging_target_path
        mapper = _mapper_name(volume_id)

        LOG.info("NodeUnstageVolume: volume=%s staging=%s mapper=%s", volume_id, staging_path, mapper)

        try:
            if _is_mounted(staging_path):
                _umount(staging_path)
            luks.luks_close(mapper)
        except Exception as e:
            LOG.exception("NodeUnstageVolume failed")
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(e))
            return csi_pb2.NodeUnstageVolumeResponse()

        return csi_pb2.NodeUnstageVolumeResponse()

    def NodePublishVolume(self, request, context):
        """Bind-mount the staged path to the pod-specific target path."""
        volume_id = request.volume_id
        staging_path = request.staging_target_path
        target_path = request.target_path
        readonly = request.readonly

        LOG.info(
            "NodePublishVolume: volume=%s staging=%s target=%s readonly=%s",
            volume_id, staging_path, target_path, readonly,
        )

        try:
            os.makedirs(target_path, exist_ok=True)
            if _is_mounted(target_path):
                LOG.info("Target path %s already mounted", target_path)
            else:
                _mount(staging_path, target_path, flags=["--bind"])
                if readonly:
                    # A bind mount inherits rw; a separate remount is required to make it ro.
                    _mount(target_path, target_path, flags=["-o", "remount,ro,bind"])
                    LOG.info("Bind-mounted %s → %s (read-only)", staging_path, target_path)
                else:
                    LOG.info("Bind-mounted %s → %s", staging_path, target_path)
        except Exception as e:
            LOG.exception("NodePublishVolume failed")
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(e))
            return csi_pb2.NodePublishVolumeResponse()

        return csi_pb2.NodePublishVolumeResponse()

    def NodeUnpublishVolume(self, request, context):
        """Unmount the pod-specific target path."""
        target_path = request.target_path
        LOG.info("NodeUnpublishVolume: target=%s", target_path)

        try:
            if _is_mounted(target_path):
                _umount(target_path)
        except Exception as e:
            LOG.exception("NodeUnpublishVolume failed")
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(e))
            return csi_pb2.NodeUnpublishVolumeResponse()

        return csi_pb2.NodeUnpublishVolumeResponse()

    def NodeGetCapabilities(self, request, context):
        return csi_pb2.NodeGetCapabilitiesResponse(
            capabilities=[
                csi_pb2.NodeServiceCapability(
                    rpc=csi_pb2.NodeServiceCapability.RPC(
                        type=csi_pb2.NodeServiceCapability.RPC.STAGE_UNSTAGE_VOLUME,
                    )
                )
            ]
        )

    def NodeGetInfo(self, request, context):
        return csi_pb2.NodeGetInfoResponse(node_id=socket.gethostname())
