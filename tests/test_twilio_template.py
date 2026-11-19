import json

import httpx

from server.notify import env
from server.notify import whatsapp as wa
from tools import twilio_template as tt


def test_set_env_changes_lines_in_place(tmp_path, monkeypatch):
    monkeypatch.setattr(env, "ROOT", tmp_path)
    (tmp_path / ".env").write_text(
        '# keys\nTWILIO_CONTENT_SID=HXold\nTWILIO_CONTENT_VARIABLES={"1":"12/1"}\nX=1\n'
    )
    env._dotenv.cache_clear()
    tt.set_env({"TWILIO_CONTENT_SID": "HXnew", "TWILIO_CONTENT_VARIABLES": "", "NEW": "y"})
    assert (tmp_path / ".env").read_text().splitlines() == [
        "# keys", "TWILIO_CONTENT_SID=HXnew", "TWILIO_CONTENT_VARIABLES=", "X=1", "NEW=y"]  # fmt: skip
    assert env.get("TWILIO_CONTENT_SID") == "HXnew" and not env.get("TWILIO_CONTENT_VARIABLES")
    env._dotenv.cache_clear()


def test_create_reuses_or_makes_the_template():
    made = []

    def handler(req):
        if req.method == "GET":
            return httpx.Response(200, json={"contents": [{"friendly_name": "other", "sid": "HX0"}]})
        made.append(json.loads(req.content))
        return httpx.Response(201, json={"sid": "HX1"})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    assert tt.create(http) == "HX1"
    assert made[0]["types"]["twilio/text"]["body"] == "Dhadkan alert: {{1}}" and "1" in made[0]["variables"]


def test_template_variable_has_no_new_lines():
    seen = []

    def handler(req):
        form = httpx.QueryParams(req.content.decode())
        seen.append(form)
        if "ContentSid" not in form:
            return httpx.Response(400, json={"code": 21654, "message": "ContentSid Required"})
        return httpx.Response(201, json={"sid": "SM1"})

    t = wa.TwilioWhatsApp("AC1", "tok", "whatsapp:+17372508034", content_sid="HX1",
                          client=httpx.Client(transport=httpx.MockTransport(handler)))  # fmt: skip
    t.send("+91 98765 43210", "Loom 1 bearing.\n\nReply 1 =    mechanic", [])
    assert json.loads(seen[1]["ContentVariables"]) == {"1": "Loom 1 bearing. · Reply 1 = mechanic"}
