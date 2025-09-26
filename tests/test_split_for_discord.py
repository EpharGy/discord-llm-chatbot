from src.message_router import MessageRouter

class Dummy:
    def __init__(self):
        pass

class DummyTokenizer:
    def estimate_tokens_messages(self, messages):
        return sum(len(m.get('content','').split()) for m in messages)
    def truncate_text_tokens(self, text, max_tokens):
        toks = text.split()
        return ' '.join(toks[:max_tokens])

class DummyTemplate:
    def build_system_message_for(self, is_nsfw=False):
        return 'SYS'
    def render(self, **kwargs):
        return ''

class DummyMemory:
    def get_recent(self, cid, limit=10):
        return []

class DummyPolicy:
    def window_size(self):
        return 5
    def is_response_chance_override(self, cid):
        return False

class DummyLogger:
    def debug(self,*a,**k):
        pass
    def info(self,*a,**k):
        pass
    def error(self,*a,**k):
        pass

class DummyLLM:
    async def generate_chat(self,*a,**k):
        return {'text':'ok'}


def make_router():
    return MessageRouter(DummyTemplate(), DummyTokenizer(), DummyMemory(), DummyPolicy(), DummyLogger(), None, None, DummyLLM(), {'models':['x'],'max_tokens':128,'temperature':0.7,'top_p':1.0}, lore=None, lore_config={'enabled':False})


def test_split_for_discord_single_chunk():
    r = make_router()
    long = 'a' * 100
    parts = r._split_for_discord(long)
    assert len(parts) == 1


def test_split_for_discord_multi_chunk(monkeypatch):
    r = make_router()
    # Force small discord char limit
    class FakeCfg:
        def __init__(self):
            pass
        def discord_message_char_limit(self):
            return 50
        def max_response_messages(self):
            return 3
    monkeypatch.setenv('PYTHONPATH','src')
    # monkeypatch config_service.ConfigService to return FakeCfg-like attributes via dynamic class
    import src.config_service as cs
    original = cs.ConfigService
    class StubCfg(cs.ConfigService):
        def __init__(self,_):
            pass
        def discord_message_char_limit(self): return 50
        def max_response_messages(self): return 3
    cs.ConfigService = StubCfg
    try:
        text = ' '.join(['word']*200)
        parts = r._split_for_discord(text)
        # Should not exceed 3 according to stubbed config
        assert 1 <= len(parts) <= 3
    finally:
        cs.ConfigService = original
