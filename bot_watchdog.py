"""
bot_watchdog.py — 자동매매 봇 감시 + 오류 자동 분석 + 노션 보고 시스템
═══════════════════════════════════════════════════════════════
기능:
  1. 봇 프로세스 생존 감시 → 죽으면 재시작 + 텔레그램 알림
  2. 로그 오류 감지 → 분석 보고서 생성
  3. 노션에 일일 보고서 자동 작성
  4. 텔레그램 명령 수신 → 파일 수정/재시작

사용법:
  # .env에 추가:
  # NOTION_TOKEN=ntn_xxxxx
  # NOTION_PAGE_ID=abc123def456
  # TELEGRAM_TOKEN_WATCHDOG=xxx  (별도 봇 또는 기존 봇 공유)
  # TELEGRAM_CHAT_ID=xxx

  python bot_watchdog.py                    # 기본 실행
  python bot_watchdog.py --check-only       # 1회 점검만
  python bot_watchdog.py --no-notion        # 노션 없이 실행
"""

import os, sys, json, time, subprocess, requests, traceback, re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()

KST = timezone(timedelta(hours=9))

# ═══════════════════════════════════════════════════════════
#  설정
# ═══════════════════════════════════════════════════════════

CONFIG = {
    # 텔레그램 (기존 봇과 같은 토큰 사용 가능)
    "TELEGRAM_TOKEN": os.environ.get("TELEGRAM_TOKEN_WATCHDOG",
                      os.environ.get("TELEGRAM_TOKEN_TRADER",
                      os.environ.get("TELEGRAM_TOKEN_MONITOR", ""))),
    "TELEGRAM_CHAT_ID": os.environ.get("TELEGRAM_CHAT_ID", ""),

    # 노션
    "NOTION_TOKEN": os.environ.get("NOTION_TOKEN", ""),
    "NOTION_PAGE_ID": os.environ.get("NOTION_PAGE_ID", "32c6b195d47f8037b433c171b134b9f1"),
    "NOTION_VERSION": "2022-06-28",

    # 감시 대상 봇
    "BOTS": {
        "bots": {
            "name": "Bot1+Bot2 (V9.2 + Hunt)",
            "cmd": "python3 auto_trader_v9.py --mode E --live & python3 auto_trader_v9_bot2.py --mode E --live",
            "positions_files": [
                "trade_logs/positions_v9.json",
                "trade_logs/positions_v9_bot2.json",
            ],
            "history_files": [
                "trade_logs/trade_history_v9.jsonl",
                "trade_logs/trade_history_v9_bot2.jsonl",
            ],
            "tmux_session": "bots",
        },
        "bots_2": {
            "name": "Semi-Auto (V8.2 소형코인)",
            "cmd": "python3 semi_auto_trader.py --live",
            "positions_files": [
                "trade_logs_semi/positions_semi.json",
            ],
            "history_files": [
                "trade_logs_semi/trade_history_semi.jsonl",
            ],
            "tmux_session": "bots_2",
        },
    },

    # 감시 주기
    "CHECK_INTERVAL_SEC": 300,     # 5분마다 점검
    "DAILY_REPORT_HOUR": 8,       # 매일 오전 8시 일일 보고
    "ERROR_PATTERNS": [
        r"Traceback \(most recent call last\)",
        r"Error|ERROR|Exception",
        r"🔴 오류",
        r"SL 설정 완전 실패",
        r"긴급 청산",
        r"수동 확인 필요",
        r"ConnectionError|TimeoutError|APIError",
    ],

    # 작업 디렉토리
    "WORK_DIR": os.path.dirname(os.path.abspath(__file__)),
    "REPORT_DIR": "trade_logs/reports",
}


# ═══════════════════════════════════════════════════════════
#  텔레그램
# ═══════════════════════════════════════════════════════════

class TG:
    def __init__(self, token, chat_id):
        self.token = token; self.chat_id = chat_id
        self.ok = bool(token and chat_id)

    def send(self, msg):
        if not self.ok:
            print(f"[TG] {msg[:200]}"); return
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                data={"chat_id": self.chat_id, "text": msg[:4000]},
                timeout=10)
            if r.status_code != 200:
                print(f"[TG] 전송 실패: {r.status_code}")
        except Exception as e:
            print(f"[TG] 오류: {e}")

    def get_updates(self, offset=0):
        """텔레그램 메시지 수신 (명령어 처리용)"""
        if not self.ok: return []
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{self.token}/getUpdates",
                params={"offset": offset, "timeout": 5, "limit": 10},
                timeout=15)
            if r.status_code == 200:
                return r.json().get("result", [])
        except: pass
        return []


# ═══════════════════════════════════════════════════════════
#  노션 보고서 작성
# ═══════════════════════════════════════════════════════════

class NotionReporter:
    def __init__(self, token, page_id, version="2022-06-28"):
        self.token = token
        self.page_id = page_id
        self.version = version
        self.ok = bool(token and page_id)
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": version,
            "Content-Type": "application/json",
        }

    def append_report(self, title, content_blocks):
        """노션 페이지에 보고서 블록 추가"""
        if not self.ok:
            print("[Notion] 토큰 또는 페이지 ID 미설정")
            return False

        blocks = []

        # 제목 (heading_2)
        blocks.append({
            "object": "block",
            "type": "heading_2",
            "heading_2": {
                "rich_text": [{"type": "text", "text": {"content": title}}]
            }
        })

        # 내용 블록들
        for block in content_blocks:
            if isinstance(block, str):
                # 텍스트 블록
                blocks.append({
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [{"type": "text", "text": {"content": block[:2000]}}]
                    }
                })
            elif isinstance(block, dict) and block.get("type") == "code":
                # 코드 블록
                blocks.append({
                    "object": "block",
                    "type": "code",
                    "code": {
                        "rich_text": [{"type": "text", "text": {"content": block["content"][:2000]}}],
                        "language": block.get("language", "plain text")
                    }
                })
            elif isinstance(block, dict) and block.get("type") == "divider":
                blocks.append({"object": "block", "type": "divider", "divider": {}})

        try:
            r = requests.patch(
                f"https://api.notion.com/v1/blocks/{self.page_id}/children",
                headers=self.headers,
                json={"children": blocks},
                timeout=15)
            if r.status_code == 200:
                print(f"[Notion] 보고서 작성 완료: {title}")
                return True
            else:
                print(f"[Notion] 작성 실패: {r.status_code} {r.text[:200]}")
                return False
        except Exception as e:
            print(f"[Notion] 오류: {e}")
            return False


# ═══════════════════════════════════════════════════════════
#  봇 프로세스 감시
# ═══════════════════════════════════════════════════════════

class ProcessMonitor:
    """tmux 세션 기반 봇 생존 감시"""

    @staticmethod
    def is_alive(tmux_session):
        """tmux 세션이 살아있는지 확인"""
        try:
            result = subprocess.run(
                ["tmux", "has-session", "-t", tmux_session],
                capture_output=True, timeout=5)
            return result.returncode == 0
        except:
            return False

    @staticmethod
    def get_recent_output(tmux_session, lines=50):
        """tmux 세션의 최근 출력 가져오기"""
        try:
            result = subprocess.run(
                ["tmux", "capture-pane", "-t", tmux_session, "-p", "-S", f"-{lines}"],
                capture_output=True, text=True, timeout=5)
            return result.stdout if result.returncode == 0 else ""
        except:
            return ""

    @staticmethod
    def restart_bot(tmux_session, cmd, work_dir):
        """tmux 세션에서 봇 재시작"""
        try:
            # 기존 세션 종료
            subprocess.run(["tmux", "kill-session", "-t", tmux_session],
                          capture_output=True, timeout=5)
            time.sleep(1)
            # 새 세션에서 봇 시작
            subprocess.run(
                ["tmux", "new-session", "-d", "-s", tmux_session, "-c", work_dir, cmd],
                capture_output=True, timeout=5)
            return True
        except:
            return False


# ═══════════════════════════════════════════════════════════
#  로그 분석
# ═══════════════════════════════════════════════════════════

class LogAnalyzer:
    def __init__(self, error_patterns):
        self.patterns = [re.compile(p, re.IGNORECASE) for p in error_patterns]
        self._last_positions = {}  # {filepath: last_read_position}

    def check_new_errors(self, filepath):
        """마지막 체크 이후 새로 추가된 오류 찾기"""
        if not os.path.exists(filepath):
            return []

        last_pos = self._last_positions.get(filepath, 0)
        try:
            with open(filepath, 'r', errors='replace') as f:
                f.seek(0, 2)  # 파일 끝으로
                file_size = f.tell()

                if file_size < last_pos:
                    # 파일이 작아짐 (로테이션) → 처음부터
                    last_pos = 0

                f.seek(last_pos)
                new_content = f.read()
                self._last_positions[filepath] = f.tell()

            if not new_content:
                return []

            errors = []
            lines = new_content.split('\n')
            for i, line in enumerate(lines):
                for pattern in self.patterns:
                    if pattern.search(line):
                        # 오류 전후 5줄 컨텍스트
                        start = max(0, i - 2)
                        end = min(len(lines), i + 5)
                        context = '\n'.join(lines[start:end])
                        errors.append({
                            'line': line.strip(),
                            'context': context,
                            'pattern': pattern.pattern,
                            'time': datetime.now(KST).isoformat(),
                        })
                        break  # 같은 줄에서 중복 매칭 방지
            return errors
        except Exception as e:
            print(f"[Log] 읽기 실패 {filepath}: {e}")
            return []

    def get_trade_summary(self, history_file):
        """거래 히스토리에서 오늘 요약 추출"""
        if not os.path.exists(history_file):
            return None
        try:
            today = datetime.now(KST).strftime('%Y-%m-%d')
            trades = []
            with open(history_file) as f:
                for line in f:
                    try:
                        t = json.loads(line.strip())
                        if today in t.get('time', ''):
                            trades.append(t)
                    except: continue

            if not trades:
                return None

            total_pnl = sum(t.get('pnl', 0) for t in trades)
            wins = sum(1 for t in trades if t.get('pnl', 0) > 0)
            losses = len(trades) - wins

            return {
                'date': today,
                'trades': len(trades),
                'wins': wins,
                'losses': losses,
                'pnl': round(total_pnl, 2),
                'details': trades,
            }
        except:
            return None


# ═══════════════════════════════════════════════════════════
#  메인 워치독
# ═══════════════════════════════════════════════════════════

class BotWatchdog:
    def __init__(self, cfg=None):
        self.cfg = cfg or CONFIG
        self.tg = TG(self.cfg["TELEGRAM_TOKEN"], self.cfg["TELEGRAM_CHAT_ID"])
        self.notion = NotionReporter(
            self.cfg["NOTION_TOKEN"],
            self.cfg["NOTION_PAGE_ID"])
        self.monitor = ProcessMonitor()
        self.analyzer = LogAnalyzer(self.cfg["ERROR_PATTERNS"])
        self._daily_report_done = False
        self._tg_offset = 0

        os.makedirs(self.cfg["REPORT_DIR"], exist_ok=True)

    # ── 1. 프로세스 생존 체크 ──

    def check_processes(self):
        """모든 봇 프로세스 생존 확인"""
        results = {}
        for bot_id, bot_cfg in self.cfg["BOTS"].items():
            alive = self.monitor.is_alive(bot_cfg["tmux_session"])
            results[bot_id] = alive

            if not alive:
                self.tg.send(
                    f"🚨 [{bot_cfg['name']}] 프로세스 죽음 감지!\n"
                    f"tmux 세션 '{bot_cfg['tmux_session']}' 없음\n"
                    f"→ 재시작 시도 중...")

                # 재시작 시도
                ok = self.monitor.restart_bot(
                    bot_cfg["tmux_session"],
                    bot_cfg["cmd"],
                    self.cfg["WORK_DIR"])

                if ok:
                    self.tg.send(f"✅ [{bot_cfg['name']}] 재시작 성공")
                    # 노션 기록
                    self.notion.append_report(
                        f"🚨 봇 재시작 — {datetime.now(KST).strftime('%m/%d %H:%M')}",
                        [f"{bot_cfg['name']} 프로세스 죽음 감지 → 자동 재시작 완료"])
                else:
                    self.tg.send(f"❌ [{bot_cfg['name']}] 재시작 실패! 수동 확인 필요")

        return results

    # ── 2. 로그 오류 감지 ──

    def check_errors(self):
        """모든 봇 로그에서 새 오류 감지"""
        all_errors = {}
        for bot_id, bot_cfg in self.cfg["BOTS"].items():
            # tmux 출력에서 오류 감지
            output = self.monitor.get_recent_output(bot_cfg["tmux_session"], 100)
            if output:
                # 임시 파일로 저장 후 분석
                tmp_log = os.path.join(self.cfg["REPORT_DIR"], f"{bot_id}_latest.log")
                with open(tmp_log, 'w') as f:
                    f.write(output)
                errors = self.analyzer.check_new_errors(tmp_log)
                if errors:
                    all_errors[bot_id] = errors

            # 로그 파일에서도 체크 (있으면)
            for lf in bot_cfg.get("log_files", []):
                if lf and os.path.exists(lf):
                    errors = self.analyzer.check_new_errors(lf)
                    if errors:
                        all_errors.setdefault(bot_id, []).extend(errors)

        # 오류 발견 시 알림
        for bot_id, errors in all_errors.items():
            bot_name = self.cfg["BOTS"][bot_id]["name"]
            for err in errors[:3]:  # 최대 3개만 알림 (스팸 방지)
                self.tg.send(
                    f"⚠️ [{bot_name}] 오류 감지\n"
                    f"━━━━━━━━━━━━━━━━\n"
                    f"패턴: {err['pattern']}\n"
                    f"내용: {err['line'][:200]}\n"
                    f"━━━━━━━━━━━━━━━━\n"
                    f"컨텍스트:\n{err['context'][:500]}")

            # 노션 기록
            if errors:
                self.notion.append_report(
                    f"⚠️ 오류 감지 [{bot_name}] — {datetime.now(KST).strftime('%m/%d %H:%M')}",
                    [f"감지된 오류: {len(errors)}건"] +
                    [{"type": "code", "content": e['context'][:500], "language": "plain text"}
                     for e in errors[:3]] +
                    [{"type": "divider"}])

        return all_errors

    # ── 3. 일일 보고서 ──

    def daily_report(self):
        """일일 거래 요약 + 봇 상태 보고"""
        now = datetime.now(KST)
        lines = [f"📊 일일 보고서 [{now.strftime('%Y-%m-%d %H:%M')}]"]
        notion_blocks = []

        for bot_id, bot_cfg in self.cfg["BOTS"].items():
            alive = self.monitor.is_alive(bot_cfg["tmux_session"])
            status = "🟢 작동중" if alive else "🔴 중단"
            lines.append(f"\n{bot_cfg['name']}: {status}")
            notion_blocks.append(f"{bot_cfg['name']}: {status}")

            # 포지션 현황 (여러 파일 지원)
            all_positions = []
            for pos_file in bot_cfg.get("positions_files", []):
                if pos_file and os.path.exists(pos_file):
                    try:
                        with open(pos_file) as f:
                            all_positions.extend(json.load(f))
                    except: pass

            if all_positions:
                lines.append(f"  포지션: {len(all_positions)}개")
                for p in all_positions:
                    sym = p.get('symbol', '?')
                    d = p.get('direction', '?')
                    ep = p.get('entry_price', 0)
                    lines.append(f"    {d[0].upper()} {sym} @{ep:,.4f}")
            else:
                lines.append("  포지션: 없음")

            # 거래 요약 (여러 히스토리 파일 합산)
            total_trades = 0; total_wins = 0; total_pnl = 0
            has_summary = False
            for hf in bot_cfg.get("history_files", []):
                summary = self.analyzer.get_trade_summary(hf)
                if summary:
                    has_summary = True
                    total_trades += summary['trades']
                    total_wins += summary['wins']
                    total_pnl += summary['pnl']

            if has_summary:
                total_losses = total_trades - total_wins
                lines.append(f"  오늘 거래: {total_trades}건 "
                           f"(승:{total_wins} 패:{total_losses})")
                lines.append(f"  오늘 손익: ${total_pnl:+,.2f}")
                notion_blocks.append(
                    f"거래 {total_trades}건 | "
                    f"승:{total_wins} 패:{total_losses} | "
                    f"PnL: ${total_pnl:+,.2f}")
            else:
                notion_blocks.append("오늘 거래 없음")

        msg = '\n'.join(lines)
        self.tg.send(msg)

        # 노션 일일 보고
        self.notion.append_report(
            f"📊 일일 보고 — {now.strftime('%Y-%m-%d')}",
            notion_blocks + [{"type": "divider"}])

        # 로컬 보고서 저장
        report_path = os.path.join(
            self.cfg["REPORT_DIR"],
            f"daily_{now.strftime('%Y%m%d')}.json")
        with open(report_path, 'w') as f:
            json.dump({"date": now.isoformat(), "report": lines}, f,
                      indent=2, ensure_ascii=False)

    # ── 4. 텔레그램 명령 수신 ──

    def process_commands(self):
        """텔레그램에서 수신한 명령 처리"""
        updates = self.tg.get_updates(self._tg_offset)
        for update in updates:
            self._tg_offset = update["update_id"] + 1
            msg = update.get("message", {})
            text = msg.get("text", "").strip()
            chat_id = str(msg.get("chat", {}).get("id", ""))

            # 본인 chat_id만 처리
            if chat_id != self.cfg["TELEGRAM_CHAT_ID"]:
                continue

            if text.startswith("/status"):
                self._cmd_status()
            elif text.startswith("/restart"):
                parts = text.split()
                bot_id = parts[1] if len(parts) > 1 else "all"
                self._cmd_restart(bot_id)
            elif text.startswith("/errors"):
                self._cmd_errors()
            elif text.startswith("/report"):
                self.daily_report()
            elif text.startswith("/help"):
                bot_list = ", ".join(self.cfg["BOTS"].keys())
                self.tg.send(
                    "🤖 워치독 명령어:\n"
                    "/status — 봇 상태 확인\n"
                    f"/restart [{bot_list}|all] — 봇 재시작\n"
                    "/errors — 최근 오류 확인\n"
                    "/report — 일일 보고서 생성")

    def _cmd_status(self):
        lines = ["🤖 봇 상태:"]
        for bot_id, bot_cfg in self.cfg["BOTS"].items():
            alive = self.monitor.is_alive(bot_cfg["tmux_session"])
            status = "🟢" if alive else "🔴"
            lines.append(f"  {status} {bot_cfg['name']} ({bot_cfg['tmux_session']})")
        self.tg.send('\n'.join(lines))

    def _cmd_restart(self, bot_id):
        if bot_id == "all":
            targets = list(self.cfg["BOTS"].keys())
        elif bot_id in self.cfg["BOTS"]:
            targets = [bot_id]
        else:
            self.tg.send(f"❌ 알 수 없는 봇: {bot_id}"); return

        for bid in targets:
            bc = self.cfg["BOTS"][bid]
            self.tg.send(f"🔄 {bc['name']} 재시작 중...")
            ok = self.monitor.restart_bot(
                bc["tmux_session"], bc["cmd"], self.cfg["WORK_DIR"])
            self.tg.send(f"{'✅ 성공' if ok else '❌ 실패'}: {bc['name']}")

    def _cmd_errors(self):
        errors = self.check_errors()
        if not errors:
            self.tg.send("✅ 최근 오류 없음")

    # ── 메인 루프 ──

    def run(self):
        now = datetime.now(KST)
        self.tg.send(
            f"🤖 워치독 시작 [{now.strftime('%m/%d %H:%M')}]\n"
            f"감시 대상: {len(self.cfg['BOTS'])}개 봇\n"
            f"점검 주기: {self.cfg['CHECK_INTERVAL_SEC']}초\n"
            f"노션: {'✅' if self.notion.ok else '❌ 미설정'}\n"
            f"명령어: /help")
        print(f"[Watchdog] 시작 — {len(self.cfg['BOTS'])}개 봇 감시")

        while True:
            try:
                now = datetime.now(KST)

                # 프로세스 생존 체크
                self.check_processes()

                # 로그 오류 체크
                self.check_errors()

                # 텔레그램 명령 처리
                self.process_commands()

                # 일일 보고 (오전 8시)
                if now.hour == self.cfg["DAILY_REPORT_HOUR"] and not self._daily_report_done:
                    self.daily_report()
                    self._daily_report_done = True
                elif now.hour != self.cfg["DAILY_REPORT_HOUR"]:
                    self._daily_report_done = False

                time.sleep(self.cfg["CHECK_INTERVAL_SEC"])

            except KeyboardInterrupt:
                self.tg.send("🤖 워치독 종료"); break
            except Exception as e:
                traceback.print_exc()
                self.tg.send(f"🤖 워치독 오류: {e}")
                time.sleep(60)

    def check_once(self):
        """1회 점검만 실행"""
        print("=" * 50)
        print("  Bot Watchdog — 1회 점검")
        print("=" * 50)

        # 프로세스 체크
        results = self.check_processes()
        for bot_id, alive in results.items():
            name = self.cfg["BOTS"][bot_id]["name"]
            print(f"  {'✅' if alive else '❌'} {name}")

        # 오류 체크
        errors = self.check_errors()
        if errors:
            for bot_id, errs in errors.items():
                print(f"  ⚠️ {bot_id}: {len(errs)}건 오류")
        else:
            print("  ✅ 오류 없음")

        # 일일 보고
        self.daily_report()


# ═══════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description='Bot Watchdog Agent')
    parser.add_argument('--check-only', action='store_true',
                        help='1회 점검만 실행')
    parser.add_argument('--no-notion', action='store_true',
                        help='노션 보고 비활성화')
    args = parser.parse_args()

    cfg = CONFIG.copy()
    if args.no_notion:
        cfg["NOTION_TOKEN"] = ""
        cfg["NOTION_PAGE_ID"] = ""

    wd = BotWatchdog(cfg)
    if args.check_only:
        wd.check_once()
    else:
        wd.run()
