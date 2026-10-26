from pathlib import Path
from typing import Literal
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent

class Settings(BaseSettings):
    PROJECT_NAME: str = "售后客服 Agent"

    DATABASE_URL: str
    # Existing records belong to this merchant after the M0 migration.
    DEFAULT_TENANT_ID: Literal["merchant_default"] = "merchant_default"
    # 🆕 新增：只读数据库 URL，默认可回退到主库方便本地测试
    READONLY_DATABASE_URL: str = ""
    
    SECRET_KEY: str = Field(..., description="必须从 .env 注入，禁止硬编码默认值")
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24

    
    # 📡 基础设施连接配置
    REDIS_URL: str = "redis://localhost:6379/0"
    QDRANT_URL: str = "http://localhost:6333"
    MEMORY_CONTEXT_MAX_TOKENS: int = Field(default=32768, ge=512, le=131072)
    CUSTOMER_MEMORY_TTL_DAYS: int = Field(default=365, ge=1, le=3650)
    COMPANY_KNOWLEDGE_COLLECTION: str = "company_knowledge_v2"

    # 🧠 大模型 (LLM) 基础配置
    DEEPSEEK_API_KEY: str
    LLM_MODEL_NAME: str = "deepseek-v4-flash"
    LLM_BASE_URL: str = "https://api.deepseek.com"
    LLM_TEMPERATURE: float = 0.0  # 客服场景需要确定性，默认设为 0

    # 📐 向量模型 (Embedding) 基础配置
    DASHSCOPE_API_KEY: str
    EMBEDDING_MODEL_NAME: str = "text-embedding-v4"
    EMBEDDING_API_BASE: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"

    
    # 🚨 新增：安全与权限配置
    ADMIN_USERNAME: str = "admin"            # 默认后台管理员账号
    ADMIN_PASSWORD: str = Field(..., description="必须从 .env 注入，禁止硬编码默认值")
    ADMIN_APPROVAL_CODE: str = Field(...)

    # ⚖️ 新增：裁判模型配置
    QWEN_JUDGE_API_KEY: str = "" 
    QWEN_JUDGE_MODEL_NAME: str = "qwen3.7-max"
    QWEN_JUDGE_BASE_URL: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"


    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env", 
        env_file_encoding='utf-8',
        extra='ignore' # 忽略 .env 中多余的字段，防止报错
    )

settings = Settings()
