#!/usr/bin/env python3
"""KIS 실시간 클라이언트 가동 테스트 — 연결·구독·틱 수신 확인"""
import sys, os, time
sys.path.insert(0, '/home/ubuntu/kospi_box_bot')
from dotenv import load_dotenv
load_dotenv('/home/ubuntu/kospi_box_bot/.env')

from realtime.kis_realtime import KisRealtimeClient

client = KisRealtimeClient()
print(f"enabled : {client.enabled}")
print(f"status  : {client.status}")

client.start()
# 삼성전자, 금호건설 구독
for code in ['005930', '002990']:
    client.subscribe(code)
    print(f"구독 요청: {code}")

# 10초 대기 후 상태 확인
print("\n연결 대기 중 (10초)...")
time.sleep(10)

snap = client.stats_snapshot()
print(f"\n[상태 스냅샷]")
print(f"  status           : {snap['status']}")
print(f"  subscribed_count : {snap['subscribed_count']}")
print(f"  event_count      : {snap['event_count']}  ← 장중이면 여기에 틱 수")
print(f"  reconnect_attempts: {snap['reconnect_attempts']}")
print(f"  last_error       : {snap['last_connect_error'] or '없음'}")

# 수신된 틱 출력
ticks = client.poll_events(limit=20)
if ticks:
    print(f"\n수신 틱 {len(ticks)}건:")
    for t in ticks:
        print(f"  {t.code} 가격={t.price} 체결량={t.volume} ts={t.ts}")
else:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    kst_now = datetime.now(ZoneInfo('Asia/Seoul'))
    is_market = kst_now.weekday() < 5 and 9 <= kst_now.hour < 15.5
    if is_market:
        print("\n틱 없음 — 연결 문제 확인 필요")
    else:
        print(f"\n틱 없음 — 정상 (현재 장 외 시간: {kst_now.strftime('%a %H:%M KST')})")
        print("  월~금 09:00~15:30 KST 에 틱 수신 시작됩니다")

client.stop()
print("\n테스트 완료")
