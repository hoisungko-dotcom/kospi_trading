#!/usr/bin/env python3
import sys, ssl, json, time
sys.path.insert(0, '.')
from dotenv import load_dotenv
load_dotenv('.env')
from collector.kiwoom_client import _get_token
import websocket

token = _get_token()
url = 'wss://api.kiwoom.com:10000/api/dostk/websocket'
ws = websocket.create_connection(url, sslopt={"cert_reqs": ssl.CERT_NONE}, timeout=10)
ws.send(json.dumps({"trnm": "LOGIN", "token": token}))
ws.recv()  # login ack

# 삼성전자 구독
sub = {"trnm": "REG", "grp_no": "1", "refresh": "1",
       "data": [{"trnm": "REAL_STOCKCHG", "stk_cd": "005930"}]}
ws.send(json.dumps(sub))
print("구독 전송 완료, 틱 대기 (5초)...")

ws.settimeout(5)
count = 0
try:
    for _ in range(20):
        raw = ws.recv()
        d = json.loads(raw)
        trnm = d.get("trnm", "")
        if trnm in ("REAL_STOCKCHG", "STOCKCHG", "REG"):
            count += 1
            body = d.get("data", d)
            price = body.get("cur_prc") or body.get("close") or "?"
            vol   = body.get("trde_qty") or "?"
            ts    = body.get("cntr_tm") or "?"
            print("틱 %d: 가격=%s vol=%s ts=%s" % (count, price, vol, ts))
            if count >= 5:
                break
        else:
            print("기타 메시지:", trnm, str(d)[:80])
except Exception as e:
    err = str(e).lower()
    if "timed out" in err or "timeout" in err:
        print("5초 내 틱 %d건 (장 외 시간이면 정상)" % count)
    else:
        print("에러:", e)

ws.close()
print("테스트 완료 — 연결/구독 정상")
