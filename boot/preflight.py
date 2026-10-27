"""Pre-boot health checks for the Sentinel home server.

Run by ``boot_sentinel.bat`` before any service starts, so that a broken
environment is reported as a readable checklist instead of a service window
that flashes open and disappears.

Checks that can take down the interpreter natively (the conda MKL/LAPACK
loader is the known offender) are executed in a child process, so a crash is
reported as a failed check rather than killing the report itself.
"""

import json
import os
import socket
import subprocess
import sys
from datetime import datetime, timezone

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
WIDTH = 78

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"

results = []


def record(status, name, detail=""):
    results.append((status, name, detail))
    print(f"  [{status}] {name}" + (f"\n         {detail}" if detail else ""), flush=True)
    return status


def run_isolated(code, timeout=180):
    """Execute ``code`` in a child interpreter; survive native crashes."""
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, timeout=timeout, cwd=PROJECT_ROOT,
        )
    except subprocess.TimeoutExpired:
        return None, "timed out"
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        detail = tail[-1] if tail else ""
        return None, f"exit 0x{proc.returncode & 0xFFFFFFFF:08X} {detail}".strip()
    return (proc.stdout or "").strip(), None


def check_environment():
    prefix = os.environ.get("CONDA_PREFIX", "")
    expected = os.path.normcase(os.path.dirname(os.path.dirname(sys.executable)))
    if not prefix:
        return record(
            FAIL, "Conda environment activated",
            "CONDA_PREFIX is not set. Launching envs\\dash_env\\python.exe directly "
            "leaves Library\\bin off PATH, which makes numpy load the wrong MKL and "
            "crash the process. Start via boot_sentinel.bat.")
    library_bin = os.path.join(prefix, "Library", "bin")
    on_path = any(os.path.normcase(library_bin) == os.path.normcase(p)
                  for p in os.environ.get("PATH", "").split(os.pathsep))
    if not on_path:
        return record(FAIL, "Conda environment activated",
                      f"{library_bin} is missing from PATH")
    return record(PASS, "Conda environment activated", prefix)


def check_blas():
    out, err = run_isolated(
        "import numpy as np;"
        "from scipy.linalg import eigh;"
        "a=np.random.rand(64,64);"
        "b=a@a.T+np.eye(64);"
        "c=np.random.rand(64,64);c=c@c.T+np.eye(64);"
        "np.linalg.inv(b);eigh(b,c);"
        "print(np.__version__)")
    if err:
        return record(
            FAIL, "numpy / LAPACK (MKL) usable", 
            f"np.linalg.inv crashed: {err}. Speaker diarization calls this during "
            "model load, so the whole pipeline dies at startup.")
    return record(PASS, "numpy / LAPACK (MKL) usable", f"numpy {out}")


def check_torch():
    out, err = run_isolated(
        "import torch;"
        "print(torch.__version__, torch.cuda.is_available(),"
        "torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'cpu')")
    if err:
        return record(FAIL, "PyTorch import", err)
    version, available, name = out.split(" ", 2)
    if available != "True":
        return record(WARN, "PyTorch CUDA", f"torch {version} running on CPU - analysis will be slow")
    return record(PASS, "PyTorch CUDA", f"torch {version} on {name}")


def check_asr_dlls():
    """CTranslate2 hard-codes the CUDA 12 cuBLAS filename.

    gpu_asr holds a single shim for it (CUDA 13's cuBLAS under the _12 name);
    everything else it needs, including its cublasLt64_13.dll dependency, is
    resolved from torch\\lib. Check both halves of that arrangement.
    """
    shim = os.path.join(PROJECT_ROOT, "gpu_asr", "cublas64_12.dll")
    if not os.path.exists(shim):
        return record(WARN, "Whisper CUDA libraries",
                      "gpu_asr\\cublas64_12.dll is missing - transcription will "
                      "fail with 'Library cublas64_12.dll is not found'")
    torch_lib = os.path.join(sys.prefix, "Lib", "site-packages", "torch", "lib")
    dependency = os.path.join(torch_lib, "cublasLt64_13.dll")
    if not os.path.exists(dependency):
        return record(WARN, "Whisper CUDA libraries",
                      f"gpu_asr\\cublas64_12.dll needs cublasLt64_13.dll, which is "
                      f"not in {torch_lib}. A PyTorch build for a different CUDA "
                      "version would break GPU transcription.")
    return record(PASS, "Whisper CUDA libraries", "gpu_asr shim + torch\\lib")


def check_speech_stack():
    """Import the speech stack the way the pipeline does.

    speechbrain 1.0.3 calls torchaudio APIs that 2.9 removed, so it only
    imports once misc.torch_compat has run. Catching that here is much cheaper
    than discovering it after the models have started loading.
    """
    out, err = run_isolated(
        "import sys, os;"
        "sys.path.insert(0, os.getcwd());"
        "from misc.torch_compat import apply; apply();"
        "import speechbrain;"
        "from speechbrain.inference import EncoderClassifier;"
        "from pyannote.audio import Pipeline;"
        "print(speechbrain.__version__)")
    if err:
        return record(FAIL, "Diarization stack imports",
                      f"speechbrain/pyannote unusable: {err}")
    return record(PASS, "Diarization stack imports", f"speechbrain {out}")


def check_certificates():
    cert = os.path.join(PROJECT_ROOT, "certs", "certs", "hlias.crt")
    if not os.path.exists(cert):
        return record(FAIL, "TLS certificate", f"not found: {cert}")
    try:
        from cryptography import x509
        with open(cert, "rb") as handle:
            parsed = x509.load_pem_x509_certificate(handle.read())
        common = parsed.subject.rfc4514_string()
        expires = parsed.not_valid_after_utc
        if expires < datetime.now(timezone.utc):
            return record(FAIL, "TLS certificate", f"expired {expires:%Y-%m-%d}")
        return record(PASS, "TLS certificate",
                      f"{common.split(',')[0]} valid until {expires:%Y-%m-%d}")
    except Exception as exc:
        return record(WARN, "TLS certificate", f"present but unreadable: {exc}")


def _port_open(host, port, timeout=2.0):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _probe_mtls(host="127.0.0.1", port=8883, timeout=3.0):
    """Complete a real mTLS handshake against the sentinel listener.

    Returns (ok, detail).

    A bare TCP connect was enough to see that *something* was listening, but
    it told us nothing about whether a sentinel could actually authenticate --
    and because port 8883 is a TLS listener, opening and dropping a socket
    without ever sending a ClientHello made the broker log

        OpenSSL Error ... unexpected eof while reading
        Client 127.0.0.1 disconnected: protocol error.

    on every boot. That looked like a fault and was not one. Doing the
    handshake properly removes the noise and upgrades the check: it now
    verifies the CA, the client certificate and the key actually work
    together, which is the thing that breaks in practice (a client cert
    missing the clientAuth EKU passes a port check and still cannot connect).

    The socket is closed with unwrap() so the broker gets a TLS close_notify
    and records an ordinary disconnect.
    """
    import ssl

    certs = os.path.join(PROJECT_ROOT, "certs", "certs")
    ca = os.path.join(certs, "ca.crt")
    cert = os.path.join(certs, "hlias.crt")
    key = os.path.join(certs, "hlias.key")
    missing = [p for p in (ca, cert, key) if not os.path.exists(p)]
    if missing:
        return False, f"missing {', '.join(os.path.basename(p) for p in missing)}"

    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_verify_locations(cafile=ca)
        context.load_cert_chain(certfile=cert, keyfile=key)
        # The broker's certificate is issued for its own name, and this probe
        # deliberately dials the loopback address, so the name will not match.
        # The CA signature is still verified -- only the hostname check is off.
        context.check_hostname = False
        context.verify_mode = ssl.CERT_REQUIRED
    except Exception as exc:
        return False, f"cannot load client credentials: {exc}"

    raw = None
    try:
        raw = socket.create_connection((host, port), timeout=timeout)
        tls = context.wrap_socket(raw)
        raw = None                       # ownership passed to the TLS socket
        try:
            version = tls.version()
            peer = tls.getpeercert() or {}
            issued_to = dict(x for entry in peer.get("subject", ())
                             for x in entry).get("commonName", "?")
            try:
                tls.unwrap()             # send close_notify: a clean goodbye
            except OSError:
                pass                     # broker may close first; harmless
            return True, f"{version}, broker cert CN={issued_to}"
        finally:
            tls.close()
    except ssl.SSLError as exc:
        return False, f"handshake rejected: {getattr(exc, 'reason', None) or exc}"
    except OSError as exc:
        return False, f"not reachable: {exc}"
    finally:
        if raw is not None:
            raw.close()


def check_broker():
    params = load_parameters()
    host = params.get("mqtt", {}).get("broker_host", "localhost")
    port = params.get("mqtt", {}).get("broker_port", 1883)
    if not _port_open(host, port):
        return record(FAIL, "MQTT broker reachable",
                      f"nothing listening on {host}:{port} - start Mosquitto first")
    ok, detail = _probe_mtls()
    if ok:
        return record(PASS, "MQTT broker reachable",
                      f"{host}:{port} and 8883 (mTLS ok: {detail})")
    return record(WARN, "MQTT broker reachable",
                  f"{host}:{port} but 8883 (mTLS) failed - sentinels cannot "
                  f"connect: {detail}")


def check_dns():
    name = "audiosense.hmu.gr"
    try:
        addresses = sorted({info[4][0] for info in socket.getaddrinfo(name, None)})
    except OSError as exc:
        return record(WARN, "Sentinel hostname resolves", f"{name}: {exc}")
    local = {info[4][0] for info in socket.getaddrinfo(socket.gethostname(), None)}
    if not set(addresses) & local:
        return record(WARN, "Sentinel hostname resolves",
                      f"{name} -> {', '.join(addresses)} which is not this host "
                      f"({', '.join(sorted(local))}); sentinels will publish elsewhere")
    return record(PASS, "Sentinel hostname resolves", f"{name} -> {', '.join(addresses)}")


def load_parameters():
    path = os.path.join(PROJECT_ROOT, "common_files", "parameters.json")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return {}


def check_storage():
    params = load_parameters()
    inbox = params.get("mqtt", {}).get("save_sound_path", "assets/audios")
    if not os.path.isabs(inbox):
        inbox = os.path.join(PROJECT_ROOT, inbox)
    try:
        os.makedirs(inbox, exist_ok=True)
    except OSError as exc:
        return record(FAIL, "Audio inbox writable", f"{inbox}: {exc}")
    db = params.get("sqlite", {}).get("db_name", "")
    db_path = db if os.path.isabs(db) else os.path.join(PROJECT_ROOT, db)
    if not os.path.exists(db_path):
        return record(WARN, "Audio inbox and database", f"database not found yet: {db_path}")
    size = os.path.getsize(db_path) / (1024 * 1024)
    return record(PASS, "Audio inbox and database", f"{inbox} | db {size:.0f} MB")


def check_dashboard_port():
    if _port_open("127.0.0.1", 8050, timeout=1.0):
        return record(WARN, "Dashboard port 8050 free",
                      "something is already listening - an old dashboard may still be running")
    return record(PASS, "Dashboard port 8050 free")


def main():
    print("=" * WIDTH)
    print(" SENTINEL PRE-FLIGHT CHECKS")
    print("=" * WIDTH)
    check_environment()
    check_blas()
    check_torch()
    check_asr_dlls()
    check_speech_stack()
    check_certificates()
    check_broker()
    check_dns()
    check_storage()
    check_dashboard_port()

    failed = [r for r in results if r[0] == FAIL]
    warned = [r for r in results if r[0] == WARN]
    print("=" * WIDTH)
    print(f" {len(results) - len(failed) - len(warned)} passed, "
          f"{len(warned)} warnings, {len(failed)} failed")
    print("=" * WIDTH)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
