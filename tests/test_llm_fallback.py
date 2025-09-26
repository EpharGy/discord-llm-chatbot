import asyncio

class FailingLLM:
    def __init__(self):
        self.calls = []
    async def generate_chat(self, messages, model, **kwargs):
        self.calls.append(model)
        if model == 'primary/model':
            raise RuntimeError('boom')
        return {'text': 'ok', 'usage': {'input_tokens': 10, 'output_tokens': 5, 'total_tokens': 15}}

class DummyTokenizer:
    def estimate_tokens_messages(self, messages):
        return 10
    def truncate_text_tokens(self, text, max_tokens):
        return text

class DummyTemplate:
    def build_system_message_for(self, is_nsfw=False):
        return 'SYS'
    def render(self, **kwargs):
        return ''

class DummyMemory:
    def get_recent(self, cid, limit=10): return []
    def consume_conversation_message(self, cid): return True

class DummyPolicy:
    def window_size(self): return 5
    def is_response_chance_override(self, cid): return False

class DummyLogger:
    def debug(self,*a,**k): pass
    def info(self,*a,**k): pass
    def error(self,*a,**k): pass

from src.message_router import MessageRouter

async def _run():
    llm = FailingLLM()
    router = MessageRouter(DummyTemplate(), DummyTokenizer(), DummyMemory(), DummyPolicy(), DummyLogger(), None, None,
                           llm, {'models':['primary/model','secondary/model'], 'allow_auto_fallback': False, 'max_tokens':128, 'temperature':0.7,'top_p':1.0},
                           lore=None, lore_config={'enabled': False})
    events = [{'author_name':'u','content':'hi','is_bot':False}]
    out = await router.build_batch_reply('c1', events)
    return out, llm.calls


def test_llm_fallback_sequence():
    out, calls = asyncio.run(_run())
    assert out == 'ok'
    # primary failed, secondary succeeded
    assert calls == ['primary/model','secondary/model']
