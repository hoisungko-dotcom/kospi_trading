## Box v1 Legacy Extract

- Status: legacy reference only
- Production owner: `realtime/box_checker.py` (`BoxChecker v2`)

### Keep Before Delete

- [ ] v1 손절 방식 검토 후 v2 흡수 여부 기록
- [ ] v1 후보 선정 규칙 검토 후 v2 흡수 여부 기록
- [ ] v1 트레일링 로직 검토 후 v2 흡수 여부 기록

### Notes

- 현재 서버 운영본은 별도 `box_checker_v1.py` 파일이 남아 있지 않다.
- 과거 v1의 흔적은 주로 `.env` 기본값과 문구 혼선에 남아 있었다.
- 전략 유실 방지를 위해 구형 기본값은 `legacy_env_reference.env`로 보관한다.
