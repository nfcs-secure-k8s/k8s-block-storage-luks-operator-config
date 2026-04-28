"""
CSI Controller service — volume provisioning via a backing StorageClass.

CreateVolume:
  1. Derive a backing PVC name from the requested volume name.
  2. Create the backing PVC using the 'backingStorageClass' StorageClass parameter.
  3. Wait for the PVC to reach Bound.
  4. Read the backing PV to determine the raw block device path.
  5. Return a Volume whose volume_context carries backingDevice, luksType,
     filesystem, backingPvcName, and backingPvcNamespace so NodeStageVolume
     can use them.
  The volume ID is encoded as "{namespace}/{pvc_name}" so DeleteVolume can
  locate the PVC without a cross-namespace list.

DeleteVolume:
  Parse namespace and PVC name from volume ID, then delete the backing PVC.
"""

import logging

import grpc

from generated import csi_pb2, csi_pb2_grpc
import k8s

LOG = logging.getLogger(__name__)

# StorageClass parameter keys
PARAM_BACKING_SC = "backingStorageClass"
PARAM_LUKS_TYPE = "luksType"
PARAM_FS = "filesystem"
PARAM_BACKING_NS = "backingNamespace"   # optional; defaults to operator namespace

GiB = 1 << 30

# How we derive a PVC name from the CSI volume name
_PVC_PREFIX = "luks-backing-"
_PVC_NAME_MAX = 63  # Kubernetes DNS label limit


def _backing_pvc_name(volume_name: str) -> str:
    raw = _PVC_PREFIX + volume_name.lower().replace("_", "-")
    # Trim to fit Kubernetes 63-char limit
    return raw[:_PVC_NAME_MAX].rstrip("-")


def _device_from_pv(pv) -> str | None:
    """Extract the raw block device path from a PV spec (best-effort).

    Returns None for cloud-backed PVs where the device path is only known
    after the volume is attached to a node.
    """
    spec = pv.spec
    # local PV (e.g. loop device in the Lima/k3s test environment)
    local = getattr(spec, "local", None)
    if local and getattr(local, "path", None):
        return local.path
    # hostPath PV
    host_path = getattr(spec, "host_path", None)
    if host_path and getattr(host_path, "path", None):
        return host_path.path
    # CSI-backed PVs: device path is only known on the node after attachment.
    return None


class ControllerServicer(csi_pb2_grpc.ControllerServicer):

    def CreateVolume(self, request, context):
        name = request.name
        params = request.parameters
        capacity = request.capacity_range.required_bytes if request.capacity_range else 0

        backing_sc = params.get(PARAM_BACKING_SC)
        if not backing_sc:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details(f"StorageClass parameter '{PARAM_BACKING_SC}' is required")
            return csi_pb2.CreateVolumeResponse()

        luks_type = params.get(PARAM_LUKS_TYPE, "luks2")
        filesystem = params.get(PARAM_FS, "ext4")
        # Use the requesting PVC's namespace if no override is specified.
        # external-provisioner injects csi.storage.k8s.io/pvc-namespace automatically.
        namespace = (
            params.get(PARAM_BACKING_NS)
            or params.get("csi.storage.k8s.io/pvc-namespace")
            or k8s.get_operator_namespace()
        )

        # Convert bytes to a Kubernetes quantity string (minimum 1Gi)
        gib = max(1, (capacity + GiB - 1) // GiB)
        size_str = f"{gib}Gi"

        pvc_name = _backing_pvc_name(name)
        # Encode namespace in volume_id so DeleteVolume doesn't need a cross-namespace list
        volume_id = f"{namespace}/{pvc_name}"

        LOG.info(
            "CreateVolume: name=%s pvc=%s/%s sc=%s size=%s",
            name, namespace, pvc_name, backing_sc, size_str,
        )

        try:
            k8s.create_pvc(pvc_name, namespace, backing_sc, size_str)
            pv_name = k8s.wait_for_pvc_bound(pvc_name, namespace)

            # Read the PV to extract the device path (may be None for cloud volumes)
            api = k8s.core()
            pv = api.read_persistent_volume(pv_name)
            device = _device_from_pv(pv)

            volume_context = {
                "backingPvcName": pvc_name,
                "backingPvcNamespace": namespace,
                "backingPvName": pv_name,
                "luksType": luks_type,
                "filesystem": filesystem,
            }
            if device:
                volume_context["backingDevice"] = device

            # Emit an event on the user's PVC so 'kubectl describe pvc' gives
            # actionable guidance. The message differs based on whether the Secret
            # already exists — avoids a spurious warning when the user created the
            # Secret before the PVC (the recommended order).
            user_pvc_name = params.get("csi.storage.k8s.io/pvc-name")
            if user_pvc_name:
                expected_secret = f"{user_pvc_name}-luks-key"
                if k8s.secret_exists(expected_secret, namespace):
                    k8s.emit_event(
                        name=user_pvc_name,
                        namespace=namespace,
                        reason="LuksKeyFound",
                        message=(
                            f'Volume provisioned. Secret "{expected_secret}" found — '
                            f"volume is ready for use."
                        ),
                    )
                else:
                    k8s.emit_event(
                        name=user_pvc_name,
                        namespace=namespace,
                        reason="LuksKeyRequired",
                        message=(
                            f'Volume provisioned. Before scheduling pods, create Secret '
                            f'"{expected_secret}" in namespace "{namespace}" '
                            f'with a "luksKey" field containing the LUKS passphrase.'
                        ),
                    )

            return csi_pb2.CreateVolumeResponse(
                volume=csi_pb2.Volume(
                    volume_id=volume_id,
                    capacity_bytes=gib * GiB,
                    volume_context=volume_context,
                )
            )

        except Exception as e:
            LOG.exception("CreateVolume failed")
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(e))
            return csi_pb2.CreateVolumeResponse()

    def DeleteVolume(self, request, context):
        volume_id = request.volume_id
        LOG.info("DeleteVolume: volume_id=%s", volume_id)

        # volume_id is encoded as "{namespace}/{pvc_name}"
        if "/" in volume_id:
            namespace, pvc_name = volume_id.split("/", 1)
        else:
            # Fallback for volume IDs created before the namespace encoding
            namespace = k8s.get_operator_namespace()
            pvc_name = volume_id

        try:
            k8s.delete_pvc(pvc_name, namespace)
        except Exception as e:
            LOG.exception("DeleteVolume failed")
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(e))
            return csi_pb2.DeleteVolumeResponse()

        return csi_pb2.DeleteVolumeResponse()

    def ControllerGetCapabilities(self, request, context):
        return csi_pb2.ControllerGetCapabilitiesResponse(
            capabilities=[
                csi_pb2.ControllerServiceCapability(
                    rpc=csi_pb2.ControllerServiceCapability.RPC(
                        type=csi_pb2.ControllerServiceCapability.RPC.CREATE_DELETE_VOLUME,
                    )
                )
            ]
        )

    def ValidateVolumeCapabilities(self, request, context):
        # Accept any capability — we don't enforce constraints in this prototype.
        for cap in request.volume_capabilities:
            access_mode = getattr(cap, "access_mode", None)
            if not access_mode or access_mode.mode != csi_pb2.VolumeCapability.AccessMode.SINGLE_NODE_WRITER:
                return csi_pb2.ValidateVolumeCapabilitiesResponse(
                    message="only SINGLE_NODE_WRITER (ReadWriteOnce) is supported"
                )

        return csi_pb2.ValidateVolumeCapabilitiesResponse(
            confirmed=csi_pb2.ValidateVolumeCapabilitiesResponse.Confirmed(
                volume_capabilities=request.volume_capabilities,
            )
        )
