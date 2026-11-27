# Ashigaru Worker

Ansible that builds and runs Ashigaru seats: a host made ready for seats, then
one seat at a time, added or removed by name. Three kinds of seat:

| Kind | Billing | Fenced | Per host |
|------|---------|--------|----------|
| `max` | a Max subscription | yes | many |
| `vertex` | Vertex AI | yes | many |
| `prime` | per its env file | no; it is the operator | one (PID slot 0) |

## Use

From this directory, with a private inventory (start from
`inventory.example/`):

```sh
# A new host only: its OpenBao and its local gateway, from nothing. Makes
# only what is missing, so on a host that has them it changes nothing.
ansible-playbook -i <inventory>/hosts.yml playbooks/bootstrap.yml --limit <host>

# Once per host, and again after changing the Worker: idempotent.
ansible-playbook -i <inventory>/hosts.yml playbooks/host.yml [--limit <host>]

# Add a seat. Whoever adds it picks its name.
ASHIGARU_API_KEY_FILE=<desk admin key> ansible-playbook -i <inventory>/hosts.yml playbooks/seat.yml \
  -e seat_host=<host> -e seat_name=<name> -e seat_kind=max|vertex|prime [-e 'seat_desk="Dolza 5 - Max"']

# Remove it, and everything it was given.
ASHIGARU_API_KEY_FILE=... ansible-playbook -i <inventory>/hosts.yml playbooks/seat.yml \
  -e seat_host=<host> -e seat_name=<name> -e seat_state=absent [-e seat_purge_data=true]

# Record seats that predate the Worker, so new seats skip their slots.
ansible-playbook -i <inventory>/hosts.yml playbooks/adopt.yml
```

Adding a seat: the host's registry gives it an address from its kind's pool and
a PID slot; an ssh key is made on tmpfs and kept in the host's OpenBao; the key
becomes a login on each target `seat_access_targets` allows its kind, and its
fingerprint joins every audit host's key map; the address joins its kind's
gateway egress grant; the Desk gets an identity and a key for it; then the unit
is written and started, and a shortcut named after it attaches to its pane.
Max and Prime seats still need signing in once. Removing reverses each step,
including the secrets srv-secrets@ delivered to tmpfs. A Desk name with spaces
needs the inner quotes above: `-e seat_desk=Dolza 5 - Max` is key=value form,
which splits on spaces.

Bootstrapping a host: OpenBao runs rootless under `seat_user` from the pinned
crunchtools/openbao image, with a static seal key and a CA made on the host,
initialised once (the root token and recovery key stay 0600 in
`~/.config/openbao/init.json`), and given kv, AppRole and the `<prefix>-host`,
`-operator`, `-admin` and `-writeback` roles. The gateway gets one profile per
seat kind; each kind's bearer token is minted on the host, its digest kept by
the gateway and the token itself only in OpenBao, as `<kind>/<kind>.env`.

## Layout

```
playbooks/   bootstrap.yml, host.yml, seat.yml, adopt.yml: thin
roles/
  openbao/   a host's OpenBao from nothing (bootstrap.yml)
  gateway/   its local Trentina gateway, a profile and token per kind
  seat_host/ everything a host needs before its first seat
    files/   fence hook and loader, the registry (ashigaru-seats), the vault
             helper (ashigaru-kv), the grant editor (ashigaru-grant), audit
    templates/ seat-fence.conf, sshd root drop-in
  seat/      one seat: present.yml adds, absent.yml removes
    templates/seat.container.j2: the quadlet for all three kinds
inventory.example/  the variables, with documentation addresses
tests/vm/    the fresh-host test: make-vm.sh and its inventory
```

The inventory is the record of hosts and of what each kind is on each host.
Each host's registry (`/etc/ashigaru/seats/<name>.json`) is the record of its
seats; `seats.tsv`, which the fence check reads, is generated from it. A new
seat kind is a new `seat_kinds` entry; a new host is a new `host_vars` file.

The tools are tested in `tests/test_worker_tools.py`. The whole Worker is
tested on a throwaway VM (`tests/vm/`): on a libvirt host, as root,
`make-vm.sh <name> <pubkeys>` prints its address; then, with
`ASHIGARU_VM=<address>` and `ASHIGARU_API_KEY_FILE` set, run bootstrap.yml,
host.yml, and seat.yml for each kind against `tests/vm/inventory`, check
`check_seat_fence vm` on the VM, and remove the seats. A host with one network
interface needs `seat_pasta_options` (the VM inventory has them): rootless
podman otherwise hides the host's own address from its seats.

## Inventory

Groups: `seat_hosts` run seats; `servers` take seat logins but run none;
`seat_audit_hosts` is every host that keeps a key map. Variables, by file, are
documented in `inventory.example/`. The ones every seat host needs:
`seat_user`, `seat_network`, `seat_gateway`, `seat_gateway_unit`,
`seat_gateway_profiles`, `seat_services_dir` and its siblings, `seat_pools`,
`seat_fence_ssh`, `seat_ssh_from`, `seat_kinds`; and in `all`,
`seat_access_targets` and `seat_root_from`.

## Not yet

- Servers that run seats rootful (Exedore's `max1`-`4`) are not managed by
  the Worker yet.
