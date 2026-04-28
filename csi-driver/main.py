"""
Entry point for the LUKS CSI driver.

Starts a gRPC server on a Unix socket and registers the appropriate services
depending on CSI_MODE:
  controller  – Identity + Controller (runs in the operator Deployment)
  node        – Identity + Node       (runs in the DaemonSet)
  all         – Identity + Controller + Node (useful for local testing)
"""

import logging
import os
import signal
import sys
from concurrent import futures

import grpc

from generated import csi_pb2_grpc
from driver import IdentityServicer
from controller import ControllerServicer
from node import NodeServicer

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    stream=sys.stdout,
)
LOG = logging.getLogger(__name__)

DEFAULT_SOCKET = "/csi/csi.sock"


def _socket_address(path: str) -> str:
    return f"unix://{path}"


def _cleanup_socket(path: str) -> None:
    try:
        os.unlink(path)
        LOG.debug("Removed stale socket %s", path)
    except FileNotFoundError:
        pass


def serve(socket_path: str, mode: str) -> None:
    _cleanup_socket(socket_path)
    os.makedirs(os.path.dirname(socket_path), exist_ok=True)

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))

    # Identity is always registered
    csi_pb2_grpc.add_IdentityServicer_to_server(IdentityServicer(), server)

    if mode in ("controller", "all"):
        csi_pb2_grpc.add_ControllerServicer_to_server(ControllerServicer(), server)
        LOG.info("Registered Controller service")

    if mode in ("node", "all"):
        csi_pb2_grpc.add_NodeServicer_to_server(NodeServicer(), server)
        LOG.info("Registered Node service")

    addr = _socket_address(socket_path)
    server.add_insecure_port(addr)
    server.start()
    LOG.info("gRPC server listening on %s (mode=%s)", addr, mode)

    def _shutdown(signum, frame):
        LOG.info("Shutting down (signal %d)", signum)
        server.stop(grace=5)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    server.wait_for_termination()


if __name__ == "__main__":
    socket_path = os.environ.get("CSI_ENDPOINT", DEFAULT_SOCKET)
    mode = os.environ.get("CSI_MODE", "all").lower()

    if mode not in ("controller", "node", "all"):
        LOG.error("Invalid CSI_MODE=%r — must be controller, node, or all", mode)
        sys.exit(1)

    LOG.info("Starting LUKS CSI driver (mode=%s, socket=%s)", mode, socket_path)
    serve(socket_path, mode)
