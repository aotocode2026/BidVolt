#!/usr/bin/env bash
# 容器内验证 ClamAV：EICAR 样本应被拦截，正常文本应放行
set -euo pipefail

EICAR='X5O!P%@AP[4\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*'
printf '%s' "$EICAR" > /tmp/eicar.txt
printf 'hello world' > /tmp/clean.txt

/opt/bidvolt/.venv/bin/python - <<'PY'
import io
import clamd

cd = clamd.ClamdUnixSocket()
eicar = cd.instream(io.BytesIO(open("/tmp/eicar.txt", "rb").read()))
clean = cd.instream(io.BytesIO(open("/tmp/clean.txt", "rb").read()))
print("eicar:", eicar)
print("clean:", clean)
assert eicar.get("stream", ("", "OK"))[0] != "OK", "EICAR 未被拦截"
assert clean.get("stream", ("", "OK"))[0] == "OK", "正常文件被误报"
print("CLAMAV OK")
PY
