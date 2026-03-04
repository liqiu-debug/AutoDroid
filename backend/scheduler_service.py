"""
定时任务调度服务

使用 APScheduler BackgroundScheduler 实现四种调度策略:
- DAILY: 每天定时执行
- WEEKLY: 每周指定天数执行
- INTERVAL: 固定间隔执行
- ONCE: 单次指定日期执行
"""
import json
import logging
from datetime import datetime
from typing import Optional, Dict, Any
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.date import DateTrigger

logger = logging.getLogger(__name__)

# 星期映射 (前端数字 -> APScheduler 字符串)
DAY_MAP = {
    0: "mon", 1: "tue", 2: "wed", 3: "thu",
    4: "fri", 5: "sat", 6: "sun"
}
DAY_NAMES_CN = {
    0: "周一", 1: "周二", 2: "周三", 3: "周四",
    4: "周五", 5: "周六", 6: "周日"
}


class SchedulerService:
    """单例式调度服务"""

    def __init__(self):
        self.scheduler = BackgroundScheduler()
        self.scheduler.start()
        logger.info("APScheduler 调度器已启动")

    def create_trigger(self, strategy: str, config: Dict[str, Any]):
        """根据策略和配置创建 APScheduler Trigger"""
        if strategy == "DAILY":
            hour = config.get("hour", 0)
            minute = config.get("minute", 0)
            return CronTrigger(hour=hour, minute=minute)

        elif strategy == "WEEKLY":
            days = config.get("days", [])  # [0, 2, 4] = Mon, Wed, Fri
            hour = config.get("hour", 0)
            minute = config.get("minute", 0)
            day_of_week = ",".join(DAY_MAP.get(d, "mon") for d in days)
            if not day_of_week:
                day_of_week = "mon"
            return CronTrigger(day_of_week=day_of_week, hour=hour, minute=minute)

        elif strategy == "INTERVAL":
            interval_value = config.get("interval_value", 30)
            interval_unit = config.get("interval_unit", "minutes")  # minutes / hours
            if interval_unit == "hours":
                return IntervalTrigger(hours=interval_value)
            else:
                return IntervalTrigger(minutes=interval_value)

        elif strategy == "ONCE":
            run_date_str = config.get("run_date", "")
            if not run_date_str:
                raise ValueError("ONCE 策略需要 run_date 参数")
            run_date = datetime.fromisoformat(run_date_str)
            return DateTrigger(run_date=run_date)

        else:
            raise ValueError(f"未知策略: {strategy}")

    @staticmethod
    def format_schedule(strategy: str, config: Dict[str, Any]) -> str:
        """将策略配置翻译成可读的中文描述"""
        if strategy == "DAILY":
            hour = config.get("hour", 0)
            minute = config.get("minute", 0)
            return f"📅 每天 {hour:02d}:{minute:02d}"

        elif strategy == "WEEKLY":
            days = config.get("days", [])
            hour = config.get("hour", 0)
            minute = config.get("minute", 0)
            day_names = [DAY_NAMES_CN.get(d, "?") for d in sorted(days)]
            return f"📅 每{'/'.join(day_names)} {hour:02d}:{minute:02d}"

        elif strategy == "INTERVAL":
            interval_value = config.get("interval_value", 30)
            interval_unit = config.get("interval_unit", "minutes")
            unit_cn = "分钟" if interval_unit == "minutes" else "小时"
            return f"🔄 每隔 {interval_value} {unit_cn}"

        elif strategy == "ONCE":
            run_date_str = config.get("run_date", "")
            if run_date_str:
                try:
                    dt = datetime.fromisoformat(run_date_str)
                    return f"🎯 单次: {dt.strftime('%Y-%m-%d %H:%M')}"
                except ValueError:
                    return f"🎯 单次: {run_date_str}"
            return "🎯 单次: 未设置"

        return "未知"

    def add_task(self, task_id: int, strategy: str, config: Dict[str, Any], job_func, **kwargs):
        """添加调度任务"""
        job_id = f"task_{task_id}"
        # 如果已存在则先移除
        self.remove_task(task_id)
        trigger = self.create_trigger(strategy, config)
        # 将 task_id 加入 kwargs，供 job_func 回调使用
        job_kwargs = {"task_id": task_id, **kwargs}
        job = self.scheduler.add_job(
            job_func,
            trigger=trigger,
            id=job_id,
            name=f"定时任务#{task_id}",
            replace_existing=True,
            kwargs=job_kwargs
        )
        logger.info(f"已添加调度任务 {job_id}, 下次运行: {job.next_run_time}")
        return job.next_run_time

    def remove_task(self, task_id: int):
        """移除调度任务"""
        job_id = f"task_{task_id}"
        try:
            self.scheduler.remove_job(job_id)
            logger.info(f"已移除调度任务 {job_id}")
        except Exception:
            pass  # 不存在时忽略

    def pause_task(self, task_id: int):
        """暂停任务"""
        job_id = f"task_{task_id}"
        try:
            self.scheduler.pause_job(job_id)
        except Exception:
            pass

    def resume_task(self, task_id: int):
        """恢复任务"""
        job_id = f"task_{task_id}"
        try:
            self.scheduler.resume_job(job_id)
        except Exception:
            pass

    def get_next_run_time(self, task_id: int):
        """获取下次运行时间"""
        job_id = f"task_{task_id}"
        job = self.scheduler.get_job(job_id)
        if job:
            return job.next_run_time
        return None

    def shutdown(self):
        """关闭调度器"""
        self.scheduler.shutdown(wait=False)
        logger.info("APScheduler 调度器已关闭")


# 全局单例
scheduler_service: Optional[SchedulerService] = None


def get_scheduler() -> SchedulerService:
    global scheduler_service
    if scheduler_service is None:
        scheduler_service = SchedulerService()
    return scheduler_service
