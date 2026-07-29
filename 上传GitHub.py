import os
import subprocess
import sys
import json
import re
import urllib.request
import urllib.error
from pathlib import Path

# 强制设置 Python 在 Windows 终端输出 UTF-8，防止控制台打印崩溃
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# 设置你的 GitHub 用户名
GITHUB_USER = "Astraea35"

# 填写你的 GitHub Personal Access Token (PAT)
GITHUB_TOKEN = ""  # 填入你的 ghp_xxxxxxxx，不填则仅在 GitHub 已建仓时推送


def run_command(cmd_list, ignore_error=False):
    """运行 Git 命令并在控制台输出（防编码崩溃）"""
    try:
        result = subprocess.run(
            cmd_list, 
            text=True, 
            encoding="utf-8", 
            errors="replace"
        )
        if result.returncode != 0 and not ignore_error:
            print(f"⚠️ 命令执行失败: {' '.join(cmd_list)}")
        return result.returncode == 0
    except Exception as e:
        if not ignore_error:
            print(f"⚠️ 执行命令 {' '.join(cmd_list)} 发生异常: {e}")
        return False


def get_plugin_info():
    """读取 metadata.yaml 中的版本号和描述"""
    current_dir = Path.cwd()
    meta_path = current_dir / "metadata.yaml"
    version = "1.0.0"
    desc = "AstrBot 插件"

    if meta_path.exists():
        try:
            content = meta_path.read_text(encoding="utf-8", errors="ignore")
            # 提取 version
            ver_match = re.search(r"version:\s*['\"]?([^'\"\n]+)['\"]?", content, re.MULTILINE)
            if ver_match:
                version = ver_match.group(1).strip()
            # 提取 desc
            desc_match = re.search(r"desc:\s*['\"]?([^'\"\n]+)['\"]?", content, re.MULTILINE)
            if desc_match:
                desc = desc_match.group(1).strip()
        except Exception as e:
            print(f"⚠️ 读取 metadata.yaml 时发生小错误: {e}")

    return version, desc


def update_project_version(old_ver, new_ver):
    """同步修改 metadata.yaml 和 main.py 中的版本号"""
    current_dir = Path.cwd()
    updated_files = []

    # 1. 更新 metadata.yaml
    meta_path = current_dir / "metadata.yaml"
    if meta_path.exists():
        try:
            content = meta_path.read_text(encoding="utf-8", errors="ignore")
            new_content = re.sub(
                r"(version:\s*['\"]?)[^'\"\n]+(['\"]?)",
                rf"\g<1>{new_ver}\g<2>",
                content,
                flags=re.MULTILINE
            )
            meta_path.write_text(new_content, encoding="utf-8")
            updated_files.append("metadata.yaml")
        except Exception as e:
            print(f"⚠️ 修改 metadata.yaml 版本号失败: {e}")

    # 2. 更新 main.py (匹配 @register 装饰器中的版本号)
    main_path = current_dir / "main.py"
    if main_path.exists():
        try:
            content = main_path.read_text(encoding="utf-8", errors="ignore")
            lines = content.splitlines()
            new_lines = []
            replaced = False
            for line in lines:
                if "@register" in line and old_ver in line:
                    line = line.replace(f'"{old_ver}"', f'"{new_ver}"').replace(f"'{old_ver}'", f"'{new_ver}'")
                    replaced = True
                new_lines.append(line)
            if replaced:
                main_path.write_text("\n".join(new_lines), encoding="utf-8")
                updated_files.append("main.py")
        except Exception as e:
            print(f"⚠️ 修改 main.py 版本号失败: {e}")

    if updated_files:
        print(f"✨ 已同步更新版本号 [{old_ver} ➔ {new_ver}] 到文件: {', '.join(updated_files)}")


def create_github_repo(token, repo_name, description):
    """通过 GitHub API 自动创建远程仓库并写入 Description"""
    url = "https://api.github.com/user/repos"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "Python-Script"
    }
    data = json.dumps({
        "name": repo_name,
        "description": description,
        "private": False,
        "auto_init": False
    }).encode("utf-8")

    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req) as response:
            if response.status in (200, 201):
                print(f"✅ 已成功自动创建远程仓库: {repo_name}")
                print(f"📝 自动填入仓库描述: {description}")
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
    
    # 自动识别当前版本和简介
    current_ver, description = get_plugin_info()

    print("=" * 55)
    print(f"正在处理插件仓库: {repo_name}")
    print(f"当前检测到的版本: v{current_ver}")
    print(f"自动抓取的简介: {description}")
    print(f"对应 GitHub 地址: {remote_url}")
    print("=" * 55)

    # 1. 交互询问版本号
    print(f"\n🔢 当前版本号为: [{current_ver}]")
    new_ver_input = input("   请输入新版本号 (例如 1.3.1，直接回车保留当前版本): ").strip()
    
    if new_ver_input and new_ver_input != current_ver:
        update_project_version(current_ver, new_ver_input)
        active_ver = new_ver_input
    else:
        active_ver = current_ver

    # 2. 交互询问日志说明
    print(f"\n📝 请输入本次更新日志 / 提交说明：")
    user_log = input(f"   (直接回车默认使用 'v{active_ver} 更新'): ").strip()
    commit_msg = user_log if user_log else f"v{active_ver} 更新"

    # 3. Git 基础设置
    run_command(["git", "config", "--global", "user.name", GITHUB_USER])

    # 4. 初始化 Git 仓库
    git_folder = current_dir / ".git"
    if not git_folder.exists():
        print("\n[1/4] 初始化本地 Git 仓库...")
        run_command(["git", "init"])
        run_command(["git", "branch", "-M", "main"])

        print("\n[2/4] 关联远程 GitHub 仓库...")
        run_command(["git", "remote", "add", "origin", remote_url])
    else:
        print("\n[1/4] 已检测到 Git 仓库，跳过初始化...")

    # 5. 添加与提交
    print("\n[3/4] 添加文件并提交...")
    run_command(["git", "add", "."])
    print(f"📌 当前提交说明: [{commit_msg}]")
    run_command(["git", "commit", "-m", commit_msg], ignore_error=True)

    # 6. 推送到 GitHub
    print("\n[4/4] 推送到 GitHub...")
    success = run_command(["git", "push", "-u", "origin", "main", "--force"], ignore_error=True)

    # 仓库不存在时，触发自动建仓逻辑
    if not success:
        if GITHUB_TOKEN:
            print("\n⚙️ 检测到仓库不存在，正在自动创建 GitHub 仓库及填写 Description...")
            if create_github_repo(GITHUB_TOKEN, repo_name, description):
                print("🚀 仓库创建成功，重新尝试推送...")
                success = run_command(["git", "push", "-u", "origin", "main", "--force"])
        else:
            print("\n💡 提示: 未配置 GITHUB_TOKEN，请先在 GitHub 网页手动创建同名空白仓库后再试。")

    print("\n" + "=" * 55)
    if success:
        print("  🎉 推送完成！")
    else:
        print("  ❌ 推送失败，请检查网络或是否配置了有效的 Token。")
    print("=" * 55)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        print(f"\n❌ 程序发生致命异常:")
        traceback.print_exc()
    finally:
        input("\n按回车键 (Enter) 退出...")