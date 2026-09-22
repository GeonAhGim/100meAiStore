"""Read-only local dashboard with explicit handoff controls."""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from .pm import status as pm_status
from .pm_cycle import current as pm_cycle_status, start as start_pm_cycle
from .recovery import current as recovery_status, start as start_recovery
from .autopilot import start as start_autopilot, status as autopilot_status
from .state import grant_handoff, revoke_handoff, snapshot


HTML = """<!doctype html><html lang=ko><meta charset=utf-8>
<title>smart_store 관제</title><style>
body{font:14px system-ui;background:#111827;color:#e5e7eb;max-width:1100px;margin:30px auto;padding:0 18px}
h1{margin-bottom:4px}.muted{color:#9ca3af}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px}
.card{background:#1f2937;border:1px solid #374151;border-radius:10px;padding:16px;margin:12px 0}.value{font-size:25px;font-weight:700;margin-top:8px}
button{padding:9px 12px;border:0;border-radius:6px;background:#2563eb;color:white;cursor:pointer;margin-right:6px}button.stop{background:#b91c1c}
table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:8px;border-bottom:1px solid #374151}.done{color:#86efac}.in_progress{color:#93c5fd}.ready{color:#fcd34d}.blocked{color:#fca5a5}.summary-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:8px}.summary-item{background:#111827;border-radius:7px;padding:11px}.summary-item b{display:block;font-size:22px;margin-top:4px}.running{border-left:3px solid #60a5fa;padding-left:12px;margin:10px 0}.running h3{margin:0 0 5px}.running p{margin:3px 0}.badge{border:1px solid #64748b;border-radius:10px;padding:2px 7px;font-size:11px}.healthy{color:#86efac;border-color:#16a34a}.stale{color:#fcd34d;border-color:#ca8a04}.error{color:#fca5a5;border-color:#dc2626}.empty{color:#9ca3af;padding:8px 0}
</style><style>.worker-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:10px}.worker{background:#111827;border:1px solid #374151;border-radius:8px;padding:12px}.worker h3{margin:0 0 7px}.worker p{margin:4px 0}.deficit{color:#fca5a5}.ok{color:#86efac}</style><body><h1>smart_store 개발 관제</h1><p class=muted>AIOS 우선 · 독립 프로젝트 · 로컬 LLM 전용</p>
<div id=app>불러오는 중…</div><script>
const e=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function action(path){await fetch(path,{method:'POST'});load()}
function render(d){const r=d.runtime||{},s=r.slots||{},l=r.local_llm||{},c=d.counts||{},a=d.aios_live||{},pm=d.pm||{},tasks=pm.tasks||[],running=tasks.filter(x=>['in_progress','reviewing'].includes(x.status)),pool=pm.worker_pool||{},pcr=d.pm_recovery||d.pm_cycle||{},ap=d.autopilot||{};
 document.querySelector('#app').innerHTML=`<div class=grid><div class=card>AIOS 우선순위<div class=value>항상 우선</div></div><div class=card>AIOS 실시간 슬롯<div class=value>${e(a.aios_used_slots??'-')} / ${e(a.total_slots??'-')}</div><span class=muted>가용 ${e(a.available_slots??0)}</span></div><div class=card>smart_store 슬롯<div class=value>${e(s.active||0)} / ${e(a.available_slots ?? s.smart_store_reserved ?? 1)}</div></div><div class=card>로컬 LLM<div class=value>${l.enabled?'활성':'대기'}</div></div><div class=card>MVP 진행<div class=value>${e(c.done||0)} / ${e((d.milestones||{}).milestones?.length||0)}</div><span class=muted>진행 ${e(c.in_progress||0)} · 준비 ${e(c.ready||0)}</span></div></div>
 <div class=card><b>슬롯 제어</b><p class=muted>AIOS 작업이 우선이며, 명시적 핸드오프 전에는 smart_store가 로컬 LLM을 사용하지 않습니다.</p><button onclick="action('/api/handoff/grant')">핸드오프 승인</button><button class=stop onclick="action('/api/handoff/revoke')">즉시 중지</button><p>${r.handoff_granted?'현재 smart_store 예약 슬롯 사용 가능':'현재 smart_store 대기 상태'}</p></div>
 <div class=card><b>로컬 LLM PM·자동 복구</b><p class=muted>Codex 한도·불가 상황은 로컬 우선으로 전환합니다. 경고·정체·대기 작업을 자동 점검합니다. AIOS는 읽기 전용입니다.</p><button onclick="action('/api/pm/recover')">재점검·자동 복구</button><p>${e(pcr.status||'idle')} · ${e(pcr.artifact||pcr.message||'호출 대기')}</p><p class=muted>자동운영 ${ap.enabled&&ap.local_first?'활성':'비활성'} · 다음 점검 ${e(ap.poll_seconds||20)}초</p></div>
 <div class=card><h2>상태 요약</h2><div class=summary-grid><div class=summary-item>전체 작업<b>${e(tasks.length)}</b></div><div class=summary-item>실행 중<b>${e(c.in_progress||0)}</b></div><div class=summary-item>대기<b>${e(c.ready||0)}</b></div><div class=summary-item>완료<b>${e(c.done||0)}</b></div><div class=summary-item>차단<b>${e(c.blocked||0)}</b></div><div class=summary-item>확인 필요<b>${e(pm.attention||0)}</b></div><div class=summary-item>유효 슬롯<b>${e(pm.capacity?.effective??0)}</b></div></div><p class=muted>AIOS 가용 ${e(pm.capacity?.aios_available??0)} · 핸드오프 ${r.handoff_granted?'승인':'대기'} · 엔진 ${e(l.enabled?'local-http':'비활성')}</p></div>
 <div class=card><h2>워커풀 현황</h2><p>풀 <b>${e(pool.name||'smart-store-local')}</b> · 목표 <b>${e(pool.target??2)}</b> · 활성 <b>${e(pool.active??0)}</b> · 유효 <b>${e(pool.effective??0)}</b> · AIOS 가용 <b>${e(pool.aios_available??0)}</b> ${pool.deficit?`· <span class=deficit>부족 ${e(pool.deficit)}</span>`:'· <span class=ok>목표 충족</span>'}</p><p class=muted>구현 ${e(pool.in_progress??0)} · 리뷰 ${e(pool.reviewing??0)} · 리뷰 대기 ${e(pool.needs_review??0)} · 준비 ${e(pool.ready??0)} · AIOS 우선 슬롯을 침범하지 않음</p><div class=worker-grid>${(pool.workers||[]).length?(pool.workers||[]).map(w=>`<div class=worker><h3>${e(w.worker||'-')} <span class="badge ${e(w.health||'unknown')}">${e(w.health||'unknown')}</span></h3><p>task-${e(w.task_id)} · ${e(w.title||'-')}</p><p>상태 ${e(w.status||'-')} · 단계 <b>${e(w.phase||'-')}</b></p><p class=muted>heartbeat ${e(w.heartbeat_at||'-')}</p><p class=muted>시작 ${e(w.started_at||'-')}</p></div>`).join(''):'<div class=empty>현재 실행 중인 워커가 없습니다. 작업이 생기면 목표 ${e(pool.target??2)}개까지 병렬 기동됩니다.</div>'}</div></div>
 <div class=card><h2>실행 중</h2>${running.length?running.map(x=>`<div class=running><h3>task-${e(x.id)} · ${e(x.title)} <span class="badge ${e(x.health)}">${e(x.health||'unknown')}</span></h3><p>${e(x.milestone||'-')} · ${e(x.role||'local-impl')} · 워커 ${e(x.worker||x.reviewer||'-')}</p><p>단계: <b>${e(x.phase||'unknown')}</b> · heartbeat: <b>${e(x.heartbeat_at||'-')}</b></p><p class=muted>시작 ${e(x.started_at||'-')}</p></div>`).join(''):'<div class=empty>현재 실행 중인 smart_store 워커가 없습니다.</div>'}</div>
 <div class=card><h2>MVP 세부 마일스톤</h2><table><tr><th>ID</th><th>상위 단계</th><th>작업</th><th>상태</th><th>우선순위</th><th>의존성</th><th>완료 조건</th></tr>${((d.milestones||{}).milestones||[]).map(x=>`<tr><td>${e(x.id)}</td><td>${e(x.parent||'-')}</td><td>${e(x.title)}</td><td class=${e(x.status)}>${e(x.status)}</td><td>${e(x.priority??'-')}</td><td>${e((x.depends_on||[]).join(', ')||'-')}</td><td>${e(x.exit_criteria)}</td></tr>`).join('')}</table></div><div class=card><h2>로컬 PM·워커풀</h2><p>유효 용량 ${e((d.pm||{}).capacity?.effective??0)} · 설정 ${e((d.pm||{}).capacity?.configured??0)} · AIOS 가용 ${e((d.pm||{}).capacity?.aios_available??0)}</p><table><tr><th>ID</th><th>작업</th><th>상태</th><th>워커</th></tr>${((d.pm||{}).tasks||[]).map(x=>`<tr><td>${e(x.id)}</td><td>${e(x.title)}</td><td class=${e(x.status)}>${e(x.status)}</td><td>${e(x.worker||'-')}</td></tr>`).join('')}</table></div><p class=muted>갱신 ${e(d.generated_at)}</p>`}
async function load(){const r=await fetch('/api/control',{cache:'no-store'});render(await r.json())}load();setInterval(load,10000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/":
            body = HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        elif path == "/api/control":
            view = snapshot()
            view["pm"] = pm_status()
            view["pm_cycle"] = pm_cycle_status()
            view["pm_recovery"] = recovery_status()
            view["autopilot"] = autopilot_status()
            body = json.dumps(view, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
        else:
            self.send_error(404)
            return
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/api/handoff/grant":
            result = grant_handoff()
        elif path == "/api/handoff/revoke":
            result = revoke_handoff()
        elif path == "/api/pm/run":
            result = start_pm_cycle()
        elif path == "/api/pm/recover":
            result = start_recovery()
        else:
            self.send_error(404)
            return
        body = json.dumps(result, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser(description="smart_store local control dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8877)
    args = parser.parse_args()
    start_autopilot()
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
