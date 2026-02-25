# -*- coding: utf-8 -*-
"""
===================================
企业微信平台适配器
===================================

说明：
1. 支持企业微信应用回调（明文模式）
2. 支持 URL 验证（GET）
3. 支持文本消息触发命令
4. 通过企业微信应用消息 API 主动回复结果
"""

import hashlib
import logging
import time
import base64
import struct
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Dict, Any, Optional, Tuple

import requests

from bot.models import BotMessage, BotResponse, WebhookResponse, ChatType
from bot.platforms.base import BotPlatform

logger = logging.getLogger(__name__)


class WecomPlatform(BotPlatform):
    """企业微信平台适配器（应用回调）"""

    def __init__(self):
        from src.config import get_config

        config = get_config()
        self._corpid = getattr(config, "wecom_corpid", None)
        self._token = getattr(config, "wecom_token", None)
        self._agent_id = getattr(config, "wecom_agent_id", None)
        self._agent_secret = getattr(config, "wecom_agent_secret", None)
        self._encoding_aes_key = getattr(config, "wecom_encoding_aes_key", None)

        self._access_token: Optional[str] = None
        self._access_token_expire_at: float = 0.0

    @property
    def platform_name(self) -> str:
        return "wecom"

    def verify_request(self, headers: Dict[str, str], body: bytes) -> bool:
        """签名校验在 `handle_webhook` 中完成，这里保持兼容。"""
        return True

    def handle_webhook(
        self,
        headers: Dict[str, str],
        body: bytes,
        data: Dict[str, Any],
    ) -> Tuple[Optional[BotMessage], Optional[WebhookResponse]]:
        """处理企业微信回调（GET 验证 + POST 消息）。"""
        query = data.get("_query_params", {}) if isinstance(data, dict) else {}

        def qv(name: str) -> str:
            v = query.get(name, []) if isinstance(query, dict) else []
            return v[0] if v else ""

        msg_signature = qv("msg_signature") or qv("signature")
        timestamp = qv("timestamp")
        nonce = qv("nonce")
        echostr = qv("echostr")
        if echostr:
            # URL query 中 '+' 可能被框架按空格解码，需恢复后再验签/解密
            echostr = echostr.replace(" ", "+")

        # 1) URL 验证（GET）
        if echostr:
            if self._token and msg_signature and timestamp and nonce:
                # 安全模式签名：sha1(sort(token, timestamp, nonce, echostr))
                if not self._verify_signature(self._token, msg_signature, timestamp, nonce, echostr):
                    logger.warning("[WECOM] URL 验证签名失败")
                    return None, WebhookResponse.error("Invalid signature", 403)

            # 安全模式：echostr 为密文，需解密后原样返回明文
            if self._encoding_aes_key:
                plain_echostr = self._decrypt_wecom_text(echostr)
                if plain_echostr is None:
                    logger.error("[WECOM] URL 验证 echostr 解密失败")
                    return None, WebhookResponse.error("Decrypt echostr failed", 400)
                return None, WebhookResponse.text(plain_echostr)

            # 明文模式直接返回
            return None, WebhookResponse.text(echostr)

        # 2) 普通消息（POST）
        encrypt_text = self._extract_encrypt_from_xml(data.get("_raw_body", ""))
        if self._token and msg_signature and timestamp and nonce:
            if encrypt_text:
                ok = self._verify_signature(self._token, msg_signature, timestamp, nonce, encrypt_text)
            else:
                ok = self._verify_signature(self._token, msg_signature, timestamp, nonce)
            if not ok:
                logger.warning("[WECOM] 回调签名验证失败")
                return None, WebhookResponse.error("Invalid signature", 403)

        # 安全模式：POST 体为 <Encrypt>，需要先解密再解析
        if encrypt_text:
            if not self._encoding_aes_key:
                logger.error("[WECOM] 收到加密消息，但未配置 WECOM_ENCODING_AES_KEY")
                return None, WebhookResponse.error("Missing WECOM_ENCODING_AES_KEY", 400)
            plain_xml = self._decrypt_wecom_text(encrypt_text)
            if plain_xml is None:
                logger.error("[WECOM] 消息解密失败")
                return None, WebhookResponse.error("Decrypt message failed", 400)
            data = {**data, "_raw_body": plain_xml}

        message = self.parse_message(data)
        return message, None

    def parse_message(self, data: Dict[str, Any]) -> Optional[BotMessage]:
        """解析企业微信 XML 消息。"""
        raw_body = data.get("_raw_body", "") if isinstance(data, dict) else ""
        if not raw_body:
            return None

        try:
            root = ET.fromstring(raw_body)
        except ET.ParseError:
            logger.warning("[WECOM] XML 解析失败")
            return None

        # 安全模式下如果仍是 Encrypt，说明解密流程未生效
        if root.findtext("Encrypt"):
            logger.warning("[WECOM] 收到未解密消息体，已忽略")
            return None

        msg_type = (root.findtext("MsgType") or "").strip().lower()
        if msg_type != "text":
            return None

        content = (root.findtext("Content") or "").strip()
        if not content:
            return None

        from_user = (root.findtext("FromUserName") or "").strip()
        to_user = (root.findtext("ToUserName") or "").strip()
        msg_id = (root.findtext("MsgId") or root.findtext("CreateTime") or "")
        create_time = root.findtext("CreateTime") or ""

        ts = datetime.now()
        if create_time.isdigit():
            try:
                ts = datetime.fromtimestamp(int(create_time))
            except Exception:
                pass

        mentioned = "@" in content

        return BotMessage(
            platform=self.platform_name,
            message_id=msg_id,
            user_id=from_user,
            user_name=from_user,
            chat_id=from_user,
            chat_type=ChatType.PRIVATE,
            content=content,
            raw_content=content,
            mentioned=mentioned,
            timestamp=ts,
            raw_data={
                "from_user": from_user,
                "to_user": to_user,
                "agent_id": root.findtext("AgentID") or self._agent_id or "",
                "msg_type": msg_type,
            },
        )

    def format_response(self, response: BotResponse, message: BotMessage) -> WebhookResponse:
        """
        企业微信回调采用“主动发送”方式回复，Webhook 仅返回 success。
        """
        if response and response.text:
            self._send_app_message(
                touser=message.user_id,
                content=response.text,
                markdown=bool(response.markdown),
            )

        return WebhookResponse.text("success")

    @staticmethod
    def _verify_signature(token: str, signature: str, timestamp: str, nonce: str, encrypted: Optional[str] = None) -> bool:
        """企业微信签名校验。

        明文模式：sha1(sort([token,timestamp,nonce]))
        安全模式：sha1(sort([token,timestamp,nonce,encrypt]))
        """
        params = [token, timestamp, nonce]
        if encrypted:
            params.append(encrypted)
        params = sorted(params)
        sign_str = "".join(params)
        calc = hashlib.sha1(sign_str.encode("utf-8")).hexdigest()
        return calc == signature

    @staticmethod
    def _extract_encrypt_from_xml(raw_body: str) -> str:
        if not raw_body:
            return ""
        try:
            root = ET.fromstring(raw_body)
        except ET.ParseError:
            return ""
        return (root.findtext("Encrypt") or "").strip()

    @staticmethod
    def _pkcs7_unpad(data: bytes) -> bytes:
        if not data:
            raise ValueError("empty data")
        pad_len = data[-1]
        if pad_len < 1 or pad_len > 32:
            raise ValueError("invalid pkcs7 padding")
        if data[-pad_len:] != bytes([pad_len]) * pad_len:
            raise ValueError("invalid pkcs7 bytes")
        return data[:-pad_len]

    def _decrypt_wecom_text(self, encrypted: str) -> Optional[str]:
        """解密企业微信安全模式密文（echostr 或 Encrypt）。"""
        if not self._encoding_aes_key:
            return None
        try:
            from Crypto.Cipher import AES
        except Exception:
            logger.error("[WECOM] 缺少 pycryptodome 依赖，无法解密企业微信安全模式消息")
            return None

        try:
            aes_key = base64.b64decode(self._encoding_aes_key + "=")
            iv = aes_key[:16]

            cipher = AES.new(aes_key, AES.MODE_CBC, iv)
            encrypted_bytes = base64.b64decode(encrypted)
            plain_padded = cipher.decrypt(encrypted_bytes)
            plain = self._pkcs7_unpad(plain_padded)

            # 结构：16B随机串 + 4B网络序长度 + 内容 + receive_id
            msg_len = struct.unpack("!I", plain[16:20])[0]
            msg = plain[20:20 + msg_len]
            receive_id = plain[20 + msg_len:].decode("utf-8", errors="ignore")

            # 企业微信应用回调 receive_id 应为 corpId
            if self._corpid and receive_id and receive_id != self._corpid:
                logger.warning(f"[WECOM] receive_id 不匹配: {receive_id}")

            return msg.decode("utf-8", errors="ignore")
        except Exception as e:
            logger.error(f"[WECOM] 解密失败: {e}")
            return None

    def _get_access_token(self) -> Optional[str]:
        """获取并缓存企业微信 access_token。"""
        now = time.time()
        if self._access_token and now < self._access_token_expire_at:
            return self._access_token

        if not self._corpid or not self._agent_secret:
            logger.warning("[WECOM] 缺少 WECOM_CORPID 或 WECOM_AGENT_SECRET，无法发送应用消息")
            return None

        try:
            resp = requests.get(
                "https://qyapi.weixin.qq.com/cgi-bin/gettoken",
                params={
                    "corpid": self._corpid,
                    "corpsecret": self._agent_secret,
                },
                timeout=10,
            )
            if resp.status_code != 200:
                logger.error(f"[WECOM] 获取 access_token 失败，HTTP {resp.status_code}")
                return None

            data = resp.json()
            if data.get("errcode") != 0:
                logger.error(f"[WECOM] 获取 access_token 失败: {data}")
                return None

            token = data.get("access_token")
            expires_in = int(data.get("expires_in", 7200))
            if not token:
                logger.error("[WECOM] access_token 为空")
                return None

            self._access_token = token
            self._access_token_expire_at = now + max(60, expires_in - 120)
            return token

        except Exception as e:
            logger.error(f"[WECOM] 获取 access_token 异常: {e}")
            return None

    def _send_app_message(self, touser: str, content: str, markdown: bool = True) -> bool:
        """发送企业微信应用消息。"""
        access_token = self._get_access_token()
        if not access_token:
            return False

        if not self._agent_id:
            logger.warning("[WECOM] 未配置 WECOM_AGENT_ID，无法发送应用消息")
            return False

        msg_type = "markdown" if markdown else "text"
        payload: Dict[str, Any] = {
            "touser": touser,
            "msgtype": msg_type,
            "agentid": int(self._agent_id),
            "safe": 0,
        }
        if msg_type == "markdown":
            payload["markdown"] = {"content": content}
        else:
            payload["text"] = {"content": content}

        try:
            resp = requests.post(
                "https://qyapi.weixin.qq.com/cgi-bin/message/send",
                params={"access_token": access_token},
                json=payload,
                timeout=10,
            )
            if resp.status_code != 200:
                logger.error(f"[WECOM] 发送消息失败，HTTP {resp.status_code}")
                return False

            result = resp.json()
            if result.get("errcode") == 0:
                logger.info("[WECOM] 应用消息发送成功")
                return True

            logger.error(f"[WECOM] 应用消息发送失败: {result}")
            return False
        except Exception as e:
            logger.error(f"[WECOM] 发送应用消息异常: {e}")
            return False
