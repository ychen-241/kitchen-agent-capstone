"""程序入口：标准库 HTTP 服务（FastAPI 形状的壳）。

启动：  python -m app.main          （或 python run.py）
端口：  KITCHEN_AGENT_PORT，默认 8100

生产替换点：本文件换成 uvicorn.run(FastAPI app)，路由/服务/编排层零改动。
"""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from .api import routes
from .models import db

_conn = None


def get_conn():
    global _conn
    if _conn is None:
        _conn = db.get_conn()
    return _conn


class Handler(BaseHTTPRequestHandler):
    server_version = "KitchenAgentMVP/0.1"

    def log_message(self, fmt, *args):
        pass  # 静默默认访问日志，保留 notify 输出

    # ---- HTTP 基础 ----
    def _send(self, status: int, payload: dict | bytes, content_type: str = "application/json; charset=utf-8"):
        if isinstance(payload, (dict, list)):
            data = json.dumps(payload, ensure_ascii=False).encode()
        else:
            data = payload
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode())
        except json.JSONDecodeError:
            return {}

    # ---- 路由分发 ----
    def _handle(self, method: str):
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            return self._serve_index()
        if path.startswith("/api/"):
            user = security_user(self.headers.get("Authorization"))
            body = self._body() if method == "POST" else {k: v[0] for k, v in parse_qs(parsed.query).items()}
            status, payload, _ = routes.dispatch(get_conn(), user, method, path, body)
            return self._send(status, payload)
        return self._send(404, {"error": "not found"})

    def _serve_index(self):
        index = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "index.html")
        try:
            with open(index, "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        except FileNotFoundError:
            return self._send(500, {"error": "web/index.html 缺失"})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")


def security_user(auth_header):
    from .core import security
    return security.auth_user(auth_header)


def main():
    db.init_db()
    routes.install_notify()
    from .agents import patrol_agent
    patrol_agent.start_loop(get_conn())
    server = ThreadingHTTPServer(("127.0.0.1", db.config.PORT), Handler)
    mode = "LLM 模式（已配 Key）" if db.config.LLM_API_KEY else "规则模式（离线，未配 Key）"
    print("=" * 56)
    print(" 后厨出品与前厅协作智能体系统 · MVP")
    print(f" 运行模式：{mode}")
    print(f" 巡检 Agent：每 {db.config.PATROL_INTERVAL_SECONDS} 秒自主巡检（超时/积压/滞留）")
    print(f" 地址：http://127.0.0.1:{db.config.PORT}")
    print(" 账号：admin/admin123（管理员）  qianting/123456（员工）")
    print(" 停止：Ctrl+C")
    print("=" * 56)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
        server.server_close()


if __name__ == "__main__":
    main()
