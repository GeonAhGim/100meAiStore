"""Local-only dashboard server; refreshes use zero AI/model calls."""

from __future__ import annotations

import argparse
import json
import re
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from packages.store_core import SQLiteRepository, StoreControlPlane
from packages.store_core.errors import (AuthorizationError, ConflictError,
                                        NotFoundError, TenantBoundaryError)


INDEX_HTML = r"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>100meAiStore 운영 상태</title>
<style>
:root{color-scheme:light;--bg:#f5f7fb;--card:#fff;--ink:#142033;--muted:#617087;--line:#dce3ee;--accent:#1769e0;--warn:#a86100;--bad:#ba2636}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px system-ui,-apple-system,"Segoe UI",sans-serif}main{max-width:1080px;margin:auto;padding:16px}.top{display:flex;gap:12px;justify-content:space-between;align-items:center;flex-wrap:wrap}h1{font-size:1.35rem;margin:0}.muted{color:var(--muted)}.controls{display:flex;gap:8px;flex-wrap:wrap;margin:16px 0}.controls input{min-width:190px;border:1px solid var(--line);border-radius:8px;padding:9px}.controls button{background:var(--accent);color:#fff;border:0;border-radius:8px;padding:9px 14px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;box-shadow:0 1px 2px #1020400d}.card h2{font-size:1rem;margin:0 0 10px}.metric{font-size:1.55rem;font-weight:700}.ok{color:#167847}.warn{color:var(--warn)}.bad{color:var(--bad)}ul{padding-left:20px;margin:7px 0}li{margin:5px 0}.wide{grid-column:1/-1}.pill{display:inline-block;border-radius:999px;padding:3px 8px;background:#e9f1ff;color:#1457b8;font-size:.8rem}.disabled{opacity:.5;cursor:not-allowed}.footer{font-size:.8rem;color:var(--muted);margin-top:16px}
</style></head><body><main><div class="top"><h1>100meAiStore 운영 상태</h1><span id="fresh" class="pill">대기</span></div>
<p class="muted">로컬 사전 계산 read-model · 조회 시 AI 호출 0회 · 외부 쓰기 비활성</p>
<div class="controls"><button id="load">새로고침</button><button class="disabled" disabled title="명시적 별도 승인 후에만 활성화">AI 요약 (비활성)</button></div>
<section id="app" class="grid"><div class="card wide">인증된 서버 세션을 확인하고 있습니다.</div></section>
<div class="footer">자동 새로고침 기본 10초. 백그라운드 탭에서는 중지하고 다시 활성화하면 즉시 조회합니다.</div></main>
<script>
const q=id=>document.getElementById(id), esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function list(items){return (items||[]).map(x=>`<li>${esc(x)}</li>`).join('')||'<li>없음</li>'}
function card(title,body){return `<div class="card"><h2>${title}</h2>${body}</div>`}
function render(d){
 const r=d.readiness||{},w=d.workers||{},qz=d.queues||{},p=d.phase||{},t=d.tests||{};
 const completion=p.completion_percent==null?'미확인':p.completion_percent+'%';
 q('fresh').textContent=`${d.stale?'오래됨':'정상'} · ${d.generated_at||''}`;q('fresh').className='pill '+(d.stale?'warn':'');
 q('app').innerHTML=card('현재 Phase',`<div class="metric">${esc(p.name)}</div><p>${esc(completion)} · ${list(p.evidence)}</p>`)+
 card('Readiness',`<div class="metric ${r.ready?'ok':'bad'}">${r.ready?'READY':'NOT READY'}</div><p>schema ${esc(r.schema_version??'-')} · ${esc(r.integrity??r.error??'')}</p>`)+
 card('마지막 테스트',`<div class="metric">${esc(t.last_result)}</div><p>passed ${esc(t.passed??'-')} · failed ${esc(t.failed??'-')}</p>`)+
 card('Worker',`<p>실행 ${Object.keys(w.running||{}).length} · 대기 ${esc(w.waiting)} · 실패 ${esc(w.failed)}</p><ul>${list(Object.entries(w.running||{}).map(([k,v])=>k+': '+v+'개'))}</ul>`)+
 card('Agents / Heartbeat',`<ul>${list((d.agents||[]).map(a=>a.agent_id+' · '+a.state+(a.stale?' · STALE':'')+' · '+(a.current_task||'작업 없음')+(a.usage_limited?' · 사용량 제한':'')))}</ul><p class="muted">${esc(d.agent_status_source)}</p>`)+
 card('적체',`<p>outbox 대기 ${esc(qz.outbox_pending)} · lease ${esc(qz.outbox_leased)} · retry ${esc(qz.outbox_retry)} · dead ${esc(qz.outbox_dead)}</p><p>inbox ${esc(qz.inbox_received)} · reconciliation ${esc(qz.reconciliation_open)}</p>`)+
 card('승인/다음 작업',`<p>승인 필요 <b>${esc(d.approvals_required)}</b></p><p>${esc(d.next_work)}</p>`)+
 card('Blocker',`<ul>${list(d.blockers)}</ul>`)+
 card('토큰·비용',`<p>모델 호출 ${esc(d.tokens_cost?.total_tokens)} tokens · 비용 ${esc(d.tokens_cost?.total_cost)}</p><p class="muted">${esc(d.tokens_cost?.source)}</p>`)+
 card('최근 커밋',`<ul>${list((d.recent_commits||[]).map(x=>x.id+' '+x.subject))}</ul>`)+
 card('감사',`<p>이 tenant의 audit events ${esc(d.audit?.event_count)}</p>`);
}
async function load(){q('fresh').textContent='조회 중…';try{const r=await fetch('/api/dashboard',{cache:'no-store',credentials:'same-origin'});const d=await r.json();if(!r.ok)throw Error(d.error||r.status);render(d)}catch(e){q('app').innerHTML=card('조회 실패',`<p class="bad">${esc(e.message)}</p>`);q('fresh').textContent='오류'}}
q('load').onclick=load;load();setInterval(()=>{if(!document.hidden)load()},10000);document.addEventListener('visibilitychange',()=>{if(!document.hidden)load()});
</script></body></html>"""


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "100meAiStoreDashboard/1"

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send(200, "text/html; charset=utf-8", INDEX_HTML.encode())
            return
        approval_match = re.fullmatch(r"/api/approvals/([A-Za-z0-9_.:-]{1,255})", parsed.path)
        if parsed.path not in {"/api/dashboard", "/api/approvals"} and not approval_match:
            self._send(404, "application/json", b'{"error":"not_found"}')
            return
        if parsed.query:
            self._send_json(400, {"error": "client identity overrides are not accepted"})
            return
        database = Path(self.server.database_path)  # type: ignore[attr-defined]
        if not database.exists():
            self._send_json(503, {"error": "dashboard database is not initialized"})
            return
        repo = None
        try:
            repo = SQLiteRepository(database)
            service = StoreControlPlane(repo)
            context = service.authenticate_browser_session(self._session_token())
            if parsed.path == "/api/dashboard":
                value = service.dashboard_snapshot(context, project_root=str(self.server.project_root))  # type: ignore[attr-defined]
            elif parsed.path == "/api/approvals":
                value = service.approval_inbox(context)
            else:
                value = service.approval_detail(context, approval_match.group(1))  # type: ignore[union-attr]
            self._send_json(200, value)
        except AuthorizationError as exc:
            self._send_json(401, {"error": str(exc)})
        except (NotFoundError, TenantBoundaryError) as exc:
            self._send_json(404, {"error": str(exc)})
        except ConflictError as exc:
            self._send_json(409, {"error": str(exc)})
        finally:
            if repo is not None:
                repo.close()

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        match = re.fullmatch(r"/api/approvals/([A-Za-z0-9_.:-]{1,255})/(nonce|decision)", parsed.path)
        if not match or parsed.query:
            self._send_json(403, {"error": "external writes are disabled"})
            return
        if not self._same_origin() or self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            self._send_json(403, {"error": "same-origin JSON request required"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= 4096:
                raise ValueError
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except (ValueError, json.JSONDecodeError):
            self._send_json(400, {"error": "invalid request body"})
            return
        repo = None
        try:
            database = Path(self.server.database_path)  # type: ignore[attr-defined]
            repo = SQLiteRepository(database)
            service = StoreControlPlane(repo)
            session_token, approval_id, action = self._session_token(), match.group(1), match.group(2)
            if action == "nonce":
                if body:
                    raise ConflictError("nonce request body must be empty")
                issued = service.issue_approval_confirmation_nonce(session_token, approval_id)
                self._send_json(201, {"nonce": issued.token, "expires_at": issued.expires_at.isoformat()})
            else:
                if set(body) != {"approve", "reason", "nonce"}:
                    raise ConflictError("invalid decision body")
                decision = service.decide_approval_authenticated(
                    session_token, approval_id, body["approve"], body["reason"], body["nonce"])
                self._send_json(200, {"approval_id": approval_id, "state": decision.state.value})
        except (AuthorizationError, NotFoundError, ConflictError) as exc:
            self._send_json(401, {"error": str(exc)})
        finally:
            if repo is not None:
                repo.close()

    def _session_token(self) -> str:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            raise AuthorizationError("invalid browser credential") from None
        morsel = cookie.get("store_session")
        if morsel is None:
            raise AuthorizationError("browser session required")
        return morsel.value

    def _same_origin(self) -> bool:
        origin, host = self.headers.get("Origin"), self.headers.get("Host")
        return bool(origin and host and origin in {f"http://{host}", f"https://{host}"})

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send_json(self, status: int, value: dict[str, object]) -> None:
        self._send(status, "application/json; charset=utf-8", json.dumps(value, ensure_ascii=False).encode())

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class DashboardServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], database_path: str | Path, project_root: str | Path) -> None:
        super().__init__(address, DashboardHandler)
        self.database_path = str(database_path)
        self.project_root = Path(project_root)


def main() -> None:
    parser = argparse.ArgumentParser(description="serve the local zero-AI operations dashboard")
    parser.add_argument("--database", default="data/store.sqlite3")
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = DashboardServer((args.host, args.port), args.database, args.project_root)
    print(f"dashboard listening on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
