import json

from jinja2 import Environment, BaseLoader, select_autoescape

TEMPLATE = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{{ run.scenario_name }} · 接口测试报告</title>
<style>
*{box-sizing:border-box}body{font:13px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#202731;background:#f7f9fb;margin:0;padding:24px}
main{max-width:1100px;margin:auto}header,section{background:#fff;padding:20px 24px;border:1px solid #e5e9ee;border-radius:8px;margin-bottom:12px}
h1{margin:4px 0 12px;font-size:20px;font-weight:600;overflow-wrap:anywhere}h2{font-size:15px;font-weight:600;margin:0 0 8px}h3{font-size:13px;margin:16px 0 8px}p{margin:8px 0}
pre{font:12px/1.65 ui-monospace,SFMono-Regular,monospace;white-space:pre-wrap;overflow-wrap:anywhere;background:#f7f9fb;padding:12px;border:1px solid #e5e9ee;border-radius:6px}
.PASS{color:#39725a}.FAIL,.ERROR{color:#ad4f4a}.ABORTED,.SKIP,.UNCHECKED{color:#936323}small,footer{color:#677381}
table{width:100%;border-collapse:collapse;font-size:12px;table-layout:fixed}td,th{text-align:left;border-bottom:1px solid #e5e9ee;padding:8px;overflow-wrap:anywhere}th{background:#f7f9fb;color:#677381;font-weight:500}
summary{cursor:pointer;font-weight:500;padding:8px 0;color:#466b98}details{border-top:1px solid #e5e9ee;margin-top:12px}a{color:#466b98}summary:focus-visible,a:focus-visible{outline:2px solid #466b98;outline-offset:3px}
@media(max-width:600px){body{padding:8px;font-size:14px}header,section{padding:16px}table{font-size:12px}td,th{padding:6px}summary{min-height:44px}pre{font-size:13px}}
</style><main>
<header><small>AutoDroid / 接口自动化</small><h1>{{ run.scenario_name }}</h1>
{% if (run.snapshot or {}).get('description') %}<p>{{ run.snapshot.get('description') }}</p>{% endif %}
<b class="{{ run.status }}">{{ run.status }}</b> · {{ run.env_name }} · {{ run.executor_name }}
<p>开始：{{ run.started_at or run.created_at }} · 总耗时：{{ '%.0f'|format(run.duration_ms) }} ms</p>
<p>{{ summary.total }} 步 · 通过 {{ summary.passed }} · 失败 {{ summary.failed }} · 跳过 {{ summary.skipped }}{% if summary.unchecked %} · 未校验 {{ summary.unchecked }}{% endif %}</p>
{% for warning in warnings %}<p class="UNCHECKED">{{ warning }}</p>{% endfor %}
{% if failure %}<p class="FAIL">{{ failure }}</p>{% elif run.error %}<p>{{ run.error }}</p>{% endif %}</header>
{% for step in steps %}<section id="step-{{ step.step_id }}"><h2>{{ loop.index }}. {{ step.name }} <span class="{{ step.status }}">{{ step.status|status }}</span></h2>
<small>{% if step.detail.response %}HTTP {{ step.detail.response.status_code }} · {% endif %}{{ '%.0f'|format(step.duration_ms) }} ms{% if step.detail.attempts|default(1) > 1 %} · 共尝试 {{ step.detail.attempts }} 次{% endif %} · {{ step.detail.assertions|default([])|selectattr('passed')|list|length }}/{{ step.detail.assertions|default([])|length }} 条断言通过</small>
{% if step.detail.error %}<p>{{ step.detail.error }}</p>{% endif %}
{% if step.detail.retry_history %}<details><summary>重试记录（仅连接失败会重试）</summary>{% for item in step.detail.retry_history %}<p>第 {{ item.attempt }} 次：{{ item.error }}</p>{% endfor %}</details>{% endif %}
{% if step.detail.assertions %}<h3>断言</h3><table><tr><th>字段</th><th>条件</th><th>实际值</th><th>期望值</th><th>结果</th></tr>
{% for a in step.detail.assertions %}<tr><td>{{ a.path|path }}</td><td>{{ a.op|operator }}</td><td>{{ '字段不存在' if a.missing else a.actual|pretty }}</td>
<td>{{ '200–299' if a.op == 'is_2xx' else '—' if a.op in ['exists','not_empty'] else a.expected|pretty }}</td><td class="{{ 'PASS' if a.passed else 'FAIL' }}">{{ '通过' if a.passed else a.message }}</td></tr>{% endfor %}</table>{% endif %}
{% if step.detail.response %}{% set response = step.detail.response %}
<details {{ 'open' if step.status in ['FAIL','ERROR'] else '' }}><summary>响应正文</summary><pre>{{ response.body|pretty if 'body' in response else response.text }}</pre></details>
{% for key,title in [('headers','响应头'),('cookies','Cookie'),('text','原始文本')] %}<details><summary>{{ title }}</summary><pre>{{ response[key]|pretty }}</pre></details>{% endfor %}{% endif %}
{% if step.detail.request %}<details><summary>请求详情</summary><pre>{{ step.detail.request|pretty }}</pre></details>{% endif %}
{% if step.detail.references %}<details><summary>引用来源</summary>{% for ref in step.detail.references %}<p><a href="#step-{{ ref.step_id }}">{{ names.get(ref.step_id, '历史步骤') }} → {{ ref.path|path }}</a></p><pre>{{ ref.value|pretty }}</pre>{% endfor %}</details>{% endif %}
</section>{% endfor %}<footer>历史配置已冻结。已发送的业务请求不会因中止测试而撤销。</footer></main></html>"""


def summary(steps):
    return {
        "total": len(steps),
        "passed": sum(s.status == "PASS" for s in steps),
        "failed": sum(s.status in {"FAIL", "ERROR"} for s in steps),
        "skipped": sum(s.status == "SKIP" for s in steps),
        "unchecked": sum(s.status == "UNCHECKED" for s in steps),
    }


def report_warnings(run, steps):
    definitions = {step["id"]: step for step in (run.snapshot or {}).get("steps", [])}
    names = []
    for step in steps:
        definition = definitions.get(step.step_id, {})
        detail = step.detail or {}
        # Waiting is successful without assertions. Only old request results
        # labelled PASS need this compatibility warning; stored status stays intact.
        is_request = definition.get("kind", "request") != "wait" and (
            bool(definition) or "request" in detail or "response" in detail
        )
        if is_request and step.status == "PASS" and not detail.get("assertions"):
            names.append(step.name)
    if not names:
        return []
    return [f"历史记录中的「{'、'.join(names)}」未配置断言：当时的通过仅表示请求完成，未验证接口结果。"]


def render_report(run, steps):
    env = Environment(loader=BaseLoader(), autoescape=select_autoescape(default=True))
    env.filters["pretty"] = lambda value: json.dumps(value, ensure_ascii=False, indent=2, default=str)
    env.filters["path"] = lambda path: " › ".join(f"[{p}]" if isinstance(p, int) else str(p) for p in path)
    env.filters["status"] = lambda status: {"PASS": "通过", "FAIL": "断言失败", "ERROR": "执行异常", "SKIP": "已跳过", "ABORTED": "已中止", "UNCHECKED": "未校验"}.get(status, status)
    env.filters["operator"] = lambda op: {
        "is_2xx": "状态码 2xx",
        "eq": "等于",
        "ne": "不等于",
        "contains": "包含",
        "exists": "存在",
        "not_empty": "非空",
        "gt": "大于",
        "gte": "大于等于",
        "lt": "小于",
        "lte": "小于等于",
        "type": "类型为",
        "length": "长度等于",
    }.get(op, op)
    names = {s["id"]: s["name"] for s in (run.snapshot or {}).get("steps", [])}
    failure = ""
    for index, step in enumerate(steps):
        if step.status not in {"FAIL", "ERROR"}:
            continue
        check = next((a for a in step.detail.get("assertions", []) if not a["passed"]), None)
        reason = step.detail.get("error", "执行异常")
        if check:
            expected = "200–299" if check["op"] == "is_2xx" else env.filters["pretty"](check.get("expected"))
            actual = "字段不存在" if check.get("missing") else env.filters["pretty"](check.get("actual"))
            reason = f"{env.filters['path'](check['path'])}，期望 {expected}，实际 {actual}（{check['message']}）"
        failure = f"第 {index + 1} 步「{step.name}」失败：{reason}"
        break
    return env.from_string(TEMPLATE).render(run=run, steps=steps, summary=summary(steps), names=names, failure=failure,
                                          warnings=report_warnings(run, steps))
