#!/usr/bin/bash
# make-vm.sh <name> <pubkey-file>... : a fresh Fedora 44 VM on this libvirt
# host's default network, root by those keys, a seat user `alice`. Run as root
# on the libvirt host; prints the VM's address. Destroy it with
#   virsh destroy <name>; virsh undefine <name> --remove-all-storage
set -euo pipefail
name=${1:?usage: make-vm.sh <name> <pubkey-file>...}; shift
(( $# )) || { echo "make-vm.sh: at least one public key file" >&2; exit 2; }
img=Fedora-Cloud-Base-Generic-44-1.7.x86_64.qcow2
url=https://download.fedoraproject.org/pub/fedora/linux/releases/44/Cloud/x86_64/images
base=/var/lib/libvirt/images/base
mkdir -p "$base"
if [[ ! -f $base/$img ]]; then
    curl -fsSL -o "$base/$img.part" "$url/$img"
    curl -fsSL "$url/Fedora-Cloud-44-1.7-x86_64-CHECKSUM" | grep -F "($img)" > "$base/$img.sha256"
    (cd "$base" && mv "$img.part" "$img" && sha256sum -c "$img.sha256" >/dev/null) || { rm -f "$base/$img"; exit 1; }
fi
work=$(mktemp -d); trap 'rm -rf "$work"' EXIT
{
    echo '#cloud-config'
    echo "hostname: $name"
    echo 'disable_root: false'
    echo 'ssh_pwauth: false'
    echo 'users:'
    echo '  - name: root'
    echo '    ssh_authorized_keys:'
    for k in "$@"; do sed 's/^/      - /' "$k"; done
    echo '  - {name: alice, shell: /bin/bash, lock_passwd: true}'
} > "$work/user-data"
printf 'instance-id: %s-%s\nlocal-hostname: %s\n' "$name" "$(date +%s)" "$name" > "$work/meta-data"
disk=/var/lib/libvirt/images/$name.qcow2
qemu-img create -q -f qcow2 -F qcow2 -b "$base/$img" "$disk" 60G
virt-install -q --name "$name" --memory 8192 --vcpus 4 --import --disk "$disk,bus=virtio" \
    --osinfo fedora-unknown --network network=default,model=virtio \
    --cloud-init "user-data=$work/user-data,meta-data=$work/meta-data" --noautoconsole --graphics none
virsh -q dumpxml "$name" >/dev/null
for _ in $(seq 60); do
    a=$(virsh -q domifaddr "$name" 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -1)
    [[ -n $a ]] && { echo "$a"; exit 0; }
    sleep 3
done
echo "make-vm.sh: $name got no address" >&2; exit 1
