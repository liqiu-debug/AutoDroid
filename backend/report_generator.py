"""
HTML 报告生成器
使用 Jinja2 渲染测试报告
"""
import os
import base64
from datetime import datetime
from typing import List, Dict, Any, Optional
from jinja2 import Environment, FileSystemLoader
import uuid

from backend.paths import project_path


class ReportGenerator:
    """HTML 测试报告生成器"""
    
    def __init__(self):
        templates_dir = project_path("backend", "templates")
        self.env = Environment(loader=FileSystemLoader(str(templates_dir)))
        self.reports_dir = str(project_path("reports"))
        os.makedirs(self.reports_dir, exist_ok=True)
    
    def generate_report(
        self,
        case_id: int,
        case_name: str,
        steps_results: List[Dict[str, Any]],
        start_time: datetime,
        end_time: datetime,
        variables: List[Dict[str, str]] = None
    ) -> str:
        """
        生成 HTML 报告
        
        Args:
            case_id: 用例 ID
            case_name: 用例名称
            steps_results: 步骤执行结果列表
            start_time: 开始时间
            end_time: 结束时间
            variables: 变量列表
            
        Returns:
            report_id: 报告文件名（不含路径）
        """
        template = self.env.get_template("report.html")
        
        # 计算统计信息
        total = len(steps_results)
        passed = sum(1 for s in steps_results if s.get("status") == "success")
        failed = sum(1 for s in steps_results if s.get("status") == "failed")
        skipped = total - passed - failed
        
        total_duration = (end_time - start_time).total_seconds()
        pass_rate = (passed / total * 100) if total > 0 else 0
        
        # 渲染模板
        html_content = template.render(
            case_id=case_id,
            case_name=case_name,
            start_time=start_time.strftime("%Y-%m-%d %H:%M:%S"),
            end_time=end_time.strftime("%Y-%m-%d %H:%M:%S"),
            total_duration=round(total_duration, 2),
            total_steps=total,
            passed=passed,
            failed=failed,
            skipped=skipped,
            pass_rate=round(pass_rate, 1),
            steps=steps_results,
            variables=variables or [],
            generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )
        
        # 保存报告
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_id = f"report_{case_id}_{timestamp}.html"
        report_path = os.path.join(self.reports_dir, report_id)
        
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(html_content)
        
        return report_id
    
    def get_report_path(self, report_id: str) -> Optional[str]:
        """获取报告文件路径"""
        report_path = os.path.join(self.reports_dir, report_id)
        if os.path.exists(report_path):
            return report_path
        return None
    
    def generate_scenario_report(
        self,
        scenario_id: int,
        scenario_name: str,
        cases_results: List[Dict[str, Any]],
        start_time: datetime,
        end_time: datetime
    ) -> str:
        """
        生成场景测试报告
        
        Args:
            scenario_id: 场景 ID
            scenario_name: 场景名
            cases_results: 用例执行结果列表 (包含 steps 详情)
            start_time: 开始时间
            end_time: 结束时间
            
        Returns:
            report_id: 报告文件名
        """
        template = self.env.get_template("scenario_report.html")
        
        # 统计信息
        total_cases = len(cases_results)
        passed_cases = sum(1 for c in cases_results if c.get("status") == "success")
        failed_cases = sum(1 for c in cases_results if c.get("status") == "failed")
        
        total_duration = (end_time - start_time).total_seconds()
        pass_rate = (passed_cases / total_cases * 100) if total_cases > 0 else 0
        
        # 渲染模板
        html_content = template.render(
            scenario_id=scenario_id,
            scenario_name=scenario_name,
            start_time=start_time.strftime("%Y-%m-%d %H:%M:%S"),
            end_time=end_time.strftime("%Y-%m-%d %H:%M:%S"),
            total_duration=round(total_duration, 2),
            total_cases=total_cases,
            passed_cases=passed_cases,
            failed_cases=failed_cases,
            pass_rate=round(pass_rate, 1),
            cases_results=cases_results,
            generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )
        
        # 保存报告
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_id = f"report_scenario_{scenario_id}_{timestamp}.html"
        report_path = os.path.join(self.reports_dir, report_id)
        
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(html_content)
        
        return report_id

    def list_reports(self, case_id: int = None) -> List[Dict[str, Any]]:
        """列出所有报告"""
        reports = []
        for filename in os.listdir(self.reports_dir):
            if filename.endswith(".html"):
                # 解析文件名
                # 1. 场景报告: report_scenario_{id}_{timestamp}.html
                # 2. 用例报告: report_{id}_{timestamp}.html
                
                report_type = "case"
                file_id = 0
                timestamp = ""
                
                parts = filename.replace(".html", "").split("_")
                
                if len(parts) >= 4 and parts[1] == "scenario":
                    report_type = "scenario"
                    file_id = int(parts[2])
                    timestamp = "_".join(parts[3:])
                elif len(parts) >= 3:
                    file_id = int(parts[1])
                    timestamp = "_".join(parts[2:])
                
                # 过滤逻辑 (目前仅支持按 case_id 过滤 case 报告，场景报告暂时全显或不显)
                # 为简单起见，如果传了 case_id，只返回对应的 case 报告
                if case_id is not None:
                     if report_type != "case" or file_id != case_id:
                         continue

                report_path = os.path.join(self.reports_dir, filename)
                reports.append({
                    "report_id": filename,
                    "type": report_type,
                    "related_id": file_id, # case_id or scenario_id
                    "timestamp": timestamp,
                    "size": os.path.getsize(report_path),
                    "created": datetime.fromtimestamp(
                        os.path.getctime(report_path)
                    ).isoformat()
                })
        
        # 按创建时间倒序
        reports.sort(key=lambda x: x["created"], reverse=True)
        return reports


# 全局实例
report_generator = ReportGenerator()
