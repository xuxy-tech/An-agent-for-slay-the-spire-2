import json
import sys

sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')
sys.stderr.write(('engine \u2014 \u5b9d\u7bb1\n') * 12000)
sys.stderr.flush()
print(json.dumps({'type': 'ready', 'text': '\u5b9d\u7bb1'}, ensure_ascii=False), flush=True)
for line in sys.stdin:
    value = json.loads(line)
    print(json.dumps({'type': 'ok'}), flush=True)
    if value.get('cmd') == 'quit':
        break
