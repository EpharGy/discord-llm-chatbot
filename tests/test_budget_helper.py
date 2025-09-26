from types import SimpleNamespace

from src.message_router import MessageRouter

class DummyTokenizer:
    def __init__(self):
        self.truncate_calls = []
    def estimate_tokens_messages(self, messages):
        # naive: token count = sum of len(content.split())
        total = 0
        for m in messages:
            c = m.get('content')
            if isinstance(c, list):
                # multimodal stub: count text parts only
                total += sum(len(p.get('text','').split()) for p in c if p.get('type')=='text')
            elif isinstance(c, str):
                total += len(c.split())
        return total
    def truncate_text_tokens(self, text, max_tokens):
        toks = text.split()
        if len(toks) <= max_tokens:
            return text
        self.truncate_calls.append((text, max_tokens))
        return ' '.join(toks[:max_tokens])

class DummyTemplate:
    def build_system_message_for(self, is_nsfw=False):
        return 'SYSTEM NSFW' if is_nsfw else 'SYSTEM NORMAL'
    def render(self, **kwargs):
        return 'CTX'

class DummyMemory:
    def __init__(self):
        self.records = []
    def get_recent(self, cid, limit=10):
        return []
    # below used by batch path but not by helper tests
    def consume_conversation_message(self, cid):
        return True

class DummyPolicy:
    def window_size(self):
        return 5
    def is_response_chance_override(self, cid):
        return False

class DummyLogger:
    def debug(self, *a, **k):
        pass
    def info(self, *a, **k):
        pass
    def error(self, *a, **k):
        pass

class DummyLLM:
    async def generate_chat(self, messages, **kwargs):  # pragma: no cover - trivial
        return {'text': 'ok', 'usage': {'input_tokens': 0, 'output_tokens': 1, 'total_tokens': 1}}


def _make_router():
    return MessageRouter(
        template_engine=DummyTemplate(),
        tokenizer=DummyTokenizer(),
        memory=DummyMemory(),
        policy=DummyPolicy(),
        logger=DummyLogger(),
        mentions_queue=None,
        batcher=None,
        llm=DummyLLM(),
        model_cfg={'models': ['test/model'], 'max_tokens': 50, 'temperature': 0.7, 'top_p': 1.0},
        lore=None,
        lore_config={'enabled': False}
    )


def test_assemble_and_budget_no_trimming():
    r = _make_router()
    system_blocks = [{'role': 'system', 'content': 'S'}]
    history = [{'role': 'user', 'content': 'hello world'}]
    user = {'role': 'user', 'content': 'final message'}
    messages, before, after = r._assemble_and_budget(system_blocks, history, user, prompt_budget=50)
    assert messages[-1]['content'] == 'final message'
    assert before == after


def test_assemble_and_budget_trims_history_first():
    r = _make_router()
    # history long so tokens exceed small budget
    history = [{'role': 'assistant', 'content': 'A ' * 10}, {'role': 'user', 'content': 'B ' * 10}, {'role': 'user', 'content': 'C ' * 10}]
    system_blocks = [{'role': 'system', 'content': 'S'}]
    user = {'role': 'user', 'content': 'tail'}
    messages, before, after = r._assemble_and_budget(system_blocks, history, user, prompt_budget=8)
    # Should have trimmed at least one history entry
    assert len(messages) <= 1 + len(history)  # basic sanity
    assert after <= 8


def test_assemble_and_budget_truncates_user_last():
    r = _make_router()
    history = []
    system_blocks = [{'role': 'system', 'content': 'S'}]
    # user content longer than budget triggers truncation
    user = {'role': 'user', 'content': 'one two three four five six seven eight nine ten'}
    messages, before, after = r._assemble_and_budget(system_blocks, history, user, prompt_budget=5)
    assert len(messages[-1]['content'].split()) <= 5
    assert r.tok.truncate_calls, 'truncate should have been invoked'


def test_assemble_and_budget_preserves_protected_assistant():
    r = _make_router()
    protected = 'KEEP'
    history = [
        {'role': 'assistant', 'content': protected},
        {'role': 'user', 'content': 'drop this many words ' * 5},
        {'role': 'user', 'content': 'more drop words ' * 5},
    ]
    system_blocks = [{'role': 'system', 'content': 'S'}]
    user = {'role': 'user', 'content': 'tail end'}
    messages, _, _ = r._assemble_and_budget(system_blocks, history, user, prompt_budget=6, protect_last_assistant=protected)
    # Ensure protected assistant still present
    assert any(m for m in messages if m.get('role') == 'assistant' and m.get('content') == protected)
