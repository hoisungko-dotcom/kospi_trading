from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

KST_DATE_FMT = "%Y%m%d%H%M%S"
RULE_DISABLE_DEFAULTS = {
    "BOX_BOT_REQUIRE_PREFERRED_BOX": "false",
    "BOX_BOT_FOLLOW_THROUGH_ENABLED": "false",
}


@dataclass
class TradeRecord:
    code: str
    name: str
    entry_price: float
    exit_price: float
    qty: int
    pnl_pct: float
    pnl_krw: int
    entry_ts: str
    exit_ts: str
    exit_reason: str
    entry_hour: int
    entry_fee_krw: int = 0
    exit_fee_krw: int = 0
    total_fee_krw: int = 0

    @property
    def entry_date(self) -> str:
        return self.entry_ts[:8]

    @property
    def hold_minutes(self) -> int:
        try:
            start = datetime.strptime(self.entry_ts, KST_DATE_FMT)
            end = datetime.strptime(self.exit_ts, KST_DATE_FMT)
            return max(0, int((end - start).total_seconds() // 60))
        except Exception:
            return 0


@dataclass
class ReviewDecision:
    key: str
    value: str
    reason: str
    source: str = "daily_review"


class StrategyEvolutionLedger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data = self._load()

    def _load(self) -> dict:
        if not self.path.exists():
            return {
                "sessions": [],
                "rule_stats": {},
                "active_profile": {},
            }
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _save(self) -> None:
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _rule_stat(self, key: str) -> dict:
        stats = self.data["rule_stats"].setdefault(
            key,
            {
                "applied_count": 0,
                "improved_count": 0,
                "degraded_count": 0,
                "flat_count": 0,
                "cumulative_net_pnl": 0,
                "cumulative_weighted_edge": 0,
                "last_outcome": None,
                "last_delta_weighted_edge": 0,
                "last_value": None,
            },
        )
        return stats

    def get_rule_stats(self) -> dict[str, dict]:
        return self.data.get("rule_stats", {})

    def get_active_profile(self) -> dict[str, str]:
        return dict(self.data.get("active_profile", {}))

    def latest_session(self) -> dict | None:
        sessions = self.data.get("sessions", [])
        if not sessions:
            return None
        return sessions[-1]

    def evaluate_previous_session(self, current_date: str, current_metrics: dict) -> dict | None:
        sessions = self.data["sessions"]
        pending = next(
            (
                session
                for session in reversed(sessions)
                if session.get("date") < current_date and session.get("evaluation", {}).get("status") == "pending"
            ),
            None,
        )
        if not pending:
            return None

        prev_metrics = pending.get("metrics", {})
        delta_weighted_edge = current_metrics["weighted_edge"] - prev_metrics.get("weighted_edge", 0)
        delta_net_pnl = current_metrics["net_pnl"] - prev_metrics.get("net_pnl", 0)
        if delta_weighted_edge > 0:
            outcome = "improved"
        elif delta_weighted_edge < 0:
            outcome = "degraded"
        else:
            outcome = "flat"

        evaluation = {
            "status": "resolved",
            "evaluated_on": current_date,
            "target_trade_date": current_date,
            "outcome": outcome,
            "delta_weighted_edge": delta_weighted_edge,
            "delta_net_pnl": delta_net_pnl,
            "observed_metrics": current_metrics,
        }
        pending["evaluation"] = evaluation

        for decision in pending.get("decisions", []):
            key = decision["key"]
            stats = self._rule_stat(key)
            stats["applied_count"] += 1
            stats["cumulative_net_pnl"] += current_metrics["net_pnl"]
            stats["cumulative_weighted_edge"] += current_metrics["weighted_edge"]
            stats["last_outcome"] = outcome
            stats["last_delta_weighted_edge"] = delta_weighted_edge
            stats["last_value"] = decision["value"]
            if outcome == "improved":
                stats["improved_count"] += 1
            elif outcome == "degraded":
                stats["degraded_count"] += 1
            else:
                stats["flat_count"] += 1

        self._save()
        return evaluation

    def _decision_payload(self, decisions: list[ReviewDecision]) -> list[dict]:
        return [asdict(decision) for decision in decisions]

    def _rebuild_active_profile(self, latest_decisions: list[ReviewDecision]) -> dict[str, str]:
        active = dict(self.data.get("active_profile", {}))
        for decision in latest_decisions:
            active[decision.key] = decision.value
        self.data["active_profile"] = active
        return active

    def _render_rule_board(self) -> str:
        stats = self.get_rule_stats()
        if not stats:
            return "# 전략 진화 보드\n\n- 아직 누적 평가가 없습니다.\n"

        lines = [
            "# 전략 진화 보드",
            "",
            "| 규칙 | 적용 | 개선 | 악화 | 보합 | 누적 가중기대값 | 최근 판정 |",
            "|---|---:|---:|---:|---:|---:|---|",
        ]
        for key, stat in sorted(stats.items()):
            lines.append(
                f"| `{key}` | {stat['applied_count']} | {stat['improved_count']} | "
                f"{stat['degraded_count']} | {stat['flat_count']} | "
                f"{stat['cumulative_weighted_edge']:+,} | {stat['last_outcome'] or '-'} |"
            )
        lines.append("")
        return "\n".join(lines)

    def persist_session(
        self,
        date_str: str,
        metrics: dict,
        decisions: list[ReviewDecision],
        summary: dict,
        evaluation: dict | None,
        change_audit: dict | None = None,
    ) -> dict[str, str]:
        self.data["sessions"] = [session for session in self.data["sessions"] if session.get("date") != date_str]
        session = {
            "date": date_str,
            "metrics": metrics,
            "summary": summary,
            "decisions": self._decision_payload(decisions),
            "change_audit": change_audit or {},
            "evaluation": {
                "status": "pending",
                "target_trade_date": None,
            },
        }
        self.data["sessions"].append(session)
        active = self._rebuild_active_profile(decisions)
        self._save()

        board_path = self.path.parent / "strategy_evolution_board.md"
        board_path.write_text(self._render_rule_board(), encoding="utf-8")
        profile_path = self.path.parent / "active_strategy_profile.json"
        profile_path.write_text(
            json.dumps(
                {
                    "as_of": date_str,
                    "active_profile": active,
                    "latest_evaluation": evaluation,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return {
            "board_path": str(board_path),
            "profile_path": str(profile_path),
        }


class ProfitReviewAgent:
    def __init__(self, bot_root: Path, reward_to_risk: float = 2.0) -> None:
        self.bot_root = bot_root
        self.reward_to_risk = reward_to_risk
        self.state_path = bot_root / "data" / "paper_state.json"
        self.runner_log = bot_root / "logs" / "runner.log"
        self.journal_dir = bot_root / "journal"
        self.journal_dir.mkdir(parents=True, exist_ok=True)
        self.ledger = StrategyEvolutionLedger(self.journal_dir / "strategy_evolution_ledger.json")
        self.override_env_path = bot_root / ".env.ai_overrides"
        self.realtime_metrics_path = bot_root / "data" / "realtime_runtime.json"
        self.strategy_files = [
            bot_root / "realtime" / "daily_runner.py",
            bot_root / "realtime" / "box_checker.py",
            bot_root / "realtime" / "paper_engine.py",
            bot_root / "realtime" / "kiwoom_realtime.py",
            bot_root / "realtime" / "realtime_strategy.py",
            bot_root / "ai_reviewer.py",
        ]

    def _load_state(self) -> dict:
        if not self.state_path.exists():
            return {"cash": 0, "positions": {}, "trades": []}
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def _load_trades(self, date_str: str) -> list[TradeRecord]:
        state = self._load_state()
        trades = []
        allowed_keys = set(TradeRecord.__dataclass_fields__.keys())
        for raw in state.get("trades", []):
            normalized = {k: v for k, v in raw.items() if k in allowed_keys}
            rec = TradeRecord(**normalized)
            if rec.entry_date == date_str:
                trades.append(rec)
        return trades

    def _load_env_overrides(self, path: Path | None = None) -> dict[str, str]:
        target = path or self.override_env_path
        if not target.exists():
            return {}
        result: dict[str, str] = {}
        for raw_line in target.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            result[key.strip()] = value.strip()
        return result

    def _strategy_code_snapshot(self) -> dict[str, dict]:
        snapshot: dict[str, dict] = {}
        for path in self.strategy_files:
            if not path.exists():
                continue
            raw = path.read_bytes()
            snapshot[path.name] = {
                "path": str(path),
                "sha256": hashlib.sha256(raw).hexdigest()[:16],
                "mtime": datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            }
        return snapshot

    def _diff_code_snapshot(self, previous: dict[str, dict], current: dict[str, dict]) -> list[dict]:
        changes: list[dict] = []
        for name in sorted(set(previous.keys()) | set(current.keys())):
            before = previous.get(name)
            after = current.get(name)
            if before == after:
                continue
            if before is None:
                change_type = "added"
            elif after is None:
                change_type = "removed"
            else:
                change_type = "modified"
            changes.append(
                {
                    "file": name,
                    "change_type": change_type,
                    "before_sha": before.get("sha256") if before else None,
                    "after_sha": after.get("sha256") if after else None,
                    "after_mtime": after.get("mtime") if after else None,
                }
            )
        return changes

    def _manual_override_decisions(self, current_env: dict[str, str]) -> list[ReviewDecision]:
        active = self.ledger.get_active_profile()
        decisions: list[ReviewDecision] = []
        for key, value in current_env.items():
            if active.get(key) == value:
                continue
            decisions.append(
                ReviewDecision(
                    key=key,
                    value=value,
                    reason="장중 수동 수정값을 저녁 복기 ledger에 반영",
                    source="runtime_override",
                )
            )
        return decisions

    def _load_realtime_metrics(self, date_str: str) -> dict:
        if not self.realtime_metrics_path.exists():
            return {}
        try:
            payload = json.loads(self.realtime_metrics_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        if payload.get("date") != date_str:
            return {}
        return payload

    def _weighted_edge(self, trades: list[TradeRecord]) -> dict:
        gross_profit = sum(t.pnl_krw for t in trades if t.pnl_krw > 0)
        gross_loss = -sum(t.pnl_krw for t in trades if t.pnl_krw < 0)
        weighted_edge = gross_profit - int(self.reward_to_risk * gross_loss)
        return {
            "gross_profit": gross_profit,
            "gross_loss": gross_loss,
            "net_pnl": gross_profit - gross_loss,
            "weighted_edge": weighted_edge,
            "reward_to_risk": round(gross_profit / gross_loss, 3) if gross_loss > 0 else None,
        }

    def _clustered_entries(self, trades: list[TradeRecord]) -> int:
        entry_times = sorted(t.entry_ts for t in trades)
        max_cluster = 0
        for i, ts in enumerate(entry_times):
            base = datetime.strptime(ts, KST_DATE_FMT)
            count = 1
            for later in entry_times[i + 1:]:
                later_dt = datetime.strptime(later, KST_DATE_FMT)
                if (later_dt - base).total_seconds() <= 600:
                    count += 1
            max_cluster = max(max_cluster, count)
        return max_cluster

    def _summarize(self, trades: list[TradeRecord]) -> dict:
        reasons = Counter(t.exit_reason for t in trades)
        early_losses = [
            t for t in trades
            if t.pnl_krw < 0 and t.hold_minutes <= 120 and t.exit_reason in {"stop_loss", "box_breakdown"}
        ]
        winners = [t for t in trades if t.pnl_krw > 0]
        return {
            "count": len(trades),
            "wins": len(winners),
            "losses": sum(1 for t in trades if t.pnl_krw < 0),
            "win_rate": round((len(winners) / len(trades)) * 100, 1) if trades else 0.0,
            "avg_win": round(sum(t.pnl_krw for t in winners) / len(winners), 1) if winners else 0,
            "avg_loss": round(sum(t.pnl_krw for t in trades if t.pnl_krw < 0) / max(1, sum(1 for t in trades if t.pnl_krw < 0)), 1),
            "exit_reasons": dict(reasons),
            "top_exit_reason": reasons.most_common(1)[0][0] if reasons else None,
            "early_loss_count": len(early_losses),
            "clustered_entries_max_10m": self._clustered_entries(trades),
        }

    def _propose(self, trades: list[TradeRecord]) -> list[ReviewDecision]:
        summary = self._summarize(trades)
        edge = self._weighted_edge(trades)
        decisions: list[ReviewDecision] = []

        decisions.append(ReviewDecision(
            key="BOX_BOT_REWARD_TO_RISK_TARGET",
            value=f"{self.reward_to_risk:.1f}",
            reason="수익 헌법 기준값 기록",
        ))

        if edge["gross_loss"] > 0 and (edge["reward_to_risk"] or 0) < self.reward_to_risk:
            decisions.append(ReviewDecision(
                key="BOX_BOT_REQUIRE_PREFERRED_BOX",
                value="true",
                reason="손실 대비 수익 배율 미달 시 비선호 박스 진입 제거",
            ))

        if summary["clustered_entries_max_10m"] >= 3:
            decisions.append(ReviewDecision(
                key="BOX_BOT_MAX_NEW_BUYS_PER_SCAN",
                value="1",
                reason="10분 내 진입 군집이 커서 잡음 노출 증가",
            ))
            decisions.append(ReviewDecision(
                key="BOX_BOT_MAX_NEW_BUYS_PER_10MIN",
                value="2",
                reason="동시간대 과다 진입 제한으로 수익 품질 개선 시도",
            ))

        if summary["early_loss_count"] >= 2:
            decisions.append(ReviewDecision(
                key="BOX_BOT_FOLLOW_THROUGH_ENABLED",
                value="true",
                reason="초기 손실이 반복되면 추종 실패 청산을 활성화",
            ))
            decisions.append(ReviewDecision(
                key="BOX_BOT_FOLLOW_THROUGH_BARS",
                value="4",
                reason="진입 후 4봉 안에 추종 실패 여부 판정",
            ))
            decisions.append(ReviewDecision(
                key="BOX_BOT_FOLLOW_THROUGH_MIN_GAIN_PCT",
                value="0.003",
                reason="최소 +0.3% 반응이 없으면 기대값 미달로 판정",
            ))

        trailing_wins = [t for t in trades if t.exit_reason == "trailing_stop" and t.pnl_krw > 0]
        if trailing_wins and edge["gross_profit"] < edge["gross_loss"] * self.reward_to_risk:
            decisions.append(ReviewDecision(
                key="BOX_BOT_TRAILING_ARM_PCT",
                value="0.003",
                reason="승자 보유보다 빠른 보호가 유효했던 날의 기본 arm 복원",
            ))
            decisions.append(ReviewDecision(
                key="BOX_BOT_TRAILING_GAP_PCT",
                value="0.010",
                reason="수익 구간을 살리면서도 peak 대비 과한 이탈을 줄이는 기본 gap",
            ))

        return self._dedupe(decisions)

    def _dedupe(self, decisions: list[ReviewDecision]) -> list[ReviewDecision]:
        latest: dict[str, ReviewDecision] = {}
        for decision in decisions:
            latest[decision.key] = decision
        return list(latest.values())

    def _rule_feedback_decisions(self) -> list[ReviewDecision]:
        feedback: list[ReviewDecision] = []
        for key, stat in self.ledger.get_rule_stats().items():
            if key not in RULE_DISABLE_DEFAULTS:
                continue
            if stat["degraded_count"] >= 2 and stat["improved_count"] == 0:
                feedback.append(
                    ReviewDecision(
                        key=key,
                        value=RULE_DISABLE_DEFAULTS[key],
                        reason=f"전략 진화 ledger 기준 반복 악화({stat['degraded_count']}회)로 비활성화",
                        source="evolution_ledger",
                    )
                )
        return feedback

    def _merge_with_active_profile(self, decisions: list[ReviewDecision]) -> list[ReviewDecision]:
        active = self.ledger.get_active_profile()
        merged: dict[str, ReviewDecision] = {
            key: ReviewDecision(
                key=key,
                value=value,
                reason="이전 거래일 전략 프로필 유지",
                source="active_profile",
            )
            for key, value in active.items()
        }
        for decision in decisions + self._rule_feedback_decisions():
            merged[decision.key] = decision
        return list(merged.values())

    def _render_rule_health(self) -> list[str]:
        stats = self.ledger.get_rule_stats()
        if not stats:
            return ["- 아직 누적 전략 평가가 없어 오늘부터 진화 추적 시작."]
        lines = []
        for key, stat in sorted(stats.items()):
            lines.append(
                f"- `{key}` 적용 {stat['applied_count']}회 / 개선 {stat['improved_count']} / "
                f"악화 {stat['degraded_count']} / 최근 {stat['last_outcome'] or '-'}"
            )
        return lines

    def _render_journal(
        self,
        date_str: str,
        trades: list[TradeRecord],
        decisions: list[ReviewDecision],
        evaluation: dict | None,
        change_audit: dict,
        realtime_metrics: dict,
    ) -> str:
        edge = self._weighted_edge(trades)
        summary = self._summarize(trades)
        lines = [
            f"# 한국봇 AI 매매일지 — {date_str}",
            "",
            "## 수익 헌법",
            f"- 손실 1당 목표 수익: {self.reward_to_risk:.1f}",
            f"- 총이익: {edge['gross_profit']:+,}원",
            f"- 총손실: -{edge['gross_loss']:,}원",
            f"- 순손익: {edge['net_pnl']:+,}원",
            f"- 수익/손실 배율: {edge['reward_to_risk'] if edge['reward_to_risk'] is not None else 'loss=0'}",
            f"- 가중 기대값: {edge['weighted_edge']:+,}원",
            "",
            "## 거래 요약",
            f"- 거래수: {summary['count']}",
            f"- 승수/패수: {summary['wins']} / {summary['losses']}",
            f"- 평균 승리: {summary['avg_win']:+,.1f}원",
            f"- 평균 손실: {summary['avg_loss']:+,.1f}원",
            f"- 10분 내 최대 진입 군집: {summary['clustered_entries_max_10m']}",
            f"- 초기 손실 건수(120분 이내): {summary['early_loss_count']}",
            "",
            "## 종료 사유 분포",
        ]
        for reason, count in summary["exit_reasons"].items():
            lines.append(f"- {reason}: {count}")
        lines.extend([
            "",
            "## 전략 진화 판정",
        ])
        if evaluation:
            lines.extend([
                f"- 전일 변경안 평가 결과: {evaluation['outcome']}",
                f"- 가중 기대값 변화: {evaluation['delta_weighted_edge']:+,}",
                f"- 순손익 변화: {evaluation['delta_net_pnl']:+,}",
            ])
        else:
            lines.append("- 전일 미해결 변경안이 없어 오늘 변경안을 새 기준점으로 저장.")
        lines.extend([
            "",
            "## 규칙 생존도",
            *self._render_rule_health(),
            "",
            "## 당일 수정 흔적",
        ])
        runtime_overrides = change_audit.get("runtime_overrides", {})
        manual_overrides = change_audit.get("manual_override_decisions", [])
        code_changes = change_audit.get("code_changes", [])
        if runtime_overrides:
            lines.append(f"- 장 종료 시점 override 수: {len(runtime_overrides)}")
        else:
            lines.append("- 장 종료 시점 override 없음")
        if manual_overrides:
            for decision in manual_overrides:
                lines.append(f"- 수동반영 `{decision['key']}={decision['value']}` : {decision['reason']}")
        else:
            lines.append("- active profile 대비 새 수동 override 없음")
        if code_changes:
            for change in code_changes:
                lines.append(
                    f"- 코드 {change['file']} {change['change_type']} "
                    f"({change['before_sha'] or '-'} -> {change['after_sha'] or '-'})"
                )
        else:
            lines.append("- 직전 복기 세션 대비 전략 코드 변경 없음")
        lines.extend([
            "",
            "## 실시간 품질",
        ])
        if realtime_metrics:
            lines.append(f"- 연결상태: {realtime_metrics.get('status', '-')}")
            lines.append(f"- stale 발생: {realtime_metrics.get('stale_events', 0)}")
            lines.append(f"- near_breakout: {realtime_metrics.get('near_breakout_count', 0)}")
            lines.append(f"- entry_pending: {realtime_metrics.get('entry_pending_count', 0)}")
            lines.append(f"- breakout_watch 탈락: {realtime_metrics.get('breakout_watch_reject_count', 0)}")
            lines.append(f"- follow_through_fail: {realtime_metrics.get('follow_through_fail_count', 0)}")
        else:
            lines.append("- 실시간 런타임 메트릭 없음")
        lines.extend([
            "",
            "## AI 판단",
            "- 오늘 복기의 기준은 방어가 아니라 수익 기대값이다.",
            f"- 수익/손실 배율이 {self.reward_to_risk:.1f} 미만이면 차단 강화와 조기 실패 판정이 우선이다.",
            "- 전략 진화 ledger 는 전일 변경안이 오늘 성과를 개선했는지 누적 채점한다.",
            "",
            "## 자동 수정안",
        ])
        if decisions:
            for decision in decisions:
                lines.append(f"- `{decision.key}={decision.value}` : {decision.reason} ({decision.source})")
        else:
            lines.append("- 오늘은 유지. 핵심 수익 기술이 손실 기술보다 우위라고 판단.")
        lines.extend([
            "",
            "## 거래별 기록",
        ])
        if trades:
            for trade in trades:
                lines.append(
                    f"- {trade.name}({trade.code}) {trade.entry_ts[8:12]}->{trade.exit_ts[8:12]} "
                    f"{trade.pnl_pct:+.2f}% / {trade.pnl_krw:+,}원 / {trade.exit_reason}"
                )
        else:
            lines.append("- 오늘 거래 데이터가 없어 환경값 유지 중심으로 기록.")
        return "\n".join(lines) + "\n"

    def _write_decisions(self, decisions: list[ReviewDecision], path: Path) -> None:
        payload = [asdict(decision) for decision in decisions]
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _write_env(self, decisions: list[ReviewDecision], path: Path) -> None:
        latest: dict[str, str] = {}
        for decision in decisions:
            latest[decision.key] = decision.value
        lines = [f"{key}={value}" for key, value in latest.items()]
        path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

    def _learning_points(self, trades: list[TradeRecord], decisions: list[ReviewDecision], realtime_metrics: dict) -> list[str]:
        points: list[str] = []
        exit_counter = Counter(trade.exit_reason for trade in trades)
        if exit_counter:
            top_reason, top_count = exit_counter.most_common(1)[0]
            points.append(f"주요 손익 원인은 `{top_reason}` {top_count}건")

        weighted = self._weighted_edge(trades)
        if weighted["weighted_edge"] < 0:
            points.append(f"기대값이 음수({weighted['weighted_edge']:+,}원)라 진입 품질 강화 우선")
        else:
            points.append(f"기대값이 양수({weighted['weighted_edge']:+,}원)라 승자 확장 유지 검토")

        if realtime_metrics:
            stale_events = int(realtime_metrics.get("stale_events", 0) or 0)
            reconnects = int(realtime_metrics.get("reconnect_attempts", 0) or 0)
            if stale_events > 0 or reconnects > 0:
                points.append(f"실시간 품질 저하(stale {stale_events}, reconnect {reconnects})는 별도 보수 판정 필요")

        daily_review_decisions = [d for d in decisions if d.source == "daily_review"]
        if daily_review_decisions:
            points.append(f"당일 복기 수정안 {len(daily_review_decisions)}건 반영")
        return points[:4]

    def run(self, date_str: str, apply: bool = False, env_output: Path | None = None) -> dict:
        trades = self._load_trades(date_str)
        metrics = self._weighted_edge(trades)
        summary = self._summarize(trades)
        evaluation = self.ledger.evaluate_previous_session(date_str, metrics)
        realtime_metrics = self._load_realtime_metrics(date_str)
        runtime_overrides = self._load_env_overrides(self.override_env_path)
        manual_override_decisions = self._manual_override_decisions(runtime_overrides)
        current_code_snapshot = self._strategy_code_snapshot()
        previous_session = self.ledger.latest_session() or {}
        previous_code_snapshot = previous_session.get("change_audit", {}).get("code_snapshot", {})
        code_changes = self._diff_code_snapshot(previous_code_snapshot, current_code_snapshot)
        proposed = self._propose(trades)
        decisions = self._merge_with_active_profile(manual_override_decisions + proposed)
        change_audit = {
            "runtime_overrides": runtime_overrides,
            "manual_override_decisions": [asdict(decision) for decision in manual_override_decisions],
            "code_snapshot": current_code_snapshot,
            "code_changes": code_changes,
            "realtime_metrics": realtime_metrics,
        }

        journal_path = self.journal_dir / f"{date_str}_ai_review.md"
        journal_path.write_text(
            self._render_journal(date_str, trades, decisions, evaluation, change_audit, realtime_metrics),
            encoding="utf-8",
        )

        decisions_path = self.journal_dir / f"{date_str}_ai_changes.json"
        self._write_decisions(decisions, decisions_path)

        if apply:
            target = env_output or (self.bot_root / ".env.ai_overrides")
            self._write_env(decisions, target)

        ledger_outputs = self.ledger.persist_session(
            date_str,
            metrics,
            decisions,
            summary,
            evaluation,
            change_audit=change_audit,
        )
        learning_points = self._learning_points(trades, decisions, realtime_metrics)
        return {
            "journal_path": str(journal_path),
            "decisions_path": str(decisions_path),
            "applied_env_path": str(env_output or (self.bot_root / ".env.ai_overrides")) if apply else None,
            "ledger_path": str(self.ledger.path),
            "board_path": ledger_outputs["board_path"],
            "profile_path": ledger_outputs["profile_path"],
            "trade_count": len(trades),
            "decision_count": len(decisions),
            "net_pnl": int(metrics.get("net_pnl", 0) or 0),
            "weighted_edge": int(metrics.get("weighted_edge", 0) or 0),
            "win_rate": float(summary.get("win_rate", 0.0) or 0.0),
            "top_exit_reason": summary.get("top_exit_reason"),
            "learning_points": learning_points,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="한국 박스봇 AI 복기 에이전트")
    parser.add_argument("--bot-root", type=Path, default=Path("."))
    parser.add_argument("--date", required=True, help="YYYYMMDD")
    parser.add_argument("--reward-to-risk", type=float, default=2.0)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--env-output", type=Path, default=None)
    args = parser.parse_args()

    agent = ProfitReviewAgent(args.bot_root, reward_to_risk=args.reward_to_risk)
    result = agent.run(args.date, apply=args.apply, env_output=args.env_output)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
