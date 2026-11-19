"""A2A (Agent2Agent) interop: Dhadkan's own agent endpoint, a client for partner agents, and DEMO partners.

server/a2a/protocol.py     JSON-RPC 2.0 `message/send`, Message and Task shapes, agent cards
server/a2a/client.py       call an allow-listed partner agent; every answer comes back UNTRUSTED
server/a2a/router.py       Dhadkan's agent card and endpoint (read-only skills, no actions)
server/a2a/demo_agents.py  two DEMO partner agents (a mechanic's workshop, a parts dealer) on :8701
"""
