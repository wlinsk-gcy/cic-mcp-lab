"""Router (design §6.2): every input goes through intent recognition, then code decides what happens.

Agent outcomes arrive asynchronously via `on_outcome`. All output goes through `emit`, so the CLI and the
tests observe the same events.
"""
from __future__ import annotations

from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from .intent import IntentDecision
from .ledger import Ledger
from .recommend.recommender import Recommendation
from .session import (
    RETRY_OPTIONS,
    STATUS_LABELS,
    Candidate,
    Pending,
    PendingCandidates,
    PendingRetry,
    Session,
    Thread,
    Turn,
)
from .task_watcher import ConversationBusy, Submission, SubmitFailed, TaskOutcome

EventKind = Literal["intent", "router", "candidates", "submitted", "agent", "failure", "notice"]
DELIVERY_LABELS = {"direct": "直接返回", "push": "推送", "recheck": "订阅后补查", "poll": "轮询"}


@dataclass(frozen=True)
class RouterEvent:
    kind: EventKind
    text: str


class Recognizer(Protocol):
    async def recognize(self, session: Session, text: str, image_count: int = 0) -> IntentDecision: ...


class Responder(Protocol):
    async def answer(self, session: Session, instruction: str | None = None) -> str: ...


class AgentRecommender(Protocol):
    async def recommend(
        self, query: str, *, with_image: bool = False, include_mock: bool = False, exclude: Collection[str] = ()
    ) -> list[Recommendation]: ...


class AgentSubmitter(Protocol):
    async def submit(
        self, agent_id: str, message: str, image_urls: Sequence[str] = (), conversation_id: str | None = None
    ) -> Submission: ...


def describe(decision: IntentDecision) -> str:
    target = {
        "supplement": f" → {decision.thread_ref}",
        "select_candidate": f" → [{decision.candidate_index}]",
        "recommend": f" → 「{decision.search_query}」" if decision.search_query else "",
    }.get(decision.intent, "")
    return f"[intent] {decision.intent}{target}（{decision.reason}）"


def _option_count(pending: Pending) -> int:
    return len(pending.candidates) if isinstance(pending, PendingCandidates) else len(RETRY_OPTIONS)


class Router:
    def __init__(
        self,
        *,
        recognizer: Recognizer,
        responder: Responder,
        recommender: AgentRecommender,
        submitter: AgentSubmitter,
        ledger: Ledger,
        emit: Callable[[RouterEvent], None],
        include_mock: bool = False,
    ) -> None:
        self._recognizer = recognizer
        self._responder = responder
        self._recommender = recommender
        self._submitter = submitter
        self._ledger = ledger
        self._sink = emit
        self._include_mock = include_mock
        self.session = Session()

    def reset(self) -> None:
        """Start a new session (/new); tasks still running keep settling in the ledger."""
        self.session = Session()

    async def handle(self, text: str, image_urls: Sequence[str] = ()) -> None:
        images = tuple(image_urls)
        seen = self.session.pending  # the choice the user was looking at when they typed
        decision = await self._recognizer.recognize(self.session, text, len(images))
        self._emit("intent", describe(decision))
        suffix = f"（附带 {len(images)} 张图片）" if images else ""
        self.session.history.append(Turn("user", text + suffix))
        # Any input other than a choice drops the pending choice (design §6.2 rule 3) — unless a failure
        # posted a newer choice while the input was being recognised; that one stays for the next turn.
        if self.session.pending is seen:
            self.session.pending = None
        if decision.intent == "select_candidate":
            await self._select(seen, decision.candidate_index)
        elif decision.intent == "supplement":
            await self._supplement(decision.thread_ref, text, images)
        elif decision.intent == "recommend":
            await self._recommend(text, images, decision.search_query or text)
        elif decision.intent == "clarify":
            await self._answer(
                f"用户的意图不明确（{decision.reason}）。请用一两句话向用户澄清；涉及多个会话时列出 T1、T2… 让用户指明。"
            )
        else:
            await self._answer()

    def on_outcome(self, outcome: TaskOutcome) -> None:
        """A submitted turn has finished (design §6.8); the agent's reply is shown as-is (§6.5)."""
        thread = self.session.thread_for_submission(outcome.submission_id)
        if thread is None:  # the session was reset while the task ran; the ledger settled it regardless
            state = "已结算" if outcome.ok else "失败，不计费"
            self._emit("notice", f"[notice] {outcome.agent_id} 的一个任务已结束，但不属于当前会话（{state}）")
            return
        if not outcome.ok or outcome.reply is None:
            self._fail(thread, f"{outcome.failure}: {outcome.detail}")
            return
        reply = outcome.reply
        thread.conversation_id, thread.status, thread.last_reply = reply.conversation_id, reply.status, reply.reply
        via = DELIVERY_LABELS.get(outcome.delivery or "", "未知途径")
        header = (
            f"[{thread.display_name} · {thread.provider_id}] {thread.ref} · {STATUS_LABELS[thread.status]}"
            f"（经{via}，{outcome.elapsed_seconds:.1f}s）"
        )
        self._emit("agent", f"{header}\n{reply.reply}\n{self._credit_line(thread, reply.credit_usage)}")
        self.session.history.append(Turn("agent", reply.reply, thread.ref, f"{thread.display_name} · {thread.ref}"))

    # ---- branches -------------------------------------------------------------------------------

    async def _select(self, pending: Pending | None, index: int | None) -> None:
        if pending is None or index is None or not 1 <= index <= _option_count(pending):
            if self.session.pending is None:
                self.session.pending = pending  # keep the choice open (unless a newer one has appeared)
            await self._answer("用户像是在做选择，但当前没有待选项或编号超出范围。请提示用户按编号从当前待选项中选择，或直接说明新的需求。")
            return
        if isinstance(pending, PendingCandidates):
            chosen = pending.candidates[index - 1]
            thread = self.session.open_thread(chosen.agent_id, chosen.display_name, chosen.provider_id)
            await self._submit(thread, pending.message, pending.image_urls)
            return
        failed = self.session.threads[pending.thread_ref]
        if index == 1:
            await self._submit(failed, pending.message, pending.image_urls)
        else:
            await self._recommend(pending.message, pending.image_urls, pending.message, exclude=(failed.agent_id,))

    async def _supplement(self, ref: str | None, text: str, images: tuple[str, ...]) -> None:
        thread = self.session.threads.get(ref or "")
        if thread is None:
            await self._answer(f"用户像是在补充某个会话，但会话 {ref} 不存在。请向用户澄清是在补充哪个会话。")
            return
        if thread.status == "running":
            self._say(f"{thread.display_name}（{thread.ref}）正在处理上一条消息，请稍候。")
            return
        await self._submit(thread, text, images)

    async def _recommend(
        self, message: str, images: tuple[str, ...], query: str, exclude: Collection[str] = ()
    ) -> None:
        picks = await self._recommender.recommend(
            query, with_image=bool(images), include_mock=self._include_mock, exclude=exclude
        )
        if not picks:
            self._say("暂无合适的 Agent，我先尽力回答。")
            await self._answer()
            return
        candidates = tuple(
            Candidate(
                p.agent_id,
                p.display_name,
                p.provider_id,
                p.score,
                "画像" if p.matched_kind == "profile" else f"示例问题「{p.matched_text}」",
            )
            for p in picks
        )
        self.session.pending = PendingCandidates(message, images, candidates)
        lines = ["为你找到以下 Agent（按匹配度排序）："]
        lines += [
            f"  [{i}] {c.display_name} · {c.provider_id}   匹配度 {c.score:.2f}   依据：{c.basis}"
            for i, c in enumerate(candidates, 1)
        ]
        lines.append("回复编号或 Agent 名称进行选择，或直接输入其他内容")
        text = "\n".join(lines)
        self._emit("candidates", text)
        self.session.history.append(Turn("router", text))

    async def _answer(self, instruction: str | None = None) -> None:
        self._say(await self._responder.answer(self.session, instruction))

    # ---- submitting and failures --------------------------------------------------------------------

    async def _submit(self, thread: Thread, message: str, images: tuple[str, ...]) -> None:
        thread.status = "running"
        thread.last_message, thread.last_images = message, images
        try:
            submission = await self._submitter.submit(thread.agent_id, message, images, thread.conversation_id)
        except ConversationBusy:
            self._say(f"{thread.display_name}（{thread.ref}）正在处理上一条消息，请稍候。")
            return
        except SubmitFailed as exc:
            self._fail(thread, str(exc))
            return
        thread.submission_id, thread.task_id = submission.submission_id, submission.task_id
        task = submission.task_id or "无，Provider 直接返回"
        self._emit("submitted", f"[{thread.ref}] 已提交给 {thread.display_name}（taskId={task}），处理中…")
        for warning in submission.warnings:
            self._emit("notice", f"[warn] {warning}")

    def _fail(self, thread: Thread, detail: str) -> None:
        thread.status = "failed"
        self.session.pending = PendingRetry(thread.ref, thread.last_message, thread.last_images)
        options = "  ".join(f"[{i}] {option}" for i, option in enumerate(RETRY_OPTIONS, 1))
        text = f"{thread.display_name}（{thread.ref}）暂时不可用：{detail}（本次不计费）。\n你可以选择：{options}"
        self._emit("failure", text)
        self.session.history.append(Turn("router", text))

    def _credit_line(self, thread: Thread, credit: float) -> str:
        entries = [
            entry
            for entry in self._ledger.entries
            if entry.agent_id == thread.agent_id and entry.conversation_id == thread.conversation_id
        ]
        total = sum(entry.credit_usage for entry in entries)
        hkd = f" ≈ HKD {entries[-1].hkd}" if entries else ""
        return f"[credit] {thread.agent_id} credit_usage={credit}{hkd}（本会话累计 {total} Credit）"

    def _say(self, text: str) -> None:
        self._emit("router", text)
        self.session.history.append(Turn("router", text))

    def _emit(self, kind: EventKind, text: str) -> None:
        self._sink(RouterEvent(kind, text))
