"""Rich report fixtures for the isolated UI preview. No routers, database or I/O clients.

Integrate with ui_preview_app: create reports = make_report_fixtures(NOW, DEVICES),
replace FASTBOT/COMPAT from it, then call report_fixture_response(path,
request.method, request.query_params, reports, SVG) before generic report branches.
A None result means this module does not handle the request.
"""
from copy import deepcopy
from fastapi.responses import Response


def make_report_fixtures(now, devices):
    serials = [item['serial'] for item in devices[:2]]
    android = serials[0]
    trace = dict(path='preview/slow-launch.perfetto-trace', capture_mode='diagnostic', time='10:24:08',
                 analysis_status='ANALYZED', startup_mode='cold', ai_summary='### 诊断结论\n主线程布局耗时偏高。\n\n### 建议\n拆分初始化任务并复查首帧时间。\n\n此结果为隔离示例。',
                 analysis=dict(suspected_causes=[dict(title='主线程长任务', detail='隔离示例：布局计算 82 ms')],
                               top_busy_threads=[dict(thread_name='main', running_ms=82)], level='WARNING'))
    summary = dict(session_type='fastbot', duration_seconds=1800, total_events=2680, total_crashes=1,
                   total_anrs=1, avg_cpu=12.4, max_cpu=34.2, avg_mem=214.5, max_mem=286.2,
                   performance_monitor_enabled=True, jank_frame_monitor_enabled=True, local_replay_enabled=True,
                   active_avg_jank_rate=0.024, max_jank_rate=0.09, severe_jank_events=2, analyzed_trace_count=1,
                   peak_jank_rate_window={'time':'10:24:08'},
                   verdict=dict(level='FAIR',label='需关注',reason='个别页面切换出现卡顿，发现 1 次崩溃。',suggestion='先检查异常日志和首帧初始化耗时。'))
    startup_summary = dict(summary, session_type='startup', success_rate=0.9, slow_count=1,
                          startup_config=dict(startup_modes=['cold','hot'],iterations=5,cooldown_sec=2,
                                              resolved_component='com.demo.shop/.MainActivity',ready_check={'enabled':True},
                                              perfetto_slow_trace={'cold_threshold_ms':2000,'hot_threshold_ms':600}),
                          startup_aggregate={'cold':{'ready_p90_ms':2340,'p90_ms':1850},'hot':{'ready_p90_ms':480,'p90_ms':350}},
                          startup_runs=[dict(mode='cold' if i%2 else 'hot',iteration=i,success=i!=3,
                                             total_time_ms=2350 if i==3 else 400+i*70,this_time_ms=320,wait_time_ms=415,
                                             ready_time_ms=2490 if i==3 else 500+i*50,ready_status='FOUND',
                                             activity='com.demo.shop/.MainActivity',error='示例：首帧超时' if i==3 else '') for i in range(1,11)],
                          slow_events=[dict(mode='cold',iteration=3,total_time_ms=2350,threshold_ms=2000,
                                            trace_path=trace['path'],diagnosis_status='ANALYZED')])
    fastbot = [dict(id=i,name=f'商城稳定性 · {i}',package_name='com.demo.shop',device_serial=android,
                    status='COMPLETED',duration=30,minutes=30,started_at=now,finished_at=now,created_at=now,
                    total_crashes=1,total_anrs=1,report_ready=True,executor_name='林序',
                    summary=deepcopy(startup_summary if i==2 else summary)) for i in range(1,7)]
    fastbot_reports={str(i):dict(summary=deepcopy(startup_summary if i==2 else summary),
                      performance_data=[dict(time=f'10:24:{j:02d}',cpu=10+j%7,mem=214+j%9) for j in range(30)],
                      jank_data=[dict(time=f'10:24:{j:02d}',jank_rate=.09 if j==8 else .015,fps=46 if j==8 else 59,source='framestats') for j in range(30)],
                      jank_events=[dict(time='10:24:08',severity='SEVERE',jank_rate=.09,fps=46,total_frames=60,janky_frames=5,activity='首页')],
                      crash_events=[dict(time='10:24:08',type='CRASH',full_log='FATAL EXCEPTION: main\njava.lang.IllegalStateException: 隔离界面验收示例\n    at PreviewActivity.render(PreviewActivity.kt:42)',local_replay={'status':'UNAVAILABLE','error':'隔离示例未提供实际设备视频'})],
                      trace_artifacts=[deepcopy(trace)]) for i in range(1,7)}
    pages=[dict(id=1,key='home',name='首页'),dict(id=2,key='profile',name='会员中心与超长标题测试')]
    compat=[]
    for i in range(1,7):
        cells=[]
        for n,serial in enumerate(serials):
            results=[dict(id=100+n*10+j,page_key=page['key'],page_name=page['name'],status='WARNING' if n and j else 'PASS',
                          baseline_screenshot_path='preview.svg',candidate_screenshot_path='preview.svg',diff_screenshot_path='preview.svg',
                          metrics={'ssim':.92 if n and j else .99,'pixel_diff_ratio':.08 if n and j else .01,'same_resolution':not n},
                          message='示例：文字布局轻微差异' if n and j else '页面一致',duration_ms=420) for j,page in enumerate(pages)]
            cells.append(dict(id=n+1,device_serial=serial,device_name=devices[n]['model'],is_baseline=n==0,status='PASS',pages=results,current_stage='DONE'))
        compat.append(dict(id=i,name=f'核心页面兼容性 · {i}',status='WARNING',created_at=now,started_at=now,
                           execution_mode='install',source_type='page_set',mode='clean',compare_mode='device',
                           baseline_device_serial=android,total_cells=2,pass_count=3,warning_count=1,fail_count=0,
                           page_set={'name':'商城核心页面','pages':pages},package_name='com.demo.shop',cells=cells,executor_name='林序'))
    nodes=[dict(state_id=i,id=i,display_index=i,display_name=name,display_label=f'P{i:03d}',branch_key='guest',
                activity='com.demo.shop/.MainActivity',page_role='PAGE',depth=i-1,reachability_evidence='OBSERVED',
                replay_eligibility='ELIGIBLE',screenshot_path='preview.svg',xml_path='preview.xml',
                thumbnail_path='preview.svg',image_width=360,image_height=780,observation_count=2,
                representative_observation_id=i,created_at=now,captured_at=now,selected_for_regression=True,
                expansion_status='COMPLETED',expanded_at=now,non_navigation_actions=[])
           for i,name in enumerate(['商城首页','账号登录','会员中心'],1)]
    links=[dict(id=i,source=i,target=i+1,from_state_id=i,to_state_id=i+1,sequence=i,status='SUCCESS',
                action_type='click',target_label='登录' if i==1 else '我的',duration_ms=420,
                locator={'by':'text','value':'登录'},branch_key='guest',topology_type='TREE') for i in range(1,3)]
    graph=dict(schema_version=6,hierarchy_version=1,nodes=nodes,links=links,tree={},
               stats=dict(states=3,stable_count=3,transitions=2,actual_device_actions=2),
               summary=dict(replay={'total':2,'verified':1,'observed':1}),coverage_assessment={})
    return dict(fastbot=fastbot,fastbot_reports=fastbot_reports,compat=compat,graph=graph,now=now)


def report_fixture_response(path, method, query, fixtures, svg):
    parts=path.strip('/').split('/')
    if path=='reports/flaky':
        row=dict(scenario_id=1,scenario_name='账号登录回归',total=20,passed=12,failed=8,pass_rate=60,flip_count=7,score=56,last_status='FAIL',last_time=fixtures['now'])
        return dict(min_samples=5,items=[row],step_items=[dict(row,step_name='[账号登录] 断言欢迎文本')])
    if path=='reports/executions/compare':
        meta=dict(id=1,status='PASS',start_time=fixtures['now'],duration=1.2,device_serial='preview-android-1',device_info='Pixel 9',executor_name='林序')
        return dict(scenario_name='账号登录回归',base=meta,target=dict(meta,id=3,status='FAIL',duration=5.4),
                    summary=dict(regressed=1,fixed=0,still_failing=0,unchanged=2,added=0,removed=0),
                    steps=[dict(step_order=3,step_name='[账号登录] 断言欢迎文本',change='regressed',diff_type='regressed',
                                base={'status':'PASS','duration':1.2,'error_message':''},
                                target={'status':'FAIL','duration':5.4,'error_message':'未找到欢迎文本','error_code':'ASSERT_TIMEOUT'})])
    if path.startswith('fastbot/reports/'):
        if parts[-1]=='trace_ai_summary': return {'success':True,'analysis_result':'### 诊断结论\n隔离示例：建议减少主线程初始化任务。','cached':True,'token_usage':0}
        return deepcopy(fixtures['fastbot_reports'].get(parts[2],fixtures['fastbot_reports']['1']))
    return None
