import os
import subprocess
import sys
import json
import urllib.request
import urllib.error
from pathlib import Path

# 设置你的 GitHub 用户名
GITHUB_USER = "Astraea35"

# 【可选】填写你的 GitHub Personal Access Token (PAT)
# 如果填写了 Token，当 GitHub 仓库不存在时，脚本会自动为你调用 API 创建仓库！
GITHUB_TOKEN = "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"  # 例如: "ghp_xxxxxxxxxxxxxxxxxxxx"


def run_command(cmd_list, ignore_error=False):
    """运行 Git 命令并在控制台输出"""
    result = subprocess.run(cmd_list, text=True)
    if result.returncode != 0 and not ignore_error:
        print(f"⚠️ 命令执行失败: {' '.join(cmd_list)}")
    return result.returncode == 0


def create_github_repo(token, repo_name):
    """通过 GitHub API 自动创建远程仓库"""
    url = "https://api.github.com/user/repos"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "Python-Script"
    }
    data = json.dumps({
        "name": repo_name,
        "private": False,
        "auto_init": False
    }).encode("utf-8")

    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req) as response:
            if response.status in (200, 201):
                print(f"✅ 已通过 GitHub API 自动创建远程仓库: {repo_name}")
                return True
    except urllib.error.HTTPError as e:
        print(f"❌ 自动创建仓库失败 (HTTP {e.code}): {e.read().decode('utf-8')}")
    except Exception as e:
        print(f"❌ 自动创建仓库发生异常: {e}")
    return False


def main():
    current_dir = Path.cwd()
    repo_name = current_dir.name
    remote_url = f"https://github.com/{GITHUB_USER}/{repo_name}.git"

    print("=" * 55)
    print(f"正在处理插件仓库: {repo_name}")
    print(f"对应 GitHub 地址: {remote_url}")
    print("=" * 55)

    # 1. 自动补全全局配置
    run_command(["git", "config", "--global", "user.name", GITHUB_USER])

    # 2. 检查并初始化 Git 仓库
    git_folder = current_dir / ".git"
    if not git_folder.exists():
        print("\n[1/4] 初始化本地 Git 仓库...")
        run_command(["git", "init"])
        run_command(["git", "branch", "-M", "main"])

        print("\n[2/4] 关联远程 GitHub 仓库...")
        run_command(["git", "remote", "add", "origin", remote_url])
    else:
        print("\n[1/4] 已检测到 Git 仓库，跳过初始化...")

    # 3. 添加与提交
    print("\n[3/4] 添加文件并提交...")
    run_command(["git", "add", "."])
    run_command(["git", "commit", "-m", "feat: auto sync/update"], ignore_error=True)

    # 4. 推送到 GitHub
    print("\n[4/4] 推送到 GitHub...")
    success = run_command(["git", "push", "-u", "origin", "main", "--force"], ignore_error=True)

    # 如果推送失败且配置了 Token，尝试自动建仓后二次推送
    if not success:
        if GITHUB_TOKEN:
            print("\n⚙️ 检测到推送失败，尝试使用 Token 自动创建 GitHub 仓库...")
            if create_github_repo(GITHUB_TOKEN, repo_name):
                print("🚀 仓库创建成功，重新尝试推送...")
                success = run_command(["git", "push", "-u", "origin", "main", "--force"])
        else:
            print("\n💡 提示: 未配置 GITHUB_TOKEN，请先在 GitHub 网页手动创建同名空白仓库后再试。")

    print("\n" + "=" * 55)
    if success:
        print("  🎉 推送完成！")
    else:
        print("  ❌ 推送失败，请确保已在 GitHub 上创建该仓库。")
    print("=" * 55)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\n❌ 程序发生异常: {e}")
    finally:
        input("\n按回车键 (Enter) 退出...")