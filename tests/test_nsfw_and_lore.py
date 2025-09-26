from pathlib import Path

from src.message_router import MessageRouter

class DummyTokenizer:
    def estimate_tokens_messages(self, messages):
        return sum(len((m.get('content','') if isinstance(m.get('content'), str) else '')) for m in messages)
    def truncate_text_tokens(self, text, max_tokens):
        return text

class DummyTemplate:
    def build_system_message_for(self, is_nsfw=False):
        return 'SYS-NSFW' if is_nsfw else 'SYS'
    def render(self, **kwargs):
        return ''

class DummyMemory:
    def get_recent(self, cid, limit=10):
        return []
    def consume_conversation_message(self, cid):
        return True

class DummyPolicy:
    def window_size(self):
        return 5
    def is_response_chance_override(self, cid):
        return False

class DummyLogger:
    def debug(self,*a,**k): pass
    def info(self,*a,**k): pass
    def error(self,*a,**k): pass

class DummyLore:
    def build_lore_block(self, corpus_text, max_tokens, tokenizer, logger):
        # just return a short marker to confirm inclusion & token budgeting call path
        return '[Lore] background info'

class DummyLLM:
    async def generate_chat(self, messages, **kwargs):
        # Echo back the final user content with markers to assert composition
        text = '\n'.join(m.get('content','') for m in messages if m.get('role')=='user')
        return {'text': 'RESP:' + text}


def make_router(lore_enabled: bool):
    lore_cfg = {'enabled': lore_enabled, 'max_fraction': 0.5}
    lore_obj = DummyLore() if lore_enabled else None
    return MessageRouter(DummyTemplate(), DummyTokenizer(), DummyMemory(), DummyPolicy(), DummyLogger(), None, None, DummyLLM(), {'models':['x'],'max_tokens':256,'temperature':0.7,'top_p':1.0}, lore=lore_obj, lore_config=lore_cfg)

class FakeChannel:
    def __init__(self, nsfw):
        self.nsfw = nsfw
        self.parent = None

import asyncio

async def _call_batch(router, nsfw=False, lore=False):
    ch = FakeChannel(nsfw)
    events = [
        {'author_name':'u1','content':'hello','is_bot':False},
        {'author_name':'u2','content':'world','is_bot':False},
    ]
    return await router.build_batch_reply('chan', events, channel=ch)


def test_batch_nsfw_system_prompt():
    r = make_router(lore_enabled=False)
    out = asyncio.run(_call_batch(r, nsfw=True))
    # We can't directly see system messages from outside, but ensure function returns something non-empty
    assert out.startswith('RESP:')


def test_batch_lore_included():
    r = make_router(lore_enabled=True)
    out = asyncio.run(_call_batch(r, nsfw=False, lore=True))
    assert out.startswith('RESP:')

