import json
import os
import subprocess
import time
from pathlib import Path
import requests
from .config import ROOT

class OpenSERPClient:
    def __init__(self, settings):
        self.settings = settings
        self.base = settings["base_url"].rstrip("/")
        self.process = None
        self.log = None

    def start(self):
        try:
            r = requests.get(self.base + "/health", timeout=3)
            if r.ok:
                return
        except requests.RequestException:
            pass
        binary = ROOT / "bin/openserp.exe"
        if not binary.exists():
            raise RuntimeError("Missing bin/openserp.exe; run setup-native.ps1")
        log_directory = Path(self.settings.get('log_directory',ROOT/'logs'))
        log_directory.mkdir(parents=True,exist_ok=True)
        self.log = (log_directory/'openserp-server.log').open("a", encoding="utf-8")
        self.process = subprocess.Popen(
            [str(binary), "serve", "--config", str(ROOT / "config/openserp.yaml"), "-a", "127.0.0.1", "-p", "7000"],
            cwd=ROOT, stdout=self.log, stderr=self.log,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        for _ in range(40):
            if self.process.poll() is not None:
                raise RuntimeError("OpenSERP exited; see logs/openserp-server.log")
            try:
                if requests.get(self.base + "/health", timeout=2).ok:
                    return
            except requests.RequestException:
                pass
            time.sleep(0.5)
        raise RuntimeError("OpenSERP did not become healthy")

    def search(self, text, engine="google", limit=10):
        response = requests.get(self.base + "/" + engine + "/search",
                                params={"text":text,"limit":limit,"lang":"EN","region":"US"},
                                timeout=self.settings["timeout_seconds"])
        response.raise_for_status()
        payload = response.json()
        results = payload if isinstance(payload, list) else payload.get("results", [])
        if not isinstance(payload, list) and payload.get("error"):
            raise RuntimeError(str(payload["error"]))
        return payload, [r for r in results if r.get("url") and r.get("type","organic") == "organic"]

    def close(self):
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        if self.log:
            self.log.close()

