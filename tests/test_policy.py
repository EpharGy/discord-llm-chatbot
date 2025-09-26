import types
from datetime import datetime, timedelta, timezone

from src.participation_policy import ParticipationPolicy

class DummyMemory:
    def __init__(self):
        self._responses = []  # list[(channel_id, timestamp)]
        self._messages_since_last = {}
    def responses_in_window(self, cid, window_seconds):
        now = datetime.now(timezone.utc)
        self._responses = [(c,t) for (c,t) in self._responses if (now - t).total_seconds() <= window_seconds]
        return sum(1 for c,_ in self._responses if c == cid)
    def last_reply_info(self, cid):
        for c,t in reversed(self._responses):
            if c == cid:
                return t
        return None
    def messages_since_last_reply(self, cid):
        return self._messages_since_last.get(cid, 0)
    def simulate_reply(self, cid):
        self._responses.append((cid, datetime.now(timezone.utc)))
        self._messages_since_last[cid] = 0
    def add_message(self, cid):
        self._messages_since_last[cid] = self._messages_since_last.get(cid, 0) + 1


def make_policy(overrides: dict | None = None):
    base = {
        'rate_limits': {
            'min_messages_between_replies': 3,
            'min_seconds_between_replies': 30,
            'window_seconds': 120,
            'max_responses': 5,
            'warning_ttl_seconds': 5
        },
        'participation': {
            'mention_required': False,
            'respond_to_name': True,
            'name_aliases': ['bot'],
            'random_response_chance': 1.0,  # deterministic allow unless anti-spam/cooldown
            'general_chat': {
                'allowed_channels': ['c1','c2'],
                'response_chance_override': ['c2']
            },
            'cooldown': {
                'min_messages_between_replies': 3,
                'min_seconds_between_replies': 30
            }
        }
    }
    if overrides:
        # shallow merge convenience
        for k,v in overrides.items():
            if k in base:
                base[k].update(v) if isinstance(base[k], dict) else None
        if 'participation' in overrides:
            base['participation'].update(overrides['participation'])
    return ParticipationPolicy(base['rate_limits'], base['participation'])


def make_event(channel='c1', content='hello world', mentioned=False, reply_to_bot=False, author='user1'):
    return {
        'channel_id': channel,
        'channel_name': channel,
        'content': content,
        'author_name': author,
        'author_id': author,
        'is_mentioned': mentioned,
        'is_reply_to_bot': reply_to_bot,
        'message_id': 'm1'
    }


def test_policy_allows_general_when_chance_1():
    p = make_policy()
    mem = DummyMemory()
    ev = make_event()
    d = p.should_reply(ev, mem)
    assert d['allow'] is True
    assert d['reason'] in ('general','general-override')


def test_policy_blocks_non_allowed_channel():
    p = make_policy()
    mem = DummyMemory()
    ev = make_event(channel='cX')
    d = p.should_reply(ev, mem)
    assert d['allow'] is False
    assert d['reason'] == 'general-not-allowed-channel'


def test_policy_mentions_force_reply_style_reply():
    p = make_policy({'participation': {'mention_required': True}})
    mem = DummyMemory()
    ev = make_event(mentioned=True)
    d = p.should_reply(ev, mem)
    assert d['allow'] is True and d['style'] == 'reply'


def test_policy_cooldown_enforced():
    p = make_policy()
    mem = DummyMemory()
    ev = make_event()
    # Simulate a recent reply
    mem.simulate_reply('c1')
    # Not enough messages or seconds passed
    d = p.should_reply(ev, mem)
    assert d['allow'] is False and d['reason'] == 'cooldown'


def test_policy_override_bypasses_cooldown():
    p = make_policy()
    mem = DummyMemory()
    ev = make_event(channel='c2')
    mem.simulate_reply('c2')
    # Should bypass cooldown because c2 is override channel
    d = p.should_reply(ev, mem)
    assert d['allow'] is True and d['reason'] == 'general-override'


def test_policy_anti_spam_window():
    p = make_policy({'rate_limits': {'max_responses': 2}})
    mem = DummyMemory()
    ev = make_event()
    mem.simulate_reply('c1')
    mem.simulate_reply('c1')
    mem.simulate_reply('c1')  # third within window exceeds limit 2
    d = p.should_reply(ev, mem)
    assert d['allow'] is False and d['reason'] == 'anti-spam'
