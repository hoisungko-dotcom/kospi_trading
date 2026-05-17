import logging
import os
from urllib import parse, request


logger = logging.getLogger(__name__)


class TelegramReporter:
    """간단한 텔레그램 메시지 전송기."""

    def __init__(self):
        self.token = os.getenv("TELEGRAM_TOKEN")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID")

    def is_enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def send_message(self, message: str, prefix: str = "🇰🇷 [한국주식 보고]"):
        if not self.is_enabled():
            return

        text = f"{prefix}\n{message}"
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        data = parse.urlencode(
            {
                "chat_id": self.chat_id,
                "text": text,
            }
        ).encode("utf-8")

        try:
            req = request.Request(url, data=data, method="POST")
            with request.urlopen(req, timeout=10) as resp:
                if resp.status != 200:
                    logger.error(f"❌ 텔레그램 전송 실패: HTTP {resp.status}")
        except Exception as e:
            logger.error(f"❌ 텔레그램 전송 실패: {e}")
