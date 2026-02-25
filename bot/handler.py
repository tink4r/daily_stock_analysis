# -*- coding: utf-8 -*-
"""
===================================
Bot Webhook 处理器
===================================

处理各平台的 Webhook 回调，分发到命令处理器。
"""

import json
import logging
import time
from typing import Dict, Any, Optional, TYPE_CHECKING

from bot.models import WebhookResponse
from bot.dispatcher import get_dispatcher
from bot.platforms import ALL_PLATFORMS

if TYPE_CHECKING:
    from bot.platforms.base import BotPlatform

logger = logging.getLogger(__name__)

# 平台实例缓存
_platform_instances: Dict[str, 'BotPlatform'] = {}

# 短窗去重缓存：fingerprint -> ts
_recent_message_cache: Dict[str, float] = {}
_DUPLICATE_WINDOW_SECONDS = 120


def _is_duplicate_message(message) -> bool:
    """基于平台+消息ID+会话ID的短窗去重。"""
    now = time.time()
    # 清理过期缓存
    expired_keys = [k for k, ts in _recent_message_cache.items() if now - ts > _DUPLICATE_WINDOW_SECONDS]
    for k in expired_keys:
        _recent_message_cache.pop(k, None)

    message_id = getattr(message, "message_id", "") or ""
    chat_id = getattr(message, "chat_id", "") or ""
    content = getattr(message, "content", "") or ""
    platform = getattr(message, "platform", "") or ""
    fingerprint = f"{platform}:{message_id}:{chat_id}:{content.strip()}"

    if fingerprint in _recent_message_cache:
        return True

    _recent_message_cache[fingerprint] = now
    return False


def get_platform(platform_name: str) -> Optional['BotPlatform']:
    """
    获取平台适配器实例
    
    使用缓存避免重复创建。
    
    Args:
        platform_name: 平台名称
        
    Returns:
        平台适配器实例，或 None
    """
    if platform_name not in _platform_instances:
        platform_class = ALL_PLATFORMS.get(platform_name)
        if platform_class:
            _platform_instances[platform_name] = platform_class()
        else:
            logger.warning(f"[BotHandler] 未知平台: {platform_name}")
            return None
    
    return _platform_instances[platform_name]


def handle_webhook(
    platform_name: str,
    headers: Dict[str, str],
    body: bytes,
    query_params: Optional[Dict[str, list]] = None
) -> WebhookResponse:
    """
    处理 Webhook 请求
    
    这是所有平台 Webhook 的统一入口。
    
    Args:
        platform_name: 平台名称 (feishu, dingtalk, wecom, telegram)
        headers: HTTP 请求头
        body: 请求体原始字节
        query_params: URL 查询参数（用于某些平台的验证）
        
    Returns:
        WebhookResponse 响应对象
    """
    logger.info(f"[BotHandler] 收到 {platform_name} Webhook 请求")
    
    # 检查机器人功能是否启用
    from src.config import get_config
    config = get_config()
    
    if not getattr(config, 'bot_enabled', True):
        logger.info("[BotHandler] 机器人功能未启用")
        return WebhookResponse.success()
    
    # 获取平台适配器
    platform = get_platform(platform_name)
    if not platform:
        return WebhookResponse.error(f"Unknown platform: {platform_name}", 400)
    
    # 解析请求数据
    data: Dict[str, Any] = {}
    if body:
        try:
            data = json.loads(body.decode('utf-8'))
        except json.JSONDecodeError:
            # 企业微信回调是 XML，这里保留原始 body 给平台解析
            data = {
                "_raw_body": body.decode('utf-8', errors='ignore')
            }

    if query_params:
        data["_query_params"] = query_params
    
    logger.debug(f"[BotHandler] 请求数据: {json.dumps(data, ensure_ascii=False)[:500]}")
    
    # 处理 Webhook
    message, challenge_response = platform.handle_webhook(headers, body, data)
    
    # 如果是验证请求，直接返回验证响应
    if challenge_response:
        logger.info(f"[BotHandler] 返回验证响应")
        return challenge_response
    
    # 如果没有消息需要处理，返回空响应
    if not message:
        logger.debug("[BotHandler] 无需处理的消息")
        return WebhookResponse.success()
    
    if _is_duplicate_message(message):
        logger.info(
            f"[BotHandler] 检测到重复消息，已忽略: platform={message.platform}, "
            f"message_id={message.message_id}, user={message.user_name}"
        )
        return WebhookResponse.success()

    logger.info(f"[BotHandler] 解析到消息: user={message.user_name}, content={message.content[:50]}")
    
    # 分发到命令处理器
    dispatcher = get_dispatcher()
    response = dispatcher.dispatch(message)
    
    # 格式化响应
    if response.text:
        webhook_response = platform.format_response(response, message)
        return webhook_response
    
    return WebhookResponse.success()


def handle_feishu_webhook(headers: Dict[str, str], body: bytes) -> WebhookResponse:
    """处理飞书 Webhook"""
    return handle_webhook('feishu', headers, body)


def handle_dingtalk_webhook(headers: Dict[str, str], body: bytes) -> WebhookResponse:
    """处理钉钉 Webhook"""
    return handle_webhook('dingtalk', headers, body)


def handle_wecom_webhook(headers: Dict[str, str], body: bytes) -> WebhookResponse:
    """处理企业微信 Webhook"""
    return handle_webhook('wecom', headers, body)


def handle_telegram_webhook(headers: Dict[str, str], body: bytes) -> WebhookResponse:
    """处理 Telegram Webhook"""
    return handle_webhook('telegram', headers, body)
