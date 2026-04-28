# LUKS CSI Driver

A Python implementation of a Container Storage Interface (CSI) driver that provides
transparent LUKS disk encryption as a Kubernetes StorageClass. Users create a PVC
with `storageClassName: luks-encrypted` and get an encrypted filesystem — no custom
resources, no helper pods, no per-workload operator configuration.

---

## How it works

```
User PVC (storageClassName: luks-encrypted)
        │
        ▼
  external-provisioner sidecar
        │  calls CreateVolume
        ▼
  Controller plugin (Deployment)
  ├── creates a backing PVC against the configured backingStorageClass
  └── waits for it to Bind → returns VolumeContext (device path, LUKS params)
        │
        │  kubelet calls NodeStageVolume on the node where the pod is scheduled
        ▼
  Node plugin (DaemonSet, privileged)
  ├── NodeStageVolume:
  │     cryptsetup isLuks  → luksFormat (first use only)
  │     cryptsetup luksOpen → /dev/mapper/luks-<id>
  │     mkfs.<fs>           (first use only)
  │     mount               → global staging path
  ├── NodePublishVolume:
  │     bind-mount staging path → pod-specific path
  ├── NodeUnpublishVolume: umount pod path
  └── NodeUnstageVolume:  cryptsetup luksClose
```

The LUKS passphrase is never generated or stored by the driver. Users create a
Kubernetes Secret named `<pvc-name>-luks-key` before provisioning. The CSI secret
mechanism passes its contents to `NodeStageVolume` at mount time.

---

## Project structure

```
csi-driver/
├── main.py               # gRPC server entry point; CSI_MODE=controller|node|all
├── driver.py             # Identity service (GetPluginInfo, Probe)
├── controller.py         # Controller service (CreateVolume / DeleteVolume)
├── node.py               # Node service (NodeStageVolume, NodePublishVolume, ...)
├── luks.py               # cryptsetup and mkfs subprocess wrappers
├── k8s.py                # Kubernetes API helpers (PVC lifecycle, Secret reads)
├── requirements.txt
├── Dockerfile
├── generate_proto.sh     # Generates Python gRPC stubs from csi.proto
├── SECURITY.md           # Security review and known issues for sensitive data workloads
├── proto/
│   └── csi.proto         # CSI spec v1 (from container-storage-interface/spec)
├── generated/            # Auto-generated — run generate_proto.sh to create
│   ├── csi_pb2.py
│   └── csi_pb2_grpc.py
└── manifests/
    ├── csidriver.yaml     # CSIDriver registration
    ├── storageclass.yaml  # Example StorageClass (luks-encrypted)
    ├── controller.yaml    # Deployment: luks-csi (controller) + external-provisioner
    ├── node.yaml          # DaemonSet: luks-csi (node) + node-driver-registrar
    ├── rbac.yaml          # ServiceAccounts, ClusterRoles, ClusterRoleBindings
    └── test-resources.yaml  # Loop-device test: SC, PV, Secret, PVC, Pod
```

---

## Prerequisites

**Tools required on your workstation:**

- Python 3.13+
- `grpcio-tools` (to generate gRPC stubs from `csi.proto`)
- Docker (to build the container image)
- `kubectl` 1.28+

**Cluster requirements:**

- Kubernetes 1.28+ (k3s, RKE2, GKE, EKS, AKS, etc.)
- A block-mode StorageClass (e.g. Ceph RBD, OpenStack Cinder, AWS EBS in block mode,
  or a local/loop device for testing)
- CSI sidecar images accessible from the cluster
  (`registry.k8s.io/sig-storage/csi-provisioner:v5.1.0` and
  `registry.k8s.io/sig-storage/csi-node-driver-registrar:v2.12.0`)

**For local development with Lima + k3s (macOS):**

- [Lima](https://lima-vm.io/) 2.0+ — `brew install lima`
- A running k3s Lima VM (see [Local development with Lima](#local-development-with-lima) below)

---

## Generate gRPC stubs

The `generated/` directory is not included in the repository. Run this once after
cloning (requires `grpcio-tools`):

```bash
pip install grpcio-tools
cd csi-driver
bash generate_proto.sh
```

This writes `generated/csi_pb2.py` and `generated/csi_pb2_grpc.py` and is also
run automatically inside the Docker build.

---

## Quick start (any cluster)

### 1. Build the container image

```bash
docker build -t luks-csi:dev ./csi-driver/
```

Push to a registry accessible from your cluster, or load it directly into your
cluster's container runtime (see [Local development with Lima](#local-development-with-lima)
for the k3s import workflow).

```bash
docker tag luks-csi:dev <your-registry>/luks-csi:dev
docker push <your-registry>/luks-csi:dev
```

Update `image:` in `manifests/controller.yaml` and `manifests/node.yaml` to match,
and set `imagePullPolicy: IfNotPresent` (or `Always`).

### 2. Set up a backing StorageClass

Edit `manifests/storageclass.yaml` and set `backingStorageClass` to a StorageClass
in your cluster that provisions raw block volumes:

```yaml
parameters:
  backingStorageClass: csi-cinder-sc-retain   # or rbd-sc, ebs-sc, etc.
  luksType: luks2
  filesystem: ext4
```

### 3. Deploy the CSI driver

```bash
kubectl apply -f csi-driver/manifests/csidriver.yaml \
              -f csi-driver/manifests/rbac.yaml \
              -f csi-driver/manifests/storageclass.yaml \
              -f csi-driver/manifests/controller.yaml \
              -f csi-driver/manifests/node.yaml
```

Wait for both workloads to be ready:

```bash
kubectl rollout status deployment/luks-csi-controller -n kube-system
kubectl rollout status daemonset/luks-csi-node -n kube-system
```

### 4. Provision an encrypted volume

Create the key Secret **before** the PVC (naming convention: `<pvc-name>-luks-key`):

```bash
kubectl create secret generic my-pvc-luks-key \
  --from-literal=luksKey=<your-passphrase> \
  --namespace=default
```

Create the PVC:

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: my-pvc
  namespace: default
spec:
  storageClassName: luks-encrypted
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 10Gi
```

The key ownership stays entirely with the user. The driver reads the Secret at mount
time and never logs or persists the passphrase.

---

## Local development with Lima

These instructions set up a self-contained test environment on macOS using
[Lima](https://lima-vm.io/) with no external registry.

### One-time setup

**1. Create and start a k3s Lima VM:**

```bash
limactl create --name=k3s template://k3s
limactl start k3s
```

**2. Configure kubectl to point at the Lima k3s cluster:**

```bash
mkdir -p ~/.kube
limactl shell k3s -- sudo cat /etc/rancher/k3s/k3s.yaml > ~/.kube/config
# The kubeconfig uses 127.0.0.1 — update the IP to the VM's address:
K3S_IP=$(limactl shell k3s -- hostname -I | awk '{print $1}')
sed -i '' "s/127.0.0.1/${K3S_IP}/g" ~/.kube/config
kubectl get nodes   # should show k3s node as Ready
```

**3. Create a Lima Docker VM for image builds (no registry needed):**

```bash
limactl create --name=docker template://docker
limactl start docker
```

The Docker Lima VM mounts your home directory read-only inside the VM, so the
project directory is accessible directly at its host path.

### Build and import the image

Run these steps after any code change:

```bash
# Build inside the Docker Lima VM
limactl shell docker -- docker build \
  -t luks-csi:dev \
  "${HOME}/Documents/k8s-luks-operator/csi-driver/"

# Import into k3s (no registry needed — pipes directly into k3s containerd)
limactl shell docker -- docker save luks-csi:dev \
  | limactl shell k3s -- sudo k3s ctr images import -
```

After import, restart the workloads to pick up the new image:

```bash
kubectl rollout restart deployment/luks-csi-controller -n kube-system
kubectl rollout restart daemonset/luks-csi-node -n kube-system
```

### Set up a loop device (test block storage)

Run inside the k3s VM once per VM restart:

```bash
limactl shell k3s -- sudo bash -c "
  dd if=/dev/zero of=/tmp/test-block.img bs=1M count=1200
  losetup /dev/loop0 /tmp/test-block.img
"
```

### Deploy the CSI driver

```bash
kubectl apply -f csi-driver/manifests/csidriver.yaml \
              -f csi-driver/manifests/rbac.yaml \
              -f csi-driver/manifests/storageclass.yaml \
              -f csi-driver/manifests/controller.yaml \
              -f csi-driver/manifests/node.yaml
```

Wait for both workloads to be ready:

```bash
kubectl rollout status deployment/luks-csi-controller -n kube-system
kubectl rollout status daemonset/luks-csi-node -n kube-system
```

### Run the end-to-end test

```bash
kubectl apply -f csi-driver/manifests/test-resources.yaml
```

This creates:
- `loop-backing` StorageClass (static provisioner for the loop device)
- `luks-loop-pv` PV pointing at `/dev/loop0`
- `luks-encrypted-loop` StorageClass (our CSI driver, backed by `loop-backing`)
- `test-pvc-luks-key` Secret with the LUKS passphrase
- `test-pvc` PVC using `luks-encrypted-loop`
- `luks-test-pod` that writes `hello from luks` to `/mnt/data/test.txt`

Watch it converge:

```bash
kubectl get pvc test-pvc -w          # should reach Bound within ~30s
kubectl get pod luks-test-pod -w     # should reach Running
```

Verify the encrypted volume works:

```bash
# File written by the pod
kubectl exec luks-test-pod -- cat /mnt/data/test.txt

# Filesystem mounted on the decrypted device
kubectl exec luks-test-pod -- df -h /mnt/data

# Active LUKS mapper on the node (confirms real encryption)
limactl shell k3s -- sudo cryptsetup status $(
  ls /dev/mapper/ | grep ^luks- | head -1
)
```

Expected output from `cryptsetup status`:
```
type:    LUKS2
cipher:  aes-xts-plain64
keysize: 512 bits
device:  /dev/loop0
mode:    read/write
```

### Tear down

```bash
kubectl delete -f csi-driver/manifests/test-resources.yaml
```

---

## Security considerations

See [`SECURITY.md`](SECURITY.md) for a full review of known issues and mitigations,
including concerns specific to health data and other sensitive workloads.

---

## Comparison to the kopf operator approach

The original implementation (`main.py` at the project root) uses the
[kopf](https://github.com/nolar/kopf) framework and a custom `EncryptedVolume` CRD.

| Aspect | kopf Operator | CSI Driver |
|---|---|---|
| **User interface** | Custom Resource (`EncryptedVolume`) | Standard PVC (`storageClassName: luks-encrypted`) |
| **Key management** | Secret path hard-coded in operator | User creates `<pvc-name>-luks-key`; driver reads it at mount time via CSI secret mechanism |
| **Device path** | Hard-coded `/dev/vdc` in pod shell script | Derived from the backing PV's spec; passed via VolumeContext |
| **Encryption setup** | init container inside every workload pod (installs cryptsetup at runtime) | Node plugin runs once per volume on the node; no changes to user pods |
| **Privileged pods** | Every user workload pod needs `privileged: true` | Only the node DaemonSet is privileged; user pods are unprivileged |
| **Lifecycle management** | No delete handler; LUKS device left open on pod exit | `NodeUnstageVolume` calls `cryptsetup luksClose`; clean unmount on pod deletion |
| **Multi-pod attach** | Not handled | CSI capabilities enforce RWO; kubelet manages attach/detach |
| **Backend portability** | Tied to OpenStack Cinder (`/dev/vdc` path assumption) | Works with any block StorageClass via `backingStorageClass` parameter |
| **Kubernetes integration** | Operator must be running for volumes to work | Standard CSI; volumes work independently of the operator process |
| **Observability** | kopf events + custom status fields | Standard PVC/PV events; `kubectl describe pvc` shows provisioning errors |

### Key limitations of the operator approach

1. **Hard-coded device path** — `/dev/vdc` is assumed in the init container shell
   script. Different clouds or StorageClasses assign different device names, causing
   silent failures.

2. **Privileged workload pods** — Every pod that needs an encrypted volume must run
   with `privileged: true`. This widens the blast radius of any container escape and
   is typically blocked by Pod Security Admission in production clusters.

3. **No unmount lifecycle** — There is no `@kopf.on.delete` handler. When the
   `EncryptedVolume` CR or pod is deleted, the LUKS mapper stays open on the node
   until it is manually closed or the node reboots.

4. **Encryption runs inside the user container** — `cryptsetup` is installed via
   `apk` inside the init container on every startup. This is slow, requires internet
   access from the pod, and runs encryption setup as part of the application
   container's lifecycle.

5. **No StorageClass abstraction** — Users must reference specific PVC names and
   storage classes manually; there is no self-service provisioning path.

### When to use each approach

| Scenario | Recommendation |
|---|---|
| Production cluster, standard storage | **CSI driver** — standard interface, no privileged workloads |
| Existing cluster with a block StorageClass | **CSI driver** — drop-in replacement via StorageClass |
| Quick PoC on a known device path | Operator is faster to deploy |
| Cluster where CSI sidecars are unavailable | Operator (no sidecar dependencies) |
