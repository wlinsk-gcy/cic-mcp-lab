from cic_mcp_lab.cic.contract import AgentReply
from cic_mcp_lab.cic.intent import IntentDecision
from cic_mcp_lab.cic.ledger import Ledger, LedgerEntry
from cic_mcp_lab.cic.recommend.recommender import Recommendation
from cic_mcp_lab.cic.router import Router
from cic_mcp_lab.cic.session import PendingCandidates, PendingRetry
from cic_mcp_lab.cic.task_watcher import Submission, SubmitFailed, TaskOutcome


def decide(intent, *, thread_ref=None, candidate_index=None, search_query=None, reason="r"):
    return IntentDecision(
        intent=intent, thread_ref=thread_ref, candidate_index=candidate_index, search_query=search_query, reason=reason
    )


class ScriptedRecognizer:
    def __init__(self):
        self.next = []

    async def recognize(self, session, text, image_count=0):
        return self.next.pop(0)


class EchoResponder:
    def __init__(self):
        self.instructions = []

    async def answer(self, session, instruction=None):
        self.instructions.append(instruction)
        return "答：" + next(turn.text for turn in reversed(session.history) if turn.role == "user")


class FakeRecommender:
    def __init__(self, *picks):
        self.picks, self.calls = list(picks), []

    async def recommend(self, query, *, with_image=False, include_mock=False, exclude=()):
        self.calls.append((query, with_image, tuple(exclude)))
        return [pick for pick in self.picks if pick.agent_id not in exclude]


class FakeSubmitter:
    def __init__(self):
        self.calls, self.error = [], None

    async def submit(self, agent_id, message, image_urls=(), conversation_id=None):
        if self.error:
            raise self.error
        self.calls.append((agent_id, message, tuple(image_urls), conversation_id))
        return Submission(f"sub_{len(self.calls)}", agent_id, f"task_{len(self.calls)}")


def pick(agent_id, name):
    return Recommendation(agent_id, name, "provider-a", True, 0.8, "example", f"{name}的示例问题")


TRIP, COPY = pick("trip_planner", "旅行规划"), pick("product_copy", "商品文案")


class Harness:
    def __init__(self, *picks):
        self.recognizer, self.responder = ScriptedRecognizer(), EchoResponder()
        self.recommender, self.submitter = FakeRecommender(*picks), FakeSubmitter()
        self.ledger, self.events = Ledger(), []
        self.router = Router(
            recognizer=self.recognizer,
            responder=self.responder,
            recommender=self.recommender,
            submitter=self.submitter,
            ledger=self.ledger,
            emit=self.events.append,
        )

    async def say(self, text, decision, images=()):
        self.recognizer.next.append(decision)
        await self.router.handle(text, images)

    def texts(self, kind):
        return [event.text for event in self.events if event.kind == kind]

    def finish(self, submission_id, agent_id, conversation_id, reply="请问出发日期和预算？", credit=42):
        # TaskWatcher settles before it hands the outcome over.
        self.ledger.settle(LedgerEntry(f"t_{submission_id}", "provider-a", agent_id, conversation_id, credit, 0.0001))
        answer = AgentReply(conversation_id, "input_required", reply, credit)
        self.router.on_outcome(TaskOutcome(submission_id, agent_id, "t", "push", answer, None, "", (), 1.5))

    def fail(self, submission_id, agent_id):
        outcome = TaskOutcome(submission_id, agent_id, "t", None, None, "task_failed", "MCP error -32603: boom", (), 1.0)
        self.router.on_outcome(outcome)


async def test_greetings_are_answered_by_the_router_alone():
    h = Harness(TRIP)
    await h.say("你好", decide("router_answer"))

    assert h.texts("router") == ["答：你好"]
    assert (h.recommender.calls, h.submitter.calls) == ([], [])
    assert [turn.role for turn in h.router.session.history] == ["user", "router"]


async def test_chosen_agent_gets_the_original_message_and_images_in_a_new_thread():
    h = Harness(COPY, TRIP)
    await h.say("帮这个商品写文案", decide("recommend", search_query="为商品写营销文案"), images=["https://img/1.png"])
    await h.say("用第一个", decide("select_candidate", candidate_index=1))

    assert h.recommender.calls == [("帮这个商品写文案", True, ())]  # v1 searches with the user's own words (decision 35)
    assert "[1] 商品文案" in h.texts("candidates")[0]
    assert h.submitter.calls == [("product_copy", "帮这个商品写文案", ("https://img/1.png",), None)]
    thread = h.router.session.threads["T1"]
    assert (thread.agent_id, thread.status, thread.submission_id) == ("product_copy", "running", "sub_1")
    assert h.router.session.pending is None


async def test_agent_reply_is_shown_as_is_and_the_next_supplement_reuses_its_conversation():
    h = Harness(TRIP)
    await h.say("帮我规划东京旅行", decide("recommend"))
    await h.say("1", decide("select_candidate", candidate_index=1))
    h.finish("sub_1", "trip_planner", "conv_1")
    await h.say("12月5号出发，预算1万", decide("supplement", thread_ref="T1"))

    agent_text = h.texts("agent")[0]
    assert "[旅行规划 · provider-a] T1 · 等待补充" in agent_text
    assert "请问出发日期和预算？" in agent_text and "credit_usage=42 ≈ HKD 0.0042" in agent_text
    assert ("agent", "请问出发日期和预算？", "T1") in [(t.role, t.text, t.thread_ref) for t in h.router.session.history]
    assert h.submitter.calls[-1] == ("trip_planner", "12月5号出发，预算1万", (), "conv_1")
    assert h.router.session.threads["T1"].status == "running"


async def test_supplementing_a_running_thread_waits_instead_of_submitting():
    h = Harness(TRIP)
    await h.say("帮我规划东京旅行", decide("recommend"))
    await h.say("1", decide("select_candidate", candidate_index=1))
    await h.say("预算改成2万", decide("supplement", thread_ref="T1"))

    assert len(h.submitter.calls) == 1
    assert "旅行规划（T1）正在处理上一条消息" in h.texts("router")[-1]


async def test_other_input_drops_the_pending_choice_and_invalid_choices_keep_it():
    h = Harness(TRIP, COPY)
    await h.say("帮我规划东京旅行", decide("recommend"))
    await h.say("第五个", decide("select_candidate", candidate_index=5))

    assert isinstance(h.router.session.pending, PendingCandidates)
    assert h.responder.instructions[-1] is not None
    await h.say("算了，讲个笑话", decide("router_answer"))
    assert h.router.session.pending is None
    assert h.submitter.calls == []


async def test_no_candidate_above_the_threshold_falls_back_to_a_router_answer():
    h = Harness()
    await h.say("帮我订明天去上海的机票", decide("recommend"))

    assert h.texts("router") == ["暂无合适的 Agent，我先尽力回答。", "答：帮我订明天去上海的机票"]
    assert h.router.session.pending is None


async def test_failed_turn_offers_a_retry_on_the_same_conversation():
    h = Harness(TRIP)
    await h.say("帮我规划东京旅行", decide("recommend"))
    await h.say("1", decide("select_candidate", candidate_index=1))
    h.finish("sub_1", "trip_planner", "conv_1")
    await h.say("预算1万", decide("supplement", thread_ref="T1"))
    h.fail("sub_2", "trip_planner")

    failure = h.texts("failure")[0]
    assert h.router.session.threads["T1"].status == "failed"
    assert h.router.session.pending == PendingRetry("T1", "预算1万", ())
    assert "不计费" in failure and "[1] 重试" in failure and "[2] 重新推荐其他 Agent" in failure
    await h.say("重试", decide("select_candidate", candidate_index=1))
    assert h.submitter.calls[-1] == ("trip_planner", "预算1万", (), "conv_1")


async def test_after_a_submit_failure_another_agent_is_recommended_without_the_failed_one():
    h = Harness(TRIP, COPY)
    await h.say("帮我规划东京旅行", decide("recommend"))
    h.submitter.error = SubmitFailed("trip_planner: provider down")
    await h.say("1", decide("select_candidate", candidate_index=1))
    h.submitter.error = None
    await h.say("换一个", decide("select_candidate", candidate_index=2))

    assert "provider down" in h.texts("failure")[0]
    assert h.recommender.calls[-1] == ("帮我规划东京旅行", False, ("trip_planner",))
    pending = h.router.session.pending
    assert isinstance(pending, PendingCandidates)
    assert (pending.message, [c.agent_id for c in pending.candidates]) == ("帮我规划东京旅行", ["product_copy"])


async def test_clarify_and_unknown_threads_ask_the_user():
    h = Harness()
    await h.say("好的，就这样", decide("clarify", reason="T1 和 T2 都在等待补充"))
    await h.say("补充一下", decide("supplement", thread_ref="T9"))

    assert "T1 和 T2 都在等待补充" in h.responder.instructions[0]
    assert "T9" in h.responder.instructions[1]
    assert h.submitter.calls == []


def test_outcomes_after_a_reset_are_reported_but_not_attached():
    h = Harness()
    h.router.reset()
    h.fail("sub_9", "trip_planner")

    assert "不属于当前会话" in h.texts("notice")[0]
    assert h.router.session.pending is None


async def test_a_failure_arriving_during_recognition_neither_hijacks_nor_drops_choices():
    h = Harness(TRIP, COPY)
    await h.say("帮我规划东京旅行", decide("recommend"))
    await h.say("1", decide("select_candidate", candidate_index=1))  # T1 running as sub_1
    await h.say("另外帮这个商品写文案", decide("recommend"))  # a new candidate list is shown
    read_input = h.recognizer.recognize

    async def failure_meanwhile(session, text, image_count=0):
        h.fail("sub_1", "trip_planner")  # T1 fails while the LLM is still reading "第二个"
        return await read_input(session, text, image_count)

    h.recognizer.recognize = failure_meanwhile
    await h.say("第二个", decide("select_candidate", candidate_index=2))

    assert h.submitter.calls[-1] == ("product_copy", "另外帮这个商品写文案", (), None)
    assert h.router.session.pending == PendingRetry("T1", "帮我规划东京旅行", ())
