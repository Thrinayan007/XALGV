"""GUI automation endpoints for the sandbox tool server.

Provides FastAPI endpoints for desktop GUI automation inside the X11 virtual desktop
(Xvfb on DISPLAY=:99) using xdotool, wmctrl, and scrot.

Includes:
- Strict coordinate bounds validation
- Mandatory post-action screenshot capture for feedback and observation
- Rigorous action safety (app whitelisting, hotkey sanitation, no shell injection)
- Comprehensive action logging
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/tools/gui", tags=["gui"])

WORKSPACE = Path("/workspace")
SCREENSHOTS_DIR = WORKSPACE / ".gui_screenshots"
DEFAULT_DISPLAY = ":99"
DEFAULT_WIDTH = 1280
DEFAULT_HEIGHT = 720

# Safe application whitelist for gui_launch_app
ALLOWED_APPS: dict[str, list[str]] = {
    "xterm": ["xterm"],
    "terminal": ["xterm"],
    "feh": ["feh"],
    "image_viewer": ["feh"],
    "libreoffice": ["libreoffice"],
    "soffice": ["soffice"],
    "calc": ["libreoffice", "--calc"],
    "writer": ["libreoffice", "--writer"],
    "impress": ["libreoffice", "--impress"],
}

# Strict whitelist regex for keyboard shortcuts / key names
# Accepts keys, functional keys, arrows, navigation, edit keys, and modifiers joined by '+'
VALID_HOTKEY_REGEX = re.compile(
    r"^(?:(?:ctrl|control|alt|shift|super|meta)\+)*(?:[a-zA-Z0-9]|F[1-9]|F1[0-2]|Return|Enter|BackSpace|Tab|Escape|Delete|Insert|Home|End|Page_Up|Page_Down|Prior|Next|Up|Down|Left|Right|space|plus|minus|slash|period|comma)$",
    re.IGNORECASE,
)


def get_screen_dimensions() -> tuple[int, int]:
    """Retrieve virtual desktop dimensions from environment or defaults."""
    res_env = os.environ.get("XVFB_RESOLUTION", "1280x720x24")
    m = re.match(r"^(\d+)x(\d+)", res_env)
    if m:
        return int(m.group(1)), int(m.group(2))
    return DEFAULT_WIDTH, DEFAULT_HEIGHT


def validate_coordinates(x: int, y: int, width: int | None = None, height: int | None = None) -> tuple[bool, str]:
    """Strictly validate that (x, y) coordinates reside within the screen bounds."""
    w, h = (width, height) if (width is not None and height is not None) else get_screen_dimensions()
    if not (0 <= x <= w):
        return False, f"X coordinate {x} is out of bounds [0, {w}]."
    if not (0 <= y <= h):
        return False, f"Y coordinate {y} is out of bounds [0, {h}]."
    return True, ""


# ── Request / Response models ────────────────────────────────────────────

class GuiResponse(BaseModel):
    success: bool
    output: str = ""
    error: str | None = None
    artifacts: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class GuiClickRequest(BaseModel):
    x: int = Field(..., description="Horizontal pixel coordinate (0-1280)")
    y: int = Field(..., description="Vertical pixel coordinate (0-720)")
    button: int = Field(default=1, description="Mouse button: 1=left, 2=middle, 3=right")
    double: bool = Field(default=False, description="Perform double click")
    capture_after: bool = Field(default=True, description="Capture screenshot after action for visual feedback")


class GuiTypeRequest(BaseModel):
    text: str = Field(..., description="Text to type into the active window")
    delay_ms: int = Field(default=12, ge=0, le=1000, description="Delay between keystrokes in ms")
    capture_after: bool = Field(default=True, description="Capture screenshot after action for visual feedback")


class GuiHotkeyRequest(BaseModel):
    keys: str = Field(..., description="Keyboard shortcut, e.g. 'ctrl+c', 'Return', 'Alt+F4'")
    capture_after: bool = Field(default=True, description="Capture screenshot after action for visual feedback")


class GuiScreenshotRequest(BaseModel):
    path: str | None = Field(default=None, description="Optional destination path in /workspace")


class GuiListWindowsRequest(BaseModel):
    all_desktops: bool = Field(default=True, description="List windows across all desktops")


class GuiLaunchAppRequest(BaseModel):
    app: str = Field(..., description="Authorized application name ('terminal', 'xterm', 'feh', 'libreoffice', etc.)")
    args: list[str] = Field(default_factory=list, description="Optional arguments (e.g. file path)")
    capture_after: bool = Field(default=True, description="Capture screenshot after application launch")
    wait_seconds: float = Field(default=1.0, ge=0.1, le=10.0, description="Wait seconds for window initialization")


# ── Internal Helpers ─────────────────────────────────────────────────────

async def _capture_screenshot(label: str = "gui_action") -> tuple[str, dict[str, Any]]:
    """Capture virtual desktop screenshot via scrot and store under /workspace/.gui_screenshots."""
    try:
        SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    timestamp = int(time.time() * 1000)
    filename = f"{label}_{timestamp}.png"
    target_path = SCREENSHOTS_DIR / filename
    tmp_path = Path("/tmp/screenshot.png")

    display = os.environ.get("DISPLAY", DEFAULT_DISPLAY)
    env = {**os.environ, "DISPLAY": display}

    try:
        proc = await asyncio.create_subprocess_exec(
            "scrot", "-z", str(target_path),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode == 0 and target_path.exists():
            file_size = target_path.stat().st_size
            try:
                shutil.copyfile(target_path, tmp_path)
            except Exception:
                pass
            return str(target_path), {
                "screenshot_path": str(target_path),
                "size_bytes": file_size,
                "timestamp": timestamp,
                "display": display,
            }
        err_msg = stderr.decode("utf-8", errors="replace").strip() if stderr else "scrot returned non-zero code"
        logger.warning("scrot capture failed: %s", err_msg)
        return "", {"error": f"Screenshot failed: {err_msg}"}
    except FileNotFoundError:
        logger.warning("scrot binary not installed or not found on PATH")
        return "", {"error": "scrot binary not found"}
    except Exception as exc:
        logger.warning("Screenshot error: %s", exc)
        return "", {"error": f"Screenshot error: {exc}"}


async def _list_windows_internal() -> list[dict[str, Any]]:
    """Query currently open desktop windows using wmctrl with xdotool fallback."""
    display = os.environ.get("DISPLAY", DEFAULT_DISPLAY)
    env = {**os.environ, "DISPLAY": display}
    windows: list[dict[str, Any]] = []

    # Primary method: wmctrl -l -p -G
    try:
        proc = await asyncio.create_subprocess_exec(
            "wmctrl", "-l", "-p", "-G",
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode == 0:
            for line in stdout.decode("utf-8", errors="replace").splitlines():
                parts = line.split(maxsplit=8)
                if len(parts) >= 9:
                    windows.append({
                        "window_id": parts[0],
                        "desktop": int(parts[1]) if parts[1].lstrip("-").isdigit() else 0,
                        "pid": int(parts[2]) if parts[2].isdigit() else None,
                        "x": int(parts[3]) if parts[3].isdigit() else 0,
                        "y": int(parts[4]) if parts[4].isdigit() else 0,
                        "width": int(parts[5]) if parts[5].isdigit() else 0,
                        "height": int(parts[6]) if parts[6].isdigit() else 0,
                        "client": parts[7],
                        "title": parts[8].strip(),
                    })
            if windows:
                return windows
    except Exception as exc:
        logger.debug("wmctrl query exception: %s", exc)

    # Fallback method: xdotool search
    try:
        proc = await asyncio.create_subprocess_exec(
            "xdotool", "search", "--onlyvisible", "--name", ".*",
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode == 0:
            for line in stdout.decode("utf-8", errors="replace").splitlines():
                win_id = line.strip()
                if not win_id:
                    continue
                name_proc = await asyncio.create_subprocess_exec(
                    "xdotool", "getwindowname", win_id,
                    env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                name_out, _ = await name_proc.communicate()
                title = name_out.decode("utf-8", errors="replace").strip()
                windows.append({
                    "window_id": hex(int(win_id)) if win_id.isdigit() else win_id,
                    "desktop": 0,
                    "pid": None,
                    "x": 0,
                    "y": 0,
                    "width": 0,
                    "height": 0,
                    "client": "unknown",
                    "title": title,
                })
    except Exception as exc:
        logger.debug("xdotool fallback exception: %s", exc)

    return windows


# ── Endpoints ────────────────────────────────────────────────────────────

@router.post("/click", response_model=GuiResponse)
async def gui_click(req: GuiClickRequest) -> GuiResponse:
    """Click at screen coordinates (x, y) with coordinate validation and post-action screenshot."""
    # 1. Coordinate validation
    valid, err_msg = validate_coordinates(req.x, req.y)
    if not valid:
        logger.warning("Coordinate validation failed for click: %s", err_msg)
        return GuiResponse(success=False, output="", error=f"Coordinate validation error: {err_msg}")

    if req.button not in (1, 2, 3):
        return GuiResponse(success=False, output="", error=f"Invalid mouse button {req.button}. Must be 1 (left), 2 (middle), or 3 (right).")

    display = os.environ.get("DISPLAY", DEFAULT_DISPLAY)
    env = {**os.environ, "DISPLAY": display}

    # 2. Safe execution via xdotool
    cmd = [
        "xdotool",
        "mousemove", str(req.x), str(req.y),
        "click",
        "--repeat", "2" if req.double else "1",
        str(req.button),
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace").strip()
            return GuiResponse(success=False, output="", error=f"xdotool click failed: {err}")

        click_type = "double click" if req.double else "click"
        btn_name = {1: "left", 2: "middle", 3: "right"}.get(req.button, str(req.button))
        out_msg = f"Successfully performed {btn_name} {click_type} at ({req.x}, {req.y})."
        artifacts: list[str] = []
        meta: dict[str, Any] = {"x": req.x, "y": req.y, "button": req.button, "double": req.double}

        # 3. Post-action screenshot feedback
        if req.capture_after:
            ss_path, ss_meta = await _capture_screenshot("click")
            if ss_path:
                artifacts.append(ss_path)
                meta["screenshot"] = ss_meta
                out_msg += f" Captured observation screenshot: {ss_path}"

        logger.info("GUI click executed: %s", out_msg)
        return GuiResponse(success=True, output=out_msg, artifacts=artifacts, metadata=meta)

    except FileNotFoundError:
        return GuiResponse(success=False, output="", error="xdotool binary not found on system.")
    except Exception as exc:
        logger.exception("Error executing GUI click")
        return GuiResponse(success=False, output="", error=f"GUI click error: {exc}")


@router.post("/type", response_model=GuiResponse)
async def gui_type(req: GuiTypeRequest) -> GuiResponse:
    """Type text into the focused window with keystroke delay and post-action screenshot."""
    if not req.text:
        return GuiResponse(success=False, output="", error="Text to type cannot be empty.")

    display = os.environ.get("DISPLAY", DEFAULT_DISPLAY)
    env = {**os.environ, "DISPLAY": display}

    # Safe execution: argv list with '--' prevents argument injection into xdotool
    cmd = ["xdotool", "type", "--delay", str(req.delay_ms), "--", req.text]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace").strip()
            return GuiResponse(success=False, output="", error=f"xdotool type failed: {err}")

        preview = req.text if len(req.text) <= 30 else f"{req.text[:27]}..."
        out_msg = f"Typed {len(req.text)} characters ('{preview}') with {req.delay_ms}ms delay."
        artifacts: list[str] = []
        meta: dict[str, Any] = {"char_count": len(req.text), "delay_ms": req.delay_ms}

        # Post-action screenshot feedback
        if req.capture_after:
            ss_path, ss_meta = await _capture_screenshot("type")
            if ss_path:
                artifacts.append(ss_path)
                meta["screenshot"] = ss_meta
                out_msg += f" Captured observation screenshot: {ss_path}"

        logger.info("GUI type executed: typed %d chars", len(req.text))
        return GuiResponse(success=True, output=out_msg, artifacts=artifacts, metadata=meta)

    except FileNotFoundError:
        return GuiResponse(success=False, output="", error="xdotool binary not found on system.")
    except Exception as exc:
        logger.exception("Error executing GUI type")
        return GuiResponse(success=False, output="", error=f"GUI type error: {exc}")


@router.post("/hotkey", response_model=GuiResponse)
async def gui_hotkey(req: GuiHotkeyRequest) -> GuiResponse:
    """Execute a keyboard shortcut with strict key sanitation and post-action screenshot."""
    keys_clean = req.keys.strip()
    if not keys_clean:
        return GuiResponse(success=False, output="", error="Hotkey keys parameter cannot be empty.")

    # Validate against strict regex to prevent command injection
    if not VALID_HOTKEY_REGEX.match(keys_clean):
        return GuiResponse(
            success=False,
            output="",
            error=(
                f"Invalid or unsafe hotkey '{req.keys}'. Only standard key combinations are allowed "
                "(e.g. 'ctrl+c', 'Return', 'Alt+F4', 'ctrl+alt+t', 'BackSpace')."
            ),
        )

    display = os.environ.get("DISPLAY", DEFAULT_DISPLAY)
    env = {**os.environ, "DISPLAY": display}

    cmd = ["xdotool", "key", keys_clean]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace").strip()
            return GuiResponse(success=False, output="", error=f"xdotool hotkey failed: {err}")

        out_msg = f"Executed keyboard shortcut: '{keys_clean}'."
        artifacts: list[str] = []
        meta: dict[str, Any] = {"keys": keys_clean}

        # Post-action screenshot feedback
        if req.capture_after:
            ss_path, ss_meta = await _capture_screenshot("hotkey")
            if ss_path:
                artifacts.append(ss_path)
                meta["screenshot"] = ss_meta
                out_msg += f" Captured observation screenshot: {ss_path}"

        logger.info("GUI hotkey executed: %s", keys_clean)
        return GuiResponse(success=True, output=out_msg, artifacts=artifacts, metadata=meta)

    except FileNotFoundError:
        return GuiResponse(success=False, output="", error="xdotool binary not found on system.")
    except Exception as exc:
        logger.exception("Error executing GUI hotkey")
        return GuiResponse(success=False, output="", error=f"GUI hotkey error: {exc}")


@router.post("/screenshot", response_model=GuiResponse)
async def gui_screenshot(req: GuiScreenshotRequest) -> GuiResponse:
    """Capture full virtual desktop screenshot."""
    dest_path = None
    if req.path:
        p = Path(req.path)
        if not p.is_absolute():
            p = WORKSPACE / p
        resolved = p.resolve()
        if not resolved.is_relative_to(WORKSPACE.resolve()):
            return GuiResponse(success=False, output="", error=f"Destination path must remain inside /workspace: {req.path}")
        dest_path = resolved

    ss_path, ss_meta = await _capture_screenshot("desktop")
    if not ss_path:
        return GuiResponse(success=False, output="", error=ss_meta.get("error", "Failed to capture desktop screenshot."))

    if dest_path:
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ss_path, dest_path)
        ss_path = str(dest_path)
        ss_meta["screenshot_path"] = ss_path

    w, h = get_screen_dimensions()
    out_msg = f"Captured desktop screenshot ({w}x{h}): {ss_path}"
    return GuiResponse(
        success=True,
        output=out_msg,
        artifacts=[ss_path],
        metadata={**ss_meta, "width": w, "height": h},
    )


@router.post("/list_windows", response_model=GuiResponse)
async def gui_list_windows(req: GuiListWindowsRequest) -> GuiResponse:
    """Identify available open windows on the virtual desktop."""
    windows = await _list_windows_internal()
    if not windows:
        return GuiResponse(
            success=True,
            output="No open application windows detected on the virtual desktop.",
            artifacts=[],
            metadata={"windows": [], "count": 0},
        )

    lines = [f"Detected {len(windows)} window(s):"]
    for idx, win in enumerate(windows, 1):
        lines.append(
            f"  {idx}. ID: {win['window_id']} | Title: '{win['title']}' | Geometry: {win['width']}x{win['height']} at ({win['x']},{win['y']})"
        )

    return GuiResponse(
        success=True,
        output="\n".join(lines),
        artifacts=[],
        metadata={"windows": windows, "count": len(windows)},
    )


@router.post("/launch_app", response_model=GuiResponse)
async def gui_launch_app(req: GuiLaunchAppRequest) -> GuiResponse:
    """Launch an authorized desktop application on the virtual desktop with observation feedback."""
    app_key = req.app.strip().lower()
    if app_key not in ALLOWED_APPS:
        return GuiResponse(
            success=False,
            output="",
            error=(
                f"Application '{req.app}' is not authorized. "
                f"Allowed applications: {sorted(set(ALLOWED_APPS.keys()))}"
            ),
        )

    # Sanitize arguments: no shell metacharacters
    for arg in req.args:
        if any(char in arg for char in (";", "&", "|", "`", "$", "\n", "\r")):
            return GuiResponse(
                success=False,
                output="",
                error=f"Unsafe argument detected in app launch parameters: '{arg}'",
            )
        # If argument looks like a local file path, ensure it remains inside /workspace
        if arg.startswith(("/", ".")):
            p = Path(arg)
            if not p.is_absolute():
                p = WORKSPACE / p
            try:
                resolved = p.resolve()
                if not resolved.is_relative_to(WORKSPACE.resolve()):
                    return GuiResponse(
                        success=False,
                        output="",
                        error=f"Path argument must remain inside workspace: '{arg}'",
                    )
            except Exception:
                pass

    base_cmd = list(ALLOWED_APPS[app_key])
    full_cmd = base_cmd + req.args

    display = os.environ.get("DISPLAY", DEFAULT_DISPLAY)
    env = {**os.environ, "DISPLAY": display}

    try:
        proc = await asyncio.create_subprocess_exec(
            *full_cmd,
            env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

        # Allow application window to initialize
        await asyncio.sleep(req.wait_seconds)

        # Check if windows appeared
        windows = await _list_windows_internal()
        app_windows = [w for w in windows if any(term in w["title"].lower() for term in (app_key, "terminal", "xterm", "calc", "writer"))]

        out_msg = f"Launched application '{req.app}' (PID: {proc.pid})."
        if app_windows:
            out_msg += f" Window detected: '{app_windows[0]['title']}' (ID: {app_windows[0]['window_id']})."
        else:
            out_msg += f" Total windows on desktop: {len(windows)}."

        artifacts: list[str] = []
        meta: dict[str, Any] = {
            "app": req.app,
            "pid": proc.pid,
            "windows": windows,
            "app_windows": app_windows,
        }

        # Post-action screenshot feedback
        if req.capture_after:
            ss_path, ss_meta = await _capture_screenshot(f"launch_{app_key}")
            if ss_path:
                artifacts.append(ss_path)
                meta["screenshot"] = ss_meta
                out_msg += f" Captured observation screenshot: {ss_path}"

        logger.info("GUI app launched: %s (PID %d)", req.app, proc.pid)
        return GuiResponse(success=True, output=out_msg, artifacts=artifacts, metadata=meta)

    except FileNotFoundError:
        return GuiResponse(success=False, output="", error=f"Binary for '{req.app}' ({base_cmd[0]}) not found on system.")
    except Exception as exc:
        logger.exception("Error launching GUI app '%s'", req.app)
        return GuiResponse(success=False, output="", error=f"App launch error: {exc}")

