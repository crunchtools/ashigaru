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
# Once per host, and again after changing the Worker: idempotent.
ansible-playbook -i <inventory>/hosts.yml playbooks/host.yml [--limit <host>]

# Add a seat. Whoever adds it picks its name.
ASHIGARU_API_KEY_FILE=<desk admin key> ansible-playbook -i <inventory>/hosts.yml playbooks/seat.yml \
  -e seat_host=<host> -e seat_name=<name> -e seat_kind=max|vertex|prime [-e "seat_desk=<Desk name>"]

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
Max and Prime seats still need signing in once. Removing reverses each step.

## Layout

```
playbooks/   host.yml, seat.yml, adopt.yml: thin, one role each
roles/
  seat_host/ everything a host needs before its first seat
    files/   fence hook and loader, the registry (ashigaru-seats), the vault
             helper (ashigaru-kv), the grant editor (ashigaru-grant), audit
    templates/ seat-fence.conf, sshd root drop-in
  seat/      one seat: present.yml adds, absent.yml removes
    templates/seat.container.j2: the quadlet for all three kinds
inventory.example/  the variables, with documentation addresses
```

The inventory is the record of hosts and of what each kind is on each host.
Each host's registry (`/etc/ashigaru/seats/<name>.json`) is the record of its
seats; `seats.tsv`, which the fence check reads, is generated from it. A new
seat kind is a new `seat_kinds` entry; a new host is a new `host_vars` file.

The tools are tested in `tests/test_worker_tools.py`.

## Inventory

Groups: `seat_hosts` run seats; `servers` take seat logins but run none;
`seat_audit_hosts` is every host that keeps a key map. Variables, by file, are
documented in `inventory.example/`. The ones every seat host needs:
`seat_user`, `seat_network`, `seat_gateway`, `seat_gateway_unit`,
`seat_gateway_profiles`, `seat_services_dir` and its siblings, `seat_pools`,
`seat_fence_ssh`, `seat_ssh_from`, `seat_kinds`; and in `all`,
`seat_access_targets` and `seat_root_from`.

## Not yet

- The host role expects the host's OpenBao (with its operator AppRole) and its
  local Trentina gateway to be running already; it checks and stops if not.
  Building those from nothing on a fresh host is the next step.
- Seats run rootless under `seat_user` on desktops. Servers that run seats
  rootful (Exedore's `max1`-`4`) are not managed by the Worker yet.
