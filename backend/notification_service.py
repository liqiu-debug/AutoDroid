"""
飞书通知服务 (Feishu Notification Service)

负责在定时任务执行完毕后，向飞书群发送卡片消息。
"""
import logging
import httpx
from typing import Optional, Dict, Any, List
from sqlmodel import Session, select

from backend.database import engine
from backend.models import SystemSetting

logger = logging.getLogger(__name__)


class NotificationService:
    """飞书群机器人通知服务"""

    @staticmethod
    def _get_setting(key: str) -> Optional[str]:
        """从数据库读取系统配置"""
        with Session(engine) as session:
            setting = session.exec(
                select(SystemSetting).where(SystemSetting.key == key)
            ).first()
            return setting.value if setting and setting.value else None

    @staticmethod
    def _build_card(
        task_name: str,
        status: str,
        total: int,
        passed: int,
        failed: int,
        duration_seconds: float,
        errors: List[str],
        report_url: str,
        device_count: int = 1,
        passed_devices: Optional[List[str]] = None,
        failed_devices: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """组装飞书卡片 JSON"""
        is_pass = status == "PASS"
        template = "green" if is_pass else "red"
        emoji = "✅" if is_pass else "🔴"
        
        # 判断设备的通过/失败状态
        if passed_devices is not None and failed_devices is not None:
            if len(failed_devices) > 0 and len(passed_devices) > 0:
                status_text = "部分失败"
                template = "orange"
                emoji = "⚠️"
                result_emoji = "⚠️ 部分失败"
            elif len(failed_devices) > 0:
                status_text = "全部失败"
                result_emoji = "❌ 全部失败"
            else:
                status_text = "全部通过"
                result_emoji = "✅ 全部通过"
        else:
            status_text = "全部通过" if is_pass else "存在失败"
            result_emoji = "✅ 通过" if is_pass else "❌ 失败"

        pass_rate = round(passed / total * 100) if total else 0

        # 格式化耗时
        minutes = int(duration_seconds // 60)
        seconds = int(duration_seconds % 60)
        duration_str = f"{minutes}分{seconds}秒" if minutes > 0 else f"{seconds}秒"

        # 格式化设备信息
        devices_str = f"**执行设备:** {device_count} 台"
        if passed_devices is not None or failed_devices is not None:
            details = []
            if passed_devices:
                details.append(f"<font color='green'>通过 {len(passed_devices)}</font>")
            if failed_devices:
                details.append(f"<font color='red'>失败 {len(failed_devices)}</font> ({', '.join(failed_devices)})")
            if details:
                devices_str += f" ({' | '.join(details)})"

        # 主体内容
        content_lines = [
            f"**任务名称:** {task_name}",
            devices_str,
            f"**执行结果:** {result_emoji} (用例通过率: {pass_rate}%)",
            f"**步骤统计:** 运行 {total} 步 | 成功 {passed} 步 | <font color='red'>失败 {failed} 步</font>",
            f"**最大耗时:** {duration_str}",
        ]

        # 失败时展示前 3 个错误
        if errors:
            top_errors = errors[:3]
            error_text = "\n".join(f"  • {e}" for e in top_errors)
            remaining = len(errors) - 3
            if remaining > 0:
                error_text += f"\n  ...还有 {remaining} 个错误"
            content_lines.append(f"\n**🐛 错误详情摘要:**\n{error_text}")

        card = {
            "msg_type": "interactive",
            "card": {
                "config": {"wide_screen_mode": True},
                "header": {
                    "template": template,
                    "title": {
                        "content": f"{emoji} [{status_text}] {task_name}",
                        "tag": "plain_text"
                    }
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "content": "\n".join(content_lines),
                            "tag": "lark_md"
                        }
                    },
                    {"tag": "hr"},
                    {
                        "tag": "action",
                        "actions": [
                            {
                                "tag": "button",
                                "text": {
                                    "content": "📊 查看详细报告",
                                    "tag": "plain_text"
                                },
                                "type": "primary",
                                "value": {"action": "view_report"},
                                "url": report_url
                            }
                        ]
                    }
                ]
            }
        }
        return card

    @classmethod
    def send_report_card(
        cls,
        task_name: str,
        execution_id: int,
        status: str,
        total: int,
        passed: int,
        failed: int,
        duration_seconds: float,
        errors: Optional[List[str]] = None,
        device_count: int = 1,
        passed_devices: Optional[List[str]] = None,
        failed_devices: Optional[List[str]] = None
    ):
        """
        发送测试报告卡片到飞书群。

        Args:
            task_name: 定时任务名称
            execution_id: TestExecution ID
            status: "PASS" 或 "FAIL"
            total: 用例总数
            passed: 通过数
            failed: 失败数
            duration_seconds: 耗时(秒)
            errors: 错误消息列表
            device_count: 执行的设备数量
            passed_devices: 成功执行的设备序列号列表
            failed_devices: 失败执行的设备序列号列表
        """
        webhook_url = cls._get_setting("feishu_webhook")
        if not webhook_url:
            logger.info("飞书 Webhook 未配置，跳过通知")
            return

        base_url = cls._get_setting("system_base_url") or "http://localhost:5173"
        report_url = f"{base_url.rstrip('/')}/reports/{execution_id}"

        card = cls._build_card(
            task_name=task_name,
            status=status,
            total=total,
            passed=passed,
            failed=failed,
            duration_seconds=duration_seconds,
            errors=errors or [],
            report_url=report_url,
            device_count=device_count,
            passed_devices=passed_devices,
            failed_devices=failed_devices
        )

        try:
            with httpx.Client(timeout=10) as client:
                resp = client.post(webhook_url, json=card)
                if resp.status_code == 200:
                    body = resp.json()
                    if body.get("code") == 0 or body.get("StatusCode") == 0:
                        logger.info(f"飞书通知发送成功: {task_name}")
                    else:
                        logger.warning(f"飞书通知返回异常: {body}")
                else:
                    logger.warning(f"飞书通知 HTTP 错误: {resp.status_code} {resp.text}")
        except Exception as e:
            logger.error(f"飞书通知发送失败: {e}")

    @classmethod
    def send_fastbot_report_card(
        cls,
        task_name: str,
        fastbot_task_id: int,
        package_name: str,
        device_serial: str,
        status: str,
        duration_seconds: float,
        total_crashes: int = 0,
        total_anrs: int = 0,
        avg_cpu: float = 0,
        max_cpu: float = 0,
        avg_mem: float = 0,
        max_mem: float = 0,
    ):
        """发送智能探索报告卡片到飞书群（使用 fastbot_webhook）"""
        webhook_url = cls._get_setting("fastbot_webhook")
        if not webhook_url:
            logger.info("智能探索 Webhook 未配置，跳过通知")
            return

        base_url = cls._get_setting("system_base_url") or "http://localhost:5173"
        report_url = f"{base_url.rstrip('/')}/fastbot/report/{fastbot_task_id}"

        is_ok = status == "COMPLETED"
        template = "green" if is_ok else "red"
        emoji = "✅" if is_ok else "🔴"
        status_text = "完成" if is_ok else "失败"

        minutes = int(duration_seconds // 60)
        seconds = int(duration_seconds % 60)
        duration_str = f"{minutes}分{seconds}秒" if minutes > 0 else f"{seconds}秒"

        content_lines = [
            f"**任务名称:** {task_name}",
            f"**目标包名:** {package_name} (设备: {device_serial})",
            f"**执行结果:** {emoji} {status_text}",
            f"**总耗时:** {duration_str}",
            f"**稳定性指标:** <font color='red'>{total_crashes}</font> 次崩溃 | <font color='red'>{total_anrs}</font> 次 ANR",
            f"**性能指标 (CPU):** 平均 {avg_cpu}% / 峰值 {max_cpu}%",
            f"**性能指标 (内存):** 平均 {avg_mem}MB / 峰值 {max_mem}MB",
        ]

        card = {
            "msg_type": "interactive",
            "card": {
                "config": {"wide_screen_mode": True},
                "header": {
                    "template": template,
                    "title": {
                        "content": f"{emoji} [智能探索 {status_text}] {task_name}",
                        "tag": "plain_text"
                    }
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "content": "\n".join(content_lines),
                            "tag": "lark_md"
                        }
                    },
                    {"tag": "hr"},
                    {
                        "tag": "action",
                        "actions": [
                            {
                                "tag": "button",
                                "text": {
                                    "content": "📊 查看详细报告",
                                    "tag": "plain_text"
                                },
                                "type": "primary",
                                "url": report_url
                            }
                        ]
                    }
                ]
            }
        }

        try:
            with httpx.Client(timeout=10) as client:
                resp = client.post(webhook_url, json=card)
                if resp.status_code == 200:
                    body = resp.json()
                    if body.get("code") == 0 or body.get("StatusCode") == 0:
                        logger.info(f"智能探索通知发送成功: {task_name}")
                    else:
                        logger.warning(f"智能探索通知返回异常: {body}")
                else:
                    logger.warning(f"智能探索通知 HTTP 错误: {resp.status_code} {resp.text}")
        except Exception as e:
            logger.error(f"智能探索通知发送失败: {e}")

    @classmethod
    def send_test_message(cls, webhook_url: str) -> Dict[str, Any]:
        """
        发送测试消息验证 Webhook 地址是否有效。

        Args:
            webhook_url: 飞书 Webhook 地址

        Returns:
            {"success": bool, "message": str}
        """
        card = {
            "msg_type": "interactive",
            "card": {
                "config": {"wide_screen_mode": True},
                "header": {
                    "template": "blue",
                    "title": {
                        "content": "🔔 AutoDroid 通知测试",
                        "tag": "plain_text"
                    }
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "content": "**恭喜！** 飞书通知配置成功 🎉\n\n此消息由 AutoDroid 发送，用于验证 Webhook 地址。",
                            "tag": "lark_md"
                        }
                    }
                ]
            }
        }

        try:
            with httpx.Client(timeout=10) as client:
                resp = client.post(webhook_url, json=card)
                if resp.status_code == 200:
                    body = resp.json()
                    if body.get("code") == 0 or body.get("StatusCode") == 0:
                        return {"success": True, "message": "测试消息发送成功"}
                    else:
                        return {"success": False, "message": f"飞书返回错误: {body.get('msg', str(body))}"}
                else:
                    return {"success": False, "message": f"HTTP {resp.status_code}: {resp.text[:200]}"}
        except Exception as e:
            return {"success": False, "message": f"请求失败: {str(e)}"}
