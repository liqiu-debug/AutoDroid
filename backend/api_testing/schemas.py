from typing import Any, Dict, List, Literal, Optional, Union
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator, model_serializer

Path = List[Union[StrictStr, StrictInt]]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Value(Contract):
    """Tagged values avoid interpreting user JSON as executable expressions."""

    kind: Literal["literal", "env", "ref", "template", "object", "array"] = "literal"
    value: Any = None
    name: str = ""
    step_id: str = ""
    path: Path = Field(default_factory=list)
    parts: List["Value"] = Field(default_factory=list)
    fields: Dict[str, "Value"] = Field(default_factory=dict)
    items: List["Value"] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_value(self):
        if self.kind == "env" and not self.name:
            raise ValueError("环境变量名称不能为空")
        if self.kind == "ref" and (not self.step_id or not self.path):
            raise ValueError("引用需要步骤 ID 和响应字段路径")
        if self.kind == "literal" and isinstance(self.value, (dict, list)):
            raise ValueError("对象和数组必须使用结构化值")
        if self.kind == "template" and any(p.kind not in {"literal", "env", "ref"} for p in self.parts):
            raise ValueError("文本模板只支持标量、环境变量和步骤引用")
        return self


def literal(value=None):
    return Value(value=value)


def from_json(value):
    if isinstance(value, dict):
        return Value(kind="object", fields={k: from_json(v) for k, v in value.items()})
    if isinstance(value, list):
        return Value(kind="array", items=[from_json(v) for v in value])
    return literal(value)


class Parameter(Contract):
    name: str = Field(min_length=1, max_length=512)
    value: Value = Field(default_factory=lambda: literal(""))
    enabled: bool = True


class Auth(Contract):
    kind: Literal["none", "bearer", "basic", "api_key"] = "none"
    token: Value = Field(default_factory=lambda: literal(""))
    username: Value = Field(default_factory=lambda: literal(""))
    password: Value = Field(default_factory=lambda: literal(""))
    key_name: str = "X-API-Key"
    location: Literal["header", "query"] = "header"


class RequestSpec(Contract):
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"] = "GET"
    url: Value = Field(default_factory=lambda: literal(""))
    path_params: List[Parameter] = Field(default_factory=list)
    query: List[Parameter] = Field(default_factory=list)
    headers: List[Parameter] = Field(default_factory=list)
    body_type: Literal["none", "json", "form", "text"] = "none"
    body: Value = Field(default_factory=literal)
    form: List[Parameter] = Field(default_factory=list)
    auth: Auth = Field(default_factory=Auth)
    timeout_seconds: float = Field(default=30, gt=0, le=120)


class Assertion(Contract):
    id: str = Field(default_factory=lambda: str(uuid4()))
    path: Path = Field(default_factory=lambda: ["status_code"])
    op: Literal["eq", "ne", "contains", "exists", "not_empty", "gt", "gte", "lt", "lte", "type", "length", "is_2xx"] = (
        "is_2xx"
    )
    expected: Value = Field(default_factory=literal)


class DefinitionConfig(Contract):
    request: RequestSpec = Field(default_factory=RequestSpec)
    assertions: List[Assertion] = Field(default_factory=lambda: [Assertion()])
    # Legacy field retained for compatibility; results now display raw values.
    sensitive_paths: List[Path] = Field(default_factory=list)


class ResponseField(Contract):
    path: Path
    type: Literal["string", "number", "boolean", "null", "object", "array"]
    example: Any = None
    children: List["ResponseField"] = Field(default_factory=list)
    truncated: bool = False

    @model_serializer(mode="wrap")
    def serialize_field(self, handler):
        data = handler(self)
        if "example" not in self.model_fields_set or self.type in {"object", "array"}:
            data.pop("example", None)
        return data


class ResponseSchema(Contract):
    """Editor-only examples. Never used as runtime outputs or Cookie state."""
    fields: List[ResponseField] = Field(default_factory=list)
    source: Literal["debug", "sample"] = "sample"
    env_id: Optional[int] = None
    env_name: str = ""
    captured_at: str = ""

    @model_validator(mode="after")
    def validate_size(self):
        def count(nodes, depth=0):
            if depth > 32:
                raise ValueError("响应字段层级不能超过 32 层")
            return sum(1 + count(node.children, depth + 1) for node in nodes)
        if count(self.fields) > 2000:
            raise ValueError("响应字段不能超过 2000 个")
        return self


class Step(Contract):
    id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=64)
    name: str = Field(default="接口步骤", min_length=1, max_length=200)
    kind: Literal["request", "wait"] = "request"
    interface_id: Optional[int] = None
    interface_version: Optional[int] = None
    snapshot: DefinitionConfig = Field(default_factory=DefinitionConfig)
    # Request fields are overridden as a whole; nested JSON bodies are never
    # accidentally deep-merged, which would resurrect explicitly removed keys.
    overrides: Dict[str, Any] = Field(default_factory=dict)
    seconds: float = Field(default=1, ge=0, le=300)
    # Only failures that prove the request never reached the server are ever
    # repeated, so a write operation cannot be silently duplicated.
    retry_count: int = Field(default=0, ge=0, le=3)
    response_schema: Optional[ResponseSchema] = None

    @model_validator(mode="after")
    def check_overrides(self):
        if set(self.overrides) - {"request", "assertions", "sensitive_paths"}:
            raise ValueError("不支持的步骤覆盖项")
        self.effective()
        return self

    def effective(self) -> DefinitionConfig:
        data = self.snapshot.model_dump()
        overrides = dict(self.overrides)
        request = overrides.pop("request", {})
        if not isinstance(request, dict):
            raise ValueError("请求覆盖必须是对象")
        data["request"].update(request)
        data.update(overrides)
        return DefinitionConfig.model_validate(data)


class SampleType(Contract):
    path: Path
    type: Literal["string", "number", "boolean", "null", "object", "array"]


class DefinitionWrite(Contract):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    folder_id: Optional[int] = None
    version: Optional[int] = Field(default=None, ge=1)
    config: DefinitionConfig = Field(default_factory=DefinitionConfig)
    sample: Any = None
    sample_types: List[SampleType] = Field(default_factory=list, max_length=2000)


class ScenarioWrite(Contract):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    folder_id: Optional[int] = None
    version: Optional[int] = Field(default=None, ge=1)
    env_id: Optional[int] = None
    steps: List[Step] = Field(default_factory=list, max_length=100)


class PrecheckRequest(ScenarioWrite):
    validation_mode: Literal["debug", "run"] = "run"


class FolderWrite(Contract):
    name: str = Field(min_length=1, max_length=100)
    kind: Literal["interface", "scenario"] = "interface"
    parent_id: Optional[int] = None


class RunRequest(Contract):
    env_id: Optional[int] = None
    notify: bool = False
    version: int = Field(ge=1)


class DebugCreate(Contract):
    editor_id: str = Field(min_length=1, max_length=100)
    env_id: Optional[int] = None


class DebugExecute(Contract):
    editor_id: str
    env_id: Optional[int] = None
    steps: List[Step] = Field(min_length=1, max_length=100)
    step_id: str
    mode: Literal["single", "through"] = "single"


class CurlInput(Contract):
    command: str = Field(min_length=1, max_length=100000)


class SyncItem(Contract):
    scenario_id: int
    step_id: str = Field(min_length=1, max_length=64)
    # The caller's view of the scenario version; a mismatch refuses the write
    # rather than silently discarding someone else's edit.
    version: int = Field(ge=1)
    choices: Dict[str, Literal["keep", "template"]] = Field(default_factory=dict)


class SyncApplyRequest(Contract):
    items: List[SyncItem] = Field(min_length=1, max_length=200)


class SpecInput(Contract):
    # Matches specs.MAX_SPEC_CHARS so oversized documents fail at the contract.
    content: str = Field(min_length=1, max_length=2000000)


class SpecApplyRequest(Contract):
    folder_id: Optional[int] = None
    items: List[DefinitionWrite] = Field(min_length=1, max_length=200)
