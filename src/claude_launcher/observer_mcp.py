"""Agent-to-user Observer tools exposed by the shared claunch MCP server."""
from __future__ import annotations
import base64
import os
from pathlib import Path
from urllib.parse import quote
from . import daemon_client, mcp_rpc

COMMON = {"text":{"type":"string","description":"Concrete user-facing report or question; include outcome and evidence."},
          "images":{"type":"array","items":{"type":"string"},"maxItems":4,"description":"Absolute local paths of PNG/JPEG/WebP screenshots already captured by your tools, <=5 MiB each. Files are copied into durable Observer storage."},
          "request_id":{"type":"string","description":"Stable unique identifier. Reuse on retry to avoid duplicate reports."}}
TOOLS = [
    {"name":"observer_report","description":"Proactively report a meaningful result, blocker or screenshot to the user in Observer. Use for milestones, test/deployment results and visual evidence; omit routine tool chatter. Available even when automatic LLM observation is disabled. Returns a durable report ID.",
     "inputSchema":{"type":"object","properties":{**COMMON,"state":{"type":"string","enum":["working","waiting","blocked","done","unknown"]}},"required":["text","request_id"]}},
    {"name":"observer_ask","description":"Ask the user a concrete question in Observer, optionally with screenshots and suggested choices. The user can also enter free text. Returns immediately with a request ID; do independent work while waiting. Read answers with observer_requests. This does not create or approve a cflow gate.",
     "inputSchema":{"type":"object","properties":{**COMMON,"choices":{"type":"array","items":{"type":"string"},"maxItems":10}},"required":["text","request_id"]}},
    {"name":"observer_requests","description":"Read your own Observer reports and durable user answers. Answers also attempt delivery to your session. Check a known request_id after being notified, before continuing dependent work; missing answers never imply approval.",
     "inputSchema":{"type":"object","properties":{"request_id":{"type":"string"}}}},
]

class ObserverMcpError(Exception):
    pass


def call_tool(name, args):
    session=os.environ.get("CLAUNCH_SESSION")
    if not session:
        raise ObserverMcpError("Observer tools require a managed CLAUNCH_SESSION")
    client,why=daemon_client.connect_with_diagnosis()
    if client is None:
        raise ObserverMcpError(daemon_client.unreachable_reason(why))
    base="/api/observer/"+quote(session,safe="")
    if name=="observer_requests":
        result=client.get(base+"/reports")
        key=args.get("request_id")
        if key:
            result["reports"]=[e for e in result["reports"] if e.get("request_id")==key or e["id"]==key]
        return result
    if name not in ("observer_report","observer_ask"):
        raise ObserverMcpError("unknown Observer tool")
    if not isinstance(args.get("request_id"),str) or not args["request_id"].strip():
        raise ObserverMcpError("a stable request_id is required")
    if not isinstance(args.get("text"),str) or not args["text"].strip():
        raise ObserverMcpError("text is required")
    images=args.get("images",[])
    if not isinstance(images,list) or len(images)>4:
        raise ObserverMcpError("at most four image paths")
    attachments=[]
    for image in images:
        if not isinstance(image,str) or not Path(image).is_absolute():
            raise ObserverMcpError("image path must be absolute")
        path=Path(image)
        if path.stat().st_size>5*1024*1024:
            raise ObserverMcpError("image exceeds 5 MiB")
        raw=path.read_bytes()
        if len(raw)>5*1024*1024:
            raise ObserverMcpError("image exceeds 5 MiB")
        attachments.append(client.post(base+"/images",{"data":base64.b64encode(raw).decode()})["id"])
    return client.post(base+"/reports",{"text":args.get("text"),"request_id":args.get("request_id"),
        "question":name=="observer_ask","choices":args.get("choices",[]),"attachments":attachments,
        "state":"waiting" if name=="observer_ask" else args.get("state","working")})

SERVER=mcp_rpc.Server(name="claunch-observer",tools=tuple(TOOLS),dispatch=call_tool,
    errors=(ObserverMcpError,daemon_client.DaemonClientError,OSError,ValueError))
