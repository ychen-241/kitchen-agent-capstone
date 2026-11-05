"""配置外置：所有可变参数走环境变量（对应架构设计方案 core/config.py）。"""
import os
import secrets

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.environ.get("KITCHEN_AGENT_DB", os.path.join(DATA_DIR, "kitchen_agent.db"))
WEB_DIR = os.path.join(BASE_DIR, "web")

# 端口：部署平台注入的 PORT 优先，其次 KITCHEN_AGENT_PORT，默认 8100
PORT = int(os.environ.get("PORT") or os.environ.get("KITCHEN_AGENT_PORT") or "8100")
# 绑定地址：部署沙箱（有 PORT 注入）需绑 0.0.0.0；本地默认 127.0.0.1 更安全
HOST = "0.0.0.0" if os.environ.get("PORT") else os.environ.get("KITCHEN_AGENT_HOST", "127.0.0.1")

# LLM 可替换供应商：不配 Key 全流程离线跑（stub 兜底），配 Key 自动升级
LLM_API_KEY = os.environ.get("KITCHEN_LLM_API_KEY", "")
LLM_BASE_URL = os.environ.get("KITCHEN_LLM_BASE_URL", "https://api.openai.com/v1")
LLM_MODEL = os.environ.get("KITCHEN_LLM_MODEL", "gpt-4o-mini")
LLM_MIN_CONFIDENCE = float(os.environ.get("KITCHEN_LLM_MIN_CONFIDENCE", "0.6"))
LLM_TIMEOUT = int(os.environ.get("KITCHEN_LLM_TIMEOUT", "20"))

# JWT 密钥：优先环境变量，否则首次生成并持久化
_SECRET_FILE = os.path.join(DATA_DIR, "secret.key")


def jwt_secret() -> bytes:
    if os.environ.get("KITCHEN_AGENT_JWT_SECRET"):
        return os.environ["KITCHEN_AGENT_JWT_SECRET"].encode()
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(_SECRET_FILE):
        with open(_SECRET_FILE, "w", encoding="utf-8") as f:
            f.write(secrets.token_hex(32))
    with open(_SECRET_FILE, "r", encoding="utf-8") as f:
        return f.read().encode()


# 计分权重（可按需调整）
SCORE_ALLERGY_P0 = 100   # 过敏备注置顶（不冲突时）
SCORE_URGE = 40          # 每次催菜
SCORE_TAKEOUT = 10       # 外卖
SCORE_WAIT_PER_MIN = 2   # 等待时长
SCORE_STATION_PRESSURE = 1  # 档口压力（用户自创规则保留）

URGE_DEDUPE_SECONDS = 300      # 同单催菜去重窗口
REPAIR_DEADLINE_MINUTES = 15   # 修复队列默认时效

# 巡检 Agent（自主循环：感知 → 决策 → 预警，只读不写业务数据）
PATROL_INTERVAL_SECONDS = int(os.environ.get("KITCHEN_PATROL_INTERVAL", "60"))
PATROL_BACKLOG_THRESHOLD = int(os.environ.get("KITCHEN_PATROL_BACKLOG", "5"))
PATROL_BLOCKED_MINUTES = int(os.environ.get("KITCHEN_PATROL_BLOCKED_MIN", "10"))
PATROL_DEDUPE_SECONDS = int(os.environ.get("KITCHEN_PATROL_DEDUPE", "600"))
