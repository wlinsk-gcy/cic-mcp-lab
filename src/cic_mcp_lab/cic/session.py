"""Router session state (design §6.1): conversation history, third-party threads and the pending choice."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

ThreadStatus = Literal["running", "input_required", "completed", "failed"]
STATUS_LABELS = {"running": "处理中", "input_required": "等待补充", "completed": "已完成", "failed": "失败"}
RETRY_OPTIONS = ("重试", "重新推荐其他 Agent")


@dataclass
class Thread:
    """One conversation with a third-party agent; the LLM only ever sees its alias (T1, T2 …)."""

    ref: str
    agent_id: str
    display_name: str
    provider_id: str
    conversation_id: str | None = None  # issued by the provider with its first reply
    status: ThreadStatus = "running"
    last_reply: str = ""
    last_message: str = ""  # the last message sent to the agent; resent when the user retries
    last_images: tuple[str, ...] = ()
    submission_id: str | None = None
    task_id: str | None = None


@dataclass(frozen=True)
class Candidate:
    agent_id: str
    display_name: str
    provider_id: str
    score: float
    basis: str  # why it matched: the profile or one example question


@dataclass(frozen=True)
class PendingCandidates:
    """Recommended agents awaiting a choice; `message` goes verbatim to the chosen agent (design §6.2)."""

    message: str
    image_urls: tuple[str, ...]
    candidates: tuple[Candidate, ...]


@dataclass(frozen=True)
class PendingRetry:
    """After a failed turn: option 1 resends it to the same conversation, option 2 recommends another agent."""

    thread_ref: str
    message: str
    image_urls: tuple[str, ...]


Pending = PendingCandidates | PendingRetry


@dataclass(frozen=True)
class Turn:
    role: Literal["user", "router", "agent"]
    text: str
    thread_ref: str | None = None  # agent turns: the thread that replied
    label: str | None = None  # agent turns: e.g. "旅行规划 · T1"


@dataclass
class Session:
    history: list[Turn] = field(default_factory=list)
    threads: dict[str, Thread] = field(default_factory=dict)
    pending: Pending | None = None

    def open_thread(self, agent_id: str, display_name: str, provider_id: str) -> Thread:
        thread = Thread(f"T{len(self.threads) + 1}", agent_id, display_name, provider_id)
        self.threads[thread.ref] = thread
        return thread

    def thread_for_submission(self, submission_id: str) -> Thread | None:
        return next((t for t in self.threads.values() if t.submission_id == submission_id), None)
