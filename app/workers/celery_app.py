# app/workers/celery_app.py
import os
import logging
from celery import Celery
from celery.signals import worker_shutting_down, worker_process_shutdown
from dotenv import load_dotenv
from app.core.config import settings




# 🚨 1. 强行加载 .env，确保能读到真实的配置
load_dotenv()

# 🚨 2. 获取 Redis URL
redis_url = settings.REDIS_URL

# 🚨 3. 初始化全局 Logger
logger = logging.getLogger("celery_worker")

# 🚨 4. 初始化 Celery 实例
celery_app = Celery(
    "agent_worker", 
    broker=redis_url,   
    backend=redis_url,  
    include=['app.workers.tasks']
)

# ==========================================
# 🚨 5. 统一配置中心 (基础配置 + 持久化保命配置)
# ==========================================
celery_app.conf.update(
    # --- 你的原有基础配置 ---
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="Asia/Shanghai",
    enable_utc=False,

    # --- 核心保命机制：优雅停机与状态持久化 ---
    # 开启任务晚确认：Worker 只有在彻底执行完任务(return)后，才会向 Redis 发送 ACK
    task_acks_late=True,
    
    # 配合 acks_late，当 Worker 意外丢失时，将任务重新放回队列
    task_reject_on_worker_lost=True,
    
    # 防止任务卡死：强制每个 Worker 进程每次只能预取 1 个任务
    worker_prefetch_multiplier=1,
    
    broker_transport_options={'visibility_timeout': 10},
    beat_schedule={
        "retry-review-outbox": {
            "task": "app.workers.tasks.dispatch_pending_review_outbox",
            "schedule": 15.0,
        },
        "recover-stalled-approved-reviews": {
            "task": "app.workers.tasks.schedule_stalled_approved_reviews",
            "schedule": 30.0,
        },
    },
)

# ==========================================
# 🚨 6. 信号拦截：捕获 docker stop / Ctrl+C
# ==========================================
@worker_shutting_down.connect
def handle_worker_shutdown(sig, how, exitcode, **kwargs):
    """
    当接收到关闭信号时触发。此时 Celery 会进入 Queue Drain 状态：
    拒绝接收任何新任务，并死死保住当前正在运行的任务，直到它执行完毕。
    """
    logger.critical("⚠️ [优雅停机] 接收到关闭信号 (SIGTERM/SIGINT)！已停止接收新任务，正在等待当前执行中的任务安全落库...")

@worker_process_shutdown.connect
def handle_process_shutdown(pid, **kwargs):
    """
    当 Worker 进程当前手头的所有任务都干净利落地完成后，即将彻底退出时触发。
    """
    logger.critical(f"🛑 [优雅停机] Worker 进程 (PID: {pid}) 任务已清空，资源释放完毕，安全退出。")