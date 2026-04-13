#!/bin/bash
set -euo pipefail

PROFILE_NAME="K8s-luks-restricted"
SSH_KEY="${HOME}/.ssh/id_rsa_ceph-ansible-labs"
SSH_USER="ubuntu"

NODES=$(kubectl get nodes -o jsonpath='{range .items[*]}{.status.addresses[?(@.type=="InternalIP")].address}{" "}{end}')
eval "$(ssh-agent -s)"
ssh-add "$SSH_KEY"

for NODE in $NODES; do
  echo "Processing node $NODE"
  ssh $SSH_USER@$NODE "sudo tee /etc/apparmor.d/$PROFILE_NAME > /dev/null && sudo apparmor_parser -r /etc/apparmor.d/$PROFILE_NAME" <<'EOF'
    # Import standard variable definitions
include <tunables/global>

profile k8s-luks-restricted flags=(attach_disconnected) {
  # Include standard abstractions
  include <abstractions/base>
  include <abstractions/nameservice>

  # 1. BINARIES (Including chmod)
  /bin/sh ix,
  /bin/busybox ix,
  /sbin/apk ix,
  /{usr/,}sbin/cryptsetup ix,
  /{usr/,}bin/nsenter ix,
  /{usr/,}bin/lsblk ix,
  /{usr/,}bin/findmnt ix,
  /{usr/,}sbin/dmsetup ix,
  /{usr/,}bin/fuser ix,
  /{usr/,}bin/umount ix,
  /{usr/,}bin/mount ix,
  /{usr/,}bin/tr ix,
  /{usr/,}bin/awk ix,
  /{usr/,}bin/grep ix,
  /{usr/,}bin/sleep ix,
  /{usr/,}bin/mkdir ix,
  /{usr/,}sbin/mkfs.ext4 ix,
  /{usr/,}sbin/mke2fs ix,
  /{usr/,}bin/chown ix,
  /{usr/,}bin/chmod ix, 

  # 2. CAPABILITIES (The "Force" permissions)
  capability chown,
  capability fowner,
  capability fsetid,
  capability dac_override,
  capability dac_read_search,
  capability sys_chroot,
  capability sys_admin,
  capability sys_resource,
  capability ipc_lock,
  capability sys_ptrace,
  capability sys_nice,
  capability mknod,

  # 3. PTRACE & NETWORK
  ptrace (read, trace) peer=unconfined,
  network alg,
  network unix,

  # 4. DEVICE NODES (rwmk for locking/mapping)
  /dev/vd* rwmk,
  /dev/sd* rwmk,
  /dev/dm-* rwmk,
  /dev/mapper/* rwmk,
  /dev/mapper/control rwmk,
  /dev/encrypted-block rwmk,
  /dev/urandom r,
  /dev/random r,

  # 5. SYSTEM INFO & CONFIG
  / r,
  /etc/mke2fs.conf r,
  /etc/ssl/openssl.cnf r,
  /proc/crypto r,
  /proc/devices r,
  @{PROC}/self/mountinfo r,
  @{PROC}/[0-9]*/mountinfo r,
  /sys/dev/block/** r,
  /sys/devices/** r,
  /sys/block/** r,
  /sys/fs/ r,

  # 6. RUNTIME & LOCKING
  /run/ r,
  /run/mount/ rw,
  /run/mount/** rw,
  /run/cryptsetup/ rwk,
  /run/cryptsetup/** rwk,

  # 7. VAULT & MOUNT POINTS
  /vault/secrets/ r,
  /vault/secrets/** r,
  /mnt/shared/ rw,
  /mnt/shared/** rw,

  mount fstype=ext4,
  umount,

  # 8. HARD DENIALS
  deny /etc/shadow w,
  deny /root/** w,
  deny /sys/firmware/** rw,
}
EOF

  echo "Complete $NODE"

done