"""Operator-only manual takeover. Never run activate/failback without owner approval.

A health timeout is NEVER evidence. The reviewed primary must contain the
startup fence guard before this tool can disable restart and stop its gateway.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import uuid
from typing import Any

from standby.policy import PolicyDenied


class Operator:
    def __init__(self, main_wrapper: str, backup_wrapper: str):
        self.main_wrapper, self.backup_wrapper = main_wrapper, backup_wrapper

    def main(self, cmd: str) -> str:
        return self._call(self.main_wrapper, cmd)

    def backup(self, cmd: str) -> str:
        return self._call(self.backup_wrapper, cmd)

    @staticmethod
    def _call(wrapper: str, cmd: str) -> str:
        completed = subprocess.run(
            [wrapper, cmd, "--timeout", "90"],
            capture_output=True,
            text=True,
            timeout=125,
        )
        if completed.returncode:
            # Do not reproduce possibly sensitive remote stdout/stderr.
            raise PolicyDenied(
                "Remote operation failed; state may remain fenced. Inspect manually"
            )
        return completed.stdout.strip()


def require_commit(commit: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise PolicyDenied("An exact reviewed commit is required")


def activate(
    operator: Operator, commit: str, *, approve_primary_stop: bool = False
) -> dict[str, Any]:
    require_commit(commit)
    if not approve_primary_stop:
        raise PolicyDenied("Explicit primary-disruption approval required")
    # Check prerequisites BEFORE touching primary. Deployment review and audience
    # approval remain operator responsibilities; no credentials are read/printed.
    operator.backup(
        'set -e; test "$(cat /opt/maxwell-standby/REVIEWED_COMMIT)" = '
        + shlex.quote(commit)
        + "; "
        "cd /opt/maxwell-standby/app; /opt/maxwell-standby/venv/bin/python -m standby.preflight; "
        "test -s /etc/maxwell-standby/audience.env; test ! -e /run/maxwell-standby/activation.json; test ! -e /run/maxwell-standby/owner.json; "
        'test "$(systemctl show maxwell-standby -p MainPID --value)" = 0'
    )
    observed = json.loads(
        operator.main(
            "python3 - <<'PY'\n"
            "import hashlib,json,subprocess\n"
            "p='/root/maxwell'\n"
            "sha=subprocess.check_output(['git','-C',p,'rev-parse','HEAD'],text=True).strip()\n"
            "assert not subprocess.check_output(['git','-C',p,'status','--porcelain','--untracked-files=no'],text=True).strip()\n"
            "assert 'assert_primary_unfenced(Config.DATA_DIR)' in open(p+'/bot.py').read()\n"
            "d=json.loads(subprocess.check_output(['docker','inspect','maxwell'],text=True))[0]\n"
            "env=dict(x.split('=',1) for x in d['Config']['Env'] if '=' in x)\n"
            "assert env.get('DATA_DIR','data') in {'data','/app/data'}\n"
            "assert d['Config']['WorkingDir']=='/app'\n"
            "assert any(m.get('Source')=='/root/maxwell' and m.get('Destination')=='/app' for m in d['Mounts'])\n"
            "assert not any(m.get('Destination','').startswith('/app/') for m in d['Mounts'])\n"
            "assert d['Config']['Entrypoint']==['/entrypoint.sh'] and not d['Config']['Cmd']\n"
            "assert subprocess.check_output(['docker','exec','maxwell','python','-c','import os; from config import Config; print(os.path.realpath(Config.DATA_DIR))'],text=True).strip()=='/app/data'\n"
            "assert 'bot.py' in open(p+'/docker/supervisor.py').read()\n"
            "assert subprocess.check_output(['docker','exec','maxwell','sha256sum','/entrypoint.sh'],text=True).split()[0]==hashlib.sha256(open(p+'/docker/entrypoint.sh','rb').read()).hexdigest()\n"
            "print(json.dumps({'commit':sha,'restart':d['HostConfig']['RestartPolicy']['Name']}))\nPY"
        )
    )
    if observed.get("commit") != commit or observed.get("restart") not in {
        "no",
        "always",
        "unless-stopped",
    }:
        raise PolicyDenied(
            "Reviewed guarded primary revision/restart policy not verified"
        )
    fence_id = uuid.uuid4().hex
    marker = {
        "fence_id": fence_id,
        "reviewed_commit": commit,
        "previous_restart": observed["restart"],
    }
    # Exclusive create: do not overwrite an existing fence or hide its owner.
    operator.main(
        "set -e\npython3 - <<'PY'\nimport os,json\n"
        "fd=os.open('/root/maxwell/data/standby-primary-fence.json',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)\n"
        "with os.fdopen(fd,'w') as f:\n f.write("
        + repr(json.dumps(marker))
        + ");f.flush();os.fsync(f.fileno())\n"
        "directory=os.open('/root/maxwell/data',os.O_RDONLY|os.O_DIRECTORY)\n"
        "try: os.fsync(directory)\nfinally: os.close(directory)\nPY\n"
        "docker update --restart=no maxwell >/dev/null && docker stop --time 30 maxwell >/dev/null"
    )
    proof = json.loads(
        operator.main(
            "python3 - <<'PY'\nimport json,subprocess\n"
            "d=json.loads(subprocess.check_output(['docker','inspect','maxwell'],text=True))[0]\n"
            "m=json.load(open('/root/maxwell/data/standby-primary-fence.json'))\n"
            "print(json.dumps({'running':d['State']['Running'],'pid':d['State']['Pid'],"
            "'restart':d['HostConfig']['RestartPolicy']['Name'],'fence_id':m['fence_id']}))\nPY"
        )
    )
    if proof != {"running": False, "pid": 0, "restart": "no", "fence_id": fence_id}:
        raise PolicyDenied(
            "Primary fencing not positively verified; backup remains disconnected"
        )
    record = {
        "state": "manually_fenced",
        "primary_running": False,
        "primary_restart_policy": "no",
        "primary_fence_id": fence_id,
        "reviewed_commit": commit,
        "previous_restart": observed["restart"],
    }
    operator.backup(
        "set -e\ninstall -d -m 700 /run/maxwell-standby\npython3 - <<'PY'\nimport os\n"
        "fd=os.open('/run/maxwell-standby/activation.json',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)\n"
        "with os.fdopen(fd,'w') as f:\n f.write("
        + repr(json.dumps(record))
        + ");f.flush();os.fsync(f.fileno())\nPY\n"
        "systemctl start maxwell-standby && systemctl is-active --quiet maxwell-standby"
    )
    return record


def failback(operator: Operator, *, approve_primary_start: bool = False) -> None:
    if not approve_primary_start:
        raise PolicyDenied("Explicit failback approval required")
    # Stop and verify backup FIRST, while primary remains durably fenced.
    record = json.loads(
        operator.backup(
            "set -e; systemctl stop maxwell-standby; "
            'test "$(systemctl show maxwell-standby -p MainPID --value)" = 0; '
            'test "$(systemctl show maxwell-standby -p ActiveState --value)" = inactive; '
            "cat /run/maxwell-standby/owner.json"
        )
    )
    fence_id = record.get("primary_fence_id", "")
    if not re.fullmatch(r"[a-f0-9]{32}", fence_id):
        raise PolicyDenied("Invalid activation receipt; primary remains fenced")
    operator.backup("rm -f /run/maxwell-standby/activation.json")
    previous = record.get("previous_restart")
    reviewed = record.get("reviewed_commit", "")
    require_commit(reviewed)
    if previous not in {"no", "always", "unless-stopped"}:
        raise PolicyDenied("Recovery receipt missing original restart policy")
    operator.main(
        "python3 - <<'PY'\nimport os,json,subprocess\nfrom pathlib import Path\n"
        "p=Path('/root/maxwell/data/standby-primary-fence.json')\n"
        "assert subprocess.check_output(['git','-C','/root/maxwell','rev-parse','HEAD'],text=True).strip()=="
        + repr(reviewed)
        + "\n"
        "d=json.loads(subprocess.check_output(['docker','inspect','maxwell'],text=True))[0]\n"
        "if p.exists():\n"
        " m=json.loads(p.read_text());assert m['fence_id']==" + repr(fence_id) + "\n"
        " assert not d['State']['Running'] and d['State']['Pid']==0\n"
        " p.unlink()\n"
        " directory=os.open(p.parent,os.O_RDONLY|os.O_DIRECTORY)\n"
        " try: os.fsync(directory)\n finally: os.close(directory)\n"
        "subprocess.run(['docker','update','--restart='+"
        + repr(previous)
        + ",'maxwell'],check=True,stdout=subprocess.DEVNULL)\n"
        "if not d['State']['Running']:\n subprocess.run(['docker','start','maxwell'],check=True,stdout=subprocess.DEVNULL)\n"
        "d=json.loads(subprocess.check_output(['docker','inspect','maxwell'],text=True))[0]\n"
        "assert d['State']['Running'] and d['State']['Pid']>0\nPY"
    )
    # Owner evidence is not an activation credential. Retain it through failures
    # so ordered failback can safely be retried after a transient main failure.
    operator.backup("rm /run/maxwell-standby/owner.json")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["plan", "activate", "failback"])
    parser.add_argument("--main-wrapper", default="/workspace/tools/bin/zrun")
    parser.add_argument("--backup-wrapper", default="/workspace/tools/bin/brun")
    parser.add_argument("--reviewed-commit", default="")
    parser.add_argument("--approve-primary-stop", action="store_true")
    parser.add_argument("--approve-primary-start", action="store_true")
    args = parser.parse_args()
    if args.action == "plan":
        print(
            "Manual only: review/deploy guarded revision; approve audience and config snapshot; "
            "provision token; explicitly approve primary stop; verify durable fence+stopped PID; "
            "only then start standby. Failback stops/verifies standby before unfencing primary. "
            "No health-timeout takeover and no automatic failover."
        )
        return
    operator = Operator(args.main_wrapper, args.backup_wrapper)
    if args.action == "activate":
        activate(
            operator,
            args.reviewed_commit,
            approve_primary_stop=args.approve_primary_stop,
        )
    else:
        failback(operator, approve_primary_start=args.approve_primary_start)


if __name__ == "__main__":
    main()
