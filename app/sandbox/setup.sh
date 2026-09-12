#!/bin/sh
# Runs as root inside a fresh mount+pid namespace, hardens the mount table, then execs
# the real command (as the environment user when one was given). Arguments:
#   workspace cwd uid gid command...
set -e
ws="$1"; cwd="$2"; uid="$3"; gid="$4"; shift 4
# Hold the workspace open: once /tmp is overmounted its path may no longer resolve, and
# the bind below goes through this descriptor rather than the path.
exec 3<"$ws"
mount --make-rprivate /
mount -o remount,bind,ro /
# Docker-style bind mounts (/etc/hosts and friends) are separate mounts that the root
# remount does not touch; best effort, since some (proc, sys, devpts) refuse.
while read -r _ mp _; do
  case "$mp" in
    /|/proc*|/sys*|/dev*) ;;
    *) mount -o remount,bind,ro "$mp" 2>/dev/null || true ;;
  esac
done < /proc/self/mounts
mount -t tmpfs -o size=256m,mode=1777 tmpfs /tmp
mkdir -p "$ws"
mount --no-canonicalize --bind /proc/self/fd/3 "$ws"
mount -o remount,bind,rw "$ws"
exec 3<&-
# The cwd Popen set refers to the pre-mount view of the tree, which is now read-only;
# re-enter it so the path resolves through the writable bind mount.
cd "$cwd"
if [ "$uid" != "0" ]; then
  exec setpriv --reuid "$uid" --regid "$gid" --clear-groups -- "$@"
fi
exec "$@"
