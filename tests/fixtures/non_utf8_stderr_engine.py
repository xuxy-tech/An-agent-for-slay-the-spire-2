import json
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.buffer.write((b"legacy \xa1\xa2 diagnostics\n") * 12000)
sys.stderr.buffer.flush()
print(json.dumps({"type": "ready", "text": "ok"}), flush=True)
for line in sys.stdin:
    value = json.loads(line)
    print(json.dumps({"type": "ok"}), flush=True)
    if value.get("cmd") == "quit":
        break
