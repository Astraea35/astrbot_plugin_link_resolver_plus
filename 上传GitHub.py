import os
import subprocess
import sys
from pathlib import Path

# 设置你的 GitHub 用户名
GITHUB_USER = "Astraea35"


def run_command(cmd_list, ignore_error=False):
    """运行 Git 命令并在控制台实时输出"""
    result = subprocess.run(cmd_list, text=True)
    if result.returncode != 0 and not ignore_error:
        print(f"⚠️ 命令执行失败: {' '.join(cmd_list)}")
    return result.returncode == 0


def main():
    # 自动安全获取当前文件夹名称
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

    # 即使没有新文件提交也不中断脚本
    run_command(["git", "commit", "-m", "feat: auto sync/update"], ignore_error=True)

    # 4. 推送到 GitHub
    print("\n[4/4] 强行推送到 GitHub...")
    success = run_command(["git", "push", "-u", "origin", "main", "--force"])

    print("\n" + "=" * 55)
    if success:
        print("  🎉 推送完成！")
    else:
        print("  ❌ 推送失败，请检查网络或是否已在 GitHub 创建该仓库。")
    print("=" * 55)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\n❌ 程序发生异常: {e}")
    finally:
        # 防止窗口直接闪退
        input("\n按回车键 (Enter) 退出...")