"""Whole-site visual QA fixtures; no database, hardware, network clients or webhooks.

Run from the repository root:
  AUTODROID_UI_PREVIEW=1 .venv/bin/python -m uvicorn backend.tests.ui_preview_app:app --host 127.0.0.1 --port 8767
Login: preview / preview-only (admin), member / preview-only (ordinary user).
AUTODROID_UI_PREVIEW_DIST may select an independently built baseline directory.
Every mutation changes only this process's in-memory fixtures. Restart to reset.
"""
import asyncio
import base64
import copy
import os
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request, WebSocket
from backend.core.errors import install_error_handlers
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from backend.tests.ui_preview_report_fixtures import make_report_fixtures, report_fixture_response

if os.environ.get('AUTODROID_UI_PREVIEW') != '1':
    raise RuntimeError('Explicit AUTODROID_UI_PREVIEW=1 is required')
ROOT = Path(__file__).resolve().parents[2]
DIST = Path(os.environ.get('AUTODROID_UI_PREVIEW_DIST', ROOT / 'frontend/dist')).resolve()
app = FastAPI(title='Isolated AutoDroid visual QA')
install_error_handlers(app)
NOW = '2026-10-03T10:24:00'
USER = dict(id=1, username='preview', full_name='林序', role='admin', is_active=True, created_at=NOW)
ENVS = [dict(id=1, name='集成测试', description='隔离界面验收'), dict(id=2, name='预发布', description='示例数据')]
STATES = ['PASS', 'PASS', 'FAIL', 'RUNNING', 'QUEUED', 'WARNING', None]
NAMES = ['账号密码登录', '手机号验证码登录', '首页核心入口可达', '商品详情与规格选择', '购物车批量结算', '订单支付与结果确认', '收货地址新增与编辑', '优惠券领取与核销', '搜索联想与筛选', '消息通知与设置', '用户资料更新', '退出登录与安全校验']

def common(i, name):
    return dict(id=i, name=name, user_id=1 if i % 3 else 2, creator_name='林序' if i % 3 else '周宁', updater_name='林序', created_at='2026-09-25T09:00:00', updated_at=NOW, folder_id=(i % 3)+1)

def steps(i=1):
    return [dict(uuid=f'{i}-1', action='start_app', selector='com.demo.shop', selector_type='text', value='', description='', timeout=10, execute_on=['android', 'ios'], platform_overrides={'android':None,'ios':None}, error_strategy='ABORT'), dict(uuid=f'{i}-2', action='click', selector='登录', selector_type='text', value='', description='进入账号登录', timeout=10, execute_on=['android','ios'], platform_overrides={'android':None,'ios':None}, error_strategy='ABORT'), dict(uuid=f'{i}-3', action='assert_text', selector='', selector_type='text', value='欢迎回来', description='校验登录结果', timeout=10, execute_on=['android','ios'], platform_overrides={'android':None,'ios':None}, error_strategy='ABORT')]
CASES = [{**common(i, NAMES[(i-1)%12]+(' · 回归验证' if i>12 else '')), 'tags':[f'P{(i-1)%4}'], 'steps':steps(i), 'variables':[], 'last_run_status':STATES[(i-1)%7]} for i in range(1,33)]
SCENARIOS = [{**common(i, ['购买主流程','会员中心回归','活动优惠全链路','售后退款流程','账号安全巡检','首页导航回归'][(i-1)%6]+f' · {i:02d}'), 'step_count':3+(i%5), 'last_run_status':STATES[(i-1)%7], 'last_run_time':NOW,'last_execution_id':i,'last_report_id':i,'last_failed_step':'优惠金额校验' if i%7==3 else None,'last_executor':'林序','last_run_duration':26.4,'duration':26.4} for i in range(1,29)]
DEVICES = [dict(serial='preview-android-1', platform='android', model='Pixel 9', market_name='Pixel 9', custom_name='Android · 回归机', status='IDLE', resolution='1080x2400', os_version='15', version='15', battery=86, connection_type='usb', transport='usb'), dict(serial='preview-ios-1',platform='ios',model='iPhone 16',market_name='iPhone 16',custom_name='iOS · 回归机',status='IDLE',resolution='1179x2556',os_version='18.1',version='18.1',battery=74,connection_type='usb',transport='usb'), dict(serial='preview-android-2',platform='android',model='Xiaomi 15',custom_name='Android · 稳定性',status='BUSY',resolution='1080x2400',os_version='15',battery=61),dict(serial='preview-ios-2',platform='ios',model='iPhone 15',custom_name='iOS · 待维护',status='WDA_DOWN',resolution='1179x2556',os_version='18',battery=40),dict(serial='preview-offline',platform='android',model='OPPO Find X8',custom_name='Android · 离线',status='OFFLINE',resolution='1080x2400',os_version='15',battery=0)]
SVG = '''<svg xmlns="http://www.w3.org/2000/svg" width="360" height="780"><rect width="360" height="780" fill="#f7f9fb"/><text x="26" y="32" font-family="sans-serif" font-size="14" fill="#202731">9:41</text><rect x="24" y="76" width="312" height="42" rx="12" fill="#e7edf3"/><text x="40" y="103" font-family="sans-serif" font-size="14" fill="#677381">搜索商品</text><rect x="24" y="142" width="312" height="170" rx="16" fill="#dae7e2"/><text x="46" y="202" font-family="sans-serif" font-size="28" fill="#345f50">生活，从容一点</text><text x="46" y="239" font-family="sans-serif" font-size="14" fill="#56756a">AUTODROID · 隔离示例画面</text><rect x="24" y="338" width="148" height="182" rx="12" fill="#eee7df"/><rect x="188" y="338" width="148" height="182" rx="12" fill="#e1e8ef"/><text x="38" y="549" font-family="sans-serif" font-size="16" fill="#202731">精选好物</text><text x="202" y="549" font-family="sans-serif" font-size="16" fill="#202731">每日上新</text><rect x="24" y="586" width="312" height="48" rx="9" fill="#466b98"/><text x="148" y="616" font-family="sans-serif" font-size="17" fill="white">登录</text><line x1="0" y1="710" x2="360" y2="710" stroke="#e5e9ee"/><text x="37" y="745" font-family="sans-serif" font-size="14" fill="#466b98">首页</text><text x="119" y="745" font-family="sans-serif" font-size="14" fill="#677381">分类</text><text x="201" y="745" font-family="sans-serif" font-size="14" fill="#677381">购物车</text><text x="291" y="745" font-family="sans-serif" font-size="14" fill="#677381">我的</text></svg>'''
SCREEN = base64.b64encode(SVG.encode()).decode()
DUMP = dict(screenshot=SCREEN,screenshot_format='svg+xml',device_info=dict(serial=DEVICES[0]['serial'],width=360,height=780,displayWidth=360,displayHeight=780),hierarchy_xml='<hierarchy><node text="登录" resource-id="com.demo.shop:id/login" class="android.widget.Button" clickable="true" bounds="[24,586][336,634]"/><node text="首页" class="android.widget.TextView" clickable="true" bounds="[20,710][80,760]"/></hierarchy>')
REPORTS = [dict(id=i,scenario_id=i,scenario_name=SCENARIOS[(i-1)%28]['name'],case_name=CASES[(i-1)%32]['name'],name=SCENARIOS[(i-1)%28]['name'], status=STATES[(i-1)%7] or 'PASS', start_time=NOW,end_time=NOW,duration=26.4,executor_name='林序',device_serial=DEVICES[(i-1)%2]['serial'],device_info=DEVICES[(i-1)%2]['model'],platform=DEVICES[(i-1)%2]['platform'],batch_id=f'preview-batch-{(i+1)//2}' if i < 5 else f'preview-batch-{i}',report_path='preview-report.html',passed_steps=3,failed_steps=int(i%7==3),total_steps=3) for i in range(1,29)]
PACKAGES = [{**common(1,'演示商城'), 'file_name':'shop-2.8.0.apk','filename':'shop-2.8.0.apk','package_name':'com.demo.shop','version_name':'2.8.0','version_code':20800,'platform':'android','file_size':43112233,'size':43112233,'app_name':'演示商城'}, {**common(2,'演示商城 iOS'), 'file_name':'shop-2.8.0.ipa','filename':'shop-2.8.0.ipa','package_name':'com.demo.shop','version_name':'2.8.0','platform':'ios','file_size':52112233,'size':52112233,'app_name':'演示商城'}]
TASKS = [dict(id=i,name=f'夜间回归 · {i}',task_type='ui',scenario_id=i,scenario_name=SCENARIOS[i-1]['name'],device_serials=[DEVICES[0]['serial']],env_id=1,cron='0 2 * * *',enabled=i%2==1,next_run_time='2026-10-04T02:00:00',created_at=NOW) for i in range(1,5)]
FASTBOT = [dict(id=i,name=f'商城稳定性 · {i}',package_name='com.demo.shop',device_serial=DEVICES[0]['serial'],device_info='Pixel 9',status='COMPLETED',duration=30,minutes=30,started_at=NOW,finished_at=NOW,created_at=NOW,crash_count=0,anr_count=0,session_type='startup' if i==2 else 'fastbot',executor_name='林序') for i in range(1,7)]
PAGESETS = [dict(id=1,name='商城核心页面',package_name='com.demo.shop',platform='android',created_at=NOW,pages=[dict(id=1,name='首页',case_id=1),dict(id=2,name='购物车',case_id=2)])]
COMPAT = [dict(id=i,name=f'核心页面兼容性 · {i}',status='COMPLETED',created_at=NOW,started_at=NOW,finished_at=NOW,execution_mode='install',compare_mode='device',device_count=2,total_cells=2,completed_cells=2,passed_cells=2,failed_cells=0,page_set=PAGESETS[0],page_set_name='商城核心页面',package_name='com.demo.shop',cells=[]) for i in range(1,7)]
PROFILES = [dict(id=1,name='商城巡检',package_name='com.demo.shop',platform='android',goal='探索首页与下单流程',max_steps=80,max_minutes=30,branches={'guest':{'name':'未登录','prepare_case_id':1,'entry_case_id':2,'env_id':1,'ready_assertion':{'by':'text','selector':'首页','timeout':5},'scope':'full'}},budgets={'duration_seconds':1800},monitor_options={},allowed_packages=['com.demo.shop'],created_at=NOW)]
INSPECTIONS = [dict(id=i,name=f'商城智能巡检 · {i}',profile_id=1,profile_name='商城巡检',package_name='com.demo.shop',device_serial=DEVICES[0]['serial'],platform='android',status='COMPLETED',started_at=NOW,finished_at=NOW,created_at=NOW,steps=36,total_steps=36,stable_count=7,state_count=9,transition_count=16,stop_reason='coverage_complete',terminal_outcome='completed',replay_evidence_available=False,coverage_assessment={}) for i in range(1,5)]
REPORT_FIXTURES = make_report_fixtures(NOW, DEVICES)
FASTBOT = REPORT_FIXTURES['fastbot']
COMPAT = REPORT_FIXTURES['compat']
INSPECTIONS = REPORT_FIXTURES['inspections']
SETTINGS = [{'key':key,'value':value} for key,value in dict(system_base_url='http://127.0.0.1:8767',api_testing_ai_enabled='true',ai_model='隔离模拟模型',ai_api_base='https://preview.invalid/v1',ai_api_key='preview-never-sent').items()]
FLAGS = dict(model_inspection=True,inspection_identity_v2=True,inspection_similarity_convergence=True,inspection_exploration_family_convergence=True,inspection_business_coverage_v2=False,content_addressed_assets=True,tiered_asset_retention=False)


def literal(value=None):
    return dict(kind='literal',value=value,name='',step_id='',path=[],parts=[],fields={},items=[])

def config(i=1):
    return dict(request=dict(method='GET' if i%2 else 'POST',url=literal('https://api.demo.test/orders'),path_params=[],query=[],headers=[],body_type='none',body=literal(),form=[],auth=dict(kind='none',token=literal(''),username=literal(''),password=literal(''),key_name='X-API-Key',location='header'),timeout_seconds=30),assertions=[dict(id='status',path=['status_code'],op='is_2xx',expected=literal())],sensitive_paths=[])
INTERFACES = [{**common(i,NAMES[(i-1)%12]+'接口'), 'description':'隔离示例，不发送外部请求','version':1,'config':config(i),'method':'GET' if i%2 else 'POST','url':'https://api.demo.test/orders','sample':{'code':0,'data':{'id':42}},'sample_fields':[]} for i in range(1,25)]
API_SCENARIOS = [{**common(i,f'下单接口回归 · {i:02d}'), 'description':'登录 → 查询订单','version':1,'env_id':1,'step_count':2,'steps':[dict(id=f'step-{j}',name=INTERFACES[j-1]['name'],kind='request',interface_id=j,interface_version=1,snapshot=config(j),overrides={},seconds=1,response_schema=None) for j in range(1,3)]} for i in range(1,25)]
DEBUG_SESSIONS = {}
API_RUNS = [dict(id=f'preview-api-{i}',scenario_id=i,scenario_name=API_SCENARIOS[i-1]['name'],status='FAIL' if i==2 else 'PASS',executor_name='林序',started_at=NOW,finished_at=NOW,duration_ms=430,env_id=1,env_name='集成测试',error='断言未通过' if i==2 else '',notification_status='SKIPPED',snapshot=copy.deepcopy(API_SCENARIOS[i-1]),summary=dict(total=1,passed=0 if i==2 else 1,failed=1 if i==2 else 0,skipped=0)) for i in range(1,7)]


def page(items, request):
    items = list(items)
    keyword = request.query_params.get('keyword','').lower()
    if keyword: items = [row for row in items if keyword in str(row).lower()]
    folder_id = request.query_params.get('folder_id')
    if folder_id and folder_id != '0': items = [row for row in items if str(row.get('folder_id')) == folder_id]
    skip = int(request.query_params.get('skip',0)); limit = int(request.query_params.get('limit',20))
    return dict(items=items[skip:skip+limit],total=len(items))


def tree(rows, kind):
    leaves=[dict(id=f'{kind}-{r["id"]}',name=r['name'],type=kind,**{f'{kind}_id':r['id']}) for r in rows]
    return dict(tree=[dict(id=f'folder-{i}',folder_id=i,name=n,type='folder',children=[leaves[j] for j,r in enumerate(rows) if r['folder_id']==i]) for i,n in enumerate(['核心回归','交易流程','账号与安全'],1)], **{f'all_{kind}s':leaves})


@app.middleware('http')
async def local_only(request, call_next):
    if request.client and request.client.host not in {'127.0.0.1','::1','testclient'}:
        return JSONResponse({'detail':'Preview is restricted to localhost'},status_code=403)
    return await call_next(request)


@app.api_route('/api/{path:path}', methods=['GET','POST','PUT','PATCH','DELETE'])
async def fixture(path: str, request: Request):
    path=path.strip('/'); method=request.method
    body={}
    if method!='GET':
        try: body=await request.json()
        except Exception: pass
    report_result = report_fixture_response(path, method, request.query_params, REPORT_FIXTURES, SVG)
    if report_result is not None: return report_result
    if path=='auth/token':
        params=parse_qs((await request.body()).decode()); username=params.get('username',[''])[0]
        if username not in {'preview','member'} or params.get('password',[''])[0]!='preview-only': raise HTTPException(401,'隔离预览账号或密码错误')
        return dict(access_token=f'preview-{username}',token_type='bearer')
    if path=='auth/users/me': return {**USER, **({'id':2,'role':'user','username':'member','full_name':'周宁'} if request.headers.get('authorization','').endswith('preview-member') else {})}
    if path in {'auth/registration-status','admin/registration-settings'}: return dict(allow_registration=True)
    if path=='settings/feature-flags': return FLAGS
    if path=='settings':
        if method=='POST' and isinstance(body,list): SETTINGS[:]=body
        return SETTINGS
    if path=='settings/test-notification': return dict(success=True,message='隔离预览：模拟发送，不发送外部通知')
    if path=='environments': return ENVS
    if path.startswith('environments/') and path.endswith('/variables'): return [dict(id=1,key='base_url',value='https://api.demo.test',description='隔离环境地址'),dict(id=2,key='account',value='preview',description='演示账号')]
    if path in {'devices','fastbot/devices'}: return DEVICES
    if path=='device-agents': return []
    if path=='device/dump': return DUMP
    if path.endswith('/screenshot'): return dict(screenshot=SCREEN,screenshot_format='svg+xml')
    if path=='stream/devices': return []
    if path=='device/interact': return dict(success=True,dump=DUMP,step=steps()[1] if body.get('record_step') else None)
    if path=='device/inspect': return dict(node={'text':'登录','bounds':'[24,586][336,634]'},step=steps()[1])
    if path=='device/execute_step': return dict(success=True,dump=DUMP,message='隔离模拟成功')
    if path=='folders/tree': return tree(CASES,'case')
    if path=='scenario-folders/tree': return tree(SCENARIOS,'scenario')
    if path=='runs/active':
        target=int(request.query_params.get('target_id',0)); status=STATES[(target-1)%7] if target else None
        return dict(items=[dict(run_id=target,execution_id=target,batch_id=f'preview-batch-{target}',device_serial=DEVICES[0]['serial'],status=status,queue_position=2 if status=='QUEUED' else None)] if status in {'RUNNING','QUEUED'} else [])
    if path=='limiter/stats': return dict(max_concurrent=4,active_count=2,queued_count=1)
    if path=='reports/dashboard/overview': return dict(range='7d',platform='all',generated_at=NOW,kpis=dict(total_executions=168,pass_rate=94.6,failed_scenarios=3,avg_duration=26.4,running_executions=2,idle_devices=2,active_tasks=4),trend=[dict(date=f'09-{i}',total=20+i%7,pass_count=18+i%7,fail_count=i%3,warning_count=1) for i in range(24,31)],status_distribution=[dict(status='PASS',count=159),dict(status='FAIL',count=6),dict(status='WARNING',count=3)],top_failed_scenarios=[],alerts=[dict(level='warning',title='1 台 iOS 设备需要维护',message='WDA 未就绪',link='/assets/devices')],recent_executions=REPORTS[:6],upcoming_tasks=TASKS)
    if path=='reports/dashboard/stats': return dict(total=168,passed=159,failed=6,pass_rate=94.6)
    if path=='reports/flaky': return dict(items=[],total=0)
    if path=='reports/executions/compare': return dict(summary={},cases=[],steps=[],items=[],base=REPORTS[0],target=REPORTS[1])
    if path=='reports/executions': return page(REPORTS,request)
    if path.startswith('reports/executions/') and path.endswith('/download'):
        return HTMLResponse('<!doctype html><meta charset="utf-8"><h1>隔离预览报告</h1><p>示例执行结果</p>')
    if path.startswith('reports/executions/'):
        i=int(path.split('/')[-1]); row=copy.deepcopy(REPORTS[(i-1)%len(REPORTS)])
        row['steps']=[dict(id=j,step_order=j,step_name=f'[{CASES[0]["name"]}] '+name,status='FAIL' if i==3 and j==3 else 'PASS',duration=1.2,error_message='未找到预期文本：欢迎回来' if i==3 and j==3 else '',screenshot_path='preview.svg' if j==3 else '',report_display={}) for j,name in enumerate(['启动应用','点击登录按钮','断言登录成功'],1)]
        return row
    if path=='report-assets/preview.svg': return Response(SVG,media_type='image/svg+xml')
    if path=='assets/status': return dict(enabled=True,total_assets=48,total_bytes=124151398,physical_bytes=82767598,saved_bytes=41383800,deduplication_ratio=33.3,by_kind=[])
    if path=='packages': return page(PACKAGES,request)
    if path=='tasks': return page(TASKS,request)
    if path=='tokens': return [dict(id=1,name='CI 回归流水线',token_prefix='preview_',user_name='林序',created_at=NOW,last_used_at=NOW)]
    if path=='admin/users': return [USER,{**USER,'id':2,'username':'member','full_name':'周宁','role':'user'}]
    if path in {'fastbot/tasks','fastbot/startup/tasks','fastbot/fluency/sessions'}: return page(FASTBOT,request)
    if path.startswith('fastbot/tasks/'): return FASTBOT[(int(path.split('/')[-1])-1)%6]
    if path.startswith('fastbot/reports/'):
        i=int(path.split('/')[2]); return dict(summary=dict(session_type='startup' if i==2 else 'fastbot',duration_seconds=1800,total_events=2680,crash_count=0,anr_count=0,avg_cpu=12.4,avg_memory=214.5,avg_fps=58.8,jank_count=3,performance_monitor_enabled=True,jank_frame_monitor_enabled=True,local_replay_enabled=False),performance_data=[dict(timestamp=f'10:{j:02d}:00',cpu=10+j%7,memory=214+j%9,fps=58+j%3,battery=86-j//10) for j in range(30)],jank_data=[],jank_events=[],trace_artifacts=[],crash_events=[],startup_data=[])
    if path=='compatibility/page-sets': return PAGESETS
    if path=='compatibility/runs': return page(COMPAT,request)
    if path.startswith('compatibility/runs/'): return COMPAT[(int(path.split('/')[2])-1)%6]
    if path=='inspections/profiles': return PROFILES
    if path=='inspections/runs': return page(INSPECTIONS,request)
    if path.startswith('inspections/runs/'):
        tail=path.split('/')[-1]
        if tail=='graph': return dict(schema_version=6,hierarchy_version=1,nodes=[],links=[],tree={},stats=dict(stable_count=7,state_count=9,transition_count=16),summary={})
        if tail=='families': return []
        if tail=='live': return dict(status='COMPLETED',events=[])
        return INSPECTIONS[(int(path.split('/')[2])-1)%4]
    if path=='api-testing/folders': return [dict(id=i,name=n,kind=kind,parent_id=None) for i,n in enumerate(['基础服务','交易流程','用户中心'],1) for kind in ['interface','scenario']]
    if path=='api-testing/notification-status': return dict(configured=False)
    if path=='api-testing/ai/status': return dict(enabled=True,configured=True,available=True)
    if path=='api-testing/precheck': return dict(errors=[],warnings=[])
    if path=='api-testing/ai/explain-failure': return dict(facts=[dict(id='F1',step_id='step-1',message='订单状态与预期不一致',path=['body','data','status'])],possible_causes=[dict(text='隔离示例返回了 created，预期为 paid。',evidence_ids=['F1'])],next_steps=[dict(text='核对订单支付流程与断言约定。',evidence_ids=['F1'])],warnings=[])
    if path=='api-testing/debug-sessions' and method=='POST':
        key=f'preview-debug-{len(DEBUG_SESSIONS)+1}';DEBUG_SESSIONS[key]=dict(id=key,status='IDLE',results={},error='');return DEBUG_SESSIONS[key]
    if path.startswith('api-testing/debug-sessions/'):
        key=path.split('/')[2];session=DEBUG_SESSIONS.get(key)
        if session is None: raise HTTPException(404,'调试会话不存在')
        if method=='DELETE': DEBUG_SESSIONS.pop(key);return dict(ok=True)
        if path.endswith('/execute'):
            draft=body.get('steps',[]);target=body.get('step_id');selected=draft if body.get('mode')=='through' else [step for step in draft if step['id']==target]
            for step in selected:
                session['results'][step['id']]=dict(step_id=step['id'],name=step['name'],status='PASS',duration_ms=42,detail=dict(request=step['snapshot']['request'],response=dict(status_code=200,headers={'Content-Type':'application/json'},body={'code':0,'data':{'id':42,'status':'created'}}),assertions=[dict(id='status',path=['status_code'],op='is_2xx',passed=True,actual=200,expected=None)],fields=[]))
            session['status']='COMPLETED'
        if path.endswith('/assertions'): return dict(result=session['results'].get(body.get('step_id'),{}))
        return session
    if '/schedules' in path and path.startswith('api-testing/'): return []
    if path.endswith('/download') or path=='report-assets/preview-report.html':
        return HTMLResponse('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>隔离报告</title><body><h1>隔离预览报告</h1><p>模拟执行已完成，不连接真实设备。</p></body></html>')
    if path=='api-testing/runs': return page(API_RUNS,request)
    if path.startswith('api-testing/runs/'):
        row=copy.deepcopy(next((r for r in API_RUNS if r['id']==path.split('/')[2]),API_RUNS[0]))
        row['steps']=[dict(id=1,step_id='step-1',position=0,name='查询订单',status=row['status'],duration_ms=430,detail=dict(request=dict(method='GET',url='https://api.demo.test/orders',headers={}),response=dict(status_code=200,headers={'Content-Type':'application/json'},body={'code':0,'data':{'id':42,'status':'created'}}),assertions=[dict(id='status',path=['status_code'],op='is_2xx',passed=True,actual=200,expected=None)]))];row['results']=row['steps'];return row
    # Local-memory CRUD for editors and asset lists. No application service imports.
    for prefix,rows in [('cases',CASES),('scenarios',SCENARIOS),('api-testing/interfaces',INTERFACES),('api-testing/scenarios',API_SCENARIOS)]:
        if path==prefix:
            if method=='POST':
                row={**common(max(r['id'] for r in rows)+1,body.get('name','新建记录')),**body};rows.append(row);return row
            return page(rows,request)
        if path.startswith(prefix+'/'):
            parts=path[len(prefix)+1:].split('/'); i=int(parts[0]);row=next((r for r in rows if r['id']==i),None)
            if row is None: raise HTTPException(404,'隔离示例不存在')
            tail=parts[-1]
            if tail=='precheck': return dict(ok=True,has_runnable_steps=True,has_runnable_cases=True,global_checks=[],steps=[],cases=[])
            if tail in {'run','run-batch','runs'}: return dict(id='preview-api-1',batch_id='preview-batch-1',run_ids=[1],execution_ids=[1],runs=[dict(queued=True,queue_position=2)],blocked_prechecks=[])
            if tail=='sync-preview': return dict(changes=[],added=[],removed=[],modified=[])
            if tail=='steps':
                if method!='GET': row['standard_steps' if prefix=='cases' else 'scenario_steps']=body;return body
                if prefix=='cases': return row.get('standard_steps',[])
                return row.get('scenario_steps',[dict(id=j,case_id=j,order=j,alias=CASES[j-1]['name']) for j in range(1,4)])
            if method in {'PUT','PATCH'}: row.update(body);row['version']=row.get('version',0)+1
            if method=='DELETE': rows.remove(row);return dict(ok=True)
            return row
    if method!='GET': return dict(success=True,message='隔离模拟操作完成',id=99,items=[])
    raise HTTPException(404,f'No isolated fixture for {path}')


@app.websocket('/ws/run/{case_id}')
@app.websocket('/api/scenarios/ws/run/{case_id}')
async def simulated_execution(socket: WebSocket, case_id: str):
    if socket.client and socket.client.host not in {'127.0.0.1','::1','testclient'}:
        await socket.close(code=1008);return
    await socket.accept()
    await socket.send_json(dict(type='run_start',run_id=99,execution_id=99,batch_id='preview-batch',case_name='隔离模拟执行',device_serial=DEVICES[0]['serial'],total_steps=3,timestamp=NOW))
    await asyncio.sleep(.3)
    await socket.send_json(dict(type='run_complete',success=True,status='PASS',passed=3,failed=0,total_duration=.3,report_id='preview-report.html'))
    await socket.close()

app.mount('/assets',StaticFiles(directory=DIST/'assets'),name='assets')

@app.get('/{path:path}')
def spa(path: str):
    if path.startswith('api/'): raise HTTPException(404,'No preview route')
    return FileResponse(DIST/'index.html')
