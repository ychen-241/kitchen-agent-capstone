# kitchen-agent · 后厨出品与前厅协作智能体系统 MVP

按《架构设计方案》实现的**可运行 MVP**。事实依据：《业务需求发现报告》（36 条事实、REQ-1~6）。

## 运行

```bash
cd src
python -m app.main        # 或 python run.py
# 打开 http://127.0.0.1:8100
```

账号：`admin / admin123`（管理员）、`qianting / 123456`、`chushi / 123456`（员工）
> ⚠️ 以上为演示账号，生产部署必须修改。

测试：

```bash
cd src
python -m unittest discover -s tests -v    # 11 个用例，覆盖三条验收路径 + 巡检 Agent
```

## 运行模式（LLM 有边界）

| 模式 | 触发条件 | 行为 |
|------|---------|------|
| 规则模式 | 未配 `KITCHEN_LLM_API_KEY` | 100% 离线，规则解析，全部功能可用 |
| LLM 模式 | 配置 Key | 仅自由文本解析/知识库问答多走一步 LLM |
| 降级路径 | LLM 任何异常 | 自动回规则解析，置信度封顶 0.5，强制进人工修复队列 |

环境变量：`KITCHEN_LLM_API_KEY`、`KITCHEN_LLM_BASE_URL`、`KITCHEN_LLM_MODEL`、`KITCHEN_LLM_MIN_CONFIDENCE`（默认 0.6）、`KITCHEN_AGENT_PORT`（默认 8100）、`KITCHEN_AGENT_DB`。

## 三条验收路径

1. **拦截路径**：点单文本含过敏冲突（如 宫保鸡丁+花生过敏）→ BLOCK 整单暂停 → 管理员页食安签核放行（剔除冲突菜，其余进看板），全程事件留痕；
2. **正常路径**：下单 → 计分排菜（过敏P0+100 > 催菜+40 > 外卖+10 > 等待+2/分钟 > 档口压力+1，每条带理由）→ 催菜重排（同单5分钟去重）→ 开工/完工/上桌结单；
3. **降级路径**：拔掉 LLM Key（或模拟故障）→ 规则解析兜底，置信度封顶、转人工，流程不中断。

## 架构与实现对照

| 架构设计方案 | 本仓库实现 | 生产替换点 |
|---|---|---|
| FastAPI（异步） | `app/main.py` 标准库 http.server 同构壳 | 换 uvicorn + FastAPI，路由签名一致 |
| PostgreSQL + pgvector | `app/models/db.py` SQLite + 稀疏 n-gram 向量 | 表结构一致，直接迁 PostgreSQL |
| HS256 JWT / PBKDF2 | `app/core/security.py` 自实现（零依赖） | 换 PyJWT/Passlib |
| LLM 可换供应商 | `app/llm/` Provider 抽象 + OpenAI 兼容实现 + stub | base.py 注册表扩展 |
| 事件总线 + 只追加留痕 | `app/core/events.py` + `events` 表 | 可换消息队列 |
| 人工修复队列 | `repair_queue` 表（责任人 + 时效） | 对接工单系统 |
| 巡检 Agent（自主循环） | `app/agents/patrol_agent.py`：定时自主扫描修复队列超时/档口积压/订单滞留，发布 `patrol.*` 预警，只读不写业务数据 | 接入企业微信/钉钉推送 |

## 目录

```
docs/                          # 项目文档（业务挖掘与设计依据）
├── discovery-report.md/.json/.csv   # 业务需求发现报告（36 条事实、REQ-1~6）
├── 业务挖掘报告.md
├── 架构设计方案.md            # 四层架构 / 目录结构 / 表设计 / 首周验证目标
├── 餐饮后厨出品协同数字化行业分析报告.docx
└── images/                    # 业务现状流程图、AI 后系统流程图
src/
├── app/
│   ├── main.py            # 入口（http.server 壳）
│   ├── core/              # config / security(JWT) / events(事件总线)
│   ├── api/routes.py      # 路由表（入口层）
│   ├── services/          # 服务层（订单状态机/看板/知识库/审计）
│   ├── agents/            # 编排层：orchestrator + 解析/校验/调度/通知/巡检 Agent
│   ├── llm/               # Provider 抽象 + OpenAI 兼容 + 规则 stub
│   ├── kb/                # 切分向量化 + 余弦检索
│   └── models/db.py       # 9 张表 + 种子数据
├── web/index.html         # 员工端 + 管理员端单页
├── tests/test_mvp.py      # 11 个验收用例
└── run.py                 # 启动入口
```

## 种子数据

- 6 道菜（含档口、标准制作时间、过敏原）：红烧肉/清蒸鲈鱼/麻婆豆腐/宫保鸡丁/蒜蓉西兰花/番茄蛋汤
- 2 份知识库文档：《工艺卡总表 v1》《食安与过敏处理制度 v1》（已向量化）

## 设计红线（来自发现报告）

- 过敏是不可逆风险：BLOCK 级拦截 + 食安签核，不做提示了事；
- 试点期默认只读原则体现在：AI 只做解析/校验/排序建议，写操作全部留痕可查；
- 每条排菜输出排序理由（可解释），正确性不依赖 LLM。
