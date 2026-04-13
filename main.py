import kopf
import kubernetes
from kubernetes.client.rest import ApiException
import hvac
import os
import secrets
import datetime

def ensure_block_pvc(namespace: str, pvc_name: str, storage_class: str, size: str, owner_name: str, owner_uid: str, logger, policy="Delete"):
    """
    Ensure a Block-mode PVC exists. If it already exists, do nothing.
    """
    v1 = kubernetes.client.CoreV1Api()
    storage_api = kubernetes.client.StorageV1Api()
    binding_mode = None

        # basic metadata
    metadata = {
    "name": pvc_name, 
    "namespace": namespace
    }


    try:
        sc = storage_api.read_storage_class(storage_class)
        binding_mode = (sc.volume_binding_mode or "").strip()
        logger.info(f"StorageClass {storage_class} volumeBindingMode={binding_mode!r}")
    except ApiException as e:
        
        logger.warning(
            f"Could not read StorageClass {storage_class}: {e}. "
            "Proceeding without waiting for PVC to bind."
        )

    pvc_manifest = {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": metadata,
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "volumeMode": "Block",
            "resources": {"requests": {"storage": size}},
            "storageClassName": storage_class,
        },
    }

    #Create if missing
    try:
        v1.read_namespaced_persistent_volume_claim(name=pvc_name, namespace=namespace)
        logger.info(f"PVC {pvc_name} already exists.")
    except ApiException as e:
        if e.status == 404:
            logger.info(f"Creating PVC {pvc_name} (Block, {size}, SC={storage_class})...")
            v1.create_namespaced_persistent_volume_claim(namespace=namespace, body=pvc_manifest)
        else:
            raise

    if binding_mode == "WaitForFirstConsumer":
        logger.info(
            f"PVC {namespace}/{pvc_name}: not waiting for Bound because "
            f"StorageClass {storage_class} uses WaitForFirstConsumer."
        )
        return

    # Unknown binding mode (or if  StorageClass unreadable)
    logger.info(
        f"PVC {namespace}/{pvc_name}: not waiting for Bound "
        f"(bindingMode={binding_mode!r}); proceeding to allow scheduling."
    )
    return
# vault

def get_vault_client(logger):
    # inject token from servioce account
    with open('/var/run/secrets/kubernetes.io/serviceaccount/token', 'r') as f:
        jwt = f.read()
        logger.info(f"check jwt: {jwt}")
    
    client = hvac.Client(url=os.environ.get('VAULT_ADDR', 'http://vault.default:8200'))
    client.auth.kubernetes.login(
        role=os.environ.get('VAULT_ROLE', 'luks-operator-role'),
        jwt=jwt,
    )
    logger.info(f"Check client: {client}")
    return client

# delete vault secret
def delete_vault_secret(path: str, logger):
    client = get_vault_client(logger)
    parts = path.split('/')
    logger.info(f"check parts: {parts}")
    mount_point = parts[0]
    logger.info(f"check mount_point: {mount_point}")
    secret_path = "/".join(parts[1:])
    logger.info(f"check secret_path: {secret_path}")

    try:
        client.secrets.kv.v2.delete_metadata_and_all_versions(
            mount_point=mount_point,
            path=secret_path
        )
        logger.info(f"Successfully deleted Vault secret and metadata at {path}")
    except hvac.exceptions.InvalidPath:
        logger.warning(f"Secret at {path} already gone or never existed. Skipping.")
    except Exception as e:
        logger.error(f"Failed to delete Vault secret: {e}")
        # raise a TemporaryError so Kopf retries the deletion
        raise kopf.TemporaryError(f"Vault deletion failed: {e}", delay=30)

def ensure_vault_secret(path: str, logger):
    client = get_vault_client(logger)
    mount_point, secret_path = path.split('/', 1)
    data_path = f"{mount_point}/data/{secret_path}"
    logger.info(f"check mount_point: {mount_point}")
    logger.info(f"check secret_path: {secret_path}")
    
    try:
        read_response = client.secrets.kv.v2.read_secret_version(
            mount_point=mount_point,
            path=secret_path
        )
        version = read_response['data']['metadata']['version']
        logger.info(f"Key already exists in Vault at {path} (v{version})")
        return version
    except hvac.exceptions.InvalidPath:
        logger.info(f"Key missing at {path}. Generating new LUKS key...")
        new_key = secrets.token_hex(32)
        logger.info(f"check new_key: {new_key}")
        create_response = client.secrets.kv.v2.create_or_update_secret(
            mount_point=mount_point,
            path=secret_path,
            secret=dict(key=new_key)
        )
        logger.info(f"check create_response:{create_response}")
        return create_response['data']['version']

#Operator logic

@kopf.on.create("crypto.example.com", "v1", "encryptedvolumes")
def handle_volume_creation(spec, name, namespace, logger, body, **kwargs):
    owner_uid = body['metadata']['uid']
    logger.info(f"check owner_uid: {owner_uid}")
    institution = spec.get("institution", "default")
    logger.info(f"check institution: {institution}")
    
    # paths and names
    vault_path = f"secret/tenants/{institution}/luks-keys/{name}"
    mount_point, secret_path = vault_path.split('/', 1)
    logger.info(f"check mount_point: {mount_point}")
    logger.info(f"check secret_path: {secret_path}")
    data_path = f"{mount_point}/data/{secret_path}"
    
    pvc_name = spec.get("pvcName", f"pvc-{name}")
    mapper_name = f"luks-{name}"
    INTERNAL_DEVICE_PATH = "/dev/encrypted-block"

    # ensure external resources
    version = ensure_vault_secret(vault_path, logger)
    ensure_block_pvc(
        namespace=namespace,
        pvc_name=pvc_name,
        storage_class=spec.get("storageClassName", "csi-cinder"),
        size=spec.get("size", "1Gi"),
        owner_name=name,
        owner_uid=owner_uid,
        logger=logger
    )

    # create pod manifest
    vault_role = os.environ.get('VAULT_ROLE', 'luks-operator-role')
    logger.info(f"check vault_role: {vault_role}")
    luks_type = spec.get("luksType", "luks2")
    fs_type = spec.get("filesystem", "ext4")

    pod_manifest = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": f"encrypt-{name}",
            "namespace": namespace,
            "labels": {"app": "encrypted-volume"},
            "ownerReferences": [{
                "apiVersion": "crypto.example.com/v1",
                "kind": "EncryptedVolume",
                "name": name,
                "uid": owner_uid,
                "controller": True
            }],
            "annotations": {
                "vault.hashicorp.com/agent-inject": "true",
                "vault.hashicorp.com/role": vault_role,
                "vault.hashicorp.com/agent-init-first": "true",
                "vault.hashicorp.com/agent-inject-secret-luks.key": vault_path,
                "vault.hashicorp.com/agent-inject-template-luks.key":
                    '{{ with secret "' + data_path + '" }}{{ .Data.data.key }}{{ end }}',
            }
        },
        "spec": {
            "restartPolicy": "Never",
            "serviceAccountName": "encrypted-volume-operator",
            "imagePullSecrets":[{
                "name": "gitlab-regcred-cam",
            }               
            ],  
            "containers": [{
                "name": "jupyter-notebook",
                "image": "jupyter/base-notebook",
                "securityContext": {"privileged": False, "runAsUser": 1000}, # secure
                "volumeMounts": [{
                    "name": "shared-data",
                    "mountPath": "/home/jovyan/work",
                    "subPath": "secure-data",
                    "mountPropagation": "HostToContainer"
                }]
            }],
            "initContainers": [
                {
                    "name": "luks-setup",
                    "image": "registry.gitlab.developers.cam.ac.uk/rcs/platforms/cloud-services/k8s-cinder-luks-operator-config:luks-storage-tool-v1",
                    "env": [{"name": "CRYPTSETUP_UDEV_SYNC_DISABLE", "value": "1"}],
                    "securityContext": {
                        "privileged": True,
                        "appArmorProfile": {
                            "type": "Localhost",
                            "localhostProfile": "k8s-luks-restricted"
                        }
                        
                        },
                    "command": ["sh", "-c"],
                    "args": [f"""
                                set -eux
                                DEVICE_PATH="{INTERNAL_DEVICE_PATH}"
                                KEY_FILE="/vault/secrets/luks.key"
                                mapper_name="{mapper_name}"

                                while [ ! -f $KEY_FILE ]; do sleep 1; done

                                # Kill "ghost" mappers
                                cryptsetup luksClose $mapper_name || true

                                # 1. Format if fresh
                                if ! cryptsetup isLuks "$DEVICE_PATH" >/dev/null 2>&1; then
                                    cryptsetup luksFormat "$DEVICE_PATH" "$KEY_FILE" --batch-mode --type {luks_type}
                                    cryptsetup luksOpen "$DEVICE_PATH" $mapper_name --key-file "$KEY_FILE"
                                    mkfs.{fs_type} /dev/mapper/$mapper_name
                                    cryptsetup luksClose $mapper_name
                                fi

                                # 2. Open
                                if [ ! -e /dev/mapper/$mapper_name ]; then
                                    cryptsetup luksOpen "$DEVICE_PATH" $mapper_name --key-file "$KEY_FILE"
                                fi

                                mkdir -p /mnt/shared/secure-data
                                mount /dev/mapper/$mapper_name /mnt/shared/secure-data

                                chown -R 1000:100 /mnt/shared/secure-data
                                chmod -R 770 /mnt/shared/secure-data

                                echo " Encryption prepared and mounted to /mnt/shared/secure-data"


                    """],
                    "volumeDevices": [{"devicePath": INTERNAL_DEVICE_PATH, "name": "block-pvc"}],
                    "volumeMounts": [{
                        "name": "shared-data",
                        "mountPath": "/mnt/shared",
                        "mountPropagation": "Bidirectional"
                    }]
                }
            ],
            "volumes": [
                {"name": "block-pvc", "persistentVolumeClaim": {"claimName": pvc_name}},
                {"name": "shared-data", "emptyDir": {}}
            ]
        }
    }

    v1 = kubernetes.client.CoreV1Api()
    try:
        v1.create_namespaced_pod(namespace=namespace, body=pod_manifest)
    except ApiException as e:
        if e.status != 409: raise

    return {
        "vaultInfo": {"path": vault_path, "currentVersion": version},
        "volumeStatus": "Ready"
    }


@kopf.on.event('', 'v1', 'pods', labels={'app': 'encrypted-volume'})
def capture_node_name(event, logger, **kwargs):
    # ignore deletion events
    logger.info(f"check: capture_node_name s called!!!")
    logger.info(f"check event: {event}")
    if event['type'] == 'DELETED':
        return

    pod = event['object']
    logger.info(f"check pod: {pod}")
    # check if pod is currently terminataing
    if pod.get('metadata', {}).get('deletionTimestamp'):
        return

    node_name = pod.get('spec', {}).get('nodeName')
    if node_name and 'ownerReferences' in pod['metadata']:
        owner_name = pod['metadata']['ownerReferences'][0]['name']
        logger.info(f"check owner_name in capture_node_name function: {owner_name}")
        logger.info(f"check node_name in capture_node_name function: {node_name}")
        
        api = kubernetes.client.CustomObjectsApi()
        try:
            api.patch_namespaced_custom_object_status(
                group="crypto.example.com",
                version="v1",
                namespace=pod['metadata']['namespace'],
                plural="encryptedvolumes",
                name=owner_name,
                body={"status": {"nodeName": node_name}}
                
            )
            logger.info(f"Linked {owner_name} to node {node_name}")
            
        except kubernetes.client.exceptions.ApiException as e:
            if e.status == 404:
                pass
            else:
                raise
# --- Cleanup Handlers ---
@kopf.on.delete("crypto.example.com", "v1", "encryptedvolumes")
def cleanup_resources(spec, status, name, namespace, logger, **kwargs):
    logger.info(f"Cleanup triggered for {name}")
    
    node_name = status.get('nodeName')
    creation_status = status.get('handle_volume_creation', {})
    vault_path = creation_status.get('vaultInfo', {}).get('path')
    pvc_name = spec.get("pvcName", f"pvc-{name}")
    pod_name = f"encrypt-{name}" # The name of the Jupyter pod

    # kill the pod to release it from the pvc
    v1 = kubernetes.client.CoreV1Api()
    try:
        v1.delete_namespaced_pod(name=pod_name, namespace=namespace)
        logger.info(f"Evicted pod {pod_name} to release disk handles.")
    except kubernetes.client.exceptions.ApiException as e:
        if e.status != 404:
            logger.warning(f"Note: Could not delete pod {pod_name}: {e}")

    # cleanup janitor phase to close the mapper
    if node_name:
        batch_api = kubernetes.client.BatchV1Api()
        job_name = f"janitor-{name}"
        try:
            job = batch_api.read_namespaced_job(job_name, namespace)
            if not job.status.succeeded:
                if job.status.failed:
                    batch_api.delete_namespaced_job(job_name, namespace, propagation_policy='Background')
                raise kopf.TemporaryError("Waiting for Janitor Job...", delay=10)
            logger.info("Janitor successfully closed the LUKS device.")
        except kubernetes.client.exceptions.ApiException as e:
            if e.status == 404:
                spawn_janitor_job(batch_api, name, namespace, node_name)
                raise kopf.TemporaryError("Dispatched Janitor Job.", delay=10)

    # Vault Phase - destroy the key
    if spec.get('deletionPolicy') == "Delete" and vault_path:
        delete_vault_secret(vault_path, logger)
        logger.info(f"Vault key destroyed: {vault_path}")

    # PVC Phase - Delete the storage
    if spec.get('deletionPolicy') == "Delete":
        try:
            v1.delete_namespaced_persistent_volume_claim(pvc_name, namespace)
            logger.info(f"PVC {pvc_name} deleted.")
        except kubernetes.client.exceptions.ApiException as e:
            if e.status != 404: raise

    logger.info(f"Final cleanup for {name} complete. Finalizer will be removed.")



def spawn_janitor_job(api, name, namespace, node_name):
    job_name = f"janitor-{name}"
    
    manifest = {
        "metadata": {"name": job_name},
        "spec": {
            "template": {
                "spec": {
                    "nodeName": node_name,
                    "hostPID": True,
                    "hostNetwork": True,
                    "restartPolicy": "Never",
                    "imagePullSecrets":[{
                        "name": "gitlab-regcred-cam",
                    }               
                    ],  
                    "containers": [{
                        "name": "cleanup",
                        "image": "registry.gitlab.developers.cam.ac.uk/rcs/platforms/cloud-services/k8s-cinder-luks-operator-config:luks-storage-tool-v1",
                        "securityContext": {
                            "privileged": True,
                            "appArmorProfile": {
                                "type": "Localhost",
                                "localhostProfile": "k8s-luks-restricted"
                            }
                            },
                        "command": ["sh", "-c"],
                    "args": [f"""
                        set -x

                        MAPPER_NAME="luks-{name}"
                        DEV_PATH="/dev/mapper/$MAPPER_NAME"
                        
                        echo "#### Identify Device ####"
                        MAJMIN=$(nsenter -t 1 -m -p -- lsblk -dno MAJ:MIN "$DEV_PATH" | tr -d '[:space:]' || true)
                        echo "Cleaned Device ID: '$MAJMIN'"

                        echo "#### Kill ALL Mounts and Processes ####"
                        if [ ! -z "$MAJMIN" ]; then
                            echo "Killing processes holding $DEV_PATH..."
                            nsenter -t 1 -m -p -- fuser -mvk "$DEV_PATH" || true
                            
                            # 2. Search for the ID in mountinfo (now without space errors)
                            TARGETS=$(nsenter -t 1 -m -p -- grep "$MAJMIN" /proc/self/mountinfo | awk '{{print $5}}' || true)
                            for mnt in $TARGETS; do
                                echo "Force clearing mount: $mnt"
                                nsenter -t 1 -m -p -- fuser -mvk "$mnt" || true
                                nsenter -t 1 -m -p -- umount -fl "$mnt" || true
                            done
                        fi

                        echo "#### the close ####"
                        if ! timeout 10s nsenter -t 1 -m -p -- cryptsetup luksClose "$MAPPER_NAME"; then
                            echo "cryptsetup busy. Applying Device-Mapper force-deferred removal..."
                            nsenter -t 1 -m -p -- dmsetup remove --force "$MAPPER_NAME" || true
                            nsenter -t 1 -m -p -- dmsetup remove --deferred "$MAPPER_NAME" || true
                        fi

                        echo "janitor script finished."
                    """]

                    }],
                    "volumes": [
                        {"name": "host-dev", "hostPath": {"path": "/dev"}},
                        {"name": "host-proc", "hostPath": {"path": "/proc"}}
                    ]
                }
            },
            "backoffLimit": 0,
            "ttlSecondsAfterFinished": 60
        }
    }
    api.create_namespaced_job(namespace=namespace, body=manifest)

# Change the decorator to watch the specific status field
@kopf.on.field("crypto.example.com", "v1", "encryptedvolumes", field='metadata.annotations.vaultversion')
def rotate_luks_key(spec, status, name, namespace, logger, body, new, **kwargs):
    if not new:
        return
    new_version = int(new)
    
    logger.info(f"ROTATION TRIGGERED! Target Version: {new_version}")
    last_processed = status.get('rotate_luks_key', {}).get('version')
    if last_processed == new_version:
        logger.info(f"Version {new_version} already handled. No action.")
        return

    if new_version <= 1:
        return

    # setup vault
    client = get_vault_client(logger)
    institution = spec.get("institution", "default")
    vault_path = f"secret/tenants/{institution}/luks-keys/{name}"
    pvc_name = spec.get("pvcName", f"pvc-{name}")
    mount_point, secret_path = vault_path.split('/', 1)

    secret_response = client.secrets.kv.v2.read_secret_version(mount_point=mount_point, path=secret_path)
    new_key = secret_response['data']['data']['key']
   
    old_response = client.secrets.kv.v2.read_secret_version(
        mount_point=mount_point, path=secret_path, version=new_version - 1
    )
    old_key = old_response['data']['data']['key']

    node_name = status.get('nodeName')
    if not node_name:
        raise kopf.TemporaryError("Waiting for volume to be assigned to a node...", delay=30)

    #create job manifest
    job_name = f"rekey-{name}-v{new_version}"
    rekey_manifest = {
        "metadata": {"name": job_name},
        "spec": {
            "ttlSecondsAfterFinished": 60,
            "template": {
                "spec": {
                    "nodeName": node_name,
                    "restartPolicy": "Never",
                    "containers": [{
                        "name": "rekey",
                        "image": "registry.gitlab.developers.cam.ac.uk/rcs/platforms/cloud-services/k8s-cinder-luks-operator-config:luks-storage-tool-v1",
                        "securityContext": {
                            "privileged": True,
                            "appArmorProfile": {"type": "Localhost", "localhostProfile": "k8s-luks-restricted"}
                        },
                        "command": ["sh", "-c"],
                        "args": [f"""
                            set -eux
                            DEV="/dev/encrypted-block"
                            echo -n '{new_key}' | cryptsetup luksAddKey "$DEV" --key-file <(echo -n '{old_key}') --batch-mode
                            echo -n '{old_key}' | cryptsetup luksRemoveKey "$DEV" --batch-mode
                            echo "Key rotation to v{new_version} complete."
                        """],
                        "volumeDevices": [{"devicePath": "/dev/encrypted-block", "name": "block-pvc"}]
                    }],
                    "volumes": [
                        {"name": "block-pvc", "persistentVolumeClaim": {"claimName": pvc_name}},
                        {"name": "host-dev", "hostPath": {"path": "/dev"}}
                    ]
                }
            }
        }
    }

    # check or create the job
    batch_api = kubernetes.client.BatchV1Api()
    try:
        job = batch_api.read_namespaced_job(name=job_name, namespace=namespace)
        
        if not job.status.succeeded:
            if job.status.failed and job.status.failed > 0:
                logger.error(f"Rekey Job {job_name} failed.")
                raise kopf.PermanentError(f"Rekey Job failed for v{new_version}")
           
            raise kopf.TemporaryError(f"Waiting for Job {job_name} to finish...", delay=10)
           
        logger.info(f"Job {job_name} succeeded!")

    except kubernetes.client.exceptions.ApiException as e:
        if e.status == 404:
            batch_api.create_namespaced_job(namespace=namespace, body=rekey_manifest)
            raise kopf.TemporaryError(f"Dispatched Rekey Job {job_name}", delay=10)
        else:
            raise
    return {'version': new_version}

@kopf.timer("crypto.example.com", "v1", "encryptedvolumes", interval=30.0)
def sync_vault_version(spec, name, namespace, patch, logger, **kwargs):
    logger.info(f"Timer: Checking Vault for {name}")
    
    try:
        client = get_vault_client(logger)
        institution = spec.get("institution", "default")
        vault_path = f"secret/tenants/{institution}/luks-keys/{name}"
        mount_point, secret_path = vault_path.split('/', 1)
        
        secret_response = client.secrets.kv.v2.read_secret_version(mount_point=mount_point, path=secret_path)
        vault_version = secret_response['data']['metadata']['version']
        patch.metadata.annotations['vaultversion'] = str(vault_version)
        
        # Keep status updated for visibility
        patch.status['current_vault_version'] = vault_version
        
        logger.info(f"Timer found Vault v{vault_version}. Synced to 'vaultversion' annotation.")

    except Exception as e:
        logger.error(f"Timer error: {e}")

