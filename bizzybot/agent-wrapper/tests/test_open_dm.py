"""Run with: uv run --with pytest -m pytest tests

post_message, read_messages and add_reaction take a person as well as a
channel: a user id or @handle opens (or reuses) the bot's DM with them.
"""

import asyncio

from bizzybot_agent_wrapper.slack_tools import _conversation_id


class FakeSlack:
    def __init__(self):
        self.opened = []

    async def conversations_open(self, users):
        self.opened.append(users)
        return {"channel": {"id": "D" + users[1:]}}

    async def conversations_list(self, **_):
        return {"channels": [{"id": "C0ENG00001", "name": "eng"}], "response_metadata": {}}

    async def users_list(self, **_):
        return {"members": [{"id": "U0FREDDY01", "name": "freddy"}], "response_metadata": {}}


def resolve(ref):
    slack = FakeSlack()
    return asyncio.run(_conversation_id(slack, ref)), slack.opened


def test_user_id_opens_a_dm():
    assert resolve("U0FREDDY01") == (("D0FREDDY01", ""), ["U0FREDDY01"])
    assert resolve("<@U0FREDDY01>") == (("D0FREDDY01", ""), ["U0FREDDY01"])


def test_handle_opens_a_dm():
    assert resolve("@freddy") == (("D0FREDDY01", ""), ["U0FREDDY01"])
    assert resolve("Freddy") == (("D0FREDDY01", ""), ["U0FREDDY01"])


def test_channels_and_dm_ids_are_unchanged():
    assert resolve("#eng") == (("C0ENG00001", ""), [])
    assert resolve("eng") == (("C0ENG00001", ""), [])
    assert resolve("D0EXISTING1") == (("D0EXISTING1", ""), [])


def test_unknown_ref_is_an_error():
    (cid, problem), opened = resolve("@nobody")
    assert cid is None and "nobody" in problem and opened == []
    (cid, problem), _ = resolve("#freddy")  # a #name is only ever a channel
    assert cid is None and "#freddy" in problem
