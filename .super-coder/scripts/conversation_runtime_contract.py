"""F89 experimental native driver contract; production adapters are unchanged.

A session controller owns one driver for its entire generation. Drivers own
continuous native readers/descriptors, never an API lifetime or submission
ledger. Native writes return bounded receipts; processing, terminal, work and
control outcomes arrive independently through emit(). No native ID is an OS PID.
"""
from __future__ import annotations

import abc
import hashlib
import json
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

CONTRACT_REVISION = "f89-native-runtime-v1"
MAX_FRAME_BYTES = 256 * 1024
MAX_EVENT_BYTES = 64 * 1024
EVENT_KINDS = frozenset({
    "runtime.ready", "runtime.setup", "runtime.lost", "ownership.failed",
    "activity.started", "activity.processed", "activity.terminal",
    "output.delta", "output.final", "work.observed", "work.terminal",
    "snapshot.observed", "control.acknowledged", "control.outcome",
    "capability.observed",
})
# Only ephemeral output may be truncated; identity/intents/outcomes require
# reserved journal capacity and must never become silent success.
OUTPUT_KINDS = frozenset({"output.delta"})
CAP_SUBMISSION = "submission"
CAP_STOP_REPLY = "stop_reply"
CAP_STOP_WORK = "stop_work"
CAP_AUTOMATION = "automation"
# A separately checked operation, not a grant inherited from submission.
CAP_HISTORY_RESUME = "history_resume"
# Shared GUI/checker operation names; diagnostics may retain additional native
# capability names, but repeated input must never be keyed as 'conversation'.
GUI_CAPABILITIES = frozenset({CAP_SUBMISSION,CAP_STOP_REPLY,CAP_STOP_WORK,CAP_AUTOMATION,CAP_HISTORY_RESUME})
# A shared requirement, not evidence that any installed provider can resume.
HISTORY_COVERAGE = frozenset({
    'consumed_history_interface', 'owned_history_baseline', 'native_history_identity',
    'current_workspace', 'no_history_prompt_replay', 'no_restored_work_definitions',
    'pre_resume_goal_tool_policy', 'repeated_input', 'native_cleanup', 'owned_unit_cleanup',
})
# Private socket wire: one UTF-8 JSON object + newline per connection, <=256KiB.
# Request generation/contract/op; reply {ok:true,result:{...}} or
# {ok:false,error:<stable code>,detail:<redacted detail>}. No auth env/token
# in asset frames: owner-only endpoint + SO_PEERCRED/owned cgroup authorize.
def asset_frame(generation: str, payload: Mapping[str, Any], *, timeout: float = 1) -> dict[str, Any]:
    return {"generation":generation,"contract":CONTRACT_REVISION,"op":"asset",
            "payload":dict(payload),"timeout":min(max(timeout,0.01),5)}

Grade = Literal["compatible", "incompatible", "inconclusive", "unverified"]
Freshness = Literal["current", "last_observed", "stale", "unknown"]
ActivitySource = Literal["gui", "native_completion", "automation", "reconciliation", "system"]
WriteState = Literal["not_written", "written", "unknown", "unsupported", "rejected"]
_SENSITIVE_KEYS = frozenset({
    "token", "api_key", "authorization", "credentials", "credential", "password",
    "secret", "env", "environment", "thinking", "reasoning", "analysis",
})
_IDENTITY_KEYS = frozenset({"generation_id","conversation_id","root_id","thread_id",
                          "parent_thread_id","activity_id","native_activity_id","item_id",
                          "work_id","native_process_id","request_id","control_id",
                          "boot_digest","policy_digest","payload_digest","sha256",
                          "setup_id","configuration_sha256","executable_sha256",
                          "source_conversation_id","source_generation_id","native_root_id",
                          "source_boot_digest","source_policy_digest","cleanup_digest",
                          "source_binding_digest","creation_provenance_digest","implementation_digest",
                          "fingerprint_key"})


class RuntimeContractError(RuntimeError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code


def payload_digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def public_payload(value: Any, *, sensitive_values: tuple[str, ...] = ()) -> Any:
    """Scrub structured keys AND known credential/environment values.

    The controller persistence boundary must pass its ephemeral known sensitive
    values for every field (including receipt detail/provenance/output), not just
    event data. Key filtering alone cannot redact secrets embedded in strings.
    Private reasoning must be excluded by driver normalization before emission.
    """
    if isinstance(value, Mapping):
        for key,item in value.items():
            if key in _IDENTITY_KEYS and isinstance(item,str) and any(secret and secret in item for secret in sensitive_values):
                raise RuntimeContractError("IDENTITY_PRIVATE", "identity contains private material; refusing persistence without rewriting its target")
        return {str(key): public_payload(item, sensitive_values=sensitive_values) for key, item in value.items()
                if str(key).lower() not in _SENSITIVE_KEYS}
    if isinstance(value, (list, tuple)):
        return [public_payload(item, sensitive_values=sensitive_values) for item in value]
    if isinstance(value, str):
        for secret in sorted(set(sensitive_values), key=len, reverse=True):
            if secret:
                value = value.replace(secret, "[redacted]")
        return value
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise RuntimeContractError("EVENT_INVALID", "event payload is not JSON data")


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    start_ticks: int

    def __post_init__(self) -> None:
        if self.pid <= 0 or self.start_ticks < 0:
            raise RuntimeContractError("PROCESS_INVALID", "invalid OS process identity")


@dataclass(frozen=True)
class ExecutableBinding:
    path: Path
    sha256: str
    version: str


@dataclass(frozen=True)
class HistorySourceIdentity:
    """Retained creation identity, independently qualified for native resume.

    The owner hashes the exact predecessor CID/GID/native root, tenant/shell/
    role, immutable boot/policy/creation provenance and all-generation cleanup
    into source_binding_digest. Equal policy on another root is a different
    subject. No raw subject IDs enter the reusable cache. This value records
    creation provenance; it does not assert effective native policy, settled
    definitions, source ownership or compatibility. Unknown retained fields
    cannot be replaced by an installed fingerprint or desired configuration.
    """
    harness: str
    executable_sha256: str
    executable_version: str
    driver_revision: str
    implementation_digest: str
    policy_digest: str
    provider: str
    model: str
    effort: str
    creation_provenance_digest: str
    source_binding_digest: str
    contract_revision: str = CONTRACT_REVISION

    def __post_init__(self) -> None:
        if not isinstance(self.harness,str) or self.harness not in {'codex','claude'}:
            raise ValueError('captured history provider required')
        for key in ('executable_sha256','implementation_digest','policy_digest',
                    'creation_provenance_digest','source_binding_digest'):
            value=getattr(self,key)
            if not isinstance(value,str) or len(value)!=64 or any(c not in '0123456789abcdef' for c in value):
                raise ValueError('exact retained history identity digests required')
        for key in ('executable_version','driver_revision','contract_revision','provider','model','effort'):
            value=getattr(self,key)
            if not isinstance(value,str) or not 1<=len(value)<=255 or any(c in value for c in '\x00\r\n'):
                raise ValueError('bounded retained history selection required')


@dataclass(frozen=True)
class WorkspaceIdentity:
    """Fresh canonical checkout observed by the owner; not old Git restoration.

    Providers must re-observe this identity after native resume, before the
    first current nonce/input. It does not certify vendor workspace behavior.
    """
    cwd: Path
    git_common_dir: Path
    branch: str
    head: str

    def __post_init__(self) -> None:
        if (any(not isinstance(path,Path) or not path.is_absolute() or '..' in path.parts for path in (self.cwd,self.git_common_dir))
                or not isinstance(self.branch,str) or not 1<=len(self.branch)<=255 or any(c in self.branch for c in '\x00\r\n')
                or not isinstance(self.head,str) or len(self.head) not in {40,64} or any(c not in '0123456789abcdef' for c in self.head)):
            raise RuntimeContractError('HISTORY_WORKSPACE_INVALID','bounded canonical Git workspace identity required')


@dataclass(frozen=True)
class NativeHistory:
    """Server-issued predecessor selection; never a client native operand.

    The owner positively validates retained source tenancy, immutable route,
    canonical worktree and all-generation composite cleanup before issuing
    this value and again before mutation/start. Digests bind that proof; they
    are not compatibility grades. Drivers capture their provider-specific
    historical transcript/item baseline before exposing any current event.
    Initial continuation reuses the exact native root (Claude direct resume,
    not fork) in a new engine conversation/generation and private journal.
    """
    source_conversation_id: str
    source_generation_id: str
    native_root_id: str
    harness: str
    model: str
    effort: str
    source_worktree: Path
    source_boot_digest: str
    source_policy_digest: str
    cleanup_digest: str

    def __post_init__(self) -> None:
        for key in ('source_conversation_id','source_generation_id','native_root_id','model','effort'):
            value=getattr(self,key)
            if not isinstance(value,str) or not 1<=len(value)<=255 or any(c in value for c in ('\x00','\n','\r')):
                raise RuntimeContractError('HISTORY_INVALID','bounded owned history identities required')
        if (not isinstance(self.harness,str) or self.harness not in {'codex','claude'} or not isinstance(self.source_worktree,Path)
                or not self.source_worktree.is_absolute() or '..' in self.source_worktree.parts):
            raise RuntimeContractError('HISTORY_INVALID','history requires captured provider and canonical worktree')
        for key in ('source_boot_digest','source_policy_digest','cleanup_digest'):
            value=getattr(self,key)
            if not isinstance(value,str) or len(value)!=64 or any(c not in '0123456789abcdef' for c in value):
                raise RuntimeContractError('HISTORY_INVALID','history requires exact source and cleanup digests')


@dataclass(frozen=True)
class RuntimeContext:
    generation_id: str
    conversation_id: str
    shell_id: int
    owner_user_id: int
    harness: str
    state_root: Path
    worktree: Path
    executable: ExecutableBinding
    driver_revision: str
    boot_digest: str
    policy_digest: str
    permission_mode: str
    provider: str | None = None
    model: str | None = None
    effort: str | None = None
    boot_content: str = field(default="", repr=False)
    execution_prefix: tuple[str, ...] = ()
    managed_mcp_files: tuple[Path, ...] = ()
    # Canonical managed MCP injection only, never general launch/policy args.
    # Codex uses paired '-c', 'mcp_servers.<name>.<field>=...' arguments.
    managed_mcp_args: tuple[str, ...] = ()
    # Owner attaches grades only after matching checker evidence fingerprint
    # (executable/driver/contract/policy/route) and required coverage. Full
    # evidence/provenance remains in the owner cache; empty means unverified.
    capability_evidence: Mapping[str, Grade] = field(default_factory=dict)
    # Only an owner-scoped finite compatibility probe may exercise these
    # operations before certification. GUI context always leaves this empty.
    probe_capabilities: tuple[str, ...] = ()
    # Ephemeral handoff only: never serialize these values in receipts/journal.
    env: Mapping[str, str] = field(default_factory=dict, repr=False)
    # Hooks/channel assets use the controller's already-owned private socket.
    # No independent public HTTP server or extra API owner is permitted.
    controller_endpoint: Path | None = None
    history: NativeHistory | None = None
    workspace: WorkspaceIdentity | None = None

    def __post_init__(self) -> None:
        if not self.permission_mode or not self.policy_digest or not self.boot_digest:
            raise RuntimeContractError("POLICY_MISSING", "explicit prepared boot and policy are required")
        if self.history is not None:
            history=self.history
            if (not isinstance(history,NativeHistory)
                    or history.source_conversation_id==self.conversation_id
                    or history.source_generation_id==self.generation_id
                    or (history.harness,history.model,history.effort,history.source_worktree)
                    !=(self.harness,self.model,self.effort,self.worktree)):
                raise RuntimeContractError('HISTORY_INVALID','history requires a distinct conversation/generation and unchanged owned route/worktree')
        if self.workspace is not None and (not isinstance(self.workspace,WorkspaceIdentity) or self.workspace.cwd!=self.worktree):
            raise RuntimeContractError('HISTORY_WORKSPACE_INVALID','prepared workspace differs from captured canonical cwd')
        # The harness driver must reject unsupported modes; never substitute a
        # probe-friendly policy for this canonical prepared value.
        if len(self.managed_mcp_args) % 2 or any(
            self.managed_mcp_args[index] != "-c" or
            not self.managed_mcp_args[index+1].startswith("mcp_servers.") or
            "=" not in self.managed_mcp_args[index+1]
            for index in range(0, len(self.managed_mcp_args), 2)
        ):
            raise RuntimeContractError("MCP_INVALID", "only canonical MCP config arguments are accepted")
        if any(grade not in {"compatible","incompatible","inconclusive","unverified"}
               for grade in self.capability_evidence.values()):
            raise RuntimeContractError("CAPABILITY_INVALID", "unknown capability evidence grade")
        if "conversation" in self.capability_evidence or "conversation" in self.probe_capabilities:
            raise RuntimeContractError("CAPABILITY_INVALID", "repeated input uses the shared submission capability key")

    def execution_argv(self, argv: list[str]) -> list[str]:
        return [*self.execution_prefix, *argv]


@dataclass(frozen=True)
class NativeReference:
    root_id: str
    thread_id: str | None = None
    parent_thread_id: str | None = None
    activity_id: str | None = None
    item_id: str | None = None
    work_id: str | None = None
    # Opaque app-server processId/task handle; never convert to an OS PID.
    native_process_id: str | None = None
    os_process: ProcessIdentity | None = None

    def __post_init__(self) -> None:
        for key in ("root_id","thread_id","parent_thread_id","activity_id","item_id","work_id","native_process_id"):
            value=getattr(self,key)
            if (key=="root_id" and value is None) or (value is not None and (not isinstance(value,str) or not 1<=len(value)<=255)):
                raise RuntimeContractError("REFERENCE_INVALID","bounded opaque native identities required")


@dataclass(frozen=True)
class RuntimeIdentity:
    root_id: str
    # Metadata only: ancestry/parent links, not session_id, authorize children.
    session_id: str | None = None
    process: ProcessIdentity | None = None
    protocol: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StartupConsent:
    """One captured Claude local-development choice, never a capability grade.

    Driver revalidates this exact observed phase/configuration at its write
    edge. setup_id identifies one finite phase epoch, not reusable permission.
    Confirmation is not readiness; attributable successful processing/terminal
    evidence must subsequently produce runtime.ready.
    """
    generation_id: str
    setup_id: str
    executable_sha256: str
    driver_revision: str
    configuration_sha256: str
    observed_at: float
    detail: str = ''
    phase: Literal['local_channel_development_consent'] = 'local_channel_development_consent'
    action: Literal['enable_local_channel'] = 'enable_local_channel'

    def __post_init__(self) -> None:
        if (self.phase!='local_channel_development_consent' or self.action!='enable_local_channel'
                or any(not isinstance(v,str) or not 1<=len(v)<=255
                       for v in (self.generation_id,self.setup_id,self.driver_revision))
                or any(not isinstance(v,str) or len(v)!=64 or any(c not in '0123456789abcdef' for c in v)
                       for v in (self.executable_sha256,self.configuration_sha256))
                or isinstance(self.observed_at,bool) or not isinstance(self.observed_at,(int,float))
                or not math.isfinite(self.observed_at) or self.observed_at<=0
                or not isinstance(self.detail,str) or len(self.detail)>4096):
            raise RuntimeContractError('SETUP_INVALID','captured finite startup consent descriptor required')


@dataclass(frozen=True)
class DriverStart:
    state: Literal["ready", "unavailable", "unknown", "needs_consent"]
    identity: RuntimeIdentity | None = None
    detail: str = ""
    capabilities: Mapping[str, Grade] = field(default_factory=dict)
    setup: StartupConsent | None = None

    def __post_init__(self) -> None:
        if (self.state not in {'ready','unavailable','unknown','needs_consent'}
                or (self.state=='needs_consent')!=(self.setup is not None)
                or (self.setup is not None and not isinstance(self.setup,StartupConsent))):
            raise RuntimeContractError('SETUP_INVALID','needs_consent requires its captured startup descriptor')


@dataclass(frozen=True)
class NativeSubmission:
    request_id: str
    # Stable per-generation ordinal allocated with durable engine intent.
    # The journal retains a rejection floor when resolved IDs are compacted.
    request_sequence: int
    payload_digest: str
    text: str
    message_id: str | None = None
    run_id: str | None = None
    source: ActivitySource = "gui"


@dataclass(frozen=True)
class NativeControl:
    control_id: str
    request_sequence: int
    payload_digest: str
    action: Literal["stop_reply", "stop_work", "stop_automation", "close", "enable_local_channel"]
    target: NativeReference | None = None
    expected_activity_id: str | None = None
    # No arbitrary child text/input. Driver-defined bounded fields only.
    options: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if (self.action=='close' and self.options and
                (set(self.options)!={'wake_quiet_epoch','wake_quiet_seconds'}
                 or any(type(v) is not int or v<0 for v in self.options.values())
                 or self.target is not None or self.expected_activity_id is not None)):
            raise RuntimeContractError('COMMAND_INVALID','conditional wake Close requires exact nonnegative quiet epoch/seconds')
        if self.action=='enable_local_channel':
            sid=self.options.get('setup_id')
            digest=self.options.get('configuration_sha256')
            if (set(self.options)!={'setup_id','configuration_sha256'}
                    or not isinstance(sid,str) or not 1<=len(sid)<=255
                    or not isinstance(digest,str) or len(digest)!=64
                    or any(c not in '0123456789abcdef' for c in digest)
                    or self.expected_activity_id is not None):
                raise RuntimeContractError('SETUP_INVALID','only exact finite startup choice binding is allowed')


@dataclass(frozen=True)
class WriteReceipt:
    state: WriteState
    acknowledged: bool = False
    native_activity_id: str | None = None
    detail: str = ""
    def __post_init__(self) -> None:
        if self.state not in {"not_written","written","unknown","unsupported","rejected"} or type(self.acknowledged) is not bool:
            raise RuntimeContractError("RECEIPT_INVALID","invalid bounded native write receipt")
    # A transport ack or native RPC result is not a processing/terminal/cleanup
    # promise. Those require attributable events or an explicit snapshot.


@dataclass(frozen=True)
class RuntimeEvent:
    kind: str
    reference: NativeReference | None = None
    request_id: str | None = None
    control_id: str | None = None
    source: ActivitySource = "system"
    provenance: str = ""
    observed_at: float = field(default_factory=time.time)
    freshness: Freshness = "current"
    partial: bool = False
    grade: Grade = "unverified"
    data: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in EVENT_KINDS:
            raise RuntimeContractError("EVENT_INVALID", "unknown driver event kind")
        if self.kind=='runtime.setup':
            try:
                StartupConsent(**self.data)
            except TypeError as exc:
                raise RuntimeContractError('SETUP_INVALID','typed finite startup descriptor required') from exc
        if self.source not in {"gui","native_completion","automation","reconciliation","system"} or self.freshness not in {"current","last_observed","stale","unknown"} or self.grade not in {"compatible","incompatible","inconclusive","unverified"}:
            raise RuntimeContractError("EVENT_INVALID","invalid event source/freshness/grade")
        if not isinstance(self.observed_at,(int,float)) or not math.isfinite(self.observed_at):
            raise RuntimeContractError("EVENT_INVALID","finite native observation time required")
        if self.kind.startswith(("activity.", "work.", "output.")) and self.reference is None:
            raise RuntimeContractError("EVENT_INVALID", "native activity/work/output needs an attributable reference")
        clean = public_payload(self.data)
        if len(json.dumps(clean, allow_nan=False).encode()) > MAX_EVENT_BYTES:
            raise RuntimeContractError("EVENT_TOO_LARGE", "driver event exceeds bounded payload size")
        object.__setattr__(self, "data", clean)


EventSink = Callable[[RuntimeEvent], None]


@dataclass(frozen=True)
class NativeWork:
    reference: NativeReference
    kind: Literal["terminal", "child", "automation", "task"]
    state: str
    provenance: str
    observed_at: float
    freshness: Freshness = "last_observed"
    grade: Grade = "unverified"
    # Scheduling must prove non-durable at pre-execution, not infer it here.
    durable: bool | None = None
    data: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class NativeSnapshot:
    identity: RuntimeIdentity | None
    primary: NativeReference | None
    work: tuple[NativeWork, ...] = ()
    observed_at: float = field(default_factory=time.time)
    freshness: Freshness = "unknown"
    partial: bool = True
    capabilities: Mapping[str, Grade] = field(default_factory=dict)
    # Empty/last Stop is not idle proof. Drivers must state the evidence.
    primary_state: Literal["idle", "active", "unknown"] = "unknown"
    provenance: str = ""


@dataclass(frozen=True)
class NativeCleanup:
    outcome: Literal["complete", "pending", "failed", "inconclusive"]
    unresolved_work: tuple[NativeReference, ...] = ()
    unresolved_definitions: tuple[str, ...] = ()
    detail: str = ""
    # OS-unit/PID/cgroup exit is independently verified by the owner. Unit
    # absence cannot discharge unresolved native definition obligations.


class RuntimeDriver(abc.ABC):
    """One generation, continuously drained; no DB ledger or API worker owned pipe.

    All deadlines are absolute time.monotonic() values. Emit does bounded local
    journal work only, never calls a GUI/API or waits for another native RPC.
    Submit/control futures must not stall the reader or Close. Drivers expose
    only operations proven on their installed route; unsupported stays explicit.
    Factory in each harness module: create_driver() -> RuntimeDriver.
    """
    harness: str
    revision: str

    @abc.abstractmethod
    def start(self, context: RuntimeContext, emit: EventSink, *, deadline: float) -> DriverStart:
        raise NotImplementedError

    def resume_history(self, context: RuntimeContext, emit: EventSink, *, deadline: float) -> DriverStart:
        """Explicit native history operation; old adapters never fresh-start it.

        Provider implementation must validate the captured predecessor root,
        establish a historical baseline before current event harvesting, and
        prepare fresh policy/hooks/MCP plus generation-bound setup. Readiness
        cannot be inferred from old messages/terminals or old consent. Native
        and OS cleanup obligations remain identical to an ordinary generation.
        """
        return DriverStart('unavailable',detail='NATIVE_HISTORY_UNAVAILABLE',
                           capabilities={CAP_HISTORY_RESUME:'inconclusive'})

    @abc.abstractmethod
    def submit(self, command: NativeSubmission, *, deadline: float) -> WriteReceipt:
        raise NotImplementedError

    @abc.abstractmethod
    def inventory(self, *, deadline: float) -> NativeSnapshot:
        raise NotImplementedError

    @abc.abstractmethod
    def control(self, command: NativeControl, *, deadline: float) -> WriteReceipt:
        raise NotImplementedError

    @abc.abstractmethod
    def cleanup(self, *, deadline: float) -> NativeCleanup:
        raise NotImplementedError

    def asset(self, payload: Mapping[str, Any], *, peer: ProcessIdentity,
              deadline: float) -> Mapping[str, Any]:
        """Private Claude hook/channel ingress and bounded notification pull.

        Controller verifies generation, SO_PEERCRED and peer membership in its
        owned unit before routing. Driver owns the asset operations/normalizer:
        e.g. {kind:'hook',event:{...}}, {kind:'channel.ready'},
        {kind:'channel.pull',after:0}, {kind:'channel.reply',...}.
        Raw asset payloads are not journaled; emit only scoped normalized data.
        A channel pull may wait to deadline, without holding dispatch/Close locks.
        No asset message may invent an engine user request or claim a model
        processed it merely because a write/optional reply tool was acknowledged.
        """
        raise RuntimeContractError("ASSET_UNAVAILABLE", "driver has no private asset ingress")
