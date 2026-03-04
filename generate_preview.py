import sys
import os
from jinja2 import Environment, FileSystemLoader

env = Environment(loader=FileSystemLoader('/Users/liuzhenyu/Desktop/x/AutoDroid/backend/templates'))

template1 = env.get_template('report.html')
html1 = template1.render(
    case_name="Login Test", case_id=1, 
    start_time="2026-02-20 10:00:00", end_time="2026-02-20 10:00:15", total_duration=15.0,
    total_steps=5, passed=4, failed=1, pass_rate=80.0,
    steps=[
        {"status": "success", "action": "click", "description": "Click Login Button", "selector_type": "text", "selector": "Login", "duration": 1.2},
        {"status": "failed", "action": "input", "description": "Type Password", "selector_type": "id", "selector": "password_field", "duration": 5.0, "error": "Element not found after 3 retries"}
    ],
    variables=[{"key": "USERNAME", "value": "admin"}, {"key": "PASSWORD", "value": "123456"}],
    generated_at="2026-02-20 10:01:00"
)
with open('/Users/liuzhenyu/Desktop/x/AutoDroid/test_report_preview.html', 'w') as f:
    f.write(html1)

template2 = env.get_template('scenario_report.html')
html2 = template2.render(
    scenario_name="Core Flow Smoke Test", scenario_id=10,
    start_time="2026-02-20 10:00:00", end_time="2026-02-20 10:05:00", total_duration=300.0,
    total_cases=2, passed_cases=1, failed_cases=1, pass_rate=50.0,
    cases_results=[
        {
            "case_name": "Login Case", "case_id": 1, "status": "success",
            "steps": [{"status": "success", "action": "click", "description": "Click Login", "selector_type": "text", "selector": "Login", "duration": 2.0}]
        },
        {
            "case_name": "Checkout Case", "case_id": 2, "status": "failed",
            "steps": [
                {"status": "success", "action": "click", "description": "Add to Cart", "selector_type": "id", "selector": "btn_add", "duration": 1.0},
                {"status": "failed", "action": "click", "description": "Pay Now", "selector_type": "text", "selector": "Pay", "error": "Timeout waiting for element", "duration": 10.0}
            ]
        }
    ],
    generated_at="2026-02-20 10:05:00"
)
with open('/Users/liuzhenyu/Desktop/x/AutoDroid/test_scenario_preview.html', 'w') as f:
    f.write(html2)
print("Previews generated.")
