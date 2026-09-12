# 发布到 GitHub

本插件已经可以直接发布，仓库需要的文件都已就位。下面是完整步骤。

## 仓库里应该有什么

```
comfyui-sysmon/
├── .github/workflows/tests.yml   # CI：3 个 Python 版本跑后端测试 + 前端静态检查
├── .gitignore                    # 忽略 logs/、sysmon_config.json、__pycache__
├── LICENSE                       # MIT
├── README.md                     # 中文文档（含实测数据与兼容性说明）
├── pyproject.toml                # ComfyUI Registry / 打包元数据
├── __init__.py
├── sysmon/                       # 后端
├── web/                          # 前端面板
└── tests/                        # 测试
```

**确认不会提交敏感信息**：`.gitignore` 已排除 `sysmon_config.json`（里面存 API Key）
和 `logs/`（运行记录）。首次 `git add` 后建议再执行一次下面的自查。

## 一次性准备

```powershell
# 1. 安装 Git（本机目前没有）
winget install --id Git.Git -e --source winget
# 装完重开一个终端，或刷新 PATH：
$env:Path = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [Environment]::GetEnvironmentVariable("Path","User")

# 2. 基本身份（只需一次）
git config --global user.name  "你的名字"
git config --global user.email "你的邮箱"
```

## 初始化并首次提交

```powershell
cd "C:\Users\32337\AppData\Local\Comfy-Desktop\ComfyUI-Installs\ComfyUI_Start\ComfyUI\custom_nodes\comfyui-sysmon"

git init -b main
git add .
git status                      # 确认列表里没有 sysmon_config.json / logs/
git commit -m "feat: ComfyUI system monitor with per-node peak attribution

- live GPU/VRAM/CPU/RAM/disk sampling (pynvml -> nvidia-smi -> torch fallback)
- per-node peak attribution via ComfyUI's ProgressRegistry
- run duration + traceback logging with JSON persistence
- optional AI diagnosis via DeepSeek / any OpenAI-compatible endpoint
- zero runtime dependencies; 118 backend assertions + web static checks"
```

## 建远程仓库并推送

先在 GitHub 网页上建一个**空仓库**（不要勾选 README / .gitignore / License，否则会冲突），
然后：

```powershell
git remote add origin https://github.com/<你的用户名>/comfyui-sysmon.git
git branch -M main
git push -u origin main
```

也可以直接用 GitHub CLI（`winget install --id GitHub.cli -e`）：

```powershell
gh auth login
gh repo create comfyui-sysmon --public --source=. --remote=origin --push
```

## 提交前自查（建议每次都做）

```powershell
# 有没有把 Key 或日志带进版本库
git ls-files | Select-String -Pattern 'sysmon_config|logs/'
# 仓库里有没有出现 key 样式的字符串
git grep -n -I -E 'sk-[A-Za-z0-9]{16,}' -- . ':!README.md'
```

两条都**没有输出**才算干净。

## 发布后（可选）

- **ComfyUI Registry**：填好 `pyproject.toml` 里的 `PublisherId` 后，
  用 `comfy node publish` 发布，用户即可通过 ComfyUI-Manager 安装。
- **README 里的占位符**：把 `<your-name>` / `<your-publisher-id>` 换成实际值。
- 想让别人一眼看懂效果，建议把面板截图放到 `docs/panel.png`，
  并在 README 顶部引用（`.gitignore` 已允许 `docs/*.png` 被提交）。
