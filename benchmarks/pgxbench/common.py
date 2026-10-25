import base64
import json
import math
import os
from pathlib import Path
import subprocess
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
TARGETS = ("pgxio", "postgrest", "openresty", "node", "go", "supabase")


def dump(path, value, private=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if private else 0o644)
    if private:
        os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")


def command(argv, **kwargs):
    return subprocess.run([str(v) for v in argv], check=True, text=True,
                          capture_output=True, **kwargs).stdout.strip()


def encode(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def subject(index):
    return str(uuid.UUID(int=index + 1))


def route(index):
    return str(uuid.UUID(int=1_000_001 + index))


def token(workdir, index, seconds=172800):
    payload = {"sub": subject(index), "role": "web", "iss": "pgxbench",
               "aud": "pgxbench", "iat": int(time.time()), "exp": int(time.time()) + seconds}
    data = (encode(b'{"alg":"RS256","kid":"pgxbench","typ":"JWT"}') + "." +
            encode(json.dumps(payload, separators=(",", ":")).encode())).encode()
    result = subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(Path(workdir) / "jwt-private.pem")],
                            input=data, capture_output=True, check=True)
    return data.decode() + "." + encode(result.stdout)


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def distribution(values):
    return {"count": len(values), "p50_ms": percentile(values, .5),
            "p95_ms": percentile(values, .95), "p99_ms": percentile(values, .99),
            "max_ms": max(values) if values else None}


def compose(workdir, *args, input=None):
    return command(["docker", "compose", "--project-name", "pgxbench", "--file",
                    Path(workdir).resolve() / "compose.json", *args], input=input)


def load_settings(workdir):
    return json.loads((Path(workdir) / "settings.json").read_text())


def public_settings(settings):
    return {k: v for k, v in settings.items() if k not in
            {"admin_password", "web_password", "realtime_secret", "secret_key_base", "external_database_url"}}
