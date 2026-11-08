"""Validated foundation contracts; legacy output/error fields remain compatible."""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SessionRequest(Request):
    action: Literal["start", "stop", "restart", "health"] = "start"


class OpenRequest(Request):
    url: str = Field(min_length=1, max_length=8192)
    new_tab: bool = False
    wait_for: Literal["load", "domcontentloaded", "networkidle", "commit"] = "load"


class TabsRequest(Request):
    action: Literal["list", "new", "switch", "close", "back", "forward", "reload"] = "list"
    tab: str | None = None
    url: str | None = None

    @model_validator(mode="after")
    def target_required(self):
        if self.action in {"switch", "close"} and not self.tab:
            raise ValueError("tab is required for switch and close")
        if self.url is not None and self.action != "new":
            raise ValueError("url is only supported for new tabs")
        return self


class ScreenshotRequest(Request):
    full_page: bool = False
    selector: str | None = None
    path: str | None = None
    marks: bool = False

    @model_validator(mode="after")
    def mark_scope(self):
        if self.marks and self.selector:
            raise ValueError("Marked screenshots capture the page, not an element selector")
        return self


class ObserveRequest(Request):
    scope: Literal["viewport", "page"] = "viewport"
    text: bool = False
    max_elements: int = Field(default=30, ge=1, le=100)
    max_chars: int = Field(default=2400, ge=800, le=8000)


class TargetRequest(Request):
    selector: str | None = None
    ref: int | None = Field(default=None, ge=1)
    snapshot_id: str | None = None

    @model_validator(mode="after")
    def reference_pair(self):
        if (self.ref is None) != (self.snapshot_id is None):
            raise ValueError("ref and snapshot_id must be provided together")
        if self.ref is not None and self.selector is not None:
            raise ValueError("Choose a reference or a selector")
        return self


class ClickRequest(TargetRequest):
    text: str | None = None
    role: str | None = None

    @model_validator(mode="after")
    def click_target(self):
        if self.ref is not None and (self.text is not None or self.role is not None):
            raise ValueError("Reference targeting cannot be mixed with text or role")
        if not (self.selector or self.text or self.ref):
            raise ValueError("Provide ref + snapshot_id, selector, or text")
        return self


class TypeRequest(TargetRequest):
    text: str
    clear_first: bool = True
    press_enter: bool = False

    @model_validator(mode="after")
    def type_target(self):
        if not (self.selector or self.ref):
            raise ValueError("Provide ref + snapshot_id or selector")
        return self


class PressRequest(Request):
    key: str = Field(min_length=1, max_length=80)


class WaitRequest(Request):
    condition: Literal["text", "selector", "idle", "ms"]
    value: str | None = Field(default=None, max_length=2000)
    timeout_ms: int = Field(default=5000, ge=1, le=30000)
    milliseconds: int = Field(default=400, ge=0, le=10000)

    @model_validator(mode="after")
    def wait_target(self):
        if self.condition in {"text", "selector"} and not self.value:
            raise ValueError("text/selector waits require value")
        return self


class ScrollRequest(TargetRequest):
    direction: Literal["up", "down", "left", "right", "top", "bottom"] = "down"
    pixels: int = Field(default=600, ge=0, le=10000)
    wait_ms: int = Field(default=100, ge=0, le=5000)


class OpenStep(Request):
    tool: Literal["open"]
    args: OpenRequest


class ObserveStep(Request):
    tool: Literal["observe"]
    args: ObserveRequest = Field(default_factory=ObserveRequest)


class ClickStep(Request):
    tool: Literal["click"]
    args: ClickRequest


class TypeStep(Request):
    tool: Literal["type"]
    args: TypeRequest


class PressStep(Request):
    tool: Literal["press"]
    args: PressRequest


class ScrollStep(Request):
    tool: Literal["scroll"]
    args: ScrollRequest = Field(default_factory=ScrollRequest)


class TabsStep(Request):
    tool: Literal["tabs"]
    args: TabsRequest = Field(default_factory=TabsRequest)


class WaitStep(Request):
    tool: Literal["wait"]
    args: WaitRequest


class ScreenshotStep(Request):
    tool: Literal["screenshot"]
    args: ScreenshotRequest = Field(default_factory=ScreenshotRequest)


BrowserStep = Annotated[
    OpenStep | ObserveStep | ClickStep | TypeStep | PressStep | ScrollStep | TabsStep | WaitStep | ScreenshotStep,
    Field(discriminator="tool"),
]


class ApprovalRequest(Request):
    tool: Literal["click", "type", "press"]
    args: dict[str, Any]


class ImageRequest(Request):
    path: str


class ArtifactCheck(Request):
    kind: Literal["artifact"]
    path: str = Field(min_length=1)
    min_bytes: int = Field(default=1, ge=1)
    contains: str | None = None
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    png: bool = False
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)


class PageCheck(Request):
    kind: Literal["url", "text", "tab_count"]
    expected: str = Field(min_length=1, max_length=10000)


class VerificationRequest(Request):
    checks: list[Annotated[ArtifactCheck | PageCheck, Field(discriminator="kind")]] = Field(min_length=1, max_length=50)


class RunRequest(Request):
    steps: list[BrowserStep] = Field(min_length=1, max_length=20)
    timeout_ms: int = Field(default=60000, ge=1, le=90000)
    action_budget: int = Field(default=20, ge=1, le=20)

    @model_validator(mode="after")
    def within_allowance(self):
        if len(self.steps) > self.action_budget:
            raise ValueError("Macro exceeds its action allowance")
        return self


class TabInfo(BaseModel):
    tab_id: str
    url: str
    title: str
    active: bool


class BrowserResponse(BaseModel):
    success: bool
    output: str = ""
    error: str | None = None
    error_code: str | None = None
    action: str = ""
    session_id: str = ""
    generation: int = 0
    tab_id: str | None = None
    restarted: bool = False
    snapshot_id: str | None = None
    state_before: str | None = None
    state_after: str | None = None
    state_changed: bool = False
    artifacts: list[str] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)
    # Mechanical evidence is deliberately distinct from the user's task outcome.
    action_verification: dict[str, Any] = Field(default_factory=dict)
    task_verification: Literal["not_evaluated", "passed", "failed"] = "not_evaluated"
