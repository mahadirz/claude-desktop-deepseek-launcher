#!/usr/bin/env python3
"""
Launch Claude Desktop Cowork on 3P using DeepSeek's Anthropic-compatible API.

This writes a DeepSeek gateway profile and starts a local compatibility proxy
for Claude Desktop's 3P gateway mode.

Set DEEPSEEK_API_KEY before running, or pass --api-key:

    export DEEPSEEK_API_KEY=...
    python3 launch_claude_desktop_deepseek.py
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


PROFILE_ID = "00000000-0000-4000-8000-00000000d335"
PROFILE_NAME = "DeepSeek"
ORG_UUID = "00000000-0000-4000-8000-00000000d335"
DEFAULT_BASE_URL = "https://api.deepseek.com/anthropic"
DEFAULT_PROXY_HOST = "127.0.0.1"
DEFAULT_PROXY_PORT = 17631
DEFAULT_AUTH_SCHEME = "bearer"
DEFAULT_CLAUDE_ROUTE_MODELS = ["claude-sonnet-4-5", "anthropic/claude-sonnet-4-5"]
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-pro"
DEFAULT_DISPLAY_MODEL_MAP = [
    ("Claude Mythos Flash", "deepseek-v4-flash"),
    ("Claude Mythos", "deepseek-v4-pro"),
]
DEFAULT_ROUTE_MODEL_MAP = {
    route: upstream
    for route, (_, upstream) in zip(DEFAULT_CLAUDE_ROUTE_MODELS, DEFAULT_DISPLAY_MODEL_MAP)
}
DEFAULT_AUTO_ALLOWED_TOOLS = [
    "list_pages",
    "select_page",
    "navigate_page",
    "take_snapshot",
    "wait_for",
    "click",
    "fill",
    "evaluate_script",
]
DEFAULT_MODELS: list[Any] = [
    {"name": route, "labelOverride": label}
    for route, (label, _) in zip(DEFAULT_CLAUDE_ROUTE_MODELS, DEFAULT_DISPLAY_MODEL_MAP)
]


class LaunchError(RuntimeError):
    pass


def state_dir() -> Path:
    root = Path.home() / "Library" / "Application Support" / "Claude-3p"
    if platform.system().lower() == "windows":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            root = Path(local) / "Claude-3p"
    return root / "deepseek-launcher"


def route_model_map_path() -> Path:
    return state_dir() / "model-routes.json"


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        raise LaunchError(f"Cannot parse JSON at {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise LaunchError(f"Expected JSON object at {path}")
    return data


def write_json(path: Path, data: dict[str, Any], dry_run: bool = False) -> None:
    if dry_run:
        print(f"would write {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = path.with_name(f"{path.name}.{stamp}.bak")
        shutil.copy2(path, backup)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    tmp.replace(path)


def mac_paths() -> tuple[Path, Path]:
    base = Path.home() / "Library" / "Application Support"
    return base / "Claude", base / "Claude-3p"


def windows_paths() -> tuple[Path, Path]:
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        user_profile = os.environ.get("USERPROFILE")
        if not user_profile:
            raise LaunchError("LOCALAPPDATA or USERPROFILE is required on Windows")
        local = str(Path(user_profile) / "AppData" / "Local")
    return Path(local) / "Claude", Path(local) / "Claude-3p"


def config_roots() -> tuple[Path, Path]:
    system = platform.system().lower()
    if system == "darwin":
        return mac_paths()
    if system == "windows":
        return windows_paths()
    raise LaunchError("Claude Desktop 3P launch is supported here only on macOS and Windows")


def claude_app_exists() -> bool:
    system = platform.system().lower()
    if system == "darwin":
        return Path("/Applications/Claude.app").exists() or (
            Path.home() / "Applications" / "Claude.app"
        ).exists()
    if system == "windows":
        local = os.environ.get("LOCALAPPDATA", "")
        candidates = [
            Path(local) / "Programs" / "Claude" / "Claude.exe",
            Path(local) / "Programs" / "Claude Desktop" / "Claude.exe",
            Path(local) / "Claude" / "Claude.exe",
            Path(local) / "Claude Desktop" / "Claude.exe",
            Path(local) / "AnthropicClaude" / "Claude.exe",
        ]
        return any(path.exists() for path in candidates)
    return False


def set_deployment_mode(path: Path, mode: str, dry_run: bool) -> None:
    cfg = read_json(path)
    cfg["deploymentMode"] = mode
    write_json(path, cfg, dry_run)


def managed_mcp_servers_from_config(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    managed_servers: list[dict[str, Any]] = []
    mcp_servers = cfg.get("mcpServers")
    if not isinstance(mcp_servers, dict):
        return managed_servers

    for name, server in mcp_servers.items():
        if not isinstance(name, str) or not isinstance(server, dict):
            continue
        managed: dict[str, Any] = {
            "name": name,
            "transport": server.get("transport", "stdio"),
        }
        for key in ("command", "args", "env"):
            if key in server:
                managed[key] = server[key]
        allowed_tools = server.get("alwaysAllow")
        if not isinstance(allowed_tools, list):
            allowed_tools = DEFAULT_AUTO_ALLOWED_TOOLS if name == "chrome-devtools" else []
        if allowed_tools:
            managed["toolPolicy"] = {
                tool: "allow"
                for tool in allowed_tools
                if isinstance(tool, str)
            }
        managed_servers.append(managed)
    return managed_servers


def apply_cowork_auto_mode_config(path: Path, dry_run: bool) -> list[dict[str, Any]]:
    cfg = read_json(path)
    cfg["autoModeEnabled"] = True

    preferences = cfg.setdefault("preferences", {})
    if isinstance(preferences, dict):
        bypass_by_account = preferences.get("bypassPermissionsGateByAccount")
        if isinstance(bypass_by_account, dict):
            for account_id in list(bypass_by_account):
                bypass_by_account[account_id] = True

    managed_servers = managed_mcp_servers_from_config(cfg)
    if managed_servers:
        cfg["managedMcpServers"] = managed_servers

    write_json(path, cfg, dry_run)
    return managed_servers


def apply_profile(
    third_party_root: Path,
    api_key: str,
    base_url: str,
    auth_scheme: str,
    models: list[Any],
    managed_mcp_servers: list[dict[str, Any]],
    dry_run: bool,
) -> None:
    meta_path = third_party_root / "configLibrary" / "_meta.json"
    profile_path = third_party_root / "configLibrary" / f"{PROFILE_ID}.json"

    meta = read_json(meta_path)
    entries = [e for e in meta.get("entries", []) if not (isinstance(e, dict) and e.get("id") == PROFILE_ID)]
    entries.append({"id": PROFILE_ID, "name": PROFILE_NAME})
    meta["appliedId"] = PROFILE_ID
    meta["entries"] = entries
    write_json(meta_path, meta, dry_run)

    profile = read_json(profile_path)
    profile.update(
        {
            "deploymentOrganizationUuid": ORG_UUID,
            "inferenceProvider": "gateway",
            "inferenceGatewayBaseUrl": base_url.rstrip("/"),
            "inferenceGatewayApiKey": api_key,
            "inferenceGatewayAuthScheme": auth_scheme,
            "inferenceModels": models,
            "autoModeEnabled": True,
            "disableDeploymentModeChooser": True,
        }
    )
    if managed_mcp_servers:
        profile["managedMcpServers"] = managed_mcp_servers
    write_json(profile_path, profile, dry_run)


def restore_profile(normal_root: Path, third_party_root: Path, dry_run: bool) -> None:
    set_deployment_mode(normal_root / "claude_desktop_config.json", "1p", dry_run)
    set_deployment_mode(third_party_root / "claude_desktop_config.json", "1p", dry_run)

    meta_path = third_party_root / "configLibrary" / "_meta.json"
    profile_path = third_party_root / "configLibrary" / f"{PROFILE_ID}.json"

    meta = read_json(meta_path)
    if meta.get("appliedId") == PROFILE_ID:
        meta.pop("appliedId", None)
    meta["entries"] = [
        e for e in meta.get("entries", []) if not (isinstance(e, dict) and e.get("id") == PROFILE_ID)
    ]
    write_json(meta_path, meta, dry_run)

    profile = read_json(profile_path)
    for key in [
        "inferenceProvider",
        "inferenceGatewayBaseUrl",
        "inferenceGatewayApiKey",
        "inferenceGatewayAuthScheme",
        "inferenceModels",
        "deploymentOrganizationUuid",
    ]:
        profile.pop(key, None)
    profile["disableDeploymentModeChooser"] = False
    write_json(profile_path, profile, dry_run)


def existing_profile_api_key(third_party_root: Path) -> str:
    profile_path = third_party_root / "configLibrary" / f"{PROFILE_ID}.json"
    key = read_json(profile_path).get("inferenceGatewayApiKey", "")
    return key.strip() if isinstance(key, str) else ""


def is_claude_running() -> bool:
    system = platform.system().lower()
    if system == "darwin":
        result = subprocess.run(
            ["pgrep", "-f", "Claude.app/Contents/MacOS/Claude"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return result.returncode == 0
    if system == "windows":
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                "(Get-Process claude -ErrorAction SilentlyContinue | "
                "Where-Object { $_.MainWindowHandle -ne 0 } | "
                "Select-Object -First 1).Id",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        return bool(result.stdout.strip())
    return False


def quit_claude() -> None:
    system = platform.system().lower()
    if system == "darwin":
        subprocess.run(["osascript", "-e", 'tell application "Claude" to quit'], check=False)
        return
    if system == "windows":
        subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                "Get-Process claude -ErrorAction SilentlyContinue | "
                "Where-Object { $_.MainWindowHandle -ne 0 } | "
                "ForEach-Object { [void]$_.CloseMainWindow() }",
            ],
            check=False,
        )


def open_claude() -> None:
    system = platform.system().lower()
    if system == "darwin":
        subprocess.run(["open", "-a", "Claude"], check=True)
        return
    if system == "windows":
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", "Start-Process Claude"],
            check=True,
        )
        return
    raise LaunchError("Cannot open Claude Desktop on this platform")


def deepseek_headers_from_incoming(accept: str | None = None) -> dict[str, str]:
    return {
        "Accept": accept or "application/json",
        "Content-Type": "application/json",
    }


def sanitize_user_ids(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if key in {"user_id", "userid"}:
                out[key] = sanitize_user_id_value(item)
            else:
                out[key] = sanitize_user_ids(item)
        return out
    if isinstance(value, list):
        return [sanitize_user_ids(item) for item in value]
    return value


def rewrite_model_routes(value: Any, upstream_model: str = DEFAULT_DEEPSEEK_MODEL) -> Any:
    route_model_map = load_route_model_map()
    return rewrite_model_routes_with_map(value, route_model_map, upstream_model)


def rewrite_model_routes_with_map(
    value: Any,
    route_model_map: dict[str, str],
    upstream_model: str = DEFAULT_DEEPSEEK_MODEL,
) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if key == "model" and isinstance(item, str) and is_claude_route_model(item):
                out[key] = route_model_map.get(item, route_model_map.get(canonical_route_model(item), upstream_model))
            else:
                out[key] = rewrite_model_routes_with_map(item, route_model_map, upstream_model)
        return out
    if isinstance(value, list):
        return [rewrite_model_routes_with_map(item, route_model_map, upstream_model) for item in value]
    return value


def is_claude_route_model(value: str) -> bool:
    return value.startswith("claude-") or value.startswith("anthropic/claude-")


def canonical_route_model(value: str) -> str:
    if value.startswith("anthropic/"):
        value = value[len("anthropic/") :]
    if value.endswith("[1m]"):
        value = value[: -len("[1m]")]
    return value


def load_route_model_map() -> dict[str, str]:
    data = read_json(route_model_map_path())
    routes = data.get("routes")
    if not isinstance(routes, dict):
        return dict(DEFAULT_ROUTE_MODEL_MAP)
    out: dict[str, str] = {}
    for route, upstream in routes.items():
        if isinstance(route, str) and isinstance(upstream, str):
            out[route] = upstream
    return out or dict(DEFAULT_ROUTE_MODEL_MAP)


def write_route_model_map(route_model_map: dict[str, str], dry_run: bool) -> None:
    write_json(route_model_map_path(), {"routes": route_model_map}, dry_run)


def current_model_routes() -> list[str]:
    route_model_map = load_route_model_map()
    return list(route_model_map.keys())


def sanitize_user_id_value(value: Any) -> str:
    if not isinstance(value, str):
        return "claude_desktop"
    candidate = re.sub(r"[^a-zA-Z0-9_-]+", "_", value).strip("_")
    if not candidate:
        return "claude_desktop"
    return candidate[:128]


def estimate_input_tokens(value: Any) -> int:
    if isinstance(value, str):
        return max(1, len(value) // 4)
    if isinstance(value, dict):
        return sum(estimate_input_tokens(item) for item in value.values())
    if isinstance(value, list):
        return sum(estimate_input_tokens(item) for item in value)
    if value is None:
        return 0
    return max(1, len(str(value)) // 4)


class DeepSeekProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ClaudeDeepSeekProxy/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        message = "%s - %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), fmt % args)
        log_path = state_dir() / "proxy.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(message)

    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path.rstrip("/")
        if path == "/v1/models":
            body = json.dumps(
                {
                    "data": [
                        {"id": route, "type": "model"}
                        for route in current_model_routes()
                    ],
                    "object": "list",
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
            self.close_connection = True
            return
        self.send_error(404, "not found")

    def do_HEAD(self) -> None:
        path = urllib.parse.urlparse(self.path).path.rstrip("/")
        if path in {"", "/", "/v1", "/v1/models"}:
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            return
        self.send_error(404, "not found")

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path.rstrip("/")
        if path not in {"/v1/messages", "/v1/messages/count_tokens"}:
            self.send_error(404, "not found")
            return

        length = int(self.headers.get("Content-Length", "0") or "0")
        raw_body = self.rfile.read(length)
        try:
            body = json.loads(raw_body.decode("utf-8"))
        except json.JSONDecodeError:
            self.send_error(400, "invalid json")
            return

        if path == "/v1/messages/count_tokens":
            response = json.dumps({"input_tokens": estimate_input_tokens(body)}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(response)
            self.wfile.flush()
            self.close_connection = True
            return

        forwarded_body = rewrite_model_routes(sanitize_user_ids(body))
        forwarded = json.dumps(forwarded_body, separators=(",", ":")).encode("utf-8")
        req = urllib.request.Request(
            DEFAULT_BASE_URL + "/v1/messages",
            data=forwarded,
            method="POST",
            headers=deepseek_headers_from_incoming(self.headers.get("Accept")),
        )

        auth = self.headers.get("Authorization")
        x_api_key = self.headers.get("x-api-key")
        if auth:
            req.add_header("Authorization", "Bearer " + normalize_api_key(auth))
        elif x_api_key:
            req.add_header("Authorization", "Bearer " + normalize_api_key(x_api_key))
        else:
            self.send_error(401, "missing gateway credential")
            return

        for header in ("anthropic-version", "anthropic-beta"):
            value = self.headers.get(header)
            if value:
                req.add_header(header, value)

        try:
            with urllib.request.urlopen(req, timeout=None) as resp:
                self.send_response(resp.status)
                content_type = resp.headers.get("Content-Type", "application/json")
                for key, value in resp.headers.items():
                    if key.lower() in {"connection", "transfer-encoding", "content-encoding"}:
                        continue
                    self.send_header(key, value)
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    if content_type.startswith("text/event-stream"):
                        for line in resp:
                            self.wfile.write(line)
                            self.wfile.flush()
                    else:
                        while True:
                            chunk = resp.read(65536)
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                self.close_connection = True
        except urllib.error.HTTPError as exc:
            data = exc.read()
            self.send_response(exc.code)
            self.send_header("Content-Type", exc.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(data)
            self.wfile.flush()
            self.close_connection = True


def serve_proxy(host: str, port: int) -> int:
    state_dir().mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((host, port), DeepSeekProxyHandler)
    (state_dir() / "proxy.pid").write_text(str(os.getpid()) + "\n", encoding="utf-8")
    print(f"DeepSeek proxy listening on http://{host}:{port}", flush=True)
    server.serve_forever()
    return 0


def proxy_is_running(host: str, port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/v1/models", timeout=1) as resp:
            return resp.status == 200
    except Exception:
        return False


def stop_proxy() -> None:
    pid_path = state_dir() / "proxy.pid"
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except PermissionError:
        return
    deadline = time.time() + 3
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    try:
        pid_path.unlink()
    except FileNotFoundError:
        pass


def start_proxy(host: str, port: int, restart: bool = False) -> None:
    if restart:
        stop_proxy()
    if proxy_is_running(host, port):
        return
    log_path = state_dir() / "proxy.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log:
        subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--serve-proxy",
                "--proxy-host",
                host,
                "--proxy-port",
                str(port),
            ],
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    deadline = time.time() + 5
    while time.time() < deadline:
        if proxy_is_running(host, port):
            return
        time.sleep(0.1)
    raise LaunchError(f"DeepSeek proxy did not start; see {log_path}")


def launch_or_restart(no_launch: bool, yes: bool) -> None:
    if no_launch:
        return
    if not is_claude_running():
        open_claude()
        return
    if not yes:
        answer = input("Claude Desktop is running. Quit and restart it now? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("Quit and reopen Claude Desktop when you are ready for the profile change to take effect.")
            return
    quit_claude()
    deadline = time.time() + 30
    while time.time() < deadline:
        if not is_claude_running():
            open_claude()
            return
        time.sleep(0.2)
    raise LaunchError("Claude Desktop did not quit; quit it manually and run this launcher again")


def parse_models(values: list[str] | None) -> list[Any]:
    if not values:
        return DEFAULT_MODELS
    models: list[Any] = []
    for value in values:
        if value.endswith("[1m]"):
            models.append({"name": value, "supports1m": True})
        else:
            models.append(value)
    return models


def parse_model_maps(values: list[str] | None) -> tuple[list[Any], dict[str, str]]:
    if not values:
        return DEFAULT_MODELS, dict(DEFAULT_ROUTE_MODEL_MAP)

    models: list[Any] = []
    route_model_map: dict[str, str] = {}
    if len(values) > len(DEFAULT_CLAUDE_ROUTE_MODELS):
        raise LaunchError(f"Only {len(DEFAULT_CLAUDE_ROUTE_MODELS)} custom model mappings are available by default")

    for index, value in enumerate(values):
        label, sep, upstream = value.partition("=")
        if not sep or not label.strip() or not upstream.strip():
            raise LaunchError("Use --model-map DISPLAY_NAME=UPSTREAM, for example 'Claude Mythos=deepseek-v4-pro'")

        route = DEFAULT_CLAUDE_ROUTE_MODELS[index]
        label = label.strip()
        upstream = upstream.strip()
        if not upstream:
            raise LaunchError("Model map upstream value cannot be empty")

        model: dict[str, Any] = {"name": route, "labelOverride": label}
        models.append(model)
        route_model_map[route] = upstream

    return models, route_model_map


def normalize_api_key(value: str) -> str:
    value = value.strip()
    prefix = "bearer "
    if value.lower().startswith(prefix):
        return value[len(prefix) :].strip()
    return value


def validate_api_key(value: str) -> str:
    key = normalize_api_key(value)
    if not key.startswith("sk-"):
        raise LaunchError("DeepSeek API key must start with sk-. Pass the real key with --api-key or set DEEPSEEK_API_KEY.")
    if "*" in key or "..." in key:
        raise LaunchError("DeepSeek API key looks masked. Pass the full real key with --api-key or set DEEPSEEK_API_KEY.")
    return key


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Configure and launch Claude Desktop Cowork on 3P with DeepSeek Cloud API."
    )
    parser.add_argument("--api-key", default=os.environ.get("DEEPSEEK_API_KEY"), help="DeepSeek API key")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="DeepSeek Anthropic-compatible base URL")
    parser.add_argument(
        "--direct",
        action="store_true",
        help="Point Claude Desktop directly at DeepSeek instead of the local compatibility proxy.",
    )
    parser.add_argument("--proxy-host", default=DEFAULT_PROXY_HOST, help="Local proxy host")
    parser.add_argument("--proxy-port", type=int, default=DEFAULT_PROXY_PORT, help="Local proxy port")
    parser.add_argument("--serve-proxy", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--auth-scheme",
        choices=["auto", "bearer", "x-api-key"],
        default=DEFAULT_AUTH_SCHEME,
        help="How Claude Desktop sends the gateway credential. DeepSeek should use bearer.",
    )
    parser.add_argument(
        "--model",
        action="append",
        dest="models",
        help="Anthropic route model to expose in Claude Desktop. Repeat to add more. Defaults to claude-sonnet-4-5.",
    )
    parser.add_argument(
        "--model-map",
        action="append",
        dest="model_maps",
        help=(
            "Show DISPLAY_NAME in Claude Desktop and forward it to UPSTREAM. "
            "Example: 'Claude Mythos Flash=deepseek-v4-flash'"
        ),
    )
    parser.add_argument("--restore", action="store_true", help="Restore Claude Desktop to normal 1P mode")
    parser.add_argument("--no-launch", action="store_true", help="Write config but do not open Claude Desktop")
    parser.add_argument("--yes", "-y", action="store_true", help="Restart Claude Desktop without prompting")
    parser.add_argument("--dry-run", action="store_true", help="Print target writes without changing files")
    args = parser.parse_args()

    if args.serve_proxy:
        return serve_proxy(args.proxy_host, args.proxy_port)

    if args.models and args.model_maps:
        raise LaunchError("Use either --model or --model-map, not both")

    if not args.base_url.startswith("https://"):
        raise LaunchError("DeepSeek base URL must be https://")
    if not claude_app_exists():
        print("Warning: Claude Desktop app was not found in the usual install paths.", file=sys.stderr)

    normal_root, third_party_root = config_roots()
    if not args.restore and not args.api_key:
        args.api_key = existing_profile_api_key(third_party_root)
    if not args.restore and not args.api_key:
        raise LaunchError("Set DEEPSEEK_API_KEY, pass --api-key, or run once with an existing DeepSeek profile")
    if args.restore:
        restore_profile(normal_root, third_party_root, args.dry_run)
        print("Claude Desktop restored to normal 1P mode.")
    else:
        gateway_base_url = args.base_url.rstrip("/")
        if not args.direct:
            if args.model_maps:
                models, route_model_map = parse_model_maps(args.model_maps)
            else:
                models = parse_models(args.models)
                if args.models:
                    route_model_map = {
                        canonical_route_model(model["name"] if isinstance(model, dict) else model): DEFAULT_DEEPSEEK_MODEL
                        for model in models
                        if is_claude_route_model(model["name"] if isinstance(model, dict) else model)
                    } or dict(DEFAULT_ROUTE_MODEL_MAP)
                else:
                    route_model_map = dict(DEFAULT_ROUTE_MODEL_MAP)
            if not args.dry_run:
                write_route_model_map(route_model_map, args.dry_run)
            if not args.dry_run:
                start_proxy(args.proxy_host, args.proxy_port, restart=True)
            gateway_base_url = f"http://{args.proxy_host}:{args.proxy_port}"
        else:
            models = parse_models(args.models)
        set_deployment_mode(normal_root / "claude_desktop_config.json", "3p", args.dry_run)
        set_deployment_mode(third_party_root / "claude_desktop_config.json", "3p", args.dry_run)
        managed_mcp_servers = apply_cowork_auto_mode_config(
            normal_root / "claude_desktop_config.json", args.dry_run
        )
        managed_mcp_servers = apply_cowork_auto_mode_config(
            third_party_root / "claude_desktop_config.json", args.dry_run
        ) or managed_mcp_servers
        apply_profile(
            third_party_root=third_party_root,
            api_key=validate_api_key(args.api_key),
            base_url=gateway_base_url,
            auth_scheme=args.auth_scheme,
            models=models,
            managed_mcp_servers=managed_mcp_servers,
            dry_run=args.dry_run,
        )
        print("Claude Desktop profile changed to DeepSeek Cloud API.")
    launch_or_restart(args.no_launch or args.dry_run, args.yes)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (LaunchError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
