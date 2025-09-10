#!/usr/bin/env python3
# =============================================================================
# 实例：harmony项目v1.1.0版本发布流程验证脚本
# 功能：验证harmony仓库v1.1.0版本发布的分支、核心文件完整性、发布PR及合并方式合规性
# 依赖: requests, python-dotenv (安装：pip install requests python-dotenv)
# 使用说明：1. 配置.env.release文件；2. 确保仓库存在release-v1.1.0分支及相关文件；3. 直接运行脚本
# =============================================================================

import sys
import os
import requests
import base64
import re
from typing import Dict, List, Optional, Tuple
from dotenv import load_dotenv


# --------------------------
# 1. 通用工具函数（已替换占位符为harmony项目实际配置）
# --------------------------
def _load_env() -> Tuple[Optional[str], Optional[str]]:
    """加载环境变量：GitHub发布令牌和harmony项目所属组织名"""
    load_dotenv(".env.release")  # 实际使用的环境变量文件（专用于发布验证）
    github_token = os.environ.get("GITHUB_RELEASE_TOKEN")  # 发布专用令牌变量名
    github_org = os.environ.get("GITHUB_ORG_HARMONY")  # harmony项目所属组织变量名（如"my-team-harmony"）
    return github_token, github_org


def _build_headers(github_token: str) -> Dict[str, str]:
    """构建GitHub API请求头（含发布验证所需鉴权）"""
    return {
        "Authorization": f"token {github_token}",  # GitHub令牌鉴权格式（固定"token "前缀）
        "Accept": "application/vnd.github.v3+json",  # GitHub API v3稳定版格式
        "User-Agent": "harmony-release-verification-tool"  # 自定义用户代理（便于API日志识别）
    }


def _call_github_api(
    endpoint: str,
    headers: Dict[str, str],
    org: str,
    repo: str
) -> Tuple[bool, Optional[Dict]]:
    """调用GitHub API，处理响应状态码（适配harmony项目资源查询）"""
    url = f"https://api.github.com/repos/{org}/{repo}/{endpoint}"
    try:
        response = requests.get(url, headers=headers, timeout=10)  # 10秒超时防止卡壳
        if response.status_code == 200:  # 读取类接口（分支/文件/PR）成功状态码
            return True, response.json()
        elif response.status_code == 404:  # 资源未找到（如分支不存在）特殊提示
            print(f"[API 提示] {endpoint} 资源未找到（404），请确认harmony仓库是否存在该资源", file=sys.stderr)
            return False, None
        else:
            print(f"[API 错误] {endpoint} 调用失败，状态码：{response.status_code}，响应：{response.text[:200]}", file=sys.stderr)
            return False, None
    except Exception as e:
        print(f"[API 异常] 调用 {endpoint} 时出错：{str(e)}（可能是网络问题或令牌权限不足）", file=sys.stderr)
        return False, None


def _check_branch_exists(
    branch_name: str,
    headers: Dict[str, str],
    org: str,
    repo: str
) -> bool:
    """验证harmony项目指定分支是否存在（如release-v1.1.0）"""
    success, _ = _call_github_api(f"branches/{branch_name}", headers, org, repo)
    return success


def _get_file_content(
    file_path: str,
    branch: str,
    headers: Dict[str, str],
    org: str,
    repo: str
) -> Optional[str]:
    """获取harmony项目指定分支下文件的Base64解码内容（UTF-8编码）"""
    success, file_data = _call_github_api(
        f"contents/{file_path}?ref={branch}", headers, org, repo
    )
    if not success or not file_data:
        return None
    
    try:
        base64_content = file_data.get("content", "").replace("\n", "")  # 清理Base64换行符
        return base64.b64decode(base64_content).decode("utf-8")  # harmony项目文件默认UTF-8编码
    except Exception as e:
        print(f"[文件解码错误] {file_path}：{str(e)}（可能是文件编码非UTF-8）", file=sys.stderr)
        return None


def _find_merged_pr(
    pr_title_keyword: str,
    base_branch: str,
    headers: Dict[str, str],
    org: str,
    repo: str
) -> Optional[Dict]:
    """查找harmony项目已合并的发布PR（标题含关键词，合并到main分支）"""
    # 拉取最近100条已关闭PR（覆盖大部分发布场景）
    success, pr_list = _call_github_api(
        f"pulls?state=closed&base={base_branch}&per_page=100", headers, org, repo
    )
    if not success or not isinstance(pr_list, List):
        return None
    
    # 筛选：标题含关键词 + 已合并（merged_at不为空）
    for pr in pr_list:
        pr_title = pr.get("title", "").lower()
        is_merged = pr.get("merged_at") is not None
        if pr_title_keyword.lower() in pr_title and is_merged:
            return pr
    return None


def _verify_squash_merge(
    pr_number: int,
    headers: Dict[str, str],
    org: str,
    repo: str
) -> bool:
    """验证harmony项目PR是否通过Squash and Merge合并（父提交数=1）"""
    # 1. 获取PR详情，提取合并提交SHA
    success, pr_detail = _call_github_api(f"pulls/{pr_number}", headers, org, repo)
    if not success or not pr_detail:
        return False
    merge_commit_sha = pr_detail.get("merge_commit_sha")
    if not merge_commit_sha:
        print(f"[PR 错误] harmony项目PR #{pr_number} 无合并提交SHA", file=sys.stderr)
        return False
    
    # 2. 检查合并提交父节点数（Squash Merge固定1个父节点）
    success, commit_detail = _call_github_api(f"commits/{merge_commit_sha}", headers, org, repo)
    if not success or not commit_detail:
        return False
    
    parent_count = len(commit_detail.get("parents", []))
    if parent_count != 1:
        print(f"[合并方式错误] 预期Squash Merge（1个父提交），实际{parent_count}个父提交（可能是普通Merge）", file=sys.stderr)
        return False
    
    # 3. 验证提交消息含PR编号（Squash Merge默认行为）
    commit_msg = commit_detail.get("commit", {}).get("message", "")
    if f"#{pr_number}" not in commit_msg:
        print(f"[合并方式错误] 提交消息未包含PR #{pr_number}，不符合Squash Merge规范", file=sys.stderr)
        return False
    
    return True


# --------------------------
# 2. 核心验证逻辑（适配harmony项目v1.1.0发布场景）
# --------------------------
def _verify_environment() -> Tuple[Optional[str], Optional[Dict]]:
    """验证harmony项目发布所需环境变量是否配置完整"""
    print(f"[1/6] 验证harmony项目发布环境配置...")
    github_token, github_org = _load_env()
    
    # 检查令牌和组织名是否存在
    if not github_token:
        print(f"[环境错误] 未配置 GITHUB_RELEASE_TOKEN（需在.env.release中设置，令牌需含repo权限）", file=sys.stderr)
        return None, None
    if not github_org:
        print(f"[环境错误] 未配置 GITHUB_ORG_HARMONY（需在.env.release中设置，如my-team-harmony）", file=sys.stderr)
        return None, None
    
    headers = _build_headers(github_token)
    print(f"[环境就绪] harmony项目所属组织：{github_org}，发布令牌已配置")
    return github_org, headers


def _verify_branches(
    headers: Dict[str, str],
    org: str,
    repo: str
) -> bool:
    """验证harmony项目v1.1.0发布所需分支（release-v1.1.0和main）是否存在"""
    print(f"\n[2/6] 验证harmony项目发布分支存在性...")
    release_branch = "release-v1.1.0"  # v1.1.0版本发布分支
    base_branch = "main"  # 发布PR合并的基础分支
    
    # 验证发布分支
    if not _check_branch_exists(release_branch, headers, org, repo):
        print(f"[分支错误] harmony项目发布分支 {release_branch} 不存在，请先创建该分支", file=sys.stderr)
        return False
    # 验证基础分支
    if not _check_branch_exists(base_branch, headers, org, repo):
        print(f"[分支错误] harmony项目基础分支 {base_branch} 不存在（异常情况，需检查仓库完整性）", file=sys.stderr)
        return False
    
    print(f"[分支验证通过] 发布分支：{release_branch}，基础分支：{base_branch}")
    return True


def _verify_core_files(
    headers: Dict[str, str],
    org: str,
    repo: str
) -> bool:
    """验证harmony项目v1.1.0发布所需核心文件（编码/注册表/版本/日志）完整性"""
    print(f"\n[3/6] 验证harmony项目v1.1.0核心发布文件（共4个）...")
    # 核心文件配置（路径、必需内容、最小大小）
    core_files = [
        {
            "name": "编码配置文件",
            "path": "src/encoding.rs",
            "branch": "main",
            "required_content": 'FormattingToken::MetaSep => "<|meta_sep|>"',  # v1.1.0修复的MetaSep映射
            "min_size": 500  # 确保文件非空且核心逻辑完整
        },
        {
            "name": "注册表文件",
            "path": "src/registry.rs",
            "branch": "main",
            "required_contents": [
                '(FormattingToken::MetaSep, "<|meta_sep|>")',
                '(FormattingToken::MetaEnd, "<|meta_end|>")'  # v1.1.0新增的MetaEnd配置
            ],
            "min_size": 500
        },
        {
            "name": "版本配置文件",
            "path": "Cargo.toml",
            "branch": "main",
            "required_content": 'version = "1.1.0"',  # v1.1.0版本号
            "min_size": 200  # 确保Cargo配置完整
        },
        {
            "name": "变更日志文件",
            "path": "CHANGELOG.md",
            "branch": "main",
            "required_keywords": [
                "## [1.1.0] - 2025-08-07",  # v1.1.0发布日期
                "MetaSep token mapping bug",  # 修复的核心问题
                "Fixed MetaSep token",  # 修复描述
                "Registry now properly recognizes"  # 注册表修复说明
            ],
            "min_size": 300  # 确保日志记录完整
        }
    ]
    
    all_passed = True
    for file in core_files:
        print(f"\n  正在验证：{file['name']}（路径：{file['path']}，分支：{file['branch']}）")
        file_content = _get_file_content(file["path"], file["branch"], headers, org, repo)
        
        # 检查文件是否存在
        if not file_content:
            print(f"  [错误] {file['name']} 未找到或无法读取", file=sys.stderr)
            all_passed = False
            continue
        
        # 检查文件大小
        if len(file_content) < file["min_size"]:
            print(f"  [错误] {file['name']} 大小不足（实际：{len(file_content)}字节，要求≥{file['min_size']}字节）", file=sys.stderr)
            all_passed = False
            continue
        
        # 检查文件内容（单内容/多内容/多关键词）
        content_valid = True
        if "required_content" in file:
            if file["required_content"] not in file_content:
                print(f"  [错误] 缺少必需内容：{file['required_content'][:50]}...", file=sys.stderr)
                content_valid = False
        elif "required_contents" in file:
            missing = [c for c in file["required_contents"] if c not in file_content]
            if missing:
                print(f"  [错误] 缺少必需内容：{[m[:30]+'...' for m in missing]}", file=sys.stderr)
                content_valid = False
        elif "required_keywords" in file:
            missing = [k for k in file["required_keywords"] if k not in file_content]
            if missing:
                print(f"  [错误] 缺少必需关键词：{missing}", file=sys.stderr)
                content_valid = False
        
        if not content_valid:
            all_passed = False
            continue
        
        print(f"  [成功] {file['name']} 验证通过")
    
    if all_passed:
        print(f"\n[所有文件验证通过] harmony项目v1.1.0核心发布文件均符合要求")
    else:
        print(f"\n[文件验证失败] 部分核心文件不符合v1.1.0发布规范，请修正后重试", file=sys.stderr)
    return all_passed


def _verify_release_pr(
    headers: Dict[str, str],
    org: str,
    repo: str
) -> Optional[int]:
    """查找并验证harmony项目v1.1.0发布PR（标题含Release v1.1.0，已合并到main）"""
    print(f"\n[4/6] 查找harmony项目v1.1.0发布PR...")
    pr_title_keyword = "Release v1.1.0"  # 发布PR标题关键词
    base_branch = "main"
    
    release_pr = _find_merged_pr(pr_title_keyword, base_branch, headers, org, repo)
    if not release_pr:
        print(f"[PR 错误] 未找到标题含「{pr_title_keyword}」且合并到「{base_branch}」的已合并PR", file=sys.stderr)
        return None
    
    pr_number = release_pr.get("number")
    pr_title = release_pr.get("title")
    print(f"[PR 找到] 发布PR：#{pr_number}（标题：{pr_title}，合并时间：{release_pr.get('merged_at')[:10]}）")
    return pr_number


def _verify_pr_merge_target(
    pr_number: int,
    headers: Dict[str, str],
    org: str,
    repo: str
) -> bool:
    """验证harmony项目发布PR是否合并到正确的基础分支（main）"""
    print(f"\n[5/6] 验证发布PR合并目标分支...")
    expected_base = "main"
    
    # 获取PR详情
    success, pr_detail = _call_github_api(f"pulls/{pr_number}", headers, org, repo)
    if not success or not pr_detail:
        print(f"[PR 错误] 无法获取PR #{pr_number} 详情", file=sys.stderr)
        return False
    
    actual_base = pr_detail.get("base", {}).get("ref")
    if actual_base != expected_base:
        print(f"[合并目标错误] PR #{pr_number} 合并到「{actual_base}」，预期「{expected_base}」", file=sys.stderr)
        return False
    
    print(f"[合并目标验证通过] PR #{pr_number} 正确合并到main分支")
    return True


def _verify_merge_method(
    pr_number: int,
    headers: Dict[str, str],
    org: str,
    repo: str
) -> bool:
    """验证harmony项目发布PR是否通过Squash and Merge合并（v1.1.0发布规范要求）"""
    print(f"\n[6/6] 验证发布PR合并方式（需Squash and Merge）...")
    
    if _verify_squash_merge(pr_number, headers, org, repo):
        print(f"[合并方式验证通过] PR #{pr_number} 符合Squash and Merge规范")
        return True
    else:
        print(f"[合并方式错误] PR #{pr_number} 不符合v1.1.0发布的Squash and Merge要求", file=sys.stderr)
        return False


# --------------------------
# 3. 主验证流程（harmony项目v1.1.0发布入口）
# --------------------------
def run_harmony_release_verification() -> bool:
    """执行harmony项目v1.1.0版本发布全流程验证"""
    # 打印流程开始信息
    separator = "=" * 60
    print(separator)
    print("开始执行 harmony项目v1.1.0版本发布流程验证")
    print(separator)
    
    # 步骤1：环境验证
    github_org, headers = _verify_environment()
    if not github_org or not headers:
        return False
    
    # 步骤2：分支验证（release-v1.1.0和main）
    repo_name = "harmony"  # harmony项目仓库名
    if not _verify_branches(headers, github_org, repo_name):
        return False
    
    # 步骤3：核心文件验证
    if not _verify_core_files(headers, github_org, repo_name):
        return False
    
    # 步骤4：查找发布PR
    pr_number = _verify_release_pr(headers, github_org, repo_name)
    if not pr_number:
        return False
    
    # 步骤5：验证PR合并目标
    if not _verify_pr_merge_target(pr_number, headers, github_org, repo_name):
        return False
    
    # 步骤6：验证合并方式
    if not _verify_merge_method(pr_number, headers, github_org, repo_name):
        return False
    
    # 所有步骤通过，输出汇总
    print(f"\n{separator}")
    print("🎉 harmony项目v1.1.0版本发布流程所有验证步骤通过！")
    print(f"验证对象：{github_org}/{repo_name}")
    print(f"发布分支：release-v1.1.0")
    print(f"发布PR：#{pr_number}（标题：Release v1.1.0）")
    print(f"合并方式：Squash and Merge（符合发布规范）")
    print(separator)
    return True


# --------------------------
# 4. 脚本入口（直接运行即可执行验证）
# --------------------------
if __name__ == "__main__":
    # 执行harmony项目v1.1.0发布验证，返回退出码（0成功，1失败）
    verification_success = run_harmony_release_verification()
    sys.exit(0 if verification_success else 1)
