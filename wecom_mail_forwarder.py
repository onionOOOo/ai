#!/usr/bin/env python3
"""将指定邮箱账户中的新邮件转发到企业微信机器人。"""

from __future__ import annotations

import json
import os
import re
import time
import imaplib
from dataclasses import dataclass
from datetime import datetime, timezone
from email import message_from_bytes
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


@dataclass
class Config:
    imap_host: str
    imap_port: int
    username: str
    password: str
    mailbox: str
    keyword: str
    webhook: str
    poll_interval: int
    state_file: Path


def load_config() -> Config:
    def required(name: str) -> str:
        value = os.getenv(name, "").strip()
        if not value:
            raise ValueError(f"缺少环境变量: {name}")
        return value

    return Config(
        imap_host=required("IMAP_HOST"),
        imap_port=int(os.getenv("IMAP_PORT", "993")),
        username=required("IMAP_USER"),
        password=required("IMAP_PASS"),
        mailbox=os.getenv("IMAP_MAILBOX", "INBOX"),
        keyword=required("TARGET_ACCOUNT_KEYWORD"),
        webhook=required("WECOM_WEBHOOK"),
        poll_interval=int(os.getenv("POLL_INTERVAL", "30")),
        state_file=Path(os.getenv("STATE_FILE", ".mail_forwarder_state.json")),
    )


def decode_mime(value: Optional[str]) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def extract_text(msg: Message, limit: int = 1200) -> str:
    plain_parts: list[str] = []
    html_parts: list[str] = []

    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = (part.get("Content-Disposition") or "").lower()
            if "attachment" in disp:
                continue
            payload = part.get_payload(decode=True) or b""
            charset = part.get_content_charset() or "utf-8"
            try:
                text = payload.decode(charset, errors="replace")
            except LookupError:
                text = payload.decode("utf-8", errors="replace")
            if ctype == "text/plain":
                plain_parts.append(text)
            elif ctype == "text/html":
                html_parts.append(text)
    else:
        payload = msg.get_payload(decode=True) or b""
        charset = msg.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, errors="replace")
        except LookupError:
            text = payload.decode("utf-8", errors="replace")
        if msg.get_content_type() == "text/html":
            html_parts.append(text)
        else:
            plain_parts.append(text)

    raw = "\n".join(plain_parts).strip()
    if not raw and html_parts:
        raw_html = "\n".join(html_parts)
        raw = re.sub(r"<[^>]+>", " ", raw_html)
        raw = re.sub(r"\s+", " ", raw).strip()

    if len(raw) > limit:
        return raw[:limit] + "..."
    return raw


def parse_mail_date(value: Optional[str]) -> str:
    if not value:
        return "未知时间"
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    except Exception:
        return value


def read_state(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return int(data.get("last_uid", 0))
    except Exception:
        return 0


def save_state(path: Path, uid: int) -> None:
    path.write_text(
        json.dumps({"last_uid": uid, "updated_at": datetime.now().isoformat()}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def send_wecom_markdown(webhook: str, content: str) -> None:
    payload = {
        "msgtype": "markdown",
        "markdown": {"content": content},
    }
    body = json.dumps(payload).encode("utf-8")
    req = Request(webhook, data=body, headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        raise RuntimeError(f"企业微信请求失败: HTTP {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError(f"企业微信请求失败: {exc.reason}") from exc

    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"企业微信响应不是 JSON: {raw}") from exc

    if result.get("errcode") != 0:
        raise RuntimeError(f"企业微信返回错误: {result}")


def build_wecom_message(msg: Message, uid: int) -> str:
    subject = decode_mime(msg.get("Subject")) or "(无主题)"
    from_ = decode_mime(msg.get("From")) or "(未知发件人)"
    to_ = decode_mime(msg.get("To")) or "(未知收件人)"
    date_ = parse_mail_date(msg.get("Date"))
    snippet = extract_text(msg) or "(邮件正文为空)"

    return (
        f"**📧 新邮件提醒（UID: {uid}）**\n"
        f"> **主题：** {subject}\n"
        f"> **发件人：** {from_}\n"
        f"> **收件人：** {to_}\n"
        f"> **时间：** {date_}\n\n"
        f"**正文摘要：**\n"
        f"> {snippet.replace(chr(10), '<br/>')}"
    )


def should_forward(msg: Message, keyword: str) -> bool:
    keyword = keyword.lower()
    to_ = decode_mime(msg.get("To")).lower()
    cc_ = decode_mime(msg.get("Cc")).lower()
    delivered_to = decode_mime(msg.get("Delivered-To")).lower()
    return keyword in to_ or keyword in cc_ or keyword in delivered_to


def fetch_new_uids(conn: imaplib.IMAP4_SSL, min_uid: int) -> list[int]:
    criteria = f"(UID {min_uid + 1}:* UNSEEN)"
    status, data = conn.uid("search", None, criteria)
    if status != "OK":
        raise RuntimeError(f"IMAP 搜索失败: {status} / {data}")
    raw = data[0].decode("utf-8", errors="ignore").strip()
    if not raw:
        return []
    return [int(x) for x in raw.split()]


def fetch_message(conn: imaplib.IMAP4_SSL, uid: int) -> Message:
    status, data = conn.uid("fetch", str(uid), "(RFC822)")
    if status != "OK" or not data or data[0] is None:
        raise RuntimeError(f"获取邮件失败: UID={uid}")

    raw_mail = data[0][1]
    if not isinstance(raw_mail, (bytes, bytearray)):
        raise RuntimeError(f"邮件数据异常: UID={uid}")

    return message_from_bytes(raw_mail)


def run_forever(cfg: Config) -> None:
    last_uid = read_state(cfg.state_file)
    print(f"启动成功，当前 last_uid={last_uid}，轮询间隔={cfg.poll_interval}s")

    while True:
        try:
            with imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port) as conn:
                conn.login(cfg.username, cfg.password)
                status, _ = conn.select(cfg.mailbox)
                if status != "OK":
                    raise RuntimeError(f"无法打开邮箱文件夹: {cfg.mailbox}")

                uids = fetch_new_uids(conn, last_uid)
                if uids:
                    print(f"发现 {len(uids)} 封候选新邮件")

                for uid in uids:
                    msg = fetch_message(conn, uid)
                    if should_forward(msg, cfg.keyword):
                        content = build_wecom_message(msg, uid)
                        send_wecom_markdown(cfg.webhook, content)
                        print(f"已转发 UID={uid} 到企业微信")
                    else:
                        print(f"跳过 UID={uid}（不匹配关键词）")
                    last_uid = max(last_uid, uid)
                    save_state(cfg.state_file, last_uid)
        except Exception as exc:
            print(f"处理循环异常: {exc}")

        time.sleep(cfg.poll_interval)


def main() -> None:
    cfg = load_config()
    run_forever(cfg)


if __name__ == "__main__":
    main()
