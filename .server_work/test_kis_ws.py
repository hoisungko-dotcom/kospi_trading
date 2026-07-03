#!/usr/bin/env python3
"""KIS 실전 WebSocket 연결 + 삼성전자 틱 수신 테스트"""
import json, os, time, requests, websocket, threading
from dotenv import load_dotenv
load_dotenv('/home/ubuntu/kospi_box_bot/.env')

APPKEY    = os.environ['KIS_REAL_APPKEY']
APPSECRET = os.environ['KIS_REAL_APPSECRET']
BASE_URL  = os.environ.get('KIS_REAL_BASE_URL', 'https://openapi.koreainvestment.com:9443')
WS_URL    = os.environ.get('KIS_REAL_WS_URL', 'ws://ops.koreainvestment.com:21000')

# 1. WebSocket 접속키 발급
print("1. WebSocket 접속키 발급...")
r = requests.post(
    f"{BASE_URL}/oauth2/Approval",
    headers={"content-type": "application/json"},
    json={"grant_type": "client_credentials", "appkey": APPKEY, "secretkey": APPSECRET},
    timeout=10,
)
d = r.json()
approval_key = d.get("approval_key", "")
if not approval_key:
    print("접속키 발급 실패:", d)
    exit(1)
print(f"   접속키 발급 OK: {approval_key[:20]}...")

# 2. WebSocket 연결
print(f"2. WebSocket 연결: {WS_URL}")
received = []
done = threading.Event()

def on_message(ws, msg):
    received.append(msg)
    if msg.startswith('0|') or msg.startswith('1|'):
        # 실시간 데이터: "0|TR_ID|건수|데이터"
        parts = msg.split('|')
        if len(parts) >= 4 and parts[1] == 'H0STCNT0':
            fields = parts[3].split('^')
            # H0STCNT0 필드: 0=종목코드, 2=체결시간, 3=현재가, 12=체결량
            code  = fields[0] if len(fields) > 0 else '?'
            ts    = fields[2] if len(fields) > 2 else '?'
            price = fields[3] if len(fields) > 3 else '?'
            vol   = fields[12] if len(fields) > 12 else '?'
            print(f"   틱: {code} 가격={price} 체결량={vol} 시각={ts}")
            if len([m for m in received if '|H0STCNT0|' in m]) >= 3:
                done.set()
    elif 'tr_id' in msg.lower() or 'pingpong' in msg.lower():
        print(f"   제어메시지: {msg[:100]}")

def on_open(ws):
    print("   WebSocket 연결 성공")
    # 삼성전자 실시간 체결 구독
    sub = {
        "header": {
            "approval_key": approval_key,
            "custtype": "P",
            "tr_type": "1",
            "content-type": "utf-8"
        },
        "body": {
            "input": {"tr_id": "H0STCNT0", "tr_key": "005930"}
        }
    }
    ws.send(json.dumps(sub))
    print("   구독 요청 전송: H0STCNT0 / 005930(삼성전자)")

def on_error(ws, err):
    print(f"   에러: {err}")
    done.set()

def on_close(ws, code, msg):
    print(f"   연결 종료: {code} {msg}")
    done.set()

ws = websocket.WebSocketApp(
    WS_URL,
    on_open=on_open,
    on_message=on_message,
    on_error=on_error,
    on_close=on_close,
)
t = threading.Thread(target=ws.run_forever, kwargs={"ping_interval": 20, "ping_timeout": 10})
t.daemon = True
t.start()

# 장 중이면 틱, 장 외이면 연결/구독 응답만 확인
done.wait(timeout=8)
ws.close()
t.join(timeout=3)

print(f"\n수신 메시지 {len(received)}건")
for m in received[:5]:
    print(" ", m[:150])
print("\n테스트 완료")
