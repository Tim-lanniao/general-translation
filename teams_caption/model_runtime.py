"""Control the local model through its configured launcher."""
import os
import subprocess
import time
import urllib.request
from pathlib import Path

NO_WINDOW = subprocess.CREATE_NO_WINDOW


def _http_ok(url: str, timeout: float = 1.0) -> bool:
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(urllib.request.Request(url), timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


class ModelRuntime:
    def __init__(self, cfg: dict, log):
        self.cfg = cfg
        self.log = log
        self.owned = False
        self.url = cfg["llm_url"].rstrip("/")
        self.label = cfg.get("llm_label") or cfg["llm_model"]

    def ready(self) -> bool:
        return _http_ok(self.url + "/health") or _http_ok(self.url + "/v1/models")

    def control(self, action: str, timeout: float) -> str:
        script = Path(self.cfg.get("llm_controller") or "")
        if not script.is_file():
            raise FileNotFoundError(f"找不到翻译服务启动脚本：{script}")
        out = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), action],
            env={**os.environ, "AUDIO_TRANSLATION_CONTROLLER": "1"}, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, creationflags=NO_WINDOW,
        )
        text = (out.stdout or "") + (out.stderr or "")
        self.log(f"llm controller {action} -> exit {out.returncode}: {text.strip()[-600:]}")
        if out.returncode != 0:
            raise RuntimeError(f"翻译服务 {action} 失败（exit {out.returncode}）：{text.strip()[-300:]}")
        return text

    def start(self, progress) -> None:
        if self.ready():
            if self.cfg.get("llm_controller") and not self.owned:
                raise RuntimeError("检测到网关外启动的模型。请先结束本地 AI 测试，再使用翻译。")
            progress(f"翻译模型 {self.label} 已在运行，直接使用")
            return
        if not self.cfg.get("llm_controller"):
            raise RuntimeError(f"本地翻译服务 {self.url} 没有运行。请先启动它，或在 config.json 里设置 llm_controller 让本程序自动启动。")
        progress(f"正在启动翻译模型 {self.label}（约 25 秒）…")
        self.owned = True  # a failing controller may already have spawned its server
        try:
            self.control("start", timeout=300)
            deadline = time.time() + 120
            while not self.ready():
                if time.time() > deadline:
                    raise TimeoutError(f"翻译服务已启动，但 {self.url} 未就绪")
                time.sleep(0.5)
        except BaseException:
            self.stop()
            raise

    def stop(self) -> None:
        if self.owned:
            self.control("stop", timeout=90)
            if self.ready():
                raise RuntimeError("模型仍在运行，请检查模型控制脚本的停止操作")
            self.owned = False
