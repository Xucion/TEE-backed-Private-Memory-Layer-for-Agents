from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable


DEFAULT_MEDIA_KINDS = ["image", "emoji", "video", "video_thumb", "voice", "file"]
TERMINAL_STATUSES = {"done", "error", "cancelled"}
WECHAT_EXPORT_TOOL_NAME = "export_wechat_chat"
WECHAT_EXPORT_TOOL_SCHEMA: dict[str, Any] = {
    "name": WECHAT_EXPORT_TOOL_NAME,
    "description": "Export selected WeChat conversations through a local WeChatDataAnalysis API.",
    "parameters": {
        "type": "object",
        "properties": {
            "api_base": {
                "type": "string",
                "description": "WeChatDataAnalysis API base URL, for example http://127.0.0.1:10392.",
            },
            "account": {
                "type": ["string", "null"],
                "description": "Decrypted WeChat account directory name.",
            },
            "usernames": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Conversation usernames to export.",
            },
            "start_time": {
                "type": ["integer", "null"],
                "description": "Inclusive Unix seconds start time.",
            },
            "end_time": {
                "type": ["integer", "null"],
                "description": "Inclusive Unix seconds end time.",
            },
            "export_name": {
                "type": ["string", "null"],
                "description": "Output export name; .zip suffix is optional.",
            },
            "output_root": {
                "type": "string",
                "description": "Local directory for downloaded and extracted export files.",
            },
            "backend_output_dir": {
                "type": ["string", "null"],
                "description": "Optional absolute output_dir sent to the WeChatDataAnalysis backend.",
            },
            "include_media": {
                "type": "boolean",
                "description": "Whether to ask WeChatDataAnalysis to package media files.",
            },
            "privacy_mode": {
                "type": "boolean",
                "description": "Whether to enable WeChatDataAnalysis privacy_mode.",
            },
        },
        "required": ["api_base", "usernames", "output_root"],
    },
}


class WeChatExportApiError(RuntimeError):
    """Raised when the WeChatDataAnalysis export API cannot complete a job."""


@dataclass(frozen=True)
class WeChatExportRequest:
    api_base: str
    account: str | None
    usernames: list[str]
    start_time: int | None = None
    end_time: int | None = None
    export_name: str | None = None
    output_root: Path = Path("src/tools/wechatOutput")
    backend_output_dir: str | None = None
    include_media: bool = True
    media_kinds: list[str] | None = None
    privacy_mode: bool = False
    scope: str = "selected"
    export_format: str = "json"
    timeout_seconds: int = 600
    poll_interval_seconds: float = 1.0


@dataclass(frozen=True)
class WeChatExportResult:
    export_id: str
    status: str
    zip_path: Path
    export_dir: Path
    job: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "export_id": self.export_id,
            "status": self.status,
            "zip_path": str(self.zip_path),
            "export_dir": str(self.export_dir),
            "job": self.job,
        }


class WeChatExportApiClient:
    def __init__(
        self,
        api_base: str,
        *,
        urlopen: Callable[..., Any] | None = None,
        request_timeout_seconds: int = 30,
    ) -> None:
        base = str(api_base or "").strip().rstrip("/")
        if not base:
            raise ValueError("api_base is required.")
        self.api_base = base
        self._urlopen = urlopen or urllib.request.urlopen
        self._request_timeout_seconds = int(request_timeout_seconds)

    def create_export_job(
        self,
        *,
        account: str | None,
        scope: str,
        usernames: Iterable[str],
        export_format: str,
        start_time: int | None,
        end_time: int | None,
        include_media: bool,
        media_kinds: Iterable[str],
        backend_output_dir: str | None,
        privacy_mode: bool,
        export_name: str | None,
    ) -> dict[str, Any]:
        payload = {
            "account": account or None,
            "scope": scope,
            "usernames": [str(item).strip() for item in usernames if str(item).strip()],
            "format": export_format,
            "start_time": int(start_time) if start_time is not None else None,
            "end_time": int(end_time) if end_time is not None else None,
            "include_media": bool(include_media),
            "media_kinds": list(media_kinds),
            "output_dir": str(backend_output_dir).strip() if backend_output_dir else None,
            "privacy_mode": bool(privacy_mode),
            "file_name": _zip_file_name(export_name) if export_name else None,
        }
        if payload["scope"] == "selected" and not payload["usernames"]:
            raise ValueError("At least one username is required for selected export.")

        response = self._json_request("POST", "/api/chat/exports", payload)
        job = response.get("job")
        if response.get("status") != "success" or not isinstance(job, dict):
            raise WeChatExportApiError(f"Unexpected create export response: {response}")
        return job

    def get_export_job(self, export_id: str) -> dict[str, Any]:
        export_id = str(export_id or "").strip()
        if not export_id:
            raise ValueError("export_id is required.")
        response = self._json_request("GET", f"/api/chat/exports/{urllib.parse.quote(export_id)}", None)
        job = response.get("job")
        if response.get("status") != "success" or not isinstance(job, dict):
            raise WeChatExportApiError(f"Unexpected export status response: {response}")
        return job

    def wait_export_job(
        self,
        export_id: str,
        *,
        timeout_seconds: int = 600,
        poll_interval_seconds: float = 1.0,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + max(1, int(timeout_seconds))
        last_job: dict[str, Any] | None = None
        while time.monotonic() <= deadline:
            last_job = self.get_export_job(export_id)
            status = str(last_job.get("status") or "").strip()
            if status in TERMINAL_STATUSES:
                if status != "done":
                    error = str(last_job.get("error") or "").strip()
                    raise WeChatExportApiError(f"Export job {export_id} ended with status {status}: {error}")
                if not bool(last_job.get("zipReady")):
                    raise WeChatExportApiError(f"Export job {export_id} is done but zip is not ready.")
                return last_job
            time.sleep(max(0.2, float(poll_interval_seconds)))
        raise WeChatExportApiError(f"Timed out waiting for export job {export_id}: {last_job}")

    def download_export_zip(self, export_id: str, output_path: Path) -> Path:
        export_id = str(export_id or "").strip()
        if not export_id:
            raise ValueError("export_id is required.")
        output_path = output_path.resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        url = self._url(f"/api/chat/exports/{urllib.parse.quote(export_id)}/download")
        request = urllib.request.Request(url, method="GET")
        try:
            with self._urlopen(request, timeout=self._request_timeout_seconds) as response:
                data = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise WeChatExportApiError(f"Download failed: HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise WeChatExportApiError(f"Download failed: {exc}") from exc
        output_path.write_bytes(data)
        return output_path

    def _json_request(self, method: str, path: str, payload: dict[str, Any] | None) -> dict[str, Any]:
        data = None
        headers: dict[str, str] = {}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        request = urllib.request.Request(self._url(path), data=data, headers=headers, method=method)
        try:
            with self._urlopen(request, timeout=self._request_timeout_seconds) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise WeChatExportApiError(f"{method} {path} failed: HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise WeChatExportApiError(f"{method} {path} failed: {exc}") from exc
        try:
            parsed = json.loads(raw.decode("utf-8-sig"))
        except Exception as exc:
            raise WeChatExportApiError(f"{method} {path} returned invalid JSON.") from exc
        if not isinstance(parsed, dict):
            raise WeChatExportApiError(f"{method} {path} returned non-object JSON.")
        return parsed

    def _url(self, path: str) -> str:
        return self.api_base + "/" + str(path or "").lstrip("/")


def export_wechat_chat(request: WeChatExportRequest) -> WeChatExportResult:
    client = WeChatExportApiClient(request.api_base)
    job = client.create_export_job(
        account=request.account,
        scope=request.scope,
        usernames=request.usernames,
        export_format=request.export_format,
        start_time=request.start_time,
        end_time=request.end_time,
        include_media=request.include_media,
        media_kinds=request.media_kinds or DEFAULT_MEDIA_KINDS,
        backend_output_dir=request.backend_output_dir,
        privacy_mode=request.privacy_mode,
        export_name=request.export_name,
    )
    export_id = str(job.get("exportId") or "").strip()
    if not export_id:
        raise WeChatExportApiError(f"Create export response has no exportId: {job}")

    completed_job = client.wait_export_job(
        export_id,
        timeout_seconds=request.timeout_seconds,
        poll_interval_seconds=request.poll_interval_seconds,
    )

    export_stem = _export_stem(request.export_name, completed_job, export_id)
    zip_path = (request.output_root / f"{export_stem}.zip").resolve()
    export_dir = (request.output_root / export_stem).resolve()
    downloaded_zip = client.download_export_zip(export_id, zip_path)
    extract_zip_safely(downloaded_zip, export_dir)

    return WeChatExportResult(
        export_id=export_id,
        status=str(completed_job.get("status") or ""),
        zip_path=downloaded_zip,
        export_dir=export_dir,
        job=completed_job,
    )


def call_wechat_export_tool(arguments: dict[str, Any]) -> dict[str, Any]:
    """Tool-calling facade for enterprise agent runtimes.

    The agent supplies business arguments matching WECHAT_EXPORT_TOOL_SCHEMA.
    This function hides HTTP job creation, polling, zip download, and extraction.
    """

    if not isinstance(arguments, dict):
        raise ValueError("Tool arguments must be a JSON object.")
    request = WeChatExportRequest(
        api_base=str(arguments.get("api_base") or "").strip(),
        account=_optional_str(arguments.get("account")),
        usernames=_string_list(arguments.get("usernames")),
        start_time=_optional_int(arguments.get("start_time")),
        end_time=_optional_int(arguments.get("end_time")),
        export_name=_optional_str(arguments.get("export_name")),
        output_root=Path(str(arguments.get("output_root") or "src/tools/wechatOutput")),
        backend_output_dir=_optional_str(arguments.get("backend_output_dir")),
        include_media=bool(arguments.get("include_media", True)),
        privacy_mode=bool(arguments.get("privacy_mode", False)),
        timeout_seconds=int(arguments.get("timeout_seconds") or 600),
        poll_interval_seconds=float(arguments.get("poll_interval_seconds") or 1.0),
    )
    return export_wechat_chat(request).to_dict()


def extract_zip_safely(zip_path: Path, output_dir: Path) -> Path:
    zip_path = zip_path.resolve()
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.infolist():
            target = (output_dir / member.filename).resolve()
            if not _is_relative_to(target, output_dir):
                raise WeChatExportApiError(f"Zip member escapes output directory: {member.filename}")
        zf.extractall(output_dir)
    return output_dir


def _export_stem(export_name: str | None, job: dict[str, Any], export_id: str) -> str:
    if export_name:
        return _strip_zip_suffix(_safe_name(export_name))
    zip_path = str(job.get("zipPath") or "").strip()
    if zip_path:
        name = Path(zip_path.replace("\\", "/")).name
        if name:
            return _strip_zip_suffix(_safe_name(name))
    return f"wechat_chat_export_{export_id}"


def _zip_file_name(export_name: str | None) -> str | None:
    if not export_name:
        return None
    safe = _safe_name(export_name)
    if not safe:
        return None
    return safe if safe.lower().endswith(".zip") else f"{safe}.zip"


def _strip_zip_suffix(name: str) -> str:
    return name[:-4] if name.lower().endswith(".zip") else name


def _safe_name(value: str) -> str:
    text = str(value or "").strip().replace("\\", "_").replace("/", "_")
    bad_chars = '<>:"|?*\x00'
    for ch in bad_chars:
        text = text.replace(ch, "_")
    return text.strip(" .") or "wechat_chat_export"


def _optional_str(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        items = [value]
    else:
        items = list(value or [])
    return [str(item).strip() for item in items if str(item).strip()]


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
