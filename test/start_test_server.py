"""
最小化 XtQuantManager 测试服务器

使用 Python 内建 http.server，不依赖 fastapi/uvicorn。
响应 GET /api/v1/health 以支持 XtQuantClient.connect()。
其他端点返回空成功响应，避免测试代码因 HTTP 错误阻塞。
"""
import json
import sys
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

PORT = 8888


class MinimalHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # 静默，不输出访问日志

    def _send_json(self, data: dict, status: int = 200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/v1/health":
            self._send_json({
                "success": True,
                "data": {
                    "status": "ok",
                    "accounts": {},
                    "total": 0,
                    "healthy": 0,
                }
            })
        else:
            self._send_json({"success": True, "data": {}})

    def do_POST(self):
        content_len = int(self.headers.get("Content-Length", 0))
        _ = self.rfile.read(content_len)
        self._send_json({"success": True, "data": {}}, status=201)

    def do_DELETE(self):
        self._send_json({"success": True, "data": {}})


def main():
    server = HTTPServer(("127.0.0.1", PORT), MinimalHandler)
    print(f"[test_server] 启动 XtQuantManager 测试服务器 http://127.0.0.1:{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    print("[test_server] 停止", flush=True)


if __name__ == "__main__":
    main()
