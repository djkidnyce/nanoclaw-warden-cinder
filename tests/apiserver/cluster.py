"""Boot a real etcd and kube-apiserver (envtest binaries) with an admin client cert.

No kubelet or controllers run, so nothing schedules. What does run is everything the
API server enforces on its own: schema and CRD validation, RBAC, impersonation,
Pod Security admission and ValidatingAdmissionPolicy.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _sh(*args, cwd=None):
    subprocess.run(args, check=True, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class Cluster:
    def __init__(self, bin_dir: str):
        self.bin = Path(bin_dir)
        self.dir = Path(tempfile.mkdtemp(prefix="nanoclaw-apiserver-"))
        self.procs: list[subprocess.Popen] = []
        self.kubeconfig = self.dir / "kubeconfig"

    def start(self) -> "Cluster":
        d = self.dir
        # Certificates: a CA, an admin client in system:masters, and a service account key pair.
        _sh("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", "/CN=test-ca",
            "-keyout", "ca.key", "-out", "ca.crt", cwd=d)
        _sh("openssl", "req", "-newkey", "rsa:2048", "-nodes", "-subj", "/O=system:masters/CN=admin",
            "-keyout", "admin.key", "-out", "admin.csr", cwd=d)
        _sh("openssl", "x509", "-req", "-in", "admin.csr", "-CA", "ca.crt", "-CAkey", "ca.key", "-CAcreateserial",
            "-days", "2", "-out", "admin.crt", cwd=d)
        _sh("openssl", "genrsa", "-out", "sa.key", "2048", cwd=d)
        _sh("openssl", "rsa", "-in", "sa.key", "-pubout", "-out", "sa.pub", cwd=d)

        etcd_port, peer_port, api_port = _port(), _port(), _port()
        self.procs.append(subprocess.Popen(
            [str(self.bin / "etcd"), "--data-dir", str(d / "etcd"),
             "--listen-client-urls", f"http://127.0.0.1:{etcd_port}",
             "--advertise-client-urls", f"http://127.0.0.1:{etcd_port}",
             "--listen-peer-urls", f"http://127.0.0.1:{peer_port}",
             "--initial-advertise-peer-urls", f"http://127.0.0.1:{peer_port}",
             "--initial-cluster", f"default=http://127.0.0.1:{peer_port}",
             "--unsafe-no-fsync"],
            stdout=open(d / "etcd.log", "w"), stderr=subprocess.STDOUT))
        self.procs.append(subprocess.Popen(
            [str(self.bin / "kube-apiserver"),
             "--etcd-servers", f"http://127.0.0.1:{etcd_port}",
             "--bind-address", "127.0.0.1", "--secure-port", str(api_port),
             "--cert-dir", str(d / "serving"),
             "--client-ca-file", str(d / "ca.crt"),
             "--service-account-issuer", "https://kubernetes.default.svc",
             "--service-account-key-file", str(d / "sa.pub"),
             "--service-account-signing-key-file", str(d / "sa.key"),
             "--service-cluster-ip-range", "10.96.0.0/16",
             "--authorization-mode", "RBAC",
             # Quota admission needs the quota controller to publish status, which is not running here.
             "--disable-admission-plugins", "ResourceQuota",
             "--allow-privileged=false"],
            stdout=open(d / "apiserver.log", "w"), stderr=subprocess.STDOUT))
        self.kubeconfig.write_text(f"""apiVersion: v1
kind: Config
clusters:
- name: test
  cluster:
    server: https://127.0.0.1:{api_port}
    insecure-skip-tls-verify: true
users:
- name: admin
  user:
    client-certificate: {d / 'admin.crt'}
    client-key: {d / 'admin.key'}
contexts:
- name: test
  context: {{cluster: test, user: admin}}
current-context: test
""")
        deadline = time.time() + 90
        while time.time() < deadline:
            r = self.kubectl("get", "--raw", "/readyz", check=False)
            if r.returncode == 0 and "ok" in r.stdout:
                return self
            time.sleep(1)
        raise RuntimeError("kube-apiserver did not become ready:\n" + (d / "apiserver.log").read_text()[-3000:])

    def kubectl(self, *args, stdin: str | None = None, check: bool = True, as_user: str | None = None):
        cmd = [str(self.bin / "kubectl"), "--kubeconfig", str(self.kubeconfig)]
        if as_user:
            cmd += ["--as", as_user]
        cmd += list(args)
        r = subprocess.run(cmd, input=stdin, capture_output=True, text=True)
        if check and r.returncode != 0:
            raise RuntimeError(f"kubectl {' '.join(args)} failed:\n{r.stdout}\n{r.stderr}")
        return r

    def stop(self):
        for p in reversed(self.procs):
            p.terminate()
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()
        shutil.rmtree(self.dir, ignore_errors=True)


def envtest_dir() -> str | None:
    d = os.environ.get("ENVTEST_DIR")
    if d and all((Path(d) / b).exists() for b in ("etcd", "kube-apiserver", "kubectl")):
        return d
    return None
