# Sandbox tiers

A `Sandbox` has one job: spawn a process under this environment's constraints. Three
implementations exist, and the strongest the host supports is chosen at startup by
running real probes (`app/sandbox/detect.py`), not by trusting configuration.

| Tier | Requires | Gives |
|---|---|---|
| `directory` | nothing | Workspace cwd, minimal environment, POSIX rlimits. Not a security boundary. |
| `user` | root, `useradd`/`userdel` | A system user per environment (`envapi_<id>`), workspace chowned to it, privileges dropped before exec. Ordinary permissions do the isolating and `RLIMIT_NPROC` becomes meaningful. |
| `namespace` | a usable `unshare` (plus `setpriv` and `useradd` when root) | Mount + PID namespaces: root filesystem remounted read-only, the workspace bind-mounted back read-write, a private tmpfs on `/tmp`, `/proc` showing only the environment's processes, and no network namespace at all when egress is disabled. As root it also drops to the per-environment user; rootless it uses a user namespace instead. |

The probe for the namespace tier does what the tier does: it unshares mount and pid
namespaces and remounts `/` read-only inside. A Docker seccomp profile that leaves
`unshare` on disk but makes it useless fails the probe, and the tier is not claimed.

## Honesty rules

* `/health/ready` and every environment record report the active tier.
* `ENVAPI_MIN_SANDBOX_TIER` refuses to start below the configured tier. Failing loudly at
  boot beats discovering months later that everything ran on `directory`.
* A unit test asserts the detector's verdict matches what the host can actually do.

## How the namespace tier spawns

```
unshare [--user --map-root-user] --mount --pid --fork --mount-proc --kill-child [--net] -- \
  /bin/sh app/sandbox/setup.sh <workspace> <cwd> <uid> <gid> /bin/bash
```

`setup.sh` runs as root inside the fresh namespaces: it holds the workspace open on a
descriptor, makes the mount tree private, remounts `/` and every other mount it can
read-only, mounts a tmpfs on `/tmp`, binds the workspace back through the descriptor
(`--no-canonicalize`, so it works even when the workspace lives under `/tmp`), re-enters
the requested cwd so the path resolves through the writable bind, then `exec setpriv`
into the environment's user and finally the shell. `--kill-child` ensures the shell dies
with `unshare`.

Because the shell is then a descendant of `unshare` rather than the process the service
spawned, the shell record exposes both `pid` (the root process, whose group is signalled
on close) and `shell_pid` (the shell itself). The shell reports `$$` as 1 inside its
namespace; `shell_pid` is the host pid, found by walking `/proc`.

## Layout on disk under the user and namespace tiers

```
ROOT/                     0711 root
└── accounts/<acct>/<profile>/<env>/   0711 root  (traversable, not listable)
    ├── environment.json  0600 root
    ├── logs/             0755 root  (written by the service only)
    └── workspace/        0755 envapi_<id>
```

## Network

`ENVAPI_ALLOW_NETWORK=false` withholds the network namespace at the namespace tier, which
is a real guarantee (not even loopback comes up). At the other tiers it is only a recorded
request; say so to callers rather than implying more.
